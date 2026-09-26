"""Checks for nethub/schema.py and the migrations it runs (PLAN.md WS-6).

Upgrading NetHub is "back up, bump the image, restart", so these check what
that restart does to a database: a fresh one is created, a current one is left
alone, an older one is migrated with its rows intact, one from before
migrations is repaired or refused, a newer one is refused, and a migration
that fails leaves nothing behind. Every database here is a file under
tmp_path, never the shared test database.
"""

import shutil
import sqlite3
from pathlib import Path

import pytest
from alembic import command
from flask import Flask

from nethub import models, schema
from nethub.extensions import db

FIXTURES = Path(__file__).parent / "fixtures" / "schemas"
HEAD = "0005_user_management"


def url(path):
    return f"sqlite:///{path}"


def raw(path):
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def create_all(path):
    """A database built the way every database was before migrations."""
    app = Flask("create-all")
    app.config["SQLALCHEMY_DATABASE_URI"] = url(path)
    db.init_app(app)
    with app.app_context():
        db.create_all()
        db.engine.dispose()


def migrate_to(path, revision):
    command.upgrade(schema.alembic_config(url(path)), revision)


def version(path):
    return schema.current_revision(url(path))


@pytest.fixture
def head_schema(tmp_path):
    path = tmp_path / "head.db"
    schema.upgrade_database(url(path))
    return schema.describe(url(path))


class TestTheMigrationsAreTheModels:
    def test_a_migrated_database_matches_create_all(self, tmp_path):
        """What the tests (and every pre-migration database) got from
        create_all() is what a real deployment now gets from migrations --
        columns, keys, CHECK constraints, partial indexes and the trigger."""
        migrated, built = tmp_path / "migrated.db", tmp_path / "built.db"
        schema.upgrade_database(url(migrated))
        create_all(built)
        assert schema.differences(schema.describe(url(migrated)),
                                  schema.describe(url(built))) == []

    def test_the_comparison_notices_a_missing_trigger(self, tmp_path):
        """Otherwise the test above could pass vacuously."""
        path = tmp_path / "a.db"
        schema.upgrade_database(url(path))
        before = schema.describe(url(path))
        with raw(path) as c:
            c.execute("DROP TRIGGER upgrade_phase_jobs_terminal_immutable")
        assert schema.differences(schema.describe(url(path)), before) == [
            "trigger upgrade_phase_jobs_terminal_immutable differs or is missing"
        ]

    def test_head_is_the_newest_migration(self):
        assert schema.head_revision() == HEAD

    def test_the_vocabulary_in_the_model_is_the_one_migrated(self):
        assert "internal" in models.PHASE_FAILURE_STAGES


class TestUpgradeDatabase:
    def test_an_empty_database_is_created_at_head(self, tmp_path):
        path = tmp_path / "fresh.db"
        assert schema.upgrade_database(url(path)) == HEAD
        assert version(path) == HEAD

    def test_a_current_database_is_left_alone(self, tmp_path):
        path = tmp_path / "fresh.db"
        schema.upgrade_database(url(path))
        assert schema.upgrade_database(url(path)) is None

    def test_an_older_database_is_migrated_with_its_rows(self, tmp_path):
        path = tmp_path / "old.db"
        migrate_to(path, schema.BASELINE)
        with raw(path) as c:
            c.execute("INSERT INTO user (id, username, password_hash, failed_logins) "
                      "VALUES (1, 'alice', 'x', 0)")
            c.execute("INSERT INTO upgrade_runs (id, platform, submitted_by, "
                      "device_username_used, request_document, request_sha512, state, "
                      "created_at) VALUES (1, 'iosxe', 1, 'jsmith', '{}', 'x', 'failed', "
                      "'2026-09-01')")
            c.execute("INSERT INTO upgrade_run_hosts (run_id, hostname, ansible_host, "
                      "filename, sha512, version, file_size, flash_dir, state) VALUES "
                      "(1, 'sw01', '192.0.2.10', 'i', 'x', 'v', 1, 'flash:', 'failed')")
            c.execute("INSERT INTO upgrade_phase_jobs (id, run_id, phase, attempt, "
                      "status, failure_stage, created_at) VALUES "
                      "(1, 1, 'stage', 1, 'failed', 'connect', '2026-09-01')")
            c.execute("INSERT INTO upgrade_host_phase_results (run_id, hostname, "
                      "phase, attempt, status, failure_stage) VALUES "
                      "(1, 'sw01', 'stage', 1, 'failed', 'connect')")

        assert schema.upgrade_database(url(path)) == HEAD

        with raw(path) as c:
            assert c.execute("SELECT id, failure_stage FROM upgrade_phase_jobs"
                             ).fetchall() == [(1, "connect")]
            assert c.execute("SELECT count(*) FROM upgrade_host_phase_results"
                             ).fetchone() == (1,)
            # 0002 rebuilt this table; the terminal trigger must survive it.
            with pytest.raises(sqlite3.IntegrityError, match="terminal"):
                c.execute("UPDATE upgrade_phase_jobs SET status = 'queued' WHERE id = 1")
            assert c.execute("PRAGMA foreign_key_check").fetchall() == []

    def test_0002_accepts_internal_and_still_refuses_nonsense(self, tmp_path):
        path = tmp_path / "a.db"
        schema.upgrade_database(url(path))
        with raw(path) as c:
            c.execute("INSERT INTO user (id, username, password_hash, failed_logins) "
                      "VALUES (1, 'a', 'x', 0)")
            c.execute("INSERT INTO upgrade_runs (id, platform, submitted_by, "
                      "device_username_used, request_document, request_sha512, state, "
                      "created_at) VALUES (1, 'iosxe', 1, 'j', '{}', 'x', 'failed', 'x')")
            c.execute("INSERT INTO upgrade_phase_jobs (id, run_id, phase, attempt, status, "
                      "failure_stage, created_at) VALUES (1, 1, 'stage', 1, 'failed', "
                      "'internal', 'x')")
            with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
                c.execute("INSERT INTO upgrade_phase_jobs (id, run_id, phase, attempt, "
                          "status, failure_stage, created_at) VALUES "
                          "(2, 1, 'stage', 2, 'failed', 'nonsense', 'x')")

    def test_0002_downgrades_only_while_nothing_uses_internal(self, tmp_path):
        """Not used by NetHub itself, which never downgrades, but written, so
        checked: it works on an unused vocabulary value and otherwise rolls
        back whole instead of leaving a half-rebuilt table."""
        path = tmp_path / "a.db"
        schema.upgrade_database(url(path))
        command.downgrade(schema.alembic_config(url(path)), schema.BASELINE)
        assert version(path) == schema.BASELINE
        schema.upgrade_database(url(path))

        with raw(path) as c:
            c.execute("INSERT INTO user (id, username, password_hash, failed_logins) "
                      "VALUES (1, 'a', 'x', 0)")
            c.execute("INSERT INTO upgrade_runs (id, platform, submitted_by, "
                      "device_username_used, request_document, request_sha512, state, "
                      "created_at) VALUES (1, 'iosxe', 1, 'j', '{}', 'x', 'failed', 'x')")
            c.execute("INSERT INTO upgrade_phase_jobs (id, run_id, phase, attempt, status, "
                      "failure_stage, created_at) VALUES (1, 1, 'stage', 1, 'failed', "
                      "'internal', 'x')")
        before = schema.describe(url(path))
        with pytest.raises(Exception, match="CHECK constraint failed"):
            command.downgrade(schema.alembic_config(url(path)), schema.BASELINE)
        assert version(path) == HEAD
        assert schema.differences(schema.describe(url(path)), before) == []

    @staticmethod
    def seed_run(c):
        c.execute("INSERT INTO user (id, username, password_hash, failed_logins) "
                  "VALUES (1, 'a', 'x', 0)")
        c.execute("INSERT INTO upgrade_runs (id, platform, submitted_by, "
                  "device_username_used, request_document, request_sha512, state, "
                  "created_at) VALUES (1, 'iosxe', 1, 'j', '{}', 'x', 'running', 'x')")

    def test_0004_keeps_existing_jobs_and_marks_none_a_retry(self, tmp_path):
        path = tmp_path / "a.db"
        migrate_to(path, "0003_sealed_credential")
        with raw(path) as c:
            self.seed_run(c)
            c.execute("INSERT INTO upgrade_phase_jobs (id, run_id, phase, attempt, status, "
                      "created_at) VALUES (1, 1, 'stage', 1, 'succeeded', 'x')")
        assert schema.upgrade_database(url(path)) == HEAD
        with raw(path) as c:
            assert c.execute("SELECT id, status, is_retry FROM upgrade_phase_jobs"
                             ).fetchall() == [(1, "succeeded", 0)]

    def test_0004_accepts_partial_as_a_terminal_status(self, tmp_path):
        path = tmp_path / "a.db"
        schema.upgrade_database(url(path))
        with raw(path) as c:
            self.seed_run(c)
            c.execute("INSERT INTO upgrade_phase_jobs (id, run_id, phase, attempt, status, "
                      "created_at) VALUES (1, 1, 'stage', 1, 'partial', 'x')")
            with pytest.raises(sqlite3.IntegrityError, match="terminal"):
                c.execute("UPDATE upgrade_phase_jobs SET status = 'queued' WHERE id = 1")
            with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
                c.execute("INSERT INTO upgrade_phase_jobs (id, run_id, phase, attempt, "
                          "status, created_at) VALUES (2, 1, 'stage', 2, 'partly', 'x')")

    def test_0004_downgrades_only_while_nothing_is_partial(self, tmp_path):
        path = tmp_path / "a.db"
        schema.upgrade_database(url(path))
        command.downgrade(schema.alembic_config(url(path)), "0003_sealed_credential")
        assert version(path) == "0003_sealed_credential"
        schema.upgrade_database(url(path))
        with raw(path) as c:
            self.seed_run(c)
            c.execute("INSERT INTO upgrade_phase_jobs (id, run_id, phase, attempt, status, "
                      "created_at) VALUES (1, 1, 'stage', 1, 'partial', 'x')")
        before = schema.describe(url(path))
        with pytest.raises(Exception, match="CHECK constraint failed"):
            command.downgrade(schema.alembic_config(url(path)), "0003_sealed_credential")
        assert version(path) == HEAD
        assert schema.differences(schema.describe(url(path)), before) == []

    def test_a_newer_database_is_refused(self, tmp_path):
        """Someone went back to an older image. Writing to a schema this code
        does not understand is how rows get corrupted; restore the backup."""
        path = tmp_path / "newer.db"
        schema.upgrade_database(url(path))
        with raw(path) as c:
            c.execute("UPDATE alembic_version SET version_num = '0099_from_the_future'")
        with pytest.raises(schema.SchemaError, match="0099_from_the_future.*backup"):
            schema.upgrade_database(url(path))

    def test_a_failed_migration_changes_nothing(self, tmp_path, monkeypatch):
        """env.py runs every migration in one real transaction. pysqlite would
        otherwise autocommit each DDL statement, and a migration failing
        halfway would leave its first half applied."""
        scripts = tmp_path / "migrations"
        shutil.copytree(schema.MIGRATIONS_DIR, scripts,
                        ignore=shutil.ignore_patterns("__pycache__"))
        (scripts / "versions" / "0003_broken.py").write_text(
            "from alembic import op\n"
            "import sqlalchemy as sa\n"
            "revision = '0003_broken'\n"
            f"down_revision = '{HEAD}'\n"
            "branch_labels = depends_on = None\n"
            "def upgrade():\n"
            "    op.create_table('half_done', sa.Column('id', sa.Integer, primary_key=True))\n"
            "    op.add_column('user', sa.Column('half_done', sa.Integer))\n"
            "    raise RuntimeError('this migration fails halfway')\n"
            "def downgrade():\n"
            "    pass\n"
        )
        path = tmp_path / "a.db"
        schema.upgrade_database(url(path))
        before = schema.describe(url(path))

        monkeypatch.setattr(schema, "MIGRATIONS_DIR", scripts)
        with pytest.raises(RuntimeError, match="fails halfway"):
            schema.upgrade_database(url(path))

        assert version(path) == HEAD
        assert schema.differences(schema.describe(url(path)), before) == []


class TestAdoptingAPreMigrationDatabase:
    """create_all() ran at every start before WS-6: it added missing tables but
    never added or removed a column. These are real create_all() schemas from
    the commits named in tests/fixtures/schemas/."""

    @staticmethod
    def load(path, fixture):
        with raw(path) as c:
            c.executescript((FIXTURES / f"{fixture}.sql").read_text())

    @staticmethod
    def seed(path, *, dangling_artifact):
        """Rows the way that commit's code wrote them."""
        c = sqlite3.connect(path)  # foreign keys off, as before WS-2 enabled them
        user_columns = [r[1] for r in c.execute("PRAGMA table_info(user)")]
        if "failed_logins" in user_columns:
            c.execute("INSERT INTO user (id, username, password_hash, failed_logins) "
                      "VALUES (1, 'alice', 'x', 0)")
        else:
            c.execute("INSERT INTO user (id, username, password_hash) VALUES (1, 'alice', 'x')")
        c.execute("INSERT INTO upgrade_runs (id, platform, submitted_by, device_username_used, "
                  "image_transport_used, shared_account_mode, request_document, "
                  "request_sha512, state, created_at) VALUES (1, 'iosxe', 1, 'jsmith', "
                  "'push_scp', 0, '{}', 'x', 'completed', '2026-09-01')")
        c.execute("INSERT INTO upgrade_run_hosts (run_id, hostname, ansible_host, artifact_id, "
                  "filename, sha512, version, file_size, flash_dir, state) VALUES "
                  "(1, 'sw01', '192.0.2.10', ?, 'img.bin', 'x', '17.12.06', 1, 'flash:', "
                  "'verified')", (99 if dangling_artifact else None,))
        c.execute("INSERT INTO upgrade_phase_jobs (id, run_id, phase, attempt, status, "
                  "created_at) VALUES (1, 1, 'precheck', 1, 'succeeded', '2026-09-01')")
        c.commit()
        c.close()

    @pytest.mark.parametrize("fixture, dangling", [
        ("513aaec", True),   # before the login budget columns and the scan tables
        ("c76688d", True),   # before WS-2's artifact_id foreign key
        ("a4fcc12", False),  # before WS-5; the key exists, so no dangling id can
    ])
    def test_it_is_repaired_to_exactly_the_current_schema(
            self, tmp_path, head_schema, fixture, dangling):
        path = tmp_path / f"{fixture}.db"
        self.load(path, fixture)
        self.seed(path, dangling_artifact=dangling)

        assert schema.upgrade_database(url(path)) == HEAD

        assert version(path) == HEAD
        assert schema.differences(schema.describe(url(path)), head_schema) == []
        with raw(path) as c:
            assert c.execute("SELECT username, failed_logins, locked_until FROM user"
                             ).fetchall() == [("alice", 0, None)]
            assert c.execute("SELECT id, device_username_used, state FROM upgrade_runs"
                             ).fetchall() == [(1, "jsmith", "completed")]
            # What ON DELETE SET NULL would have done, had the key existed.
            assert c.execute("SELECT artifact_id FROM upgrade_run_hosts").fetchall() == [(None,)]
            assert c.execute("SELECT status FROM upgrade_phase_jobs").fetchall() == [
                ("succeeded",)]
            with pytest.raises(sqlite3.IntegrityError, match="terminal"):
                c.execute("UPDATE upgrade_phase_jobs SET status = 'queued'")

    def test_the_ws5_columns_are_what_blocked_every_submit(self, tmp_path):
        """image_transport_used is NOT NULL with no default, and the models no
        longer write it -- so on an unrepaired pre-WS-5 database every submit
        fails. Adoption is what fixes that."""
        path = tmp_path / "a4fcc12.db"
        self.load(path, "a4fcc12")
        self.seed(path, dangling_artifact=False)
        insert = ("INSERT INTO upgrade_runs (id, platform, submitted_by, device_username_used, "
                  "request_document, request_sha512, state, created_at) VALUES "
                  "(2, 'iosxe', 1, 'jsmith', '{}', 'x', 'pre_checking', '2026-09-24')")
        with raw(path) as c, pytest.raises(sqlite3.IntegrityError, match="NOT NULL"):
            c.execute(insert)
        schema.upgrade_database(url(path))
        with raw(path) as c:
            c.execute(insert)

    def test_a_database_matching_the_baseline_is_only_recorded(self, tmp_path):
        path = tmp_path / "a.db"
        migrate_to(path, schema.BASELINE)
        with raw(path) as c:
            c.execute("DROP TABLE alembic_version")
            c.execute("INSERT INTO user (id, username, password_hash, failed_logins) "
                      "VALUES (1, 'alice', 'x', 0)")
        assert schema.upgrade_database(url(path)) == HEAD
        with raw(path) as c:
            assert c.execute("SELECT username FROM user").fetchall() == [("alice",)]

    def test_an_unknown_column_is_refused_and_nothing_changes(self, tmp_path):
        """Only drift NetHub itself caused is repaired. A column someone added
        by hand is theirs; dropping it would destroy data NetHub knows nothing
        about."""
        path = tmp_path / "a.db"
        self.load(path, "a4fcc12")
        with raw(path) as c:
            c.execute("ALTER TABLE user ADD COLUMN favourite_colour VARCHAR(20)")
        before = schema.describe(url(path))
        with pytest.raises(schema.SchemaError, match=r"user\.favourite_colour"):
            schema.upgrade_database(url(path))
        assert version(path) is None
        assert schema.differences(schema.describe(url(path)), before) == []

    def test_an_unknown_table_is_refused(self, tmp_path):
        path = tmp_path / "a.db"
        self.load(path, "a4fcc12")
        with raw(path) as c:
            c.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY)")
        with pytest.raises(schema.SchemaError, match="notes is not part of NetHub"):
            schema.upgrade_database(url(path))

    def test_a_dangling_key_it_does_not_repair_is_refused_and_nothing_changes(self, tmp_path):
        """At a4fcc12 the artifact key existed and was enforced, so a dangling
        id means the database was changed outside NetHub. Adoption checks
        foreign keys before committing and rolls back."""
        path = tmp_path / "a.db"
        self.load(path, "a4fcc12")
        self.seed(path, dangling_artifact=True)
        before = schema.describe(url(path))
        with pytest.raises(schema.SchemaError, match="dangling"):
            schema.upgrade_database(url(path))
        assert version(path) is None
        assert schema.differences(schema.describe(url(path)), before) == []
        with raw(path) as c:
            assert c.execute("SELECT image_transport_used FROM upgrade_runs").fetchall() == [
                ("push_scp",)]


class TestTheSiblingWaits:
    """The sibling never migrates; after an upgrade it waits for the web
    unit's startup to do it."""

    def test_it_returns_at_once_when_current(self, tmp_path):
        path = tmp_path / "a.db"
        schema.upgrade_database(url(path))
        schema.wait_for_current_schema(url(path), sleep=pytest.fail)

    def test_it_waits_for_the_web_unit_then_returns(self, tmp_path, caplog):
        path = tmp_path / "a.db"
        migrate_to(path, schema.BASELINE)
        naps = []

        def sleep(seconds):
            naps.append(seconds)
            if len(naps) == 3:  # the web unit finishes its startup
                schema.upgrade_database(url(path))

        with caplog.at_level("WARNING", logger="nethub.schema"):
            schema.wait_for_current_schema(url(path), poll=2, sleep=sleep)
        assert naps == [2, 2, 2]
        assert "waiting for the web unit" in caplog.text
        assert version(path) == HEAD

    def test_it_waits_on_an_empty_database_too(self, tmp_path):
        path = tmp_path / "empty.db"
        naps = []

        def sleep(seconds):
            naps.append(seconds)
            schema.upgrade_database(url(path))

        schema.wait_for_current_schema(url(path), sleep=sleep)
        assert len(naps) == 1

    def test_it_refuses_a_newer_database(self, tmp_path):
        path = tmp_path / "a.db"
        schema.upgrade_database(url(path))
        with raw(path) as c:
            c.execute("UPDATE alembic_version SET version_num = '0099_from_the_future'")
        with pytest.raises(schema.SchemaError, match="0099_from_the_future"):
            schema.wait_for_current_schema(url(path), sleep=pytest.fail)


def test_create_app_leaves_the_shared_database_at_head(app):
    with app.app_context(), db.engine.connect() as connection:
        rows = connection.exec_driver_sql("SELECT version_num FROM alembic_version").fetchall()
    assert rows == [(HEAD,)]
