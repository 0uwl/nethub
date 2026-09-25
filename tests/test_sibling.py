"""Checks for the dispatcher: the claim, the sweep, and the run state machine.

No device -- the phase runners are injected, and the sealed credentials are
real ones made with this module's own key pair. What is under test is §7.3's
tables: who writes which edge, and what the queue does under contention.
"""

from datetime import datetime, timedelta, timezone

import pytest
from nacl.public import PrivateKey

from nethub import sealed_credentials as SC
from nethub import sibling as S
from nethub import upgrades
from nethub.devices import connection, install, phases, transfer
from nethub.extensions import db
from nethub.models import (
    STATE_BEFORE,
    DeviceHostKey,
    HostKeyScan,
    UpgradeHostPhaseResult,
    UpgradePhaseJob,
    UpgradeRun,
    UpgradeRunHost,
    User,
)

NOW = datetime(2026, 9, 9, tzinfo=timezone.utc)
DIGEST = "a" * 128


#: The sibling's key pair for these tests.
KEY = PrivateKey.generate()
PASSWORD = "s3cret"


def sealed_for(job, *, password=PASSWORD, username="jsmith", approved_by=None,
               expires_at=None, key=KEY):
    """What Flask would have sealed into this job's row."""
    return SC.seal(
        key.public_key, job_id=job.id,
        approved_by=approved_by if approved_by is not None else S.Sibling._supplier(job),
        username=username, password=password,
        expires_at=expires_at or NOW + timedelta(hours=1),
    )


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
    kw.setdefault("private_key", KEY)
    kw.setdefault("search_dir", "/images")
    kw.setdefault("now", lambda: NOW)
    return S.Sibling(**kw)



def at_phase(job):
    """Move fresh hosts to where a run reaching `job`'s phase has them: a phase
    runs only on hosts whose cursor sits just before it (PLAN.md WS-8)."""
    for host in job.run.hosts:
        if host.state == "pending":
            host.state = STATE_BEFORE[job.phase]

def queue(run_id, phase="precheck", sealed=True, **kw):
    """A queued job carrying a sealed credential, as submit/approve leave it.
    `sealed=False` for a job nothing was sealed for (a crash-orphaned verify)."""
    kw.setdefault("attempt", 1)
    job = UpgradePhaseJob(run_id=run_id, phase=phase, status="queued",
                          created_at=NOW, **kw)
    db.session.add(job)
    db.session.flush()
    at_phase(job)
    if sealed:
        job.sealed_credential = sealed_for(job)
    db.session.commit()
    return job


def pretend_claimed(job, runner_id):
    """Put a job in the state a claim leaves it: running, and no ciphertext
    (the table's CHECK constraint refuses a running job that still has one)."""
    job.status, job.runner_instance_id, job.sealed_credential = "running", runner_id, None


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
            pretend_claimed(job, "a-dead-instance")
            db.session.commit()

            assert make_sibling().sweep() == 1
            assert db.session.get(UpgradePhaseJob, job.id).status == "abandoned"
            assert db.session.get(UpgradeRun, run).state == "failed"

    def test_our_own_running_rows_are_left_alone(self, app, run):
        with app.app_context():
            worker = make_sibling()
            job = queue(run)
            pretend_claimed(job, worker.runner_instance_id)
            db.session.commit()
            assert worker.sweep() == 0
            assert db.session.get(UpgradePhaseJob, job.id).status == "running"

    def test_abandoned_stays_distinct_from_cancelled(self, app, run):
        with app.app_context():
            job = queue(run)
            pretend_claimed(job, "gone")
            db.session.commit()
            make_sibling().sweep()
            assert db.session.get(UpgradePhaseJob, job.id).status == "abandoned"


class TestCredentialFailure:
    def test_no_credential_fails_the_phase_at_stage_credential(self, app, run):
        with app.app_context():
            job = queue(run, sealed=False)
            assert make_sibling().run_once() == "failed"
            job = db.session.get(UpgradePhaseJob, job.id)
            assert job.failure_stage == "credential"
            assert job.error_summary == "no credential was sealed for this execution"
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

    def test_activate_runs_verify_straight_after_with_no_gate(self, app, run, monkeypatch):
        """§8.1 gives verify no gate -- it is read-only and runs on completion,
        in the same pass as activate rather than back through the queue."""
        with app.app_context():
            queue(run, "activate")
            succeed(monkeypatch, "activate")
            succeed(monkeypatch, "verify")
            assert make_sibling().run_once() == "succeeded"
            row = db.session.get(UpgradeRun, run)
            assert (row.state, row.awaiting_phase) == ("awaiting_approval", "cleanup")
            verify = UpgradePhaseJob.query.filter_by(phase="verify").one()
            assert verify.status == "succeeded"
            assert verify.approved_by is None, "nobody approved verify"

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


class TestPerHostContinuation:
    """PLAN.md WS-8: one host failing a phase does not fail the run, and the
    hosts that failed can be retried."""

    @staticmethod
    def second_host(run_id, state="pending"):
        """sw02 beside the fixture's sw01, pinned like it."""
        run = db.session.get(UpgradeRun, run_id)
        db.session.add(UpgradeRunHost(
            run_id=run_id, hostname="sw02", ansible_host="192.0.2.11",
            filename="img.bin", sha512=DIGEST, version="17.12.06", file_size=1000,
            state=state))
        db.session.add(DeviceHostKey(
            ansible_host="192.0.2.11", key_type="ssh-rsa", fingerprint_sha256="SHA256:y",
            confirmed_by=run.submitted_by, confirmed_at=NOW))
        db.session.commit()

    @staticmethod
    def hosts(run_id):
        return {h.hostname: h.state for h in db.session.get(UpgradeRun, run_id).hosts}

    @staticmethod
    def runner(monkeypatch, phase, fail=(), exc=None, seen=None):
        """`phase` passes on every host but those in `fail`."""
        def run(conn, host, ctx):
            if seen is not None:
                seen.append(host.hostname)
            if host.hostname in fail:
                raise exc or transfer.TransferError("copy failed", status="not_copied")
            return phases.HostOutcome(host.hostname, "ok")
        monkeypatch.setitem(phases.PHASE_RUNNERS, phase, run)

    def test_a_partial_stage_parks_at_the_reload_gate_with_the_survivors(
            self, app, run, monkeypatch):
        with app.app_context():
            self.second_host(run)
            job = queue(run, "stage")
            self.runner(monkeypatch, "stage", fail={"sw02"})
            assert make_sibling().run_once() == "partial"
            row = db.session.get(UpgradeRun, run)
            assert (row.state, row.awaiting_phase) == ("awaiting_approval", "activate")
            assert self.hosts(run) == {"sw01": "staged", "sw02": "failed"}
            job = db.session.get(UpgradePhaseJob, job.id)
            assert (job.status, job.failure_stage) == ("partial", "transfer")

    def test_activate_then_runs_only_on_the_hosts_that_staged(self, app, run, monkeypatch):
        with app.app_context():
            self.second_host(run, state="failed")
            db.session.get(UpgradeRun, run).hosts[1].last_phase = "stage"
            db.session.commit()
            seen = []
            queue(run, "activate")
            self.runner(monkeypatch, "activate", seen=seen)
            self.runner(monkeypatch, "verify")
            assert make_sibling().run_once() == "succeeded"
            assert seen == ["sw01"]
            assert self.hosts(run) == {"sw01": "verified", "sw02": "failed"}

    def test_a_partial_activate_verifies_only_the_hosts_it_activated(
            self, app, run, monkeypatch):
        with app.app_context():
            self.second_host(run)
            queue(run, "activate")
            self.runner(monkeypatch, "activate", fail={"sw02"},
                        exc=install.ReloadTimeout("never came back", status="reload_timeout"))
            seen = []
            self.runner(monkeypatch, "verify", seen=seen)
            make_sibling().run_once()
            assert seen == ["sw01"]
            row = db.session.get(UpgradeRun, run)
            assert (row.state, row.awaiting_phase) == ("awaiting_approval", "cleanup")

    def test_a_phase_every_host_fails_still_fails_the_run(self, app, run, monkeypatch):
        with app.app_context():
            self.second_host(run)
            queue(run, "stage")
            self.runner(monkeypatch, "stage", fail={"sw01", "sw02"})
            assert make_sibling().run_once() == "failed"
            assert db.session.get(UpgradeRun, run).state == "failed"

    def retry_stage(self, run_id):
        """What `upgrades.retry` leaves: the run off its gate, a retry job queued."""
        row = db.session.get(UpgradeRun, run_id)
        row.state, row.awaiting_phase = "running", None
        return queue(run_id, "stage", attempt=2, is_retry=True,
                     approved_by=row.submitted_by, approved_at=NOW)

    def partial_stage(self, run_id, monkeypatch):
        self.second_host(run_id)
        queue(run_id, "stage")
        self.runner(monkeypatch, "stage", fail={"sw02"})
        make_sibling().run_once()

    def test_a_retry_runs_only_on_the_failed_hosts_and_they_rejoin(
            self, app, run, monkeypatch):
        with app.app_context():
            self.partial_stage(run, monkeypatch)
            job = self.retry_stage(run)
            seen = []
            self.runner(monkeypatch, "stage", seen=seen)
            assert make_sibling().run_once() == "succeeded"
            assert seen == ["sw02"]
            assert self.hosts(run) == {"sw01": "staged", "sw02": "staged"}
            row = db.session.get(UpgradeRun, run)
            assert (row.state, row.awaiting_phase) == ("awaiting_approval", "activate")
            # Both attempts are on record for sw02.
            assert [(r.attempt, r.status) for r in UpgradeHostPhaseResult.query.filter_by(
                hostname="sw02").order_by(UpgradeHostPhaseResult.attempt)] == [
                (1, "not_copied"), (2, "ok")]
            assert db.session.get(UpgradePhaseJob, job.id).attempt == 2

    def test_a_retry_that_fails_again_returns_to_the_gate_and_can_be_retried(
            self, app, run, monkeypatch):
        with app.app_context():
            self.partial_stage(run, monkeypatch)
            self.retry_stage(run)
            assert make_sibling().run_once() == "failed"
            row = db.session.get(UpgradeRun, run)
            assert (row.state, row.awaiting_phase) == ("awaiting_approval", "activate")
            assert self.hosts(run) == {"sw01": "staged", "sw02": "failed"}
            assert upgrades.retryable_phases(row) == [("stage", ["sw02"])]

    def test_a_retried_activate_is_verified_under_a_new_attempt(
            self, app, run, monkeypatch):
        with app.app_context():
            self.second_host(run)
            queue(run, "activate")
            self.runner(monkeypatch, "activate", fail={"sw02"},
                        exc=install.ReloadTimeout("never came back", status="reload_timeout"))
            self.runner(monkeypatch, "verify")
            make_sibling().run_once()
            row = db.session.get(UpgradeRun, run)
            row.state, row.awaiting_phase = "running", None
            queue(run, "activate", attempt=2, is_retry=True,
                  approved_by=row.submitted_by, approved_at=NOW)
            seen = []
            self.runner(monkeypatch, "activate")
            self.runner(monkeypatch, "verify", seen=seen)
            assert make_sibling().run_once() == "succeeded"
            assert seen == ["sw02"], "sw01 was already verified"
            verify = UpgradePhaseJob.query.filter_by(phase="verify").order_by(
                UpgradePhaseJob.attempt).all()
            assert [(j.attempt, j.status) for j in verify] == [(1, "succeeded"),
                                                               (2, "succeeded")]
            assert self.hosts(run) == {"sw01": "verified", "sw02": "verified"}

    def test_a_retried_activate_that_activates_nothing_queues_no_verify(
            self, app, run, monkeypatch):
        with app.app_context():
            self.second_host(run, state="failed")
            hosts = db.session.get(UpgradeRun, run).hosts
            hosts[0].state = "verified"
            hosts[1].last_phase = "activate"
            db.session.commit()
            queue(run, "activate", attempt=2, is_retry=True,
                  approved_by=db.session.get(UpgradeRun, run).submitted_by)
            self.runner(monkeypatch, "activate", fail={"sw02"})
            assert make_sibling().run_once() == "failed"
            assert UpgradePhaseJob.query.filter_by(phase="verify").count() == 0
            row = db.session.get(UpgradeRun, run)
            assert (row.state, row.awaiting_phase) == ("awaiting_approval", "cleanup")

    def test_a_reapproved_abandoned_phase_skips_the_hosts_it_finished(
            self, app, run, monkeypatch):
        """Before WS-8 every host that had not failed ran again: a second
        `install add` on a switch that had already reloaded."""
        with app.app_context():
            self.second_host(run, state="staged")
            db.session.get(UpgradeRun, run).hosts[0].state = "activated"
            db.session.commit()
            seen = []
            queue(run, "activate", attempt=2)
            self.runner(monkeypatch, "activate", seen=seen)
            self.runner(monkeypatch, "verify")
            make_sibling().run_once()
            assert seen == ["sw02"]

    def test_an_abandoned_precheck_retry_returns_to_the_stage_gate(self, app, run):
        with app.app_context():
            self.second_host(run, state="failed")
            hosts = db.session.get(UpgradeRun, run).hosts
            hosts[0].state, hosts[0].last_phase = "precheck_ok", "precheck"
            hosts[1].last_phase = "precheck"
            db.session.commit()
            job = queue(run, "precheck", attempt=2, is_retry=True,
                        approved_by=db.session.get(UpgradeRun, run).submitted_by)
            # The dead sibling had reset sw02 before it died.
            hosts[1].state = "pending"
            pretend_claimed(job, "dead-runner")
            db.session.commit()
            make_sibling().sweep()
            row = db.session.get(UpgradeRun, run)
            assert (row.state, row.awaiting_phase) == ("awaiting_approval", "stage")
            assert self.hosts(run) == {"sw01": "precheck_ok", "sw02": "failed"}
            assert upgrades.retryable_phases(row) == [("precheck", ["sw02"])]


class TestCredentialFailureStopsTheWave:
    """A refused password is refused on every host, and each attempt counts
    toward the AAA server's lockout (PLAN.md "Found while working", WS-8)."""

    def three_hosts(self, run_id):
        run = db.session.get(UpgradeRun, run_id)
        for name, address in (("sw02", "192.0.2.11"), ("sw03", "192.0.2.12")):
            db.session.add(UpgradeRunHost(
                run_id=run_id, hostname=name, ansible_host=address,
                filename="img.bin", sha512=DIGEST, version="17.12.06", file_size=1000))
            db.session.add(DeviceHostKey(
                ansible_host=address, key_type="ssh-rsa", fingerprint_sha256="SHA256:y",
                confirmed_by=run.submitted_by, confirmed_at=NOW))
        db.session.commit()

    def connect_refusing(self, monkeypatch, refused, logins):
        def connect(host, username, password):
            logins.append(host.hostname)
            if host.hostname in refused:
                raise connection.AuthenticationError("authentication failed")
            return _FakeConn()
        monkeypatch.setattr(phases, "default_connect", connect)

    def test_the_first_refusal_stops_the_phase(self, app, run, monkeypatch):
        with app.app_context():
            self.three_hosts(run)
            job = queue(run, "stage")
            logins = []
            self.connect_refusing(monkeypatch, {"sw01", "sw02", "sw03"}, logins)
            succeed(monkeypatch, "stage")
            assert make_sibling().run_once() == "failed"
            assert logins == ["sw01"], "one refused login, not one per host"
            job = db.session.get(UpgradePhaseJob, job.id)
            assert job.failure_stage == "credential"
            rows = {r.hostname: (r.status, r.failure_stage)
                    for r in UpgradeHostPhaseResult.query}
            assert rows["sw02"] == rows["sw03"] == ("not_attempted", "credential")
            assert "stopped after the credential was refused on sw01" in (
                db.session.get(UpgradeRun, run).hosts[2].error_summary)

    def test_hosts_before_the_refusal_keep_their_progress(self, app, run, monkeypatch):
        with app.app_context():
            self.three_hosts(run)
            queue(run, "stage")
            logins = []
            self.connect_refusing(monkeypatch, {"sw02"}, logins)
            succeed(monkeypatch, "stage")
            assert make_sibling().run_once() == "partial"
            assert logins == ["sw01", "sw02"]
            states = {h.hostname: (h.state, h.last_phase)
                      for h in db.session.get(UpgradeRun, run).hosts}
            assert states == {"sw01": ("staged", "stage"), "sw02": ("failed", "stage"),
                              "sw03": ("failed", "stage")}
            row = db.session.get(UpgradeRun, run)
            assert upgrades.retryable_phases(row) == [("stage", ["sw02", "sw03"])]

    def test_other_failures_do_not_stop_the_phase(self, app, run, monkeypatch):
        with app.app_context():
            self.three_hosts(run)
            queue(run, "stage")
            logins = []
            self.connect_refusing(monkeypatch, set(), logins)
            monkeypatch.setitem(
                phases.PHASE_RUNNERS, "stage",
                lambda conn, host, ctx: (_ for _ in ()).throw(
                    transfer.TransferError("copy failed", status="not_copied")))
            make_sibling().run_once()
            assert logins == ["sw01", "sw02", "sw03"]


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


class TestSealedCredential:
    """PLAN.md WS-7: the credential travels sealed in the job row, and the
    claim is what takes it off the row."""

    @staticmethod
    def capture(monkeypatch, phase="precheck"):
        seen = {}

        def runner(conn, host, ctx):
            seen["username"], seen["password"] = ctx.device_username, ctx.device_password
            return phases.HostOutcome(host.hostname, "ok")
        monkeypatch.setitem(phases.PHASE_RUNNERS, phase, runner)
        return seen

    def test_the_phase_gets_the_credential_that_was_sealed(self, app, run, monkeypatch):
        seen = self.capture(monkeypatch)
        with app.app_context():
            queue(run)
            assert make_sibling().run_once() == "succeeded"
        assert seen == {"username": "jsmith", "password": PASSWORD}

    def test_the_claim_takes_the_ciphertext_off_the_row(self, app, run):
        with app.app_context():
            job = queue(run)
            assert db.session.get(UpgradePhaseJob, job.id).sealed_credential is not None
            worker = make_sibling()
            assert worker._claim(job.id)[0] is True
            row = db.session.get(UpgradePhaseJob, job.id)
            db.session.refresh(row)
            assert (row.status, row.sealed_credential) == ("running", None)

    def test_a_second_claim_gets_nothing(self, app, run):
        with app.app_context():
            job = queue(run)
            first, second = make_sibling(), make_sibling()
            assert first._claim(job.id)[0] is True
            assert second._claim(job.id) == (False, None)

    def test_a_restart_of_the_web_process_costs_nothing(self, app, run, monkeypatch):
        """The WS-7 done-when. Under the socket the credential lived in the web
        process's memory, so a restart between approval and claim failed the
        phase. Now it is in the row; a fresh app over the same database is
        all the "restart" there is, and the phase still gets its credential."""
        from nethub import create_app
        seen = self.capture(monkeypatch)
        with app.app_context():
            job = queue(run)
            job_id = job.id
        restarted = create_app()
        with restarted.app_context():
            assert make_sibling().run_once() == "succeeded"
            assert db.session.get(UpgradePhaseJob, job_id).status == "succeeded"
        assert seen["password"] == PASSWORD

    @pytest.mark.parametrize("tamper, summary", [
        (lambda job: b"\x00" * len(sealed_for(job)), "could not be opened"),
        (lambda job: sealed_for(job, key=PrivateKey.generate()), "could not be opened"),
        (lambda job: sealed_for(job, approved_by=9999), "different identity"),
        (lambda job: sealed_for(job, expires_at=NOW - timedelta(seconds=1)), "expired"),
    ], ids=["garbage", "sealed-to-another-key", "wrong-approver", "expired"])
    def test_a_credential_that_does_not_open_or_check_fails_the_phase(
            self, app, run, monkeypatch, tamper, summary):
        ran = []
        monkeypatch.setitem(phases.PHASE_RUNNERS, "precheck",
                            lambda conn, host, ctx: ran.append(1))
        with app.app_context():
            job = queue(run, sealed=False)
            job.sealed_credential = tamper(job)
            db.session.commit()
            assert make_sibling().run_once() == "failed"
            row = db.session.get(UpgradePhaseJob, job.id)
            assert row.failure_stage == "credential"
            assert summary in row.error_summary
            assert row.sealed_credential is None
            assert db.session.get(UpgradeRun, run).state == "failed"
        assert ran == [], "no device was touched"

    def test_a_credential_sealed_for_another_job_is_refused(self, app, run, monkeypatch):
        """Copying one job's ciphertext onto another row is exactly what
        binding the job id stops."""
        monkeypatch.setitem(phases.PHASE_RUNNERS, "precheck",
                            lambda conn, host, ctx: pytest.fail("device touched"))
        with app.app_context():
            other = queue(run, phase="stage")
            job = queue(run, sealed=False)
            job.sealed_credential = other.sealed_credential
            job.created_at = NOW - timedelta(minutes=1)  # first in the queue
            db.session.commit()
            assert make_sibling().run_once() == "failed"
            row = db.session.get(UpgradePhaseJob, job.id)
            assert "different execution" in row.error_summary

    def test_a_job_that_expires_unclaimed_says_its_credential_was_discarded(self, app, run):
        """Maintainer decision (PLAN.md WS-7): an expired job that carried a
        credential records failure_stage='credential', so the run page says
        why, and the ciphertext goes in the same update."""
        with app.app_context():
            job = queue(run, deadline_at=NOW - timedelta(hours=1))
            assert make_sibling().run_once() == "expired"
            row = db.session.get(UpgradePhaseJob, job.id)
            assert (row.status, row.failure_stage) == ("expired", "credential")
            assert "discarded unused" in row.error_summary
            assert row.sealed_credential is None

    def test_a_cancelled_job_drops_its_ciphertext_without_a_failure_stage(self, app, run):
        with app.app_context():
            job = queue(run)
            db.session.get(UpgradeRun, run).cancel_requested_at = NOW
            db.session.commit()
            assert make_sibling().run_once() == "cancelled"
            row = db.session.get(UpgradePhaseJob, job.id)
            assert (row.sealed_credential, row.failure_stage) == (None, None)

    def test_the_table_refuses_ciphertext_on_a_job_that_is_not_queued(self, app, run):
        from sqlalchemy.exc import IntegrityError
        with app.app_context():
            job = queue(run)
            job.status = "running"
            with pytest.raises(IntegrityError, match="ck_sealed_credential_only_while_queued"):
                db.session.commit()
            db.session.rollback()


class TestVerifyRunsOnActivatesCredential:
    """No approval ever holds a credential for verify: the sibling queues it
    itself when activate succeeds. Fetching one could only fail, which is
    how every app-driven run used to end -- failed right after the switch
    was upgraded. It runs on the credential activate's approval supplied."""

    def test_one_credential_covers_activate_and_verify(self, app, run, monkeypatch):
        seen, opened = {}, []
        real_open = S.open_sealed

        def counting_open(*args, **kwargs):
            opened.append(kwargs["job_id"])
            return real_open(*args, **kwargs)
        monkeypatch.setattr(S, "open_sealed", counting_open)

        def verify(conn, host, ctx):
            seen["password"] = ctx.device_password
            seen["ctx"] = ctx
            return phases.HostOutcome(host.hostname, "verified")

        with app.app_context():
            job_id = queue(run, "activate",
                           approved_by=db.session.get(UpgradeRun, run).submitted_by).id
            succeed(monkeypatch, "activate")
            monkeypatch.setitem(phases.PHASE_RUNNERS, "verify", verify)
            make_sibling().run_once()

        assert opened == [job_id], "one credential opened, activate's"
        assert seen["password"] == PASSWORD
        assert seen["ctx"].device_password == "", "cleared once verify ended"

    def test_a_failed_activate_queues_no_verify(self, app, run, monkeypatch):
        with app.app_context():
            queue(run, "activate")
            monkeypatch.setitem(
                phases.PHASE_RUNNERS, "activate",
                lambda conn, host, ctx: (_ for _ in ()).throw(
                    install.InstallError("install failed", status="install_failed")))
            assert make_sibling().run_once() == "failed"
            assert UpgradePhaseJob.query.filter_by(phase="verify").count() == 0
            assert db.session.get(UpgradeRun, run).state == "failed"

    def test_a_cancel_during_activate_stops_verify(self, app, run, monkeypatch):
        """Verify goes through the same pre-claim checks as a queued job."""
        ran = []

        def activate(conn, host, ctx):
            host.run.cancel_requested_at = NOW
            return phases.HostOutcome(host.hostname, "activated")

        with app.app_context():
            queue(run, "activate")
            monkeypatch.setitem(phases.PHASE_RUNNERS, "activate", activate)
            monkeypatch.setitem(phases.PHASE_RUNNERS, "verify",
                                lambda conn, host, ctx: ran.append(1))
            assert make_sibling().run_once() == "cancelled"
            assert ran == []
            verify = UpgradePhaseJob.query.filter_by(phase="verify").one()
            assert verify.status == "cancelled"
            assert db.session.get(UpgradeRun, run).state == "cancelled"

    def test_a_verify_left_queued_by_a_crash_fails_rather_than_hangs(
        self, app, run, monkeypatch
    ):
        """If the sibling dies between activate and verify, the restarted one
        finds a queued verify with no credential held for it. That fails
        with failure_stage='credential' -- visible, not stuck."""
        with app.app_context():
            queue(run, "verify", sealed=False)
            assert make_sibling().run_once() == "failed"
            job = UpgradePhaseJob.query.filter_by(phase="verify").one()
            assert job.failure_stage == "credential"


class TestAbandonedPhaseCanBeReApproved:
    """WS-3.1: the §7.3 retry was unreachable.

    `sweep()` used to call `_fail_run`, so the run went terminal and
    `approve()`'s first guard refused forever -- while `models.py` said
    `attempt` existed to permit the retry and `approve()` computed
    `1 + count(abandoned)`, an expression that had never returned anything
    but 1. The test above (`test_a_foreign_running_row_is_abandoned`) still
    asserts `failed`, and correctly: it queues a `precheck`, which nobody
    approves.
    """

    @pytest.fixture(autouse=True)
    def one_clock(self, monkeypatch):
        """The sweep sets `gate_expires_at` from the sibling's fixed NOW, and
        `approve()` refuses a gate past it by the wall clock. Put both on NOW."""
        from nethub import upgrades
        monkeypatch.setattr(upgrades, "_utcnow", lambda: NOW)

    def abandon(self, app, run_id, phase):
        job = queue(run_id, phase=phase)
        pretend_claimed(job, "a-dead-instance")
        db.session.commit()
        assert make_sibling().sweep() == 1
        return job

    def test_an_abandoned_stage_parks_at_its_gate(self, app, run):
        with app.app_context():
            job = self.abandon(app, run, "stage")
            assert db.session.get(UpgradePhaseJob, job.id).status == "abandoned"
            row = db.session.get(UpgradeRun, run)
            assert row.state == "awaiting_approval"
            assert row.awaiting_phase == "stage"
            assert row.finished_at is None, "a parked run has not finished"
            assert row.gate_expires_at is not None

    def test_approve_then_creates_attempt_two(self, app, run, make_user):
        """The walk-through the suite never did: sweep() -> approve()."""
        from nethub import upgrades
        from nethub.models import User

        username, _ = make_user(username="zoe", password="zoe-long-enough-pw")
        with app.app_context():
            user = User.query.filter_by(username=username).first()
            user.device_username = "zoe"
            db.session.commit()

            self.abandon(app, run, "activate")
            row = db.session.get(UpgradeRun, run)
            job = upgrades.approve(run=row, phase="activate", user=user,
                                   password=PASSWORD, public_key=KEY.public_key)
            assert job.attempt == 2, "1 + count(abandoned)"
            assert job.status == "queued"
            assert job.approved_by == user.id
            assert db.session.get(UpgradeRun, run).state == "running"

    def test_a_second_abandon_gives_attempt_three(self, app, run, make_user):
        from nethub import upgrades
        from nethub.models import User

        username, _ = make_user(username="yan", password="yan-long-enough-pw")
        with app.app_context():
            user = User.query.filter_by(username=username).first()
            user.device_username = "yan"
            db.session.commit()

            self.abandon(app, run, "activate")
            second = upgrades.approve(
                run=db.session.get(UpgradeRun, run), phase="activate", user=user,
                password=PASSWORD, public_key=KEY.public_key)
            pretend_claimed(second, "another-dead-one")
            db.session.commit()
            assert make_sibling().sweep() == 1

            third = upgrades.approve(
                run=db.session.get(UpgradeRun, run), phase="activate", user=user,
                password=PASSWORD, public_key=KEY.public_key)
            assert third.attempt == 3

    def test_an_abandoned_precheck_still_fails_the_run(self, app, run):
        """Parking a phase nobody can approve would be stuck, not failed.

        `precheck` has no gate (§8.1) and `verify` follows `activate` without
        one, so `approve()` refuses both as "not a phase anyone approves".
        Parking either would leave the run at `awaiting_approval` forever --
        worse than terminal, because it looks recoverable.
        """
        with app.app_context():
            self.abandon(app, run, "precheck")
            assert db.session.get(UpgradeRun, run).state == "failed"

    def test_an_abandoned_verify_still_fails_the_run(self, app, run):
        with app.app_context():
            self.abandon(app, run, "verify")
            assert db.session.get(UpgradeRun, run).state == "failed"


class TestSweepPredicate:
    def test_a_null_runner_id_is_swept(self, app, run):
        """`!=` is NULL, not true, for a NULL column -- so such a row was
        invisible to every sweep forever. Latent: `claim()` sets status and
        runner id in one UPDATE, so nothing produces this today.
        """
        with app.app_context():
            job = queue(run, phase="stage")
            pretend_claimed(job, None)
            db.session.commit()
            assert make_sibling().sweep() == 1
            assert db.session.get(UpgradePhaseJob, job.id).status == "abandoned"


class TestHostKeyScanDispatch:
    """WS-6.2b: a scan is dispatched like a phase job, but simpler -- no
    credential, no PhaseContext, no gate, no state machine beyond
    queued/running/succeeded/failed/abandoned.
    """

    @pytest.fixture
    def scan_user(self, app):
        with app.app_context():
            u = User(username="alice")
            u.set_password("hunter2")
            db.session.add(u)
            db.session.commit()
            yield u.id

    def queue_scan(self, requested_by, status="queued", **kw):
        scan = HostKeyScan(ansible_host="192.0.2.10", requested_by=requested_by,
                           status=status, created_at=NOW, **kw)
        db.session.add(scan)
        db.session.commit()
        return scan

    def test_next_queued_scan_is_fifo(self, app, scan_user):
        with app.app_context():
            older = self.queue_scan(scan_user)
            newer = self.queue_scan(scan_user)
            newer.created_at = NOW + timedelta(minutes=5)
            db.session.commit()
            assert make_sibling().next_queued_scan().id == older.id

    def test_only_one_sibling_wins_a_scan(self, app, scan_user):
        with app.app_context():
            scan = self.queue_scan(scan_user)
            first, second = make_sibling(), make_sibling()
            assert first.claim_scan(scan.id) is True
            assert second.claim_scan(scan.id) is False, "a read-then-write would double-claim"
            assert db.session.get(HostKeyScan, scan.id).runner_instance_id == \
                first.runner_instance_id

    def test_run_scan_once_returns_none_when_the_queue_is_empty(self, app):
        with app.app_context():
            assert make_sibling().run_scan_once() is None

    def test_run_scan_once_records_a_successful_scan(self, app, scan_user, monkeypatch):
        with app.app_context():
            scan_id = self.queue_scan(scan_user).id
        monkeypatch.setattr(
            connection, "scan_host_key",
            lambda host, **kw: connection.HostKey("ssh-rsa", "SHA256:real"))
        worker = make_sibling()
        with app.app_context():
            assert worker.run_scan_once() == "succeeded"
            row = db.session.get(HostKeyScan, scan_id)
            assert row.status == "succeeded"
            assert row.key_type == "ssh-rsa"
            assert row.fingerprint_sha256 == "SHA256:real"
            assert row.finished_at is not None

    def test_run_scan_once_records_a_failure_without_the_library_text(
        self, app, scan_user, monkeypatch
    ):
        with app.app_context():
            scan_id = self.queue_scan(scan_user).id

        def boom(host, **kw):
            raise connection.DeviceConnectionError(
                "could not reach 192.0.2.10:22: secret=hunter2",
                summary="could not reach 192.0.2.10:22",
            )
        monkeypatch.setattr(connection, "scan_host_key", boom)
        worker = make_sibling()
        with app.app_context():
            assert worker.run_scan_once() == "failed"
            row = db.session.get(HostKeyScan, scan_id)
            assert row.status == "failed"
            assert row.error_summary == "could not reach 192.0.2.10:22"
            assert "hunter2" not in row.error_summary

    def test_a_stale_running_scan_is_abandoned_by_sweep(self, app, scan_user):
        with app.app_context():
            scan = self.queue_scan(scan_user, status="running",
                                   runner_instance_id="a-dead-instance")
            make_sibling().sweep()
            row = db.session.get(HostKeyScan, scan.id)
            assert row.status == "abandoned"
            assert row.finished_at is not None

    def test_our_own_running_scan_is_left_alone_by_sweep(self, app, scan_user):
        with app.app_context():
            worker = make_sibling()
            scan = self.queue_scan(scan_user, status="running",
                                   runner_instance_id=worker.runner_instance_id)
            worker.sweep()
            assert db.session.get(HostKeyScan, scan.id).status == "running"


class TestUnexpectedErrorDoesNotStrandTheRow:
    """WS-1.3: an exception that escaped `run_once()` after `claim()` left the
    job `running` under this instance's own id, which `sweep()` never matches,
    so the run sat `running` until the sibling restarted."""

    def test_a_raising_phase_fails_the_job_and_the_run(self, app, run, monkeypatch):
        def boom(job, ctx, now):
            raise RuntimeError("password=s3cret leaked into a message")
        monkeypatch.setattr(phases, "execute_phase", boom)
        with app.app_context():
            job = queue(run)
            assert make_sibling().tick() is None
            row = db.session.get(UpgradePhaseJob, job.id)
            assert row.status == "failed"
            assert row.failure_stage == "internal", "our bug, not a device fault"
            assert row.finished_at is not None
            assert "s3cret" not in row.error_summary
            assert db.session.get(UpgradeRun, run).state == "failed"

    def test_a_raising_scan_is_failed_too(self, app, run, monkeypatch):
        def boom(host, **kw):
            raise RuntimeError("not a DeviceConnectionError")
        monkeypatch.setattr(connection, "scan_host_key", boom)
        with app.app_context():
            scan = HostKeyScan(ansible_host="192.0.2.10", status="queued",
                               requested_by=db.session.get(UpgradeRun, run).submitted_by,
                               created_at=NOW)
            db.session.add(scan)
            db.session.commit()
            assert make_sibling().tick() is None
            row = db.session.get(HostKeyScan, scan.id)
            assert row.status == "failed" and row.finished_at is not None

    def test_another_instances_running_row_is_not_touched(self, app, run):
        """That row may be live under a sibling we cannot see; it is the
        startup sweep's to judge, not ours."""
        with app.app_context():
            job = queue(run)
            pretend_claimed(job, "someone-else")
            db.session.commit()
            assert make_sibling().recover_own() == 0
            assert db.session.get(UpgradePhaseJob, job.id).status == "running"


class TestGateExpiry:
    """WS-1.4: `gate_expires_at` was written in five places and read in none,
    so a run parked at a gate stayed approvable forever."""

    def park(self, run_id, expires):
        row = db.session.get(UpgradeRun, run_id)
        row.state, row.awaiting_phase, row.gate_expires_at = (
            "awaiting_approval", "stage", expires)
        db.session.commit()

    def test_a_gate_past_its_ttl_expires_the_run(self, app, run):
        with app.app_context():
            self.park(run, NOW - timedelta(seconds=1))
            assert make_sibling().expire_gates() == 1
            row = db.session.get(UpgradeRun, run)
            assert row.state == "expired"
            assert row.awaiting_phase is None
            assert row.finished_at is not None

    def test_a_gate_inside_its_ttl_is_left_alone(self, app, run):
        with app.app_context():
            self.park(run, NOW + timedelta(hours=1))
            assert make_sibling().expire_gates() == 0
            assert db.session.get(UpgradeRun, run).state == "awaiting_approval"

    def test_a_run_elsewhere_is_never_expired_by_a_stale_column(self, app, run):
        """Only `awaiting_approval` has a gate to expire at."""
        with app.app_context():
            row = db.session.get(UpgradeRun, run)
            row.state, row.gate_expires_at = "running", NOW - timedelta(days=1)
            db.session.commit()
            assert make_sibling().expire_gates() == 0
            assert db.session.get(UpgradeRun, run).state == "running"

    def test_the_loop_runs_the_expiry(self, app, run):
        with app.app_context():
            self.park(run, NOW - timedelta(seconds=1))
            make_sibling().tick()
            assert db.session.get(UpgradeRun, run).state == "expired"

    def test_a_sweep_parked_gate_expires_on_the_same_ttl(self, app, run):
        """`_abandon_run` sets the TTL from the sibling's clock; a week on, the
        same sibling expires it."""
        with app.app_context():
            job = queue(run, phase="stage")
            pretend_claimed(job, "a-dead-instance")
            db.session.commit()
            make_sibling().sweep()
            assert db.session.get(UpgradeRun, run).state == "awaiting_approval"
            later = make_sibling(now=lambda: NOW + S.DEFAULT_GATE_TTL)
            assert later.expire_gates() == 1

    def test_an_approval_that_lands_first_is_not_overwritten(self, app, run, monkeypatch):
        """The expiry is conditional on the run still being at the gate."""
        with app.app_context():
            self.park(run, NOW - timedelta(seconds=1))
            real_update = db.session.query(UpgradeRun).__class__.update

            def approve_first(query, values, **kw):
                # Flask commits an approval between our read and our write.
                db.session.execute(
                    UpgradeRun.__table__.update()
                    .where(UpgradeRun.id == run).values(state="running"))
                return real_update(query, values, **kw)

            monkeypatch.setattr(db.session.query(UpgradeRun).__class__,
                                "update", approve_first)
            assert make_sibling().expire_gates() == 0
            monkeypatch.undo()
            db.session.expire_all()
            assert db.session.get(UpgradeRun, run).state == "running"


class TestNoSecretKey:
    """WS-3.2: the sibling signs nothing and must start without SECRET_KEY.

    Run in a subprocess: `config.py` validates the key at import, and in this
    process it has long since been imported with one set, so an in-process
    test would pass whether or not the sibling still imported it.
    """

    def run(self, code, tmp_path):
        import os
        import subprocess
        import sys
        env = {k: v for k, v in os.environ.items()
               if k not in ("SECRET_KEY", "CREDENTIALS_DIRECTORY",
                            "NETHUB_CREDENTIAL_KEY_FILE", "NETHUB_SEARCH_DIR")}
        env["DATABASE_PATH"] = str(tmp_path / "sibling.db")
        return subprocess.run([sys.executable, "-c", code], env=env, cwd=os.getcwd(),
                              capture_output=True, text=True, timeout=60, check=False)

    def test_the_database_app_loads_without_a_key(self, tmp_path):
        result = self.run(
            "from nethub.sibling import _database_app\n"
            "app = _database_app()\n"
            "print(app.config['SECRET_KEY'], app.config['SQLALCHEMY_DATABASE_URI'])\n",
            tmp_path)
        assert result.returncode == 0, result.stderr
        key, uri = result.stdout.split()
        assert key == "None"
        assert uri.endswith("sibling.db")

    @staticmethod
    def sibling_env(tmp_path, private_key):
        """What the sibling unit sets: no SECRET_KEY, a search dir, and the
        private key as a file (the public key comes from conftest's env)."""
        import os
        key_file = tmp_path / "credential_private_key"
        key_file.write_text(SC.encode_key(private_key))
        env = {k: v for k, v in os.environ.items()
               if k not in ("SECRET_KEY", "CREDENTIALS_DIRECTORY")}
        env.update(DATABASE_PATH=str(tmp_path / "sibling.db"),
                   NETHUB_SEARCH_DIR=str(tmp_path),
                   NETHUB_CREDENTIAL_KEY_FILE=str(key_file))
        return env

    def test_the_sibling_starts_without_a_key(self, tmp_path, credential_private_key):
        """PLAN.md WS-3's done-when: `python -m nethub.sibling` starts. It
        gets as far as its sweep and start-up log line, then is stopped."""
        import os
        import subprocess
        import sys
        env = self.sibling_env(tmp_path, credential_private_key)
        # The web tier creates the schema; the sibling waits for it.
        subprocess.run(
            [sys.executable, "-c", "from nethub import create_app; create_app()"],
            env={**env, "SECRET_KEY": "k" * 64}, cwd=os.getcwd(),
            capture_output=True, timeout=60, check=True)
        proc = subprocess.Popen([sys.executable, "-m", "nethub.sibling"], env=env,
                                cwd=os.getcwd(), stderr=subprocess.PIPE, text=True)
        try:
            line = proc.stderr.readline()
            assert "started; swept 0 abandoned row(s)" in line, (
                line + proc.stderr.read() if proc.poll() is not None else line)
        finally:
            proc.kill()
            proc.wait(timeout=10)

    def test_the_sibling_refuses_a_private_key_that_does_not_match(self, tmp_path):
        """Otherwise every job would fail one at a time with a credential
        error that says nothing about keys."""
        import os
        import subprocess
        import sys
        env = self.sibling_env(tmp_path, PrivateKey.generate())
        result = subprocess.run([sys.executable, "-m", "nethub.sibling"], env=env,
                                cwd=os.getcwd(), capture_output=True, text=True,
                                timeout=60, check=False)
        assert result.returncode != 0
        assert "does not match NETHUB_CREDENTIAL_PUBLIC_KEY" in result.stderr

    def test_the_sibling_refuses_to_start_with_no_private_key(self, tmp_path):
        import os
        import subprocess
        import sys
        env = self.sibling_env(tmp_path, PrivateKey.generate())
        del env["NETHUB_CREDENTIAL_KEY_FILE"]
        result = subprocess.run([sys.executable, "-m", "nethub.sibling"], env=env,
                                cwd=os.getcwd(), capture_output=True, text=True,
                                timeout=60, check=False)
        assert result.returncode != 0
        assert "no private key" in result.stderr

    def test_the_web_tier_still_refuses_to_start_without_one(self, tmp_path):
        result = self.run("from nethub import create_app; create_app()", tmp_path)
        assert result.returncode != 0
        assert "SECRET_KEY" in result.stderr
