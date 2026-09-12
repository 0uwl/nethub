"""The dispatch process: claims phase jobs, runs them, advances the run.

Design doc §7.3 and §9. This is a *separate process* from Flask, in its own
unit and its own PID namespace -- distinct namespaces are what prevent
same-uid `ptrace` between the two, so never put them in a shared `Pod=`.

Everything after a job row is created belongs here. Flask writes exactly one
job edge (the `queued` row); the sibling writes every other one, plus the
per-host rows via `phases.execute_phase`. The startup sweep lives here rather
than in Flask so it can never fire against a run that is healthy under another
process -- with the consequence, stated in §7.3, that a sibling which dies and
stays dead is swept by nobody. Flask reads `heartbeat_at` and renders
"stalled"; noticing is not the sweep's job.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from nethub.credential_socket import CredentialError, fetch_credential
from nethub.devices import install, phases, transfer
from nethub.extensions import db
from nethub.models import UpgradePhaseJob, UpgradeRun

#: What runs next once a phase succeeds. `None` means a gate: the run parks at
#: `awaiting_approval` until a human approves the next phase. `verify` follows
#: `activate` with no gate because §8.1's table gives it none -- it is
#: read-only and runs on completion.
NEXT_PHASE = {
    'precheck': ('stage', True),
    'stage': ('activate', True),
    'activate': ('verify', False),
    'verify': ('cleanup', True),
    'cleanup': (None, False),
}

DEFAULT_GATE_TTL = timedelta(days=7)


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
    pull_target: transfer.PullTarget | None = None
    reload_wait: install.ReloadWait = install.DEFAULT_RELOAD_WAIT

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
            UpgradePhaseJob.runner_instance_id != self.runner_instance_id,
        ).all()
        for job in stale:
            job.status = 'abandoned'
            job.failure_stage = 'connect'
            job.error_summary = 'runner exited while this phase was running'
            job.finished_at = self.now()
            self._fail_run(job.run)
        db.session.commit()
        return len(stale)

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

    def run_once(self) -> str | None:
        """Take at most one job off the queue and see it through."""
        job = self.next_queued()
        if job is None:
            return None

        if job.run.cancel_requested_at is not None:
            return self._finish(job, 'cancelled')
        deadline = _aware(job.deadline_at)
        if deadline is not None and self.now() >= deadline:
            return self._finish(job, 'expired')
        if not self.claim(job.id):
            return None  # another instance took it; nothing to do

        try:
            username, password = fetch_credential(self.connect_socket, job.id)
        except CredentialError as exc:
            job.status = 'failed'
            job.failure_stage = 'credential'
            job.error_summary = str(exc)[:500]
            job.finished_at = self.now()
            self._fail_run(job.run)
            db.session.commit()
            return 'failed'

        ctx = phases.PhaseContext(
            device_username=username,
            device_password=password,
            search_dir=self.search_dir,
            pull_target=self.pull_target,
            reload_wait=self.reload_wait,
        )
        try:
            status = phases.execute_phase(job, ctx, now=self.now)
        finally:
            # The credential is bound to this execution and nothing longer.
            ctx.device_password = ''
        self._advance_run(job, status)
        db.session.commit()
        return status

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

    def _advance_run(self, job: UpgradePhaseJob, status: str) -> None:
        run = job.run
        if status == 'cancelled':
            run.state, run.finished_at = 'cancelled', self.now()
            return
        if status != 'succeeded':
            self._fail_run(run)
            return

        following, gated = NEXT_PHASE[job.phase]
        if following is None:
            run.state = 'completed'
            run.awaiting_phase = None
            run.gate_expires_at = None
            run.finished_at = self.now()
            return
        if gated:
            # An approval is a row, so the run parks here until a human writes
            # one. `awaiting_phase` says which gate -- inferring it from the
            # highest phase row present cannot tell "awaiting cleanup" from
            # "cleanup declined" (§5).
            run.state = 'awaiting_approval'
            run.awaiting_phase = following
            run.gate_expires_at = self.now() + self.gate_ttl
            return
        run.state = 'running'
        run.awaiting_phase = None
        run.gate_expires_at = None
        db.session.add(UpgradePhaseJob(
            run_id=run.id, phase=following, attempt=1, status='queued',
            created_at=self.now(),
        ))


def _database_app():
    """A Flask app for nothing but SQLAlchemy's context.

    Deliberately *not* `create_app()`: that registers routes and runs the
    first-boot admin bootstrap, and the sibling must do neither. It shares the
    configuration and the engine, and nothing else -- the two processes are
    separate units in separate PID namespaces on purpose (§9).
    """
    from flask import Flask

    from . import config as config_module

    app = Flask(__name__)
    app.config.from_object(config_module)
    db.init_app(app)
    return app


def main(poll_interval: float = 5.0) -> None:
    """Sweep once, then work the queue until killed.

    `NETHUB_CREDENTIAL_SOCKET` is the path the mount puts the socket at, and
    `NETHUB_SEARCH_DIR` is the published subtree both transports read from
    (§3.3 -- one source, whichever direction the bytes move).
    """
    import logging
    import os
    import time

    from .credential_socket import connect_to

    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
    log = logging.getLogger('nethub.sibling')

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
            try:
                status = worker.run_once()
            except Exception:
                log.exception('phase execution raised; continuing')
                db.session.rollback()
                status = None
            if status is None:
                time.sleep(poll_interval)


if __name__ == '__main__':
    main()
