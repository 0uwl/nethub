"""Checks for nethub/sealed_credentials.py (PLAN.md WS-7).

The sealed box is libsodium's; what is ours, and tested here, is what gets
sealed, what opening it checks, and how the keys are found. Every refusal is
a CredentialError whose message is fixed text, because it becomes a
year-retained error_summary.
"""

import json
import os
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest
from nacl.public import PrivateKey, SealedBox

from nethub import sealed_credentials as SC

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
KEY = PrivateKey.generate()


def sealed(**overrides):
    fields = {"job_id": 7, "approved_by": 3, "username": "jsmith", "password": "s3cret",
              "expires_at": NOW + timedelta(hours=1)}
    fields.update(overrides)
    return SC.seal(KEY.public_key, **fields)


def open_(blob, job_id=7, approved_by=3, now=NOW, key=KEY):
    return SC.open_sealed(key, blob, job_id=job_id, approved_by=approved_by, now=now)


def raw_payload(payload: dict | bytes) -> bytes:
    """A sealed box around anything, bypassing seal()'s own checks -- what a
    compromised Flask could write to the column."""
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return SealedBox(KEY.public_key).encrypt(body)


class TestSealAndOpen:
    def test_round_trip(self):
        credential = open_(sealed())
        assert (credential.username, credential.password) == ("jsmith", "s3cret")

    def test_the_password_is_not_in_the_ciphertext_or_the_repr(self):
        blob = sealed()
        assert b"s3cret" not in blob and b"jsmith" not in blob
        assert "s3cret" not in repr(open_(blob))

    def test_each_seal_is_different(self):
        """An observer of the column cannot tell two approvals by the same
        person with the same password apart."""
        assert sealed() != sealed()

    def test_naive_timestamps_are_read_as_utc(self):
        """SQLite hands deadline_at back naive; sealing it must not shift it."""
        blob = sealed(expires_at=(NOW + timedelta(hours=1)).replace(tzinfo=None))
        open_(blob, now=NOW + timedelta(minutes=59))
        with pytest.raises(SC.CredentialError, match="expired"):
            open_(blob, now=NOW + timedelta(hours=1))

    def test_seal_refuses_what_the_sibling_would_refuse(self):
        for bad in ("", "tab\there", "é", "x" * 129):
            with pytest.raises(SC.CredentialError):
                sealed(password=bad)


class TestOpenRefuses:
    def test_nothing_sealed(self):
        with pytest.raises(SC.CredentialError, match="no credential was sealed"):
            open_(None)

    def test_a_tampered_byte(self):
        blob = bytearray(sealed())
        blob[-1] ^= 1
        with pytest.raises(SC.CredentialError, match="could not be opened"):
            open_(bytes(blob))

    def test_another_key(self):
        with pytest.raises(SC.CredentialError, match="could not be opened"):
            open_(sealed(), key=PrivateKey.generate())

    def test_another_job(self):
        """Stops one job's ciphertext being copied onto another job's row."""
        with pytest.raises(SC.CredentialError, match="different execution"):
            open_(sealed(), job_id=8)

    def test_a_bool_is_not_job_1(self):
        """`True == 1`; `type(...) is int` is what keeps them apart."""
        blob = raw_payload({"job_id": True, "approved_by": 3, "username": "j",
                            "password": "p", "expires_at": (NOW + timedelta(hours=1)).isoformat()})
        with pytest.raises(SC.CredentialError, match="different execution"):
            open_(blob, job_id=1)

    def test_another_approver(self):
        with pytest.raises(SC.CredentialError, match="different identity"):
            open_(sealed(), approved_by=4)

    def test_expired(self):
        with pytest.raises(SC.CredentialError, match="expired"):
            open_(sealed(), now=NOW + timedelta(hours=1))

    @pytest.mark.parametrize("payload", [
        b"not json",
        b"\xff\xfe",
        {"job_id": 7},
        {"job_id": 7, "approved_by": 3, "username": "j", "password": "p",
         "expires_at": "tomorrow"},
        {"job_id": 7, "approved_by": 3, "username": "j", "password": "p",
         "expires_at": "2099-01-01T00:00:00+00:00", "extra": 1},
    ], ids=["not-json", "not-utf8", "missing-fields", "bad-expiry", "extra-field"])
    def test_a_malformed_payload(self, payload):
        with pytest.raises(SC.CredentialError, match="malformed"):
            open_(raw_payload(payload))

    def test_a_credential_outside_the_allowlist_even_if_sealed(self):
        """The sibling is the privileged side and a compromised Flask chooses
        what is sealed: the allowlist is applied again on opening."""
        blob = raw_payload({"job_id": 7, "approved_by": 3, "username": "jsmith",
                            "password": "pw\nreload",
                            "expires_at": (NOW + timedelta(hours=1)).isoformat()})
        with pytest.raises(SC.CredentialError, match="allowlist"):
            open_(blob)

    def test_an_oversize_blob_is_not_even_opened(self):
        with pytest.raises(SC.CredentialError, match="size cap"):
            open_(b"\x00" * (SC.MAX_SEALED_BYTES + 1))


class TestKeys:
    def test_public_key_is_required_and_checked(self):
        with pytest.raises(SC.KeyConfigError, match="NETHUB_CREDENTIAL_PUBLIC_KEY is not set"):
            SC.load_public_key(None)
        with pytest.raises(SC.KeyConfigError, match="32-byte key"):
            SC.load_public_key("not-a-key")
        assert bytes(SC.load_public_key(SC.encode_key(KEY.public_key))) == bytes(KEY.public_key)

    def test_private_key_from_a_file(self, tmp_path, monkeypatch):
        path = tmp_path / "key"
        path.write_text(SC.encode_key(KEY) + "\n")
        monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
        monkeypatch.setenv(SC.PRIVATE_KEY_FILE_ENV, str(path))
        assert bytes(SC.load_private_key()) == bytes(KEY)

    def test_the_systemd_credential_wins_over_the_file(self, tmp_path, monkeypatch):
        other = PrivateKey.generate()
        (tmp_path / SC.PRIVATE_KEY_CREDENTIAL).write_text(SC.encode_key(other))
        (tmp_path / "file").write_text(SC.encode_key(KEY))
        monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
        monkeypatch.setenv(SC.PRIVATE_KEY_FILE_ENV, str(tmp_path / "file"))
        assert bytes(SC.load_private_key()) == bytes(other)

    def test_no_private_key_is_an_error_naming_both_sources(self, monkeypatch):
        monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
        monkeypatch.delenv(SC.PRIVATE_KEY_FILE_ENV, raising=False)
        with pytest.raises(SC.KeyConfigError, match="credential_private_key.*NETHUB_CREDENTIAL_KEY_FILE"):
            SC.load_private_key()

    def test_an_unreadable_key_file_names_the_problem_not_the_path(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
        monkeypatch.setenv(SC.PRIVATE_KEY_FILE_ENV, str(tmp_path / "absent"))
        with pytest.raises(SC.KeyConfigError, match="FileNotFoundError"):
            SC.load_private_key()

    def test_a_mismatched_pair_is_refused(self):
        with pytest.raises(SC.KeyConfigError, match="does not match"):
            SC.check_pair(KEY, PrivateKey.generate().public_key)
        SC.check_pair(KEY, KEY.public_key)


class TestKeygenCommand:
    def run(self, *args):
        return subprocess.run([sys.executable, "-m", "nethub.sealed_credentials", *args],
                              capture_output=True, text=True, timeout=60, check=False)

    def test_it_writes_a_0600_private_key_and_prints_the_matching_public_key(self, tmp_path):
        out = tmp_path / "credential_private_key"
        result = self.run("keygen", "--out", str(out))
        assert result.returncode == 0, result.stderr
        assert stat.S_IMODE(os.stat(out).st_mode) == 0o600
        printed = next(line for line in result.stdout.splitlines()
                       if line.startswith(SC.PUBLIC_KEY_ENV + "="))
        key = PrivateKey(SC._decode(out.read_text(), "test"))
        assert printed.split("=", 1)[1] == SC.encode_key(key.public_key)
        assert self.run("public-key", "--key", str(out)).stdout.strip() == printed.split("=", 1)[1]

    def test_it_never_overwrites_a_private_key(self, tmp_path):
        out = tmp_path / "credential_private_key"
        out.write_text("existing")
        result = self.run("keygen", "--out", str(out))
        assert result.returncode == 1
        assert out.read_text() == "existing"


class TestWebStartup:
    """create_app() checks the key before it migrates anything."""

    def run(self, tmp_path, public_key):
        env = {k: v for k, v in os.environ.items() if k != SC.PUBLIC_KEY_ENV}
        env.update(DATABASE_PATH=str(tmp_path / "web.db"), SECRET_KEY="k" * 64)
        if public_key is not None:
            env[SC.PUBLIC_KEY_ENV] = public_key
        return subprocess.run([sys.executable, "-c", "from nethub import create_app; create_app()"],
                              env=env, cwd=os.getcwd(), capture_output=True, text=True,
                              timeout=60, check=False)

    def test_it_refuses_to_start_without_a_public_key(self, tmp_path):
        result = self.run(tmp_path, None)
        assert result.returncode != 0
        assert "NETHUB_CREDENTIAL_PUBLIC_KEY is not set" in result.stderr
        assert not (tmp_path / "web.db").exists(), "refused before migrating"

    def test_it_refuses_a_malformed_public_key(self, tmp_path):
        result = self.run(tmp_path, "not-a-key")
        assert result.returncode != 0
        assert "32-byte key" in result.stderr
