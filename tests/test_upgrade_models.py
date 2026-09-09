"""Schema checks for the upgrade tables.

These test constraints rather than columns. Every one of them is something
design doc §5/§7.3 says the database must refuse, and every one is silently
absent if the declaration is wrong -- SQLite enforces no foreign key unless
asked, and a trigger that was never created raises nothing.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from nethub.extensions import db
from nethub.models import (
    TERMINAL_JOB_STATUSES,
    DeviceHostKey,
    UpgradeHostPhaseResult,
    UpgradePhaseJob,
    UpgradeRun,
    UpgradeRunHost,
    User,
    is_terminal,
)

NOW = datetime(2026, 9, 9, tzinfo=timezone.utc)
DIGEST = "a" * 128


def make_run(user_id, **kw):
    kw.setdefault("device_username_used", "jsmith")
    kw.setdefault("image_transport_used", "push_scp")
    kw.setdefault("request_document", '{"hosts": []}')
    kw.setdefault("request_sha512", DIGEST)
    run = UpgradeRun(submitted_by=user_id, **kw)
    db.session.add(run)
    db.session.commit()
    return run


def make_host(run, hostname="sw01"):
    host = UpgradeRunHost(
        run_id=run.id, hostname=hostname, ansible_host="192.0.2.10",
        filename="img.bin", sha512=DIGEST, version="17.12.06", file_size=471177027,
    )
    db.session.add(host)
    db.session.commit()
    return host


def make_job(run, phase="stage", attempt=1, **kw):
    job = UpgradePhaseJob(run_id=run.id, phase=phase, attempt=attempt, **kw)
    db.session.add(job)
    db.session.commit()
    return job


@pytest.fixture
def user(app):
    with app.app_context():
        u = User(username="alice")
        u.set_password("hunter2")
        db.session.add(u)
        db.session.commit()
        yield u.id


class TestForeignKeys:
    def test_sqlite_enforcement_is_actually_on(self, app, user):
        """Without PRAGMA foreign_keys=ON every check below silently passes."""
        with app.app_context():
            enabled = db.session.execute(db.text("PRAGMA foreign_keys")).scalar()
            assert enabled == 1

    def test_a_result_needs_a_phase_job_that_exists(self, app, user):
        """Nothing may record a host result for an execution nobody approved."""
        with app.app_context():
            run = make_run(user)
            make_host(run)
            db.session.add(UpgradeHostPhaseResult(
                run_id=run.id, hostname="sw01", phase="stage", attempt=1,
                status="image_copied",
            ))
            with pytest.raises(IntegrityError):
                db.session.commit()

    def test_a_result_needs_a_host_in_the_run(self, app, user):
        with app.app_context():
            run = make_run(user)
            make_host(run, "sw01")
            make_job(run)
            db.session.add(UpgradeHostPhaseResult(
                run_id=run.id, hostname="never-targeted", phase="stage", attempt=1,
                status="image_copied",
            ))
            with pytest.raises(IntegrityError):
                db.session.commit()

    def test_a_result_with_both_parents_is_accepted(self, app, user):
        with app.app_context():
            run = make_run(user)
            make_host(run)
            make_job(run)
            db.session.add(UpgradeHostPhaseResult(
                run_id=run.id, hostname="sw01", phase="stage", attempt=1,
                status="image_copied", scp_restore_confirmed=True,
            ))
            db.session.commit()
            assert UpgradeHostPhaseResult.query.count() == 1


class TestApprovalMutex:
    def test_two_approvals_of_the_same_phase_collide(self, app, user):
        """Two admins clicking "approve: reload" must not queue two reloads."""
        with app.app_context():
            run = make_run(user)
            make_job(run, phase="activate", attempt=1)
            with pytest.raises(IntegrityError):
                make_job(run, phase="activate", attempt=1)

    def test_a_retry_is_a_new_attempt_and_is_allowed(self, app, user):
        """§7.3 grants an abandoned phase a fresh, separately-approved retry."""
        with app.app_context():
            run = make_run(user)
            make_job(run, phase="activate", attempt=1, status="abandoned")
            make_job(run, phase="activate", attempt=2)
            assert UpgradePhaseJob.query.filter_by(phase="activate").count() == 2

    def test_a_host_cannot_be_named_twice_in_one_run(self, app, user):
        with app.app_context():
            run = make_run(user)
            make_host(run, "sw01")
            with pytest.raises(IntegrityError):
                make_host(run, "sw01")


class TestTerminalRowsAreImmutable:
    @pytest.mark.parametrize("status", sorted(TERMINAL_JOB_STATUSES))
    def test_a_terminal_job_cannot_be_updated(self, app, user, status):
        with app.app_context():
            run = make_run(user)
            job = make_job(run, status=status)
            job.error_summary = "rewritten after the fact"
            with pytest.raises(IntegrityError, match="terminal"):
                db.session.commit()

    def test_a_running_job_can_still_be_updated(self, app, user):
        with app.app_context():
            run = make_run(user)
            job = make_job(run, status="running")
            job.heartbeat_at = NOW
            job.status = "succeeded"
            db.session.commit()
            assert db.session.get(UpgradePhaseJob, job.id).status == "succeeded"

    def test_is_terminal_agrees_with_the_trigger(self):
        assert is_terminal("succeeded") and is_terminal("abandoned")
        assert not is_terminal("queued") and not is_terminal("running")


class TestVocabularies:
    @pytest.mark.parametrize("bad", ["", "reboot", "STAGE"])
    def test_an_unknown_phase_is_refused(self, app, user, bad):
        with app.app_context():
            run = make_run(user)
            with pytest.raises((IntegrityError, LookupError, ValueError)):
                make_job(run, phase=bad)

    def test_an_unknown_run_state_is_refused(self, app, user):
        with app.app_context():
            with pytest.raises((IntegrityError, LookupError, ValueError)):
                make_run(user, state="halfway")

    def test_an_unknown_transport_is_refused(self, app, user):
        with app.app_context():
            with pytest.raises((IntegrityError, LookupError, ValueError)):
                make_run(user, image_transport_used="tftp")

    def test_precheck_may_have_no_approver_but_other_phases_record_one(self, app, user):
        """Pre-check runs on submit with no gate (§8.1)."""
        with app.app_context():
            run = make_run(user)
            precheck = make_job(run, phase="precheck")
            assert precheck.approved_by is None
            activate = make_job(run, phase="activate", approved_by=user, approved_at=NOW)
            assert activate.approved_by == user


class TestDeviceHostKeys:
    def test_one_pin_per_address(self, app):
        with app.app_context():
            db.session.add(DeviceHostKey(
                ansible_host="192.0.2.10", key_type="ssh-rsa", fingerprint_sha256="SHA256:x"))
            db.session.commit()
            db.session.add(DeviceHostKey(
                ansible_host="192.0.2.10", key_type="ssh-ed25519",
                fingerprint_sha256="SHA256:y"))
            with pytest.raises(IntegrityError):
                db.session.commit()

    def test_seeing_a_key_is_not_confirming_it(self, app, user):
        """An unconfirmed row is not a usable pin -- §4.3 keeps them apart."""
        with app.app_context():
            key = DeviceHostKey(
                ansible_host="192.0.2.11", key_type="ssh-rsa", fingerprint_sha256="SHA256:z")
            db.session.add(key)
            db.session.commit()
            assert key.first_seen_at is not None
            assert not key.is_confirmed

            key.confirmed_by, key.confirmed_at = user, NOW
            db.session.commit()
            assert key.is_confirmed


class TestRunShape:
    def test_a_gate_records_which_phase_it_is_waiting_at(self, app, user):
        with app.app_context():
            run = make_run(
                user, state="awaiting_approval", awaiting_phase="activate",
                gate_expires_at=NOW + timedelta(days=7),
            )
            assert (run.state, run.awaiting_phase) == ("awaiting_approval", "activate")

    def test_deleting_a_run_takes_its_children(self, app, user):
        """§7.4 purges a run as a unit with its host and phase rows."""
        with app.app_context():
            run = make_run(user)
            make_host(run)
            make_job(run)
            db.session.delete(run)
            db.session.commit()
            assert UpgradeRunHost.query.count() == 0
            assert UpgradePhaseJob.query.count() == 0
