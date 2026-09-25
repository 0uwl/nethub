"""Route and service checks for submitting and approving an upgrade.

Most of these are refusals. The submit path is where a request document meets
the two constraints on the only connection var it may carry -- the target CIDR
and the confirmed host key -- and where the credential is taken without ever
being stored.
"""

from datetime import datetime, timedelta, timezone

import pytest
from flask import current_app

from nethub import sealed_credentials as SC
from nethub import upgrades
from nethub.extensions import db
from nethub.models import (
    Artifact,
    DeviceHostKey,
    DeviceHostKeyAudit,
    HostKeyScan,
    UpgradePhaseJob,
    UpgradeRun,
    User,
)

NOW = datetime(2026, 9, 9, tzinfo=timezone.utc)
DIGEST = "a" * 128
PASSWORD = "d3vice-pass"
IMAGE = "cat9k_lite_iosxe.17.12.06.SPA.bin"


@pytest.fixture
def app(app):
    app.config.update(DEVICE_TARGET_CIDRS=["192.0.2.0/24"])
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
        cidrs=["192.0.2.0/24"] if cidrs is None else cidrs,
        password=PASSWORD,
        public_key=current_app.extensions["credential_public_key"],
    )


def approve(**kwargs):
    """upgrades.approve with the device password and the app's public key."""
    kwargs.setdefault("password", PASSWORD)
    kwargs.setdefault("public_key", current_app.extensions["credential_public_key"])
    return upgrades.approve(**kwargs)


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
                approve(run=run, phase="activate",
                                 user=db.session.get(User, user))

    def test_a_second_approval_of_the_same_gate_is_refused(self, app, user, confirmed):
        """Two admins clicking approve must not queue two reloads."""
        with app.app_context():
            run, _ = submit(user)
            self.park(run, "activate")
            alice = db.session.get(User, user)
            approve(run=run, phase="activate", user=alice)
            self.park(run, "activate")  # pretend the gate reopened
            with pytest.raises(upgrades.RequestError, match="already been approved"):
                approve(run=run, phase="activate", user=alice)
            assert UpgradePhaseJob.query.filter_by(phase="activate").count() == 1

    def test_approval_records_who_and_when(self, app, user, confirmed):
        with app.app_context():
            run, _ = submit(user)
            self.park(run, "stage")
            job = approve(run=run, phase="stage",
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
                approve(run=run, phase="stage",
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


class TestSealedAtSubmitAndApprove:
    """PLAN.md WS-7: the credential is sealed into the job row it was
    collected for, to the sibling's public key. These open it with the
    private half, as the sibling would."""

    @staticmethod
    def open_as_sibling(key, job, approved_by, now=None):
        return SC.open_sealed(key, job.sealed_credential, job_id=job.id,
                              approved_by=approved_by, now=now or datetime.now(timezone.utc))

    def test_precheck_is_sealed_for_the_submitter(
            self, app, user, confirmed, credential_private_key):
        """Pre-check has no gate, so the identity that supplied the credential
        is the submitter -- the rule the socket interlock once got wrong."""
        with app.app_context():
            _run, job = submit(user)
            assert job.approved_by is None
            credential = self.open_as_sibling(credential_private_key, job, user)
            assert (credential.username, credential.password) == ('jsmith', PASSWORD)

    def test_a_gated_phase_is_sealed_for_its_approver(
            self, app, user, confirmed, credential_private_key):
        with app.app_context():
            run, _ = submit(user)
            run.state, run.awaiting_phase = 'awaiting_approval', 'stage'
            db.session.commit()
            job = approve(run=run, phase='stage', user=db.session.get(User, user))
            assert self.open_as_sibling(credential_private_key, job, user).password == PASSWORD
            with pytest.raises(SC.CredentialError, match='different identity'):
                self.open_as_sibling(credential_private_key, job, user + 1)

    def test_it_expires_with_the_jobs_deadline(
            self, app, user, confirmed, credential_private_key):
        with app.app_context():
            _run, job = submit(user)
            deadline = job.deadline_at.replace(tzinfo=timezone.utc)
            self.open_as_sibling(credential_private_key, job, user,
                                 now=deadline - timedelta(seconds=1))
            with pytest.raises(SC.CredentialError, match='expired'):
                self.open_as_sibling(credential_private_key, job, user, now=deadline)

    def test_the_password_is_not_in_the_row_in_the_clear(self, app, user, confirmed):
        with app.app_context():
            _run, job = submit(user)
            assert PASSWORD.encode() not in job.sealed_credential
            assert b'jsmith' not in job.sealed_credential


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
            job = approve(
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
        """Forces the flush to raise the way a real race would.

        The sequential path is already covered (the check catches it); this is
        the case the check cannot see, which only became reachable when the
        app started running more than one thread. The flush, not the commit,
        is where the constraint fires: the route holds the credential between
        the two.
        """
        from sqlalchemy.exc import IntegrityError

        with app.app_context():
            run, _ = submit(user)
            run.state, run.awaiting_phase = 'awaiting_approval', 'activate'
            db.session.commit()

            real = db.session.flush
            calls = []

            def once_failing(*a, **kw):
                calls.append(1)
                if len(calls) == 1:
                    raise IntegrityError('forced', None, Exception('forced'))
                return real(*a, **kw)

            monkeypatch.setattr(db.session, 'flush', once_failing)
            with pytest.raises(upgrades.RequestError, match='already been approved'):
                approve(run=run, phase='activate',
                                 user=db.session.get(User, user))

    def test_the_constraint_still_forbids_two_rows(self, app, user, confirmed):
        """Whatever the route does, the database is what stops two reloads."""
        from sqlalchemy.exc import IntegrityError

        with app.app_context():
            run, _ = submit(user)
            run.state, run.awaiting_phase = 'awaiting_approval', 'activate'
            db.session.commit()
            approve(run=run, phase='activate',
                             user=db.session.get(User, user))
            db.session.add(UpgradePhaseJob(
                run_id=run.id, phase='activate', attempt=1, status='queued',
                created_at=upgrades._utcnow()))
            with pytest.raises(IntegrityError):
                db.session.commit()
            db.session.rollback()


class TestCancelDropsTheCredential:
    """A cancelled queued job will be finished `cancelled` without a claim, so
    its credential will never be opened: cancel drops the ciphertext at once
    instead of leaving it until the sibling gets there."""

    def login(self, client):
        client.post('/login', data={'username': 'alice', 'password': 'hunter2'})
        return client

    def test_cancelling_clears_a_queued_jobs_ciphertext(
        self, app, client, user, confirmed
    ):
        self.login(client)
        resp = client.post('/upgrades/new', data={
            'bundle': 'iosxe-17-12-06',
            'hosts': 'sw01, 192.0.2.10', 'device_password': PASSWORD,
        }, follow_redirects=True)
        assert resp.status_code == 200

        with app.app_context():
            run = UpgradeRun.query.one()
            run_id = run.id
            assert UpgradePhaseJob.query.one().sealed_credential is not None
        client.post(f'/upgrades/{run_id}/cancel', follow_redirects=True)
        with app.app_context():
            job = UpgradePhaseJob.query.one()
            # Cleared, but still `queued`: the sibling writes `cancelled`,
            # since Flask writes no job-status edge after creation (§7.3).
            assert (job.status, job.sealed_credential) == ('queued', None)


class TestHostkeyScanDispatch:
    """WS-6.2b: scanning is dispatched to the sibling, not run inline."""

    def login(self, client):
        client.post('/login', data={'username': 'alice', 'password': 'hunter2'})
        return client

    def test_a_valid_address_queues_a_scan_without_blocking(self, app, client, user, monkeypatch):
        from nethub.devices import connection

        def hang(*a, **kw):
            raise AssertionError('scan_hostkey must not call scan_host_key inline')
        monkeypatch.setattr(connection, 'scan_host_key', hang)

        self.login(client)
        resp = client.post('/hostkeys/scan', data={'address': '192.0.2.10'})
        assert resp.status_code == 302
        with app.app_context():
            scan = HostKeyScan.query.one()
            assert scan.status == 'queued'
            assert scan.ansible_host == '192.0.2.10'
            assert scan.requested_by == user

    def test_an_address_outside_the_cidr_is_refused(self, app, client, user):
        self.login(client)
        resp = client.post('/hostkeys/scan', data={'address': '198.51.100.7'},
                           follow_redirects=True)
        assert b'outside the configured' in resp.data
        with app.app_context():
            assert HostKeyScan.query.count() == 0

    def test_a_hostname_is_refused(self, app, client, user):
        self.login(client)
        resp = client.post('/hostkeys/scan', data={'address': 'sw01.example.net'},
                           follow_redirects=True)
        assert b'not an IP literal' in resp.data
        with app.app_context():
            assert HostKeyScan.query.count() == 0


class TestConfirmHostkeyBinding:
    """WS-6.3: confirm_hostkey binds to a HostKeyScan NetHub itself produced,
    rather than trusting whatever a form claims.
    """

    def login(self, client):
        client.post('/login', data={'username': 'alice', 'password': 'hunter2'})
        return client

    def make_scan(self, app, *, requested_by, status='succeeded',
                  ansible_host='192.0.2.10', key_type='ssh-rsa',
                  fingerprint='SHA256:x', finished_at=None, consumed_at=None):
        with app.app_context():
            scan = HostKeyScan(
                ansible_host=ansible_host, requested_by=requested_by, status=status,
                key_type=key_type if status == 'succeeded' else None,
                fingerprint_sha256=fingerprint if status == 'succeeded' else None,
                finished_at=finished_at or upgrades._utcnow(), consumed_at=consumed_at,
            )
            db.session.add(scan)
            db.session.commit()
            return scan.id

    def test_confirming_a_succeeded_scan_pins_the_address(self, app, client, user):
        scan_id = self.make_scan(app, requested_by=user)
        self.login(client)
        resp = client.post('/hostkeys/confirm', data={'scan_id': scan_id},
                           follow_redirects=True)
        assert b'Confirmed' in resp.data
        with app.app_context():
            row = DeviceHostKey.query.filter_by(ansible_host='192.0.2.10').one()
            assert row.is_confirmed
            assert row.key_type == 'ssh-rsa'
            assert row.fingerprint_sha256 == 'SHA256:x'

    def test_a_scan_belonging_to_a_different_user_is_refused(self, app, client, user):
        with app.app_context():
            other = User(username='bob', device_username='bob')
            other.set_password('bob-long-enough-pw')
            db.session.add(other)
            db.session.commit()
            other_id = other.id
        scan_id = self.make_scan(app, requested_by=other_id)
        self.login(client)
        resp = client.post('/hostkeys/confirm', data={'scan_id': scan_id},
                           follow_redirects=True)
        assert b'No matching scan' in resp.data
        with app.app_context():
            assert DeviceHostKey.query.count() == 0

    def test_a_queued_scan_cannot_confirm(self, app, client, user):
        scan_id = self.make_scan(app, requested_by=user, status='queued')
        self.login(client)
        resp = client.post('/hostkeys/confirm', data={'scan_id': scan_id},
                           follow_redirects=True)
        assert b'No matching scan' in resp.data

    def test_a_failed_scan_cannot_confirm(self, app, client, user):
        scan_id = self.make_scan(app, requested_by=user, status='failed')
        self.login(client)
        resp = client.post('/hostkeys/confirm', data={'scan_id': scan_id},
                           follow_redirects=True)
        assert b'No matching scan' in resp.data

    def test_an_already_consumed_scan_cannot_confirm_twice(self, app, client, user):
        """A succeeded scan confirms at most once -- the same one-shot
        pattern §4.1 uses for the provisioning allowlist."""
        scan_id = self.make_scan(app, requested_by=user)
        self.login(client)
        client.post('/hostkeys/confirm', data={'scan_id': scan_id}, follow_redirects=True)
        resp = client.post('/hostkeys/confirm', data={'scan_id': scan_id},
                           follow_redirects=True)
        assert b'already been used' in resp.data
        with app.app_context():
            assert db.session.get(HostKeyScan, scan_id).consumed_at is not None

    def test_a_stale_scan_outside_the_freshness_window_is_refused(self, app, client, user):
        old = upgrades._utcnow() - timedelta(minutes=20)
        scan_id = self.make_scan(app, requested_by=user, finished_at=old)
        self.login(client)
        resp = client.post('/hostkeys/confirm', data={'scan_id': scan_id},
                           follow_redirects=True)
        assert b'too old' in resp.data

    def test_confirm_ignores_extra_fields_in_the_post_body(self, app, client, user):
        """key_type/fingerprint/address come from the HostKeyScan row, never
        from whatever else a POST body might carry alongside scan_id."""
        scan_id = self.make_scan(app, requested_by=user, key_type='ssh-rsa',
                                 fingerprint='SHA256:real')
        self.login(client)
        client.post('/hostkeys/confirm', data={
            'scan_id': scan_id, 'key_type': 'ssh-ed25519', 'fingerprint': 'SHA256:fake',
            'address': '198.51.100.7',
        }, follow_redirects=True)
        with app.app_context():
            row = DeviceHostKey.query.filter_by(ansible_host='192.0.2.10').one()
            assert row.key_type == 'ssh-rsa'
            assert row.fingerprint_sha256 == 'SHA256:real'


class TestHostkeyAudit:
    """WS-6.4: confirm/delete leave an audit row with the pre-image."""

    def login(self, client):
        client.post('/login', data={'username': 'alice', 'password': 'hunter2'})
        return client

    def test_deleting_a_pin_leaves_an_audit_row_with_the_pre_delete_fingerprint(
        self, app, client, user, confirmed
    ):
        self.login(client)
        with app.app_context():
            key_id = DeviceHostKey.query.filter_by(ansible_host='192.0.2.10').one().id
        client.post(f'/hostkeys/{key_id}/delete', follow_redirects=True)
        with app.app_context():
            entry = DeviceHostKeyAudit.query.filter_by(ansible_host='192.0.2.10').one()
            assert entry.action == 'deleted'
            assert entry.fingerprint_sha256 == 'SHA256:x'
            assert entry.actor_id == user

    def test_confirming_leaves_an_audit_row_with_the_new_fingerprint(self, app, client, user):
        with app.app_context():
            scan = HostKeyScan(ansible_host='192.0.2.10', requested_by=user,
                               status='succeeded', key_type='ssh-rsa',
                               fingerprint_sha256='SHA256:new',
                               finished_at=upgrades._utcnow())
            db.session.add(scan)
            db.session.commit()
            scan_id = scan.id
        self.login(client)
        client.post('/hostkeys/confirm', data={'scan_id': scan_id}, follow_redirects=True)
        with app.app_context():
            entry = DeviceHostKeyAudit.query.filter_by(
                ansible_host='192.0.2.10', action='confirmed'
            ).one()
            assert entry.fingerprint_sha256 == 'SHA256:new'

    def test_the_audit_row_survives_the_pins_deletion(self, app, client, user, confirmed):
        self.login(client)
        with app.app_context():
            key_id = DeviceHostKey.query.one().id
        client.post(f'/hostkeys/{key_id}/delete', follow_redirects=True)
        with app.app_context():
            assert DeviceHostKey.query.count() == 0
            assert DeviceHostKeyAudit.query.filter_by(
                ansible_host='192.0.2.10'
            ).count() == 1


class TestSealedInTheSameTransaction:
    """WS-1.1's race, closed by construction. The queued row used to be
    committed before its credential was held, so the sibling could claim it
    in the gap and fail the run. Now the ciphertext is part of the row: the
    hook below plays the sibling at the worst moment, straight after the
    commit that makes the job claimable, and finds it already there."""

    def login(self, client):
        client.post('/login', data={'username': 'alice', 'password': 'hunter2'})

    def sibling_polls_after_commit(self, monkeypatch, phase):
        real = db.session.commit
        seen = []

        def commit_then_poll():
            real()
            job = UpgradePhaseJob.query.filter_by(phase=phase, status='queued').first()
            if job is not None and not seen:
                seen.append(job.sealed_credential)

        monkeypatch.setattr(db.session, 'commit', commit_then_poll)
        return seen

    def test_a_submitted_precheck_is_never_claimable_without_its_credential(
        self, app, client, user, confirmed, monkeypatch
    ):
        self.login(client)
        seen = self.sibling_polls_after_commit(monkeypatch, 'precheck')
        client.post('/upgrades/new', data={
            'bundle': 'iosxe-17-12-06', 'hosts': 'sw01, 192.0.2.10',
            'device_password': PASSWORD,
        })
        assert len(seen) == 1 and seen[0] is not None

    def test_an_approved_phase_is_never_claimable_without_its_credential(
        self, app, client, user, confirmed, monkeypatch
    ):
        self.login(client)
        client.post('/upgrades/new', data={
            'bundle': 'iosxe-17-12-06', 'hosts': 'sw01, 192.0.2.10',
            'device_password': PASSWORD,
        })
        with app.app_context():
            run = UpgradeRun.query.one()
            run.state, run.awaiting_phase = 'awaiting_approval', 'stage'
            run_id = run.id
            db.session.commit()
        seen = self.sibling_polls_after_commit(monkeypatch, 'stage')
        client.post(f'/upgrades/{run_id}/approve',
                    data={'phase': 'stage', 'device_password': PASSWORD})
        assert len(seen) == 1 and seen[0] is not None

    @pytest.mark.parametrize('password', ['', 'tab\there', 'x' * 129],
                             ids=['empty', 'control-character', 'over-the-cap'])
    def test_a_refused_password_leaves_no_rows(self, app, client, user, confirmed, password):
        """Checked against the sibling's allowlist before anything is written,
        so a refusal is a message, not a rollback."""
        self.login(client)
        resp = client.post('/upgrades/new', data={
            'bundle': 'iosxe-17-12-06', 'hosts': 'sw01, 192.0.2.10',
            'device_password': password,
        }, follow_redirects=True)
        assert b'Device password refused' in resp.data
        with app.app_context():
            assert UpgradeRun.query.count() == 0
            assert UpgradePhaseJob.query.count() == 0

    def test_a_device_username_the_sibling_would_refuse_is_refused_first(
            self, app, user, confirmed):
        with app.app_context():
            db.session.get(User, user).device_username = 'j\u00e9r\u00f4me'
            db.session.commit()
            with pytest.raises(upgrades.RequestError, match='Device username refused'):
                submit(user)
            assert UpgradeRun.query.count() == 0

    def test_a_failed_commit_leaves_no_rows(
        self, app, client, user, confirmed, monkeypatch
    ):
        from sqlalchemy.exc import OperationalError

        self.login(client)

        def failing_commit():
            raise OperationalError('forced', None, Exception('database is locked'))

        monkeypatch.setattr(db.session, 'commit', failing_commit)
        with pytest.raises(OperationalError):
            client.post('/upgrades/new', data={
                'bundle': 'iosxe-17-12-06', 'hosts': 'sw01, 192.0.2.10',
                'device_password': PASSWORD,
            })
        monkeypatch.undo()
        with app.app_context():
            assert UpgradeRun.query.count() == 0


class TestApproverChecks:
    def park(self, run, phase='stage'):
        run.state, run.awaiting_phase = 'awaiting_approval', phase
        db.session.commit()

    def test_an_approver_without_a_device_username_is_refused(
        self, app, user, confirmed
    ):
        """WS-1.2: the route used to hold `None` as the username, and the
        sibling refused it as malformed after the gate had been spent."""
        with app.app_context():
            run, _ = submit(user)
            self.park(run)
            bob = User(username='bob')
            bob.set_password('hunter2hunter2')
            db.session.add(bob)
            db.session.commit()
            with pytest.raises(upgrades.RequestError, match='device username is not set'):
                approve(run=run, phase='stage', user=bob)
            assert UpgradePhaseJob.query.filter_by(phase='stage').count() == 0

    def test_an_expired_gate_is_refused_even_before_the_sibling_sees_it(
        self, app, user, confirmed
    ):
        """WS-1.4: the TTL holds while the sibling is down. Flask refuses; it
        does not write the `expired` edge, which is the sibling's (§7.3)."""
        with app.app_context():
            run, _ = submit(user)
            run.gate_expires_at = upgrades._utcnow() - timedelta(seconds=1)
            self.park(run)
            with pytest.raises(upgrades.RequestError, match='expired'):
                approve(run=run, phase='stage',
                                 user=db.session.get(User, user))
            assert db.session.get(UpgradeRun, run.id).state == 'awaiting_approval'


class TestSubmitRacingADelete:
    def test_a_submit_whose_artifact_vanishes_is_a_refusal_not_a_500(
        self, app, user, confirmed, monkeypatch
    ):
        """WS-2.2: the artifact is deleted between resolving the bundle and
        inserting the host rows. The foreign key refuses the insert, and the
        submitter gets the ordinary "not registered" message."""
        real = upgrades.resolve_bundle
        calls = []

        def resolve_then_delete(bundle, platform='iosxe'):
            artifact = real(bundle, platform=platform)
            if not calls:
                calls.append(1)
                # Another request's delete: its own connection, committed.
                with db.engine.begin() as other:
                    other.execute(db.text('DELETE FROM artifacts'))
            return artifact

        monkeypatch.setattr(upgrades, 'resolve_bundle', resolve_then_delete)
        with app.app_context():
            with pytest.raises(upgrades.RequestError, match='No published image'):
                submit(user)
            assert UpgradeRun.query.count() == 0
