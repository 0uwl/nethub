"""Route and service checks for submitting and approving an upgrade.

Most of these are refusals. The submit path is where a request document meets
the two constraints on the only connection var it may carry -- the target CIDR
and the confirmed host key -- and where the credential is taken without ever
being stored.
"""

from datetime import datetime, timezone

import pytest

from nethub import upgrades
from nethub.extensions import db
from nethub.models import Artifact, DeviceHostKey, UpgradePhaseJob, UpgradeRun, User

NOW = datetime(2026, 9, 9, tzinfo=timezone.utc)
DIGEST = "a" * 128
PASSWORD = "d3vice-pass"
IMAGE = "cat9k_lite_iosxe.17.12.06.SPA.bin"


@pytest.fixture
def app(app):
    app.config.update(DEVICE_TARGET_CIDRS=["192.0.2.0/24"],
                      IMAGE_TRANSPORT="push_scp", SHARED_ACCOUNT_MODE=False)
    return app


@pytest.fixture
def user(app):
    with app.app_context():
        u = User(username="alice", device_username="jsmith")
        u.set_password("hunter2")
        db.session.add(u)
        db.session.add(Artifact(
            kind="image", platform="iosxe", bundle_key="iosxe-17-12-06",
            filename=IMAGE, sha512=DIGEST, file_size=471084127,
            storage_path="/images/" + IMAGE, version="17.12.06",
            state="published", bytes_state="present",
        ))
        db.session.commit()
        yield u.id


@pytest.fixture
def confirmed(app, user):
    with app.app_context():
        db.session.add(DeviceHostKey(
            ansible_host="192.0.2.10", key_type="ssh-rsa",
            fingerprint_sha256="SHA256:x", confirmed_by=user, confirmed_at=NOW))
        db.session.commit()


def submit(user_id, hosts="sw01, 192.0.2.10", bundle="iosxe-17-12-06", cidrs=None):
    return upgrades.submit(
        user=db.session.get(User, user_id),
        bundle=bundle,
        hosts_raw=hosts,
        transport="push_scp",
        cidrs=["192.0.2.0/24"] if cidrs is None else cidrs,
    )


class TestTargetValidation:
    def test_a_hostname_is_refused_rather_than_resolved(self):
        """A name would let the CIDR check and the connection disagree."""
        with pytest.raises(upgrades.RequestError, match="not an IP literal"):
            upgrades.check_target("sw01.example.net", ["192.0.2.0/24"])

    def test_an_address_outside_the_cidr_is_refused(self):
        with pytest.raises(upgrades.RequestError, match="outside the configured"):
            upgrades.check_target("198.51.100.7", ["192.0.2.0/24"])

    def test_no_configured_cidr_refuses_everything(self):
        """Fail closed: an unset deployment setting is not 'allow any'."""
        with pytest.raises(upgrades.RequestError, match="No DEVICE_TARGET_CIDRS"):
            upgrades.check_target("192.0.2.10", [])

    def test_an_address_inside_the_cidr_passes(self):
        assert upgrades.check_target("192.0.2.10", ["192.0.2.0/24"]) == "192.0.2.10"


class TestHostParsing:
    def test_a_host_named_twice_is_refused_by_line(self):
        with pytest.raises(upgrades.RequestError, match="named twice"):
            upgrades.parse_hosts("sw01, 192.0.2.10\nsw01, 192.0.2.11")

    def test_blank_lines_and_comments_are_ignored(self):
        assert upgrades.parse_hosts("# a\n\nsw01, 192.0.2.10\n") == [("sw01", "192.0.2.10")]

    def test_a_malformed_line_names_itself(self):
        with pytest.raises(upgrades.RequestError, match="Line 2"):
            upgrades.parse_hosts("sw01, 192.0.2.10\njust-a-hostname")


class TestSubmit:
    def test_an_unconfirmed_address_cannot_be_targeted(self, app, user):
        """No confirmed pin, no run -- checked before any row is written."""
        with app.app_context():
            with pytest.raises(upgrades.RequestError, match="no confirmed host key"):
                submit(user)
            assert UpgradeRun.query.count() == 0

    def test_a_seen_but_unconfirmed_pin_is_still_refused(self, app, user):
        with app.app_context():
            db.session.add(DeviceHostKey(ansible_host="192.0.2.10", key_type="ssh-rsa",
                                         fingerprint_sha256="SHA256:x"))
            db.session.commit()
            with pytest.raises(upgrades.RequestError, match="no confirmed host key"):
                submit(user)

    def test_a_user_without_a_device_username_cannot_submit(self, app, user, confirmed):
        with app.app_context():
            db.session.get(User, user).device_username = None
            db.session.commit()
            with pytest.raises(upgrades.RequestError, match="device username is not set"):
                submit(user)

    def test_an_unknown_bundle_is_refused(self, app, user, confirmed):
        with app.app_context(), pytest.raises(
            upgrades.RequestError, match="No published image"
        ):
            submit(user, bundle="nope")

    def test_a_pruned_artifact_cannot_be_installed(self, app, user, confirmed):
        """The row outlives the bytes and says so (§7.4)."""
        with app.app_context():
            Artifact.query.one().bytes_state = "pruned"
            db.session.commit()
            with pytest.raises(upgrades.RequestError, match="pruned"):
                submit(user)

    def test_a_successful_submit_snapshots_the_entry(self, app, user, confirmed):
        with app.app_context():
            run, _job = submit(user)
            host = run.hosts[0]
            assert (host.filename, host.sha512, host.version, host.file_size) == (
                IMAGE, DIGEST, "17.12.06", 471084127)
            assert host.artifact_id == Artifact.query.one().id
            assert host.state == "pending"

    def test_submit_queues_precheck_with_no_approval(self, app, user, confirmed):
        """Pre-check runs on submit and needs no gate (§8.1)."""
        with app.app_context():
            run, job = submit(user)
            assert (job.phase, job.status, job.approved_by) == ("precheck", "queued", None)
            assert run.state == "pre_checking"

    def test_the_request_document_is_stored_with_its_digest(self, app, user, confirmed):
        import hashlib
        with app.app_context():
            run, _ = submit(user)
            assert run.request_sha512 == hashlib.sha512(
                run.request_document.encode()).hexdigest()
            assert "192.0.2.10" in run.request_document

    def test_the_device_username_is_snapshotted_not_submitted(self, app, user, confirmed):
        with app.app_context():
            run, _ = submit(user)
            assert run.device_username_used == "jsmith"
            assert "jsmith" not in run.request_document


class TestGates:
    def park(self, run, phase):
        run.state, run.awaiting_phase = "awaiting_approval", phase
        db.session.commit()

    def test_approving_the_wrong_phase_is_refused(self, app, user, confirmed):
        with app.app_context():
            run, _ = submit(user)
            self.park(run, "stage")
            with pytest.raises(upgrades.RequestError, match="waiting at the stage gate"):
                upgrades.approve(run=run, phase="activate",
                                 user=db.session.get(User, user))

    def test_a_second_approval_of_the_same_gate_is_refused(self, app, user, confirmed):
        """Two admins clicking approve must not queue two reloads."""
        with app.app_context():
            run, _ = submit(user)
            self.park(run, "activate")
            alice = db.session.get(User, user)
            upgrades.approve(run=run, phase="activate", user=alice)
            self.park(run, "activate")  # pretend the gate reopened
            with pytest.raises(upgrades.RequestError, match="already been approved"):
                upgrades.approve(run=run, phase="activate", user=alice)
            assert UpgradePhaseJob.query.filter_by(phase="activate").count() == 1

    def test_approval_records_who_and_when(self, app, user, confirmed):
        with app.app_context():
            run, _ = submit(user)
            self.park(run, "stage")
            job = upgrades.approve(run=run, phase="stage",
                                   user=db.session.get(User, user))
            assert job.approved_by == user and job.approved_at is not None
            assert run.state == "running" and run.awaiting_phase is None

    def test_declining_cleanup_completes_the_run(self, app, user, confirmed):
        with app.app_context():
            run, _ = submit(user)
            self.park(run, "cleanup")
            upgrades.decline_cleanup(run=run)
            assert run.state == "completed" and run.finished_at is not None

    def test_a_run_not_at_a_gate_cannot_be_approved(self, app, user, confirmed):
        with app.app_context():
            run, _ = submit(user)
            with pytest.raises(upgrades.RequestError, match="not waiting at a gate"):
                upgrades.approve(run=run, phase="stage",
                                 user=db.session.get(User, user))

    def test_cancelling_at_a_gate_closes_the_run_immediately(self, app, user, confirmed):
        """Nothing is running, so no sibling would ever see the column."""
        with app.app_context():
            run, _ = submit(user)
            self.park(run, "activate")
            upgrades.request_cancel(run=run, user=db.session.get(User, user))
            assert run.state == "cancelled"

    def test_cancelling_a_running_run_only_sets_the_column(self, app, user, confirmed):
        with app.app_context():
            run, _ = submit(user)
            run.state = "running"
            db.session.commit()
            upgrades.request_cancel(run=run, user=db.session.get(User, user))
            assert run.state == "running", "the sibling stops it between hosts"
            assert run.cancel_requested_at is not None


class TestThroughTheClient:
    def login(self, client, app, user):
        client.post('/login', data={'username': 'alice', 'password': 'hunter2'})
        return client

    def test_the_password_is_never_written_to_a_row(self, app, client, user, confirmed):
        self.login(client, app, user)
        resp = client.post('/upgrades/new', data={
            'bundle': 'iosxe-17-12-06',
            'hosts': 'sw01, 192.0.2.10', 'device_password': PASSWORD,
        }, follow_redirects=True)
        assert resp.status_code == 200
        with app.app_context():
            blob = " ".join(
                str(v) for r in UpgradeRun.query.all()
                for v in (r.request_document, r.device_username_used, r.request_sha512))
            assert PASSWORD not in blob

    def test_pages_require_login(self, client):
        for path in ('/upgrades', '/upgrades/new', '/hostkeys', '/hostkeys/scan',
                     '/artifacts', '/artifacts/new'):
            resp = client.get(path)
            assert resp.status_code in (302, 401), path


class TestCredentialInterlock:
    """The identity cross-check has to accept the one phase with no approver."""

    def verify(self, app):
        """The production wiring, reached without socket activation."""
        from nethub.credential_socket import CredentialError
        from nethub.models import UpgradePhaseJob

        def _verify(job_id):
            with app.app_context():
                job = db.session.get(UpgradePhaseJob, job_id)
                if job is None or job.status != 'running':
                    raise CredentialError("no running execution with that id")
                return (job.approved_by if job.approved_by is not None
                        else job.run.submitted_by)
        return _verify

    def test_precheck_verifies_against_the_submitter(self, app, user, confirmed):
        """Pre-check has no gate, so `approved_by` is null by design."""
        with app.app_context():
            _run, job = submit(user)
            assert job.approved_by is None
            job.status = 'running'
            db.session.commit()
            assert self.verify(app)(job.id) == user

    def test_a_gated_phase_verifies_against_its_approver(self, app, user, confirmed):
        with app.app_context():
            run, _ = submit(user)
            run.state, run.awaiting_phase = 'awaiting_approval', 'stage'
            db.session.commit()
            job = upgrades.approve(run=run, phase='stage',
                                   user=db.session.get(User, user))
            job.status = 'running'
            db.session.commit()
            assert self.verify(app)(job.id) == user

    def test_the_store_and_the_interlock_agree_for_precheck(self, app, user, confirmed):
        """What the route holds under must be what the socket checks against."""
        from nethub.credential_socket import CredentialStore
        with app.app_context():
            _run, job = submit(user)
            job.status = 'running'
            db.session.commit()
            store = CredentialStore()
            # The route holds under `job.approved_by or current_user.id`.
            store.hold(job.id, 'jsmith', PASSWORD,
                       approved_by=job.approved_by or user)
            assert store.release(job.id, self.verify(app)(job.id)) == ('jsmith', PASSWORD)


class TestPhaseDeadlines:
    """WS-3.3: `deadline_at` was declared, read in two places, and written by
    nothing -- so §7.3's `timed_out` and `expired` were unreachable and a
    phase execution had no wall-clock bound. Both existing deadline tests set
    the column by hand, so the suite was green over inert machinery.
    """

    def test_submit_writes_a_deadline(self, app, user, confirmed):
        with app.app_context():
            _, job = submit(user)
            assert job.deadline_at is not None

    def test_approve_writes_a_deadline(self, app, user, confirmed):
        with app.app_context():
            run, _ = submit(user)
            run.state, run.awaiting_phase = 'awaiting_approval', 'stage'
            db.session.commit()
            job = upgrades.approve(
                run=run, phase='stage', user=db.session.get(User, user))
            assert job.deadline_at is not None

    def test_the_deadline_survives_the_sqlite_round_trip(self, app, user, confirmed):
        """SQLite hands datetimes back naive, which is why `_aware()` exists.

        A comparison against an aware `now()` raises TypeError for any row read
        back from the database -- always in production, never in a test that
        skips the round trip.
        """
        from nethub.devices.phases import _aware

        with app.app_context():
            _, job = submit(user)
            job_id = job.id
            db.session.expunge_all()
            fresh = db.session.get(UpgradePhaseJob, job_id)
            assert _aware(fresh.deadline_at) > upgrades._utcnow()

    def test_a_bigger_image_gets_a_longer_stage_budget(self):
        from datetime import datetime, timezone
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        small = upgrades.phase_deadline('stage', hosts=1, image_bytes=100 << 20, now=now)
        large = upgrades.phase_deadline('stage', hosts=1, image_bytes=1200 << 20, now=now)
        assert large > small, "stage is dominated by the transfer"

    def test_more_hosts_gets_a_longer_budget(self):
        from datetime import datetime, timezone
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        one = upgrades.phase_deadline('activate', hosts=1, now=now)
        many = upgrades.phase_deadline('activate', hosts=20, now=now)
        assert many > one, "a phase walks hosts one at a time"

    def test_every_phase_has_a_budget(self):
        """A phase with no entry would KeyError at submit or approve."""
        from nethub.models import PHASES
        for phase in PHASES:
            assert phase in upgrades.PHASE_BUDGET_SECONDS, phase

    def test_the_stage_budget_clears_what_real_hardware_measured(self):
        """471 MB took ~370s on the lab switch (CLAUDE.md). A deadline that
        fires on a healthy run is worse than no deadline.
        """
        from datetime import datetime, timezone
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        budget = (upgrades.phase_deadline(
            'stage', hosts=1, image_bytes=471 << 20, now=now) - now).total_seconds()
        assert budget > 370 * 4, f"only {budget / 370:.1f}x the measured time"


class TestApproveRace:
    """WS-5.2: check-then-insert, and the check is not the thing that holds."""

    def test_a_concurrent_duplicate_approval_is_a_message_not_a_500(
        self, app, user, confirmed, monkeypatch
    ):
        """Forces the commit to raise the way a real race would.

        The sequential path is already covered (the check catches it); this is
        the case the check cannot see, which only became reachable when the
        app started running more than one thread.
        """
        from sqlalchemy.exc import IntegrityError

        with app.app_context():
            run, _ = submit(user)
            run.state, run.awaiting_phase = 'awaiting_approval', 'activate'
            db.session.commit()

            real = db.session.commit
            calls = []

            def once_failing():
                calls.append(1)
                if len(calls) == 1:
                    raise IntegrityError('forced', None, Exception('forced'))
                return real()

            monkeypatch.setattr(db.session, 'commit', once_failing)
            with pytest.raises(upgrades.RequestError, match='already been approved'):
                upgrades.approve(run=run, phase='activate',
                                 user=db.session.get(User, user))

    def test_the_constraint_still_forbids_two_rows(self, app, user, confirmed):
        """Whatever the route does, the database is what stops two reloads."""
        from sqlalchemy.exc import IntegrityError

        with app.app_context():
            run, _ = submit(user)
            run.state, run.awaiting_phase = 'awaiting_approval', 'activate'
            db.session.commit()
            upgrades.approve(run=run, phase='activate',
                             user=db.session.get(User, user))
            db.session.add(UpgradePhaseJob(
                run_id=run.id, phase='activate', attempt=1, status='queued',
                created_at=upgrades._utcnow()))
            with pytest.raises(IntegrityError):
                db.session.commit()
            db.session.rollback()
