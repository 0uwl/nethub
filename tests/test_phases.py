"""Checks for nethub.devices.phases -- the mapping, the rows, and the loop.

No device: PhaseContext.connect is injectable, so every phase runs against a
fake connection. What is under test is what ends up in the database and what
never does.
"""

from datetime import datetime, timedelta, timezone

import pytest

from nethub.devices import connection, facts, install, phases, transfer
from nethub.extensions import db
from nethub.models import (
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
            image_transport_used="push_scp", request_document="{}",
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


def job_for(run_id, phase="stage", **kw):
    job = UpgradePhaseJob(run_id=run_id, phase=phase, attempt=1, status="running", **kw)
    db.session.add(job)
    db.session.commit()
    return job


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
            assert phases.execute_phase(job, ctx_returning(None)) == "failed"

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
            host.run.cancel_requested_at = NOW  # cancel arrives during sw01
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
