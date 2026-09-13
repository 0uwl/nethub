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

from sqlalchemy import or_

from nethub.credential_socket import CredentialError, fetch_credential
from nethub.devices import install, phases, transfer
from nethub.extensions import db
from nethub.models import APPROVABLE, UpgradePhaseJob, UpgradeRun

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
        db.session.commit()
        return len(stale)

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
