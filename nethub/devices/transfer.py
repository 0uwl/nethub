"""Getting an image onto a device, in whichever direction the deployment says.

`stage_image()` is the entry point and owns everything transport-independent:
the source cross-check, the free-space gate, the skip-if-already-staged test,
and the `verify /sha512` that both directions end with. The two adapters below
it move bytes and nothing else.

Transport is a deployment setting and never a request field (design doc
§4.3.1): selecting the transport selects whose credential gets spent. Push is
the default because a nontrivial number of deployments block the outbound
connection pull needs -- where that rule is in force pull does not degrade, it
does not work.

NetHub is the sole source of the bytes either way (§3.3). Both adapters read
the same published subtree: push off a local mount, pull via the path the
SFTP daemon exposes, which must be that same path. There is no second store
and no per-artifact directory to name.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from netmiko.cisco.cisco_ios import CiscoIosFileTransfer

from nethub.devices import facts

if TYPE_CHECKING:
    from netmiko.base_connection import BaseConnection

Transport = Literal["push_scp", "pull_sftp"]

#: `verify /sha512 (flash:packages.conf) = <128 hex>` -- captured from a real
#: 17.12.06 device. We match the digest, never the word "Verified": a bare
#: substring match on one English word passes on any message containing it.
_DIGEST_RE = re.compile(r"=\s*([0-9a-fA-F]{128})\b")

#: Names that reach a CLI command are checked as text. There is no module
#: argument handling between us and the device (netmiko.md, "what gets harder").
_IMAGE_NAME_RE = re.compile(r"[\w.+-]+")
_REMOTE_PATH_RE = re.compile(r"[\w:./-]+")
_HOSTNAME_RE = re.compile(r"[\w.:-]+")
_USERNAME_RE = re.compile(r"[\w.@-]+")

_SCP_ENABLE = "ip scp server enable"
_SCP_SHOW = "show running-config | include ^ip scp server"

#: A ~1.2 GB image over a branch link is the longest device operation there is.
TRANSFER_READ_TIMEOUT = 7200.0
#: Hashing 1.2 GB on a switch CPU is minutes, not seconds.
VERIFY_READ_TIMEOUT = 900.0
SCP_SOCKET_TIMEOUT = 60.0


class TransferError(Exception):
    """Staging failed. `status` is the operator-facing word for the reason.

    `scp_restore_confirmed` mirrors the `upgrade_host_phase_results` column
    (design doc §5): False means a push left the device's SCP server in an
    unknown state, None means no bracket ran -- either the failure came before
    it or the transport was pull, which reconfigures nothing. Null there has
    to be read together with the run's transport rather than alone.
    """

    def __init__(
        self,
        message: str,
        *,
        status: str,
        scp_restore_confirmed: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.scp_restore_confirmed = scp_restore_confirmed


class VerificationError(TransferError):
    """The bytes on the device are not the bytes we published (§7.3 `checksum`)."""


class ScpRestoreError(TransferError):
    """The device's SCP server could not be confirmed back in its prior state."""


@dataclass(frozen=True)
class StageOutcome:
    status: Literal["image_copied", "already_staged"]
    transport: Transport
    sha512: str
    #: None under pull: there was never anything to restore.
    scp_restore_confirmed: bool | None


@dataclass(frozen=True)
class PullTarget:
    """Where the device dials under `pull_sftp`. Not a second image store --
    this is an address, and the directory behind it is NetHub's own."""

    host: str
    username: str
    #: repr=False: see the note on `credential_socket._Held`.
    password: str = field(repr=False)


def stage_image(
    conn: BaseConnection,
    *,
    image: str,
    sha512: str,
    search_dir: str,
    transport: Transport,
    file_size: int | None = None,
    file_system: str = "flash:",
    pull_target: PullTarget | None = None,
    transfer_read_timeout: float = TRANSFER_READ_TIMEOUT,
    verify_read_timeout: float = VERIFY_READ_TIMEOUT,
) -> StageOutcome:
    """Put `image` in `file_system` on the device and prove it arrived intact.

    Returns without transferring if the image is already there *and* the device
    re-derives the expected digest for it. That is deliberately a hash and not
    a `dir` presence check: a file of the right name is not the file staging
    checked, and design doc §8.1 is explicit that "still verifies" for a later
    phase has to mean re-running `verify /sha512`.
    """
    if transport not in ("push_scp", "pull_sftp"):
        # An unrecognised value would otherwise skip both adapters and leave
        # the host with no image, which `verify` would then report as a
        # confusing verification failure rather than as a misconfiguration.
        raise TransferError(
            f"image_transport must be 'push_scp' or 'pull_sftp', got {transport!r}",
            status="not_copied",
        )
    _check_name(image, _IMAGE_NAME_RE, "image name")
    _check_name(file_system, _REMOTE_PATH_RE, "file system")
    source, size = resolve_source(search_dir, image, declared_size=file_size)
    expected = _normalise_digest(sha512)

    if _already_staged(conn, file_system, image, expected, size, verify_read_timeout):
        return StageOutcome("already_staged", transport, expected, None)

    _check_space(conn, file_system, image, size)

    if transport == "push_scp":
        restore_confirmed = _push_scp(
            conn,
            source=source,
            image=image,
            file_system=file_system,
            read_timeout=transfer_read_timeout,
        )
    else:
        if pull_target is None:
            raise TransferError(
                "the pull_sftp transport needs a distribution host, user and password",
                status="not_copied",
            )
        _pull_sftp(
            conn,
            target=pull_target,
            search_dir=search_dir,
            image=image,
            file_system=file_system,
            read_timeout=transfer_read_timeout,
        )
        restore_confirmed = None

    verify_sha512(
        conn,
        file_system=file_system,
        image=image,
        expected=expected,
        read_timeout=verify_read_timeout,
        scp_restore_confirmed=restore_confirmed,
    )
    return StageOutcome("image_copied", transport, expected, restore_confirmed)


def resolve_source(
    search_dir: str, image: str, declared_size: int | None = None
) -> tuple[Path, int]:
    """Locate the image on NetHub's own mount and measure it.

    The measurement is the authority and a declared `file_size` is a
    cross-check: if the two disagree the run stops here, before any transfer,
    because the bytes on the mount are not the bytes the caller describes and
    the device-side digest would only catch that after moving the whole image.
    """
    source = Path(search_dir) / image
    try:
        measured = source.stat().st_size
    except OSError as exc:
        raise TransferError(f"cannot read {source}: {exc}", status="not_copied") from exc
    if not source.is_file():
        raise TransferError(f"{source} is not a regular file", status="not_copied")
    if declared_size is not None and declared_size != measured:
        raise TransferError(
            f"{source} is {measured} bytes but was declared as {declared_size}; "
            f"refusing to stage bytes that are not the ones described",
            status="not_copied",
        )
    return source, measured


def verify_sha512(
    conn: BaseConnection,
    *,
    file_system: str,
    image: str,
    expected: str,
    read_timeout: float = VERIFY_READ_TIMEOUT,
    scp_restore_confirmed: bool | None = None,
) -> str:
    """Have the device hash what it now holds, and compare here.

    This is the third consumption of the digest computed once at ingest
    (design doc §3.4) and it is identical in both directions.

    The device is asked for the digest rather than handed the expected one to
    check: `verify /sha512 <file> <digest>` echoes the digest back, so a
    substring test against that output can pass on the echo alone -- the same
    class of no-op as matching the word "Verified". Comparing here also lets a
    mismatch report what the device actually computed.
    """
    output = conn.send_command(
        f"verify /sha512 {file_system}{image}", read_timeout=read_timeout
    )
    match = _DIGEST_RE.search(output)
    if not match:
        raise VerificationError(
            f"no SHA-512 digest in verify output for {file_system}{image}:\n{output[:800]}",
            status="not_verified",
            scp_restore_confirmed=scp_restore_confirmed,
        )
    actual = match.group(1).lower()
    if actual != expected:
        raise VerificationError(
            f"{file_system}{image} on the device hashes to {actual}, expected "
            f"{expected}. Do not install it.",
            status="not_verified",
            scp_restore_confirmed=scp_restore_confirmed,
        )
    return actual


def _push_scp(
    conn: BaseConnection,
    *,
    source: Path,
    image: str,
    file_system: str,
    read_timeout: float,
) -> bool:
    """SCP the image to the device, bracketed by its SCP server's prior state.

    This is the only device configuration NetHub changes outside the upgrade
    itself (design doc §4.3.1). Capture, enable only if it was off, restore in
    `finally`, and confirm the restore by re-reading the running-config rather
    than trusting the config module's exit status. An unconfirmed restore
    fails the host outright -- a warning would leave a device with its SCP
    server on and no record of it, and the activate phase's own `write memory`
    would then ride that into startup-config.

    Not covered, and not claimed to be: a killed process or a crashed
    container never reaches `finally` either (design doc §4.3.1, §10).
    """
    prior_enabled = _scp_server_enabled(conn.send_command(_SCP_SHOW))
    try:
        if not prior_enabled:
            conn.send_config_set([_SCP_ENABLE])
        _scp_put(conn, source=source, image=image, file_system=file_system)
    except TransferError:
        raise
    except Exception as exc:
        raise TransferError(f"SCP push of {image} failed: {exc}", status="not_copied") from exc
    finally:
        confirmed = _restore_scp_server(conn, prior_enabled)
        if not confirmed:
            # Raised from `finally`, so it wins over any in-flight push
            # failure and keeps it as __context__. That order is deliberate: a
            # device left changed is the more urgent of the two facts.
            raise ScpRestoreError(
                f"the SCP server on this device was changed and could not be "
                f"confirmed back to {'enabled' if prior_enabled else 'disabled'}. "
                f"Fix it by hand before any configuration is saved.",
                status="scp_not_restored",
                scp_restore_confirmed=False,
            )
    return True


def _scp_put(conn: BaseConnection, *, source: Path, image: str, file_system: str) -> None:
    """One SCP put over a second session to the same pinned address.

    `hash_supported=False` is not an optimisation. Netmiko's transfer class
    MD5s the source in its constructor whenever it is left on -- including
    under `file_transfer(disable_md5=True)`, which only skips the *comparison*
    -- and SHA-512 is the only hash algorithm in this system (design doc §3.4).
    A second one here would be a ~1.2 GB pass computing a digest nothing reads.
    """
    with CiscoIosFileTransfer(
        conn,
        source_file=str(source),
        dest_file=image,
        file_system=file_system,
        direction="put",
        hash_supported=False,
        socket_timeout=SCP_SOCKET_TIMEOUT,
    ) as transfer:
        transfer.transfer_file()


def _pull_sftp(
    conn: BaseConnection,
    *,
    target: PullTarget,
    search_dir: str,
    image: str,
    file_system: str,
    read_timeout: float,
) -> None:
    """Have the device fetch the image itself. Nothing here is reconfigured.

    The password is answered at the device's own `Password:` prompt and is
    never embedded as `sftp://user:pass@host/` -- that form lands in the
    device's command history and in AAA command accounting, which is the
    record §4.3's attribution property depends on.

    Naming only the filesystem as the destination is deliberate: it forces the
    `Destination filename` prompt, so both prompts always appear in a known
    order. **The prompt sequence is unverified against real hardware** (design
    doc §10) -- a wrong list hangs until the read timeout rather than failing
    fast. Record the answer when someone runs it.
    """
    _check_name(target.host, _HOSTNAME_RE, "distribution host")
    _check_name(target.username, _USERNAME_RE, "distribution user")
    _check_name(search_dir, _REMOTE_PATH_RE, "search dir")
    if not target.password:
        raise TransferError("no distribution password supplied", status="not_copied")

    command = f"copy sftp://{target.username}@{target.host}{search_dir}/{image} {file_system}"
    try:
        conn.write_channel(command + "\n")
        conn.read_until_pattern(r"[Dd]estination filename", read_timeout=60.0)
        conn.write_channel(image + "\n")
        conn.read_until_pattern(r"[Pp]assword:", read_timeout=60.0)
        conn.write_channel(target.password + "\n")
        conn.read_until_pattern(r"[>#]", read_timeout=read_timeout)
    except Exception as exc:
        # str(exc) can quote channel content, and the password was written to
        # that channel. Report the command, never the exception text.
        raise TransferError(
            f"SFTP pull of {image} from {target.host} failed", status="not_copied"
        ) from exc


def _already_staged(
    conn: BaseConnection,
    file_system: str,
    image: str,
    expected: str,
    size: int,
    read_timeout: float,
) -> bool:
    """True only if the device already holds these exact bytes."""
    present = facts.get_filesystem(conn, file_system).size_of(image)
    if present is None or present != size:
        return False
    try:
        verify_sha512(
            conn,
            file_system=file_system,
            image=image,
            expected=expected,
            read_timeout=read_timeout,
        )
    except VerificationError:
        return False
    return True


def _check_space(conn: BaseConnection, file_system: str, image: str, size: int) -> None:
    filesystem = facts.get_filesystem(conn, file_system)
    already = filesystem.size_of(image) or 0
    if filesystem.free_bytes + already <= size:
        raise TransferError(
            f"{file_system} has {filesystem.free_bytes} bytes free, {image} needs {size}",
            status="no_space",
        )


def _restore_scp_server(conn: BaseConnection, prior_enabled: bool) -> bool:
    """Put the SCP server back and confirm it by re-reading the running-config.

    Never raises. A restore that could not even be attempted is an unconfirmed
    restore, and the caller turns that into a failed host either way.
    """
    line = _SCP_ENABLE if prior_enabled else f"no {_SCP_ENABLE}"
    try:
        conn.send_config_set([line])
        return _scp_server_enabled(conn.send_command(_SCP_SHOW)) is prior_enabled
    except Exception:  # noqa: BLE001 -- an unattemptable restore is an unconfirmed one
        return False


def _scp_server_enabled(running_config: str) -> bool:
    """Whole-line match, because `no ip scp server enable` contains the
    enabled form as a substring and a naive `in` reads it backwards."""
    return any(line.strip() == _SCP_ENABLE for line in running_config.splitlines())


def _normalise_digest(sha512: str) -> str:
    digest = sha512.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{128}", digest):
        raise TransferError(f"not a SHA-512 digest: {sha512!r}", status="not_verified")
    return digest


def _check_name(value: str, pattern: re.Pattern[str], what: str) -> None:
    if not value or not pattern.fullmatch(value):
        raise TransferError(f"invalid {what} {value!r}", status="not_copied")
