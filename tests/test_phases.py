"""Checks for nethub.devices.phases -- the mapping, the rows, and the loop.

No device: PhaseContext.connect is injectable, so every phase runs against a
fake connection. What is under test is what ends up in the database and what
never does.
"""

import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from nethub.devices import connection, facts, install, phases, transfer
from nethub.extensions import db
from nethub.models import (
    STATE_BEFORE,
    DeviceHostKey,
    UpgradeHostPhaseResult,
    UpgradePhaseJob,
    UpgradeRun,
    UpgradeRunHost,
    User,
)

NOW = datetime(2026, 9, 9, tzinfo=timezone.utc)
DIGEST = "a" * 128
PASSWORD = "sup3rs3cret"


@pytest.fixture
def run(app):
    with app.app_context():
        user = User(username="alice")
        user.set_password("hunter2")
        db.session.add(user)
        db.session.commit()
        run = UpgradeRun(
            submitted_by=user.id, device_username_used="jsmith",
            request_document="{}",
            request_sha512=DIGEST, state="running",
        )
        db.session.add(run)
        db.session.commit()
        for name, addr in (("sw01", "192.0.2.10"), ("sw02", "192.0.2.11")):
            db.session.add(UpgradeRunHost(
                run_id=run.id, hostname=name, ansible_host=addr,
                filename="img.bin", sha512=DIGEST, version="17.12.06",
                file_size=1000,
            ))
            db.session.add(DeviceHostKey(
                ansible_host=addr, key_type="ssh-rsa",
                fingerprint_sha256=f"SHA256:{name}", confirmed_by=user.id,
                confirmed_at=NOW,
            ))
        db.session.commit()
        yield run.id



def at_phase(job):
    """Move fresh hosts to where a run reaching `job`'s phase has them: a phase
    runs only on hosts whose cursor sits just before it (PLAN.md WS-8)."""
    for host in job.run.hosts:
        if host.state == "pending":
            host.state = STATE_BEFORE[job.phase]

def job_for(run_id, phase="stage", **kw):
    job = UpgradePhaseJob(run_id=run_id, phase=phase, attempt=1, status="running", **kw)
    db.session.add(job)
    db.session.flush()
    at_phase(job)
    db.session.commit()
    return job


def cancel_as_flask(run_id):
    """Request a cancel the way Flask does: another connection's write.

    Phase runners run in worker threads and must not touch `db.session`, so a
    test that cancels mid-host does it from outside the session, which is
    also what production looks like -- the web process is another writer.
    """
    conn = sqlite3.connect(os.environ["DATABASE_PATH"], timeout=5)
    try:
        conn.execute("UPDATE upgrade_runs SET cancel_requested_at = ? WHERE id = ?",
                     ("2026-09-09 00:00:00.000000", run_id))
        conn.commit()
    finally:
        conn.close()


class FakeConn:
    def __init__(self):
        self.disconnected = False

    def disconnect(self):
        self.disconnected = True


def ctx_returning(outcome_or_exc, **kw):
    """A context whose phase runner is replaced with a canned result."""
    conns = []

    def connect(host, username, password):
        assert password == PASSWORD
        conn = FakeConn()
        conns.append(conn)
        return conn

    ctx = phases.PhaseContext(
        device_username="jsmith", device_password=PASSWORD,
        search_dir="/images", connect=connect, **kw,
    )
    ctx.conns = conns
    return ctx


class TestFailureStageMapping:
    @pytest.mark.parametrize("exc,expected", [
        (connection.HostKeyError("changed"), "hostkey"),
        (phases.UnconfirmedHost("unconfirmed"), "hostkey"),
        (connection.AuthenticationError("rejected"), "credential"),
        (connection.DeviceConnectionError("unreachable"), "connect"),
        (transfer.VerificationError("bad digest", status="not_verified"), "checksum"),
        (transfer.ScpRestoreError("not restored", status="scp_not_restored"), "transfer"),
        (transfer.TransferError("no space", status="no_space"), "transfer"),
        (install.ReloadTimeout("never came back", status="reload_timeout"), "reload"),
        (install.PostCheckError("wrong version", status="wrong_version"), "postcheck"),
        (install.InstallError("not level 15", status="privilege"), "privilege"),
        (install.InstallError("install failed", status="install_failed"), "install"),
        (facts.FactsError("unparseable"), "precheck"),
    ])
    def test_every_device_exception_has_a_stage(self, exc, expected):
        assert phases.failure_stage_for(exc) == expected

    def test_every_mapped_stage_is_in_the_schema_vocabulary(self):
        from nethub.models import PHASE_FAILURE_STAGES
        for exc in (connection.HostKeyError(""), transfer.TransferError("", status="x"),
                    install.InstallError("", status="y"), facts.FactsError(""),
                    RuntimeError("")):
            assert phases.failure_stage_for(exc) in PHASE_FAILURE_STAGES


class TestErrorSummaryNeverLeaksTheCredential:
    def test_our_own_exceptions_keep_their_message(self):
        exc = transfer.VerificationError("digest mismatch", status="not_verified")
        assert phases._summarise(exc) == "digest mismatch"

    def test_a_foreign_exception_contributes_only_its_type(self):
        """error_summary is kept for a year; a library string is a durable leak."""
        exc = RuntimeError(f"paramiko sent password={PASSWORD} to the device")
        summary = phases._summarise(exc)
        assert summary == "unexpected RuntimeError"
        assert PASSWORD not in summary

    def test_summaries_are_bounded_and_single_line(self):
        exc = facts.FactsError("line one\nline two " + "x" * 900)
        summary = phases._summarise(exc)
        assert len(summary) <= 500 and "\n" not in summary


class TestPinnedKey:
    def test_a_confirmed_row_is_used(self, app, run):
        with app.app_context():
            host = UpgradeRunHost.query.filter_by(hostname="sw01").one()
            assert phases.pinned_key(host).fingerprint_sha256 == "SHA256:sw01"

    def test_an_unknown_address_is_refused(self, app, run):
        with app.app_context():
            host = UpgradeRunHost.query.filter_by(hostname="sw01").one()
            host.ansible_host = "198.51.100.7"
            with pytest.raises(phases.UnconfirmedHost, match="no confirmed host key"):
                phases.pinned_key(host)

    def test_a_seen_but_unconfirmed_key_is_refused(self, app, run):
        """Seeing a key is not accepting it (§4.3)."""
        with app.app_context():
            DeviceHostKey.query.filter_by(ansible_host="192.0.2.10").update(
                {"confirmed_at": None, "confirmed_by": None})
            db.session.commit()
            host = UpgradeRunHost.query.filter_by(hostname="sw01").one()
            with pytest.raises(phases.UnconfirmedHost, match="nobody has"):
                phases.pinned_key(host)


class TestExecutePhase:
    def test_a_successful_phase_writes_a_row_per_host(self, app, run, monkeypatch):
        with app.app_context():
            job = job_for(run)
            monkeypatch.setitem(
                phases.PHASE_RUNNERS, "stage",
                lambda conn, host, ctx: phases.HostOutcome(
                    host.hostname, "image_copied", scp_restore_confirmed=True),
            )
            assert phases.execute_phase(job, ctx_returning(None)) == "succeeded"

            rows = UpgradeHostPhaseResult.query.all()
            assert {r.hostname for r in rows} == {"sw01", "sw02"}
            assert all(r.scp_restore_confirmed is True for r in rows)
            assert all(h.state == "staged" for h in UpgradeRunHost.query.all())

    def test_one_failing_host_does_not_stop_the_others(self, app, run, monkeypatch):
        def runner(conn, host, ctx):
            if host.hostname == "sw01":
                raise transfer.VerificationError("digest mismatch", status="not_verified")
            return phases.HostOutcome(host.hostname, "image_copied")

        with app.app_context():
            job = job_for(run)
            monkeypatch.setitem(phases.PHASE_RUNNERS, "stage", runner)
            assert phases.execute_phase(job, ctx_returning(None)) == "partial"

            assert UpgradeHostPhaseResult.query.count() == 2, "both hosts recorded"
            sw01 = UpgradeRunHost.query.filter_by(hostname="sw01").one()
            sw02 = UpgradeRunHost.query.filter_by(hostname="sw02").one()
            assert (sw01.state, sw02.state) == ("failed", "staged")
            assert job.failure_stage == "checksum"
            assert job.error_summary == "digest mismatch"

    def test_the_connection_is_closed_even_when_the_phase_raises(
        self, app, run, monkeypatch
    ):
        with app.app_context():
            job = job_for(run)
            monkeypatch.setitem(
                phases.PHASE_RUNNERS, "stage",
                lambda conn, host, ctx: (_ for _ in ()).throw(OSError("boom")))
            ctx = ctx_returning(None)
            phases.execute_phase(job, ctx)
            assert all(c.disconnected for c in ctx.conns)

    def test_cancel_is_honoured_between_hosts(self, app, run, monkeypatch):
        seen = []

        def runner(conn, host, ctx):
            seen.append(host.hostname)
            cancel_as_flask(run)  # cancel arrives during sw01
            return phases.HostOutcome(host.hostname, "image_copied")

        with app.app_context():
            job = job_for(run)
            monkeypatch.setitem(phases.PHASE_RUNNERS, "stage", runner)
            assert phases.execute_phase(job, ctx_returning(None)) == "cancelled"
            assert seen == ["sw01"], "sw02 was never started"
            assert UpgradeHostPhaseResult.query.count() == 1

    def test_the_deadline_stops_the_run_between_hosts(self, app, run, monkeypatch):
        with app.app_context():
            job = job_for(run, deadline_at=NOW + timedelta(minutes=5))
            clock = {"t": NOW}

            def runner(conn, host, ctx):
                clock["t"] = NOW + timedelta(hours=1)  # sw01 overruns the deadline
                return phases.HostOutcome(host.hostname, "image_copied")

            monkeypatch.setitem(phases.PHASE_RUNNERS, "stage", runner)
            assert phases.execute_phase(
                job, ctx_returning(None), now=lambda: clock["t"]) == "timed_out"
            assert UpgradeHostPhaseResult.query.count() == 1, "sw02 never started"

    def test_a_host_that_already_failed_is_not_retried_in_a_later_phase(
        self, app, run, monkeypatch
    ):
        with app.app_context():
            UpgradeRunHost.query.filter_by(hostname="sw01").update({"state": "failed"})
            db.session.commit()
            job = job_for(run, phase="activate")
            seen = []
            monkeypatch.setitem(
                phases.PHASE_RUNNERS, "activate",
                lambda conn, host, ctx: seen.append(host.hostname) or
                phases.HostOutcome(host.hostname, "activated"))
            phases.execute_phase(job, ctx_returning(None))
            assert seen == ["sw02"]

    def test_the_credential_reaches_no_row(self, app, run, monkeypatch):
        with app.app_context():
            job = job_for(run)
            monkeypatch.setitem(
                phases.PHASE_RUNNERS, "stage",
                lambda conn, host, ctx: (_ for _ in ()).throw(
                    RuntimeError(f"auth failed with {PASSWORD}")))
            phases.execute_phase(job, ctx_returning(None))
            blob = " ".join(
                str(v) for r in UpgradeHostPhaseResult.query.all()
                for v in (r.status, r.failure_stage, r.error_summary)
            ) + " " + str(job.error_summary)
            assert PASSWORD not in blob


# --------------------------------------------------------------------------
# Hosts in parallel (PLAN.md WS-9)
# --------------------------------------------------------------------------

@pytest.fixture
def fleet(app):
    """A run of eight confirmed hosts."""
    with app.app_context():
        user = User(username="alice")
        user.set_password("hunter2")
        db.session.add(user)
        db.session.commit()
        run = UpgradeRun(
            submitted_by=user.id, device_username_used="jsmith",
            request_document="{}", request_sha512=DIGEST, state="running",
        )
        db.session.add(run)
        db.session.commit()
        for n in range(1, 9):
            addr = f"192.0.2.{10 + n}"
            db.session.add(UpgradeRunHost(
                run_id=run.id, hostname=f"sw{n:02d}", ansible_host=addr,
                filename="img.bin", sha512=DIGEST, version="17.12.06",
                file_size=1000,
            ))
            db.session.add(DeviceHostKey(
                ansible_host=addr, key_type="ssh-rsa",
                fingerprint_sha256=f"SHA256:sw{n:02d}", confirmed_by=user.id,
                confirmed_at=NOW,
            ))
        db.session.commit()
        yield run.id


class Tracker:
    """A phase runner that records how many hosts it is running at once."""

    def __init__(self, seconds=0.0):
        self.seconds = seconds
        self.lock = threading.Lock()
        self.active = 0
        self.most = 0
        self.seen = []

    def __call__(self, conn, host, ctx):
        with self.lock:
            self.active += 1
            self.most = max(self.most, self.active)
            self.seen.append(host.hostname)
        time.sleep(self.seconds)
        with self.lock:
            self.active -= 1
        return phases.HostOutcome(host.hostname, "ok")


class TestParallelHosts:
    HOST = 0.3  # seconds one fake host takes

    def test_eight_hosts_on_four_workers_take_two_host_durations(
        self, app, fleet, monkeypatch
    ):
        tracker = Tracker(self.HOST)
        with app.app_context():
            job = job_for(fleet)
            monkeypatch.setitem(phases.PHASE_RUNNERS, "stage", tracker)
            started = time.monotonic()
            status = phases.execute_phase(job, ctx_returning(None), concurrency=4)
            elapsed = time.monotonic() - started

            assert status == "succeeded"
            assert UpgradeHostPhaseResult.query.count() == 8
            assert all(h.state == "staged" for h in UpgradeRunHost.query.all())
        assert tracker.most == 4, "never more than four at once, and four were used"
        assert 2 * self.HOST <= elapsed < 4 * self.HOST, elapsed

    def test_activate_runs_one_host_at_a_time_whatever_the_setting(
        self, app, fleet, monkeypatch
    ):
        tracker = Tracker(0.02)
        with app.app_context():
            job = job_for(fleet, phase="activate")
            monkeypatch.setitem(phases.PHASE_RUNNERS, "activate", tracker)
            assert phases.execute_phase(job, ctx_returning(None), concurrency=4) == "succeeded"
        assert tracker.most == 1
        assert len(tracker.seen) == 8

    def test_cancel_stops_new_hosts_and_lets_running_ones_finish(
        self, app, fleet, monkeypatch
    ):
        cancelled = threading.Event()
        seen = []

        def runner(conn, host, ctx):
            seen.append(host.hostname)
            if host.hostname == "sw01":
                cancel_as_flask(fleet)
                cancelled.set()
            else:
                # sw02 is already running when the cancel lands, and finishes.
                assert cancelled.wait(5)
            return phases.HostOutcome(host.hostname, "ok")

        with app.app_context():
            job = job_for(fleet)
            monkeypatch.setitem(phases.PHASE_RUNNERS, "stage", runner)
            assert phases.execute_phase(job, ctx_returning(None), concurrency=2) == "cancelled"
            assert sorted(seen) == ["sw01", "sw02"], "nothing started after the cancel"
            rows = UpgradeHostPhaseResult.query.all()
            assert sorted(r.hostname for r in rows) == ["sw01", "sw02"]
            assert UpgradeRunHost.query.filter_by(state="precheck_ok").count() == 6

    def test_the_heartbeat_advances_while_a_host_is_still_running(
        self, app, fleet, monkeypatch
    ):
        """The heartbeat used to move only between hosts, so a healthy
        15-minute stage looked dead for fifteen minutes."""
        release = threading.Event()
        clock = {"t": NOW}
        beats = []

        def now():
            clock["t"] += timedelta(seconds=1)
            return clock["t"]

        def runner(conn, host, ctx):
            assert release.wait(5), "the host was never released"
            return phases.HostOutcome(host.hostname, "ok")

        with app.app_context():
            UpgradeRunHost.query.filter(UpgradeRunHost.hostname != "sw01").update(
                {"state": "failed"})
            db.session.commit()
            job = job_for(fleet)
            job_id = job.id

            def on_tick():
                beats.append(db.session.query(UpgradePhaseJob.heartbeat_at)
                              .filter_by(id=job_id).scalar())
                if len(beats) == 3:
                    release.set()

            monkeypatch.setitem(phases.PHASE_RUNNERS, "stage", runner)
            assert phases.execute_phase(job, ctx_returning(None), now=now,
                                        tick_interval=0.01, on_tick=on_tick) == "succeeded"
        assert len(beats) >= 3
        assert beats[0] < beats[1] < beats[2], "written, committed, and moving"

    def test_workers_never_touch_the_database(self, app, fleet, monkeypatch):
        """Only the thread holding the session writes rows; a worker has no app
        context, and an ORM object read after a commit reloads itself."""
        from sqlalchemy import event

        threads = set()

        def seen(*_args):
            threads.add(threading.current_thread().name)

        # The real `default_connect`, so the pin lookup is under test too.
        monkeypatch.setattr(connection, "connect", lambda *args: FakeConn())
        ctx = phases.PhaseContext(device_username="jsmith", device_password=PASSWORD,
                                  search_dir="/images")
        with app.app_context():
            job = job_for(fleet)
            monkeypatch.setitem(phases.PHASE_RUNNERS, "stage", Tracker())
            event.listen(db.engine, "before_cursor_execute", seen)
            try:
                assert phases.execute_phase(job, ctx, concurrency=4) == "succeeded"
            finally:
                event.remove(db.engine, "before_cursor_execute", seen)
        assert threads == {threading.current_thread().name}

    def test_an_unconfirmed_host_fails_on_the_main_thread_and_the_rest_run(
        self, app, fleet, monkeypatch
    ):
        tracker = Tracker()
        with app.app_context():
            DeviceHostKey.query.filter_by(ansible_host="192.0.2.13").update(
                {"confirmed_at": None, "confirmed_by": None})
            db.session.commit()
            job = job_for(fleet)
            monkeypatch.setitem(phases.PHASE_RUNNERS, "stage", tracker)
            assert phases.execute_phase(job, ctx_returning(None), concurrency=4) == "partial"
            sw03 = UpgradeRunHost.query.filter_by(hostname="sw03").one()
            assert sw03.state == "failed"
            row = UpgradeHostPhaseResult.query.filter_by(hostname="sw03").one()
            assert row.failure_stage == "hostkey"
        assert "sw03" not in tracker.seen and len(tracker.seen) == 7

    def test_default_connect_uses_the_pin_the_main_thread_looked_up(
        self, app, fleet, monkeypatch
    ):
        """Activate reconnects from its worker after the reload, where a
        database lookup is not possible."""
        calls = []
        monkeypatch.setattr(connection, "connect",
                            lambda address, user, password, pin: calls.append((address, pin)))
        with app.app_context():
            host = UpgradeRunHost.query.filter_by(hostname="sw01").one()
            target = phases.HostTarget.of(host)
        phases.default_connect(target, "jsmith", PASSWORD)
        assert calls == [("192.0.2.11", connection.HostKey("ssh-rsa", "SHA256:sw01"))]


class TestLoginGate:
    """A wrong password is tried once, not once per host running at once."""

    def ctx(self, connect):
        return phases.PhaseContext(device_username="jsmith", device_password=PASSWORD,
                                   search_dir="/images", connect=connect)

    def test_a_refused_password_is_tried_on_one_host(self, app, fleet, monkeypatch):
        logins = []

        def connect(host, username, password):
            logins.append(host.hostname)
            time.sleep(0.05)  # long enough for the others to reach their login
            raise connection.AuthenticationError("authentication failed")

        with app.app_context():
            job = job_for(fleet)
            monkeypatch.setitem(phases.PHASE_RUNNERS, "stage", Tracker())
            assert phases.execute_phase(job, self.ctx(connect), concurrency=4) == "failed"
            assert len(logins) == 1, logins
            rows = UpgradeHostPhaseResult.query.all()
            assert len(rows) == 8
            assert all(r.failure_stage == "credential" for r in rows)
            skipped = [r for r in rows if r.status == "not_attempted"]
            assert len(skipped) == 7
            assert all(logins[0] in r.error_summary for r in skipped)
            assert UpgradeRunHost.query.filter_by(state="failed").count() == 8

    def test_an_unreachable_first_host_does_not_hold_the_others(
        self, app, fleet, monkeypatch
    ):
        """Nothing was learned about the password, so the next host tries."""
        tracker = Tracker()

        def connect(host, username, password):
            if host.hostname == "sw01":
                raise connection.DeviceConnectionError("unreachable")
            return FakeConn()

        with app.app_context():
            job = job_for(fleet)
            monkeypatch.setitem(phases.PHASE_RUNNERS, "stage", tracker)
            assert phases.execute_phase(job, self.ctx(connect), concurrency=4) == "partial"
            assert UpgradeRunHost.query.filter_by(state="staged").count() == 7
        assert len(tracker.seen) == 7

    def test_hosts_log_in_together_once_the_password_has_worked(self):
        gate = phases.LoginGate()
        assert gate.enter() == "probe"
        waiting = []
        thread = threading.Thread(target=lambda: waiting.append(gate.enter()))
        thread.start()
        time.sleep(0.05)
        assert waiting == [], "held while the first login is in progress"
        gate.settle(True, "sw01")
        thread.join(5)
        assert waiting == ["go"]
        assert gate.enter() == "go"

    def test_a_refusal_after_the_gate_opened_stops_later_logins(self):
        gate = phases.LoginGate()
        assert gate.enter() == "probe"
        gate.settle(True, "sw01")
        gate.refuse("sw05")
        assert gate.enter() == "stop"
        assert gate.refused_on == "sw05"
