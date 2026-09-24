"""Web-only settings: the session-signing key, cookies, the upload cap, DEBUG.

Only `create_app()` loads this, after `shared_config.py`. The sibling never
imports it, which is why `SECRET_KEY` is validated here at import and the
sibling unit carries no key (PLAN.md WS-3).
"""
import os
from datetime import timedelta

from .credentials import read_credential

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
# `sessions` row in this alpha (§4.5, see CLAUDE.md), so the cookie signature is
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
# server-side sessions row to revoke it against (§4.5, see CLAUDE.md) -- it
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

# Cap request size so an upload can't exhaust disk/memory (1.5 GB, comfortably
# above the largest IOS-XE images seen so far -- a cat9k_lite bundle is ~450 MB).
MAX_CONTENT_LENGTH = 1_500 * 1024 * 1024
