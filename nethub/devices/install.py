"""Activating a staged image, waiting out the reload, and cleaning up after.

This is the most dangerous code in NetHub: every function here either reboots a
production switch or decides whether one is healthy afterwards. It is written
to be driven by the phase model (design doc §8.1) rather than as one procedure
-- `activate()` ends with the device rebooting and the session dead, and
`wait_for_device()` therefore takes a *factory* rather than a connection.

The guards in front of `activate()` are re-run rather than inherited from
pre-check. §8.1 is explicit that device state is re-gathered per phase because
"a pre-check from three days ago must not authorize today's reload", and the
image check specifically has to be a `verify /sha512` and not a `dir`: a file
of the right name is not the file staging checked.

Validated against hardware end to end, including two live reloads: a
17.12.6 -> 17.12.08 -> 17.12.6 round trip through activate, the reconnect
loop, the version check and `install remove inactive`. The command strings
were ported from the hand-run playbooks this replaced (deleted at build step
6); the failure handling around them is this module's own.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from nethub.devices import facts, transfer

if TYPE_CHECKING:
    from netmiko.base_connection import BaseConnection

#: `install add ... activate commit` runs the whole install before rebooting.
INSTALL_READ_TIMEOUT = 1800.0
CLEANUP_READ_TIMEOUT = 600.0
WRITE_MEMORY_READ_TIMEOUT = 300.0

#: IOS-XE reports its own install failures in-band, with a session that stays
#: up -- so a clean return is not the same as a successful install.
_INSTALL_FAILED_RE = re.compile(r"^\s*(FAILED|%\s*Error|ERROR)", re.IGNORECASE | re.MULTILINE)
_INSTALL_SUCCESS_RE = re.compile(r"\bSUCCESS\b")

#: `install remove inactive` prompt and terminal markers, captured from a real
#: 17.12.08 device. Matching the terminal marker as well as the prompt is what
#: keeps a "nothing to remove" run from hanging until the read timeout.
_CLEANUP_PROMPT = r"remove the above files\? \[y/n\]"
_CLEANUP_DONE = r"(?:SUCCESS|FAILED): install_remove"
#: Declining the prompt still prints `SUCCESS: install_remove`, so the success
#: marker alone does not mean anything was removed.
_CLEANUP_REJECTED_RE = re.compile(r"User Rejected Deletion", re.IGNORECASE)
_CLEANUP_DELETED_RE = re.compile(
    r"The following files will be deleted:\n(.*?)(?:\n\s*\n|\nDo you want)", re.DOTALL
)


class InstallError(Exception):
    """An upgrade step failed. `status` is the operator-facing word."""

    def __init__(self, message: str, *, status: str) -> None:
        super().__init__(message)
        self.status = status


class ReloadTimeout(InstallError):
    """The device did not come back within the deadline (§7.3 `reload`)."""


class PostCheckError(InstallError):
    """The device came back on the wrong software (§7.3 `postcheck`)."""


@dataclass(frozen=True)
class ReloadWait:
    """Reconnect policy after an activate. Defaults match the
    `wait_for_connection` the playbook used."""

    delay: float = 60.0
    interval: float = 30.0
    timeout: float = 900.0


#: One shared instance, so it is never constructed in a default argument.
DEFAULT_RELOAD_WAIT = ReloadWait()


@dataclass(frozen=True)
class ActivateOutcome:
    status: Literal["activating"]
    version_before: str
    target_version: str
    #: Device output up to the point the session went away, for the audit row.
    transcript: str


def assert_ready_to_activate(
    conn: BaseConnection,
    *,
    image: str,
    sha512: str,
    target_version: str,
    file_system: str = "flash:",
) -> facts.Facts:
    """Everything that must be true before a reload is worth risking.

    Ordered cheapest-first, and every one of them is a refusal rather than a
    warning. Returns the facts it gathered so the caller does not re-read them.
    """
    device = facts.get_facts(conn)

    if facts.same_version(device.version, target_version):
        raise InstallError(
            f"already running {device.version}; nothing to activate",
            status="already_current",
        )
    if device.boot_mode != "INSTALL":
        # `install add ... activate commit` is an INSTALL-mode procedure. A
        # BUNDLE-mode device needs a different one, and "" means facts could
        # not tell -- both are refusals, not a reason to try anyway.
        raise InstallError(
            f"device boot mode is {device.boot_mode or 'unknown'}, not INSTALL; "
            f"`install add` is not the right procedure here",
            status="wrong_boot_mode",
        )
    privilege = facts.get_privilege(conn)
    if privilege < 15:
        raise InstallError(
            f"account is at privilege {privilege}, not 15", status="privilege"
        )
    if facts.get_filesystem(conn, file_system).size_of(image) is None:
        raise InstallError(f"{image} is not staged in {file_system}", status="image_missing")

    # Not a presence check: §8.1 requires the later phase to re-hash. A host
    # whose staged image failed verification is still sitting in flash under
    # the target filename, and would otherwise be installed anyway.
    transfer.verify_sha512(conn, file_system=file_system, image=image, expected=sha512)
    return device


def capture_running_config(conn: BaseConnection) -> str:
    """The pre-upgrade configuration, for the caller to store.

    Where it goes is not this layer's business -- the playbook wrote a file
    next to itself, and under the phase model it belongs to the job row.
    """
    return conn.send_command("show running-config", read_timeout=300.0)


def activate(
    conn: BaseConnection,
    *,
    image: str,
    sha512: str,
    target_version: str,
    file_system: str = "flash:",
    read_timeout: float = INSTALL_READ_TIMEOUT,
) -> ActivateOutcome:
    """Save configuration, install the image, and leave the device rebooting.

    Returns when the device is on its way down; it does not wait. The caller
    reconnects with `wait_for_device()` and then calls `verify_upgrade()`.

    `write memory` is deliberate and comes first: the install reboots, and an
    unsaved running-config would be lost. It is also the reason the stage
    phase's SCP-server restore has to be *confirmed* rather than assumed --
    this is the write that would otherwise carry an un-restored enable into
    startup-config (design doc §4.3.1).
    """
    device = assert_ready_to_activate(
        conn, image=image, sha512=sha512, target_version=target_version,
        file_system=file_system,
    )
    conn.send_command("write memory", read_timeout=WRITE_MEMORY_READ_TIMEOUT)

    command = (
        f"install add file {file_system}{image} activate commit prompt-level none"
    )
    try:
        output = conn.send_command(command, read_timeout=read_timeout)
    except Exception as exc:  # noqa: BLE001 -- any failure here may be the reload
        # Observed against a real 17.12.08 install: the command runs the whole
        # add/activate/commit and returns `SUCCESS` with the session still up,
        # roughly ten minutes in, and only then reboots. So this branch is the
        # *unobserved* one -- it is here because a reload that takes the
        # session with it is indistinguishable from a network drop at the same
        # instant, and neither should be reported as an install failure. The
        # post-reload version check is what actually decides whether this
        # worked, which is why it is a separate phase.
        return ActivateOutcome("activating", device.version, target_version, f"{exc}")

    if _INSTALL_FAILED_RE.search(output) or not _INSTALL_SUCCESS_RE.search(output):
        # IOS-XE reports some install failures in-band and stays up. A command
        # that returned cleanly without saying SUCCESS has not installed
        # anything, and treating it as "rebooting" would wait out the full
        # reload deadline before reporting a failure visible immediately.
        raise InstallError(
            f"install did not report success on {image}:\n{output[-800:]}",
            status="install_failed",
        )
    return ActivateOutcome("activating", device.version, target_version, output)


def wait_for_device(
    connect: Callable[[], BaseConnection],
    *,
    wait: ReloadWait = DEFAULT_RELOAD_WAIT,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> BaseConnection:
    """Reconnect after a reload, or raise once the deadline passes.

    Takes a factory rather than a connection because the old session died with
    the device. The factory is where the caller supplies the credential and the
    pinned host key, so neither is held here.

    A device answering SSH is not a device that is ready: IOS-XE accepts
    connections while it is still bringing line cards up. Each attempt
    therefore runs a real command before the connection is accepted as good,
    and a connection that answers but cannot be used is closed rather than
    returned.
    """
    deadline = clock() + wait.timeout
    sleep(wait.delay)
    last: Exception | None = None

    while True:
        conn = None
        try:
            conn = connect()
            facts.get_facts(conn)  # proves the CLI is actually serving
            return conn
        except Exception as exc:  # noqa: BLE001 -- every failure is just 'not back yet'
            last = exc
            if conn is not None:
                try:
                    conn.disconnect()
                except Exception:  # noqa: BLE001, S110 -- already-dead session
                    pass
        if clock() >= deadline:
            raise ReloadTimeout(
                f"device did not return within {wait.timeout:.0f}s of the reload; "
                f"last attempt: {last}",
                status="reload_timeout",
            )
        sleep(wait.interval)


def verify_upgrade(conn: BaseConnection, *, target_version: str) -> facts.Facts:
    """Confirm the device came back on the software we installed."""
    device = facts.get_facts(conn)
    if not facts.same_version(device.version, target_version):
        raise PostCheckError(
            f"device returned running {device.version}, expected {target_version}",
            status="wrong_version",
        )
    return device


@dataclass(frozen=True)
class CleanupOutcome:
    removed: tuple[str, ...]
    transcript: str


def cleanup(
    conn: BaseConnection, *, read_timeout: float = CLEANUP_READ_TIMEOUT
) -> CleanupOutcome:
    """Remove inactive packages left behind by the install.

    `install remove inactive` asks for confirmation and has no `prompt-level
    none`, so the prompt is answered rather than suppressed.

    This is its own phase behind its own gate (§8.1): it frees flash but
    removes the packages a rollback would need.
    """
    output = conn.send_command(
        "install remove inactive",
        expect_string=f"(?:{_CLEANUP_PROMPT}|{_CLEANUP_DONE})",
        read_timeout=read_timeout,
    )
    if re.search(_CLEANUP_PROMPT, output):
        output += conn.send_command(
            "y", expect_string=_CLEANUP_DONE, read_timeout=read_timeout
        )

    # Order matters: a declined deletion prints the success marker too, so
    # testing for SUCCESS first would report a cleanup that removed nothing as
    # a clean one. Verified by answering `n` to a real device.
    if _CLEANUP_REJECTED_RE.search(output):
        raise InstallError(
            "cleanup was declined at the device prompt and removed nothing",
            status="not_clean",
        )
    if "SUCCESS: install_remove" not in output:
        raise InstallError(
            f"cleanup did not report success:\n{output[-800:]}", status="not_clean"
        )
    return CleanupOutcome(removed=_parse_removed(output), transcript=output)


def _parse_removed(output: str) -> tuple[str, ...]:
    """The files the device said it would delete, for the audit row."""
    match = _CLEANUP_DELETED_RE.search(output)
    if not match:
        return ()
    return tuple(
        line.split(":", 1)[1].strip()
        for line in match.group(1).splitlines()
        if ":" in line and line.strip()
    )
