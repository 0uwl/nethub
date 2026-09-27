"""User management (PLAN.md WS-10): disabling, session revocation, password
changes and resets, unlocking, and the append-only audit of all of them.

Two clients stand for two browsers, so "their session ends" is checked the
way it happens: a request with an existing cookie gets bounced to /login.
"""

import sqlite3

import pytest
from sqlalchemy.exc import IntegrityError

from nethub import auth
from nethub.extensions import db
from nethub.models import User, UserAdminAudit


def login(client, username, password):
    return client.post('/login', data={'username': username, 'password': password})


def logged_in(client):
    """A page every signed-in user can open; /login bounces signed-out ones."""
    return client.get('/users').status_code == 200


def user_id(app, username):
    with app.app_context():
        return User.query.filter_by(username=username).one().id


def audit(app):
    with app.app_context():
        return [(e.action, e.target_user_id, e.actor_user_id)
                for e in UserAdminAudit.query.order_by(UserAdminAudit.id)]


@pytest.fixture
def bob(app, make_user):
    """A second user, logged in on a second client."""
    make_user('bob', 'bob-long-enough-pw')
    other = app.test_client()
    login(other, 'bob', 'bob-long-enough-pw')
    assert logged_in(other)
    return other


class TestDisable:
    def test_a_disabled_users_existing_session_ends_on_its_next_request(
            self, app, logged_in_client, bob):
        """WS-10's "done when"."""
        bob_id = user_id(app, 'bob')
        assert logged_in_client.post(f'/users/{bob_id}/disable').status_code == 302
        response = bob.get('/users')
        assert response.status_code == 302
        assert '/login' in response.headers['Location']

    def test_a_disabled_user_cannot_log_in_and_gets_the_usual_message(
            self, app, logged_in_client, bob):
        logged_in_client.post(f'/users/{user_id(app, "bob")}/disable')
        response = login(app.test_client(), 'bob', 'bob-long-enough-pw')
        assert response.status_code == 200
        assert b'Invalid username or password' in response.data

    def test_a_disabled_login_still_pays_for_the_hash(self, app, logged_in_client,
                                                     bob, monkeypatch):
        """Refusing a disabled account before checking its password would be
        a timing oracle for "this account exists and is disabled"."""
        logged_in_client.post(f'/users/{user_id(app, "bob")}/disable')
        calls = []
        real = User.check_password
        monkeypatch.setattr(User, 'check_password',
                            lambda self, pw: calls.append(pw) or real(self, pw))
        login(app.test_client(), 'bob', 'bob-long-enough-pw')
        assert len(calls) == 1

    def test_you_cannot_disable_yourself(self, app, logged_in_client):
        alice = user_id(app, 'alice')
        response = logged_in_client.post(f'/users/{alice}/disable', follow_redirects=True)
        assert b'cannot disable your own account' in response.data
        with app.app_context():
            assert db.session.get(User, alice).is_active

    def test_the_last_active_user_is_never_disabled(self, app, logged_in_client, bob,
                                                    monkeypatch):
        """Two users disabling each other at once: each request passed its
        checks before the other committed. Simulated by disabling alice in the
        database while her request to disable bob is in flight; the check in
        the UPDATE then finds nobody else active and changes nothing."""
        alice, bob_id = user_id(app, 'alice'), user_id(app, 'bob')
        real = auth._target

        def concurrent(target_id):
            target = real(target_id)
            db.session.execute(db.text('UPDATE user SET is_active = 0 WHERE id = :id'),
                               {'id': alice})
            return target

        monkeypatch.setattr(auth, '_target', concurrent)
        response = logged_in_client.post(f'/users/{bob_id}/disable', follow_redirects=True)
        assert b'last active user' in response.data
        with app.app_context():
            assert db.session.get(User, bob_id).is_active
            assert UserAdminAudit.query.filter_by(action='disabled').count() == 0

    def test_enable_lets_them_back_in(self, app, logged_in_client, bob):
        bob_id = user_id(app, 'bob')
        logged_in_client.post(f'/users/{bob_id}/disable')
        logged_in_client.post(f'/users/{bob_id}/enable')
        assert login(app.test_client(), 'bob', 'bob-long-enough-pw').status_code == 302
        assert [a[0] for a in audit(app)][-2:] == ['disabled', 'enabled']

    def test_enabling_does_not_revive_the_old_session(self, app, logged_in_client, bob):
        bob_id = user_id(app, 'bob')
        logged_in_client.post(f'/users/{bob_id}/disable')
        logged_in_client.post(f'/users/{bob_id}/enable')
        assert not logged_in(bob)


class TestSessions:
    def test_a_cookie_without_an_epoch_is_refused(self, app, client, make_user):
        """Cookies issued before WS-10 carry a bare id; everyone logs in once."""
        make_user()
        with client.session_transaction() as sess:
            sess['_user_id'] = str(user_id(app, 'alice'))
            sess['_fresh'] = True
        assert not logged_in(client)

    def test_a_cookie_with_a_stale_epoch_is_refused(self, app, logged_in_client):
        with app.app_context():
            user = User.query.filter_by(username='alice').one()
            user.revoke_sessions()
            db.session.commit()
        assert not logged_in(logged_in_client)

    def test_an_inactive_row_ends_the_session_even_with_the_right_epoch(
            self, app, logged_in_client):
        """The disable route also bumps the epoch, so this pins the other two
        layers, which a row changed by any other path relies on: the loader
        refuses an inactive user, and Flask-Login's `is_authenticated` is
        `is_active` too, so `login_required` would refuse one anyway."""
        with app.app_context():
            User.query.filter_by(username='alice').one().is_active = False
            db.session.commit()
        assert not logged_in(logged_in_client)

    def test_the_session_id_carries_the_epoch(self, app, logged_in_client):
        with logged_in_client.session_transaction() as sess:
            assert sess['_user_id'] == f'{user_id(app, "alice")}:0'


class TestOwnPassword:
    def change(self, client, current='hunter2', new='a-new-long-password',
               confirm=None):
        return client.post('/profile/password', data={
            'current_password': current, 'new_password': new,
            'confirm_password': new if confirm is None else confirm,
        }, follow_redirects=True)

    def test_changing_it_ends_your_other_sessions_but_not_this_one(
            self, app, make_user, client):
        make_user()
        other = app.test_client()
        login(client, 'alice', 'hunter2')
        login(other, 'alice', 'hunter2')
        response = self.change(client)
        assert b'Password changed' in response.data
        assert logged_in(client), 'this session carries on'
        assert not logged_in(other), 'the other one ended'
        assert login(app.test_client(), 'alice', 'a-new-long-password').status_code == 302
        assert audit(app)[-1] == ('password_changed', user_id(app, 'alice'),
                                  user_id(app, 'alice'))

    def test_a_wrong_current_password_is_refused_and_counted(self, app, logged_in_client):
        response = self.change(logged_in_client, current='guess')
        assert b'current password was not accepted' in response.data
        with app.app_context():
            user = User.query.filter_by(username='alice').one()
            assert user.check_password('hunter2')
            assert user.failed_logins == 1

    def test_guessing_the_current_password_locks_the_account(self, app, logged_in_client):
        for _ in range(auth.MAX_FAILED_LOGINS):
            self.change(logged_in_client, current='guess')
        response = self.change(logged_in_client)  # the right one, too late
        assert b'current password was not accepted' in response.data
        with app.app_context():
            assert User.query.filter_by(username='alice').one().check_password('hunter2')

    def test_mismatched_or_short_passwords_are_refused(self, app, logged_in_client):
        assert b'do not match' in self.change(logged_in_client, confirm='other-long-pw').data
        assert b'at least' in self.change(logged_in_client, new='short').data
        with app.app_context():
            assert User.query.filter_by(username='alice').one().check_password('hunter2')


class TestAdminReset:
    def test_a_reset_sets_the_password_and_ends_their_sessions(
            self, app, logged_in_client, bob):
        bob_id = user_id(app, 'bob')
        response = logged_in_client.post(f'/users/{bob_id}/reset-password',
                                         data={'password': 'bob-new-long-password'},
                                         follow_redirects=True)
        assert b'sessions are ended' in response.data
        assert not logged_in(bob)
        assert login(app.test_client(), 'bob', 'bob-new-long-password').status_code == 302
        assert audit(app)[-1] == ('password_reset', bob_id, user_id(app, 'alice'))

    def test_a_reset_clears_a_lockout(self, app, logged_in_client, bob):
        bob_id = user_id(app, 'bob')
        with app.app_context():
            user = db.session.get(User, bob_id)
            for _ in range(auth.MAX_FAILED_LOGINS):
                user.register_failed_login(limit=auth.MAX_FAILED_LOGINS,
                                           lockout=auth.LOCKOUT_DURATION)
            db.session.commit()
        logged_in_client.post(f'/users/{bob_id}/reset-password',
                              data={'password': 'bob-new-long-password'})
        assert login(app.test_client(), 'bob', 'bob-new-long-password').status_code == 302

    def test_your_own_password_is_changed_on_your_profile(self, app, logged_in_client):
        response = logged_in_client.post(f'/users/{user_id(app, "alice")}/reset-password',
                                         data={'password': 'a-new-long-password'})
        assert response.headers['Location'].endswith('/profile')
        with app.app_context():
            assert User.query.filter_by(username='alice').one().check_password('hunter2')

    def test_a_short_password_is_refused(self, app, logged_in_client, bob):
        response = logged_in_client.post(f'/users/{user_id(app, "bob")}/reset-password',
                                         data={'password': 'short'})
        assert b'at least' in response.data


class TestUnlock:
    def test_unlock_clears_the_lockout(self, app, logged_in_client, bob):
        bob_id = user_id(app, 'bob')
        client = app.test_client()
        for _ in range(auth.MAX_FAILED_LOGINS):
            login(client, 'bob', 'wrong')
        assert login(client, 'bob', 'bob-long-enough-pw').status_code == 200, 'locked'
        page = logged_in_client.get('/users').get_data(as_text=True)
        assert 'locked until' in page and f'/users/{bob_id}/unlock' in page
        logged_in_client.post(f'/users/{bob_id}/unlock')
        assert login(client, 'bob', 'bob-long-enough-pw').status_code == 302
        assert audit(app)[-1] == ('unlocked', bob_id, user_id(app, 'alice'))


class TestAudit:
    def test_creating_a_user_is_recorded_with_who_did_it(self, app, logged_in_client):
        logged_in_client.post('/users/new', data={'username': 'carol',
                                                  'password': 'carol-long-enough-pw'})
        assert audit(app) == [('created', user_id(app, 'carol'), user_id(app, 'alice'))]

    def test_the_command_line_is_recorded_as_no_user(self, app):
        app.test_cli_runner().invoke(
            args=['create-admin', 'dave'], input='dave-long-enough-pw\ndave-long-enough-pw\n')
        with app.app_context():
            entry = UserAdminAudit.query.one()
            assert (entry.action, entry.actor_user_id, entry.detail) == (
                'created', None, 'create-admin command')

    def test_the_table_is_append_only(self, app, logged_in_client):
        logged_in_client.post('/users/new', data={'username': 'carol',
                                                  'password': 'carol-long-enough-pw'})
        with app.app_context():
            for statement in ("UPDATE user_admin_audit SET action = 'enabled'",
                              "DELETE FROM user_admin_audit"):
                with pytest.raises(IntegrityError, match='append-only'):
                    db.session.execute(db.text(statement))
                db.session.rollback()
            assert UserAdminAudit.query.count() == 1

    def test_no_password_reaches_the_audit_table(self, app, logged_in_client, bob):
        logged_in_client.post(f'/users/{user_id(app, "bob")}/reset-password',
                              data={'password': 'bob-new-long-password'})
        path = app.config['SQLALCHEMY_DATABASE_URI'].removeprefix('sqlite:///')
        rows = sqlite3.connect(path).execute('SELECT * FROM user_admin_audit').fetchall()
        assert rows and 'bob-new-long-password' not in repr(rows)

    def test_the_history_page_lists_what_happened(self, app, logged_in_client, bob):
        bob_id = user_id(app, 'bob')
        logged_in_client.post(f'/users/{bob_id}/disable')
        page = logged_in_client.get(f'/users/{bob_id}/history').get_data(as_text=True)
        assert 'disabled' in page and 'alice' in page
        assert '{{' not in page and '{%' not in page


class TestUsersPage:
    def test_every_action_form_carries_a_csrf_token(self, app, logged_in_client, bob):
        page = logged_in_client.get('/users').get_data(as_text=True)
        forms = page.count('method="post"')
        assert forms >= 1
        assert page.count('name="csrf_token"') >= forms

    def test_you_are_offered_no_action_against_yourself(self, app, logged_in_client):
        page = logged_in_client.get('/users').get_data(as_text=True)
        alice = user_id(app, 'alice')
        assert f'/users/{alice}/disable' not in page
        assert f'/users/{alice}/reset-password' not in page

    def test_the_profile_page_offers_a_password_change(self, logged_in_client):
        page = logged_in_client.get('/profile').get_data(as_text=True)
        assert 'action="/profile/password"' in page
        assert 'name="current_password"' in page
