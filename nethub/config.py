import os
from datetime import timedelta

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

# Refusing an *absent* key is not enough: the reference Quadlet unit used to
# ship a working placeholder, so a deployment copied from it started normally
# with a signing key published in a public repository. There is no server-side
# `sessions` row in this alpha (§4.5, see alpha.md), so the cookie signature is
# the only thing authenticating anyone -- a known key is a forged admin session
# with no password and no login event. A placeholder is an unset setting wearing
# a value, and it fails closed for the same reason DEVICE_TARGET_CIDRS does.
SECRET_KEY_MIN_LENGTH = 32
_REJECTED_SECRET_KEYS = frozenset({
    'CHANGE_ME_use_openssl_rand_hex_32',
    'CHANGE_ME',
    'changeme',
    'secret',
    'dev',
    'development',
})
if SECRET_KEY in _REJECTED_SECRET_KEYS:
    raise ValueError(
        "SECRET_KEY is a known placeholder value and would be trivially "
        "guessable. Generate a real one with `openssl rand -hex 32`."
    )
if len(SECRET_KEY) < SECRET_KEY_MIN_LENGTH:
    raise ValueError(
        f"SECRET_KEY must be at least {SECRET_KEY_MIN_LENGTH} characters; got "
        f"{len(SECRET_KEY)}. Generate one with `openssl rand -hex 32`."
    )

# Session cookie hardening. design-document.md §4.5 asks specifically for
# SameSite=Strict on approvals -- a cross-site "approve: reload" is a fleet
# outage -- and CSRF is already complete, so these are defence in depth rather
# than the primary control. Secure defaults ON: the Quadlet unit publishes
# plain HTTP on 8080, so a deployment with no TLS terminator in front would
# otherwise send the cookie in cleartext on the ops LAN, and there is no
# server-side sessions row to revoke it against (§4.5, see alpha.md) -- it
# stays valid until SECRET_KEY rotates. Set SESSION_COOKIE_INSECURE=1 for
# local HTTP development only.
SESSION_COOKIE_SAMESITE = 'Strict'
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SECURE = os.getenv('SESSION_COOKIE_INSECURE', '') != '1'

# An absolute bound on a signed cookie that otherwise carries no expiry of its
# own. Flask applies this only to a session marked `permanent`, which
# nethub/auth.py does at login -- the two halves are what make it effective, so
# removing `session.permanent = True` there silently turns this back into dead
# configuration with no error anywhere. Verified live: a real login emits
# `Expires=` roughly 12 hours out.
PERMANENT_SESSION_LIFETIME = timedelta(hours=12)

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
