import os

from .credentials import read_credential

# Project root (one level up from this package), not this file's own
# directory -- database.db and instance/ live beside the package, matching
# Flask's own instance-folder convention and the existing .gitignore entries.
basedir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Off by default now that a login form exists (credential path) -- a
# debugger that renders frame locals would render a submitted password
# right along with them. Set DEBUG=1 for local dev only.
DEBUG = os.getenv('DEBUG', '') == '1'

# Secret key for session management. A systemd credential named 'secret_key'
# (LoadCredential=/SetCredential=) takes priority over the plaintext
# SECRET_KEY env var, same idea as the ADMIN_PASSWORD/admin_password
# credential in nethub/bootstrap.py -- see quadlet/nethub.container.
SECRET_KEY = read_credential('secret_key') or os.getenv('SECRET_KEY')
if SECRET_KEY is None:
    raise ValueError("SECRET_KEY cannot be empty, please generate a random string and supply it through an env variable")

# Bare SQLite file path -- not a full SQLAlchemy URL. Overridable so a
# container can point it at a mounted volume (e.g. /app/data/database.db).
DATABASE_PATH = os.getenv('DATABASE_PATH', os.path.join(basedir, 'database.db'))
SQLALCHEMY_DATABASE_URI = 'sqlite:///' + DATABASE_PATH
SQLALCHEMY_TRACK_MODIFICATIONS = False

# Cap request size so an upload can't exhaust disk/memory (1.5 GB, comfortably
# above the largest IOS-XE images seen so far -- a cat9k_lite bundle is ~450 MB).
MAX_CONTENT_LENGTH = 1_500 * 1024 * 1024

# Where NetHub keeps the image bytes it was given. NetHub owns this directory
# now (design doc §3.3 -- it is the sole source of the bytes), which is the
# difference from the REGISTRIES_ROOT this replaced at build step 7: that was
# a place an admin bind-mounted *their* files into for NetHub to point at.
# Both transports address it by filename, so it is one flat directory and
# Artifact.storage_path is a path inside it.
ARTIFACT_STORE = os.getenv('ARTIFACT_STORE', os.path.join(basedir, 'instance', 'artifacts'))

# Deployment-level upgrade settings. Design doc §5 puts these in a `settings`
# table with an append-only audit; alpha has neither, so they are env vars and
# this is the honest stand-in rather than a stand-in for one.

# Which direction day-2 image bytes move. Deployment-level and never
# request-level: selecting the transport selects whose credential gets spent
# (design doc §4.3.1). Push is the default because pull needs the device to
# open an outbound connection to NetHub, which many deployments block.
IMAGE_TRANSPORT = os.getenv('IMAGE_TRANSPORT', 'push_scp')

# Comma-separated CIDRs a submitted target address must fall inside. One of
# the two constraints on `ansible_host`, the only connection var a request may
# supply (design doc §4.3/§8.1); the other is the fail-closed host-key check.
# Empty means no run may be submitted -- fail closed rather than open.
DEVICE_TARGET_CIDRS = [
    c.strip() for c in os.getenv('DEVICE_TARGET_CIDRS', '').split(',') if c.strip()
]

# §4.4's shared account mode is not implemented in alpha and must not be
# inferred from anything. It is snapshotted onto every run so an auditor can
# tell "jsmith ran this" from "everyone runs as jsmith".
SHARED_ACCOUNT_MODE = False
