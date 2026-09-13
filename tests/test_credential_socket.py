"""Checks for the secret channel (design doc §9.1/§9.2).

Real AF_UNIX sockets, in a temp directory. The socket here is created by the
test rather than by the app, which is the point: production gets its listening
socket from a systemd `.socket` unit, and nothing in nethub/ calls bind().
"""

import json
import os
import socket
import threading
from datetime import datetime, timedelta, timezone

import pytest

from nethub import credential_socket as CS

NOW = datetime(2026, 9, 9, tzinfo=timezone.utc)
PASSWORD = "d3vice-p@ss"


@pytest.fixture
def clock():
    return {"t": NOW}


@pytest.fixture
def store(clock):
    return CS.CredentialStore(now=lambda: clock["t"])


@pytest.fixture
def server(tmp_path, store):
    """A listening socket plus the serving thread, as the sibling sees it."""
    path = str(tmp_path / "cred.sock")
    listening = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listening.bind(path)
    os.chmod(path, 0o600)
    listening.listen(4)
    stop = threading.Event()
    state = {"approved_by": 7, "running": True}

    def verify_running(job_id):
        if not state["running"]:
            raise CS.CredentialError("job is not running")
        return state["approved_by"]

    thread = threading.Thread(
        target=CS.serve, args=(listening, store, verify_running),
        kwargs={"stop": stop}, daemon=True,
    )
    thread.start()

    def connect():
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(path)
        return client

    yield connect, state, path
    stop.set()
    thread.join(timeout=3)
    listening.close()


class TestAllowlist:
    @pytest.mark.parametrize("bad", ["", "a" * 129, "pass\nword", "pass\x00word", "tab\there"])
    def test_credentials_outside_the_allowlist_are_refused(self, bad):
        with pytest.raises(CS.CredentialError):
            CS.check_credential(bad)

    def test_ordinary_printable_passwords_pass(self):
        assert CS.check_credential("P@ssw0rd! ~#$%") == "P@ssw0rd! ~#$%"


class TestStore:
    def test_release_is_one_shot(self, store):
        store.hold(1, "jsmith", PASSWORD, approved_by=7)
        assert store.release(1, 7) == ("jsmith", PASSWORD)
        with pytest.raises(CS.CredentialError, match="no credential held"):
            store.release(1, 7)

    def test_a_held_credential_expires(self, store, clock):
        store.hold(1, "jsmith", PASSWORD, approved_by=7)
        clock["t"] = NOW + timedelta(hours=2)
        with pytest.raises(CS.CredentialError, match="expired"):
            store.release(1, 7)

    def test_the_approving_identity_is_cross_checked(self, store):
        """Keyed by job, and the approver must match -- §9.1."""
        store.hold(1, "jsmith", PASSWORD, approved_by=7)
        with pytest.raises(CS.CredentialError, match="approving identity"):
            store.release(1, approved_by=8)

    def test_keys_are_job_ids_not_run_ids(self, store):
        """Two phases of one run hold separately, or one person's password
        would eventually serve another person's approved execution."""
        store.hold(10, "alice", "aaa", approved_by=1)
        store.hold(11, "bob", "bbb", approved_by=2)
        assert store.release(11, 2) == ("bob", "bbb")
        assert store.release(10, 1) == ("alice", "aaa")

    def test_expired_entries_can_be_purged(self, store, clock):
        store.hold(1, "jsmith", PASSWORD, approved_by=7)
        store.hold(2, "jsmith", PASSWORD, approved_by=7)
        clock["t"] = NOW + timedelta(hours=2)
        assert store.purge_expired() == 2
        assert len(store) == 0


class TestOverTheSocket:
    def test_the_sibling_fetches_a_held_credential(self, server, store):
        connect, _, _ = server
        store.hold(42, "jsmith", PASSWORD, approved_by=7)
        assert CS.fetch_credential(connect, 42) == ("jsmith", PASSWORD)

    def test_a_second_fetch_gets_nothing(self, server, store):
        connect, _, _ = server
        store.hold(42, "jsmith", PASSWORD, approved_by=7)
        CS.fetch_credential(connect, 42)
        with pytest.raises(CS.CredentialError, match="not released"):
            CS.fetch_credential(connect, 42)

    def test_a_job_that_is_not_running_gets_nothing(self, server, store):
        connect, state, _ = server
        store.hold(42, "jsmith", PASSWORD, approved_by=7)
        state["running"] = False
        with pytest.raises(CS.CredentialError):
            CS.fetch_credential(connect, 42)
        assert len(store) == 1, "the credential was not consumed by a refused request"

    def test_the_socket_carries_no_control_information(self, server, store):
        """One message type in, one out. Anything else is refused."""
        connect, _, _ = server
        for message in (
            {"job_id": 42, "cancel": True},
            {"run_id": 1},
            {"job_id": "42"},
            {},
            ["job_id", 42],
        ):
            client = connect()
            client.sendall(json.dumps(message).encode() + b"\n")
            reply = json.loads(client.recv(4096).decode())
            client.close()
            assert reply["ok"] is False

    def test_garbage_does_not_crash_the_server(self, server, store):
        connect, _, _ = server
        client = connect()
        client.sendall(b"not json at all\n")
        assert json.loads(client.recv(4096).decode())["ok"] is False
        client.close()
        # Still serving.
        store.hold(1, "jsmith", PASSWORD, approved_by=7)
        assert CS.fetch_credential(connect, 1) == ("jsmith", PASSWORD)

    def test_an_oversized_request_is_refused(self, server, store):
        connect, _, _ = server
        client = connect()
        client.sendall(b'{"job_id": 1, "pad": "' + b"x" * 9000 + b'"}\n')
        try:
            data = client.recv(4096)
        except OSError:
            data = b""
        client.close()
        assert data == b"" or json.loads(data.decode())["ok"] is False

    def test_the_socket_file_is_not_world_accessible(self, server):
        _, _, path = server
        assert oct(os.stat(path).st_mode)[-3:] == "600"


class TestSiblingSideValidation:
    """The sibling parses bytes Flask chose, so the reply is untrusted input."""

    def reply_with(self, payload):
        class FakeSock:
            def settimeout(self, _):
                pass

            def sendall(self, _):
                pass

            def recv(self, _):
                data, self.done = (b"" if getattr(self, "done", False) else payload), True
                return data

            def close(self):
                pass

        return lambda: FakeSock()

    def test_a_password_outside_the_allowlist_is_rejected(self):
        payload = json.dumps({"ok": True, "username": "j", "password": "a\nb"}).encode() + b"\n"
        with pytest.raises(CS.CredentialError, match="allowlist"):
            CS.fetch_credential(self.reply_with(payload), 1)

    def test_an_overlong_password_is_rejected(self):
        payload = json.dumps(
            {"ok": True, "username": "j", "password": "x" * 500}).encode() + b"\n"
        with pytest.raises(CS.CredentialError):
            CS.fetch_credential(self.reply_with(payload), 1)

    def test_a_malformed_reply_is_rejected(self):
        for payload in (b"{]\n", json.dumps({"ok": True}).encode() + b"\n",
                        json.dumps({"ok": True, "username": 1, "password": 2}).encode() + b"\n"):
            with pytest.raises(CS.CredentialError):
                CS.fetch_credential(self.reply_with(payload), 1)


# --- WS-2: the store is bounded by its TTL, not only by use ----------------

class TestStoreIsBounded:
    """`purge_expired` and `discard` existed with no caller anywhere in
    nethub/ -- the TTL was enforced *only* inside `release`, so an approval
    whose job never ran left a plaintext AAA password in the worker until a
    restart.
    """

    def test_hold_sweeps_an_expired_entry_for_another_job(self, store, clock):
        store.hold(1, "jsmith", PASSWORD, approved_by=7)
        clock["t"] = NOW + timedelta(hours=2)
        store.hold(2, "other", PASSWORD, approved_by=8)
        # Job 1 is gone without anyone ever having fetched it.
        assert len(store) == 1
        with pytest.raises(CS.CredentialError, match="no credential held"):
            store.release(1, 7)

    def test_release_sweeps_other_jobs(self, store, clock):
        store.hold(1, "jsmith", PASSWORD, approved_by=7)
        clock["t"] = NOW + timedelta(hours=2)
        store.hold(2, "other", PASSWORD, approved_by=8)
        clock["t"] = NOW + timedelta(hours=2, minutes=1)
        store.release(2, 8)
        assert len(store) == 0

    def test_an_expired_target_still_says_expired(self, store, clock):
        """The sweep must not eat the target before its own check runs.

        Sweeping first degrades "expired; the phase needs re-approval" to
        "no credential held", which is the answer a never-approved job gets --
        a worse diagnosis for whoever reads failure_stage. This is why
        `purge_expired` is called *after* the pop in `release`.
        """
        store.hold(1, "jsmith", PASSWORD, approved_by=7)
        clock["t"] = NOW + timedelta(hours=2)
        with pytest.raises(CS.CredentialError, match="expired"):
            store.release(1, 7)

    def test_discard_removes_without_releasing(self, store):
        store.hold(1, "jsmith", PASSWORD, approved_by=7)
        store.discard(1)
        assert len(store) == 0
        with pytest.raises(CS.CredentialError, match="no credential held"):
            store.release(1, 7)


class TestRequestValidation:
    def test_a_bool_job_id_is_refused(self, store):
        """bool is a subclass of int and hash(True) == hash(1), so
        {"job_id": true} used to release the credential held under key 1 --
        and `db.session.get(UpgradePhaseJob, True)` would have bound to 1 too.
        """
        store.hold(1, "jsmith", PASSWORD, approved_by=7)
        reply = json.loads(
            CS.handle_request(b'{"job_id": true}', store, lambda job_id: 7)
        )
        assert reply["ok"] is False
        # And it did not consume the real entry.
        assert len(store) == 1
        assert store.release(1, 7) == ("jsmith", PASSWORD)

    def test_a_real_int_job_id_still_works(self, store):
        """The guard must not have broken the only message type there is."""
        store.hold(1, "jsmith", PASSWORD, approved_by=7)
        reply = json.loads(
            CS.handle_request(b'{"job_id": 1}', store, lambda job_id: 7)
        )
        assert reply["ok"] is True
        assert reply["password"] == PASSWORD


class TestReprsHideTheCredential:
    def test_held_repr(self):
        from datetime import datetime, timezone
        held = CS._Held("jsmith", PASSWORD, 7, datetime.now(timezone.utc))
        assert PASSWORD not in repr(held)
        assert "jsmith" in repr(held)

    def test_phase_context_repr(self):
        from nethub.devices.phases import PhaseContext
        ctx = PhaseContext(device_username="jsmith", device_password=PASSWORD,
                           search_dir="/srv/images")
        assert PASSWORD not in repr(ctx)
        assert "jsmith" in repr(ctx)

    def test_pull_target_repr(self):
        from nethub.devices.transfer import PullTarget
        target = PullTarget("dist.example.net", "jsmith", PASSWORD)
        assert PASSWORD not in repr(target)
        assert "dist.example.net" in repr(target)
