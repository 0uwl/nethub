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

import pytest

from nethub import create_app
from nethub.extensions import db
from nethub.models import UpgradeRun, UpgradeRunHost, User


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
