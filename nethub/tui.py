"""`nhtui`: upgrade a set of switches from a terminal.

A view over `nethub.local_run.LocalRun`, which runs the sibling's own phase
engine in this process (docs/tui.md). No web app, no sibling, no database:
the run lives in memory, this process is its dispatcher, and nothing is
stored unless `--report` asks for a file. A run made here is outside
NetHub's audit trail, and it says so.

The gates are the web app's: pre-check, then an approval (with the device
password, collected again each time) before stage, activate and cleanup,
with retry of failed hosts at each gate. Every address needs a pin the
operator confirmed in this session or brought with `--pins`; there is no
trust on first use (docs/tui.md [tui-never-tofu]).

Imports no Flask and no SQLAlchemy (docs/tui.md [tui-imports-no-flask]).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import ClassVar

from textual import work
from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, RichLog, Static

from nethub import flow, targets
from nethub.devices import connection
from nethub.local_run import LocalRun, LocalRunError, MemoryLedger, load_hosts
from nethub.vocab import STATE_BEFORE


class ChoiceScreen(ModalScreen):
    """A consequence, an optional password and reload count, and buttons.
    Dismisses with `(button_id, password, reload_count)`, or None."""

    DEFAULT_CSS = """
    ChoiceScreen { align: center middle; }
    ChoiceScreen > Vertical { width: 80; height: auto; border: thick $accent;
                              background: $surface; padding: 1 2; }
    ChoiceScreen Button { width: 100%; margin-top: 1; }
    """
    BINDINGS: ClassVar = [('escape', 'close', 'Close')]

    def __init__(self, text: str, actions: list[tuple[str, str]], *,
                 password_for: str | None = None, reload_cap: int | None = None):
        super().__init__()
        self.text, self.actions = text, actions
        self.password_for, self.reload_cap = password_for, reload_cap

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self.text)
            if self.password_for:
                yield Label(f'Device password for {self.password_for}')
                yield Input(password=True, id='password')
            if self.reload_cap:
                yield Label(f'Reload this many at a time after the canary (1-{self.reload_cap})')
                yield Input('1', id='reload')
            for action, label in self.actions:
                yield Button(label, id=action)
            yield Button('Close', id='close')

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == 'close':
            self.dismiss(None)
            return
        password = self.query_one('#password', Input).value if self.password_for else ''
        reload = self.query_one('#reload', Input).value if self.reload_cap else None
        self.dismiss((event.button.id, password, reload))

    def action_close(self) -> None:
        self.dismiss(None)


class NetHubTUI(App):
    TITLE = 'nhtui'
    SUB_TITLE = 'NetHub, offline'
    BINDINGS: ClassVar = [
        ('a', 'act', 'Next step'),
        ('s', 'scan', 'Scan host key'),
        ('x', 'cancel_run', 'Cancel run'),
        ('q', 'quit', 'Quit'),
    ]

    def __init__(self, local: LocalRun):
        super().__init__()
        self.local = local
        self.busy = False
        self.logged = 0
        local.on_tick = lambda: self.call_from_thread(self.refresh_view)

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(id='status')
        yield DataTable(id='hosts', cursor_type='row')
        yield RichLog(id='log', wrap=True)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one(DataTable).add_columns('Host', 'Address', 'Pin', 'State', 'Last result')
        if not (os.environ.get('TMUX') or os.environ.get('STY')):
            self.say('[b]Not inside tmux or screen.[/b] This process is the dispatcher: if '
                     'your SSH session drops during activate, the run stops mid-reload.')
        self.refresh_view()

    # -- view ---------------------------------------------------------------
    def say(self, text: str) -> None:
        self.query_one(RichLog).write(text)

    def refresh_view(self) -> None:
        run, ledger = self.local.run, self.local.ledger
        table = self.query_one(DataTable)
        row = table.cursor_row
        table.clear()
        last = {r['hostname']: r for r in ledger.results}
        for host in run.hosts:
            pin = ledger.pins.get(host.ansible_host)
            result = last.get(host.hostname)
            table.add_row(
                host.hostname, host.ansible_host,
                pin.fingerprint_sha256 if pin else 'not confirmed', host.state,
                f"{result['phase']} {result['status']} {result['failure_stage'] or ''}"
                if result else '')
        table.move_cursor(row=min(row, len(run.hosts) - 1))
        for result in ledger.results[self.logged:]:
            self.say(f"{result['hostname']}: {result['phase']} {result['status']}"
                     + (f" ({result['failure_stage']}) {result['error_summary']}"
                        if result['failure_stage'] else ''))
        self.logged = len(ledger.results)
        state = run.state + (f' at the {run.awaiting_phase} gate' if run.awaiting_phase else '')
        self.query_one('#status', Static).update(
            f'Run: {state}' + ('  [phase running]' if self.busy else ''))

    # -- host keys ----------------------------------------------------------
    def action_scan(self) -> None:
        if self.local.jobs:
            self.notify('Pins are fixed once the run has started.')
            return
        host = self.local.run.hosts[self.query_one(DataTable).cursor_row]
        self.say(f'Scanning {host.ansible_host}...')
        self.scan(host.ansible_host)

    @work(thread=True, exclusive=True, group='scan')
    def scan(self, address: str) -> None:
        try:
            key = connection.scan_host_key(address)
        except Exception as exc:  # noqa: BLE001 -- shown, not raised
            self.call_from_thread(self.say, f'Scan of {address} failed: {type(exc).__name__}')
            return
        text = (f'{address} presented\n\n  {key.key_type} {key.fingerprint_sha256}\n\n'
                f'Compare that against the device itself before trusting it. Pinning it '
                f'lets this run connect to {address}, and only for this run.')
        self.call_from_thread(
            self.push_screen, ChoiceScreen(text, [('pin', 'It matches the device: pin it')]),
            lambda choice: self.pin(address, key, choice))

    def pin(self, address, key, choice) -> None:
        if choice is None:
            self.say(f'{address}: not pinned.')
            return
        self.local.ledger.pins[address] = key
        self.say(f'{address}: pinned {key.key_type} {key.fingerprint_sha256}.')
        self.refresh_view()

    # -- the gates ----------------------------------------------------------
    def action_act(self) -> None:
        run = self.local.run
        if self.busy:
            self.notify('A phase is running.')
            return
        if self.local.finished:
            self.notify(f'The run is {run.state}. Press q to quit.')
            return
        if run.state == 'pre_checking':
            unpinned = [h.ansible_host for h in run.hosts
                        if h.ansible_host not in self.local.ledger.pins]
            if unpinned:
                self.notify(f'No confirmed pin for {", ".join(unpinned)}. Scan (s) first.')
                return
            text = (f'Run pre-check on {len(run.hosts)} device(s). This releases your '
                    f'device credential against every address listed.')
            self.push_screen(ChoiceScreen(text, [('precheck', 'Run pre-check')],
                                          password_for=self._password_for()), self.choose)
            return
        gate = run.awaiting_phase
        hosts = [h for h in run.hosts if h.state == STATE_BEFORE[gate]]
        if gate == 'activate':
            text = (f'Reload {len(hosts)} device(s) now'
                    + (', the canary first' if len(hosts) > 1 else '')
                    + '. Each one drops traffic while it reboots, and this releases your '
                      'device credential against every address listed.')
        elif gate == 'cleanup':
            text = (f'Remove inactive packages on {len(hosts)} device(s). This frees flash '
                    f'but removes what a rollback would need.')
        else:
            text = (f'Approve {gate} on {len(hosts)} device(s). This releases your device '
                    f'credential against every address listed.')
        actions = [(f'approve-{gate}', f'Approve {gate}')] if hosts else []
        for phase, failed in flow.retryable_phases(run):
            text += (f'\n\n{len(failed)} host(s) failed {phase}: {", ".join(failed)}. '
                     f'Retrying runs {phase} again on those hosts only.')
            actions.append((f'retry-{phase}', f'Retry {phase} on {len(failed)} host(s)'))
        if gate == 'cleanup':
            actions.append(('decline-cleanup', 'Decline cleanup and close the run'))
        reloads = gate == 'activate' or any(a == 'retry-activate' for a, _ in actions)
        self.push_screen(ChoiceScreen(text, actions, password_for=self._password_for(),
                                      reload_cap=self.local.concurrency if reloads else None),
                         self.choose)

    def _password_for(self) -> str | None:
        """Ask for the password only when the run holds none: at pre-check,
        and after a refusal dropped it."""
        return self.local.username if self.local.needs_password else None

    def choose(self, choice) -> None:
        if choice is None:
            return
        action, password, reload = choice
        if action == 'decline-cleanup':
            self.local.decline_cleanup()
            self.say('Cleanup declined; the run is complete.')
            self.refresh_view()
            return
        self.busy = True
        self.refresh_view()
        self.dispatch(action, password, reload)

    @work(thread=True, exclusive=True, group='phase')
    def dispatch(self, action: str, password: str, reload) -> None:
        verb, _, phase = action.partition('-')
        try:
            if verb == 'precheck':
                status = self.local.precheck(password or None)
            elif verb == 'approve':
                status = self.local.approve(phase, password or None, reload)
            else:
                status = self.local.retry(phase, password or None, reload)
            message = f'{phase or "precheck"}: {status}.'
            if self.local.needs_password:
                message += (' The device password was refused and has been dropped; '
                            + ('restart nhtui to try again.' if self.local.finished
                               else 'the next step will ask for it again.'))
        except (LocalRunError, targets.RequestError) as exc:
            message = f'Refused: {exc}'
        self.call_from_thread(self.finish_dispatch, message)

    def finish_dispatch(self, message: str) -> None:
        self.busy = False
        self.say(message)
        self.refresh_view()

    def action_cancel_run(self) -> None:
        if self.local.finished:
            return
        text = ('Cancel the run. No further host starts; hosts already running finish, '
                'because there is no safe place to stop inside an activation.')
        self.push_screen(ChoiceScreen(text, [('cancel', 'Cancel the run')]), self.cancel_run)

    def cancel_run(self, choice) -> None:
        if choice is None or self.local.finished:
            return
        self.local.cancel()
        self.say('Cancel requested.' if self.busy else 'Run cancelled.')
        self.refresh_view()

    async def action_quit(self) -> None:
        if self.busy:
            self.notify('A phase is running. Cancel it (x) and wait for the running hosts '
                        'to finish; there is no safe stop inside an activation.')
            return
        self.exit()


def _read_pins(path: str, cidrs) -> dict:
    """`address key-type SHA256:...` per line: pins the operator brings,
    having checked them already. Each address gets the same target check."""
    pins = {}
    with open(path) as handle:
        for number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            parts = line.split()
            if len(parts) != 3:
                raise SystemExit(f'{path} line {number}: expected "address key-type SHA256:..."')
            address = targets.check_target(parts[0], cidrs)
            pins[address] = connection.HostKey(parts[1], parts[2])
    return pins


def _summary(local: LocalRun) -> str:
    lines = [f'Run {local.run.state}.']
    for host in local.run.hosts:
        lines.append(f'  {host.hostname:<20} {host.ansible_host:<16} {host.state:<12} '
                     f'{host.reported_version_post or host.reported_version_pre or ""}')
    lines.append('Nothing here is in NetHub\'s audit trail.')
    return '\n'.join(lines)


def main(argv=None) -> int:
    env_cidrs = [c.strip() for c in os.environ.get('DEVICE_TARGET_CIDRS', '').split(',')
                 if c.strip()]
    parser = argparse.ArgumentParser(prog='nhtui', description=__doc__.split('\n\n')[0])
    parser.add_argument('--hosts', required=True, metavar='FILE',
                        help='"hostname, address" per line, the canary first')
    parser.add_argument('--image', required=True, help='path to the image on this host')
    parser.add_argument('--sha512', required=True, help='expected SHA-512 of the image')
    parser.add_argument('--version', required=True,
                        help='version the devices should report afterwards')
    parser.add_argument('--user', default=os.environ.get('USER'), help='device username')
    parser.add_argument('--cidr', action='append', default=None,
                        help='a target CIDR (repeatable; default DEVICE_TARGET_CIDRS)')
    parser.add_argument('--pins', metavar='FILE',
                        help='"address key-type SHA256:..." per line, already checked')
    parser.add_argument('--concurrency', type=int, default=4,
                        help='hosts a phase runs at once, and the most reloads after the canary')
    parser.add_argument('--file-system', default='flash:')
    parser.add_argument('--report', metavar='FILE',
                        help='write every result as JSON here when you quit')
    args = parser.parse_args(argv)
    cidrs = args.cidr or env_cidrs

    try:
        with open(args.hosts) as handle:
            hosts = load_hosts(handle.read(), cidrs, image=args.image, sha512=args.sha512,
                               version=args.version, flash_dir=args.file_system)
        pins = _read_pins(args.pins, cidrs) if args.pins else {}
        local = LocalRun(hosts, image=args.image, ledger=MemoryLedger(pins=pins),
                         username=args.user or '', concurrency=args.concurrency)
    except (LocalRunError, targets.RequestError, OSError) as exc:
        print(f'nhtui: {exc}', file=sys.stderr)
        return 2

    NetHubTUI(local).run()
    local.close()
    print(_summary(local))
    if args.report:
        with open(args.report, 'w') as handle:
            json.dump({'state': local.run.state,
                       'jobs': [{'phase': j.phase, 'attempt': j.attempt, 'status': j.status,
                                 'failure_stage': j.failure_stage,
                                 'error_summary': j.error_summary} for j in local.jobs],
                       'results': local.ledger.results}, handle, indent=2, default=str)
        print(f'Report written to {args.report}.')
    return 0 if local.run.state == 'completed' else 1


if __name__ == '__main__':
    sys.exit(main())
