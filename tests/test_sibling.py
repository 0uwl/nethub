"""Checks for the dispatcher: the claim, the sweep, and the run state machine.

No device and no socket -- `connect_socket` and the phase runners are both
injected. What is under test is §7.3's tables: who writes which edge, and what
the queue does under contention.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from nethub import sibling as S
from nethub.credential_socket import CredentialError
from nethub.devices import phases, transfer
from nethub.extensions import db
from nethub.models import (
    DeviceHostKey,
    UpgradePhaseJob,
    UpgradeRun,
    UpgradeRunHost,
    User,
)

NOW = datetime(2026, 9, 9, tzinfo=timezone.utc)
DIGEST = "a" * 128


def credential_socket(username="jsmith", password="s3cret", ok=True):
    class FakeSock:
        def settimeout(self, _):
            pass

        def sendall(self, _):
            pass

        def recv(self, _):
            if getattr(self, "done", False):
                return b""
            self.done = True
            body = ({"ok": True, "username": username, "password": password}
                    if ok else {"ok": False, "error": "no credential held"})
            return json.dumps(body).encode() + b"\n"

        def close(self):
            pass

    return lambda: FakeSock()


@pytest.fixture
def run(app):
    with app.app_context():
        user = User(username="alice")
        user.set_password("hunter2")
        db.session.add(user)
        db.session.commit()
        run = UpgradeRun(
            submitted_by=user.id, device_username_used="jsmith",
            image_transport_used="push_scp", request_document="{}",
            request_sha512=DIGEST, state="pre_checking",
        )
        db.session.add(run)
        db.session.commit()
        db.session.add(UpgradeRunHost(
            run_id=run.id, hostname="sw01", ansible_host="192.0.2.10",
            filename="img.bin", sha512=DIGEST, version="17.12.06", file_size=1000))
        db.session.add(DeviceHostKey(
            ansible_host="192.0.2.10", key_type="ssh-rsa",
            fingerprint_sha256="SHA256:x", confirmed_by=user.id, confirmed_at=NOW))
        db.session.commit()
        yield run.id


def make_sibling(**kw):
    kw.setdefault("connect_socket", credential_socket())
    kw.setdefault("search_dir", "/images")
    kw.setdefault("now", lambda: NOW)
    return S.Sibling(**kw)


def queue(run_id, phase="precheck", **kw):
    job = UpgradePhaseJob(run_id=run_id, phase=phase, attempt=1, status="queued",
                          created_at=NOW, **kw)
    db.session.add(job)
    db.session.commit()
    return job


def succeed(monkeypatch, phase):
    monkeypatch.setitem(
        phases.PHASE_RUNNERS, phase,
        lambda conn, host, ctx: phases.HostOutcome(host.hostname, "ok"))


@pytest.fixture(autouse=True)
def no_real_connections(monkeypatch):
    monkeypatch.setattr(phases, "default_connect",
                        lambda host, username, password: _FakeConn())


class _FakeConn:
    def disconnect(self):
        pass


class TestRunnerInstanceId:
    def test_each_start_gets_its_own_uuid(self):
        assert make_sibling().runner_instance_id != make_sibling().runner_instance_id

    def test_it_is_a_uuid_and_not_a_pid(self):
        """A PID is reused across restarts and meaningless across namespaces."""
        import uuid
        uuid.UUID(make_sibling().runner_instance_id)


class TestClaim:
    def test_only_one_sibling_wins(self, app, run):
        with app.app_context():
            job = queue(run)
            first, second = make_sibling(), make_sibling()
            assert first.claim(job.id) is True
            assert second.claim(job.id) is False, "a read-then-write would double-claim"
            assert db.session.get(UpgradePhaseJob, job.id).runner_instance_id == \
                first.runner_instance_id

    def test_the_queue_is_fifo_by_created_at(self, app, run):
        with app.app_context():
            older = queue(run, "precheck", )
            newer = queue(run, "stage")
            newer.created_at = NOW + timedelta(minutes=5)
            db.session.commit()
            assert make_sibling().next_queued().id == older.id


class TestSweep:
    def test_a_foreign_running_row_is_abandoned(self, app, run):
        with app.app_context():
            job = queue(run)
            job.status, job.runner_instance_id = "running", "a-dead-instance"
            db.session.commit()

            assert make_sibling().sweep() == 1
            assert db.session.get(UpgradePhaseJob, job.id).status == "abandoned"
            assert db.session.get(UpgradeRun, run).state == "failed"

    def test_our_own_running_rows_are_left_alone(self, app, run):
        with app.app_context():
            worker = make_sibling()
            job = queue(run)
            job.status, job.runner_instance_id = "running", worker.runner_instance_id
            db.session.commit()
            assert worker.sweep() == 0
            assert db.session.get(UpgradePhaseJob, job.id).status == "running"

    def test_abandoned_stays_distinct_from_cancelled(self, app, run):
        with app.app_context():
            job = queue(run)
            job.status, job.runner_instance_id = "running", "gone"
            db.session.commit()
            make_sibling().sweep()
            assert db.session.get(UpgradePhaseJob, job.id).status == "abandoned"


class TestCredentialFailure:
    def test_no_credential_fails_the_phase_at_stage_credential(self, app, run):
        with app.app_context():
            job = queue(run)
            worker = make_sibling(connect_socket=credential_socket(ok=False))
            assert worker.run_once() == "failed"
            job = db.session.get(UpgradePhaseJob, job.id)
            assert job.failure_stage == "credential"
            assert db.session.get(UpgradeRun, run).state == "failed"


class TestRunStateMachine:
    def test_precheck_success_parks_at_the_stage_gate(self, app, run, monkeypatch):
        with app.app_context():
            queue(run)
            succeed(monkeypatch, "precheck")
            assert make_sibling().run_once() == "succeeded"
            row = db.session.get(UpgradeRun, run)
            assert (row.state, row.awaiting_phase) == ("awaiting_approval", "stage")
            assert row.gate_expires_at is not None, "a gate nobody returns to must expire"

    def test_stage_success_parks_at_the_reload_gate(self, app, run, monkeypatch):
        with app.app_context():
            queue(run, "stage")
            succeed(monkeypatch, "stage")
            make_sibling().run_once()
            row = db.session.get(UpgradeRun, run)
            assert (row.state, row.awaiting_phase) == ("awaiting_approval", "activate")

    def test_activate_queues_verify_with_no_gate(self, app, run, monkeypatch):
        """§8.1 gives verify no gate -- it is read-only and runs on completion."""
        with app.app_context():
            queue(run, "activate")
            succeed(monkeypatch, "activate")
            make_sibling().run_once()
            row = db.session.get(UpgradeRun, run)
            assert (row.state, row.awaiting_phase) == ("running", None)
            assert UpgradePhaseJob.query.filter_by(phase="verify",
                                                   status="queued").count() == 1

    def test_verify_parks_at_the_optional_cleanup_gate(self, app, run, monkeypatch):
        with app.app_context():
            queue(run, "verify")
            succeed(monkeypatch, "verify")
            make_sibling().run_once()
            row = db.session.get(UpgradeRun, run)
            assert (row.state, row.awaiting_phase) == ("awaiting_approval", "cleanup")

    def test_cleanup_completes_the_run(self, app, run, monkeypatch):
        with app.app_context():
            queue(run, "cleanup")
            succeed(monkeypatch, "cleanup")
            make_sibling().run_once()
            row = db.session.get(UpgradeRun, run)
            assert (row.state, row.awaiting_phase) == ("completed", None)
            assert row.finished_at is not None

    def test_a_failed_phase_fails_the_run_and_clears_the_gate(self, app, run, monkeypatch):
        with app.app_context():
            queue(run, "stage")
            monkeypatch.setitem(
                phases.PHASE_RUNNERS, "stage",
                lambda conn, host, ctx: (_ for _ in ()).throw(
                    transfer.VerificationError("bad digest", status="not_verified")))
            assert make_sibling().run_once() == "failed"
            row = db.session.get(UpgradeRun, run)
            assert row.state == "failed"
            assert row.awaiting_phase is None and row.gate_expires_at is None


class TestQueueGuards:
    def test_a_cancel_requested_before_dispatch_is_honoured(self, app, run):
        with app.app_context():
            job = queue(run)
            db.session.get(UpgradeRun, run).cancel_requested_at = NOW
            db.session.commit()
            assert make_sibling().run_once() == "cancelled"
            assert db.session.get(UpgradePhaseJob, job.id).status == "cancelled"
            assert db.session.get(UpgradeRun, run).state == "cancelled"

    def test_a_job_past_its_deadline_expires_instead_of_running(self, app, run):
        with app.app_context():
            job = queue(run, deadline_at=NOW - timedelta(hours=1))
            assert make_sibling().run_once() == "expired"
            assert db.session.get(UpgradePhaseJob, job.id).status == "expired"

    def test_an_empty_queue_is_not_an_error(self, app, run):
        with app.app_context():
            assert make_sibling().run_once() is None


class TestEndToEndOverTheSocket:
    """The two halves joined: Flask holds, the sibling fetches, a phase runs."""

    def test_a_phase_runs_with_a_credential_fetched_over_a_real_socket(
        self, app, run, monkeypatch, tmp_path
    ):
        import os
        import socket
        import threading

        from nethub import credential_socket as CS

        path = str(tmp_path / "cred.sock")
        listening = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listening.bind(path)
        os.chmod(path, 0o600)
        listening.listen(4)
        store = CS.CredentialStore()
        stop = threading.Event()

        with app.app_context():
            job = queue(run, "stage")
            approver = db.session.get(UpgradeRun, run).submitted_by
            job.approved_by = approver
            db.session.commit()
            job_id, run_id = job.id, run

            def verify_running(jid):
                # Its own app context: this runs on the serving thread, and
                # Flask-SQLAlchemy's session is thread-local. Production does
                # the same in nethub/__init__.py's _serve_credential_socket --
                # without it every request fails with a generic refusal.
                with app.app_context():
                    fresh = db.session.get(UpgradePhaseJob, jid)
                    if fresh is None or fresh.status != "running":
                        raise CredentialError("no running execution with that id")
                    return fresh.approved_by

            thread = threading.Thread(
                target=CS.serve, args=(listening, store, verify_running),
                kwargs={"stop": stop}, daemon=True)
            thread.start()
            try:
                store.hold(job_id, "jsmith", "d3vice-pass", approved_by=approver)
                seen = {}

                def runner(conn, host, ctx):
                    seen["password"] = ctx.device_password
                    seen["username"] = ctx.device_username
                    return phases.HostOutcome(host.hostname, "image_copied")

                monkeypatch.setitem(phases.PHASE_RUNNERS, "stage", runner)
                worker = S.Sibling(connect_socket=CS.connect_to(path),
                                   search_dir="/images", now=lambda: NOW)
                assert worker.run_once() == "succeeded"

                assert seen == {"password": "d3vice-pass", "username": "jsmith"}
                assert len(store) == 0, "released once, and not held afterwards"
                assert db.session.get(UpgradeRun, run_id).awaiting_phase == "activate"
            finally:
                stop.set()
                thread.join(timeout=3)
                listening.close()

    def test_a_credential_approved_by_someone_else_is_refused(
        self, app, run, monkeypatch, tmp_path
    ):
        """Keying by job and cross-checking the approver is what stops one
        person's password serving another person's approved execution."""
        import socket
        import threading

        from nethub import credential_socket as CS

        path = str(tmp_path / "cred.sock")
        listening = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listening.bind(path)
        listening.listen(4)
        store = CS.CredentialStore()
        stop = threading.Event()

        with app.app_context():
            job = queue(run, "stage")
            job.approved_by = db.session.get(UpgradeRun, run).submitted_by
            db.session.commit()
            job_id = job.id

            def verify_running(jid):
                with app.app_context():
                    return db.session.get(UpgradePhaseJob, jid).approved_by

            thread = threading.Thread(
                target=CS.serve, args=(listening, store, verify_running),
                kwargs={"stop": stop}, daemon=True)
            thread.start()
            try:
                # Held under a different approver than the job records.
                store.hold(job_id, "mallory", "other-pass", approved_by=9999)
                worker = S.Sibling(connect_socket=CS.connect_to(path),
                                   search_dir="/images", now=lambda: NOW)
                assert worker.run_once() == "failed"
                assert db.session.get(UpgradePhaseJob, job_id).failure_stage == "credential"
            finally:
                stop.set()
                thread.join(timeout=3)
                listening.close()
