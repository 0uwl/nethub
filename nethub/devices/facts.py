"""IOS-XE facts from `show version` and `dir`, parsed with ntc-templates.

Parsing is split from the connection so captured device output can be
re-parsed offline -- scripts/check_device_facts.py does both, and is how a
new IOS-XE release gets validated before it is trusted here.

Nothing in here defaults on a parse miss: the free-space number gates the
stage phase and a silently-wrong one fills a device's flash.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ntc_templates.parse import parse_output

#: What may appear in a filesystem name we interpolate into a CLI command.
_FILE_SYSTEM_RE = re.compile(r"[\w:./-]+")
_VERSION_PART_RE = re.compile(r"(\d+)([a-z]*)", re.IGNORECASE)


class FactsError(Exception):
    """Device output could not be parsed into the facts we need."""


@dataclass(frozen=True)
class Facts:
    """`show version`. hostname/model/serial are audit detail and may be
    empty; version is load-bearing and never is."""

    version: str
    hostname: str = ""
    model: str = ""
    serial: str = ""
    running_image: str = ""


@dataclass(frozen=True)
class Filesystem:
    """`dir <file_system>`. Sizes are bytes -- note ios_facts reported KB."""

    name: str
    total_bytes: int
    free_bytes: int
    files: dict[str, int]

    def size_of(self, file_name: str) -> int | None:
        return self.files.get(file_name)


def version_tuple(version: str) -> tuple[tuple[int, str], ...]:
    """Comparable form of an IOS-XE version string.

    The registry writes `17.12.06` and the device reports `17.12.6`; those are
    the same release, and comparing the strings says they are not. Letter
    suffixes (`17.9.4a`) are kept as part of their component.
    """
    parts = []
    for part in version.strip().split("."):
        match = _VERSION_PART_RE.fullmatch(part)
        if not match:
            raise FactsError(f"unparseable IOS-XE version {version!r}")
        parts.append((int(match.group(1)), match.group(2).lower()))
    if not parts:
        raise FactsError("empty IOS-XE version")
    return tuple(parts)


def same_version(a: str, b: str) -> bool:
    return version_tuple(a) == version_tuple(b)


def parse_version(output: str) -> Facts:
    row = _parse(output, "show version")[0]
    version = (row.get("version") or "").strip()
    if not version:
        raise FactsError(f"no version in 'show version' output:\n{output[:800]}")
    return Facts(
        version=version,
        hostname=row.get("hostname", ""),
        model=_first(row.get("hardware")),
        serial=_first(row.get("serial")),
        running_image=row.get("running_image", ""),
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
    files = {
        row["name"]: int(row["size"])
        for row in rows
        if row.get("name") and row.get("size", "").isdigit()
    }
    return Filesystem(
        name=rows[0].get("file_system", ""),
        total_bytes=total,
        free_bytes=free,
        files=files,
    )


def get_facts(conn) -> Facts:
    return parse_version(conn.send_command("show version"))


def get_filesystem(conn, file_system: str = "flash:") -> Filesystem:
    return parse_dir(conn.send_command(dir_command(file_system)))


def dir_command(file_system: str) -> str:
    """A filesystem name reaches the CLI as text, so it is checked as text."""
    if not _FILE_SYSTEM_RE.fullmatch(file_system):
        raise ValueError(f"invalid file system name {file_system!r}")
    return f"dir {file_system}"


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
