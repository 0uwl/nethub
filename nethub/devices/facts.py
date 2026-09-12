"""IOS-XE facts from `show version`, `dir` and `show privilege`.

Parsing is split from the connection so captured device output can be
re-parsed offline -- scripts/check_device_facts.py does both, and is how a
new IOS-XE release gets validated before it is trusted here.

Nothing in here defaults on a parse miss: the free-space number gates the
stage phase and a silently-wrong one fills a device's flash. Nothing here
polices either -- a caller that wants "privilege 15" or "enough room" reads
the number and refuses; the policy lives with the phase, not the parser.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from ntc_templates.parse import parse_output

if TYPE_CHECKING:
    from netmiko.base_connection import BaseConnection

#: What may appear in a filesystem name we interpolate into a CLI command.
_FILE_SYSTEM_RE = re.compile(r"[\w:./-]+")
_VERSION_PART_RE = re.compile(r"(\d+)([a-z]*)", re.IGNORECASE)
_PRIVILEGE_RE = re.compile(r"current privilege level is (\d+)", re.IGNORECASE)

#: `dir` lists a multi-gigabyte flash and outruns Netmiko's ~10s default.
DIR_READ_TIMEOUT = 120.0

#: How the device boots. Empty means `show version` did not say -- treat that
#: as a refusal, never as BUNDLE.
BootMode = Literal["INSTALL", "BUNDLE", ""]


class FactsError(Exception):
    """Device output could not be parsed into the facts we need."""


@dataclass(frozen=True)
class Facts:
    """`show version`. hostname/model/serial are audit detail and may be
    empty; version is load-bearing and never is.

    `boot_mode` is load-bearing too, but for the install phase rather than
    here: `install add ... activate commit` is an INSTALL-mode procedure, and
    a BUNDLE-mode device needs a different one. It is derived rather than
    read, because ntc-templates does not expose `show version`'s own Mode
    column -- see _boot_mode.
    """

    version: str
    hostname: str = ""
    model: str = ""
    serial: str = ""
    running_image: str = ""
    boot_mode: BootMode = ""


@dataclass(frozen=True)
class Filesystem:
    """`dir <file_system>`. Sizes are bytes -- note ios_facts reported KB."""

    name: str
    total_bytes: int
    free_bytes: int
    files: dict[str, int]

    def size_of(self, file_name: str) -> int | None:
        """Bytes of a regular file, or None if it is not on this filesystem."""
        return self.files.get(file_name)


def version_tuple(version: str) -> tuple[tuple[int, str], ...]:
    """Comparable form of an IOS-XE version string.

    The registry writes `17.12.06` and the device reports `17.12.6`; those are
    the same release, and comparing the strings says they are not. Letter
    suffixes (`17.9.4a`) are kept as part of their component.
    """
    version = version.strip()
    if not version:
        raise FactsError("empty IOS-XE version")
    parts = []
    for part in version.split("."):
        match = _VERSION_PART_RE.fullmatch(part)
        if not match:
            raise FactsError(f"unparseable IOS-XE version {version!r}")
        parts.append((int(match.group(1)), match.group(2).lower()))
    return tuple(parts)


def same_version(a: str, b: str) -> bool:
    return version_tuple(a) == version_tuple(b)


def parse_version(output: str) -> Facts:
    row = _parse(output, "show version")[0]
    version = (row.get("version") or "").strip()
    if not version:
        raise FactsError(f"no version in 'show version' output:\n{output[:800]}")
    running_image = row.get("running_image", "")
    return Facts(
        version=version,
        hostname=row.get("hostname", ""),
        model=_first(row.get("hardware")),
        serial=_first(row.get("serial")),
        running_image=running_image,
        boot_mode=_boot_mode(running_image),
    )


def parse_dir(output: str) -> Filesystem:
    rows = _parse(output, "dir")
    try:
        total = int(rows[0]["total_size"])
        free = int(rows[0]["total_free"])
    except (KeyError, ValueError):
        raise FactsError(
            f"no 'bytes total (bytes free)' line in 'dir' output:\n{output[:800]}"
        ) from None
    # Directories carry a size too, and `size_of` answers "is the staged image
    # here, at the right length" -- a directory must not be able to answer it.
    files = {
        row["name"]: int(row["size"])
        for row in rows
        if row.get("name")
        and row.get("size", "").isdigit()
        and not row.get("permissions", "").startswith("d")
    }
    return Filesystem(
        name=rows[0].get("file_system", ""),
        total_bytes=total,
        free_bytes=free,
        files=files,
    )


def parse_privilege(output: str) -> int:
    """`show privilege` -> `Current privilege level is 15`.

    No ntc-template covers this one, so it is a plain search rather than a
    fullmatch: the line arrives on its own today, but a banner ahead of it
    should not read as a missing privilege level.
    """
    match = _PRIVILEGE_RE.search(output)
    if not match:
        raise FactsError(f"no privilege level in 'show privilege' output:\n{output[:800]}")
    return int(match.group(1))


def get_facts(conn: BaseConnection) -> Facts:
    return parse_version(conn.send_command("show version"))


def get_filesystem(
    conn: BaseConnection,
    file_system: str = "flash:",
    read_timeout: float = DIR_READ_TIMEOUT,
) -> Filesystem:
    return parse_dir(conn.send_command(dir_command(file_system), read_timeout=read_timeout))


def get_privilege(conn: BaseConnection) -> int:
    return parse_privilege(conn.send_command("show privilege"))


def dir_command(file_system: str) -> str:
    """A filesystem name reaches the CLI as text, so it is checked as text."""
    if not _FILE_SYSTEM_RE.fullmatch(file_system):
        raise ValueError(f"invalid file system name {file_system!r}")
    return f"dir {file_system}"


def _boot_mode(running_image: str) -> BootMode:
    """INSTALL boots a `packages.conf` provisioning file, BUNDLE the .bin.

    `show version` prints the mode in a per-switch table ntc-templates does
    not capture, so the boot file is the signal. Anything else is unknown.
    """
    name = running_image.rsplit("/", 1)[-1].rsplit(":", 1)[-1].lower()
    if name.endswith(".conf"):
        return "INSTALL"
    if name.endswith(".bin"):
        return "BUNDLE"
    return ""


def _parse(output: str, command: str) -> list[dict]:
    try:
        rows = parse_output(platform="cisco_ios", command=command, data=output)
    except Exception as exc:  # textfsm/clitable raise a variety of these
        raise FactsError(f"could not parse {command!r}: {exc}") from exc
    if not rows:
        raise FactsError(f"{command!r} produced no rows. Output was:\n{output[:800]}")
    return rows


def _first(value) -> str:
    return value[0] if value else ""
