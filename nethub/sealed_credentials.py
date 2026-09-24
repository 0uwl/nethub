"""Device credentials sealed into the job row (PLAN.md WS-7, design doc §9.1).

An approval collects the approver's device password. Flask seals it to the
sibling's **public** key with libsodium's sealed box (PyNaCl `SealedBox`) and
writes the ciphertext to `upgrade_phase_jobs.sealed_credential` in the same
transaction that creates the `queued` row. The sibling clears the column in
the same statement that claims the job, opens it with its **private** key and
checks what it was sealed for. Flask can encrypt and never decrypt, so a
database copy -- a backup, a stolen file -- holds no password.

What is sealed binds the credential to one execution: the job id, the
identity that supplied it (the approver, or the submitter for pre-check,
which has no gate), and when it stops being usable. A sealed box proves
nothing about who sealed it -- anyone holding the public key can make one --
so these checks stop a blob being copied onto another job's row, not a
forgery. Whoever could forge one must supply the password themselves, and so
learns nothing by doing it.

The credential is still checked against the character allowlist on both
sides: it ends up on a device CLI, and the sibling is the privileged side
opening bytes a compromised Flask may have chosen.

Keys: `python -m nethub.sealed_credentials keygen --out <file>` writes the
private key (base64, mode 0600) and prints the public one.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone

from nacl.exceptions import CryptoError
from nacl.public import PrivateKey, PublicKey, SealedBox

from .credentials import read_credential

#: Both units carry the public key: Flask seals with it, and the sibling checks
#: its private key matches it before taking any work.
PUBLIC_KEY_ENV = 'NETHUB_CREDENTIAL_PUBLIC_KEY'
#: The sibling's private key: a systemd credential of this name...
PRIVATE_KEY_CREDENTIAL = 'credential_private_key'
#: ...or a file only the sibling unit can read. Never the key itself in an
#: environment variable: that is readable in /proc/<pid>/environ and inherited
#: by every child.
PRIVATE_KEY_FILE_ENV = 'NETHUB_CREDENTIAL_KEY_FILE'

#: What the sibling will send to a device CLI. Printable ASCII only -- a
#: control character here would reach a CLI session.
_ALLOWED = frozenset(chr(c) for c in range(0x20, 0x7F))
MAX_CREDENTIAL_LEN = 128
#: Far above any honest payload; bounds what the sibling will try to open.
MAX_SEALED_BYTES = 4096
_FIELDS = frozenset({'job_id', 'approved_by', 'username', 'password', 'expires_at'})


class CredentialError(Exception):
    """No usable credential for this execution. The message is fixed text of
    ours, never library or peer text, because it becomes `error_summary`."""


class KeyConfigError(Exception):
    """The deployment's key configuration is missing or wrong."""


def check_credential(value) -> str:
    """Allowlist a credential before it is used anywhere."""
    if not isinstance(value, str) or not value or len(value) > MAX_CREDENTIAL_LEN:
        raise CredentialError('credential is empty or over the length cap')
    if not set(value) <= _ALLOWED:
        raise CredentialError('credential contains characters outside the allowlist')
    return value


@dataclass(frozen=True)
class Credential:
    username: str
    #: repr=False here and on phases.PhaseContext: one `log.debug("%r", ...)`
    #: added while debugging would otherwise write the password to journald.
    password: str = field(repr=False)


# -- keys -----------------------------------------------------------------------

def encode_key(key: PublicKey | PrivateKey) -> str:
    return base64.b64encode(bytes(key)).decode('ascii')


def _decode(text: str, what: str) -> bytes:
    try:
        raw = base64.b64decode((text or '').strip(), validate=True)
    except (binascii.Error, ValueError):
        raw = b''
    if len(raw) != 32:
        raise KeyConfigError(f'{what} is not a base64-encoded 32-byte key')
    return raw


def load_public_key(text: str | None) -> PublicKey:
    if not text:
        raise KeyConfigError(
            f'{PUBLIC_KEY_ENV} is not set. Generate a key pair with '
            f'`python -m nethub.sealed_credentials keygen --out <file>`.'
        )
    return PublicKey(_decode(text, PUBLIC_KEY_ENV))


def load_private_key() -> PrivateKey:
    """The sibling's key: the systemd credential, else the key file."""
    text = read_credential(PRIVATE_KEY_CREDENTIAL)
    source = f'systemd credential {PRIVATE_KEY_CREDENTIAL!r}'
    if text is None:
        path = os.environ.get(PRIVATE_KEY_FILE_ENV)
        if not path:
            raise KeyConfigError(
                f'no private key: load the systemd credential '
                f'{PRIVATE_KEY_CREDENTIAL!r} or set {PRIVATE_KEY_FILE_ENV}'
            )
        try:
            with open(path, encoding='ascii') as handle:
                text = handle.read()
        except OSError as exc:
            raise KeyConfigError(f'cannot read {PRIVATE_KEY_FILE_ENV} ({type(exc).__name__})') from None
        source = PRIVATE_KEY_FILE_ENV
    return PrivateKey(_decode(text, source))


def check_pair(private_key: PrivateKey, public_key: PublicKey) -> None:
    """Refuse to start on mismatched keys. Otherwise every job would fail, one
    at a time, with a credential error that says nothing about keys."""
    if bytes(private_key.public_key) != bytes(public_key):
        raise KeyConfigError(
            f'the private key does not match {PUBLIC_KEY_ENV}; Flask would seal '
            f'credentials this sibling cannot open'
        )


# -- sealing and opening --------------------------------------------------------

def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def seal(public_key: PublicKey, *, job_id: int, approved_by: int, username: str,
         password: str, expires_at: datetime) -> bytes:
    """Flask's side. The plaintext exists only for the length of this call."""
    payload = json.dumps({
        'job_id': job_id,
        'approved_by': approved_by,
        'username': check_credential(username),
        'password': check_credential(password),
        'expires_at': _aware(expires_at).isoformat(),
    }, separators=(',', ':'))
    return SealedBox(public_key).encrypt(payload.encode('utf-8'))


def open_sealed(private_key: PrivateKey, blob: bytes | None, *, job_id: int,
                approved_by: int, now: datetime) -> Credential:
    """The sibling's side. Everything inside is treated as untrusted input."""
    if blob is None:
        raise CredentialError('no credential was sealed for this execution')
    if len(blob) > MAX_SEALED_BYTES:
        raise CredentialError('sealed credential is over the size cap')
    try:
        raw = SealedBox(private_key).decrypt(bytes(blob))
    except CryptoError:
        raise CredentialError('sealed credential could not be opened') from None
    try:
        message = json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, ValueError):
        raise CredentialError('sealed credential is malformed') from None
    if not isinstance(message, dict) or set(message) != _FIELDS:
        raise CredentialError('sealed credential is malformed')
    # `type(...) is int`, not isinstance: bool is a subclass of int, and
    # True == 1 would let a credential sealed for "true" pass for job 1.
    if type(message['job_id']) is not int or message['job_id'] != job_id:
        raise CredentialError('credential was sealed for a different execution')
    if type(message['approved_by']) is not int or message['approved_by'] != approved_by:
        raise CredentialError('credential was sealed by a different identity than approved')
    try:
        expires_at = _aware(datetime.fromisoformat(message['expires_at']))
    except (TypeError, ValueError):
        raise CredentialError('sealed credential is malformed') from None
    if _aware(now) >= expires_at:
        raise CredentialError('sealed credential expired; the phase needs re-approval')
    return Credential(check_credential(message['username']),
                      check_credential(message['password']))


# -- the key-pair command -------------------------------------------------------

def _keygen(out: str) -> int:
    key = PrivateKey.generate()
    try:
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        print(f'{out} already exists; not overwriting a private key', file=sys.stderr)
        return 1
    with os.fdopen(fd, 'w', encoding='ascii') as handle:
        handle.write(encode_key(key) + '\n')
    print(f'private key written to {out} (mode 0600). Give it to the sibling unit only,')
    print(f'as the systemd credential {PRIVATE_KEY_CREDENTIAL!r} or via {PRIVATE_KEY_FILE_ENV}.')
    print('Set this in both units:')
    print(f'{PUBLIC_KEY_ENV}={encode_key(key.public_key)}')
    return 0


def _public_key(path: str) -> int:
    os.environ[PRIVATE_KEY_FILE_ENV] = path
    print(encode_key(load_private_key().public_key))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog='python -m nethub.sealed_credentials')
    commands = parser.add_subparsers(dest='command', required=True)
    generate = commands.add_parser('keygen', help='create the sibling key pair')
    generate.add_argument('--out', required=True, help='file for the private key')
    show = commands.add_parser('public-key', help="print a private key file's public key")
    show.add_argument('--key', required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'keygen':
            return _keygen(args.out)
        return _public_key(args.key)
    except KeyConfigError as exc:
        print(exc, file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
