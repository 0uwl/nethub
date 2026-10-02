# Architecture
Code: `nethub/` (all of it) · Tests: see testing.md

---

## AI READING INSTRUCTION

Read `[SPEC]` and `[BUG]` blocks for authoritative facts.
Read `[NOTE]` only if additional context is needed.
`[?]` blocks are unverified; treat them with lower confidence.
§1 says what NetHub is. §2 holds the system-wide rules, each H3 heading
being a rule's slug. §3 is the module map, §4 the request flow, §5
decisions. Each topic file holds the detail.

---

## 1. What NetHub is

**[SPEC]**
- NetHub upgrades Cisco IOS-XE switches. A user uploads an image, names
  the switches and a bundle key, and approves each disruptive phase.
  NetHub pushes the image over SCP and installs it, driving each device
  with Netmiko.
- What is built: local login with admin and operator roles, user
  management, optional two-person rules, the artifact store, host-key
  pinning, and the day-2 upgrade path.
- What is not built: day-0 provisioning, OIDC, server-side sessions,
  retention, and a JSON API (future.md).
- Platform: IOS-XE only. A `platform` field runs through the schema, so a
  second platform would be new modules in `nethub/devices/`.

**[SPEC]**
| process | runs | does |
|---|---|---|
| web | gunicorn `nethub:create_app()`, 1 worker × 4 threads | pages, uploads, writes `queued` rows; migrates the database at startup |
| sibling | `python -m nethub.sibling` | claims queued jobs and scans, does all device I/O, writes every later job status and per-host cursor, and run state outside gates |

- Both processes share one SQLite file (WAL, `foreign_keys=ON`; schema.md)
  and the artifact directory, and nothing else (deployment.md).
- Neither starts without the sealing key pair: the web process needs the
  public key, and the sibling needs the private key and
  `NETHUB_SEARCH_DIR`.
- Without the sibling, queued work sits forever.
- `upgrade_cli.py` is the one other path that opens device sessions. It
  bypasses both processes and writes no rows.
- Device addresses are stored in columns named `ansible_host`, a name
  kept from the Ansible era.

---

## 2. System-wide rules

### 2.1 [no-device-io-in-flask]
**[SPEC]**
Flask never opens a device session, including for host-key scans. Work
that touches a device becomes a `queued` row, and the sibling does the
rest.
Pinned by: `tests/test_upgrade_routes.py::TestHostkeyScanDispatch::test_a_valid_address_queues_a_scan_without_blocking`

**[NOTE]**
A device phase runs for minutes. Running it in a request would hold a web
thread the whole time.

### 2.2 [the-row-is-the-only-channel]
**[SPEC]**
The job and run rows are the only control channel between the two
processes: status, heartbeat, cancel, approvals, and the device credential
(sealed). There is no socket or RPC. The image bytes are read from the
shared artifact directory. Flask writes only the `queued` job edge, plus
run state at a gate (dispatch.md [flask-writes-only-queued]).
Pinned by: `tests/test_end_to_end.py::TestWebRestart::test_a_web_restart_between_approval_and_claim_costs_nothing`, `tests/test_upgrade_routes.py::TestRetry::test_flask_leaves_the_host_cursors_to_the_sibling`

### 2.3 [phases-read-rows-not-requests]
**[SPEC]**
At submit, a run snapshots everything its phases act on onto
`upgrade_run_hosts`: filename, digest, version, size and position. After
that, phases never read `artifacts` or the request document, so a later
change cannot redirect a run that has already started.
Pinned by: `tests/test_artifacts.py::TestDelete::test_a_finished_run_keeps_its_snapshot_and_loses_the_link`

### 2.4 [request-is-a-closed-document]
**[SPEC]**
A request is a fixed set of fields: hosts (`hostname, address` per line)
and one bundle key, parsed by `upgrades.parse_hosts` and `submit`.
There is no user-supplied inventory, template, playbook, filename, digest,
username or connection setting. The table in `nethub/upgrades.py`'s
docstring is wider than what the route accepts: `submit()` takes
`platform` and `flash_dir` keyword arguments, but the route never passes
them.
Pinned by: `tests/test_upgrade_routes.py::TestSubmit::test_the_device_username_is_snapshotted_not_submitted`, `tests/test_upgrade_routes.py::TestTargetValidation::test_a_hostname_is_refused_rather_than_resolved`

**[NOTE]**
Device work is code in this repo, fixed at build time. Executing a
submitted template or playbook would hand arbitrary code the live device
credential.

### 2.5 [not-an-inventory]
**[SPEC]**
NetHub keeps no record of what each device is running now. Runs record
only what a device reported *during that run*, and those records are
never combined into a per-device view. Host-key pins, their audit trail
and scans are the only per-address records kept outside runs, and they
exist as a security control.
Pinned by: none

---

## 3. Module map

**[SPEC]**
| module | role | topic |
|---|---|---|
| `__init__.py` | `create_app()`: config, blueprints, CSP hook, key check, migrate, bootstrap, error pages, `/` | — |
| `shared_config.py` / `config.py` | settings for both processes (database, `ARTIFACT_STORE`, public key, `DEVICE_TARGET_CIDRS`, `PHASE_CONCURRENCY`) / web-only settings (`SECRET_KEY`, cookies, upload cap) | deployment.md |
| `credentials.py` | read a systemd credential by name | deployment.md |
| `extensions.py` | `db`, `login_manager`, `csrf`, `migrate`; SQLite pragmas | schema.md |
| `models.py` | every table, the vocabularies, triggers, `load_user` | schema.md |
| `schema.py`, `migrations/` | startup migration, adoption, the sibling's wait | schema.md |
| `auth.py` | login, users, own password, `create-admin` | auth-and-roles.md |
| `settings.py` | two-person rules (`applies`, `second_person_delete`), `/settings` | auth-and-roles.md |
| `web.py` | security headers, `confirmed`, `admin_required` | frontend.md |
| `bootstrap.py` | first-boot admin | auth-and-roles.md |
| `artifacts.py`, `artifact_routes.py` | ingest, publish, delete, `check-store` | artifacts.md |
| `upgrades.py` | submit, approve, retry, cancel, target and schedule checks, deadlines, sealing | dispatch.md, host-keys.md |
| `upgrade_routes.py` | `/upgrades*`, `/hostkeys*`, `/profile` | dispatch.md, host-keys.md |
| `sealed_credentials.py` | seal, open, keygen | credentials.md |
| `sibling.py` | the dispatch loop, claim, sweep, run state machine | dispatch.md |
| `worker_status.py` | stalled / no-worker / queue depth (read-only) | frontend.md |
| `devices/phases.py` | per-host driver, `LoginGate`, canary, failure mapping, rows | dispatch.md |
| `devices/{connection,transfer,install,facts}.py` | Netmiko, SCP push, install/reload, parsing | device-layer.md |
| `upgrade_cli.py` | manual escape hatch | device-layer.md |
| `gunicorn.conf.py` | 1 worker, gthread × 4, no control socket | deployment.md |

Scripts: `scripts/check_device_facts.py` (capture and replay device
output), `scripts/smoke_test.sh` (container smoke test),
`scripts/trivy_report.sh` (CI scan summary).

---

## 4. Request flow

**[SPEC]**
1. **Upload.** `POST /artifacts/new` streams, hashes and links the image,
   and records it as `published` or `staged` (artifacts.md).
2. **Pin.** `POST /hostkeys/scan` queues a scan. The sibling fetches the
   key, and a person confirms it from the scan row (host-keys.md).
3. **Submit.** `POST /upgrades/new` checks the targets, pins and bundle
   key; writes the run and host rows; and queues `precheck` with the
   submitter's sealed credential (dispatch.md).
4. **Pre-check.** The sibling claims it and checks privilege, boot mode
   and free space. The run then parks at the stage gate.
5. **Stage, activate, cleanup.** Each is approved at its gate, with the
   approver's credential and an optional start time. `verify` runs straight
   after `activate` on the same credential. Declining cleanup completes
   the run. A phase stopped by a refused password or a failed canary
   returns to its own gate, and a gate left for 7 days expires the run
   (dispatch.md).
6. **Status.** Every page reads rows. Run pages refresh while work is live
   and say when the sibling looks stalled or absent (frontend.md).

---

## 5. Decisions

**[NOTE]**
- **One Flask backend, one database, two processes.**
  - Why: the scale is small, and a separate process for device work keeps
    long device sessions out of the web tier.
  - Reopen if: the two need to run on different hosts. SQLite would then
    have to go.
- **NetHub is the only source of image bytes, and pushes them.** See
  artifacts.md and device-layer.md.
- **Device credentials are per person, per phase.** See credentials.md.
  NetHub's roles gate NetHub's own screens. What a person may do on a
  device is decided by the device's AAA.
- **A closed set of device code.** There are no plugins or hooks that run
  submitted text (rule 2.4).
