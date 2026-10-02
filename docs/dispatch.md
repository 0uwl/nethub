# Dispatch
Code: `nethub/sibling.py`, `nethub/devices/phases.py`, `nethub/upgrades.py` (gate side), `nethub/models.py` (vocabularies) · Tests: `tests/test_sibling.py`, `tests/test_phases.py`, `tests/test_upgrade_routes.py`, `tests/test_upgrade_models.py`, `tests/test_end_to_end.py`

---

## AI READING INSTRUCTION

Read `[SPEC]` and `[BUG]` blocks for authoritative facts.
Read `[NOTE]` only if additional context is needed.
`[?]` blocks are unverified; treat them with lower confidence.
§1–§4 are rules: each H3 heading is a rule's slug. §5 says how it works,
§6 gives decisions, §7 pitfalls and §8 known gaps.

---

## 1. Claiming, sweeping and recovering

### 1.1 [flask-writes-only-queued]
**[SPEC]**
Flask creates job rows at `queued` and never changes a job's status after
that. The sibling writes every later job edge and every per-host cursor
edge.
Pinned by: `tests/test_upgrade_routes.py::TestGates::test_cancelling_a_running_run_only_sets_the_column`

**[NOTE]**
Flask does write run state, but only at a gate:
- approve or retry: the run goes to `running`;
- cancel at a gate: `cancelled`;
- decline cleanup: `completed`.

A cancel request also clears the ciphertext on queued jobs; that is not a
status edge. §5 has the full edge tables.

### 1.2 [claim-is-conditional]
**[SPEC]**
The sibling claims a job with `UPDATE … WHERE id=? AND status='queued'`,
and the rowcount is the answer. A read-then-write lets two siblings claim
the same job, and nothing enforces that only one sibling runs.
Pinned by: `tests/test_sibling.py::TestClaim::test_only_one_sibling_wins`

**[NOTE]**
The same statement takes the sealed credential off the row
(credentials.md [claim-clears-ciphertext]).

### 1.3 [runner-id-is-uuid]
**[SPEC]**
`runner_instance_id` is a UUID minted at each sibling start, never a PID.
PIDs are reused across container restarts and mean nothing across PID
namespaces.
Pinned by: `tests/test_sibling.py::TestRunnerInstanceId::test_it_is_a_uuid_and_not_a_pid`

### 1.4 [sweep-null-safe]
**[SPEC]**
`sweep()` matches `runner_instance_id IS NULL OR runner_instance_id != self`.
In SQL, `!=` alone is NULL for a NULL column, which would leave such a row
stuck `running` forever.
Pinned by: `tests/test_sibling.py::TestSweepPredicate::test_a_null_runner_id_is_swept`

### 1.5 [abandoned-needs-approval]
**[SPEC]**
- A job left `running` by a dead sibling is never re-run automatically.
- If its phase is in `APPROVABLE` (stage, activate, cleanup), the run is
  parked at that phase's own gate for a fresh approval, even when the job
  was a retry made from a later gate.
- An abandoned precheck or verify fails the run, except an abandoned
  *retry* of one, which goes back to the gate it came from
  (`_return_to_gate`).

Pinned by: `tests/test_sibling.py::TestAbandonedPhaseCanBeReApproved::test_an_abandoned_stage_parks_at_its_gate`, `tests/test_sibling.py::TestAbandonedPhaseCanBeReApproved::test_an_abandoned_precheck_still_fails_the_run`, `tests/test_sibling.py::TestPerHostContinuation::test_an_abandoned_precheck_retry_returns_to_the_stage_gate`

**[NOTE]**
Precheck and verify have no gate. Parking either one would leave the run
at `awaiting_approval` with a phase that `approve()` refuses. That is
stuck rather than failed, which is worse, because it looks recoverable.

### 1.6 [recover-own-is-internal]
**[SPEC]**
An exception that escapes a phase or scan is caught by `Sibling.tick()`,
which rolls back and calls `recover_own()`. That marks this instance's
`running` rows `failed`, with `failure_stage='internal'` and a fixed
summary, and fails the run.
Pinned by: `tests/test_sibling.py::TestUnexpectedErrorDoesNotStrandTheRow::test_a_raising_phase_fails_the_job_and_the_run`

**[NOTE]**
It is `failed` rather than `abandoned` because the process did not die;
our code raised, and re-approving would hit the same bug. `sweep()` cannot
do this job, because it skips this instance's own id.

### 1.7 [credential-failure-finished-in-run-once]
**[SPEC]**
A credential that does not open fails its job inside `run_once`, and is
never left for the loop to handle. By then the job is `running` under this
sibling's own id, which `sweep()` skips.
Pinned by: `tests/test_sibling.py::TestCredentialFailure::test_no_credential_fails_the_phase_at_stage_credential`

### 1.8 [terminal-rows-immutable]
**[SPEC]**
The trigger `upgrade_phase_jobs_terminal_immutable` refuses any update to
a job in a terminal status, raising `IntegrityError`. So anything that
must change on a terminal job, such as clearing `sealed_credential`, has
to happen in the same update that makes it terminal.
Pinned by: `tests/test_upgrade_models.py::TestTerminalRowsAreImmutable::test_a_terminal_job_cannot_be_updated`

---

## 2. Gates, attempts and retries

### 2.1 [one-approval-per-gate]
**[SPEC]**
Two requests at one gate never produce two jobs. Three things guard it:
- `_queue_from_gate` refuses while any job of the run is queued or running;
- it leaves the gate with a conditional update
  (`WHERE state='awaiting_approval' AND awaiting_phase=:gate`);
- `UNIQUE(run_id, phase, attempt)` is the backstop.

Pinned by: `tests/test_upgrade_routes.py::TestGates::test_a_second_approval_of_the_same_gate_is_refused`, `tests/test_upgrade_routes.py::TestRetry::test_leaving_the_gate_is_conditional_on_still_being_at_it`, `tests/test_upgrade_models.py::TestApprovalMutex::test_two_approvals_of_the_same_phase_collide`

**[NOTE]**
The sibling's one-job-at-a-time rule stops two executions from
overlapping, not two rows from being created. Two admins both clicking
"approve: reload" would otherwise reload the fleet twice, once after the
other. An approval and a retry name different phases, so the unique
constraint cannot see that collision; only the conditional update does.

### 2.2 [attempt-is-max-plus-one]
**[SPEC]**
A new job's `attempt` is one more than the highest so far for that run
and phase. Re-approvals, retries and a re-chained verify all follow this.
Pinned by: `tests/test_sibling.py::TestAbandonedPhaseCanBeReApproved::test_a_second_abandon_gives_attempt_three`, `tests/test_sibling.py::TestPerHostContinuation::test_a_retried_activate_is_verified_under_a_new_attempt`

### 2.3 [eligible-by-cursor]
**[SPEC]**
A phase runs on the hosts whose cursor is `STATE_BEFORE[phase]`, in
request order (`eligible_hosts`), never on "every host that has not
failed". Otherwise a re-approved activate would send a second
`install add` to a switch that has already reloaded.
Pinned by: `tests/test_sibling.py::TestPerHostContinuation::test_a_reapproved_abandoned_phase_skips_the_hosts_it_finished`

### 2.4 [retry-reset-by-sibling]
**[SPEC]**
- A retry is an approval with `is_retry` set, for a phase in
  `RETRYABLE_AT[gate]`: one that ran since the previous gate. Cleanup has
  no retry.
- When the job starts, the sibling moves the hosts that failed that phase
  back to the cursor before it (`reset_for_retry`). Flask never writes
  host state.

Pinned by: `tests/test_sibling.py::TestPerHostContinuation::test_a_retry_runs_only_on_the_failed_hosts_and_they_rejoin`, `tests/test_upgrade_routes.py::TestRetry::test_a_phase_that_did_not_run_since_the_last_gate_is_refused`

### 2.5 [partial-carries-on]
**[SPEC]**
A job ends `succeeded` (every host passed), `partial` (some did) or
`failed` (none did). The run carries on while any host remains in it, and
fails only when none does.
Pinned by: `tests/test_sibling.py::TestPerHostContinuation::test_a_partial_stage_parks_at_the_reload_gate_with_the_survivors`, `tests/test_sibling.py::TestPerHostContinuation::test_a_phase_every_host_fails_still_fails_the_run`

### 2.6 [gate-expiry]
**[SPEC]**
- A run parked at a gate expires after `DEFAULT_GATE_TTL` (7 days).
- The sibling's `expire_gates()` checks on every tick. It is a
  conditional update, so an approval that commits first wins.
- Flask also refuses an approval past `gate_expires_at`, so the limit
  holds while the sibling is down. Flask still never writes `expired`.

Pinned by: `tests/test_sibling.py::TestGateExpiry::test_an_approval_that_lands_first_is_not_overwritten`, `tests/test_upgrade_routes.py::TestApproverChecks::test_an_expired_gate_is_refused_even_before_the_sibling_sees_it`

---

## 3. Running a wave

### 3.1 [workers-no-db]
**[SPEC]**
Worker threads do device I/O only and never touch `db.session`. The main
thread copies each host into a frozen `HostTarget` first, pinned host key
included, and writes every row.
Pinned by: `tests/test_phases.py::TestParallelHosts::test_workers_never_touch_the_database`

**[NOTE]**
Workers have no app context. An ORM object read after a commit reloads
itself from the database the next time an attribute is read. The pin is
copied into `HostTarget` because activate reconnects after the reload,
from inside its worker.

### 3.2 [login-gate]
**[SPEC]**
`LoginGate` makes the first host to log in go alone.
- If its login is accepted, the other hosts log in.
- If it is refused, no other host tries, and they become `not_attempted`.
- If the host was unreachable or failed its host-key check, the next
  host tries instead.
- A `credential` failure at any later point, on any host, shuts the gate
  too (`LoginGate.refuse`).

Pinned by: `tests/test_phases.py::TestLoginGate::test_a_refused_password_is_tried_on_one_host`, `tests/test_phases.py::TestLoginGate::test_an_unreachable_first_host_does_not_hold_the_others`

**[NOTE]**
Every host gets the same password. Refusals count toward the AAA server's
lockout, which locks the account across the whole fleet.

### 3.3 [wave-stops-on-nethub-faults]
**[SPEC]**
A wave stops starting hosts on any of these:
- `credential`;
- `internal`;
- a failed activate canary;
- `store`: `check_source` hashes the image before a stage touches any
  device.

Every other `failure_stage` fails its own host only (`STOPPING_STAGES`,
`_stop_for`). That includes `hostkey`, and an address with no confirmed pin
(`UnconfirmedHost`), which fails on the main thread before any worker starts.
Pinned by: `tests/test_phases.py::TestWhatStopsAWave::test_an_unexpected_exception_is_internal_and_stops_the_wave`, `tests/test_phases.py::TestCanary::test_a_canary_that_fails_stops_the_wave`, `tests/test_phases.py::TestWhatStopsAWave::test_a_netmiko_timeout_fails_that_host_alone`, `tests/test_phases.py::TestSourceCheck::test_a_bad_store_stops_the_stage_before_any_device`

**[NOTE]**
A refused credential or a bug in NetHub would repeat on every host. A
device's own failure (`connect`, `reload`, `postcheck`, …) tells you
nothing about the others.

### 3.4 [unknown-exception-is-internal]
**[SPEC]**
`failure_stage_for` maps exceptions like this:
- Netmiko exceptions, `paramiko.SSHException`, `OSError` and `EOFError`
  → `connect`;
- a raw `paramiko.AuthenticationException` → `credential`;
- anything unrecognised → `internal`.

A broad `except` that maps to a device stage would let a NetHub bug run
on every host, looking like a device failure.
Pinned by: `tests/test_phases.py::TestFailureStageMapping::test_anything_else_is_an_error_in_our_own_code`, `tests/test_phases.py::TestFailureStageMapping::test_a_raw_paramiko_auth_failure_is_still_a_refused_credential`

### 3.5 [stopped-phase-returns-to-gate]
**[SPEC]**
When a stage, activate or cleanup stops on its first attempt, the run goes
back to the same gate with a fresh expiry, and these hosts are *kept* at
their cursor (`record_kept`):
- the hosts it never reached;
- a host whose credential was refused;
- a host that hit `internal`.

Elsewhere (precheck, verify, retries) unreached hosts are marked `failed`
and can be retried.
Pinned by: `tests/test_end_to_end.py::TestOneHostFailing::test_a_mistyped_password_at_the_reload_gate_leaves_the_run_at_the_gate`, `tests/test_sibling.py::TestStoppedPhaseReturnsToItsGate::test_a_gate_with_nobody_left_is_not_returned_to`, `tests/test_sibling.py::TestStoppedPhaseReturnsToItsGate::test_a_stopped_retry_goes_back_to_the_gate_it_came_from`

**[NOTE]**
This is what makes a mistyped password cost one refused login and a
second approval rather than the whole run. The run only goes back if some
host is still eligible there. A canary is kept on the same terms as any
other host: kept if its first login was refused or it hit `internal` before
logging in, and otherwise marked `failed`. Hosts an activate reloaded before it stopped wait
at `activated` for the verify that follows the next activate.

### 3.6 [activated-host-never-kept]
**[SPEC]**
An activate host that got past its login is marked `failed`, never kept
(`NOT_REPEATABLE`, `HostOutcome.ran`). It may already be on the new
image, and keeping it at `staged` would earn it a second `install add`.
Pinned by: `tests/test_phases.py::TestWhatStopsAWave::test_an_activate_host_past_its_login_is_never_kept`, `tests/test_phases.py::TestWhatStopsAWave::test_a_refused_login_before_activate_still_keeps_the_cursor`

### 3.7 [canary-first-in-request-order]
**[SPEC]**
- With more than one eligible host, the activate canary is the first by
  `position`. It runs alone, and `_check_canary` then runs verify's check
  on a fresh session.
- Only after that do the rest start,
  `min(job.concurrency or 1, PHASE_CONCURRENCY)` at a time.
- A lone host gets no canary check.
- Never pick the canary by hostname or from `request_document`.

Pinned by: `tests/test_phases.py::TestCanary::test_the_canary_runs_alone_and_the_rest_wait_for_its_check`, `tests/test_phases.py::TestCanary::test_one_host_is_activated_without_a_canary_check`

### 3.8 [reload-count-capped-twice]
**[SPEC]**
`upgrade_phase_jobs.concurrency` is set only on activate jobs.
`upgrades.reload_count` validates it at approval (blank = 1; outside
1..`PHASE_CONCURRENCY` is refused), and the sibling caps it again, since
the setting can be lowered before the job runs.
Pinned by: `tests/test_phases.py::TestCanary::test_the_chosen_count_is_capped_by_phase_concurrency`, `tests/test_upgrade_routes.py::TestReloadCount::test_the_default_is_one_at_a_time`, `tests/test_upgrade_routes.py::TestReloadCount::test_a_count_outside_one_to_the_cap_is_refused`

### 3.9 [verify-chains-on-activate-credential]
**[SPEC]**
- When activate succeeds, the sibling queues verify, then claims and runs
  it in the same `run_once` on activate's credential. The password is
  cleared only after verify ends.
- Verify never waits in the queue for a credential of its own, so one
  left queued by a crash fails.
- The chained verify job has no `deadline_at` and no `approved_by`. It
  still goes through `_stop_before_claim`, so a cancel that lands during
  activate ends the run `cancelled` with the reloaded hosts left at
  `activated`, unverified.

Pinned by: `tests/test_sibling.py::TestVerifyRunsOnActivatesCredential::test_one_credential_covers_activate_and_verify`, `tests/test_sibling.py::TestVerifyRunsOnActivatesCredential::test_a_verify_left_queued_by_a_crash_fails_rather_than_hangs`

### 3.10 [heartbeat-on-timer]
**[SPEC]**
While hosts are running, the main thread writes `heartbeat_at` at least
every `HEARTBEAT_INTERVAL` (30 s), and also whenever a host finishes. A long,
healthy transfer must not look stalled.
Pinned by: `tests/test_phases.py::TestParallelHosts::test_the_heartbeat_advances_while_a_host_is_still_running`

### 3.11 [scans-not-blocked]
**[SPEC]**
`tick()` runs a queued host-key scan before any phase job, and a running
phase takes one on each heartbeat (`_between_hosts`). A scan that raises
there fails only itself.
Pinned by: `tests/test_sibling.py::TestScansDuringAPhase::test_a_scan_finishes_while_a_long_host_is_still_running`, `tests/test_sibling.py::TestScansDuringAPhase::test_a_scan_that_raises_mid_phase_fails_itself_and_not_the_phase`

### 3.12 [pool-closed-before-return]
**[SPEC]**
`execute_phase` closes its thread pool before it returns. No worker may
outlive the phase or the password that `run_once` clears afterwards.
Pinned by: none

---

## 4. Cancel, deadlines and scheduling

### 4.1 [cancel-between-hosts]
**[SPEC]**
- Cancel and the deadline stop new hosts from starting, never a host
  mid-flight. They are checked only as a host is about to start.
- The job ends `cancelled` or `timed_out`, and hosts that never started
  get no row and keep their cursor.
- If a wave stop (rule 3.3) also happened, the stop wins: the job ends
  `partial`/`failed` with `not_attempted` rows, and `_park` then ends the
  run `cancelled` (rule 4.2).

Pinned by: `tests/test_phases.py::TestParallelHosts::test_cancel_stops_new_hosts_and_lets_running_ones_finish`, `tests/test_phases.py::TestExecutePhase::test_the_deadline_stops_the_run_between_hosts`

**[NOTE]**
Nowhere inside an activation is it safe to stop. Hosts already running
finish and are recorded, and each still reaches its own SCP-restore
`finally`.

### 4.2 [park-honours-cancel]
**[SPEC]**
Every place the sibling parks a run goes through `Sibling._park`, which
ends the run `cancelled` if a cancel is pending. Cancel is read only as
each host starts, so it can still be pending when the run reaches a gate.
Pinned by: `tests/test_sibling.py::TestStoppedPhaseReturnsToItsGate::test_a_cancel_after_the_last_host_started_is_not_parked_either`

### 4.3 [deadline-set-at-queue]
**[SPEC]**
- `submit` and `_queue_from_gate` set `deadline_at` from
  `upgrades.phase_deadline()`, measured from `not_before` if one is set.
  The chained verify gets none (rule 3.9).
- A job still queued at its deadline ends `expired`; a running one ends
  `timed_out` (rule 4.1).

Pinned by: `tests/test_upgrade_routes.py::TestPhaseDeadlines::test_submit_writes_a_deadline`, `tests/test_upgrade_routes.py::TestPhaseDeadlines::test_approve_writes_a_deadline`, `tests/test_upgrade_routes.py::TestScheduledApprovals::test_the_deadline_runs_from_the_window_not_the_approval`, `tests/test_sibling.py::TestQueueGuards::test_a_job_past_its_deadline_expires_instead_of_running`

### 4.4 [queue-orders-by-due]
**[SPEC]**
`next_queued()` takes the oldest job that is due, ordered by
`models.due_at()` = `coalesce(not_before, created_at)`. Two exceptions:
- a job scheduled for later neither blocks the queue nor counts as stuck
  (`worker_status` uses the same expression);
- a job whose run has a cancel pending is due at once.

Pinned by: `tests/test_sibling.py::TestScheduledJobs::test_a_scheduled_job_does_not_block_an_unscheduled_one`, `tests/test_sibling.py::TestScheduledJobs::test_a_cancel_before_the_window_finishes_the_job_unclaimed`

**[NOTE]**
Without the cancel exception, a cancel would wait for the window, up to
72 h. All that time the run would sit `running`, its artifact could not be
deleted, and no new approval would be possible.

### 4.5 [schedule-bounds]
**[SPEC]**
- `upgrades.start_time()` refuses a start time in the past, further
  ahead than `MAX_SCHEDULE_AHEAD` (72 h), or at/after `gate_expires_at`.
- Times are UTC.
- Pre-check cannot be scheduled, and a scheduled job is never
  rescheduled in place.

Pinned by: `tests/test_upgrade_routes.py::TestScheduledApprovals::test_a_start_time_outside_the_bounds_is_refused`, `tests/test_upgrade_routes.py::TestScheduledApprovals::test_a_start_time_after_the_gate_expires_is_refused` (nothing pins "pre-check cannot be scheduled")

**[NOTE]**
The 72 h cap bounds how long a sealed password can sit in the database.
Changing a start time means cancelling the run, because only Flask
creates queued rows and it never edits one. The form is a
`datetime-local`, and with no JavaScript there is nothing to report the
browser's timezone, so everything is UTC.

---

## 5. How it works

### 5.1 Phases and gates
**[SPEC]**
| phase | device impact | gate before it | credential from |
|---|---|---|---|
| precheck | read-only | none; queued at submit | submitter |
| stage | writes flash, no traffic loss | approve: copy image | approver |
| activate | reload, traffic loss | approve: reload | approver |
| verify | read-only | none; runs straight after activate | activate's approver |
| cleanup | removes inactive packages | approve: cleanup (optional; declining completes the run) | approver |

Host cursor: `pending` → `precheck_ok` → `staged` → `activated` →
`verified`, or `failed`. Cleanup leaves the cursor at `verified`.

### 5.2 Job status edges
**[SPEC]**
| from | to | actor |
|---|---|---|
| — | `queued` | Flask: submit, approve, retry |
| `queued` | `running` | sibling, conditional claim, once due |
| `queued` | `cancelled` / `expired` | sibling, before the claim (`_stop_before_claim`) |
| `running` | `succeeded` / `partial` / `failed` | sibling, when the phase ends |
| `running` | `cancelled` / `timed_out` | sibling, when a cancel or the deadline stops new hosts |
| `running` | `failed` (`internal`) | sibling, `recover_own` |
| `running` | `abandoned` | sibling `sweep()` at startup, other runner ids |

`host_key_scans` uses the same claim and sweep, with only `queued`,
`running`, `succeeded`, `failed` and `abandoned`.

### 5.3 Run state edges
**[SPEC]**
| from | to | actor |
|---|---|---|
| — | `pre_checking` | Flask, submit |
| `pre_checking` / `running` | `awaiting_approval` | sibling (`_park`): the next gate, a stopped gated phase's own gate, or an abandoned approvable phase |
| `pre_checking` / `running` | `failed` | sibling: no host left, credential would not open, `internal`, abandoned precheck/verify, `timed_out` |
| `pre_checking` / `running` | `cancelled` / `expired` | sibling |
| `awaiting_approval` | `running` | Flask: approve or retry (conditional) |
| `awaiting_approval` | `cancelled` | Flask: cancel at a gate |
| `awaiting_approval` | `completed` | Flask: decline cleanup |
| `awaiting_approval` | `expired` | sibling, `expire_gates()` |
| `running` | `completed` | sibling, after cleanup |

### 5.4 The loop
**[SPEC]**
`main()` waits for the schema (schema.md), runs `sweep()` once, and then
calls `Sibling.tick()` repeatedly, sleeping 5 s whenever a tick found
nothing to do. Each tick:
1. `expire_gates()`.
2. Runs one queued scan, if there is one.
3. Otherwise takes the next queued job:
   - if it is cancelled or past its deadline, finishes it;
   - otherwise claims it, opens the credential, runs `execute_phase`
     then `_advance_run`, and chains into verify after activate.

### 5.5 Deadline budgets
**[SPEC]**
- `upgrades.PHASE_BUDGET_SECONDS` gives each phase a fixed part plus a
  part per host, and the total is multiplied by `DEADLINE_SAFETY` (2).
- Hosts counted: every host in the run for an approval, only the failed
  hosts for a retry. Stage adds 1.3 s per MB of the largest image, per
  host.
- The deadline bounds the whole wave. A single stuck transfer is bounded
  by `TRANSFER_READ_TIMEOUT` instead (device-layer.md).

| phase | fixed s | per host s |
|---|---|---|
| precheck | 300 | 120 |
| stage | 600 | 300 (+ the per-MB term) |
| activate | 600 | 1200 |
| verify | 300 | 180 |
| cleanup | 300 | 120 |

**[NOTE]**
The budgets assume hosts run one at a time. That is exact for an activate
at the default reload count, and generous for everything else, on purpose.
Dividing by `PHASE_CONCURRENCY` would tie a deadline written at approval
to a sibling setting that can change before the job runs. The numbers
come from single-device timings; re-derive them once a real multi-host
wave has been measured.

---

## 6. Decisions

**[NOTE]**
- **The sibling runs one job at a time, with hosts in parallel inside it
  (`PHASE_CONCURRENCY`, default 4).**
  - Why: concurrent jobs buy little at this scale and invite device work
    from two runs to interleave, while a one-at-a-time stage of 20
    switches took about two hours.
  - Reopen if: runs regularly queue behind each other for long.
- **Gates are rows, not terminal prompts.**
  - Why: the sibling has no stdin, and a row names who released each
    phase and whose credential it used. This is the audit property.
- **A failed host does not fail the run; failed hosts can be retried.**
  - Why: one bad switch should not discard 39 good ones.
- **A wave stops only on NetHub's own faults and a failed canary**
  (rules 3.3, 3.7).
  - Reopen if: some kind of device failure turns out to predict failure
    on the others.
- **Activate uses a canary, then a reload count the approver chooses
  (default 1).**
  - Why: NetHub warns rather than refuses, because it cannot see which
    devices back each other up. The approve form's four warnings
    (`partials/reload_count.html`) are part of this decision.
  - Measured: one activate takes about 14 min, so 20 switches four at a
    time take about 84 min, against 4.7 h one at a time.
- **A scheduled upgrade is an approval with a start time, not a run that
  holds one credential throughout.**
  - Why: credentials stay per phase and sealed for hours rather than
    days, and a person reviews each phase before approving the next.
  - Reopen: see future.md (non-interactive run).
- **Abandoned work needs a fresh approval.**
  - Why: the approval supplies the credential and names the person. An
    automatic retry would credit a machine's decision to whoever last
    clicked.
- **Cancel is a column the sibling polls, not a signal.**
  - Why: the job row is the only channel between the processes.
- **The sweep lives in the sibling, and Flask only shows "stalled".**
  - Why: a sweep in Flask could fire against a healthy run.
  - Cost: a sibling that dies and stays dead is swept by nobody.
    frontend.md shows the warning instead.
- **`cancelled`, `expired` and `abandoned` stay separate statuses.**
  - Why: a person stopping a run, a time limit passing and a crash need
    different follow-ups.

---

## 7. Pitfalls

**[BUG] Comparing a stored timestamp raises `TypeError`**
- Symptom: `can't compare offset-naive and offset-aware datetimes`. It
  shows up in production, never in a test that skips the database round
  trip.
- Cause: SQLite returns naive datetimes for values that were written
  aware.
- Fix: compare through `phases._aware`, `sibling._aware` or
  `upgrades._aware`.

**[BUG] Tests that patch the activate runner with several hosts fail at the canary**
- Symptom: the canary check runs the real verify against a fake device.
- Cause: `_check_canary` calls `PHASE_RUNNERS['verify']`.
- Fix: patch `verify` too. A hand-built `UpgradeRunHost` also needs a
  `position`.

**[BUG] Raising from `on_tick` fails the running phase**
- Symptom: a broken scan marks the whole phase `internal`.
- Cause: the exception reaches `tick()`, which calls `recover_own()`.
- Fix: anything run from `on_tick` catches its own errors, as
  `_between_hosts` does.

---

## 8. Known gaps

**[SPEC]**
- `sweep()` records an abandoned job as `failure_stage='connect'`, though
  nothing says the device was at fault.
- Host state `skipped` is in the vocabulary, but nothing writes it.
- `upgrade_phase_jobs.log_path` exists, but nothing writes it.
- `phase_activate` captures the running config before the reload, but
  the capture is discarded: `record()` never stores
  `HostOutcome.config_backup`, so `upgrade_run_hosts.config_backup_path`
  is never set.
- Hostnames in a request (`upgrades.parse_hosts`) are de-duplicated and
  checked non-blank, but no character set is enforced. That would matter
  if config backups were ever written to a path built from the hostname.
- Weak pins. These rules' tests touch the rule without proving it:
  - 1.1: no test asserts Flask never writes a job status.
  - 1.3: only checks that the id parses as a UUID.
  - 2.2: both tests would also pass under `1 + count(abandoned)`.
  - 3.2: the host-key branch is untested.
  - 4.5: "pre-check cannot be scheduled" is untested.
