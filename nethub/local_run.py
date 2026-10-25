"""One upgrade run, held in memory and dispatched in this process.

The engine behind `nhtui` (docs/tui.md). It drives the very code the sibling
does -- `phases.execute_phase` for each phase and `flow.next_step` for where
the run goes next -- over dataclasses instead of rows, so the login gate,
the canary, the wave stops and "an activated host is never kept" hold here
by construction ([tui-shares-the-phase-engine]).

Nothing is written anywhere ([tui-stores-nothing]): the result rows live in
`MemoryLedger.results` for the summary. The device password is given once and
held in this run's `PhaseContext` (whose repr hides it) until the process
ends. The run lives no longer than the process, so asking again at each gate
would protect nothing. A phase stopped by a refused credential drops it, and
the next step asks again ([tui-refused-password-is-dropped]). A run made
here is outside NetHub's audit trail.

Imports no Flask and no SQLAlchemy ([tui-imports-no-flask]).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from nethub import flow, targets
from nethub.devices import connection, install, phases
from nethub.sealed_credentials import CredentialError, check_credential
from nethub.vocab import APPROVABLE, RETRYABLE_AT


class LocalRunError(Exception):
    """Not allowed in the run's current state, or a bad setup value."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class Host:
    """The attributes `execute_phase` reads and writes on an `UpgradeRunHost`."""

    hostname: str
    ansible_host: str
    filename: str
    sha512: str
    file_size: int
    version: str
    flash_dir: str = 'flash:'
    state: str = 'pending'
    last_phase: str | None = None
    error_summary: str | None = None
    reported_version_pre: str | None = None
    reported_version_post: str | None = None


@dataclass
class Run:
    hosts: list[Host]
    id: int = 0
    state: str = 'pre_checking'
    awaiting_phase: str | None = None
    cancel_requested_at: datetime | None = None


@dataclass
class Job:
    """The attributes `execute_phase` reads and writes on an `UpgradePhaseJob`.
    No deadline: someone is watching, and cancel exists."""

    run: Run
    phase: str
    attempt: int
    is_retry: bool = False
    concurrency: int | None = None
    run_id: int = 0
    deadline_at: datetime | None = None
    heartbeat_at: datetime | None = None
    status: str = 'running'
    finished_at: datetime | None = None
    failure_stage: str | None = None
    error_summary: str | None = None


@dataclass
class MemoryLedger:
    """`phases.Ledger` in memory. `pins` holds only what the operator
    confirmed this session ([tui-never-tofu])."""

    pins: dict[str, connection.HostKey] = field(default_factory=dict)
    results: list[dict] = field(default_factory=list)

    def add_result(self, **fields) -> None:
        self.results.append(fields)

    def commit(self) -> None:
        pass

    def pin_for(self, address: str) -> connection.HostKey:
        pin = self.pins.get(address)
        if pin is None:
            raise phases.UnconfirmedHost(f'{address} has no confirmed host key in this session')
        return pin


def load_hosts(hosts_raw: str, cidrs, *, image: str, sha512: str, version: str,
               flash_dir: str = 'flash:') -> list[Host]:
    """The run's hosts, after the same checks a web submit makes, and after
    hashing the image: its SHA-512 must be the one claimed, as an upload's is."""
    try:
        hosts = [(name, targets.check_target(address, cidrs))
                 for name, address in targets.parse_hosts(hosts_raw)]
    except targets.RequestError as exc:
        raise LocalRunError(str(exc)) from None
    claimed = (sha512 or '').strip().lower()
    if not os.path.isfile(image):
        raise LocalRunError(f'{image} is not a file.')
    if phases._sha512_file(Path(image)) != claimed:
        raise LocalRunError(f'{os.path.basename(image)} does not match the SHA-512 given.')
    if not (version or '').strip():
        raise LocalRunError('Version is required.')
    filename, size = os.path.basename(image), os.path.getsize(image)
    return [Host(name, address, filename, claimed, size, version.strip(), flash_dir)
            for name, address in hosts]


class LocalRun:
    """A run's gates as methods. Each phase method blocks until the phase
    (and, after activate, its chained verify) has finished; the TUI calls them
    from a worker thread. `on_tick` is `execute_phase`'s, called on that thread
    while hosts are in flight."""

    def __init__(self, hosts: list[Host], *, image: str, ledger: MemoryLedger,
                 username: str, concurrency: int = 1, connect=None,
                 reload_wait: install.ReloadWait = install.DEFAULT_RELOAD_WAIT,
                 on_tick=None):
        if concurrency < 1:
            raise LocalRunError('Concurrency must be at least 1.')
        if not (username or '').strip():
            raise LocalRunError('Device username is required.')
        self.run = Run(hosts)
        self.ledger = ledger
        self.username = username.strip()
        self.concurrency = concurrency
        self.search_dir = os.path.dirname(os.path.abspath(image))
        self.connect = connect
        self.reload_wait = reload_wait
        self.on_tick = on_tick
        self.jobs: list[Job] = []
        #: The run's credential, None until given and after a refusal.
        self._ctx: phases.PhaseContext | None = None

    @property
    def finished(self) -> bool:
        return self.run.state in ('completed', 'failed', 'cancelled')

    @property
    def needs_password(self) -> bool:
        return self._ctx is None

    def close(self) -> None:
        """Drop the credential."""
        self._ctx = None

    def precheck(self, password: str | None = None) -> str:
        if self.run.state != 'pre_checking' or self.jobs:
            raise LocalRunError('Pre-check has already run.')
        return self._dispatch('precheck', password)

    def approve(self, phase: str, password: str | None = None, reload_count=None) -> str:
        self._at_gate(phase)
        concurrency = targets.reload_count(reload_count, self.concurrency) \
            if phase == 'activate' else None
        return self._dispatch(phase, password, concurrency=concurrency)

    def retry(self, phase: str, password: str | None = None, reload_count=None) -> str:
        """Run `phase` again on the hosts that failed it (`RETRYABLE_AT`)."""
        if self.run.state != 'awaiting_approval' or \
                phase not in RETRYABLE_AT.get(self.run.awaiting_phase, ()):
            raise LocalRunError(f'{phase} cannot be retried here.')
        concurrency = targets.reload_count(reload_count, self.concurrency) \
            if phase == 'activate' else None
        return self._dispatch(phase, password, concurrency=concurrency, is_retry=True)

    def decline_cleanup(self) -> None:
        self._at_gate('cleanup')
        self.run.state, self.run.awaiting_phase = 'completed', None

    def cancel(self) -> None:
        """At a gate, ends the run. During a phase, no further host starts;
        hosts already running finish (there is no safe stop inside an
        activation) and the run ends when the phase does."""
        if self.finished:
            raise LocalRunError(f'This run is already {self.run.state}.')
        self.run.cancel_requested_at = _utcnow()
        if self.run.state == 'awaiting_approval':
            self.run.state, self.run.awaiting_phase = 'cancelled', None

    def _at_gate(self, phase: str) -> None:
        if phase not in APPROVABLE or self.run.state != 'awaiting_approval' \
                or self.run.awaiting_phase != phase:
            raise LocalRunError(f'This run is not waiting at the {phase} gate.')

    def _dispatch(self, phase: str, password: str | None, *, concurrency=None,
                  is_retry=False) -> str:
        """`password`, if given, becomes the run's credential; otherwise the
        one already given is used."""
        if password:
            # The same allowlist the web app applies before sealing one.
            for what, value in (('Device username', self.username),
                                ('Device password', password)):
                try:
                    check_credential(value)
                except CredentialError as exc:
                    raise LocalRunError(f'{what} refused: {exc}.') from None
            self._ctx = phases.PhaseContext(
                device_username=self.username, device_password=password,
                search_dir=self.search_dir, reload_wait=self.reload_wait,
                connect=self.connect,
            )
        if self._ctx is None:
            raise LocalRunError('Enter the device password.')
        return self._run_phases(phase, self._ctx, concurrency, is_retry)

    def _run_phases(self, phase, ctx, concurrency, is_retry) -> str:
        run = self.run
        while True:
            attempt = 1 + sum(1 for j in self.jobs if j.phase == phase)
            job = Job(run, phase, attempt, is_retry=is_retry, concurrency=concurrency)
            self.jobs.append(job)
            run.state, run.awaiting_phase = 'running', None
            result = phases.execute_phase(
                job, ctx, ledger=self.ledger, concurrency=self.concurrency,
                on_tick=self.on_tick)
            if result.stopped_by == 'credential':
                # Never offered again: each entry costs one AAA failure at
                # most, and the next step asks for the password anew.
                self._ctx = None
            step, following = flow.next_step(run, job, result)
            if step == 'run':
                # Only verify, after activate, on the same credential.
                phase, concurrency, is_retry = following, None, False
                continue
            if step == 'gate' and run.cancel_requested_at is None:
                run.state, run.awaiting_phase = 'awaiting_approval', following
            elif step == 'gate':
                run.state = 'cancelled'
            else:
                run.state = step
            return result.status
