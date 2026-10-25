"""Where a run goes after a phase ends: the decisions of the run state machine.

Pure, so the sibling (over rows) and `nethub.local_run` (in memory) make the
same decision from one copy (docs/dispatch.md, docs/tui.md
[tui-shares-the-phase-engine]). `run` and `job` are duck-typed: anything with
the attributes an `UpgradeRun` and an `UpgradePhaseJob` have.
"""

from __future__ import annotations

from nethub.vocab import APPROVABLE, RETRYABLE_AT, STATE_BEFORE

#: What runs next once a phase succeeds. `None` means a gate: the run parks at
#: `awaiting_approval` until a human approves the next phase. `verify` follows
#: `activate` with no gate -- it is
#: read-only and runs on completion, on activate's credential (`run_once`).
NEXT_PHASE = {
    'precheck': ('stage', True),
    'stage': ('activate', True),
    'activate': ('verify', False),
    'verify': ('cleanup', True),
    'cleanup': (None, False),
}


def next_step(run, job, result) -> tuple[str, str | None]:
    """What follows `job` ending as `result` (a `phases.PhaseResult`).

    One of `('cancelled', None)`, `('failed', None)`, `('completed', None)`,
    `('gate', phase)` or `('run', phase)`; only `verify` is ever `run`.

    A phase with a gate of its own that stopped on its first attempt goes
    back to that gate: a refused password, a fault in NetHub's code, a failed
    canary or a bad image in the store left the hosts it did not reach where
    they were, so approving the gate again runs them. That is what keeps a
    mistyped password from failing the run. Hosts that activated before an
    activate stopped wait for the `verify` that follows the next activate. It
    goes back only while some host is still eligible for the phase: an
    activate host that failed after its login is marked failed rather than
    kept (it may have reloaded), and a gate with nobody left to run on is no
    gate.

    A `partial` phase moves the run on with the hosts that passed. So does a
    `failed` one while hosts that passed earlier phases are still in the run:
    that is a retry, or a re-approved abandoned phase, that got nowhere, and
    the run goes back to the gate it was at with those hosts intact. On a
    first attempt `failed` means every host in the run has failed, and the
    run fails.
    """
    status = result.status
    if status == 'cancelled':
        return 'cancelled', None
    if (result.stopped_by is not None and job.phase in APPROVABLE
            and not job.is_retry
            and any(h.state == STATE_BEFORE[job.phase] for h in run.hosts)):
        return 'gate', job.phase
    carries_on = status in ('succeeded', 'partial') or (
        status == 'failed' and any(h.state not in ('failed', 'skipped') for h in run.hosts)
    )
    if not carries_on:
        return 'failed', None

    following, gated = NEXT_PHASE[job.phase]
    while following is not None and not gated and not any(
            h.state == STATE_BEFORE[following] for h in run.hosts):
        # Nothing to run it on: a retried activate that activated nothing
        # has nothing for verify to check. Go to where verify would lead.
        following, gated = NEXT_PHASE[following]
    if following is None:
        return 'completed', None
    return ('gate' if gated else 'run'), following


def retryable_phases(run):
    """What a retry may name at the run's current gate, with the hosts it would
    run on: `[(phase, [hostname, ...]), ...]`, phases with no failed host left
    out."""
    if run.state != 'awaiting_approval':
        return []
    out = []
    for phase in RETRYABLE_AT.get(run.awaiting_phase, ()):
        failed = [h.hostname for h in run.hosts
                  if h.state == 'failed' and h.last_phase == phase]
        if failed:
            out.append((phase, failed))
    return out
