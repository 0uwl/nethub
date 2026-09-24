"""Settings both processes need: the database, the artifact store, and the
deployment-level upgrade settings.

Split out of `config.py` (PLAN.md WS-3) so the sibling can load its database
settings without importing the web tier's `SECRET_KEY` validation. The
sibling serves no HTTP and signs no cookies, and it should not hold the key
that forges an admin session. `create_app()` loads this module and then
`config.py`; `sibling._database_app()` loads only this one.
"""
import os

# Project root (one level up from this package), not this file's own
# directory -- database.db and instance/ live beside the package, matching
# Flask's own instance-folder convention and the existing .gitignore entries.
basedir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Bare SQLite file path -- not a full SQLAlchemy URL. Overridable so a
# container can point it at a mounted volume (e.g. /app/data/database.db).
DATABASE_PATH = os.getenv('DATABASE_PATH', os.path.join(basedir, 'database.db'))
SQLALCHEMY_DATABASE_URI = 'sqlite:///' + DATABASE_PATH
SQLALCHEMY_TRACK_MODIFICATIONS = False

# Where NetHub keeps the image bytes it was given. NetHub owns this directory
# now (design doc §3.3 -- it is the sole source of the bytes), which is the
# difference from the REGISTRIES_ROOT this replaced at build step 7: that was
# a place an admin bind-mounted *their* files into for NetHub to point at.
# The push addresses it by filename, so it is one flat directory and
# Artifact.storage_path is a path inside it.
ARTIFACT_STORE = os.getenv('ARTIFACT_STORE', os.path.join(basedir, 'instance', 'artifacts'))

# Deployment-level upgrade settings. Design doc §5 puts these in a `settings`
# table with an append-only audit; alpha has neither, so they are env vars and
# this is the honest stand-in rather than a stand-in for one.

# Comma-separated CIDRs a submitted target address must fall inside. One of
# the two constraints on `ansible_host`, the only connection var a request may
# supply (design doc §4.3/§8.1); the other is the fail-closed host-key check.
# Empty means no run may be submitted -- fail closed rather than open.
DEVICE_TARGET_CIDRS = [
    c.strip() for c in os.getenv('DEVICE_TARGET_CIDRS', '').split(',') if c.strip()
]
