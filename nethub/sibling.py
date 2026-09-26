"""The dispatch process: claims phase jobs, runs them, advances the run.

Design doc §7.3 and §9. This is a *separate process* from Flask, in its own
unit and its own PID namespace -- distinct namespaces are what prevent
same-uid `ptrace` between the two, so never put them in a shared `Pod=`.

Everything after a job row is created belongs here. Flask writes exactly one
job edge (the `queued` row); the sibling writes every other one, plus the
per-host rows via `phases.execute_phase`. The startup sweep lives here rather
than in Flask so it can never fire against a run that is healthy under another
process -- with the consequence, stated in §7.3, that a sibling which dies and
stays dead is swept by nobody. The design has Flask read `heartbeat_at` and
render "stalled" (not built yet; PLAN.md WS-11); noticing is not the sweep's
job.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_

from nethub.devices import connection, install, phases
from nethub.extensions import db
from nethub.models import (
    APPROVABLE,
    STATE_BEFORE,
    HostKeyScan,
    UpgradePhaseJob,
    UpgradeRun,
)
from nethub.sealed_credentials import CredentialError, open_sealed

#: What runs next once a phase succeeds. `None` means a gate: the run parks at
#: `awaiting_approval` until a human approves the next phase. `verify` follows
#: `activate` with no gate because §8.1's table gives it none -- it is
#: read-only and runs on completion, on activate's credential (`run_once`).
NEXT_PHASE = {
    'precheck': ('stage', True),
    'stage': ('activate', True),
    'activate': ('verify', False),
    'verify': ('cleanup', True),
    'cleanup': (None, False),
}

DEFAULT_GATE_TTL = timedelta(days=7)

#: Hosts a phase runs at once unless `PHASE_CONCURRENCY` says otherwise
#: (PLAN.md decision 6).
DEFAULT_PHASE_CONCURRENCY = 4

log = logging.getLogger('nethub.sibling')


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value):
    """SQLite returns naive datetimes for values written aware. See
    phases._aware -- the same trap, and the same one-line fix."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


@dataclass
class Sibling:
    """One dispatcher. `runner_instance_id` is minted per start.

    It is a UUID and never a PID: a PID is reused across container restarts
    and is meaningless across PID namespaces, so a sweep keyed on one would
    either miss abandoned rows or steal live ones (§7.3).
    """

    #: The sibling's private key (nethub/sealed_credentials.py). Only this
    #: process can open what Flask sealed into a job row.
    private_key: object
    search_dir: str
    runner_instance_id: str = ''
    now: Callable[[], datetime] = _utcnow
    gate_ttl: timedelta = DEFAULT_GATE_TTL
    reload_wait: install.ReloadWait = install.DEFAULT_RELOAD_WAIT
    #: Handed to every `PhaseContext`; None means `phases.default_connect`.
    #: Only the end-to-end test sets it, to put a fake device behind a real run.
    connect: Callable | None = None
    #: Hosts a phase runs at once (PLAN.md WS-9). Activate ignores it and runs
    #: one at a time (`phases.SERIAL_PHASES`). `main()` reads it from
    #: `PHASE_CONCURRENCY`.
    phase_concurrency: int = DEFAULT_PHASE_CONCURRENCY
    #: How often a running phase writes its heartbeat and runs queued scans.
    heartbeat_interval: float = phases.HEARTBEAT_INTERVAL

    def __post_init__(self):
        self.runner_instance_id = self.runner_instance_id or str(uuid.uuid4())

    # -- startup ----------------------------------------------------------
    def sweep(self) -> int:
        """Mark rows left `running` by a previous instance as `abandoned`.

        Keyed on `runner_instance_id`: a row still `running` under an id that
        is not ours belongs to a process that is gone. `abandoned` is for
        crashes and stays distinct from `cancelled` and `expired`, which are
        for humans and TTLs -- merging them costs the word its diagnostic
        value, in the column an operator scans first.
        """
        stale = UpgradePhaseJob.query.filter(
            UpgradePhaseJob.status == 'running',
            # `!=` alone evaluates to NULL -- not true -- for a NULL column, so
            # a `running` row with no runner id was invisible to every sweep,
            # forever. Latent rather than live: `claim()` sets status and
            # runner_instance_id in one atomic UPDATE, so no current path
            # produces such a row. It matters because this sweep is the only
            # mechanism that un-sticks a crashed execution, and the explicit
            # predicate is strictly safer than relying on that invariant
            # holding for every future writer.
            or_(
                UpgradePhaseJob.runner_instance_id.is_(None),
                UpgradePhaseJob.runner_instance_id != self.runner_instance_id,
            ),
        ).all()
        for job in stale:
            job.status = 'abandoned'
            job.failure_stage = 'connect'
            job.error_summary = 'runner exited while this phase was running'
            job.finished_at = self.now()
            self._abandon_run(job)
        self._sweep_stale_scans()
        db.session.commit()
        return len(stale)

    def _sweep_stale_scans(self) -> None:
        """Mark a `HostKeyScan` left `running` by a dead instance `abandoned`
        (WS-6.2b). Folded into `sweep()` rather than a separate call so
        nothing has to remember to invoke both; same NULL-safe predicate as
        the phase-job sweep above, same reasoning. Not counted in `sweep()`'s
        return value -- that return is a count of phase rows, asserted
        exactly by existing tests.
        """
        stale = HostKeyScan.query.filter(
            HostKeyScan.status == 'running',
            or_(
                HostKeyScan.runner_instance_id.is_(None),
                HostKeyScan.runner_instance_id != self.runner_instance_id,
            ),
        ).all()
        for scan in stale:
            scan.status = 'abandoned'
            scan.error_summary = 'runner exited while this scan was running'
            scan.finished_at = self.now()

    def _abandon_run(self, job: UpgradePhaseJob) -> None:
        """Park the run back at this phase's gate, rather than failing it.

        §7.3: "An `abandoned` device-touching phase needs a fresh approval,
        not an auto-retry -- the approval is what supplies the credential and
        names the human. The retry is a new row with an incremented
        `attempt`." That retry was unreachable: this used to call
        `_fail_run`, so the run went terminal and `approve()`'s first guard
        (`run.state != 'awaiting_approval'`) refused forever. The whole
        mechanism was built for -- `models.py` says `attempt` exists to permit
        it, and `approve()` computes `1 + count(abandoned)` -- and that
        expression had never returned anything but 1.

        Only a phase someone *can* approve is parked. `precheck` has no gate
        by design (§8.1) and `verify` follows `activate` without one, so
        parking either would leave the run at `awaiting_approval` with an
        `awaiting_phase` that `approve()` refuses as "not a phase anyone
        approves" -- stuck rather than failed, which is worse. Those stay
        terminal, and a fresh submit is the honest answer for them.
        """
        if job.phase not in APPROVABLE:
            if job.is_retry:
                self._return_to_gate(job)
                return
            self._fail_run(job.run)
            return
        run = job.run
        run.state = 'awaiting_approval'
        run.awaiting_phase = job.phase
        run.gate_expires_at = self.now() + self.gate_ttl
        run.finished_at = None

    def _return_to_gate(self, job: UpgradePhaseJob) -> None:
        """Undo an abandoned retry of `precheck` or `verify` (PLAN.md WS-8).

        Nobody approves either phase, so there is no gate of its own to park
        it at; failing the run would throw away every host that had passed.
        The hosts the retry had reset and not reached go back to `failed`, so
        they can be retried again, and the run returns to the gate the retry
        was made from.
        """
        for host in job.run.hosts:
            if host.state == STATE_BEFORE[job.phase] and host.last_phase == job.phase:
                host.state = 'failed'
                host.error_summary = 'runner exited while this phase was being retried'
        following, _gated = NEXT_PHASE[job.phase]
        run = job.run
        run.state = 'awaiting_approval'
        run.awaiting_phase = following
        run.gate_expires_at = self.now() + self.gate_ttl
        run.finished_at = None

    def recover_own(self) -> int:
        """Fail rows this instance left `running` after an unexpected error.

        `main()` calls this when a phase or scan raised something nothing
        downstream handled. The row was claimed under *our* id, and `sweep()`
        only reclaims foreign ids, so without this it stayed `running` until
        the sibling restarted. `execute_phase` joins its worker threads before
        an exception leaves it, so once one reaches `main()` nothing of ours
        is legitimately running.

        `failed`, not `abandoned`: the process did not die, our code raised,
        and parking the run for re-approval would invite the same error again.
        `failure_stage='internal'`, not `connect`: nothing here says the device
        was at fault, and `connect` sent operators looking at the network.
        The summary is fixed text. The exception went to the log, and
        `error_summary` is retained for a year (§7.4).
        """
        jobs = UpgradePhaseJob.query.filter_by(
            status='running', runner_instance_id=self.runner_instance_id
        ).all()
        for job in jobs:
            job.status = 'failed'
            job.failure_stage = 'internal'
            job.error_summary = 'the sibling hit an unexpected error; see its log'
            job.finished_at = self.now()
            self._fail_run(job.run)
        self._fail_own_scans()
        db.session.commit()
        return len(jobs)

    def _fail_own_scans(self) -> None:
        for scan in HostKeyScan.query.filter_by(
            status='running', runner_instance_id=self.runner_instance_id
        ):
            scan.status = 'failed'
            scan.error_summary = 'the sibling hit an unexpected error; see its log'
            scan.finished_at = self.now()

    def expire_gates(self) -> int:
        """Move runs parked at a gate past `gate_expires_at` to `expired`.

        §7.3 gives this edge to the sibling. No job row exists at a gate (the
        approval is what writes one), so only the run changes. Candidates are
        compared in Python, through `_aware`, because SQLite hands the column
        back naive; there are few parked runs. The write is then conditional on
        the run still being at the gate, the same shape as `claim()`: an
        approval Flask commits between our read and our write must win, not be
        overwritten with `expired`.
        """
        now = self.now()
        due = [
            run.id for run in UpgradeRun.query.filter(
                UpgradeRun.state == 'awaiting_approval',
                UpgradeRun.gate_expires_at.isnot(None),
            )
            if now >= _aware(run.gate_expires_at)
        ]
        if not due:
            return 0
        changed = (
            db.session.query(UpgradeRun)
            .filter(UpgradeRun.id.in_(due), UpgradeRun.state == 'awaiting_approval')
            .update(
                {'state': 'expired', 'awaiting_phase': None, 'finished_at': now},
                synchronize_session=False,
            )
        )
        db.session.commit()
        return changed

    # -- the loop ---------------------------------------------------------
    def tick(self) -> str | None:
        """One pass of `main()`'s loop. Returns None when there was nothing to do.

        A scan is checked before a phase job every pass (WS-6.2b): it is
        bounded by `connection.CONNECT_TIMEOUT` and an admin is very likely
        watching the result page. A scan queued while a phase is running does
        not wait for this pass: `execute_phase` calls `_between_hosts` on
        every heartbeat tick, which runs it then (PLAN.md WS-9).
        """
        try:
            self.expire_gates()
            status = self.run_scan_once()
            if status is None:
                status = self.run_once()
            return status
        except Exception:
            log.exception('phase execution raised; continuing')
            db.session.rollback()
            try:
                self.recover_own()
            except Exception:
                log.exception('could not fail the stranded row(s)')
                db.session.rollback()
            return None

    # -- the queue --------------------------------------------------------
    def claim(self, job_id: int) -> bool:
        """Conditional update. One changed row means we own the execution."""
        claimed, _sealed = self._claim(job_id)
        return claimed

    def _claim(self, job_id: int) -> tuple[bool, bytes | None]:
        """Claim the job and take its sealed credential off the row, at once.

        A read-then-write double-claims under WAL, and nothing enforces that
        only one sibling is running (§9.1) -- so the claim has to be the
        `WHERE status='queued'` itself, with the rowcount as the answer.

        The ciphertext is cleared in that same statement, so a claimed job
        never carries it (the CHECK constraint on the table holds every writer
        to that). SQLite's RETURNING gives the *new* row, which would be NULL,
        so the value is read first and the update is made conditional on it
        being unchanged: one changed row means both that the job is ours and
        that the bytes read are the ones cleared.
        """
        sealed = db.session.query(UpgradePhaseJob.sealed_credential).filter(
            UpgradePhaseJob.id == job_id).scalar()
        unchanged = (UpgradePhaseJob.sealed_credential.is_(None) if sealed is None
                     else UpgradePhaseJob.sealed_credential == sealed)
        changed = (
            db.session.query(UpgradePhaseJob)
            .filter(UpgradePhaseJob.id == job_id, UpgradePhaseJob.status == 'queued',
                    unchanged)
            .update(
                {
                    'status': 'running',
                    'started_at': self.now(),
                    'heartbeat_at': self.now(),
                    'runner_instance_id': self.runner_instance_id,
                    'sealed_credential': None,
                },
                synchronize_session=False,
            )
        )
        db.session.commit()
        return changed == 1, sealed

    def next_queued(self) -> UpgradePhaseJob | None:
        """One FIFO queue, ordered by `created_at` -- `started_at` is null
        until dispatch, so there is nothing else to order by (§5)."""
        return (
            UpgradePhaseJob.query.filter_by(status='queued')
            .order_by(UpgradePhaseJob.created_at, UpgradePhaseJob.id)
            .first()
        )

    # -- host-key scans (WS-6.2b) -------------------------------------------
    #
    # A scan is dispatched exactly like a phase job -- same conditional-claim
    # shape, same queue shape -- but simpler: no credential, no PhaseContext,
    # no gate, no state machine beyond queued/running/succeeded/failed/
    # abandoned. It gets its own small queue rather than a shared one because
    # an admin watching a confirm screen should not queue behind a phase job
    # that may be an hour-long stage: `tick()` checks for a scan first, and a
    # running phase takes one on every heartbeat tick (`_between_hosts`).

    def next_queued_scan(self) -> HostKeyScan | None:
        return (
            HostKeyScan.query.filter_by(status='queued')
            .order_by(HostKeyScan.created_at, HostKeyScan.id)
            .first()
        )

    def claim_scan(self, scan_id: int) -> bool:
        """Same conditional-update claim as `claim()`, for `HostKeyScan` rows."""
        changed = (
            db.session.query(HostKeyScan)
            .filter(HostKeyScan.id == scan_id, HostKeyScan.status == 'queued')
            .update(
                {
                    'status': 'running',
                    'started_at': self.now(),
                    'runner_instance_id': self.runner_instance_id,
                },
                synchronize_session=False,
            )
        )
        db.session.commit()
        return changed == 1

    def run_scan_once(self) -> str | None:
        """Take at most one queued scan and execute it.

        No credential fetch and no `PhaseContext`: the host key is exchanged
        before authentication, so this never needs a sealed credential.
        """
        scan = self.next_queued_scan()
        if scan is None:
            return None
        if not self.claim_scan(scan.id):
            return None  # another instance took it; nothing to do

        try:
            key = connection.scan_host_key(scan.ansible_host)
        except connection.DeviceConnectionError as exc:
            scan.status = 'failed'
            # phases._summarise reads exc.summary rather than str(exc)
            # (WS-4.2) -- the same "state the fault, don't quote the OS or
            # the peer" discipline as everywhere else a device exception is
            # recorded, folding in WS-5.4's fix for this specific finding.
            scan.error_summary = phases._summarise(exc)
            scan.finished_at = self.now()
            db.session.commit()
            return 'failed'

        scan.status = 'succeeded'
        scan.key_type = key.key_type
        scan.fingerprint_sha256 = key.fingerprint_sha256
        scan.finished_at = self.now()
        db.session.commit()
        return 'succeeded'

    def _between_hosts(self) -> None:
        """Run while a phase's hosts are in flight, on `execute_phase`'s thread.

        Takes a queued host-key scan, so an admin confirming a new switch does
        not wait behind a stage that may run for an hour. A scan that raises
        fails itself and nothing else: letting the exception out would reach
        `tick()`, whose `recover_own()` would fail the phase job as well.
        """
        try:
            self.run_scan_once()
        except Exception:
            log.exception('host-key scan raised during a phase; failing the scan')
            db.session.rollback()
            self._fail_own_scans()
            db.session.commit()

    def run_once(self) -> str | None:
        """Take at most one job off the queue and see it through.

        "Through" includes a phase with no gate that follows it. `verify` is
        the only one: the sibling queues it when `activate` succeeds, and no
        approval ever holds a credential for it, so fetching one could only
        fail. It runs here instead, on the credential still in hand from
        `activate`, and that credential is cleared once `verify` ends.
        Approving `activate` therefore releases both. Returns the status of
        the last job run.
        """
        job = self.next_queued()
        if job is None:
            return None

        stopped = self._stop_before_claim(job)
        if stopped is not None:
            return stopped
        claimed, sealed = self._claim(job.id)
        if not claimed:
            return None  # another instance took it; nothing to do

        try:
            credential = open_sealed(
                self.private_key, sealed, job_id=job.id,
                approved_by=self._supplier(job), now=self.now(),
            )
        except CredentialError as exc:
            # The row is ours now (`running` under our runner id), and sweep()
            # only matches other ids, so it must be finished here or it sits
            # `running` forever. CredentialError messages are fixed text of
            # ours, never library or peer text -- error_summary is kept a year.
            job.status = 'failed'
            job.failure_stage = 'credential'
            job.error_summary = str(exc)[:500]
            job.finished_at = self.now()
            self._fail_run(job.run)
            db.session.commit()
            return 'failed'

        ctx = phases.PhaseContext(
            device_username=credential.username,
            device_password=credential.password,
            search_dir=self.search_dir,
            reload_wait=self.reload_wait,
            connect=self.connect,
        )
        try:
            while True:
                status = phases.execute_phase(
                    job, ctx, now=self.now, concurrency=self.phase_concurrency,
                    tick_interval=self.heartbeat_interval, on_tick=self._between_hosts)
                following = self._advance_run(job, status)
                db.session.commit()
                if following is None:
                    return status
                # Go through the same checks and the same conditional claim as
                # a job taken off the queue, so a cancel that landed during
                # activate still stops verify.
                stopped = self._stop_before_claim(following)
                if stopped is not None:
                    return stopped
                if not self.claim(following.id):
                    return status
                job = following
        finally:
            # The credential is bound to the approval's executions and nothing
            # longer: this one, plus a gateless phase chained onto it.
            ctx.device_password = ''

    def _stop_before_claim(self, job: UpgradePhaseJob) -> str | None:
        """Finish a queued job that must not start: cancelled, or past its
        deadline. Returns the status it was given, or None to go ahead."""
        if job.run.cancel_requested_at is not None:
            return self._finish(job, 'cancelled')
        deadline = _aware(job.deadline_at)
        if deadline is not None and self.now() >= deadline:
            return self._finish(job, 'expired')
        return None

    # -- the run state machine (§7.3) -------------------------------------
    @staticmethod
    def _supplier(job: UpgradePhaseJob) -> int:
        """Whose credential this job must carry: the approver for a gated
        phase, the submitter for pre-check, which has no gate (§8.1). Refusing
        a null here once failed every pre-check dispatched."""
        return job.approved_by if job.approved_by is not None else job.run.submitted_by

    def _finish(self, job: UpgradePhaseJob, status: str) -> str:
        """End a queued job that must not start: cancelled, or past its deadline.

        The ciphertext goes in the same update, because the terminal-status
        trigger forbids touching the row afterwards. An expired job that was
        carrying one says so (maintainer decision, PLAN.md WS-7): the operator
        sees a credential that waited past its deadline, not a bare `expired`.
        """
        if status == 'expired' and job.sealed_credential is not None:
            job.failure_stage = 'credential'
            job.error_summary = ('not started before its deadline; its credential '
                                 'was discarded unused')
        job.sealed_credential = None
        job.status = status
        job.finished_at = self.now()
        run = job.run
        run.state = status if status in ('cancelled', 'expired') else 'failed'
        run.finished_at = self.now()
        db.session.commit()
        return status

    def _fail_run(self, run: UpgradeRun) -> None:
        run.state = 'failed'
        run.awaiting_phase = None
        run.gate_expires_at = None
        run.finished_at = self.now()

    def _advance_run(self, job: UpgradePhaseJob, status: str) -> UpgradePhaseJob | None:
        """Move the run on after `job` ended with `status`.

        Returns the job queued for a phase with no gate, which the caller runs
        on the same credential (see `run_once`); None otherwise.

        A `partial` phase moves the run on with the hosts that passed (PLAN.md
        WS-8). So does a `failed` one while hosts that passed earlier phases
        are still in the run: that is a retry, or a re-approved abandoned
        phase, that got nowhere, and the run goes back to the gate it was at
        with those hosts intact. On a first attempt `failed` means every host
        in the run has failed, and the run fails.
        """
        run = job.run
        if status == 'cancelled':
            run.state, run.finished_at = 'cancelled', self.now()
            return None
        carries_on = status in ('succeeded', 'partial') or (
            status == 'failed' and any(h.state not in ('failed', 'skipped') for h in run.hosts)
        )
        if not carries_on:
            self._fail_run(run)
            return None

        following, gated = NEXT_PHASE[job.phase]
        while following is not None and not gated and not any(
                h.state == STATE_BEFORE[following] for h in run.hosts):
            # Nothing to run it on: a retried activate that activated nothing
            # has nothing for verify to check. Go to where verify would lead.
            following, gated = NEXT_PHASE[following]
        if following is None:
            run.state = 'completed'
            run.awaiting_phase = None
            run.gate_expires_at = None
            run.finished_at = self.now()
            return None
        if gated:
            # An approval is a row, so the run parks here until a human writes
            # one. `awaiting_phase` says which gate -- inferring it from the
            # highest phase row present cannot tell "awaiting cleanup" from
            # "cleanup declined" (§5).
            run.state = 'awaiting_approval'
            run.awaiting_phase = following
            run.gate_expires_at = self.now() + self.gate_ttl
            return None
        run.state = 'running'
        run.awaiting_phase = None
        run.gate_expires_at = None
        # Not always 1: a retried activate is followed by verify again, on the
        # hosts it activated, and UNIQUE(run_id, phase, attempt) holds.
        previous = (db.session.query(db.func.max(UpgradePhaseJob.attempt))
                    .filter_by(run_id=run.id, phase=following).scalar())
        queued = UpgradePhaseJob(
            run_id=run.id, phase=following, attempt=(previous or 0) + 1, status='queued',
            created_at=self.now(),
        )
        db.session.add(queued)
        return queued


def _database_app():
    """A Flask app for nothing but SQLAlchemy's context.

    Deliberately *not* `create_app()`: that registers routes and runs the
    first-boot admin bootstrap, and the sibling must do neither. It loads
    `shared_config` only, never the web tier's `config.py`, so it needs no
    `SECRET_KEY`: the sibling signs nothing, and should not hold the key that
    forges an admin session. The two processes share the database settings
    and nothing else -- separate units in separate PID namespaces on
    purpose (§9).
    """
    from flask import Flask

    from . import shared_config

    app = Flask(__name__)
    app.config.from_object(shared_config)
    db.init_app(app)
    return app


def phase_concurrency(value: str | None) -> int:
    """`PHASE_CONCURRENCY`, or refuse to start. Unset means the default."""
    if value is None or not value.strip():
        return DEFAULT_PHASE_CONCURRENCY
    try:
        number = int(value)
    except ValueError:
        number = 0
    if number < 1:
        raise SystemExit(f'PHASE_CONCURRENCY must be a whole number of at least 1, '
                         f'not {value!r}')
    return number


def main(poll_interval: float = 5.0) -> None:
    """Wait for the schema, sweep once, then work the queue until killed.

    The sibling never migrates the database (nethub/schema.py). After an
    upgrade it waits here until the web unit's startup has brought the
    database to the revision this code expects.

    `NETHUB_SEARCH_DIR` is the published subtree the push reads from (§3.3 --
    NetHub is the one source of the bytes). The private key comes from the
    systemd credential `credential_private_key` or `NETHUB_CREDENTIAL_KEY_FILE`,
    and must match `NETHUB_CREDENTIAL_PUBLIC_KEY`, the key Flask seals to.
    `PHASE_CONCURRENCY` is how many hosts a phase runs at once (default 4).
    """
    import os
    import time

    from . import sealed_credentials, shared_config

    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
    # Alembic logs "Context impl SQLiteImpl." at INFO on every revision check,
    # and wait_for_current_schema checks every few seconds. nethub.schema says
    # what matters itself.
    logging.getLogger('alembic').setLevel(logging.WARNING)

    search_dir = os.environ.get('NETHUB_SEARCH_DIR')
    if not search_dir:
        raise SystemExit('NETHUB_SEARCH_DIR must be set')
    try:
        private_key = sealed_credentials.load_private_key()
        sealed_credentials.check_pair(private_key, sealed_credentials.load_public_key(
            shared_config.NETHUB_CREDENTIAL_PUBLIC_KEY))
    except sealed_credentials.KeyConfigError as exc:
        raise SystemExit(str(exc)) from None

    from . import schema

    worker = Sibling(private_key=private_key, search_dir=search_dir,
                     phase_concurrency=phase_concurrency(os.environ.get('PHASE_CONCURRENCY')))
    app = _database_app()
    schema.wait_for_current_schema(app.config['SQLALCHEMY_DATABASE_URI'])
    with app.app_context():
        swept = worker.sweep()
        log.info('runner %s started; swept %d abandoned row(s)',
                 worker.runner_instance_id, swept)
        while True:
            if worker.tick() is None:
                time.sleep(poll_interval)


if __name__ == '__main__':
    main()
