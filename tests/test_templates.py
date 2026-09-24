"""Rendering checks for the Jinja templates.

There were none of these at all: two route tests checked a status code and
nothing inspected a response body, so a broken `url_for`, a renamed context
variable or a CSRF token vanishing from a form would all have passed.

The CSRF check needs its own app, because the shared `app` fixture sets
WTF_CSRF_ENABLED=False -- which is what made a missing token invisible.
"""
import os
import re
import shutil
from datetime import datetime, timezone

import pytest

from nethub import create_app
from nethub.extensions import db
from nethub.models import DeviceHostKeyAudit, HostKeyScan, UpgradeRun, UpgradeRunHost, User


@pytest.fixture
def make_run(app, make_user):
    """A run parked at a gate, so the approval form actually renders.

    Built here rather than in conftest.py: only these tests need it, and
    conftest is the one file every parallel branch would otherwise touch.
    """
    def _make(state='awaiting_approval', awaiting_phase='activate'):
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
                image_transport_used='push_scp',
                request_document='{}',
                request_sha512='0' * 128,
                state=state,
                awaiting_phase=awaiting_phase,
            )
            db.session.add(run)
            db.session.flush()
            db.session.add(UpgradeRunHost(
                run_id=run.id, hostname='sw01', ansible_host='192.0.2.10',
                filename='cat9k_lite_iosxe.17.12.06.SPA.bin',
                sha512='a' * 128, version='17.12.06', file_size=1234,
            ))
            db.session.commit()
            return run.id
    return _make


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

def test_the_activate_approval_form_confirms(logged_in_client, make_run):
    run = make_run(state='awaiting_approval', awaiting_phase='activate')
    body = logged_in_client.get(f'/upgrades/{run}').get_data(as_text=True)
    form = _form_containing(body, 'upgrades.approve'.replace('.', '/')) or body
    assert 'onsubmit' in form and 'confirm(' in form
    assert 'Reload' in form


def test_the_confirmation_names_the_host_count(logged_in_client, make_run):
    run = make_run(state='awaiting_approval', awaiting_phase='activate')
    body = logged_in_client.get(f'/upgrades/{run}').get_data(as_text=True)
    # One host in the fixture, so the copy has to say 1 rather than a blank.
    assert 'Reload 1 device(s)' in body


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
def csrf_app():
    shutil.rmtree(os.environ['ARTIFACT_STORE'], ignore_errors=True)
    os.makedirs(os.environ['ARTIFACT_STORE'])
    flask_app = create_app()
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=True)
    yield flask_app
    with flask_app.app_context():
        db.session.remove()
        db.drop_all()


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
        wrappers = body.count('table-responsive')
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
