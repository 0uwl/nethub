# Schema
Code: `nethub/models.py`, `nethub/schema.py`, `nethub/migrations/` (`env.py`, `versions/`), `nethub/extensions.py` (`_configure_sqlite`) · Tests: `tests/test_schema.py`, `tests/test_upgrade_models.py`, `tests/fixtures/schemas/`

---

## AI READING INSTRUCTION

Read `[SPEC]` and `[BUG]` blocks for authoritative facts.
Read `[NOTE]` only if additional context is needed.
`[?]` blocks are unverified; treat them with lower confidence.
§1–§3 are rules: each H3 heading is a rule's slug. §4 explains how it
works (the table list, writing a migration), §5 decisions, §6 pitfalls,
§7 known gaps.

What each table *means* is in the topic files: jobs and runs in
dispatch.md, artifacts in artifacts.md, pins in host-keys.md, users and
settings in auth-and-roles.md.

---

## 1. SQLite settings

### 1.1 [pragmas-on-every-connection]
**[SPEC]**
`extensions._configure_sqlite` sets three pragmas on SQLAlchemy's generic
`Engine` `connect` event, not per app, so tests get them too:
`foreign_keys=ON`, `journal_mode=WAL` and `busy_timeout=5000`.
Pinned by: `tests/test_upgrade_models.py::TestForeignKeys::test_sqlite_enforcement_is_actually_on`, `tests/test_upgrade_models.py::TestConcurrencyPragmas::test_the_database_is_in_wal_mode`, `tests/test_upgrade_models.py::TestConcurrencyPragmas::test_a_writer_waits_for_the_lock_instead_of_failing`

**[NOTE]**
- SQLite ignores `FOREIGN KEY` unless a connection asks for it. Without
  the pragma, every foreign-key test passes for no reason.
- WAL lets one process read while the other (web or sibling) writes.
- `busy_timeout` makes a second writer wait for the lock instead of
  failing with "database is locked".

### 1.2 [enums-are-check-constraints]
**[SPEC]**
`models._enum()` builds `db.Enum(..., native_enum=False,
create_constraint=True)`, so every vocabulary column is a VARCHAR with a
CHECK. SQLAlchemy has defaulted `create_constraint` to False since 1.4,
and without it the vocabularies would not be enforced at all.
Pinned by: `tests/test_upgrade_models.py::TestVocabularies::test_an_unknown_phase_is_refused`, `tests/test_upgrade_models.py::TestVocabularies::test_an_unknown_run_state_is_refused`

### 1.3 [load-bearing-constraints]
**[SPEC]**
These constraints are enforced by the database, not by convention:
- `UNIQUE(run_id, phase, attempt)` on jobs;
- `PRIMARY KEY (run_id, hostname)` and `UNIQUE(run_id, position)` on run
  hosts;
- two foreign keys on `upgrade_host_phase_results`, to the job and to the
  run host;
- the sealed-credential CHECK;
- `concurrency >= 1`;
- the two partial unique indexes on `artifacts`.

The terminal-status trigger and the append-only triggers on
`user_admin_audit`, `artifact_audit` and `settings_audit` are part of the
schema too.
Pinned by: `tests/test_upgrade_models.py::TestApprovalMutex::test_two_approvals_of_the_same_phase_collide`, `tests/test_upgrade_models.py::TestApprovalMutex::test_a_host_cannot_be_named_twice_in_one_run`, `tests/test_upgrade_models.py::TestConcurrencyPragmas::test_a_result_needs_a_phase_job_that_exists`, `tests/test_upgrade_models.py::TestConcurrencyPragmas::test_a_result_needs_a_host_in_the_run`

---

## 2. Migrations

### 2.1 [web-migrates-at-startup]
**[SPEC]**
`create_app()` checks the public key, then runs
`schema.upgrade_database()` before anything else touches the database.
There is no `db upgrade` step to forget: upgrading means backing up,
using the new image, and restarting.
Pinned by: `tests/test_schema.py::test_create_app_leaves_the_shared_database_at_head`, `tests/test_schema.py::TestUpgradeDatabase::test_an_older_database_is_migrated_with_its_rows`, `tests/test_schema.py::TestUpgradeDatabase::test_an_empty_database_is_created_at_head`

### 2.2 [sibling-never-migrates]
**[SPEC]**
The sibling's `main()` calls `schema.wait_for_current_schema()` and blocks
until the database reaches the head revision its own code knows. It
polls every 5 s and logs once a minute.
Pinned by: `tests/test_schema.py::TestTheSiblingWaits::test_it_waits_for_the_web_unit_then_returns`, `tests/test_schema.py::TestTheSiblingWaits::test_it_waits_on_an_empty_database_too`, `tests/test_schema.py::TestTheSiblingWaits::test_it_refuses_a_newer_database`

**[NOTE]**
Two processes altering one SQLite file at once is how a schema ends up
half-changed. gunicorn runs one worker, so the web process migrates once.

### 2.3 [migration-is-one-transaction]
**[SPEC]**
`migrations/env.py` runs on a private engine with pysqlite's own
transaction handling turned off (`isolation_level = None`) and issues
`BEGIN IMMEDIATE` itself. A migration that fails halfway therefore leaves
nothing applied.
Pinned by: `tests/test_schema.py::TestUpgradeDatabase::test_a_failed_migration_changes_nothing`

**[NOTE]**
By default pysqlite issues BEGIN only before DML, so each
CREATE/ALTER/DROP would commit on its own.

### 2.4 [fks-off-during-migration]
**[SPEC]**
`env.py` sets `PRAGMA foreign_keys=OFF` on the raw connection before
BEGIN, because the pragma cannot change inside a transaction. It runs
`PRAGMA foreign_key_check` before COMMIT and refuses any dangling row.
Pinned by: `tests/test_schema.py::TestAdoptingAPreMigrationDatabase::test_a_dangling_key_it_does_not_repair_is_refused_and_nothing_changes`

**[NOTE]**
SQLite changes a constraint by rebuilding the table (Alembic's batch
mode). With foreign keys on, dropping a table that others reference
breaks those references.

### 2.5 [three-refusals]
**[SPEC]**
- A database at a revision this code does not know (because a newer image
  migrated it) is refused. The migrations define `downgrade()` for tests,
  but nothing in deployment runs one; the way back is the backup.
- A database from before migrations is adopted only through the repairs
  `schema.py` lists: `RETIRED_COLUMNS`, `ADOPT_DEFAULTS` and
  `ADOPT_EXPRESSIONS`.
- Anything else is refused, with the differences listed.

Pinned by: `tests/test_schema.py::TestUpgradeDatabase::test_a_newer_database_is_refused`, `tests/test_schema.py::TestAdoptingAPreMigrationDatabase::test_an_unknown_column_is_refused_and_nothing_changes`, `tests/test_schema.py::TestAdoptingAPreMigrationDatabase::test_an_unknown_table_is_refused`, `tests/test_schema.py::TestAdoptingAPreMigrationDatabase::test_it_is_repaired_to_exactly_the_current_schema`

### 2.6 [migrations-equal-models]
**[SPEC]**
`tests/test_schema.py` builds one database from the migrations and one
from `create_all()`, and requires them to be identical: CHECK constraints,
partial indexes and the trigger included. Every other test also starts
its app through the real migrations.
Pinned by: `tests/test_schema.py::TestTheMigrationsAreTheModels::test_a_migrated_database_matches_create_all`, `tests/test_schema.py::TestTheMigrationsAreTheModels::test_the_comparison_notices_a_missing_trigger`, `tests/test_schema.py::TestTheMigrationsAreTheModels::test_head_is_the_newest_migration`

### 2.7 [migrations-are-literals]
**[SPEC]**
A migration uses literal values and never imports from `models.py`, so it
means the same thing after the models change. Name files and revisions
`NNNN_what_it_does`.
Pinned by: `tests/test_schema.py::TestTheMigrationsAreTheModels::test_the_vocabulary_in_the_model_is_the_one_migrated`

### 2.8 [env-never-configures-logging]
**[SPEC]**
`env.py` never calls `logging.config.fileConfig`, and `alembic.ini` has
no logging sections. The sibling sets the `alembic` logger to WARNING.
Inside gunicorn, `fileConfig` would replace gunicorn's handlers and
disable every existing logger.
Pinned by: none

---

## 3. Fixtures that are evidence

### 3.1 [schema-fixtures-are-evidence]
**[SPEC]**
`tests/fixtures/schemas/*.sql` hold the real `create_all()` output from
three historical commits, which adoption is tested against. Never edit
them.
Pinned by: `tests/test_schema.py::TestAdoptingAPreMigrationDatabase::test_it_is_repaired_to_exactly_the_current_schema`

### 3.2 [drop-database-drops-alembic-version]
**[SPEC]**
`conftest.drop_database` also drops `alembic_version`. If that table were
left behind, the next test's startup would treat an empty database as
already at head. Use the `drop_database` fixture; never import
`tests.conftest`, which would load it a second time and re-run its
environment setup.
Pinned by: none

---

## 4. How it works

### 4.1 Tables
**[SPEC]**
| table | holds | see |
|---|---|---|
| `user` | accounts, role, `session_epoch`, lockout counter, `device_username` | auth-and-roles.md |
| `user_admin_audit` | user management events (append-only) | auth-and-roles.md |
| `settings`, `settings_audit` | the two-person rules and their history (audit append-only) | auth-and-roles.md |
| `artifacts`, `artifact_audit` | images and their history (audit append-only) | artifacts.md |
| `device_host_keys`, `host_key_scans`, `device_host_key_audit` | pins, scans, pin history | host-keys.md |
| `upgrade_runs` | one request; `state`, `awaiting_phase`, `gate_expires_at`, cancel columns, `request_document` + `request_sha512` | dispatch.md |
| `upgrade_run_hosts` | per host: `position`, address, the snapshot (`filename`, `sha512`, `version`, `file_size`, `flash_dir`), cursor | dispatch.md |
| `upgrade_phase_jobs` | one per phase execution; status, approver, `sealed_credential`, `not_before`, `deadline_at`, `heartbeat_at`, `concurrency` | dispatch.md, credentials.md |
| `upgrade_host_phase_results` | one row per host per job attempt | dispatch.md |

- Deleting an `upgrade_runs` row cascades to its hosts and jobs through
  ORM relationships only.
- `upgrade_host_phase_results` has no cascade, so its foreign keys block
  deleting a run that has result rows. A purge must delete results first.
- `upgrade_run_hosts.artifact_id` is the only `ON DELETE SET NULL` key.
- `artifacts` is `AUTOINCREMENT`, because `artifact_audit` keys on ids;
  a rebuild of `artifacts` must keep it.

### 4.2 Commands
**[SPEC]**
```
flask --app nethub db current                    # which revision the database is at
flask --app nethub db history
flask --app nethub db migrate -m "what changed"  # draft a migration, then edit it by hand
```

### 4.3 Writing a migration
**[SPEC]**
1. Change the models.
2. Run `flask --app nethub db migrate -m "..."`.
3. Edit the draft. Autogenerate misses two things this schema relies on:
   - **A change to an allowed-value list.** SQLite cannot alter a CHECK.
     Drop the named CHECK and rebuild with
     `batch_alter_table(..., recreate='always')`; see
     `0002_internal_failure_stage.py`.
   - **The terminal-status trigger.** Any rebuild of
     `upgrade_phase_jobs` drops it, so the same migration must recreate
     it.
4. Replace every model import with literals, and rename the file to
   `NNNN_what_it_does`. `env.py` refuses offline `--sql` mode, and
   `db migrate` writes nothing when the models have not changed.
5. Run `pytest tests/test_schema.py`, which catches both omissions.

---

## 5. Decisions

**[NOTE]**
- **SQLite, written by two processes on one host.**
  - Why: it fits the scale. Its locking is unreliable on network
    filesystems, which is also why everything stays on one host.
  - Reopen if: Flask and the sibling need to run on different machines.
    That means rebuilding the failure model around another database.
- **Migrations run automatically.**
  - Why: "back up, new image, restart" is the whole upgrade procedure,
    and the sibling waits for it.
- **No downgrades in deployment.** An older image refuses a newer
  database instead of guessing what it means.

---

## 6. Pitfalls

**[BUG] A vocabulary column accepts a typo**
- Symptom: an invalid status or phase string is stored without error.
- Cause: an `Enum` built without `create_constraint=True`.
- Fix: use `models._enum()`. If in doubt, read the emitted DDL rather
  than trusting the declaration.

**[BUG] The terminal-status trigger vanishes after a migration**
- Symptom: `test_a_migrated_database_matches_create_all` fails naming
  `upgrade_phase_jobs_terminal_immutable`.
- Cause: a batch rebuild of `upgrade_phase_jobs` drops the trigger with
  the table.
- Fix: recreate the trigger in the same migration (rule 2.6).

**[BUG] Comparing a stored timestamp raises `TypeError`**
- Symptom: `can't compare offset-naive and offset-aware datetimes`.
- Cause: SQLite returns naive datetimes for values that were written
  aware.
- Fix: route the comparison through an `_aware` helper (dispatch.md §7).

---

## 7. Known gaps

**[SPEC]**
- `device_host_key_audit` has no append-only triggers (host-keys.md).
- Several columns exist that nothing writes: `log_path`,
  `config_backup_path`, `superseded_by_id`, `bytes_pruned_at`.
- `upgrade_run_hosts.flash_dir` cannot be submitted; it always takes its
  `flash:` default.
- Weak or missing pins:
  - 2.8 and 3.2 have no tests.
  - 2.7: the cited test does not check that migrations use literals.
  - 2.4: the cited test exercises `schema.adopt`'s own foreign-key
    check, not `env.py`'s.
  - 1.2: the vocabulary tests also accept a Python-side refusal.
  - 1.3: half the constraints listed are pinned only indirectly, through
    `test_a_migrated_database_matches_create_all`.
