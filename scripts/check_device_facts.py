#!/usr/bin/env python3
"""Validate nethub.devices.facts parsing against a real IOS-XE device.

    scripts/check_device_facts.py sw01.example.net --user me
    scripts/check_device_facts.py --replay captures/sw01-20260909-131500

The first form logs in, runs `show version` and `dir <fs>`, writes both raw
outputs to a capture directory, and reports what parsed. Send that directory
back if anything is wrong -- the second form re-parses it with no device.

Throwaway validation tool: it uses Netmiko's default host-key handling, not
the fail-closed pinning the real connection path will use.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nethub.devices import facts as F


def capture(args) -> Path:
    from getpass import getpass

    from netmiko import ConnectHandler

    password = os.environ.get("NETHUB_DEVICE_PASSWORD") or getpass("Device password: ")
    out = Path(args.out or f"captures/{args.host}-{datetime.now(tz=timezone.utc):%Y%m%d-%H%M%S}")
    out.mkdir(parents=True, exist_ok=True)

    with ConnectHandler(
        device_type="cisco_ios",
        host=args.host,
        username=args.user,
        password=password,
    ) as conn:
        (out / "show_version.txt").write_text(conn.send_command("show version"))
        (out / "dir.txt").write_text(
            conn.send_command(F.dir_command(args.file_system), read_timeout=120)
        )
    print(f"captured to {out}")
    return out


def report(directory: Path, expect_version: str | None, expect_image: str | None) -> int:
    failures = 0

    try:
        f = F.parse_version((directory / "show_version.txt").read_text())
        print(f"version       {f.version!r}")
        print(f"hostname      {f.hostname!r}")
        print(f"model         {f.model!r}   <- empty means the template missed it")
        print(f"serial        {f.serial!r}   <- empty means the template missed it")
        print(f"running_image {f.running_image!r}")
        if expect_version:
            same = F.same_version(f.version, expect_version)
            print(f"matches {expect_version!r}: {same}")
            failures += not same
    except F.FactsError as exc:
        print(f"show version FAILED: {exc}")
        failures += 1

    try:
        fs = F.parse_dir((directory / "dir.txt").read_text())
        print(f"\nfile system   {fs.name!r}")
        print(f"total         {fs.total_bytes:,} bytes")
        print(f"free          {fs.free_bytes:,} bytes")
        print(f"files         {len(fs.files)} parsed")
        for name, size in sorted(fs.files.items()):
            print(f"  {size:>14,}  {name}")
        if expect_image:
            size = fs.size_of(expect_image)
            print(f"{expect_image}: {f'present, {size:,} bytes' if size else 'NOT FOUND'}")
            failures += size is None
    except F.FactsError as exc:
        print(f"dir FAILED: {exc}")
        failures += 1

    print("\nOK" if not failures else f"\n{failures} problem(s) -- send the capture directory back")
    return 1 if failures else 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("host", nargs="?", help="device to connect to")
    p.add_argument("--user", default=os.environ.get("USER"), help="device username")
    p.add_argument("--file-system", default="flash:", help="default flash:")
    p.add_argument("--out", help="capture directory (default captures/<host>-<timestamp>)")
    p.add_argument("--replay", help="re-parse an existing capture directory, no device")
    p.add_argument("--expect-version", help="registry version to compare against, e.g. 17.12.06")
    p.add_argument("--expect-image", help="image file name that should be in flash")
    args = p.parse_args()

    if args.replay:
        return report(Path(args.replay), args.expect_version, args.expect_image)
    if not args.host:
        p.error("give a host, or --replay a capture directory")
    return report(capture(args), args.expect_version, args.expect_image)


if __name__ == "__main__":
    raise SystemExit(main())
