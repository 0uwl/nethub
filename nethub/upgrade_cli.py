"""`python -m nethub.upgrade_cli` -- upgrade a device without the web app.

Build step 8. The playbooks this replaced were deliberately standalone: an
operator could run them by hand against a fleet with NetHub switched off
entirely. Folding device work into `nethub/devices/` took that away, and
`netmiko.md` records the loss as accepted rather than unnoticed. This restores
it, at the smallest scope that is honest.

It needs **no Flask app, no sibling, no credential socket and no job rows** --
`nethub/devices/` never depended on any of them, which is why this is a few
hundred lines rather than a parallel implementation. It will read a pinned
host key out of the database if one is reachable, and otherwise makes the
operator supply the fingerprint explicitly.

**A run made here is deliberately absent from the audit trail.** §7.3 assigns
every job-row edge to Flask or the sibling, and a third writer would put rows
in the database that no approval and no `runner_instance_id` accounts for --
which is worse than an honest gap, because it would look like a normal run.
So this writes nothing and says so on every invocation. If you need the audit
trail, fix NetHub and use it; this is for when you cannot.

The interactive confirmations are the same idea as the approval gates (§8.1),
collapsed onto a terminal because there is no second person to ask.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys

from netmiko.exceptions import NetmikoBaseException

from nethub.devices import connection, facts, install, transfer

PHASES = ('precheck', 'stage', 'activate', 'verify', 'cleanup')
#: Phases that change something on the device, so they are confirmed.
MUTATING = {'stage', 'activate', 'cleanup'}


def _say(message: str = '') -> None:
    print(message, flush=True)


def _confirm(prompt: str, assume_yes: bool) -> bool:
    if assume_yes:
        _say(f'{prompt} -- assumed yes (--yes)')
        return True
    try:
        return input(f'{prompt} [y/N] ').strip().lower() in ('y', 'yes')
    except EOFError:
        return False


def resolve_pin(host: str, fingerprint: str | None) -> connection.HostKey:
    """The pin, from the operator or from the database -- never invented.

    A `--fingerprint` the operator typed *is* a confirmation: they compared it
    against the device out of band, which is exactly what the web flow asks of
    them. What must never happen here is a silent first-contact accept, which
    would make this tool a way of walking around §4.3 rather than a way of
    working without the web app.
    """
    if fingerprint:
        key_type, _, digest = fingerprint.partition(' ')
        if not digest:
            key_type, digest = 'ssh-rsa', fingerprint
        return connection.HostKey(key_type.strip(), digest.strip())

    row = _pin_from_database(host)
    if row is not None:
        _say(f'Using the confirmed pin from the database: {row.key_type} {row.fingerprint_sha256}')
        return connection.HostKey(row.key_type, row.fingerprint_sha256)

    raise SystemExit(
        f'No confirmed host key for {host}, and no --fingerprint given.\n'
        f'Run with --scan first, compare the fingerprint against the device '
        f'itself, then pass it as --fingerprint "<type> <SHA256:...>".'
    )


def _pin_from_database(host: str):
    """Read the pin if the database happens to be reachable. Optional by
    design: the whole point of this tool is that NetHub may be down."""
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
        _say(f'(could not read a pin from the database: {exc})')
        return None


def run_precheck(conn, args) -> None:
    device = facts.get_facts(conn)
    privilege = facts.get_privilege(conn)
    filesystem = facts.get_filesystem(conn, args.file_system)
    _say(f'  running   {device.version} ({device.boot_mode}) via {device.running_image}')
    _say(f'  model     {device.model}  serial {device.serial}')
    _say(f'  privilege {privilege}')
    _say(f'  {args.file_system} {filesystem.free_bytes:,} free of {filesystem.total_bytes:,}')
    if privilege < 15:
        raise SystemExit(f'Account is at privilege {privilege}, not 15.')
    if device.boot_mode != 'INSTALL':
        raise SystemExit(f'Boot mode is {device.boot_mode or "unknown"}, not INSTALL.')
    already = filesystem.size_of(os.path.basename(args.image)) or 0
    if filesystem.free_bytes + already <= os.path.getsize(args.image):
        raise SystemExit('Not enough free space for the image.')


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog='python -m nethub.upgrade_cli',
        description=__doc__.split('\n\n')[0],
        epilog='Nothing this tool does is recorded in NetHub. Use the web app '
               'when it is available.',
    )
    parser.add_argument('--host', help='device address (an IP literal)')
    parser.add_argument('--scan', metavar='HOST',
                        help='fetch and print a host key fingerprint, then exit')
    parser.add_argument('--user', default=os.environ.get('USER'), help='device username')
    parser.add_argument('--fingerprint',
                        help='"<key-type> SHA256:..." as --scan printed it')
    parser.add_argument('--image', help='path to the image on this host')
    parser.add_argument('--sha512', help='expected SHA-512 of the image')
    parser.add_argument('--version', help='version the device should report afterwards')
    parser.add_argument('--file-system', default='flash:')
    parser.add_argument('--transport', default='push_scp', choices=transfer.Transport.__args__)
    parser.add_argument('--phases', default='precheck',
                        help=f'comma-separated, or "all". One of: {", ".join(PHASES)}')
    parser.add_argument('--yes', action='store_true',
                        help='skip the confirmations before device-changing phases')
    args = parser.parse_args(argv)

    if args.scan:
        pin = connection.scan_host_key(args.scan)
        _say(f'{args.scan}  {pin.key_type} {pin.fingerprint_sha256}')
        _say('Compare that against the device itself before trusting it.')
        return 0

    phases = PHASES if args.phases == 'all' else tuple(
        p.strip() for p in args.phases.split(',') if p.strip())
    unknown = [p for p in phases if p not in PHASES]
    if unknown:
        parser.error(f'unknown phase(s): {", ".join(unknown)}')
    for required in ('host', 'user'):
        if not getattr(args, required):
            parser.error(f'--{required} is required')
    needs_image = {'precheck', 'stage', 'activate', 'verify'} & set(phases)
    if needs_image and not (args.image and args.sha512 and args.version):
        parser.error('--image, --sha512 and --version are required for these phases')

    _say('This runs outside NetHub: nothing below is written to the database,')
    _say('and no approval is recorded against it.')
    _say('')

    pin = resolve_pin(args.host, args.fingerprint)
    password = os.environ.get('NETHUB_DEVICE_PASSWORD') or getpass.getpass('Device password: ')
    image = os.path.basename(args.image) if args.image else None
    search_dir = os.path.dirname(os.path.abspath(args.image)) if args.image else None

    def connect():
        return connection.connect(args.host, args.user, password, pin)

    # Inside the try: a host-key mismatch is raised by connect(), and it is the
    # single message an operator most needs to read as a sentence rather than
    # as a traceback.
    conn = None
    try:
        conn = connect()
        for phase in phases:
            if phase in MUTATING and not _confirm(f'Run the {phase} phase on {args.host}?',
                                                  args.yes):
                _say('Stopped.')
                return 1
            _say(f'== {phase}')
            if phase == 'precheck':
                run_precheck(conn, args)
            elif phase == 'stage':
                outcome = transfer.stage_image(
                    conn, image=image, sha512=args.sha512, search_dir=search_dir,
                    transport=args.transport, file_system=args.file_system)
                _say(f'  {outcome.status}; scp_restore_confirmed='
                     f'{outcome.scp_restore_confirmed}')
            elif phase == 'activate':
                result = install.activate(
                    conn, image=image, sha512=args.sha512,
                    target_version=args.version, file_system=args.file_system)
                _say(f'  was {result.version_before}; device is reloading')
                conn = install.wait_for_device(connect)
                _say('  device is back and serving CLI')
            elif phase == 'verify':
                device = install.verify_upgrade(conn, target_version=args.version)
                _say(f'  running {device.version}')
            elif phase == 'cleanup':
                result = install.cleanup(conn)
                _say(f'  removed {len(result.removed)} file(s)')
                for name in result.removed:
                    _say(f'    {name}')
    except (connection.DeviceConnectionError, transfer.TransferError,
            install.InstallError, facts.FactsError, NetmikoBaseException) as exc:
        _say(f'FAILED: {type(exc).__name__}: {exc}')
        return 1
    finally:
        if conn is not None:
            try:
                conn.disconnect()
            except Exception:  # noqa: BLE001, S110 -- a dead session is already gone
                pass
    _say('')
    _say('Done. Remember this run is not in NetHub\'s audit trail.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
