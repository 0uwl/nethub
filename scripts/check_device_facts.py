#!/usr/bin/env python3
"""Capture real IOS-XE output for tests/captures/, or re-parse a capture offline.

    scripts/check_device_facts.py 192.0.2.10 --scan
    scripts/check_device_facts.py 192.0.2.10 --user me --fingerprint "ssh-rsa SHA256:..."
    scripts/check_device_facts.py --replay captures/sw01-20260909-131500

The first form connects the same way every other NetHub process does -- a
pinned, confirmed host key and no TOFU -- runs `show version` / `dir <fs>` /
`show privilege`, writes the raw outputs to a capture directory, and reports
what parsed. This is how a new IOS-XE release gets validated: capture it
here, add the directory to tests/captures/, and replay it offline -- the
second form, which needs no device. Do not edit a capture to make a test
pass; it is evidence that ntc-templates parses what the fleet actually runs,
not a fixture.
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nethub.devices import connection
from nethub.devices import facts as F


def resolve_pin(host: str, fingerprint: str | None) -> connection.HostKey:
    """The pin, from the operator or from NetHub's database -- never invented
    (docs/device-layer.md [facts-script-never-tofu]).

    A `--fingerprint` the operator typed *is* a confirmation: they compared it
    against the device out of band, which is what the web flow asks of them.
    What must never happen is a silent first-contact accept.
    """
    if fingerprint:
        key_type, _, digest = fingerprint.partition(' ')
        if not digest:
            key_type, digest = 'ssh-rsa', fingerprint
        return connection.HostKey(key_type.strip(), digest.strip())

    pin = _pin_from_database(host)
    if pin is not None:
        print(f'Using the confirmed pin from the database: {pin.key_type} '
              f'{pin.fingerprint_sha256}')
        return pin

    raise SystemExit(
        f'No confirmed host key for {host}, and no --fingerprint given.\n'
        f'Run with --scan first, compare the fingerprint against the device '
        f'itself, then pass it as --fingerprint "<type> SHA256:...".'
    )


def _pin_from_database(host: str):
    """Read a confirmed pin if NetHub's database happens to be reachable.
    Optional by design: this script must work with NetHub down."""
    try:
        from nethub.models import DeviceHostKey
        from nethub.sibling import _database_app

        app = _database_app()
        with app.app_context():
            row = DeviceHostKey.query.filter_by(ansible_host=host).first()
            if row is None or not row.is_confirmed:
                return None
            # Detach the values; the session goes away with the context.
            return connection.HostKey(row.key_type, row.fingerprint_sha256)
    except Exception as exc:  # noqa: BLE001 -- an unreachable database is expected here
        print(f'(could not read a pin from the database: {exc})')
        return None


def capture(args) -> Path:
    from getpass import getpass

    pin = resolve_pin(args.host, args.fingerprint)

    password = os.environ.get("NETHUB_DEVICE_PASSWORD")
    if password:
        # Kept because a scripted capture run needs it, but not silently: an
        # env var sits in /proc/<pid>/environ for the whole run, is inherited
        # by every child, and lands in shell history if set inline.
        print("WARNING: reading the device password from NETHUB_DEVICE_PASSWORD.",
              file=sys.stderr)
        print("         It is readable in /proc/<pid>/environ for this whole run",
              file=sys.stderr)
        print("         and inherited by every child process. Prefer the prompt.",
              file=sys.stderr)
    else:
        password = getpass("Device password: ")

    out = Path(args.out or f"captures/{args.host}-{datetime.now(tz=UTC):%Y%m%d-%H%M%S}")
    out.mkdir(parents=True, exist_ok=True)

    with connection.connect(args.host, args.user, password, pin, auth=args.auth) as conn:
        (out / "show_version.txt").write_text(conn.send_command("show version"))
        (out / "show_privilege.txt").write_text(conn.send_command("show privilege"))
        (out / "dir.txt").write_text(
            conn.send_command(F.dir_command(args.file_system), read_timeout=F.DIR_READ_TIMEOUT)
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
        print(f"boot_mode     {f.boot_mode!r}   <- empty means neither packages.conf nor .bin")
        if expect_version:
            same = F.same_version(f.version, expect_version)
            print(f"matches {expect_version!r}: {same}")
            failures += not same
    except F.FactsError as exc:
        print(f"show version FAILED: {exc}")
        failures += 1

    privilege_capture = directory / "show_privilege.txt"
    if privilege_capture.exists():
        try:
            level = F.parse_privilege(privilege_capture.read_text())
            print(f"privilege     {level}   <- an upgrade needs 15")
            failures += level < 15
        except F.FactsError as exc:
            print(f"show privilege FAILED: {exc}")
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
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("host", nargs="?", help="device to connect to -- an IP literal, not a hostname")
    p.add_argument("--user", default=os.environ.get("USER"), help="device username")
    p.add_argument("--fingerprint",
                    help='"<key-type> SHA256:..." -- get one with --scan, or read it '
                         "off a confirmed row in NetHub's own Host keys page")
    p.add_argument("--scan", action="store_true",
                   help="print the host's key fingerprint and exit; trusts nothing")
    p.add_argument("--file-system", default="flash:", help="default flash:")
    p.add_argument(
        "--auth",
        choices=("keyboard-interactive", "password"),
        default="keyboard-interactive",
        help="SSH auth method; IOS-XE with aaa new-model offers only the default",
    )
    p.add_argument("--out", help="capture directory (default captures/<host>-<timestamp>)")
    p.add_argument("--replay", help="re-parse an existing capture directory, no device")
    p.add_argument("--expect-version", help="registry version to compare against, e.g. 17.12.06")
    p.add_argument("--expect-image", help="image file name that should be in flash")
    args = p.parse_args()

    if args.replay:
        return report(Path(args.replay), args.expect_version, args.expect_image)
    if not args.host:
        p.error("give a host, or --replay a capture directory")
    try:
        ipaddress.ip_address(args.host)
    except ValueError:
        p.error(f"{args.host!r} is not an IP literal -- hostnames are refused so DNS "
                f"cannot decide where the device credential goes")
    if args.scan:
        pin = connection.scan_host_key(args.host)
        print(f"{args.host}  {pin.key_type} {pin.fingerprint_sha256}")
        print("Compare that against the device itself before trusting it.")
        return 0
    return report(capture(args), args.expect_version, args.expect_image)


if __name__ == "__main__":
    raise SystemExit(main())
