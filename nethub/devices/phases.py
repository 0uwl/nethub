"""Running one phase of an upgrade across a run's hosts, and recording it.

This is the layer between the device modules and the database. It owns the
per-host loop, the exception-to-`failure_stage` mapping (§7.3), and the rows:
one `upgrade_host_phase_results` per host per execution, plus the
`upgrade_run_hosts.state` cursor and the phase job's terminal status.

It does **not** own dispatch. The sibling claims a `queued` job, sets it
`running`, and calls `execute_phase`; the startup sweep and the queue are the
sibling's too (§7.3, §9). Everything here therefore assumes it is already
running inside the sibling process -- never inside Flask, which holds the
unauthenticated route.

The credential arrives as an argument and is never written anywhere: not to a
row, not to `error_summary`, not to a log. `_summarise` is what enforces the
last of those: it reads an exception's `summary` attribute rather than its
`str()`, and every exception this codebase raises sets that attribute
explicitly (WS-4.2). An exception with no `summary` -- anything foreign, and
anything of ours that forgot to set one -- reduces to its type name. That is
the trust direction: opt in per exception, rather than an allowlist of
trusted *types* that a wrapper interpolating a foreign exception's text could
slip past.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime, timezone

from nethub.devices import connection, facts, install, transfer
from nethub.extensions import db
from nethub.models import (
    STATE_BEFORE,
    DeviceHostKey,
    UpgradeHostPhaseResult,
    UpgradePhaseJob,
    UpgradeRunHost,
)

#: Per-host cursor value after a phase succeeds. Cleanup does not advance it:
#: a cleaned host is still `verified` (§7.3 -- this column is a cursor, and
#: the per-phase detail lives in the results table).
_STATE_AFTER = {
    'precheck': 'precheck_ok',
    'stage': 'staged',
    'activate': 'activated',
    'verify': 'verified',
    'cleanup': 'verified',
}

#: Phases that run one host at a time whatever `concurrency` says. Activate
#: reloads switches; a fleet is not reloaded four at a time (PLAN.md
#: decision 6). It still goes through the same driver as the others.
SERIAL_PHASES = frozenset({'activate'})

#: Seconds between the driver's ticks while hosts are running: how often it
#: writes `heartbeat_at`, checks cancel and the deadline, and runs anything
#: the sibling hands it (a queued host-key scan). A healthy job's heartbeat
#: is never older than this plus one scan's `connection.CONNECT_TIMEOUT`.
HEARTBEAT_INTERVAL = 30.0

class UnconfirmedHost(Exception):
    """No confirmed `device_host_keys` row for this address (§4.3).

    `summary` is what reaches the year-retained `error_summary` column
    (WS-4.2). This exception never interpolates foreign text, so the default
    of `summary == message` is already correct -- the attribute exists purely
    so `_summarise` can read it the same way it reads every other exception's.
    """

    def __init__(self, message: str, *, summary: str | None = None) -> None:
        super().__init__(message)
        self.summary = summary if summary is not None else message


@dataclass(frozen=True)
class HostOutcome:
    hostname: str
    status: str
    failure_stage: str | None = None
    error_summary: str | None = None
    scp_restore_confirmed: bool | None = None
    version_pre: str | None = None
    version_post: str | None = None
    config_backup: str | None = None

    @property
    def ok(self) -> bool:
        return self.failure_stage is None


@dataclass(frozen=True)
class HostTarget:
    """One host as a worker thread sees it: plain values, no ORM.

    Workers never touch `db.session`. They run without the app context the
    session lives in, and an `UpgradeRunHost` read after a commit reloads
    itself from the database on attribute access. So the main thread copies
    what a phase needs into this before handing a host to a worker, including
    the pinned host key: `pinned_key` queries the database, and activate's
    reconnect after the reload needs the pin from inside its worker.
    """

    hostname: str
    ansible_host: str
    filename: str
    sha512: str
    file_size: int
    version: str
    flash_dir: str
    host_key: connection.HostKey

    @classmethod
    def of(cls, host: UpgradeRunHost) -> HostTarget:
        """Main thread only. Raises `UnconfirmedHost` if the address has no
        confirmed pin."""
        return cls(
            hostname=host.hostname,
            ansible_host=host.ansible_host,
            filename=host.filename,
            sha512=host.sha512,
            file_size=host.file_size,
            version=host.version,
            flash_dir=host.flash_dir,
            host_key=pinned_key(host),
        )


class LoginGate:
    """One login at a time until the credential has worked once.

    Every host in a phase gets the same password, so a mistyped one would be
    refused on every host that is logging in at the same time, and enough
    refusals lock the account out of TACACS+/RADIUS for the whole fleet
    (PLAN.md WS-8). With hosts running in parallel, the first host to reach
    its login goes ahead and the others wait for the answer:

    - accepted: the gate opens and every host logs in as it arrives;
    - refused: the gate shuts, and no host that has not yet logged in tries;
    - neither (unreachable, host-key mismatch): nothing was learned about the
      password, so the next waiting host tries instead.

    A refusal after the gate opened shuts it too. That is a device refusing
    a credential another device accepted, and the phase stops there as well.
    """

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._state = 'untried'  # untried | trying | open | shut
        #: Hostname the credential was first refused on, for the
        #: `not_attempted` rows of the hosts that never tried.
        self.refused_on: str | None = None

    def enter(self) -> str:
        """Block until this host may log in. Returns 'probe' (log in and
        report with `settle`), 'go' (log in), or 'stop' (do not)."""
        with self._cond:
            while self._state == 'trying':
                self._cond.wait()
            if self._state == 'shut':
                return 'stop'
            if self._state == 'open':
                return 'go'
            self._state = 'trying'
            return 'probe'

    def settle(self, accepted: bool | None, hostname: str) -> None:
        """Report a probe's login: True accepted, False refused, None unknown."""
        with self._cond:
            if accepted is False:
                self._shut(hostname)
            elif self._state == 'trying':
                self._state = 'open' if accepted else 'untried'
            self._cond.notify_all()

    def refuse(self, hostname: str) -> None:
        """A credential failure anywhere, at any time: nobody else tries."""
        with self._cond:
            self._shut(hostname)
            self._cond.notify_all()

    def _shut(self, hostname: str) -> None:
        self._state = 'shut'
        if self.refused_on is None:
            self.refused_on = hostname


@dataclass
class PhaseContext:
    """What an execution needs that the run row does not carry.

    The credential lives here and nowhere else: held for the life of *this
    phase execution* only (design doc §9.1 -- not the life of the run, which
    can park at a gate for days), and never copied to a row or a log.

    `search_dir` comes from deployment settings rather than from the run, but
    is read once at dispatch so a settings change mid-run cannot re-point an
    execution halfway through.
    """

    device_username: str
    #: repr=False, as on `sealed_credentials.Credential`. This object is
    #: the credential's entire lifetime container, so its repr is the most
    #: likely accidental leak in the codebase.
    device_password: str = field(repr=False)
    search_dir: str
    reload_wait: install.ReloadWait = install.DEFAULT_RELOAD_WAIT
    #: Injectable so tests need no device and the sibling can pass its own.
    connect: Callable[..., object] | None = None

    def open(self, host: HostTarget):
        opener = self.connect or default_connect
        return opener(host, self.device_username, self.device_password)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes even for values written aware.

    Every comparison against a stored timestamp has to go through this. Without
    it the sibling raises `TypeError: can't compare offset-naive and
    offset-aware datetimes` the first time it checks a deadline that was read
    from the database rather than one it just built -- which is to say, always
    in production and never in a test that skips the round trip.
    """
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def failure_stage_for(exc: BaseException) -> str:
    """Map an exception to §7.3's phase-job vocabulary.

    Ordered most specific first. The point of the mapping living here rather
    than in each device module is that the modules stay usable without the
    database, and this is the only place that has to know the enum.
    """
    if isinstance(exc, (connection.HostKeyError, UnconfirmedHost)):
        return 'hostkey'
    if isinstance(exc, connection.AuthenticationError):
        return 'credential'
    if isinstance(exc, connection.DeviceConnectionError):
        return 'connect'
    if isinstance(exc, transfer.VerificationError):
        return 'checksum'
    if isinstance(exc, transfer.TransferError):
        return 'transfer'
    if isinstance(exc, install.ReloadTimeout):
        return 'reload'
    if isinstance(exc, install.PostCheckError):
        return 'postcheck'
    if isinstance(exc, install.InstallError):
        # `privilege` is worth keeping distinct: it means the submitter's
        # account cannot reach level 15, which is a deployment fault rather
        # than a device fault (§7.3).
        return 'privilege' if exc.status == 'privilege' else 'install'
    if isinstance(exc, facts.FactsError):
        return 'precheck'
    return 'connect'


def _summarise(exc: BaseException) -> str:
    """Read the fault an exception opted to disclose, never its raw `str()`.

    `error_summary` is retained for a year (§7.4). Reading `summary` rather
    than checking a type allowlist is the WS-4.2 fix: a type-level check
    trusted *any* message from an allowed type, including one of ours that
    interpolated a foreign exception's text wholesale. An explicit `summary`
    attribute can only ever say what its own `__init__` chose to put there.
    """
    text = getattr(exc, "summary", None)
    if text is None:
        text = f"unexpected {type(exc).__name__}"
    return " ".join(text.split())[:500]


def pinned_key(host: UpgradeRunHost) -> connection.HostKey:
    """The confirmed pin for this host's address, or refuse.

    An address with no already-confirmed row cannot be named by a run at all
    (§4.3), so an unconfirmed row is treated exactly like a missing one --
    seeing a key is not accepting it.
    """
    row = DeviceHostKey.query.filter_by(ansible_host=host.ansible_host).first()
    if row is None:
        raise UnconfirmedHost(
            f"{host.ansible_host} has no confirmed host key; an admin must "
            f"confirm the fingerprint before a run may target it"
        )
    if not row.is_confirmed:
        raise UnconfirmedHost(
            f"{host.ansible_host} has a host key on file that nobody has "
            f"confirmed; seeing a key is not accepting it"
        )
    return connection.HostKey(row.key_type, row.fingerprint_sha256)


def default_connect(host: HostTarget, username: str, password: str):
    """The pin was looked up on the main thread (`HostTarget.of`); this runs
    in a worker, and again for each reconnect after activate's reload."""
    return connection.connect(host.ansible_host, username, password, host.host_key)


# --------------------------------------------------------------------------
# The five phases. Each takes an open connection and returns a HostOutcome;
# raising is also fine -- run_host turns either into a row.
# --------------------------------------------------------------------------

def phase_precheck(conn, host: HostTarget, ctx: PhaseContext) -> HostOutcome:
    """Read-only. Runs on submit with no gate, so it must change nothing."""
    device = facts.get_facts(conn)
    privilege = facts.get_privilege(conn)
    if privilege < 15:
        raise install.InstallError(
            f"account is at privilege {privilege}, not 15", status='privilege'
        )
    if device.boot_mode != 'INSTALL':
        raise install.InstallError(
            f"boot mode is {device.boot_mode or 'unknown'}, not INSTALL",
            status='wrong_boot_mode',
        )
    filesystem = facts.get_filesystem(conn, host.flash_dir)
    already = filesystem.size_of(host.filename) or 0
    if filesystem.free_bytes + already <= host.file_size:
        raise transfer.TransferError(
            f"{host.flash_dir} has {filesystem.free_bytes} bytes free, "
            f"{host.filename} needs {host.file_size}",
            status='no_space',
        )
    return HostOutcome(host.hostname, 'precheck_ok', version_pre=device.version)


def phase_stage(conn, host: HostTarget, ctx: PhaseContext) -> HostOutcome:
    outcome = transfer.stage_image(
        conn,
        image=host.filename,
        sha512=host.sha512,
        search_dir=ctx.search_dir,
        file_size=host.file_size,
        file_system=host.flash_dir,
    )
    return HostOutcome(
        host.hostname, outcome.status,
        scp_restore_confirmed=outcome.scp_restore_confirmed,
    )


def phase_activate(conn, host: HostTarget, ctx: PhaseContext) -> HostOutcome:
    """Reload, then wait for the device back. Verification is its own phase.

    The reconnect belongs here rather than to `verify` because `verify` cannot
    start without a device to talk to, and because a device that never returns
    is a *reload* failure, which is a different `failure_stage` and a different
    conversation than a wrong version.
    """
    backup = install.capture_running_config(conn)
    result = install.activate(
        conn,
        image=host.filename,
        sha512=host.sha512,
        target_version=host.version,
        file_system=host.flash_dir,
    )
    returned = install.wait_for_device(lambda: ctx.open(host), wait=ctx.reload_wait)
    returned.disconnect()
    return HostOutcome(
        host.hostname, 'activated',
        version_pre=result.version_before,
        config_backup=backup,
    )


def phase_verify(conn, host: HostTarget, ctx: PhaseContext) -> HostOutcome:
    device = install.verify_upgrade(conn, target_version=host.version)
    return HostOutcome(host.hostname, 'verified', version_post=device.version)


def phase_cleanup(conn, host: HostTarget, ctx: PhaseContext) -> HostOutcome:
    result = install.cleanup(conn)
    return HostOutcome(host.hostname, f"removed {len(result.removed)} files")


PHASE_RUNNERS: dict[str, Callable] = {
    'precheck': phase_precheck,
    'stage': phase_stage,
    'activate': phase_activate,
    'verify': phase_verify,
    'cleanup': phase_cleanup,
}


# --------------------------------------------------------------------------
# The driver. Called by the sibling with a job it has already claimed.
# --------------------------------------------------------------------------

def run_host(host: HostTarget, phase: str, ctx: PhaseContext,
             gate: LoginGate | None = None) -> HostOutcome:
    """One host, one phase. Turns success or any exception into an outcome.

    Runs in a worker thread, so it sees a `HostTarget` and a phase name and
    never the job or the session. Never raises: a host that fails is a row,
    not an aborted execution -- the other hosts in the wave still have to be
    attempted and recorded. The first login waits at `gate` (see `LoginGate`).
    """
    runner = PHASE_RUNNERS[phase]
    gate = gate if gate is not None else LoginGate()
    conn = None
    try:
        turn = gate.enter()
        if turn == 'stop':
            return _not_attempted_outcome(host.hostname, gate.refused_on)
        learned = None
        try:
            conn = ctx.open(host)
            learned = True
        except Exception as exc:
            learned = False if failure_stage_for(exc) == 'credential' else None
            raise
        finally:
            # In a `finally` so a probe that dies any other way still lets the
            # hosts waiting behind it go on.
            if turn == 'probe':
                gate.settle(learned, host.hostname)
        return runner(conn, host, ctx)
    except Exception as exc:  # noqa: BLE001 -- every failure becomes a row
        outcome = _failed_outcome(host.hostname, exc)
        if outcome.failure_stage == 'credential':
            gate.refuse(host.hostname)
        return outcome
    finally:
        if conn is not None:
            try:
                conn.disconnect()
            except Exception:  # noqa: BLE001, S110 -- a dead session is already gone
                pass


def _failed_outcome(hostname: str, exc: BaseException) -> HostOutcome:
    return HostOutcome(
        hostname,
        status=getattr(exc, 'status', 'failed'),
        failure_stage=failure_stage_for(exc),
        error_summary=_summarise(exc),
        scp_restore_confirmed=getattr(exc, 'scp_restore_confirmed', None),
    )


def _not_attempted_outcome(hostname: str, after: str | None) -> HostOutcome:
    """A host the phase stopped before logging in to, failed and retryable."""
    return HostOutcome(
        hostname, 'not_attempted', failure_stage='credential',
        error_summary=(f"not attempted: the phase stopped after the credential "
                       f"was refused on {after}"),
    )


def record(host: UpgradeRunHost, job: UpgradePhaseJob, outcome: HostOutcome,
           started_at: datetime) -> None:
    """Write the result row and advance the host cursor.

    The result row is the record of what happened; `UpgradeRunHost.state` is a
    cursor for the dashboard's default view and is derived from it (§5).
    """
    db.session.add(UpgradeHostPhaseResult(
        run_id=job.run_id,
        hostname=host.hostname,
        phase=job.phase,
        attempt=job.attempt,
        status=outcome.status,
        failure_stage=outcome.failure_stage,
        error_summary=outcome.error_summary,
        scp_restore_confirmed=outcome.scp_restore_confirmed,
        started_at=started_at,
        finished_at=_utcnow(),
    ))
    host.last_phase = job.phase
    host.state = _STATE_AFTER[job.phase] if outcome.ok else 'failed'
    if outcome.error_summary:
        host.error_summary = outcome.error_summary
    if outcome.version_pre:
        host.reported_version_pre = outcome.version_pre
    if outcome.version_post:
        host.reported_version_post = outcome.version_post


def eligible_hosts(job: UpgradePhaseJob) -> list[UpgradeRunHost]:
    """The hosts `job`'s phase runs on: those whose cursor sits just before it.

    Not "every host that has not failed". A phase re-approved after it was
    abandoned would otherwise run again on hosts it had already finished --
    a second `install add` on a switch that has just reloaded -- and a retry
    would sweep in hosts that are already past it (PLAN.md WS-8).
    """
    before = STATE_BEFORE[job.phase]
    return [h for h in job.run.hosts if h.state == before]


def reset_for_retry(job: UpgradePhaseJob) -> None:
    """Put the hosts that failed this phase back to where it starts.

    A retry names a phase; this is what makes it run on the hosts that failed
    that phase and on nothing else. Done here, by the sibling, when the job
    starts: §7.3 gives every per-host cursor edge to the sibling. The result
    rows of earlier attempts stay as they were, so the failure is still on
    record.
    """
    for host in job.run.hosts:
        if host.state == 'failed' and host.last_phase == job.phase:
            host.state = STATE_BEFORE[job.phase]
            host.error_summary = None


def _stop_reason(job: UpgradePhaseJob, now: Callable[[], datetime]) -> str | None:
    """Why no further host may start, or None. Main thread only: it reads the
    run, and it is what Flask's cancel reaches through."""
    if job.run.cancel_requested_at is not None:
        return 'cancelled'
    deadline = _aware(job.deadline_at)
    if deadline is not None and now() >= deadline:
        return 'timed_out'
    return None


def execute_phase(job: UpgradePhaseJob, ctx: PhaseContext,
                  now: Callable[[], datetime] = _utcnow, *,
                  concurrency: int = 1,
                  tick_interval: float = HEARTBEAT_INTERVAL,
                  on_tick: Callable[[], None] | None = None) -> str:
    """Run `job`'s phase across every eligible host. Returns the job's status.

    The caller has already claimed the job and set it `running`; this writes
    the terminal edge and the per-host rows, and nothing else touches the job.

    Up to `concurrency` hosts run at once, each in a worker thread
    (PLAN.md WS-9); a phase in `SERIAL_PHASES` runs one at a time through the
    same code. Workers do device I/O and nothing else. This thread, the one
    holding the session, builds each host's `HostTarget` before handing it
    over, writes every row, and every `tick_interval` seconds while hosts run
    it writes `heartbeat_at` and calls `on_tick` (the sibling runs queued
    host-key scans there). The pool is closed before this returns, so no
    worker outlives the phase or the credential the caller clears after it.

    `succeeded` means every host the phase ran on passed, `failed` none, and
    `partial` the rest (PLAN.md WS-8). A host that fails does not stop the
    others -- except on a refused credential. The same password goes to every
    host, so it would be refused on each of them in turn, and enough failed
    logins lock the account out of TACACS+/RADIUS for the whole fleet. So no
    host starts after one, `LoginGate` keeps the hosts already running from
    trying their own login, and every host that never tried is recorded as
    failed (`not_attempted`) so a retry with the right password picks it up.

    Cancel and the deadline stop further hosts from starting; hosts already
    running finish and are recorded (§7.3). There is no safe place to stop
    inside an activation, and a job row polled more finely would still not
    give one.
    """
    if job.is_retry:
        reset_for_retry(job)
    hosts = eligible_hosts(job)
    workers = 1 if job.phase in SERIAL_PHASES else max(1, concurrency)
    gate = LoginGate()
    waiting = list(hosts)
    running: dict = {}
    stopped = None
    refused = False

    with ThreadPoolExecutor(max_workers=workers,
                            thread_name_prefix=f'nethub-{job.phase}') as pool:
        while True:
            while waiting and len(running) < workers and not refused:
                stopped = _stop_reason(job, now)
                if stopped is not None:
                    break
                host = waiting.pop(0)
                started = now()
                try:
                    target = HostTarget.of(host)
                except UnconfirmedHost as exc:
                    record(host, job, _failed_outcome(host.hostname, exc), started)
                    db.session.commit()
                    continue
                future = pool.submit(run_host, target, job.phase, ctx, gate)
                running[future] = (host, started)
            if not running:
                break

            done, _ = wait(running, timeout=tick_interval, return_when=FIRST_COMPLETED)
            for future in done:
                host, started = running.pop(future)
                outcome = future.result()
                record(host, job, outcome, started)
                if outcome.failure_stage == 'credential':
                    refused = True
            job.heartbeat_at = now()
            db.session.commit()
            if on_tick is not None:
                on_tick()
            if stopped is not None and not running:
                break

    if refused:
        for host in waiting:
            record(host, job, _not_attempted_outcome(host.hostname, gate.refused_on), now())
        db.session.commit()
    if stopped is None:
        failed = [h for h in hosts if h.state == 'failed']
        if not failed:
            stopped = 'succeeded'
        else:
            stopped = 'partial' if len(failed) < len(hosts) else 'failed'
            job.failure_stage = _last_failure_stage(job, failed[0].hostname)
            job.error_summary = failed[0].error_summary

    job.status = stopped
    job.finished_at = now()
    db.session.commit()
    return stopped


def _last_failure_stage(job: UpgradePhaseJob, hostname: str) -> str | None:
    row = UpgradeHostPhaseResult.query.filter_by(
        run_id=job.run_id, hostname=hostname, phase=job.phase, attempt=job.attempt
    ).first()
    return row.failure_stage if row else None
