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


@event.listens_for(Engine, "connect")
def _enable_sqlite_foreign_keys(dbapi_connection, connection_record):
    """SQLite ignores FOREIGN KEY unless asked, once per connection.

    The upgrade tables lean on theirs rather than treating them as
    documentation (design doc §5): without this, a per-host result row can be
    written for a phase execution nobody approved, or for a hostname that was
    never in the run's target list -- the exact two things those keys were
    added to stop. It is set here rather than per-engine so the test fixtures
    get it too; a database that enforces less than production is a database
    that passes tests production would fail.
    """
    if isinstance(dbapi_connection, sqlite3.Connection):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()
