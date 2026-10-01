"""PLAN.md WS-11: security headers, the server half of every confirmation box,
and what `worker_status` infers about the sibling from the rows.

Rendering is tested in test_templates.py; this file holds what a template
test cannot show -- that an unticked box is refused by the route, not only
by the browser.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from nethub import artifacts as artifact_store
from nethub import upgrades, worker_status
from nethub.extensions import db
from nethub.models import (
    DeviceHostKey,
    HostKeyScan,
    UpgradePhaseJob,
    UpgradeRun,
    UpgradeRunHost,
    User,
)
from nethub.web import CONTENT_SECURITY_POLICY

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)


# --- headers ----------------------------------------------------------------

@pytest.mark.parametrize('path', ['/login', '/', '/no-such-page', '/static/css/pico.min.css'])
def test_every_response_carries_the_security_headers(client, path):
    response = client.get(path)
    assert response.headers['Content-Security-Policy'] == CONTENT_SECURITY_POLICY
    assert response.headers['X-Content-Type-Options'] == 'nosniff'
    assert response.headers['Referrer-Policy'] == 'same-origin'


def test_the_policy_refuses_every_script_and_every_frame():
    directives = dict(d.strip().split(' ', 1) for d in CONTENT_SECURITY_POLICY.split(';'))
    assert directives['script-src'] == "'none'"
    assert directives['frame-ancestors'] == "'none'"
    assert directives['default-src'] == "'self'"
    assert directives['form-action'] == "'self'"
    assert directives['base-uri'] == "'none'"
    # Pico's checkbox ticks and chevrons are data: SVGs; nothing else widens.
    assert directives['img-src'] == "'self' data:"
    assert 'style-src' not in directives and 'unsafe' not in CONTENT_SECURITY_POLICY


# --- the server refuses an unticked box -------------------------------------

@pytest.fixture
def gate_run(app, logged_in_client):
    """A run at the activate gate with one staged host and one that failed
    stage, submitted by the logged-in user, who has a device username."""
    with app.app_context():
        user = User.query.filter_by(username='alice').one()
        user.device_username = 'jsmith'
        run = UpgradeRun(submitted_by=user.id, device_username_used='jsmith',
                         request_document='{}', request_sha512='0' * 128,
                         state='awaiting_approval', awaiting_phase='activate')
        db.session.add(run)
        db.session.flush()
        for position, (name, address, state) in enumerate((('sw01', '192.0.2.10', 'staged'),
                                                           ('sw02', '192.0.2.11', 'failed'))):
            db.session.add(UpgradeRunHost(
                run_id=run.id, hostname=name, position=position, ansible_host=address,
                filename='image.bin', sha512='a' * 128, version='17.12.06',
                file_size=1, state=state, last_phase='stage'))
        db.session.commit()
        return run.id


@pytest.fixture
def spy(monkeypatch):
    """Replace a service function with one that records its calls."""
    calls = []

    def _spy(module, name):
        def record(*args, **kwargs):
            calls.append(name)
            # A job, for the routes that flash its id and its start time.
            return SimpleNamespace(id=0, not_before=None)
        monkeypatch.setattr(module, name, record)
        return calls
    return _spy


@pytest.mark.parametrize('route,service', [
    ('approve', 'approve'),
    ('retry', 'retry'),
    ('cancel', 'request_cancel'),
])
def test_a_run_action_without_its_box_never_reaches_the_service(
    logged_in_client, gate_run, spy, route, service
):
    calls = spy(upgrades, service)
    form = {'phase': 'activate' if route == 'approve' else 'stage',
            'device_password': 'device-pass'}
    response = logged_in_client.post(f'/upgrades/{gate_run}/{route}', data=form,
                                     follow_redirects=True)
    assert calls == []
    assert b'Tick the box to confirm' in response.data
    # And ticked, the same request goes through: the refusal is the box.
    logged_in_client.post(f'/upgrades/{gate_run}/{route}',
                          data={**form, 'confirm': 'yes'})
    assert calls == [service]


def test_an_unticked_approval_queues_nothing(app, logged_in_client, gate_run):
    logged_in_client.post(f'/upgrades/{gate_run}/approve',
                          data={'phase': 'activate', 'device_password': 'device-pass'})
    with app.app_context():
        assert UpgradePhaseJob.query.count() == 0
        assert db.session.get(UpgradeRun, gate_run).state == 'awaiting_approval'


def test_an_artifact_delete_without_its_box_deletes_nothing(
    app, logged_in_client, make_artifact, spy
):
    artifact_id = make_artifact().id
    calls = spy(artifact_store, 'delete')
    response = logged_in_client.post(f'/artifacts/{artifact_id}/delete',
                                     follow_redirects=True)
    assert calls == []
    assert b'Tick the box to confirm' in response.data
    logged_in_client.post(f'/artifacts/{artifact_id}/delete', data={'confirm': 'yes'})
    assert calls == ['delete']


def test_a_pin_removal_without_its_box_removes_nothing(app, logged_in_client):
    with app.app_context():
        row = DeviceHostKey(ansible_host='192.0.2.10', key_type='ssh-rsa',
                            fingerprint_sha256='SHA256:x')
        db.session.add(row)
        db.session.commit()
        key_id = row.id
    logged_in_client.post(f'/hostkeys/{key_id}/delete')
    with app.app_context():
        assert db.session.get(DeviceHostKey, key_id) is not None
    logged_in_client.post(f'/hostkeys/{key_id}/delete', data={'confirm': 'yes'})
    with app.app_context():
        assert db.session.get(DeviceHostKey, key_id) is None


def test_a_disable_without_its_box_leaves_the_user_active(app, logged_in_client, make_user):
    make_user('bob', 'bob-long-enough-pw')
    with app.app_context():
        bob = User.query.filter_by(username='bob').one().id
    response = logged_in_client.post(f'/users/{bob}/disable', data={'confirm': 'on'},
                                     follow_redirects=True)
    assert b'Tick the box to confirm' in response.data
    with app.app_context():
        assert db.session.get(User, bob).is_active


# --- worker_status: what the rows say about the sibling ---------------------

def _run(app):
    with app.app_context():
        user = User(username=f'u{User.query.count()}')
        user.set_password('long-enough-password')
        db.session.add(user)
        db.session.flush()
        run = UpgradeRun(submitted_by=user.id, device_username_used='x',
                         request_document='{}', request_sha512='0' * 128,
                         state='running')
        db.session.add(run)
        db.session.commit()
        return run.id


def _job(app, status, *, age, beat_age=None, phase='stage'):
    with app.app_context():
        run_id = _run(app)
        job = UpgradePhaseJob(run_id=run_id, phase=phase, attempt=1, status=status,
                              created_at=NOW - age)
        if status == 'running':
            job.started_at = NOW - age
            job.heartbeat_at = None if beat_age is None else NOW - beat_age
        db.session.add(job)
        db.session.commit()
        return job.id


def _scan(app, status, *, age):
    with app.app_context():
        user = User.query.first() or User(username='scanner')
        if user.id is None:
            user.set_password('long-enough-password')
            db.session.add(user)
            db.session.flush()
        scan = HostKeyScan(ansible_host='192.0.2.10', requested_by=user.id, status=status,
                           created_at=NOW - age,
                           started_at=NOW - age if status == 'running' else None)
        db.session.add(scan)
        db.session.commit()


def _check(app, fn, *args):
    with app.app_context():
        return fn(*args)


class TestStalled:
    def stalled(self, app, job_id):
        with app.app_context():
            return worker_status.is_stalled(db.session.get(UpgradePhaseJob, job_id), NOW)

    def test_a_fresh_heartbeat_is_not(self, app):
        assert not self.stalled(app, _job(app, 'running', age=timedelta(hours=1),
                                          beat_age=timedelta(seconds=30)))

    def test_three_missed_beats_is(self, app):
        assert self.stalled(app, _job(app, 'running', age=timedelta(hours=1),
                                      beat_age=timedelta(seconds=91)))

    def test_the_threshold_is_three_heartbeat_intervals(self):
        from nethub.devices.phases import HEARTBEAT_INTERVAL
        assert worker_status.STALLED_AFTER == timedelta(seconds=3 * HEARTBEAT_INTERVAL)

    def test_a_running_row_with_no_heartbeat_falls_back_to_its_start(self, app):
        assert not self.stalled(app, _job(app, 'running', age=timedelta(seconds=10)))
        assert self.stalled(app, _job(app, 'running', age=timedelta(minutes=5)))

    def test_only_a_running_job_can_stall(self, app):
        assert not self.stalled(app, _job(app, 'queued', age=timedelta(hours=1)))


class TestNoWorker:
    def test_an_empty_queue_is_fine(self, app):
        assert not _check(app, worker_status.no_worker, NOW)

    def test_a_job_queued_briefly_is_fine(self, app):
        _job(app, 'queued', age=timedelta(seconds=30))
        assert not _check(app, worker_status.no_worker, NOW)

    def test_a_job_queued_over_a_minute_with_nothing_running_is_not(self, app):
        _job(app, 'queued', age=timedelta(seconds=61))
        assert _check(app, worker_status.no_worker, NOW)

    def test_a_live_phase_explains_the_wait(self, app):
        _job(app, 'queued', age=timedelta(minutes=20))
        _job(app, 'running', age=timedelta(minutes=30), beat_age=timedelta(seconds=10))
        assert not _check(app, worker_status.no_worker, NOW)

    def test_a_stalled_phase_does_not(self, app):
        """A sibling that died mid-phase leaves a `running` row behind; that
        row must not vouch for a worker that is gone."""
        _job(app, 'queued', age=timedelta(minutes=20))
        _job(app, 'running', age=timedelta(minutes=30), beat_age=timedelta(minutes=10))
        assert _check(app, worker_status.no_worker, NOW)

    def test_a_queued_scan_counts_too(self, app):
        _scan(app, 'queued', age=timedelta(minutes=2))
        assert _check(app, worker_status.no_worker, NOW)

    def test_a_scan_being_run_is_live_work(self, app):
        _job(app, 'queued', age=timedelta(minutes=2))
        _scan(app, 'running', age=timedelta(seconds=10))
        assert not _check(app, worker_status.no_worker, NOW)

    def test_a_scan_left_running_long_ago_is_not(self, app):
        _job(app, 'queued', age=timedelta(minutes=2))
        _scan(app, 'running', age=timedelta(hours=1))
        assert _check(app, worker_status.no_worker, NOW)


def test_queue_depth_counts_every_run(app):
    _job(app, 'queued', age=timedelta(0))
    _job(app, 'queued', age=timedelta(0))
    _job(app, 'running', age=timedelta(0), beat_age=timedelta(0))
    _job(app, 'succeeded', age=timedelta(0))
    assert _check(app, worker_status.queue_depth) == (2, 1)
