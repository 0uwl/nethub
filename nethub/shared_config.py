"""Settings both processes need: the database, the artifact store, and the
deployment-level upgrade settings.

Split out of `config.py` so the sibling can load its database
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
# and is the sole source of the bytes. The push addresses it by filename, so
# it is one flat directory and Artifact.storage_path is a path inside it. The
# sibling reads the same directory as NETHUB_SEARCH_DIR.
ARTIFACT_STORE = os.getenv('ARTIFACT_STORE', os.path.join(basedir, 'instance', 'artifacts'))

# Deployment-level upgrade settings, as environment variables. The `settings`
# table holds only the two-person rules (docs/future.md).

# The sibling's public key (nethub/sealed_credentials.py). Flask
# seals device credentials to it; the sibling checks its private key matches
# it before taking work. Public, so an environment variable is fine; both
# units set it. create_app() refuses to start without it.
NETHUB_CREDENTIAL_PUBLIC_KEY = os.getenv('NETHUB_CREDENTIAL_PUBLIC_KEY')

# Comma-separated CIDRs a submitted target address must fall inside. One of
# the two constraints on `ansible_host`, the only connection var a request may
# supply; the other is the fail-closed host-key check.
# Empty means no run may be submitted -- fail closed rather than open.
DEVICE_TARGET_CIDRS = [
    c.strip() for c in os.getenv('DEVICE_TARGET_CIDRS', '').split(',') if c.strip()
]

#: Hosts a phase runs at once.
DEFAULT_PHASE_CONCURRENCY = 4


def phase_concurrency(value: str | None) -> int:
    """`PHASE_CONCURRENCY`, or refuse to start. Unset means the default."""
    if value is None or not value.strip():
        return DEFAULT_PHASE_CONCURRENCY
    try:
        number = int(value)
    except ValueError:
        number = 0
    if number < 1:
        raise SystemExit(f'PHASE_CONCURRENCY must be a whole number of at least 1, '
                         f'not {value!r}')
    return number


# How many hosts the sibling runs a phase on at once, and the most devices an
# activate approval may reload together after its canary. Here
# rather than in the sibling alone because the web process shows it as that
# cap on the approve form, so both units set it, to the same value. Both
# processes refuse to start on anything but a whole number of at least 1.
PHASE_CONCURRENCY = phase_concurrency(os.getenv('PHASE_CONCURRENCY'))
