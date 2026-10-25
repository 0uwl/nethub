"""The upgrade vocabularies: phases, states, statuses and failure stages.

Pure data, so the phase engine and `nethub.local_run` can use them without
importing SQLAlchemy (docs/tui.md [tui-imports-no-flask]). `models.py`
re-exports every name here and builds its CHECK constraints from them.
"""

#: The phases, in order. `cleanup` is optional; declining it closes the run.
PHASES = ('precheck', 'stage', 'activate', 'verify', 'cleanup')

#: The phases with an approval gate in front of them. Lives here with the other
#: vocabularies rather than in `upgrades.py`, because the sibling needs it too:
#: `sweep()` parks an abandoned phase at its gate only if a human can actually
#: approve it, and importing `upgrades` into the sibling would drag the artifact
#: store along with it. `precheck` is absent by design (it runs on submit, with
#: no gate) and so is `verify` (it follows `activate` without one).
APPROVABLE = ('stage', 'activate', 'cleanup')

#: Job status, shared by every job kind so one sweep serves them all.
#: `succeeded` means every host the phase ran on, `failed` means none of them,
#: and `partial` is the rest: the run carries on with the hosts
#: that passed, and the failed ones can be retried. Changing this tuple
#: changes a CHECK constraint and the terminal-status trigger, so it needs a
#: migration (see 0004).
JOB_STATUSES = (
    'queued', 'running',
    'succeeded', 'partial', 'failed', 'timed_out', 'abandoned', 'cancelled', 'expired',
)
TERMINAL_JOB_STATUSES = frozenset(JOB_STATUSES[2:])

#: Run state. `pre_checking` is distinct from `running` because pre-check
#: needs no gate, and it has its own failure/cancel exits for the same reason.
RUN_STATES = (
    'pre_checking', 'awaiting_approval', 'running',
    'completed', 'failed', 'cancelled', 'expired',
)

#: Per-host cursor. The per-phase detail is UpgradeHostPhaseResult; this
#: column is a cursor, not the record of what happened.
HOST_STATES = (
    'pending', 'precheck_ok', 'staged', 'activated', 'verified',
    'failed', 'skipped',
)

#: The cursor a host must be at for a phase to run on it, and the one a retry
#: puts a failed host back to. Cleanup does not advance the
#: cursor, so it runs on, and leaves hosts at, `verified`.
STATE_BEFORE = {
    'precheck': 'pending',
    'stage': 'precheck_ok',
    'activate': 'staged',
    'verify': 'activated',
    'cleanup': 'verified',
}

#: Which phases a retry may name while the run waits at a gate: the ones that
#: ran since the previous gate. A retry then carries on exactly
#: as the phase did the first time and lands back at the same gate. Nothing
#: follows cleanup, so there is no gate at which to retry it.
RETRYABLE_AT = {
    'stage': ('precheck',),
    'activate': ('stage',),
    'cleanup': ('activate', 'verify'),
}

#: Where a phase job failed. A separate vocabulary from the phase names: the
#: phases are themselves named `stage` and `verify`, so sharing one would allow
#: phase='activate', failure_stage='stage', ambiguous on its face. `internal`
#: is an error in NetHub's own code (`Sibling.recover_own`, and any exception
#: `phases.failure_stage_for` does not recognise), never a device problem.
#: `store` is the image in NetHub's own artifact store missing or not matching
#: its recorded digest, found before a stage touches any device. Changing this
#: tuple changes a CHECK constraint, so it needs a migration (see 0002, 0006).
PHASE_FAILURE_STAGES = (
    'credential', 'connect', 'hostkey', 'privilege', 'precheck',
    'transfer', 'checksum', 'install', 'reload', 'postcheck', 'internal', 'store',
)


def is_terminal(status):
    return status in TERMINAL_JOB_STATUSES
