# Credentials
Code: `nethub/sealed_credentials.py`, `nethub/upgrades.py` (`_seal_into`, `_check_credential`, `_require_device_username`), `nethub/sibling.py` (`_claim`, `run_once`, `_supplier`), `nethub/devices/phases.py` (`PhaseContext`, `_summarise`), `nethub/config.py` (`DEBUG`) · Tests: `tests/test_sealed_credentials.py`, `tests/test_upgrade_routes.py`, `tests/test_sibling.py`, `tests/test_phases.py`, `tests/test_end_to_end.py`

---

## AI READING INSTRUCTION

Read `[SPEC]` and `[BUG]` blocks for authoritative facts.
Read `[NOTE]` only if additional context is needed.
`[?]` blocks are unverified; treat them with lower confidence.
§1–§3 are rules: each H3 heading is a rule's slug. §4 explains how it
works, §5 decisions, §6 pitfalls, §7 known gaps.

This file covers the *device* credential (a person's AAA password for the
switches). Login passwords for NetHub itself are in auth-and-roles.md.

---

## 1. Sealing and opening

### 1.1 [flask-seals-never-opens]
**[SPEC]**
- Flask holds only the sibling's public key (`NETHUB_CREDENTIAL_PUBLIC_KEY`)
  and seals credentials with PyNaCl `SealedBox`.
- The private key exists only in the sibling unit, as the systemd
  credential `credential_private_key` or a read-only file named by
  `NETHUB_CREDENTIAL_KEY_FILE`, and never in an environment variable.

Pinned by: `tests/test_sealed_credentials.py::TestKeys::test_the_systemd_credential_wins_over_the_file`, `tests/test_sealed_credentials.py::TestKeys::test_private_key_from_a_file`, `tests/test_upgrade_routes.py::TestSealedAtSubmitAndApprove::test_the_password_is_not_in_the_row_in_the_clear`

**[NOTE]**
A copy of the database alone (a backup, a stolen file, a WAL fragment)
yields no password. Recovering one needs the database *and* the
sibling's private key. An environment variable is readable in
`/proc/<pid>/environ` and is inherited by every child process.

### 1.2 [sealed-in-the-creating-transaction]
**[SPEC]**
`upgrades._seal_into` writes `upgrade_phase_jobs.sealed_credential` in the
same transaction that creates the `queued` row, from `submit`
(pre-check) and `_queue_from_gate` (approve, retry). No committed queued
job ever lacks its credential, so the sibling can never claim one early.
Pinned by: `tests/test_upgrade_routes.py::TestSealedInTheSameTransaction::test_an_approved_phase_is_never_claimable_without_its_credential`, `tests/test_upgrade_routes.py::TestSealedInTheSameTransaction::test_a_submitted_precheck_is_never_claimable_without_its_credential`

### 1.3 [checked-before-written]
**[SPEC]**
`_check_credential` runs the device username and password through
`check_credential` (printable ASCII 0x20–0x7E, 1–128 chars) before any row
is written, so a refusal is a plain message with nothing to undo. `seal()`
and `open_sealed()` apply the same allowlist again.
Pinned by: `tests/test_upgrade_routes.py::TestSealedInTheSameTransaction::test_a_refused_password_leaves_no_rows`, `tests/test_upgrade_routes.py::TestSealedInTheSameTransaction::test_a_device_username_the_sibling_would_refuse_is_refused_first`

### 1.4 [sealed-payload-binds-the-execution]
**[SPEC]**
- The sealed JSON holds exactly `job_id`, `approved_by` (the supplier),
  `username`, `password` and `expires_at`.
- `expires_at` is the job's `deadline_at`, so a credential is valid
  exactly as long as its job and has no separate TTL.

Pinned by: `tests/test_upgrade_routes.py::TestSealedAtSubmitAndApprove::test_a_gated_phase_is_sealed_for_its_approver`, `tests/test_upgrade_routes.py::TestSealedAtSubmitAndApprove::test_it_expires_with_the_jobs_deadline`, `tests/test_sealed_credentials.py::TestOpenRefuses::test_a_malformed_payload`

### 1.5 [supplier-is-approver-or-submitter]
**[SPEC]**
`Sibling._supplier` expects the job's `approved_by`. If that is null, as it
always is for pre-check, which has no gate, it expects the run's
`submitted_by`. Do not tighten this to require a non-null approver, or
every pre-check fails.
Pinned by: `tests/test_upgrade_routes.py::TestSealedAtSubmitAndApprove::test_precheck_is_sealed_for_the_submitter`, `tests/test_sibling.py::TestSealedCredential::test_the_phase_gets_the_credential_that_was_sealed`

### 1.6 [claim-clears-ciphertext]
**[SPEC]**
`Sibling._claim` first reads `sealed_credential`, then runs
`UPDATE … WHERE status='queued' AND sealed_credential = <what was read>`,
which sets the column to NULL. One changed row means both "the job is
ours" and "these are the bytes we cleared".
Pinned by: `tests/test_sibling.py::TestSealedCredential::test_the_claim_takes_the_ciphertext_off_the_row`, `tests/test_sibling.py::TestSealedCredential::test_a_second_claim_gets_nothing`

**[NOTE]**
SQLite's `RETURNING` gives the *new* row, which would be NULL, so the
read-then-conditional-update is the only way to get both answers from
one statement.

### 1.7 [ciphertext-only-while-queued]
**[SPEC]**
- CHECK `ck_sealed_credential_only_while_queued`
  (`status = 'queued' OR sealed_credential IS NULL`) makes the database
  refuse ciphertext on any row that is not queued.
- Every path that ends a job unclaimed clears the column in the same
  update: `_finish` for cancelled/expired, and `request_cancel` on queued
  jobs.

Pinned by: `tests/test_sibling.py::TestSealedCredential::test_the_table_refuses_ciphertext_on_a_job_that_is_not_queued`, `tests/test_upgrade_routes.py::TestCancelDropsTheCredential::test_cancelling_clears_a_queued_jobs_ciphertext`, `tests/test_sibling.py::TestSealedCredential::test_a_cancelled_job_drops_its_ciphertext_without_a_failure_stage`

### 1.8 [open-sealed-distrusts-input]
**[SPEC]**
`open_sealed` treats what it decrypts as hostile, and checks in this
order:
1. Nothing sealed.
2. Size cap (`MAX_SEALED_BYTES` 4096), before decrypting.
3. Decrypt.
4. JSON.
5. The exact field set.
6. `job_id`: `type(...) is int`, then it must equal the job's id.
7. `approved_by`: the same two checks, against the supplier.
8. Expiry.
9. The allowlist on username and password.

Pinned by: `tests/test_sealed_credentials.py::TestOpenRefuses::test_an_oversize_blob_is_not_even_opened`, `tests/test_sealed_credentials.py::TestOpenRefuses::test_a_bool_is_not_job_1`, `tests/test_sealed_credentials.py::TestOpenRefuses::test_another_job`, `tests/test_sealed_credentials.py::TestOpenRefuses::test_another_approver`, `tests/test_sealed_credentials.py::TestOpenRefuses::test_expired`, `tests/test_sealed_credentials.py::TestOpenRefuses::test_a_credential_outside_the_allowlist_even_if_sealed`

**[NOTE]**
A compromised Flask chooses these bytes, and the sibling is the side with
privileges. `isinstance` would accept JSON `true` as job 1, because
`bool` is a subclass of `int`.

### 1.9 [unusable-credential-fails-closed]
**[SPEC]**
A credential that is missing, will not open or fails a check fails its
job with `failure_stage='credential'` and a fixed `CredentialError`
message, and fails the **run** (`_fail_run`), with no return to the gate.
Nothing falls back to a credential from another row, phase or attempt. A job that reaches its deadline unclaimed ends `expired` with
`failure_stage='credential'` ("its credential was discarded unused").
Pinned by: `tests/test_sibling.py::TestSealedCredential::test_a_credential_that_does_not_open_or_check_fails_the_phase`, `tests/test_sibling.py::TestSealedCredential::test_a_credential_sealed_for_another_job_is_refused`, `tests/test_sibling.py::TestSealedCredential::test_a_job_that_expires_unclaimed_says_its_credential_was_discarded`

**[NOTE]**
This is different from a password the *device* refuses. That is a wave
stop: the run goes back to its gate (dispatch.md
[stopped-phase-returns-to-gate]). A credential that cannot be opened at
all means the keys or the row are wrong, and approving again would not
fix it.

### 1.10 [keys-checked-at-startup]
**[SPEC]**
- `create_app()` refuses to start without a valid public key, and it
  checks this before it migrates anything.
- The sibling refuses to start with no private key, or with one that does
  not match `NETHUB_CREDENTIAL_PUBLIC_KEY` (`check_pair`).

Pinned by: `tests/test_sealed_credentials.py::TestWebStartup::test_it_refuses_to_start_without_a_public_key`, `tests/test_sealed_credentials.py::TestWebStartup::test_it_refuses_a_malformed_public_key`, `tests/test_sibling.py::TestNoSecretKey::test_the_sibling_refuses_a_private_key_that_does_not_match`, `tests/test_sibling.py::TestNoSecretKey::test_the_sibling_refuses_to_start_with_no_private_key`

**[NOTE]**
Without these checks, every job would fail one at a time with a
credential error that says nothing about keys.

---

## 2. Where the plaintext may exist

### 2.1 [plaintext-lifetime]
**[SPEC]**
The plaintext password exists in only two places:
- in Flask, for the one request that seals it;
- in the sibling, as `PhaseContext.device_password`, for one phase
  execution, plus the verify chained onto an activate.

`run_once` overwrites it in a `finally:` block. It is never written to a
row, a log, the session or a file.
Pinned by: `tests/test_end_to_end.py::TestCleanRun::test_the_password_reaches_no_row`, `tests/test_phases.py::TestExecutePhase::test_the_credential_reaches_no_row`, `tests/test_upgrade_routes.py::TestThroughTheClient::test_the_password_is_never_written_to_a_row`

### 2.2 [repr-false]
**[SPEC]**
Every field holding the plaintext is declared `field(repr=False)`
(`sealed_credentials.Credential.password`,
`phases.PhaseContext.device_password`). One `log.debug("%r", ctx)` would
otherwise write the password to journald.
Pinned by: `tests/test_sealed_credentials.py::TestSealAndOpen::test_the_password_is_not_in_the_ciphertext_or_the_repr`

### 2.3 [error-summary-reads-summary-attr]
**[SPEC]**
`phases._summarise` copies only an exception's `summary` attribute, and
reduces anything without one to `unexpected <TypeName>`. Every
exception NetHub's device layer raises sets `summary` in `__init__`. A
wrapper that puts a foreign exception's text into `message` must pass a
`summary=` that leaves it out.
Pinned by: `tests/test_phases.py::TestErrorSummaryNeverLeaksTheCredential::test_a_foreign_exception_contributes_only_its_type`, `tests/test_transfer.py::TestPushBracket::test_push_failure_summary_omits_the_wrapped_exceptions_text`, `tests/test_connection.py::test_connect_generic_failure_summary_omits_the_wrapped_exceptions_text`

**[NOTE]**
`error_summary` is kept for a year. A stray `str(exc)` reaching it is a
long-lived credential leak that shows no other symptom, because library
and device text can echo what was sent. The same rule covers scans
(`run_scan_once`) and `CredentialError`, whose messages are fixed text.

### 2.4 [debug-off]
**[SPEC]**
`DEBUG` is off unless the environment sets `DEBUG=1` (`config.py`). Do
not make it default on or easier to enable: Werkzeug's debugger renders
frame locals, submitted passwords included, into an HTTP response.
Pinned by: none

### 2.5 [env-password-warns]
**[SPEC]**
`upgrade_cli.py` and `scripts/check_device_facts.py` accept
`NETHUB_DEVICE_PASSWORD` for scripted use. Both print a warning when they
do, and otherwise prompt for the password.
Pinned by: none

---

## 3. The device side

### 3.1 [privilege-15-at-login]
**[SPEC]**
NetHub requires privilege level 15 at login and has no escalation path:
`connection.connect()` passes no `secret`, and nothing calls Netmiko's
`.enable()`. Pre-check refuses an account below 15 with
`failure_stage='privilege'` before anything changes.
Pinned by: `tests/test_end_to_end.py::TestPrecheckFailure::test_an_under_privileged_account_fails_the_run_before_any_change`, `tests/test_install.py::TestGuards::test_under_privileged_account_is_refused`

**[NOTE]**
Every upgrade command (`write memory`, `copy` to flash,
`install add … activate commit`) needs level 15 anyway. Asking for it at
login means there is no second secret to collect, and no shared enable
secret to protect. It also means that whether someone may upgrade is
decided by the device, not by NetHub's screens.

### 3.2 [device-username-is-server-side]
**[SPEC]**
- The username sent to a device is the supplier's own
  `users.device_username`, read server-side. No request field can supply
  it.
- Each user sets their own on `/profile` (`POST /profile/device-username`).
- Submit, approve and retry refuse a user who has none.

Pinned by: `tests/test_upgrade_routes.py::TestSubmit::test_the_device_username_is_snapshotted_not_submitted`, `tests/test_upgrade_routes.py::TestSubmit::test_a_user_without_a_device_username_cannot_submit`, `tests/test_upgrade_routes.py::TestApproverChecks::test_an_approver_without_a_device_username_is_refused`, `tests/test_upgrade_routes.py::TestRetry::test_a_retrier_without_a_device_username_is_refused`

### 3.3 [device-username-recorded-per-job]
**[SPEC]**
`upgrade_phase_jobs.device_username_used` records the supplier's device
username for each job, and the chained verify copies activate's.
`upgrade_runs.device_username_used` is the submitter's. The job column
is the one that matches the device's AAA log.
Pinned by: `tests/test_roles.py::TestRunRule::test_each_job_records_the_device_username_its_supplier_used`, `tests/test_sibling.py::TestVerifyRunsOnActivatesCredential::test_verify_records_activates_device_username`

---

## 4. How it works

### 4.1 Key setup
**[SPEC]**
```
python -m nethub.sealed_credentials keygen --out <file>      # private key, mode 0600, never overwritten
python -m nethub.sealed_credentials public-key --key <file>  # recover the public half
```
- Set `NETHUB_CREDENTIAL_PUBLIC_KEY` in both units.
- Give the private key file to the sibling unit only.

### 4.2 When a password is asked for
**[SPEC]**
| action | seals for | phase(s) it covers |
|---|---|---|
| submit | submitter | precheck |
| approve stage | approver | stage |
| approve activate | approver | activate and the verify chained onto it |
| approve cleanup | approver | cleanup |
| retry | retrier | the retried phase (and verify after a retried activate) |

That is up to four password entries for a clean run. A scheduled
approval adds no entry; it only delays when that one is used.

### 4.3 How long ciphertext can sit in the database
**[SPEC]**
- Unscheduled: until the sibling's next claim, which is normally seconds
  away.
- Scheduled: up to `MAX_SCHEDULE_AHEAD` (72 h) plus the phase's own
  deadline budget (dispatch.md §5.5).
- A run parked at a gate holds no credential at all, because the gate
  has not collected one yet.

---

## 5. Decisions

**[NOTE]**
- **Credentials travel sealed in the job row**, not over a second channel
  between the processes.
  - Why: the job row is already the only channel. An approval survives a
    web restart and a long queue, and Flask cannot decrypt what it
    stores.
  - Reopen if: Flask and the sibling stop sharing a database.
- **A credential belongs to one phase execution, not to a session or a
  run.**
  - Why: a run can wait at a gate for days. Holding a password that long,
    sealed or not, is what this avoids.
  - Cost: up to four password entries per run.
- **A sealed box proves nothing about who sealed it.** Anyone who can
  write the database and knows the public key (which is public) can plant
  a credential.
  - Binding the job id and the supplier stops a blob being copied onto
    another row; it does not stop a forgery. A forger has to supply the
    password, so learns nothing.
  - A compromised Flask still sees passwords as they are submitted.
- **No enable secret, ever.** See rule 3.1.
- **No Ansible Vault or similar file encryption.** The credential is
  never in a file. Vault would add a second secret, delivered the same
  way, to protect the least exposed copy.
- **Two-sided attribution needs central AAA with command accounting.**
  - With it, the same person appears in NetHub's job row and in the
    device's own AAA log, recorded by systems that share no trust.
  - With per-human accounts configured on each device, both records
    trace back to NetHub's own deployment, so attribution is weaker.

---

## 6. Pitfalls

**[BUG] A credential error that says nothing about keys**
- Symptom: every job fails `failure_stage='credential'`, one after
  another.
- Cause: the sibling's private key does not match the public key Flask
  seals to.
- Fix: rule 1.10 now refuses to start the sibling. If the error is seen
  anyway, regenerate the pair and set both units from the same `keygen`
  output.

**[BUG] Pre-check fails at `credential` for every run**
- Symptom: every submitted run fails before pre-check reaches a device.
- Cause: a supplier check that requires `approved_by` to be non-null.
  Pre-check has no approver.
- Fix: keep `Sibling._supplier`'s fallback to `submitted_by` (rule 1.5).

---

## 7. Known gaps

**[SPEC]**
- The process does not set `PR_SET_DUMPABLE` to 0, so same-uid `ptrace`
  and `/proc/<pid>/mem` are not denied. `LimitCORE=0` in both Quadlet
  units stops core dumps there, but neither protection covers a bare
  `flask run` or `upgrade_cli.py`.
- A Python `str` cannot be wiped. Overwriting `ctx.device_password`
  drops a reference; the bytes, and the form parser's copies, stay in
  the heap until reused. The heap can be swapped out unless swap is
  disabled on the host.
- The approve and new-run password fields use `autocomplete="off"`.
  Nothing stops a reverse proxy from logging POST bodies; that is the
  deployment's responsibility.
- `users.device_username` has no uniqueness constraint. Two NetHub users
  mapped to one device account would weaken attribution without any
  warning.
- The credential is tested on one host before the rest (dispatch.md
  [login-gate]). That catches a wrong password. It does not catch a
  password that AAA authorises on some device groups and not others.
- Weak or missing pins:
  - 2.4 and 2.5 have no tests.
  - 1.1: nothing shows Flask cannot open what it seals, or that the
    private key never comes from an environment variable.
  - 2.1: tests check rows only, not the `finally:` overwrite, logs or the
    session.
  - 2.2: `PhaseContext`'s repr is not tested.
  - 3.2: nothing shows a submitted form field is ignored.
