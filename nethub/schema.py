"""Keeping the database at the schema this code expects (PLAN.md WS-6).

Upgrading NetHub is meant to be "back up the database, bump the image tag,
restart". So the **web process** brings the database to the latest migration
before it serves anything (`upgrade_database`, called from `create_app()`),
and the **sibling never migrates**: it waits until the database is at the
revision its own code expects (`wait_for_current_schema`). One writer of DDL,
because two processes altering one SQLite file at once is how a schema gets
half-changed.

Three refusals, each rather than guessing:

- **A database newer than this code** (a revision the image does not know):
  someone went back to an older image. Running against a schema it does not
  understand would write rows the newer code cannot read. Restore the backup
  taken before the upgrade instead.
- **A database from before migrations** is adopted only if it can be made to
  match the baseline exactly, and only by repairs listed here
  (`RETIRED_COLUMNS`, `ADOPT_DEFAULTS`, `ADOPT_EXPRESSIONS`). Anything else --
  an unknown table or column, a required column with no known value -- is
  refused with the differences named, rather than stamped as something it
  is not.
- **A failed migration** rolls back whole (see `migrations/env.py`), so the
  database is left as it was and the web process does not start.
"""

from __future__ import annotations

import logging
import re
import tempfile
import time
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.pool import NullPool

MIGRATIONS_DIR = Path(__file__).parent / 'migrations'
BASELINE = '0001_baseline'
VERSION_TABLE = 'alembic_version'

log = logging.getLogger('nethub.schema')


class SchemaError(RuntimeError):
    """The database cannot be brought to this code's schema automatically."""


# -- what a database created before migrations may carry ---------------------
#
# `create_all()` ran at every start before WS-6. It added missing *tables*, so
# every pre-migration database has all of them, but it never added a column to
# a table that already existed and never removed one. These are the columns
# that drifted, from the history of nethub/models.py, and what adoption does
# about each. tests/fixtures/schemas/ holds real create_all() output from the
# commits involved.

#: Columns the models deliberately deleted. Their values are dropped.
RETIRED_COLUMNS = frozenset({
    # WS-5 deleted the pull transport and shared account mode. On a database
    # created before it, image_transport_used is NOT NULL with no default, so
    # every submit fails until it is gone.
    ('upgrade_runs', 'image_transport_used'),
    ('upgrade_runs', 'distribution_host_used'),
    ('upgrade_runs', 'shared_account_mode'),
})

#: SQL values for NOT NULL columns added after their table existed. A nullable
#: added column needs no entry: it is filled with NULL.
ADOPT_DEFAULTS = {
    ('user', 'failed_logins'): '0',  # the login guessing budget (5fa97ee)
}

#: Columns copied through an expression rather than as they are.
ADOPT_EXPRESSIONS = {
    # Before WS-2 there was no foreign key here, so a run could outlive the
    # artifact it named with the id left dangling. ON DELETE SET NULL is what
    # the key now does; apply it to rows that predate the key.
    ('upgrade_run_hosts', 'artifact_id'):
        'CASE WHEN artifact_id IN (SELECT id FROM artifacts) THEN artifact_id END',
}


# -- revisions ----------------------------------------------------------------

def _script() -> ScriptDirectory:
    return ScriptDirectory(str(MIGRATIONS_DIR))


def head_revision() -> str:
    return _script().get_current_head()


def known_revisions() -> set[str]:
    return {s.revision for s in _script().walk_revisions()}


def alembic_config(url: str) -> Config:
    cfg = Config()
    cfg.set_main_option('script_location', str(MIGRATIONS_DIR))
    cfg.attributes['url'] = url
    return cfg


def _engine(url: str):
    return create_engine(url, poolclass=NullPool)


def current_revision(url: str) -> str | None:
    engine = _engine(url)
    try:
        with engine.connect() as connection:
            return MigrationContext.configure(connection).get_current_revision()
    finally:
        engine.dispose()


def _refuse_if_newer(current: str | None) -> None:
    if current is not None and current not in known_revisions():
        raise SchemaError(
            f'the database is at migration {current!r}, which this version of '
            f'NetHub does not know: it was upgraded by a newer version. Run that '
            f'version, or restore the backup taken before upgrading.'
        )


# -- the web process ------------------------------------------------------------

def upgrade_database(url: str) -> str | None:
    """Bring the database at `url` to the latest migration.

    Returns the revision it migrated to, or None if it was already current.
    Raises SchemaError when it must not proceed (see the module docstring).
    """
    engine = _engine(url)
    try:
        tables = set(inspect(engine).get_table_names()) - {VERSION_TABLE}
    finally:
        engine.dispose()
    current = current_revision(url)
    _refuse_if_newer(current)

    if current is None and tables:
        adopt(url)
        current = BASELINE

    head = head_revision()
    if current == head:
        return None
    log.warning('migrating the database from %s to %s', current or 'empty', head)
    command.upgrade(alembic_config(url), 'head')
    return head


# -- the sibling -----------------------------------------------------------------

def wait_for_current_schema(url: str, *, poll: float = 5.0, log_every: float = 60.0,
                            sleep=time.sleep, clock=time.monotonic) -> None:
    """Block until the web process has brought the database to our head.

    The sibling runs from the same image as the web unit, so after an upgrade
    it only has to wait for the web unit's startup migration. It never runs
    one itself.
    """
    head = head_revision()
    logged_at = None
    while True:
        current = current_revision(url)
        if current == head:
            return
        _refuse_if_newer(current)
        if logged_at is None or clock() - logged_at >= log_every:
            log.warning('waiting for the web unit to migrate the database to %s '
                        '(it is at %s)', head, current or 'nothing yet')
            logged_at = clock()
        sleep(poll)


# -- describing a schema ----------------------------------------------------------

def _norm(sql) -> str | None:
    return None if sql is None else re.sub(r'\s+', ' ', str(sql)).strip()


def describe(url: str) -> dict:
    """Everything about a schema that matters, in a form two databases can be
    compared by. Column order and whitespace are ignored; names, types,
    nullability, keys, CHECK constraints, partial indexes and triggers are
    not."""
    engine = _engine(url)
    try:
        insp = inspect(engine)
        tables = {}
        for name in sorted(set(insp.get_table_names()) - {VERSION_TABLE}):
            tables[name] = {
                'columns': sorted(
                    (c['name'], str(c['type']), c['nullable'], _norm(c['default']))
                    for c in insp.get_columns(name)
                ),
                'primary_key': insp.get_pk_constraint(name)['constrained_columns'],
                'foreign_keys': sorted(
                    (tuple(f['constrained_columns']), f['referred_table'],
                     tuple(f['referred_columns']), f['options'].get('ondelete'))
                    for f in insp.get_foreign_keys(name)
                ),
                'unique': sorted(
                    (u['name'] or '', tuple(u['column_names']))
                    for u in insp.get_unique_constraints(name)
                ),
                'checks': sorted(
                    (c['name'] or '', _norm(c['sqltext']))
                    for c in insp.get_check_constraints(name)
                ),
                'indexes': sorted(
                    (i['name'], tuple(i['column_names']), bool(i['unique']),
                     _norm(i.get('dialect_options', {}).get('sqlite_where')))
                    for i in insp.get_indexes(name)
                ),
            }
        with engine.connect() as connection:
            triggers = {
                name: _norm(sql) for name, sql in connection.exec_driver_sql(
                    "SELECT name, sql FROM sqlite_master WHERE type = 'trigger'"
                )
            }
    finally:
        engine.dispose()
    return {'tables': tables, 'triggers': triggers}


def differences(have: dict, want: dict) -> list[str]:
    """What `have` would need to become `want`, one line per difference."""
    out = []
    for table in sorted(set(have['tables']) | set(want['tables'])):
        a, b = have['tables'].get(table), want['tables'].get(table)
        if a is None:
            out.append(f'table {table} is missing')
            continue
        if b is None:
            out.append(f'table {table} is not expected')
            continue
        for key in b:
            if a[key] != b[key]:
                out.append(f'{table}: {key} differ: have {a[key]}, want {b[key]}')
    for trigger in sorted(set(have['triggers']) | set(want['triggers'])):
        if have['triggers'].get(trigger) != want['triggers'].get(trigger):
            out.append(f'trigger {trigger} differs or is missing')
    return out


# -- adopting a database from before migrations ----------------------------------

def _baseline(tmp: str) -> tuple[dict, dict]:
    """The baseline schema, built by the migrations themselves, and the SQL
    that creates each of its objects."""
    url = f'sqlite:///{tmp}/baseline.db'
    command.upgrade(alembic_config(url), BASELINE)
    engine = _engine(url)
    try:
        with engine.connect() as connection:
            rows = connection.exec_driver_sql(
                'SELECT type, name, tbl_name, sql FROM sqlite_master '
                "WHERE sql IS NOT NULL AND name != 'alembic_version'"
            ).fetchall()
    finally:
        engine.dispose()
    sql = {}
    for kind, name, table, text in rows:
        sql.setdefault(table, []).append((kind, name, text))
    return describe(url), sql


def _column_names(described_table: dict) -> list[str]:
    return [c[0] for c in described_table['columns']]


def _plan_adoption(have: dict, want: dict) -> tuple[list[str], list[str], list[str]]:
    """Which tables to create and rebuild, and what cannot be repaired."""
    create, rebuild, problems = [], [], []
    for table in sorted(set(have['tables']) - set(want['tables'])):
        problems.append(f'table {table} is not part of NetHub')
    for table, target in want['tables'].items():
        current = have['tables'].get(table)
        if current is None:
            create.append(table)
            continue
        if current == target:
            continue
        rebuild.append(table)
        present = set(_column_names(current))
        for column in sorted(present - set(_column_names(target))):
            if (table, column) not in RETIRED_COLUMNS:
                problems.append(f'{table}.{column} is not part of NetHub')
        for name, _type, nullable, _default in target['columns']:
            if name not in present and not nullable and (table, name) not in ADOPT_DEFAULTS:
                problems.append(f'{table}.{name} is missing and has no known value to fill')
    return create, rebuild, problems


def _adoption_engine(url: str):
    """Same transaction handling as migrations/env.py: one real transaction."""
    engine = _engine(url)

    @event.listens_for(engine, 'connect')
    def _no_driver_transactions(dbapi_connection, connection_record):
        dbapi_connection.isolation_level = None

    @event.listens_for(engine, 'begin')
    def _begin_immediate(connection):
        connection.exec_driver_sql('BEGIN IMMEDIATE')

    return engine


def adopt(url: str) -> None:
    """Make a database created by `create_all()` match the baseline, then
    record it as being at the baseline. One transaction: all of it or none."""
    with tempfile.TemporaryDirectory() as tmp:
        want, baseline_sql = _baseline(tmp)
    have = describe(url)
    if have == want:
        repairs = []
        create, rebuild = [], []
    else:
        create, rebuild, problems = _plan_adoption(have, want)
        if problems:
            raise SchemaError(
                'this database predates migrations and differs from the baseline '
                'in ways NetHub will not repair automatically:\n  - '
                + '\n  - '.join(problems)
            )
        repairs = [f'create {t}' for t in create] + [f'rebuild {t}' for t in rebuild]
    log.warning('adopting a database created before migrations%s',
                f' ({", ".join(repairs)})' if repairs else '')

    engine = _adoption_engine(url)
    try:
        with engine.connect() as connection:
            connection.connection.driver_connection.execute('PRAGMA foreign_keys=OFF')
            with connection.begin():
                for table in create:
                    for _kind, _name, text in baseline_sql[table]:
                        connection.exec_driver_sql(text)
                for table in rebuild:
                    _rebuild(connection, table, have['tables'][table],
                             want['tables'][table], baseline_sql[table])
                dangling = connection.exec_driver_sql('PRAGMA foreign_key_check').fetchall()
                if dangling:
                    raise SchemaError(
                        f'adopting would leave {len(dangling)} dangling foreign '
                        f'key(s), first: {dangling[0]}; nothing was changed'
                    )
                MigrationContext.configure(connection).stamp(_script(), BASELINE)
    finally:
        engine.dispose()

    left = differences(describe(url), want)
    if left:  # pragma: no cover -- a bug in this module, not in the database
        raise SchemaError('adoption finished but the schema still differs:\n  - '
                          + '\n  - '.join(left))


def _rebuild(connection, table: str, current: dict, target: dict, sql: list) -> None:
    """SQLite's documented way to change a table: build the new one, copy the
    rows across, drop the old one, rename. Its indexes and triggers go with
    the old table and are recreated from the baseline."""
    temp = f'_adopt_{table}'
    create_table = next(text for kind, _name, text in sql if kind == 'table')
    connection.exec_driver_sql(
        re.sub(r'^CREATE TABLE\s+("?)\w+\1', f'CREATE TABLE "{temp}"', create_table)
    )
    present = set(_column_names(current))
    columns, values = [], []
    for name in _column_names(target):
        if (table, name) in ADOPT_EXPRESSIONS and name in present:
            value = ADOPT_EXPRESSIONS[(table, name)]
        elif name in present:
            value = f'"{name}"'
        elif (table, name) in ADOPT_DEFAULTS:
            value = ADOPT_DEFAULTS[(table, name)]
        else:
            value = 'NULL'
        columns.append(f'"{name}"')
        values.append(value)
    connection.exec_driver_sql(
        f'INSERT INTO "{temp}" ({", ".join(columns)}) '
        f'SELECT {", ".join(values)} FROM "{table}"'
    )
    connection.exec_driver_sql(f'DROP TABLE "{table}"')
    connection.exec_driver_sql(f'ALTER TABLE "{temp}" RENAME TO "{table}"')
    for kind, _name, text in sql:
        if kind in ('index', 'trigger'):
            connection.exec_driver_sql(text)
