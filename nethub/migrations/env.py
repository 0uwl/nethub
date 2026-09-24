"""Alembic environment for NetHub's single SQLite database.

Run two ways, with the same behaviour: by `nethub.schema.upgrade_database()`
at web startup, and by `flask --app nethub db ...` by hand. The first passes
the database URL in `config.attributes['url']`; the second gets it from the
Flask app, as Flask-Migrate's own template does.

Two things here are not Alembic's defaults, and both are what make "a failed
upgrade leaves the database as it was" true:

- **Real transactions.** pysqlite only issues BEGIN before DML, so by default
  every CREATE/ALTER/DROP autocommits on its own and a migration that fails
  halfway leaves half its DDL applied. The migration engine turns pysqlite's
  transaction handling off and issues `BEGIN IMMEDIATE` itself, so the whole
  upgrade is one transaction, holding the write lock throughout.
- **Foreign keys off while tables are rebuilt.** SQLite changes a constraint
  by building a new table, copying the rows and dropping the old one
  (Alembic's batch mode). With `foreign_keys=ON`, dropping a table other
  tables reference fails or rewrites their rows. The pragma cannot change
  inside a transaction, so it is set before BEGIN, and `PRAGMA
  foreign_key_check` runs before COMMIT so a migration cannot leave a
  dangling reference behind.
"""

from alembic import context
from sqlalchemy import create_engine, event
from sqlalchemy.pool import NullPool

from nethub import models  # noqa: F401 -- registers every table on db.metadata
from nethub.extensions import db

config = context.config


def _database_url():
    url = config.attributes.get('url')
    if url:
        return url
    from flask import current_app
    return current_app.extensions['migrate'].db.engine.url.render_as_string(
        hide_password=False
    )


def _migration_engine(url):
    """A private engine whose transactions are real SQLite transactions."""
    engine = create_engine(url, poolclass=NullPool)

    @event.listens_for(engine, 'connect')
    def _no_driver_transactions(dbapi_connection, connection_record):
        dbapi_connection.isolation_level = None

    @event.listens_for(engine, 'begin')
    def _begin_immediate(connection):
        connection.exec_driver_sql('BEGIN IMMEDIATE')

    return engine


def _skip_empty_autogenerate(context, revision, directives):
    """`flask db migrate` with no model changes writes nothing."""
    if getattr(config.cmd_opts, 'autogenerate', False) and directives[0].upgrade_ops.is_empty():
        directives[:] = []


def run_migrations_online():
    engine = _migration_engine(_database_url())
    with engine.connect() as connection:
        # On the raw connection, before SQLAlchemy begins: the pragma is a
        # no-op inside a transaction. NullPool means this connection is closed
        # afterwards rather than returned to a pool with the pragma off.
        connection.connection.driver_connection.execute('PRAGMA foreign_keys=OFF')
        context.configure(
            connection=connection,
            target_metadata=db.metadata,
            render_as_batch=True,
            transactional_ddl=True,
            process_revision_directives=_skip_empty_autogenerate,
        )
        with context.begin_transaction():
            context.run_migrations()
            dangling = connection.exec_driver_sql('PRAGMA foreign_key_check').fetchall()
            if dangling:
                raise RuntimeError(
                    f'migration left {len(dangling)} dangling foreign key(s), '
                    f'first: {dangling[0]}; rolled back'
                )
    engine.dispose()


if context.is_offline_mode():
    raise SystemExit('offline (--sql) migrations are not supported for SQLite here')
run_migrations_online()
