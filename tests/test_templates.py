"""Rendering checks for the Jinja templates.

There were none of these at all: two route tests checked a status code and
nothing inspected a response body, so a broken `url_for`, a renamed context
variable or a CSRF token vanishing from a form would all have passed.

The CSRF check needs its own app, because the shared `app` fixture sets
WTF_CSRF_ENABLED=False -- which is what made a missing token invisible.
"""
import hashlib
import os
import re
import shutil
from datetime import datetime, timedelta, timezone

import pytest

from nethub import create_app
from nethub.extensions import db
from nethub.models import (
    STATE_BEFORE,
    DeviceHostKeyAudit,
    HostKeyScan,
    UpgradePhaseJob,
    UpgradeRun,
    UpgradeRunHost,
    User,
)


@pytest.fixture
def make_run(app, make_user):
    """A run parked at a gate, so the approval form actually renders.

    Built here rather than in conftest.py: only these tests need it, and
    conftest is the one file every parallel branch would otherwise touch.
    """
    def _make(state='awaiting_approval', awaiting_phase='activate', failed_phase=None):
        with app.app_context():
            user = User.query.filter_by(username='alice').first()
            if user is None:
                user = User(username='alice', device_username='jsmith')
                user.set_password('alice-long-enough-pw')
                db.session.add(user)
                db.session.commit()
            run = UpgradeRun(
                submitted_by=user.id,
                device_username_used='jsmith',
                request_document='{}',
                request_sha512='0' * 128,
                state=state,
                awaiting_phase=awaiting_phase,
            )
            db.session.add(run)
            db.session.flush()
            # Where the gate's phase starts, so the approve form counts it:
            # a phase runs only on the hosts whose cursor sits just before it.
            db.session.add(UpgradeRunHost(
                run_id=run.id, hostname='sw01', position=0, ansible_host='192.0.2.10',
                filename='cat9k_lite_iosxe.17.12.06.SPA.bin',
                sha512='a' * 128, version='17.12.06', file_size=1234,
                state=STATE_BEFORE.get(awaiting_phase, 'pending'),
            ))
            if failed_phase:
                # A second host that failed `failed_phase`, so the run page
                # has something to offer a retry for (PLAN.md WS-8).
                db.session.add(UpgradeRunHost(
                    run_id=run.id, hostname='sw02', position=1, ansible_host='192.0.2.11',
                    filename='cat9k_lite_iosxe.17.12.06.SPA.bin',
                    sha512='a' * 128, version='17.12.06', file_size=1234,
                    state='failed', last_phase=failed_phase,
                    error_summary='digest mismatch',
                ))
            db.session.commit()
            return run.id
    return _make


@pytest.fixture
def operator(logged_in_client, app):
    """The logged-in client, with a device username set: without one, every
    form that collects a device password shows a notice instead (WS-11)."""
    with app.app_context():
        User.query.filter_by(username='alice').one().device_username = 'jsmith'
        db.session.commit()
    return logged_in_client


@pytest.fixture
def make_scan(app):
    """A `HostKeyScan` row, so the scan-result page's non-empty states render.

    Looks up 'alice' the same way `make_run` does rather than depending on
    `make_user`, so the two fixtures don't race to create the same row when a
    test asks for both `logged_in_client` and this one.
    """
    def _make(status='succeeded', key_type='ssh-rsa', fingerprint='SHA256:x',
              error_summary=None, consumed_at=None, ansible_host='192.0.2.10'):
        with app.app_context():
            user = User.query.filter_by(username='alice').first()
            if user is None:
                user = User(username='alice', device_username='jsmith')
                user.set_password('alice-long-enough-pw')
                db.session.add(user)
                db.session.commit()
            scan = HostKeyScan(
                ansible_host=ansible_host, requested_by=user.id, status=status,
                key_type=key_type if status == 'succeeded' else None,
                fingerprint_sha256=fingerprint if status == 'succeeded' else None,
                error_summary=error_summary,
                finished_at=datetime.now(timezone.utc), consumed_at=consumed_at,
            )
            db.session.add(scan)
            db.session.commit()
            return scan.id
    return _make

#: Every authenticated GET page, with the kwargs its route needs.
PAGES = [
    '/',
    '/artifacts',
    '/artifacts/new',
    '/upgrades',
    '/upgrades/new',
    '/hostkeys',
    '/hostkeys/scan',
    '/users',
    '/users/new',
    '/profile',
]


@pytest.mark.parametrize('path', PAGES)
def test_every_page_renders(logged_in_client, path):
    resp = logged_in_client.get(path)
    assert resp.status_code == 200, f'{path} returned {resp.status_code}'
    assert b'Traceback' not in resp.data


@pytest.mark.parametrize('path', PAGES)
def test_no_page_leaks_an_undefined_variable(logged_in_client, path):
    """Jinja renders an undefined as an empty string, so this is about markup.

    A context variable that was renamed shows up as a blank cell rather than
    an error; the best cheap signal is that nothing rendered the literal
    repr of a Python object or a Jinja tag that failed to close.
    """
    body = logged_in_client.get(path).get_data(as_text=True)
    assert '{{' not in body
    assert '{%' not in body


def test_the_profile_page_is_reachable_from_the_nav(logged_in_client):
    """WS-5.1: the route existed but nothing linked to it, so it may as well
    not have. `upgrades.submit` refuses every run without a device username.
    """
    body = logged_in_client.get('/artifacts').get_data(as_text=True)
    assert '/profile' in body


def test_the_profile_page_offers_the_device_username_form(logged_in_client):
    body = logged_in_client.get('/profile').get_data(as_text=True)
    assert 'name="device_username"' in body
    assert '/profile/device-username' in body


def test_setting_a_device_username_works_end_to_end(logged_in_client, app):
    logged_in_client.post('/profile/device-username',
                          data={'device_username': 'jsmith'})
    with app.app_context():
        assert User.query.filter_by(username='alice').first().device_username == 'jsmith'


def test_clearing_a_device_username_stores_null_not_empty(logged_in_client, app):
    logged_in_client.post('/profile/device-username', data={'device_username': 'x'})
    logged_in_client.post('/profile/device-username', data={'device_username': ''})
    with app.app_context():
        # Null, not '': `upgrades.submit` tests falsiness, but a null is what
        # the column means and what a later NOT NULL check would expect.
        assert User.query.filter_by(username='alice').first().device_username is None


# --- The confirmation on the one action that reboots hardware ----------------

def test_the_activate_approval_form_confirms(operator, make_run):
    """A required checkbox, not a JavaScript confirm(): the CSP blocks every
    script, so an onsubmit prompt would silently vanish (WS-11)."""
    run = make_run(state='awaiting_approval', awaiting_phase='activate')
    body = operator.get(f'/upgrades/{run}').get_data(as_text=True)
    form = _form_containing(body, f'/upgrades/{run}/approve')
    assert form, 'no approve form'
    assert _confirm_box(form)
    assert 'onsubmit' not in form and 'confirm(' not in form
    assert 'Reload' in form


def test_the_confirmation_names_the_host_count(operator, make_run):
    run = make_run(state='awaiting_approval', awaiting_phase='activate')
    body = operator.get(f'/upgrades/{run}').get_data(as_text=True)
    # One host in the fixture, so the copy has to say 1 rather than a blank.
    assert 'Reload 1 device(s)' in body


def test_the_host_count_is_the_hosts_the_phase_runs_on(operator, make_run):
    """sw02 failed stage, so activate runs on sw01 alone. Counting every
    host in the run would tell the approver two devices reload."""
    run = make_run(awaiting_phase='activate', failed_phase='stage')
    form = _form_containing(operator.get(f'/upgrades/{run}').get_data(as_text=True),
                            f'/upgrades/{run}/approve')
    assert 'Reload 1 device(s)' in form


def _confirm_box(form):
    """The form's confirmation checkbox, required, as the routes expect it."""
    box = re.search(r'<input type="checkbox" name="confirm" value="yes" required[\s>]', form)
    return box is not None and '<label class="confirm"' in form


# --- Retrying the hosts that failed a phase (PLAN.md WS-8) --------------------

def test_a_host_that_failed_stage_offers_a_retry_at_the_activate_gate(
    operator, make_run
):
    run = make_run(awaiting_phase='activate', failed_phase='stage')
    body = operator.get(f'/upgrades/{run}').get_data(as_text=True)
    form = _form_containing(body, f'/upgrades/{run}/retry')
    assert form, 'no retry form'
    assert 'name="phase" value="stage"' in form
    assert 'name="device_password"' in form
    assert 'name="csrf_token"' in form
    assert _confirm_box(form)
    assert 'Retry stage on 1 host(s)' in form
    assert 'sw02' in body
    # The approve form is still there: retrying is an option, not a detour.
    assert _form_containing(body, f'/upgrades/{run}/approve')
    assert '{{' not in body and '{%' not in body


def test_no_retry_is_offered_without_a_failed_host(logged_in_client, make_run):
    run = make_run(awaiting_phase='activate')
    body = logged_in_client.get(f'/upgrades/{run}').get_data(as_text=True)
    assert f'/upgrades/{run}/retry' not in body


def test_no_retry_is_offered_for_a_phase_from_before_the_previous_gate(
    logged_in_client, make_run
):
    """A host that failed pre-check is not retryable at the activate gate:
    only the phases that ran since the gate before it are."""
    run = make_run(awaiting_phase='activate', failed_phase='precheck')
    body = logged_in_client.get(f'/upgrades/{run}').get_data(as_text=True)
    assert f'/upgrades/{run}/retry' not in body


def test_a_retry_of_activate_warns_about_the_reload(logged_in_client, make_run):
    run = make_run(awaiting_phase='cleanup', failed_phase='activate')
    body = logged_in_client.get(f'/upgrades/{run}').get_data(as_text=True)
    form_start = body.index('Retry: activate')
    assert 'reloads the hosts listed' in body[form_start:]


def test_the_hosts_table_shows_the_last_phase(logged_in_client, make_run):
    run = make_run(awaiting_phase='activate', failed_phase='stage')
    body = logged_in_client.get(f'/upgrades/{run}').get_data(as_text=True)
    assert '<th>Last phase</th>' in body


def _form_containing(body, needle):
    for match in re.finditer(r'<form\b.*?</form>', body, re.DOTALL):
        if needle in match.group(0):
            return match.group(0)
    return None


# --- Flash categories -------------------------------------------------------

def test_a_success_flash_is_not_styled_as_an_error(logged_in_client):
    """Every flash used to render alert-error whatever it said."""
    resp = logged_in_client.post('/profile/device-username',
                                 data={'device_username': 'jsmith'},
                                 follow_redirects=True)
    body = resp.get_data(as_text=True)
    assert 'alert-success' in body
    assert 'alert-error' not in body


def test_an_uncategorised_flash_still_reads_as_an_error(logged_in_client):
    """The default category stays an error on purpose.

    Most remaining uncategorised calls are refusals, so downgrading the
    default to a neutral notice would quietly mis-style real failures.
    """
    resp = logged_in_client.get('/upgrades/99999', follow_redirects=True)
    body = resp.get_data(as_text=True)
    assert 'No such run' in body
    assert 'alert-error' in body


# --- CSRF: the check the shared fixture cannot make -------------------------

@pytest.fixture
def csrf_app(drop_database):
    shutil.rmtree(os.environ['ARTIFACT_STORE'], ignore_errors=True)
    os.makedirs(os.environ['ARTIFACT_STORE'])
    flask_app = create_app()
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=True)
    yield flask_app
    drop_database(flask_app)


def test_every_post_form_carries_a_csrf_token(csrf_app):
    """With CSRF enabled, a form missing its token is a 400 the user cannot
    get past -- and the shared `app` fixture disables CSRF, so nothing else
    in the suite would notice a token being deleted from a template.
    """
    client = csrf_app.test_client()
    with csrf_app.app_context():
        user = User(username='alice', device_username='jsmith')
        user.set_password('alice-long-enough-pw')
        db.session.add(user)
        db.session.commit()

    page = client.get('/login').get_data(as_text=True)
    token = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', page)
    assert token, 'the login form itself has no CSRF token'
    client.post('/login', data={'username': 'alice',
                                'password': 'alice-long-enough-pw',
                                'csrf_token': token.group(1)})

    missing = []
    for path in PAGES:
        body = client.get(path).get_data(as_text=True)
        for form in re.finditer(r'<form\b(.*?)</form>', body, re.DOTALL):
            attrs = form.group(0)
            if 'method="post"' not in attrs.lower():
                continue
            if 'name="csrf_token"' not in attrs:
                missing.append((path, attrs[:80]))
    assert missing == [], f'POST forms with no CSRF token: {missing}'


def test_the_logout_form_in_the_layout_has_a_token(csrf_app):
    """It lives in the layout, so it is on every page and easy to overlook."""
    client = csrf_app.test_client()
    page = client.get('/login').get_data(as_text=True)
    # Not logged in, so the nav is hidden -- assert on the login page's own form
    # and separately that the layout's logout form is templated with a token.
    assert 'name="csrf_token"' in page
    # Read through the app's own Jinja loader rather than a relative path:
    # that made the test depend on pytest's working directory, and it would
    # have failed for anyone running it from outside the repo root.
    layout, _, _ = csrf_app.jinja_env.loader.get_source(
        csrf_app.jinja_env, 'layouts/main.html'
    )
    logout = _form_containing(layout, 'auth.logout')
    assert logout and 'csrf_token' in logout


# --- Narrow screens ---------------------------------------------------------

def test_every_table_can_scroll_on_its_own(logged_in_client):
    """A table wider than a phone must scroll inside its own container rather
    than making the page body scroll sideways.
    """
    for path in PAGES:
        body = logged_in_client.get(path).get_data(as_text=True)
        tables = body.count('<table')
        wrappers = body.count('<div class="overflow-auto">')
        assert wrappers >= tables, f'{path}: {tables} tables, {wrappers} wrappers'


def test_user_created_is_styled_as_a_success(logged_in_client):
    """This one rendered as alert-error until both halves of the review work
    were merged: auth.py belonged to the login branch, the flash categories to
    the UI branch, so neither could categorise it without crossing over.
    """
    resp = logged_in_client.post(
        '/users/new',
        data={'username': 'olive', 'password': 'olive-long-enough-pw'},
        follow_redirects=True,
    )
    body = resp.get_data(as_text=True)
    assert 'User created' in body
    assert 'alert-success' in body


# --- Host-key scan result and history pages (WS-6.2b/6.4) --------------------

def test_a_succeeded_scan_shows_the_fingerprint_and_a_confirm_button(
    logged_in_client, make_scan
):
    scan_id = make_scan(status='succeeded')
    body = logged_in_client.get(f'/hostkeys/scan/{scan_id}').get_data(as_text=True)
    assert 'SHA256:x' in body
    assert 'action="/hostkeys/confirm"' in body
    assert f'name="scan_id" value="{scan_id}"' in body
    assert 'name="csrf_token"' in body
    assert '{{' not in body and '{%' not in body


def test_a_queued_scan_offers_a_reload_link_not_a_confirm_button(
    logged_in_client, make_scan
):
    scan_id = make_scan(status='queued')
    body = logged_in_client.get(f'/hostkeys/scan/{scan_id}').get_data(as_text=True)
    assert 'reload' in body.lower()
    assert 'action="/hostkeys/confirm"' not in body


def test_a_failed_scan_shows_the_error_summary(logged_in_client, make_scan):
    scan_id = make_scan(status='failed', error_summary='could not reach 192.0.2.10:22')
    body = logged_in_client.get(f'/hostkeys/scan/{scan_id}').get_data(as_text=True)
    assert 'could not reach 192.0.2.10:22' in body
    assert 'action="/hostkeys/confirm"' not in body


def test_a_consumed_scan_offers_no_confirm_button(logged_in_client, make_scan):
    scan_id = make_scan(status='succeeded', consumed_at=datetime.now(timezone.utc))
    body = logged_in_client.get(f'/hostkeys/scan/{scan_id}').get_data(as_text=True)
    assert 'action="/hostkeys/confirm"' not in body
    assert 'already been used' in body.lower()


def test_the_history_page_renders_with_no_entries(logged_in_client):
    body = logged_in_client.get('/hostkeys/history/192.0.2.10').get_data(as_text=True)
    assert 'No history' in body
    assert '{{' not in body and '{%' not in body


def test_the_history_page_lists_a_confirm_and_a_delete(logged_in_client, app):
    with app.app_context():
        # logged_in_client already created 'alice' -- look it up rather than
        # creating a second user and colliding with the unique username.
        actor = User.query.filter_by(username='alice').first()
        db.session.add(DeviceHostKeyAudit(
            ansible_host='192.0.2.10', action='confirmed', key_type='ssh-rsa',
            fingerprint_sha256='SHA256:x', actor_id=actor.id,
        ))
        db.session.add(DeviceHostKeyAudit(
            ansible_host='192.0.2.10', action='deleted', key_type='ssh-rsa',
            fingerprint_sha256='SHA256:x', actor_id=actor.id,
        ))
        db.session.commit()
    body = logged_in_client.get('/hostkeys/history/192.0.2.10').get_data(as_text=True)
    assert 'confirmed' in body
    assert 'deleted' in body
    assert 'SHA256:x' in body


# --- WS-11: Pico, no script, confirmation boxes ------------------------------

def test_the_vendored_pico_matches_its_pinned_digest(app):
    """The layout records Pico's version and SHA-256; the file must match, so
    the vendored copy cannot change without a visible diff in the layout."""
    layout, _, _ = app.jinja_env.loader.get_source(app.jinja_env, 'layouts/main.html')
    pinned = re.search(r'pico\.min\.css sha256: ([0-9a-f]{64})', layout)
    assert pinned, 'the layout no longer records the digest'
    path = os.path.join(app.static_folder, 'css', 'pico.min.css')
    with open(path, 'rb') as handle:
        assert hashlib.sha256(handle.read()).hexdigest() == pinned.group(1)


def test_nothing_is_left_under_static_but_the_stylesheets_and_icon(app):
    found = sorted(os.path.relpath(os.path.join(root, name), app.static_folder)
                   for root, _, names in os.walk(app.static_folder) for name in names)
    assert found == ['css/nethub.css', 'css/pico.min.css', 'ico/favicon.png']


def _every_page(client, make_run, make_scan):
    """PAGES, plus the pages that need a row to render."""
    run = make_run(awaiting_phase='activate', failed_phase='stage')
    scan = make_scan(status='queued')
    return PAGES + ['/login', f'/upgrades/{run}', f'/hostkeys/scan/{scan}',
                    '/hostkeys/history/192.0.2.10', '/no-such-page']


def test_no_page_has_a_script_an_inline_style_or_an_event_handler(
    operator, make_run, make_scan
):
    """The CSP blocks all three, so any of them would be dead markup at best,
    and a confirmation that silently stopped confirming at worst."""
    for path in _every_page(operator, make_run, make_scan):
        body = operator.get(path).get_data(as_text=True)
        assert '<script' not in body, path
        assert '<style' not in body, path
        assert not re.search(r'\sstyle=', body), path
        assert not re.search(r'\son[a-z]+=', body), path


def test_every_stylesheet_is_served_by_nethub(operator, make_run, make_scan):
    for path in _every_page(operator, make_run, make_scan):
        body = operator.get(path).get_data(as_text=True)
        for href in re.findall(r'<link rel="stylesheet" href="([^"]+)"', body):
            assert href.startswith('/static/'), (path, href)


def test_every_destructive_form_carries_a_confirmation_box(
    operator, app, make_run, make_artifact, make_user
):
    """Delete an artifact, remove a pin, disable a user, cancel a run, approve
    and retry: each form has the box its route checks (web.confirmed)."""
    from nethub.models import DeviceHostKey
    make_artifact()
    make_user('bob', 'bob-long-enough-pw')
    with app.app_context():
        db.session.add(DeviceHostKey(ansible_host='192.0.2.10', key_type='ssh-rsa',
                                     fingerprint_sha256='SHA256:x'))
        db.session.commit()
    run = make_run(awaiting_phase='activate', failed_phase='stage')
    pages = {
        '/artifacts': '/delete',
        '/hostkeys': '/delete',
        '/users': '/disable',
        f'/upgrades/{run}': ('/approve', '/retry', '/cancel'),
    }
    for path, actions in pages.items():
        body = operator.get(path).get_data(as_text=True)
        for action in (actions if isinstance(actions, tuple) else (actions,)):
            form = _form_containing(body, action)
            assert form, (path, action)
            assert _confirm_box(form), (path, action)


# --- WS-11: the run page says what the sibling is doing ----------------------

def _add_job(app, run_id, status, *, phase='stage', age=timedelta(0), beat_age=None):
    """A job on `run_id`, created `age` ago, last heartbeat `beat_age` ago."""
    now = datetime.now(timezone.utc)
    with app.app_context():
        attempt = 1 + UpgradePhaseJob.query.filter_by(run_id=run_id, phase=phase).count()
        job = UpgradePhaseJob(run_id=run_id, phase=phase, attempt=attempt, status=status,
                              created_at=now - age)
        if status == 'running':
            job.started_at = now - age
            job.heartbeat_at = now - (beat_age if beat_age is not None else timedelta(0))
        db.session.add(job)
        db.session.commit()
        return job.id


def test_the_run_page_refreshes_while_a_job_is_queued(logged_in_client, app, make_run):
    run = make_run(state='running', awaiting_phase=None)
    _add_job(app, run, 'queued')
    body = logged_in_client.get(f'/upgrades/{run}').get_data(as_text=True)
    assert '<meta http-equiv="refresh" content="5">' in body


def test_the_run_page_stops_refreshing_at_a_gate(logged_in_client, app, make_run):
    run = make_run(awaiting_phase='activate')
    _add_job(app, run, 'succeeded')
    body = logged_in_client.get(f'/upgrades/{run}').get_data(as_text=True)
    assert 'http-equiv="refresh"' not in body


def test_a_job_with_a_stale_heartbeat_shows_as_stalled(logged_in_client, app, make_run):
    run = make_run(state='running', awaiting_phase=None)
    _add_job(app, run, 'running', age=timedelta(minutes=10), beat_age=timedelta(minutes=5))
    body = logged_in_client.get(f'/upgrades/{run}').get_data(as_text=True)
    assert 'is stalled' in body
    assert 'nethub-sibling' in body


def test_a_job_with_a_fresh_heartbeat_does_not(logged_in_client, app, make_run):
    run = make_run(state='running', awaiting_phase=None)
    _add_job(app, run, 'running', age=timedelta(minutes=10), beat_age=timedelta(seconds=5))
    body = logged_in_client.get(f'/upgrades/{run}').get_data(as_text=True)
    assert 'stalled' not in body


def test_a_job_queued_with_nobody_working_asks_about_the_sibling(
    logged_in_client, app, make_run
):
    run = make_run(state='pre_checking', awaiting_phase=None)
    _add_job(app, run, 'queued', phase='precheck', age=timedelta(minutes=2))
    body = logged_in_client.get(f'/upgrades/{run}').get_data(as_text=True)
    assert 'No worker has picked this up' in body


def test_a_job_queued_behind_a_live_phase_does_not(logged_in_client, app, make_run):
    busy = make_run(state='running', awaiting_phase=None)
    _add_job(app, busy, 'running', age=timedelta(minutes=30), beat_age=timedelta(seconds=5))
    run = make_run(state='pre_checking', awaiting_phase=None)
    _add_job(app, run, 'queued', phase='precheck', age=timedelta(minutes=2))
    body = logged_in_client.get(f'/upgrades/{run}').get_data(as_text=True)
    assert 'No worker' not in body


def test_a_queued_scan_with_nobody_working_asks_about_the_sibling(
    logged_in_client, app, make_scan
):
    scan = make_scan(status='queued')
    with app.app_context():
        db.session.get(HostKeyScan, scan).created_at = (
            datetime.now(timezone.utc) - timedelta(minutes=2))
        db.session.commit()
    body = logged_in_client.get(f'/hostkeys/scan/{scan}').get_data(as_text=True)
    assert 'No worker has picked this up' in body
    assert '<meta http-equiv="refresh" content="5">' in body


def test_a_finished_scan_does_not_refresh(logged_in_client, make_scan):
    body = logged_in_client.get(f'/hostkeys/scan/{make_scan()}').get_data(as_text=True)
    assert 'http-equiv="refresh"' not in body


def test_the_approve_form_says_what_is_queued_ahead(operator, app, make_run):
    other = make_run(state='running', awaiting_phase=None)
    _add_job(app, other, 'running')
    _add_job(app, other, 'queued', phase='activate')
    _add_job(app, other, 'queued', phase='cleanup')
    run = make_run(awaiting_phase='activate')
    body = operator.get(f'/upgrades/{run}').get_data(as_text=True)
    assert 'Queued behind 2 job(s), with a phase running now' in body


def test_the_approve_form_says_when_nothing_is_ahead(operator, make_run):
    body = operator.get(f'/upgrades/{make_run(awaiting_phase="activate")}').get_data(
        as_text=True)
    assert 'Nothing is queued ahead' in body


# --- WS-11: no device username, told before typing a password ----------------

def test_with_no_device_username_the_new_run_page_says_so_instead_of_a_form(
    logged_in_client
):
    body = logged_in_client.get('/upgrades/new').get_data(as_text=True)
    assert 'no device username set' in body
    assert 'href="/profile"' in body
    assert 'name="device_password"' not in body
    assert 'Submit run' not in body


def test_with_a_device_username_the_new_run_page_has_the_form(operator):
    body = operator.get('/upgrades/new').get_data(as_text=True)
    assert 'no device username set' not in body
    assert 'name="device_password"' in body
    assert 'jsmith' in body


def test_with_no_device_username_the_gate_says_so_instead_of_a_form(
    logged_in_client, make_run
):
    run = make_run(awaiting_phase='activate', failed_phase='stage')
    body = logged_in_client.get(f'/upgrades/{run}').get_data(as_text=True)
    assert 'no device username set' in body
    assert 'href="/profile"' in body
    assert 'name="device_password"' not in body
    assert f'/upgrades/{run}/approve' not in body
    assert f'/upgrades/{run}/retry' not in body
    # Cancel collects no credential, so it stays.
    assert f'/upgrades/{run}/cancel' in body


def test_with_a_device_username_the_gate_has_both_forms(operator, make_run):
    run = make_run(awaiting_phase='activate', failed_phase='stage')
    body = operator.get(f'/upgrades/{run}').get_data(as_text=True)
    assert 'no device username set' not in body
    assert _form_containing(body, f'/upgrades/{run}/approve')
    assert _form_containing(body, f'/upgrades/{run}/retry')


def test_the_row_action_boxes_are_short_and_explain_themselves_on_hover(
    app, logged_in_client, make_user, make_artifact
):
    """Two-word labels; the consequence is in a CSS tooltip (Pico's
    `data-tooltip`, no script) and, for a screen reader, `aria-describedby`."""
    from nethub.models import DeviceHostKey
    make_user('bob', 'bob-long-enough-pw')
    make_artifact(bundle_key='iosxe-17-12-06')
    with app.app_context():
        db.session.add(DeviceHostKey(ansible_host='192.0.2.10', key_type='ssh-rsa',
                                     fingerprint_sha256='SHA256:x'))
        db.session.commit()
    cases = [
        ('/users', '/disable', 'Disable user',
         'Their sessions end at once, and they cannot log in until enabled again.'),
        ('/artifacts', '/delete', 'Delete artifact',
         'Removes iosxe-17-12-06 and its image file from the store.'),
        ('/hostkeys', '/delete', 'Remove pin',
         'Runs naming 192.0.2.10 are refused until it is scanned and confirmed again.'),
    ]
    for path, action, text, explainer in cases:
        form = _form_containing(logged_in_client.get(path).get_data(as_text=True), action)
        label = re.search(r'<label class="confirm"(.*?)</label>', form, re.DOTALL)
        assert re.sub(r'<[^>]+>|\s+', ' ', label.group(0)).strip() == text, path
        assert f'data-tooltip="{explainer}"' in label.group(0), path
        described = re.search(r'aria-describedby="([^"]+)"', label.group(0)).group(1)
        assert f'<small id="{described}" class="explainer">{explainer}</small>' in form, path


# --- WS-15: the reload count and the canary, on the forms that reload --------

def test_the_activate_form_asks_for_a_reload_count_and_names_the_canary(
    operator, app, make_run
):
    app.config['PHASE_CONCURRENCY'] = 6
    run = make_run(awaiting_phase='activate')
    with app.app_context():
        # A second staged host, so there is a canary to name.
        db.session.add(UpgradeRunHost(
            run_id=run, hostname='sw02', position=1, ansible_host='192.0.2.11',
            filename='cat9k_lite_iosxe.17.12.06.SPA.bin', sha512='a' * 128,
            version='17.12.06', file_size=1234, state='staged'))
        db.session.commit()
    body = operator.get(f'/upgrades/{run}').get_data(as_text=True)
    form = _form_containing(body, f'/upgrades/{run}/approve')
    assert re.search(r'<input type="number" id="approve_concurrency" name="concurrency" '
                     r'min="1" max="6"\s+value="1" required', form)
    assert '<strong>sw01</strong> is upgraded alone first, as the canary' in form
    for warning in ('mixed hardware', 'redundant pair', 'Cancel stops further reloads',
                    'does not stop the'):
        assert warning in form, warning
    assert 'Reload 2 device(s) now, the canary first.' in ' '.join(form.split())


def test_a_single_host_has_no_canary_to_name(operator, make_run):
    run = make_run(awaiting_phase='activate')
    form = _form_containing(operator.get(f'/upgrades/{run}').get_data(as_text=True),
                            f'/upgrades/{run}/approve')
    assert 'name="concurrency"' in form
    assert 'as the canary' not in form


def test_only_the_activate_form_asks_for_a_reload_count(operator, make_run):
    run = make_run(awaiting_phase='stage')
    form = _form_containing(operator.get(f'/upgrades/{run}').get_data(as_text=True),
                            f'/upgrades/{run}/approve')
    assert form and 'name="concurrency"' not in form


def test_a_retry_of_activate_asks_for_a_reload_count(operator, make_run):
    run = make_run(awaiting_phase='cleanup', failed_phase='activate')
    form = _form_containing(operator.get(f'/upgrades/{run}').get_data(as_text=True),
                            f'/upgrades/{run}/retry')
    assert 'id="retry_activate_concurrency"' in form
