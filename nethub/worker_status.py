"""What the web pages can say about the sibling, read from rows (PLAN.md WS-11).

Flask writes a `queued` row and stops (design doc §9); the sibling does the
rest. A sibling that is not running, or that died mid-phase, changes nothing,
so without these checks the pages just say "still working" forever. Every
function here reads and none writes: the sweep is the sibling's job (§7.3),
and Flask noticing a dead worker must not turn into Flask failing its rows.

There is no sibling heartbeat outside a running phase, so "is anything
picking up work" is inferred from the queue itself (maintainer decision,
2026-09-28): something has waited longer than `NO_WORKER_AFTER` while nothing
is running with a fresh heartbeat. An idle, healthy sibling claims a queued
row within its poll interval, and a busy one has a running job whose
heartbeat is fresh, so neither trips this.
"""
from datetime import timedelta

from .devices.phases import HEARTBEAT_INTERVAL, _aware
from .extensions import db
from .models import HostKeyScan, UpgradePhaseJob
from .upgrades import _utcnow

#: A running phase writes `heartbeat_at` every `HEARTBEAT_INTERVAL` while its
#: hosts are in flight (WS-9). Three missed beats is a stall, not a slow tick:
#: a tick can also run a queued host-key scan, which is bounded by the
#: connection timeout, well inside this.
STALLED_AFTER = timedelta(seconds=3 * HEARTBEAT_INTERVAL)

#: How long a row may sit `queued` with nothing live running before the page
#: asks whether the sibling is up.
NO_WORKER_AFTER = timedelta(minutes=1)


def is_stalled(job, now=None):
    """True for a `running` job whose last heartbeat is older than
    `STALLED_AFTER`. The claim writes `heartbeat_at`, so a running row always
    has one; `started_at` is the fallback for a row some other writer left
    without."""
    if job.status != 'running':
        return False
    beat = job.heartbeat_at or job.started_at
    if beat is None:
        return True
    return (now or _utcnow()) - _aware(beat) > STALLED_AFTER


def _live_work(now):
    """Is any job or scan running under a sibling that is still beating?

    A scan has no heartbeat of its own; it runs for at most the connection
    timeout, so a running scan counts as live while it is younger than
    `STALLED_AFTER`.
    """
    fresh = now - STALLED_AFTER
    running_jobs = UpgradePhaseJob.query.filter_by(status='running').all()
    if any(not is_stalled(job, now) for job in running_jobs):
        return True
    running_scans = HostKeyScan.query.filter_by(status='running').all()
    return any(scan.started_at is not None and _aware(scan.started_at) > fresh
               for scan in running_scans)


def no_worker(now=None):
    """True when something has been queued longer than `NO_WORKER_AFTER` and
    no job or scan is running with a fresh heartbeat. Global, not per-run: the
    sibling takes rows from one FIFO queue, so a missing worker strands every
    queued row at once."""
    now = now or _utcnow()
    cutoff = now - NO_WORKER_AFTER
    oldest = [
        db.session.query(db.func.min(UpgradePhaseJob.created_at))
        .filter(UpgradePhaseJob.status == 'queued').scalar(),
        db.session.query(db.func.min(HostKeyScan.created_at))
        .filter(HostKeyScan.status == 'queued').scalar(),
    ]
    if not any(t is not None and _aware(t) < cutoff for t in oldest):
        return False
    return not _live_work(now)


def queue_depth():
    """`(queued, running)` phase jobs across every run: what an approval made
    now would wait behind. The sibling runs one phase execution at a time,
    oldest first (§9), so this is the whole queue, not this run's share."""
    counts = dict(
        db.session.query(UpgradePhaseJob.status, db.func.count(UpgradePhaseJob.id))
        .filter(UpgradePhaseJob.status.in_(('queued', 'running')))
        .group_by(UpgradePhaseJob.status).all()
    )
    return counts.get('queued', 0), counts.get('running', 0)
