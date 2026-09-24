import sqlite3

from flask_login import LoginManager
from flask_sqlalchemy import SQLAlchemy
from flask_wtf import CSRFProtect
from sqlalchemy import event
from sqlalchemy.engine import Engine

db = SQLAlchemy()
login_manager = LoginManager()
login_manager.login_view = 'auth.login'
csrf = CSRFProtect()


#: How long a writer waits for the other process's lock before giving up.
#: pysqlite's own `timeout` already defaults to 5 s; setting it here means the
#: value no longer depends on a driver default nobody chose.
BUSY_TIMEOUT_MS = 5000


@event.listens_for(Engine, "connect")
def _configure_sqlite(dbapi_connection, connection_record):
    """Per-connection SQLite settings design doc §5 requires. Set on the
    generic `Engine` event rather than per app, so the test fixtures get them
    too; a database that enforces less than production passes tests
    production would fail.

    - `foreign_keys=ON`: SQLite ignores FOREIGN KEY unless asked, once per
      connection. The upgrade tables lean on theirs: without it a per-host
      result row can be written for a phase execution nobody approved, or for
      a hostname that was never in the run's target list.
    - `journal_mode=WAL`: Flask and the sibling are two processes writing one
      file. In the default rollback journal a writer blocks every reader for
      the length of its transaction, so a long sibling write stalled page
      loads. WAL lets readers proceed while one writer writes. The mode is
      stored in the database file; setting it on every connect is cheap and
      covers a file created before this line existed.
    - `busy_timeout`: a second writer waits this long for the lock instead of
      failing at once with "database is locked".
    """
    if isinstance(dbapi_connection, sqlite3.Connection):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        cursor.close()
