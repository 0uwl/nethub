# NetHub improvement plan

Written 2026-09-24 from a full code review of `main` at commit `7027aa0`.
The maintainer asked for this plan and it is the agreed direction for the
project. It is a temporary, task-scoped document: delete it in the last
workstream's branch once every workstream below is merged.

**Precedence.** Where this plan and `CLAUDE.md` disagree, this plan is the
newer decision. Several `CLAUDE.md` "hard rules" are deliberately changed by
specific workstreams below; each workstream says which rule it changes and
must update `CLAUDE.md` in the same branch. Do not refuse a workstream's
change because an older rule says otherwise.

Line numbers below are as of `7027aa0` and will drift. Search by file and
function name.

## What we want to achieve

1. **Correct behaviour first.** Fix the confirmed bugs before adding anything.
2. **Less surface.** Delete features that cannot run (pull transport, shared
   account mode) and machinery that exists only to work around other
   machinery (the credential socket and its gunicorn workaround).
3. **Architecture that matches what exists.** Today's constraints are sized
   for a day-0 phone-home route that is not built. Day-2 should be designed
   as day-2. Day-0, when it comes, is a separate service.
4. **Usable at the scale we claim.** One unreachable switch must not fail a
   whole run, and staging 20 switches must not take most of a day.
5. **Operable.** An admin can disable a user, change a password and see
   when the sibling is not running.
6. **Short, true documentation.** `CLAUDE.md` and the design doc currently
   describe code that no longer exists. Shrink them and make every claim
   checkable.

## How to work this plan

- One workstream per branch. Branch off a freshly fetched `main` using the
  branch name given. Work one workstream at a time. If your session was
  assigned a different branch name, ask the maintainer which to use rather
  than picking one silently.
- Do not trust the status table for what has merged. Each branch sets its
  own row to "in review", and `main` keeps saying that after the merge until
  a later branch changes it. Check `git log --oneline origin/main` for the
  merge of each branch your workstream depends on. When starting a
  workstream, set any row that `main` shows as merged to "merged".
- Before starting, read "Found while working" at the bottom of this file for
  items tagged with your workstream. They are part of its scope.
- Before and after every change, run the baseline:
  `python -m pytest -q`, `ruff check .` (ruff **0.16.7**, the CI pin; check
  `ruff --version` first), and `python -m yamllint .`. The test count must
  not go down.
- In the same branch as the code change: update this file's status table,
  and update `CLAUDE.md` and `design-document.md` wherever the change makes
  them wrong (the `design-doc-sync` skill lists what to check).
- Commit messages explain why. Push the branch; the maintainer opens and
  merges the PR. Do not merge yourself.
- Stay inside the workstream. If you find something outside it, add it to
  "Found while working" at the bottom of this file instead of fixing it.

## Status

| WS | Branch | Summary | Depends on | Status |
|---|---|---|---|---|
| 0 | `docs/claude-md-corrections` | Remove false claims from `CLAUDE.md` | none | merged |
| 1 | `fix/dispatch-correctness` | Credential race, approver check, stranded rows, gate expiry | none | merged |
| 2 | `fix/storage-correctness` | SQLite pragmas, artifact delete guard, `check_store` off the request path | none | merged |
| 3 | `fix/deployment-units` | `:Z` on shared volumes, sibling needing `SECRET_KEY` | none | merged |
| 4 | `test/end-to-end` | Automated Flask + sibling + fake device test | none | merged |
| 5 | `chore/remove-unbuilt` | Delete pull transport and shared account mode | none | merged |
| 6 | `chore/migrations` | Flask-Migrate with a baseline migration | 5 | merged |
| 7 | `feat/sealed-credentials` | Replace the credential socket with sealed credentials in the job row | 4, 6 | merged |
| 8 | `feat/per-host-continuation` | Partial phases continue; retry failed hosts | 4, 6 | merged |
| 9 | `feat/parallel-phases` | Bounded parallelism, real heartbeat, scans not blocked | 8 | merged |
| 10 | `feat/user-management` | Disable users, change passwords, revoke sessions | 6 | in review |
| 11 | `feat/frontend-cleanup` | Drop 2014 JS/CSS, security headers, auto-refresh, stalled and queue indicators | 9 | todo |
| 12 | `ci/hardening` | SHA-pinned actions, hashed lockfile, container smoke test | 3 | todo |
| 13 | `docs/slim-down` | Shrink `CLAUDE.md` and the design doc, strip history from comments, delete this file | all others | todo |
| 14 | `feat/scheduled-approvals` | Approve a gate now, run it at a set time | 8, 9, 15 | todo |
| 15 | `feat/canary-activation` | Canary host, then parallel reloads; stop only on NetHub's own faults | 9 | todo |
| 16 | `feat/roles` | Admin and operator roles; optional two-person rules for artifacts, host keys and runs | 10 | todo |

Workstreams 1, 2, 3 and 5 are independent and can go in any order. Do 4
before 7, 8 and 9: those three rewrite the dispatch path and need the
end-to-end test as a safety net. WS-14 and WS-15 were added after the plan
was written. Do WS-15 before WS-14, which relies on its stop-and-return-to-
the-gate rule; both still come before WS-13, which stays last. WS-16 was
added later still and comes after WS-10, whose user pages and audit table it
builds on.

## Decisions this plan makes

The review left these open. This plan picks one answer for each. If the
maintainer disagrees, change it here before the workstream starts, not
halfway through a branch.

1. **Credentials travel sealed in the job row** (WS-7), using PyNaCl's
   `SealedBox`. The Unix socket, `entrypoint.sh` and the `.socket` unit go.
2. **Gate expiry is implemented, not deleted** (WS-1). A run parked for days
   has stale pre-check data and should close itself.
3. **Pull transport and shared account mode are deleted** (WS-5). Both are
   recorded in design doc §10 as possible future work.
4. **`check_store` becomes a CLI command** (WS-2), not a web button.
5. **A phase can end `partial`** (WS-8). The run continues with the hosts
   that succeeded, and an operator can retry the failed ones.
6. **Pre-check, stage, verify and cleanup run in parallel** (WS-9), default
   concurrency 4. WS-9 keeps activate serial; **WS-15 replaces that**: activate
   upgrades one canary host alone, and once it is verified, the rest run as
   many at a time as the approver chooses (default 1), under a clear warning.
   NetHub does not know which devices back each other up and does not refuse
   on the user's behalf. Decided by the maintainer on 2026-09-25.
7. **The frontend has no JavaScript** (WS-11).
8. **`ADMIN_USERNAME` has no default** (WS-10), so the first admin is not
   always called `admin`.
9. **Schema changes go through Flask-Migrate** (WS-6) from then on, and
   **migrations run automatically**: the web process applies them at startup,
   so upgrading is "back up, new image, restart". The sibling waits for them
   and never migrates. Decided by the maintainer at the start of WS-6, in
   place of the earlier "check the revision and let the admin run
   `db upgrade`".
10. **gunicorn stays at one worker for now** because of SQLite, but after
    WS-7 that is a tuning choice, not a correctness rule.
11. **Day-0 phone-home will be a separate service** (WS-13 records this in
    the design doc). Nothing in this plan builds day-0.
12. **A scheduled upgrade is an approval with a start time** (WS-14), not a
    run that holds one credential from scheduling until it finishes. The
    credential stays per phase, so it is stored sealed for hours rather than
    days, a person still reviews each phase's result before approving the
    next, and only Flask creates queued rows. A fully non-interactive run is
    recorded in design doc §10 as possible future work. Decided by the
    maintainer on 2026-09-24.
13. **A wave stops only on NetHub's own faults, and on a failed canary**
    (WS-15). A refused credential or an error in NetHub's code (`internal`)
    will repeat on every host, so the phase stops there; a device's own
    failure (`connect`, `reload`, `postcheck` and the rest) affects that
    device, so the others carry on. A stopped phase leaves the hosts it did
    not reach where they were and returns the run to the same gate, rather
    than failing them. Decided by the maintainer on 2026-09-25.

14. **Two roles, admin and operator, and optional two-person rules**
    (WS-16). Operators upload and delete artifacts, scan, confirm and delete
    host-key pins, and submit, approve, retry and cancel runs. Admins do all
    of that and also manage users and NetHub's settings. An admin can make a
    two-person rule mandatory, separately for artifacts, host keys and run
    operations; admins are exempt from those rules. For runs the rule means
    the approver of a gate or a retry is not the run's submitter; cancelling
    and declining cleanup never need a second person. Decided by the
    maintainer on 2026-09-26.

## Workstreams

### WS-0: Remove false claims from CLAUDE.md

Branch `docs/claude-md-corrections`. Small and urgent: sessions read
`CLAUDE.md` first and it currently tells them to protect machinery that was
deleted.

Fix these, and anything else found that contradicts the code:

- The Architecture section describes publish as "registry re-render, git
  commit" and says it keeps a `registry_jobs` row, `render_state`, a `flock`
  and a startup sweep (around lines 345-354, 486-492, 607, 652, 677). None of
  that exists. Publishing is `artifacts.ingest()`, synchronous, in the
  request.
- The SCP restore is described as an `always:` block (around line 365). It is
  a Python `finally:` in `transfer._push_scp`.
- It says the `device_host_keys` table and the confirmation screen are not
  built (around line 581). Both exist (`models.DeviceHostKey`,
  `upgrade_routes.confirm_hostkey`).
- `network_cli` and "crashed EE process" wording in the architecture and
  hard-rules sections. Replace with Netmiko session and sibling process.
- It says Flask renders "stalled" from `heartbeat_at`. It does not (WS-11
  builds it). Say so, or drop the claim.

Do not restructure the file here; WS-13 does that. Also add nothing new
beyond one line pointing at this plan if it is missing.

**Done when:** every statement in `CLAUDE.md` about publish, the SCP bracket
and host keys matches the code.

### WS-1: Dispatch correctness

Branch `fix/dispatch-correctness`.

1. **Queued job committed before the credential is held.** `upgrades.submit()`
   (commit near line 292) and `upgrades.approve()` commit the `queued` job;
   `upgrade_routes.new_run` and `upgrade_routes.approve` then call `_hold()`.
   The sibling polls every 5s and can claim the job in between, failing it
   with "no credential held". If `_hold()` fails, the route deletes a row the
   sibling may already own. Fix: flush to get the job id, hold the
   credential, then commit; if the commit fails, discard the held
   credential. WS-7 later removes this class of race entirely, but this fix
   is cheap and should not wait.
2. **Approver without a device username.** `upgrades.approve()` does not check
   `user.device_username` (only `submit()` does, line 244). `_hold()` then
   stores `None` and the sibling rejects it as "malformed credential". Refuse
   the approval with the same message `submit()` uses.
3. **Rows stranded `running`.** In `sibling.main()`, an unexpected exception
   raised after `claim()` is logged and rolled back, but the job stays
   `running` under this runner's own id, and `sweep()` only reclaims rows from
   other runner ids. The row is stuck until the sibling restarts. Fix: in the
   exception handler, mark the claimed job `failed` with
   `failure_stage='connect'` and a fixed summary, and fail its run.
4. **Gate expiry is never enforced.** `gate_expires_at` is written in several
   places and never read. Add a check to the sibling's loop that moves runs at
   `awaiting_approval` past `gate_expires_at` to `expired` (the §7.3 table
   already names this edge). Keep the 7-day default (`DEFAULT_GATE_TTL`).

**Tests:** one per item. For item 1, force the sibling to poll between commit
and hold (inject a hook) and show the job still gets its credential.

**Done when:** all four have tests, and the design doc §7.3 run-state table
matches what the code does.

### WS-2: Storage correctness

Branch `fix/storage-correctness`.

1. **SQLite pragmas.** Design doc §5 requires `journal_mode=WAL`,
   `busy_timeout` and `foreign_keys=ON`. Only `foreign_keys` is set
   (`extensions._enable_sqlite_foreign_keys`). Two processes write one
   SQLite file, so add WAL and a `busy_timeout` (5000 ms) in the same
   connect hook. Add a test asserting both, like the existing foreign-key
   test.
2. **Artifact deletion ignores runs that need it.** `artifact_routes.delete_artifact`
   removes the row and bytes even when a non-terminal run's
   `upgrade_run_hosts` rows snapshot that artifact. The snapshot protects the
   metadata, not the bytes. Refuse deletion while any run in state
   `pre_checking`, `awaiting_approval` or `running` references the
   artifact's id.
3. **`check_store` runs in a request.** `artifact_routes.check_artifacts`
   hashes every image inside a request handler, which is the long-work-in-Flask
   pattern the architecture forbids. Move it to `flask --app nethub check-store`
   (print issues, exit non-zero if any). Replace the button with a short note
   naming the command.

**Done when:** each item has a test and the artifacts page no longer offers
an in-request store check.

### WS-3: Deployment units

Branch `fix/deployment-units`.

1. **SELinux label conflict.** `quadlet/nethub.container` and
   `quadlet/nethub-sibling.container` both mount
   `%h/.local/share/nethub/data` and `.../artifacts` with `:Z`. `:Z` is a
   private label, so on an SELinux-enforcing host the second container to
   start relabels the directory and locks the first out of the database.
   Change both units to `:z` for these two volumes (the socket volume already
   uses `:z` for this reason). Verify on an enforcing host if one is
   available and record the result in the unit's comment; if not, say it is
   unverified.
2. **The sibling needs `SECRET_KEY`.** `config.py` validates `SECRET_KEY` at
   import, and the sibling imports it for the database settings. The sibling
   serves no HTTP and should not hold the key that can forge admin sessions.
   Split configuration: shared settings (database, artifact store, transport,
   CIDRs) in one module, web-only settings (`SECRET_KEY`, cookies, upload
   size) in another that only `create_app()` loads. Remove `SECRET_KEY` from
   `quadlet/nethub-sibling.container`.

**Done when:** `python -m nethub.sibling` starts without `SECRET_KEY`, both
units use `:z` for shared volumes, and `CLAUDE.md`'s container section says
so.

### WS-4: End-to-end test

Branch `test/end-to-end`. No behaviour change; this is the safety net for
WS-7, WS-8 and WS-9.

Write a test that drives the real path: log in, confirm a host key, upload an
artifact, submit a run through the Flask test client, then run the sibling's
`run_once()` in the same test against a fake device (a fake `connect`
injected through `PhaseContext`, returning canned output from
`tests/captures/`), approve each gate, and assert the final run and host
states. Cover: a clean run to `completed`, a pre-check failure, a cancel at a
gate, and a credential that expired before the sibling picked it up.

Use the real credential store and a real socket pair where the current
design needs them, so WS-7 can swap the transport and keep the test.

**Done when:** the test runs in CI in under ~10s and fails if any phase
transition breaks.

### WS-5: Remove features that cannot run

Branch `chore/remove-unbuilt`.

1. **Pull transport.** `IMAGE_TRANSPORT=pull_sftp` fails every stage because
   `sibling.main()` never sets `Sibling.pull_target` and there is no
   configuration for a distribution host or credential. It has also never
   been run against hardware. Delete `transfer._pull_sftp`, `PullTarget`, the
   `pull_target` plumbing in `Sibling` and `PhaseContext`, the transport
   dispatch in `stage_image`, `IMAGE_TRANSPORT`, the `TRANSPORTS` vocabulary
   and `upgrade_runs.image_transport_used`, `distribution_host_used`, and
   their tests. `scp_restore_confirmed` stays; its null now only means "no
   bracket ran". Rewrite design doc §4.3.1 around push only and move the pull
   design to a short §10 entry ("possible future transport; needs a
   distribution host, a credential source, and a hardware test of the prompt
   sequence").
2. **Shared account mode.** `SHARED_ACCOUNT_MODE` is hardcoded `False`, has no
   username setting, and is still stored on every run and shown in
   `upgrade_detail.html` and `upgrades_list.html`. Delete the config value,
   the `upgrade_runs.shared_account_mode` column, the template text and the
   `CLAUDE.md` hard rule about it. Record it in §10 as possible future work.

Schema changes here need a fresh database. Nothing is deployed, so that is
fine; say so in the commit message.

**Hard rules changed:** "Day-2 transfer runs in whichever direction
`image_transport` says", "Transport is deployment-level", "Push is the
default" and "No shared service account ... except one explicit opt-in" all
collapse into "push over SCP is the only transport; every run uses the
submitter's own device username".

**Done when:** `grep -rn "pull_sftp\|shared_account" nethub tests` returns
nothing and the design doc no longer describes either as current.

### WS-6: Migrations

Branch `chore/migrations`. Do this after WS-5 so the baseline migration does
not include columns WS-5 deletes.

Add Flask-Migrate (pin it in `requirements.txt`). Generate a baseline
migration from the current models, including the terminal-status trigger on
`upgrade_phase_jobs` (it is DDL attached to `after_create` today; a migration
must create it explicitly). Replace `db.create_all()` in `create_app()` with
an automatic upgrade to the latest revision (decision 9): the web process
migrates, the sibling waits, a database newer than the code is refused, a
failed migration rolls back whole. Adopt databases created before migrations
by repairing the drift `create_all()` left behind, and refuse anything else.
Add the `internal` failure stage (from "Found while working") as the first
real migration. Document the upgrade procedure in the README and `CLAUDE.md`.

**Done when:** a fresh database built by the migrations matches `create_all()`,
an existing database is migrated or adopted on start with its rows intact,
and `CLAUDE.md` no longer says "migrate by hand or recreate".

### WS-7: Sealed credentials instead of the socket

Branch `feat/sealed-credentials`.

**Why.** The socket costs `credential_socket.py` (378 lines) and its tests,
`entrypoint.sh`, `quadlet/nethub-credential.socket`, a workaround for gunicorn
taking over inherited sockets, the one-worker rule, and a 30-minute TTL
(`credential_socket.DEFAULT_TTL`). It also causes failures: a Flask restart
drops every held credential, and because execution is serial, approving a
phase behind a long-running one guarantees a `credential` failure. The design
rejected an encrypted column because "the key would be readable by Flask".
That is true of a shared key, not of public-key encryption: Flask only needs
the sibling's public key.

**Design.**
- Use PyNaCl `SealedBox` (already installed through paramiko; pin it
  explicitly in `requirements.txt`). Do not hand-roll the crypto.
- The sibling owns a private key, loaded from a systemd credential or a
  file only the sibling unit can read. Flask gets the public key from
  configuration. Add a command to generate the pair.
- On submit and approve, Flask seals JSON `{job_id, approved_by, username,
  password, expires_at}` and writes it to a new nullable
  `upgrade_phase_jobs.sealed_credential` column in the same transaction that
  creates the `queued` row. This removes WS-1's race by construction.
- On claim, the sibling reads and clears the column in the same transaction,
  opens it, and checks `job_id` and `approved_by` against the row (this
  replaces `verify_running`) and `expires_at`. Any mismatch fails the phase
  with `failure_stage='credential'`.
- Keep `verify` running on `activate`'s credential. It has no approval, so
  nothing is sealed for it: `sibling.run_once` runs it straight after
  `activate` with the plaintext still in memory (branch
  `fix/verification-credential`). Sealing only on submit and approve is
  therefore enough; do not add a sealed column for `verify`.
- Clear the column on every path to a terminal state and on cancel. The
  terminal-status trigger forbids updates after a terminal state, so clear
  before or in the same update.
- The column is added by a migration (WS-6), so existing deployments get it
  on their first start of the new image.
- A queued job that reaches its deadline unclaimed ends `expired` with
  `failure_stage='credential'` and a fixed summary saying its credential was
  discarded unused, so the run page says why (maintainer decision). The WS-4
  expired-credential scenario becomes this; the 30-minute TTL it tested is
  gone.
- `expires_at` defaults to the job's `deadline_at`, so a credential waits as
  long as the job may wait.
- Keep `check_credential`'s allowlist and `field(repr=False)` on everything
  holding the plaintext.

**Delete:** `nethub/credential_socket.py`, `tests/test_credential_socket.py`,
`entrypoint.sh` and the Containerfile `ENTRYPOINT`,
`quadlet/nethub-credential.socket`, the `Sockets=`/`Requires=` lines and socket
volume in both units, `NETHUB_CREDENTIAL_SOCKET`, `_serve_credential_socket`
in `nethub/__init__.py`, and `_hold`/`discard` use in `upgrade_routes`.

**Threat model to write into the design doc §9.1.** At rest: ciphertext only,
until claimed or until the deadline. Who can decrypt: the sibling's key only.
A compromised Flask still sees passwords as they are submitted, exactly as
today. A copy of the database alone reveals nothing.

**Hard rules changed:** the "sibling-initiated Unix socket" rule, the
single-worker rule's credential rationale, and "the device credential never
reaches disk" (it now reaches disk as ciphertext for a bounded time; say
exactly that).

**Tests:** seal and open round trip; tampered ciphertext; wrong job id; wrong
approver; expired; column cleared after claim, cancel and every terminal
state. The WS-4 end-to-end test must pass unchanged apart from setup.

**Done when:** no socket code remains and a Flask restart between approval
and claim no longer fails the phase.

### WS-8: Per-host continuation and retry

Branch `feat/per-host-continuation`.

**Why.** `sibling._advance_run` fails the whole run if any host failed
(line 344). One unreachable switch out of 50 blocks the other 49, and there is
no way to retry a failed phase. `phases.execute_phase` (line 361) already
skips failed hosts in later phases, as if continuing were intended; the two
halves disagree.

**Design.**
- Add job status `partial` (some hosts succeeded, some failed) to
  `JOB_STATUSES` and the terminal set. `succeeded` means all hosts, `failed`
  means none.
- `_advance_run` treats `partial` like `succeeded`: the run moves on with the
  hosts that passed. It fails only on `failed`.
- Add `POST /upgrades/<id>/retry` with a phase. Allowed when the run is
  parked at a gate and the named phase is the one just completed as
  `partial`. It is an approval: it collects a credential and records
  `approved_by`. It creates a new attempt of that phase that runs only on
  hosts whose `state` is `failed` and `last_phase` is that phase, resetting
  their cursor first.
- Replace `approve()`'s `attempt = 1 + count(abandoned)` with
  `1 + max(existing attempts for this phase)`, so abandoned and retried
  attempts share one rule.
- The run page shows each host's latest result and a retry button when
  allowed.

**Tests:** partial stage then activate on survivors; retry succeeds and the
host rejoins; retry of a host that fails again; activate `partial` then
verify runs only on activated hosts.

**Done when:** a run with one bad host completes for the others, and the
design doc §7.3 tables include `partial` and the retry edge.

### WS-9: Parallel phases, real heartbeat, unblocked scans

Branch `feat/parallel-phases`. After WS-8, because both change
`execute_phase`.

**Why.** Everything is serial: one sibling, one job, one host. With measured
timings, 20 switches take about 2 hours to stage and 4.7 hours to activate.
The heartbeat only updates between hosts (`phases.py:376`), so any 15-minute
host looks dead. Scans are said to jump the queue, but `run_once` blocks the
loop for a whole phase (`sibling.py:420-428`), so a scan waits for it.

**Design.**
- Run pre-check, stage, verify and cleanup through a
  `ThreadPoolExecutor` with `PHASE_CONCURRENCY` workers (default 4, deployment
  setting). Activate uses one worker, so it stays serial but goes through the
  same code.
- Worker threads do device I/O only and never touch `db.session`. Before
  submitting a host, the main thread builds a plain snapshot (address,
  filename, digest, size, flash dir, the pinned `HostKey`). Note that
  `phases.pinned_key` queries the database and must run on the main thread.
  Workers return `HostOutcome`; the main thread writes rows.
- The main thread waits on futures with a timeout (about 30s). Each tick it
  updates `heartbeat_at`, checks cancel and deadline (stops submitting new
  hosts; in-flight hosts finish), and runs any queued host-key scan.
- Revisit `upgrades.phase_deadline` for concurrency. Keep it loose.

**Tests:** concurrency is bounded; cancel stops new submissions; heartbeat
advances during a long fake host; a scan queued during a long phase finishes
before the phase does.

**Done when:** a fake 8-host stage with 4 workers takes about 2 host-durations
in a test, and the design doc no longer says phases are strictly serial.

### WS-10: User management

Branch `feat/user-management`.

**Why.** There is no way to disable a user, change a password or unlock an
account from the UI. A departing admin keeps working until their 12-hour
cookie expires, and every user is an admin who can create more admins. The
default username `admin` plus the 10-attempt lockout lets anyone on the
network keep that account locked out.

**Design.**
- Add `users.is_active`. Login refuses inactive users with the same single
  message as a wrong password. The user loader returns `None` for inactive
  users, so their next request is logged out.
- Add `users.session_epoch`, stored in the session at login. Bump it on
  password change, disable and admin reset; a cookie with an old epoch is
  rejected. This gives revocation without a `sessions` table.
- Pages: change your own password (needs the current one), admin reset of
  another user's password, disable/enable, unlock. Refuse disabling yourself
  or the last active user.
- Record each of these in a small append-only `user_admin_audit` table,
  following the `device_host_key_audit` pattern.
- `ADMIN_USERNAME` has no default. If the users table is empty and it is
  unset, log that `flask --app nethub create-admin` is needed and start
  anyway. Update the README `podman run` example.

**Done when:** a disabled user's existing cookie stops working on the next
request, with a test proving it.

### WS-11: Frontend cleanup

Branch `feat/frontend-cleanup`. After WS-9, because the stalled indicator
needs the real heartbeat.

1. **Replace the 2014 assets.** `layouts/main.html` loads jQuery 1.11.1,
   Bootstrap 3.1.1 (both with known XSS CVEs), Modernizr, respond.js and
   Font Awesome 4.1 on the page where admins type AAA passwords. Fourteen
   server-rendered forms need none of it. Replace with one small hand-written
   stylesheet and no JavaScript. Delete the vendored files under
   `nethub/static/`.
2. **Security headers** in an `after_request` hook: a CSP of
   `default-src 'self'; script-src 'none'; frame-ancestors 'none';
   form-action 'self'; base-uri 'none'`, plus `X-Content-Type-Options: nosniff`
   and `Referrer-Policy: same-origin`.
3. **Auto-refresh.** A `<meta http-equiv="refresh" content="5">` on the scan
   result page and the run page while anything is `queued` or `running`.
4. **Stalled indicator.** A `running` job whose `heartbeat_at` is older than
   three heartbeat intervals shows "stalled: check the nethub-sibling unit".
5. **Nothing is picking up work.** A `queued` job or scan older than a minute
   with no sibling heartbeat anywhere shows "No worker has picked this up. Is
   nethub-sibling running?" (today this fails silently).
6. **Queue depth at the gate.** The approve form says how many jobs are queued
   ahead.
7. **Missing device username, before the form is filled.** `upgrades.submit`
   and `upgrades.approve` refuse a user with no `device_username`, but only
   after the form is posted (the first-boot `admin` user always starts
   without one). When `current_user.device_username` is unset, the new-run
   page and the approve form on the run page show a notice linking to
   `/profile` in place of the password field and submit button. The server-side
   refusal stays: this only moves the message earlier. With no JavaScript
   (item 1), this is a template condition, not a disabled button.

**Done when:** `tests/test_templates.py` covers the new states (including a
user with and without a device username on both forms) and the page loads
with no external or inline script.

### WS-12: CI hardening

Branch `ci/hardening`.

- Pin every action in `.github/workflows/ci.yml` to a commit SHA, with the tag
  as a comment. This matters most for `immanuwell/dockerfile-roast`, a small
  third-party action in a workflow that can publish images.
- Set top-level `permissions: contents: read`; grant `packages: write` only
  to the publish job.
- Replace exact-version pins with a hashed lockfile (`pip-tools`:
  `requirements.in` plus a generated `requirements.txt` installed with
  `--require-hashes`). Remove the paragraph in `requirements.txt` that says
  this is not done.
- Add a container smoke test job: build the image, start the web container
  and the sibling against a shared volume, check `/login` returns 200 and the
  sibling logs its start line. This is the check that would have caught the
  WS-3 volume bug on an enforcing runner.

**Done when:** CI runs the smoke test on every PR.

### WS-13: Documentation slim-down

Branch `docs/slim-down`. Last, because every other workstream rewrites parts
of the docs.

- **`CLAUDE.md` to about 250 lines:** status, commands, a module map,
  invariants the code actually enforces (each naming the test that pins it),
  and deployment notes. Move arguments to the design doc or delete them.
- **`design-document.md` rewritten around decisions:** a short description of
  the system as built; one entry per decision (decision, reason,
  consequences, what would reopen it); a short target-design section for
  unbuilt parts (day-0 as a separate service, OIDC and the roles it derives
  from a group claim); open questions.
  Section numbers may change once nothing depends on them; update the `§`
  references in code comments in the same branch
  (`grep -rn "§" nethub tests`).
- **Code comments:** remove `WS-x` tags (their tracker was deleted) and "this
  used to..." history. Git history holds that. Keep a comment only where the
  current behaviour would surprise a reader.
- **Remove day-0 justifications from day-2 code**, such as "Flask holds the
  only unauthenticated route", which no longer drive any decision after WS-7.
- Delete this file as the branch's last commit and remove its pointer from
  `CLAUDE.md`.

**Done when:** `CLAUDE.md` is under 300 lines, every claim in it is true of
the code, and this file is gone.

### WS-14: Scheduled approvals

Branch `feat/scheduled-approvals`. After WS-8 and WS-9: WS-8 changes what a
finished phase means, and WS-9 changes how the sibling picks the next job.

**Why.** Upgrades run in maintenance windows. Today an approval queues its
phase immediately, so reloading a fleet at 02:00 needs someone awake at 02:00
to approve it. With the credential sealed in the job row (WS-7), an approval
can wait for a start time without anything held in memory.

**Design.**
- Add a nullable `upgrade_phase_jobs.not_before` (migration). The approve
  form gets an optional "start at". Empty means now, as today. The form
  says which time zone the time is in, and the run page shows the stored
  time in UTC. Decide the time zone at the start of the branch: a
  deployment setting, or UTC throughout.
- Only gated phases (stage, activate, cleanup) can be scheduled. Pre-check
  still runs at submit, so a wrong password or an unreachable device shows
  up when the run is created, not in the window.
- Refuse a start time in the past, later than `gate_expires_at`, or further
  ahead than a cap (`MAX_SCHEDULE_AHEAD`, default 72 hours). The cap bounds
  how long a sealed password is stored.
- `deadline_at` is `not_before` plus `upgrades.phase_deadline()`'s budget,
  so the sealed `expires_at` follows it and a job that could not start in
  its window ends `expired` with `failure_stage='credential'` (the WS-7
  path).
- The sibling never claims a job before its `not_before`, and a scheduled
  job does not block the queue: pick the next job ordered by
  `coalesce(not_before, created_at)`, skipping any not yet due. A due job
  still waits for whatever is running (an activate runs its canary first
  after WS-15); the deadline covers that wait.
- `verify` keeps running straight after `activate` on the same credential.
  Nothing new is sealed for it.
- Cancel already clears the credential of queued jobs. Changing a start
  time means cancelling the run; rescheduling without cancelling would have
  Flask change a queued job, which §7.3's actor table does not allow, so it
  is out of scope.
- The run page shows "scheduled for <time>" on a queued job, beside who
  approved it and when. `approved_at`, `not_before` and `started_at`
  together are the audit record that the phase ran on a scheduled approval
  rather than with someone at the gate.
- Requires the "stop the wave on a credential failure" item in "Found while
  working" (done in WS-8) and WS-15's return to the gate. A mistyped password
  on a scheduled approval is not found until the window: it must fail on one
  host, not lock the account out across all of them, and leave the run at
  the gate for a fresh approval rather than failing it.
- Update design doc §7.3 (the `queued` → `running` edge waits for
  `not_before`), §8.1 (an approval may carry a start time) and §9.1 (how
  long a credential can be stored). Add a §10 entry for the fully
  non-interactive run (decision 12) with what it would need: a rule for
  continuing without review after WS-8, the sibling creating queued rows,
  a run-level ciphertext rule to replace "only while queued", and an audit
  column saying no person was at the gate.

**Tests:** a scheduled job is not claimed before its time and is claimed
after; a later scheduled job does not block an earlier unscheduled one; a
start time past the cap, past `gate_expires_at` or in the past is refused;
a scheduled job that cannot start before its deadline ends `expired` with
`failure_stage='credential'` and its column cleared; cancel before the start
time clears the credential; an end-to-end run with the clock advanced to the
window completes activate and verify.

**Done when:** an admin can stage a fleet during the day, approve the reload
for a start time that night, and the end-to-end test runs it at that time
with no further input.

### WS-15: Canary activation, parallel reloads, stop only on NetHub's faults

Branch `feat/canary-activation`. After WS-9, which builds the parallel host
driver (`HostTarget`, `LoginGate`, the heartbeat tick) and pins activate to
one worker through `phases.SERIAL_PHASES`. This workstream changes that rule
(decisions 6 and 13) and must update `CLAUDE.md`'s "Hosts run in parallel
within a phase" paragraph and design doc §8.1 in the same branch.

**Why.** Activating 20 switches one at a time takes about 4.7 hours. Keeping
it serial protects a network whose redundancy NetHub cannot see, but that is
the operator's knowledge and the operator's decision; NetHub should warn and
let them choose. What a serial wave did buy is a first device that fails
before the rest reload, and since WS-8 it does not even buy that: a wave
continues past every device failure, so a bad image reloads every host in
turn. A canary gives that protection back explicitly, at the cost of one
serial activation (about 14 minutes; 20 switches four at a time then take
about 84 minutes).

**Design.**

1. **The canary.** When activate has more than one eligible host, the first
   in request order is upgraded alone: `install add`, reload, reconnect, and
   the same version check `phase_verify` makes (`install.verify_upgrade`).
   Only when it is on the target version do the rest start, at the approved
   concurrency. The later `verify` phase still checks every host, the canary
   included. A retry of activate (at the cleanup gate) uses a canary too.
   Request order is not stored today: `upgrade_run_hosts` has only
   `(run_id, hostname)` as its key. Add a `position` column, set at submit
   from the request's line order (migration); don't read it back out of
   `request_document`, since phases read rows, not documents.
2. **Reload count, chosen at approval.** The activate approve form (and the
   activate retry form) gets "Reload N devices at a time after the first",
   default 1, capped at `PHASE_CONCURRENCY`. Stored on the job as
   `upgrade_phase_jobs.concurrency` (migration), so the audit trail shows who
   chose to reload several at once; the sibling uses
   `min(job.concurrency, PHASE_CONCURRENCY)` and `SERIAL_PHASES` goes. Flask
   renders the cap, so `PHASE_CONCURRENCY` moves to `shared_config.py` and
   both Quadlet units set it. The form states, next to the field:
   - the first host listed is upgraded alone first; list a representative
     one, and split mixed hardware (stacks and standalone switches) into
     separate runs, because the canary only speaks for devices like itself;
   - NetHub does not know which devices back each other up, so reloading
     both halves of a redundant pair at once drops service;
   - cancel stops further reloads from starting, not ones in progress;
   - after the canary, a device that fails its reload does not stop the
     others.
3. **Stop only on NetHub's own faults.** `phases.failure_stage_for` falls back
   to `connect` for any exception it does not recognise, which records a bug
   in NetHub's code as a network problem. Narrow it: netmiko's and paramiko's
   exceptions and `OSError` stay `connect`, anything else is `internal`. An
   unrecognised device-side exception therefore stops the wave, which errs
   the safe way. The phase stops on `credential` or `internal` (and on a
   failed canary); every other `failure_stage` fails that host alone.
4. **A stopped phase returns to its gate instead of failing hosts.** Today
   the credential stop marks every host it did not reach `failed`
   (`not_attempted`), and when that is every host the run fails (the
   "mistyped password" item in "Found while working"). Instead, for a phase
   with a gate of its own (stage, activate, cleanup): the hosts not reached
   keep their cursor and get a `not_attempted` result row; the host whose
   credential was refused, or that hit `internal`, keeps its cursor too,
   since the fault was not the device's; a failed canary is marked `failed`,
   since that one was. The job ends `partial` if any host passed and
   `failed` otherwise, and the run returns to that phase's gate with a fresh
   `gate_expires_at`. Approving again runs a new attempt on the hosts still
   eligible (a re-approved activate picks a new canary). Pre-check has no
   gate, so a stop there keeps today's behaviour: unreached hosts are failed
   and retryable at the stage gate, or the run fails if none passed. A stop
   in `verify` does the same, retryable at the cleanup gate.
5. **Check the source image once, before the stage wave.** A missing or
   altered file in the artifact store fails every host, each only after a
   transfer of up to 15 minutes, and as `transfer` or `checksum`, which reads
   as a device fault. Before handing out any stage host, the main thread
   hashes the file under `search_dir` and compares it with the hosts'
   snapshotted `sha512` (seconds for 1.2 GB). A mismatch or a missing file
   stops the phase before any device is touched, with a new `failure_stage`
   `store` telling the operator to run `flask --app nethub check-store`
   (vocabulary change: migration, and the trigger rebuild that goes with it).

**Tests:** the canary runs alone and the rest start only after it verifies;
a canary that fails its reload, its reconnect or the version check leaves
every other host `staged` and the run at the activate gate, and approving
again uses the next host as the canary; the chosen concurrency is honoured,
capped at `PHASE_CONCURRENCY`, and recorded on the job; a device failure
after the canary does not stop the others; an unexpected exception inside a
phase is recorded as `internal` and stops the wave, while a netmiko timeout
stays `connect` and does not; a mistyped password at the activate gate
leaves every host `staged` and the run at the gate, and approving again with
the right password completes it; a missing or altered source image stops
stage before any device is contacted; an end-to-end run with three fake
switches reloads the canary first and the other two together.

**Done when:** an approver can reload a fleet several at a time after one
verified canary, with the warning on the form; a refused password or a
NetHub fault returns the run to its gate instead of failing it; and the
"mistyped password" item in "Found while working" is marked done.

### WS-16: Roles and two-person rules

Branch `feat/roles`. After WS-10: it guards WS-10's user pages, extends its
"last active user" refusal and writes to its `user_admin_audit` table.
Changes `CLAUDE.md`'s recorded alpha deviation ("everyone who can log in is
an admin — no roles") and its "Known gap" on device-username attribution,
both in the same branch.

**Why.** Every user is an admin and can create more admins. `CLAUDE.md`
records that the same person can scan a host key and confirm it, which
§4.3's separation of duty is meant to prevent, and names role-based access
control as the real fix. And each gate is approved with the approver's own
device password, while the run row records only the submitter's device
username, so the audit trail and the device's AAA log can disagree about
who logged in.

**Design.**

1. **Roles.** `users.role`, `admin` or `operator`, a CHECK constraint
   (migration). Existing users become `admin`, so nobody loses access on
   upgrade. `create-admin` and first-boot bootstrap create admins; a user an
   admin creates in the UI is an `operator` unless the admin picks
   otherwise. An admin can change a role; the change goes to
   `user_admin_audit`. Demoting or disabling the last active admin is
   refused. The user loader re-reads the row on every request, so a role
   change applies on the next click. OIDC and roles derived from its group
   claim (design doc §4.4) stay target design.
2. **Who may do what.**

   | Action | Operator | Admin |
   |---|---|---|
   | Upload, publish, delete artifacts | yes, under the artifact rule | yes |
   | Scan, confirm, delete host-key pins | yes, under the host-key rule | yes |
   | Submit a run | yes | yes |
   | Approve a gate, retry, choose a reload count (WS-15) | yes, under the run rule | yes |
   | Cancel a run, decline cleanup | yes, never needs a second person | yes |
   | Set own device username | yes | yes |
   | Users: create, disable, reset password, change role | no | yes |
   | Settings, including the two-person rules | no | yes |

   One decorator for the admin-only routes; templates hide what a user may
   not do, but the server check is what counts, and every refusal is tested.
3. **Settings.** Build the `settings` table and the append-only
   `settings_audit` §5 specifies, with three booleans for now:
   `two_person_artifacts`, `two_person_hostkeys`, `two_person_runs`, all off
   by default. An admin-only page changes them; each change is an audit row
   (old value, new value, who, when), and `BEFORE UPDATE`/`BEFORE DELETE`
   triggers make the audit table append-only, as §5 asks. Read on each
   request, never cached. The env-var settings (`DEVICE_TARGET_CIDRS` and
   the rest) stay where they are; moving them is not this workstream.
4. **The rules**, which apply to operators only and are checked when the
   second action happens, not when the first did:
   - *Artifacts.* An upload lands `staged` (the state exists and is unused
     today): bytes in the store, row written, but not nameable by a run,
     since `resolve_bundle` reads published rows only. A different user
     publishes it (new `published_by`), and the bundle-key uniqueness check
     runs at that moment. The uploader may withdraw their own staged upload
     alone. Deleting a published artifact is a request (`delete_requested_by`,
     `delete_requested_at`) that a different user confirms; the existing
     refusal while a live run references the row still applies.
   - *Host keys.* Confirming requires a confirmer other than the person who
     requested the scan. This inverts WS-6.3's binding, which today requires
     the confirmer to *be* the scanner; the scan's one-shot `consumed_at`
     and 15-minute freshness window stay. Deleting a pin is a request a
     different user confirms. `device_host_key_audit` records both people.
   - *Runs.* Every gate approval and every retry must come from someone
     other than `upgrade_runs.submitted_by`, including a scheduled approval
     (WS-14). Cancel and decline cleanup are exempt: stopping is the
     conservative action.
   - *Admins* are exempt, and their acting alone is visible: record the
     actor's role at the time of the action (on the job row for approvals
     and retries, in the audit rows for artifacts and host keys), since a
     role can change later.
5. **Device username per phase.** Add `upgrade_phase_jobs.device_username_used`,
   the supplier's device username snapshotted when the job is created
   (submit, approve, retry), and show it on the run page.
   `upgrade_runs.device_username_used` stays the submitter's. This closes
   `CLAUDE.md`'s "Known gap", which the run rule would otherwise make the
   normal case.
6. **Docs.** `CLAUDE.md`: drop the "no roles" deviation, close the known
   gap, and update the host-key paragraph on separation of duty. Design doc:
   §4.4 (local roles built, OIDC-derived roles still target), §5 (the new
   columns and the settings tables), §7.3 (the `staged` → `published` edge
   and who writes it). README "Using it": roles and the two-person rules.

**Tests:** existing users become admins on upgrade; an operator is refused
every admin-only route; the last active admin cannot be demoted or disabled;
each setting change writes an audit row, and the audit table refuses UPDATE
and DELETE; with each rule off, an operator completes the action alone; with
it on, the same operator cannot publish their own upload, confirm their own
scan, delete a pin or an artifact alone, or approve or retry their own run,
and a second operator can; an admin can do each alone and the record shows
an admin acted; an uploader can withdraw their own staged upload under the
rule; cancel and decline cleanup work alone under the run rule; a run whose
gates are approved by different people records each one's device username
on its job; the rule is read when the second action happens, so switching
it off releases a waiting item.

**Done when:** operators can do the day-to-day work and nothing else, admins
can turn each two-person rule on and off with an audited change, and
`CLAUDE.md` no longer lists "no roles" or the device-username gap.

## Maintainer actions (no branch)

- **Host key across upgrades.** `install.wait_for_device` deliberately retries
  on `HostKeyError` because it is unknown whether an IOS-XE upgrade
  regenerates the host key. Every reconnect goes through `connection.connect`,
  which requires the pin. If the 2026-09-09 round trips reconnected through
  it, the key already survived two upgrades. Check the notes from that run.
  If confirmed, treat `HostKeyError` as non-transient in `wait_for_device`
  (a small follow-up branch) and close the §10 entry.
- **SELinux verification** for WS-3, if an enforcing host is available.

## Found while working

Add items here that are outside the current workstream. Each needs a file,
a one-line description and the workstream it was found in.

- `nethub/sibling.py` `recover_own()` recorded an unexpected error in our own
  code as `failure_stage='connect'`. Found in WS-1; done in WS-6, which added
  `internal` in migration 0002.
- `nethub/sibling.py` `sweep()` still records a phase abandoned by a dead
  sibling as `failure_stage='connect'`, though nothing says the device was at
  fault. Consider `internal` or a new value; a new value needs a migration.
  Found in WS-6.
- **Every database created before WS-5 could not submit a run.**
  `upgrade_runs.image_transport_used` is NOT NULL with no default, WS-5
  removed it from the models, and `create_all()` never drops a column. Found
  in WS-6 while tracing schema history, and fixed there: adoption drops the
  retired columns (`schema.RETIRED_COLUMNS`).
- `design-document.md` §4.4 (around "And `config.py` currently sets `DEBUG =
  True`") is false: `DEBUG` defaults off and is read from an env var. Fix in
  WS-13. Found in WS-3.
- **Every run failed at `verify`, right after the switch was upgraded.**
  Found in WS-4, fixed on `fix/verification-credential`: `sibling.run_once`
  now runs `verify` straight after `activate` on the same credential, as
  design doc §9.1 already said it should.
- `README.md` "Using it" said the store-drift check was a web page; it has
  been the `flask --app nethub check-store` command since WS-2. Found and
  fixed in WS-7.
- The WS-4 end-to-end test changed beyond its credential fixture in WS-7:
  its expired-credential scenario now tests a job that reaches its deadline
  unclaimed (the 30-minute TTL it tested is gone), and a web-restart
  scenario was added. Per the maintainer's decision recorded in WS-7.
- **A wrong device password is tried on every host in the wave.**
  `nethub/devices/phases.py` `execute_phase` continues to the next host after
  any per-host failure, `failure_stage='credential'` included. A mistyped
  password at an approval therefore fails a login on each of 40 switches,
  enough to trip a TACACS+/RADIUS account lockout. Stop the phase at the
  first credential failure and skip the remaining hosts, since the same
  password will fail on them too. Belongs in WS-8, which rewrites this loop
  to continue past per-host failures and must make this the exception. WS-14
  depends on it. Found in WS-7; done in WS-8: the phase stops at the first
  credential failure and the hosts it did not reach are recorded as
  `not_attempted` and failed, so a retry picks them up.
- **A mistyped password at a gate still fails the whole run.** With the
  credential stop above, the first host is refused and every other host is
  recorded failed, so no host is left to carry on and `_advance_run` fails
  the run: the operator has to submit again, pre-check included. Parking the
  run at the same gate with the phase retryable would be kinder, and matters
  more for WS-14, where the mistake is found in the window. Found in WS-8;
  belongs in WS-15 (design point 4).
- `design-document.md` still described the WS-7 socket in two places: §7.3
  ("The job row is committed only once its credential is held ... puts the
  credential in the store") and §8.1 ("§9.1 does open a second channel").
  Found and fixed in WS-8.
