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

from nethub.credential_socket import CredentialError, fetch_credential
from nethub.devices import connection, install, phases
from nethub.extensions import db
from nethub.models import APPROVABLE, HostKeyScan, UpgradePhaseJob, UpgradeRun

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

    connect_socket: Callable
    search_dir: str
    runner_instance_id: str = ''
    now: Callable[[], datetime] = _utcnow
    gate_ttl: timedelta = DEFAULT_GATE_TTL
    reload_wait: install.ReloadWait = install.DEFAULT_RELOAD_WAIT
    #: Handed to every `PhaseContext`; None means `phases.default_connect`.
    #: Only the end-to-end test sets it, to put a fake device behind a real run.
    connect: Callable | None = None

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
            self._fail_run(job.run)
            return
        run = job.run
        run.state = 'awaiting_approval'
        run.awaiting_phase = job.phase
        run.gate_expires_at = self.now() + self.gate_ttl
        run.finished_at = None

    def recover_own(self) -> int:
        """Fail rows this instance left `running` after an unexpected error.

        `main()` calls this when a phase or scan raised something nothing
        downstream handled. The row was claimed under *our* id, and `sweep()`
        only reclaims foreign ids, so without this it stayed `running` until
        the sibling restarted. The loop is single-threaded, so once an
        exception reaches `main()` nothing of ours is legitimately running.

        `failed`, not `abandoned`: the process did not die, our code raised,
        and parking the run for re-approval would invite the same error again.
        The summary is fixed text. The exception went to the log, and
        `error_summary` is retained for a year (§7.4).
        """
        jobs = UpgradePhaseJob.query.filter_by(
            status='running', runner_instance_id=self.runner_instance_id
        ).all()
        for job in jobs:
            job.status = 'failed'
            job.failure_stage = 'connect'
            job.error_summary = 'the sibling hit an unexpected error; see its log'
            job.finished_at = self.now()
            self._fail_run(job.run)
        for scan in HostKeyScan.query.filter_by(
            status='running', runner_instance_id=self.runner_instance_id
        ):
            scan.status = 'failed'
            scan.error_summary = 'the sibling hit an unexpected error; see its log'
            scan.finished_at = self.now()
        db.session.commit()
        return len(jobs)

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
        watching the result page, where a phase job may be a 15-minute stage
        already in flight.
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
        """Conditional update. One changed row means we own the execution.

        A read-then-write double-claims under WAL, and nothing enforces that
        only one sibling is running (§9.1) -- so the claim has to be the
        `WHERE status='queued'` itself, with the rowcount as the answer.
        """
        changed = (
            db.session.query(UpgradePhaseJob)
            .filter(UpgradePhaseJob.id == job_id, UpgradePhaseJob.status == 'queued')
            .update(
                {
                    'status': 'running',
                    'started_at': self.now(),
                    'heartbeat_at': self.now(),
                    'runner_instance_id': self.runner_instance_id,
                },
                synchronize_session=False,
            )
        )
        db.session.commit()
        return changed == 1

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
    # `run_once()`'s loop checks for a queued scan first every iteration (see
    # `main()` below): an admin watching a confirm screen should not queue
    # behind a phase job that may be a 15-minute stage already in flight.

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
        before authentication, so this never touches §9.1's socket.
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
        if not self.claim(job.id):
            return None  # another instance took it; nothing to do

        try:
            username, password = fetch_credential(self.connect_socket, job.id)
        except (CredentialError, OSError) as exc:
            # OSError as well as CredentialError. `fetch_credential` does not
            # wrap `connect_socket()`, and `connect_to(path)._open()` calls a
            # bare `socket.connect(path)` -- so a Flask unit restarting at the
            # moment we pick a job up raises ConnectionRefusedError or
            # FileNotFoundError, neither of which is a CredentialError.
            #
            # That escaped to main()'s `except Exception`, which logs and
            # continues -- but `claim()` had already committed status='running'
            # under *our* runner_instance_id, and `sweep()` only matches rows
            # whose id differs from its own. The instance that stranded the row
            # was structurally incapable of recovering it, so the run sat
            # `running` forever. The rare failure was handled; the common one
            # was not.
            job.status = 'failed'
            job.failure_stage = 'credential'
            job.error_summary = (
                str(exc)[:500] if isinstance(exc, CredentialError)
                # Not str(exc) for an OSError: the message is chosen by the OS
                # and the path, and error_summary is retained for a year
                # (§7.4). The distinction still matters operationally -- a
                # socket that is not there is a different problem from a
                # credential that was refused -- so name the class, not the
                # text.
                else f'could not reach the credential socket ({type(exc).__name__})'
            )
            job.finished_at = self.now()
            self._fail_run(job.run)
            db.session.commit()
            return 'failed'

        ctx = phases.PhaseContext(
            device_username=username,
            device_password=password,
            search_dir=self.search_dir,
            reload_wait=self.reload_wait,
            connect=self.connect,
        )
        try:
            while True:
                status = phases.execute_phase(job, ctx, now=self.now)
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
    def _finish(self, job: UpgradePhaseJob, status: str) -> str:
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
        """
        run = job.run
        if status == 'cancelled':
            run.state, run.finished_at = 'cancelled', self.now()
            return None
        if status != 'succeeded':
            self._fail_run(run)
            return None

        following, gated = NEXT_PHASE[job.phase]
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
        queued = UpgradePhaseJob(
            run_id=run.id, phase=following, attempt=1, status='queued',
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


def main(poll_interval: float = 5.0) -> None:
    """Sweep once, then work the queue until killed.

    `NETHUB_CREDENTIAL_SOCKET` is the path the mount puts the socket at, and
    `NETHUB_SEARCH_DIR` is the published subtree the push reads from (§3.3 --
    NetHub is the one source of the bytes).
    """
    import os
    import time

    from .credential_socket import connect_to

    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')

    socket_path = os.environ.get('NETHUB_CREDENTIAL_SOCKET')
    search_dir = os.environ.get('NETHUB_SEARCH_DIR')
    if not socket_path or not search_dir:
        raise SystemExit(
            'NETHUB_CREDENTIAL_SOCKET and NETHUB_SEARCH_DIR must both be set'
        )

    worker = Sibling(connect_socket=connect_to(socket_path), search_dir=search_dir)
    app = _database_app()
    with app.app_context():
        swept = worker.sweep()
        log.info('runner %s started; swept %d abandoned row(s)',
                 worker.runner_instance_id, swept)
        while True:
            if worker.tick() is None:
                time.sleep(poll_interval)


if __name__ == '__main__':
    main()
