# Testing
Code: `tests/conftest.py`, `tests/test_*.py`, `tests/captures/`, `tests/fixtures/schemas/`, `pyproject.toml` (`[tool.pytest.ini_options]`) · Tests: this file is about them

---

## AI READING INSTRUCTION

Read `[SPEC]` and `[BUG]` blocks for authoritative facts.
Read `[NOTE]` only if additional context is needed.
`[?]` blocks are unverified; treat them with lower confidence.
§1–§2 are rules: each H3 heading is a rule's slug. §3 says how it works
(the file map and fixtures), §4 pitfalls.

---

## 1. Fixtures and environment

### 1.1 [conftest-env-before-import]
**[SPEC]**
- `tests/conftest.py` sets `SECRET_KEY`, `DATABASE_PATH`, `ARTIFACT_STORE`
  and `NETHUB_CREDENTIAL_PUBLIC_KEY` at import, before `nethub` is
  imported, because `config.py` reads them at import time.
- It generates one sealing key pair per pytest process and hands out the
  private half as the `credential_private_key` fixture.

Pinned by: none

### 1.2 [app-fixture-runs-real-migrations]
**[SPEC]**
The `app` fixture calls the real `create_app()`, so every test's database
is built by the migrations, not by `create_all()`. It sets
`WTF_CSRF_ENABLED=False` and gives each test a fresh `ARTIFACT_STORE`.
Teardown goes through `drop_database`, which also drops `alembic_version`.
Pinned by: `tests/test_schema.py::test_create_app_leaves_the_shared_database_at_head`

### 1.3 [csrf-checked-with-its-own-app]
**[SPEC]**
The shared `app` fixture turns CSRF off, so `tests/test_templates.py`
builds its own `csrf_app` with it on. That is the only check that would
notice a CSRF token deleted from a form. Use the `drop_database` fixture;
never `import tests.conftest`.
Pinned by: `tests/test_templates.py::test_every_post_form_carries_a_csrf_token`

### 1.4 [conftest-is-shared-ground]
**[SPEC]**
`conftest.py` holds `app`, `client`, `make_user` (an admin by default),
`logged_in_client`, `make_artifact` (real bytes on disk),
`credential_private_key` and `drop_database`. Keep fixtures that only one
file uses in that file. `test_templates.py` and `test_upgrade_models.py`
each define their own `make_run`, with different signatures.
Pinned by: none

### 1.5 [pythonpath-dot]
**[SPEC]**
`pyproject.toml` sets `pythonpath = ["."]`, which is why a bare `pytest`
can `import nethub`.
Pinned by: none

---

## 2. Evidence and the end-to-end test

### 2.1 Captures (rule: device-layer.md [captures-are-evidence])
**[SPEC]**
`tests/captures/<device>-<release>/` is verbatim output from real
hardware: `show version`, `dir`, `show privilege`, the device's SSH host
key and a README giving its `ssh-keygen -lf` fingerprint. Never edit one
to make a test pass. Add a directory per release.
Pinned by: `tests/test_facts.py::TestRealCapture::test_parse_version`, `tests/test_connection.py::test_fingerprint_matches_what_ssh_keygen_prints`

### 2.2 Schema fixtures (rule: schema.md [schema-fixtures-are-evidence])
**[SPEC]**
`tests/fixtures/schemas/{513aaec,a4fcc12,c76688d}.sql` are real
`create_all()` output from historical commits, used to test adoption of
pre-migration databases. Never edit them.
Pinned by: `tests/test_schema.py::TestAdoptingAPreMigrationDatabase::test_it_is_repaired_to_exactly_the_current_schema`

### 2.3 [end-to-end-fakes-only-the-switch]
**[SPEC]**
`tests/test_end_to_end.py` runs whole runs through the UI, using Flask's
test client and the real `Sibling` built on `_database_app()`. Credentials
are really sealed and opened, and the real device layer runs. `FakeSwitch`,
which answers from `tests/captures/`, replaces only the device:
- the Sibling's `connect=` constructor argument;
- `connection.scan_host_key`;
- `transfer._scp_put`.

Pinned by: `tests/test_end_to_end.py::TestCleanRun::test_every_phase_runs_and_the_run_completes`

**[NOTE]**
Only the `credential_channel` fixture knows how a credential gets from
Flask to the sibling. A change to that mechanism changes that fixture,
not the scenarios.

### 2.4 [check-source-stubbed]
**[SPEC]**
`tests/test_phases.py` and `tests/test_sibling.py` replace
`phases.check_source` with an autouse stub, because their digests are
made up. `TestSourceCheck` tests the real one, and the end-to-end test
stages real ingested bytes through it.
Pinned by: `tests/test_phases.py::TestSourceCheck::test_a_bad_store_stops_the_stage_before_any_device`

### 2.5 [templates-tested-by-rendering]
**[SPEC]**
`tests/test_templates.py` renders every page and is the only guard on
the templates: broken `url_for`, a renamed variable, a missing token, a
script or inline style, flash categories. `tests/test_frontend.py` covers
the server half of the confirmation boxes, the security headers and
`worker_status`.
Pinned by: `tests/test_templates.py::test_every_page_renders`, `tests/test_templates.py::test_no_page_leaks_an_undefined_variable`, `tests/test_templates.py::test_an_uncategorised_flash_still_reads_as_an_error`

### 2.6 [file-local-fixtures]
**[SPEC]**
Before adding a test, read the fixtures at the top of its file. Several
change the environment for every test in it:
- `test_upgrade_routes.py` and `test_roles.py` override `app` to set
  `DEVICE_TARGET_CIDRS=['192.0.2.0/24']`; submit refuses everything
  without it.
- `test_sibling.py` has autouse `no_real_connections` and `one_clock`
  (pins `upgrades._utcnow`).
- `make_user` sets no `device_username`, so tests that submit or approve
  set one by hand.

Pinned by: none

### 2.7 [new-admin-route-in-admin-only]
**[SPEC]**
Every new admin-only route must be added to `ADMIN_ONLY` in
`tests/test_roles.py`. That list is how operators are proven to get a 403.
Pinned by: `tests/test_roles.py::TestRoles::test_an_operator_is_refused_every_admin_only_route`

---

## 3. How it works

**[SPEC]**
| file | covers |
|---|---|
| `test_sibling.py`, `test_phases.py` | dispatch.md |
| `test_upgrade_routes.py`, `test_upgrade_models.py` | gates, submit, host-key routes, constraints |
| `test_sealed_credentials.py` | credentials.md |
| `test_connection.py`, `test_transfer.py`, `test_install.py`, `test_facts.py`, `test_upgrade_cli.py` | device-layer.md |
| `test_artifacts.py`, `test_errors.py` | artifacts.md |
| `test_auth.py`, `test_user_management.py`, `test_roles.py`, `test_bootstrap.py`, `test_config.py`, `test_models.py`, `test_credentials.py` | auth-and-roles.md, deployment.md |
| `test_schema.py` | schema.md |
| `test_templates.py`, `test_frontend.py` | frontend.md |
| `test_end_to_end.py` | whole runs |
| `test_docs.py` | the docs' cited tests and slugs exist |

```
pytest                     # everything
pytest tests/test_schema.py -q
```

---

## 4. Pitfalls

**[BUG] Hand-built run hosts fail to insert**
- Symptom: `IntegrityError` on `upgrade_run_hosts.position`.
- Cause: `position` is NOT NULL, with `UNIQUE(run_id, position)`.
- Fix: give each `UpgradeRunHost` built in a test a distinct `position`.

**[BUG] A multi-host activate test hits the real verify**
- Symptom: the canary check fails or hangs against a fake.
- Cause: `_check_canary` calls `PHASE_RUNNERS['verify']`.
- Fix: patch `verify` alongside `activate`.

**[BUG] A test passes vacuously on foreign keys**
- Symptom: a foreign-key test passes even though nothing is enforced.
- Cause: a connection made outside SQLAlchemy's `Engine` event, so it
  lacks `foreign_keys=ON`.
- Fix: go through `db`, or set the pragmas yourself
  (schema.md [pragmas-on-every-connection]).

**[BUG] Renaming a helper in one test file breaks another**
- Symptom: an `ImportError` in a different test file.
- Cause: some tests import helpers from others (`test_end_to_end.py`,
  `test_install.py`, `test_sibling.py`). This works only through
  `pythonpath = ["."]`, since there is no `tests/__init__.py`.
- Fix: grep `from tests.` before renaming.

**[BUG] A user built in a test can't reach admin pages**
- Symptom: a 403 in a test that is not about roles.
- Cause: `User(...)` defaults to `operator`, unlike `make_user`.
- Fix: use `make_user`, or pass `role='admin'`.
