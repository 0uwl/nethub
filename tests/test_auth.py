def test_login_page_loads(client):
    assert client.get('/login').status_code == 200


def test_login_with_wrong_password_fails(client, make_user):
    username, _ = make_user()
    resp = client.post('/login', data={'username': username, 'password': 'nope'})
    assert resp.status_code == 200  # re-renders form, no redirect
    assert b'Invalid username or password' in resp.data


def test_login_with_correct_password_redirects_to_artifacts(client, make_user):
    username, password = make_user()
    resp = client.post('/login', data={'username': username, 'password': password})
    assert resp.status_code == 302
    assert resp.headers['Location'] == '/artifacts'


def test_logout_requires_login(client):
    assert client.post('/logout').status_code == 302  # bounced to login


def test_logout_clears_session(logged_in_client):
    resp = logged_in_client.post('/logout')
    assert resp.status_code == 302
    # protected page no longer reachable
    assert logged_in_client.get('/users').status_code == 302


def test_users_list_requires_login(client):
    assert client.get('/users').status_code == 302


def test_users_list_shows_users(logged_in_client):
    resp = logged_in_client.get('/users')
    assert resp.status_code == 200
    assert b'alice' in resp.data


def test_new_user_requires_login(client):
    assert client.get('/users/new').status_code == 302


def test_new_user_creates_account(logged_in_client, app):
    from nethub.models import User

    resp = logged_in_client.post(
        '/users/new', data={'username': 'carol', 'password': 'carol-long-enough-pw'}
    )
    assert resp.status_code == 302
    with app.app_context():
        assert User.query.filter_by(username='carol').first() is not None


def test_new_user_rejects_missing_fields(logged_in_client):
    resp = logged_in_client.post('/users/new', data={'username': '', 'password': ''})
    assert resp.status_code == 200
    assert b'both required' in resp.data


def test_new_user_rejects_duplicate_username(logged_in_client):
    resp = logged_in_client.post(
        '/users/new', data={'username': 'alice', 'password': 'whatever'}
    )
    assert resp.status_code == 200
    assert b'already taken' in resp.data


def test_create_admin_cli_creates_user(app):
    runner = app.test_cli_runner()
    result = runner.invoke(
        args=['create-admin', 'dave'], input='dave-long-enough-pw\ndave-long-enough-pw\n'
    )
    assert 'Created user "dave"' in result.output
    with app.app_context():
        from nethub.models import User

        assert User.query.filter_by(username='dave').first() is not None


def test_create_admin_cli_rejects_existing_user(app, make_user):
    make_user(username='eve')
    runner = app.test_cli_runner()
    result = runner.invoke(args=['create-admin', 'eve'], input='pw123\npw123\n')
    assert 'already exists' in result.output


# --- Login hardening (WS-5.5) ------------------------------------------------

def test_an_absent_username_still_pays_for_a_password_hash(client, monkeypatch):
    """The timing oracle was the control flow, so assert the flow, not a clock.

    Measuring elapsed time here would be flaky; what matters is that the
    absent-user branch reaches a verification at all. Before this, a missing
    user returned before any hashing and answered ~74x faster.
    """
    from nethub import auth

    calls = []
    real = auth.check_password_hash
    monkeypatch.setattr(
        auth, 'check_password_hash',
        lambda h, p: (calls.append(h), real(h, p))[1],
    )
    client.post('/login', data={'username': 'nobody-at-all', 'password': 'x'})
    assert calls == [auth._ABSENT_USER_HASH]


def test_the_absent_user_hash_has_the_same_kdf_as_a_real_one(app, make_user):
    """A cheaper dummy hash would leave the gap it exists to close."""
    from nethub import auth
    from nethub.models import User

    make_user(username='frank', password='frank-long-enough-pw')
    with app.app_context():
        real = User.query.filter_by(username='frank').first().password_hash
    # werkzeug encodes as `method$salt$hash`; the method carries the cost params.
    assert auth._ABSENT_USER_HASH.split('$')[0] == real.split('$')[0]


def test_repeated_failures_lock_the_account(client, app, make_user):
    from nethub import auth
    from nethub.models import User

    username, password = make_user(username='grace', password='grace-long-pw')
    for _ in range(auth.MAX_FAILED_LOGINS):
        client.post('/login', data={'username': username, 'password': 'wrong'})

    with app.app_context():
        assert User.query.filter_by(username=username).first().is_locked()

    # The correct password is refused while the lock holds...
    resp = client.post('/login', data={'username': username, 'password': password})
    assert resp.status_code == 200
    # ...and the refusal does not say the account exists.
    assert b'locked' not in resp.data.lower()
    assert b'Invalid username or password' in resp.data


def test_a_lock_expires(client, app, make_user):
    from datetime import datetime, timedelta, timezone

    from nethub.extensions import db
    from nethub.models import User

    username, password = make_user(username='heidi', password='heidi-long-pw')
    with app.app_context():
        user = User.query.filter_by(username=username).first()
        user.locked_until = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.session.commit()
        assert not user.is_locked()

    assert client.post(
        '/login', data={'username': username, 'password': password}
    ).status_code == 302


def test_a_successful_login_clears_the_counter(client, app, make_user):
    from nethub.models import User

    username, password = make_user(username='ivan', password='ivan-long-pw')
    for _ in range(3):
        client.post('/login', data={'username': username, 'password': 'wrong'})
    with app.app_context():
        assert User.query.filter_by(username=username).first().failed_logins == 3

    client.post('/login', data={'username': username, 'password': password})
    with app.app_context():
        user = User.query.filter_by(username=username).first()
        assert user.failed_logins == 0
        assert user.locked_until is None


def test_an_absent_username_does_not_create_a_counter_row(client, app):
    """§4.2: a counter keyed on attacker-chosen input is unbounded growth."""
    from nethub.models import User

    before = None
    with app.app_context():
        before = User.query.count()
    for i in range(5):
        client.post('/login', data={'username': f'ghost-{i}', 'password': 'x'})
    with app.app_context():
        assert User.query.count() == before


def test_a_failed_login_is_logged(client, make_user, caplog):
    username, _ = make_user(username='judy', password='judy-long-pw')
    with caplog.at_level('WARNING'):
        client.post('/login', data={'username': username, 'password': 'wrong'})
    assert any('failed login' in r.getMessage() for r in caplog.records)


def test_the_submitted_password_is_never_logged(client, make_user, caplog):
    username, _ = make_user(username='ken', password='ken-long-enough-pw')
    with caplog.at_level('WARNING'):
        client.post('/login', data={'username': username, 'password': 'S3cretGuess'})
    assert 'S3cretGuess' not in caplog.text


def test_login_marks_the_session_permanent(client, make_user):
    """PERMANENT_SESSION_LIFETIME is inert unless the session is permanent."""
    username, password = make_user(username='lena', password='lena-long-pw')
    with client:
        from flask import session
        client.post('/login', data={'username': username, 'password': password})
        assert session.permanent is True


def test_new_user_rejects_a_short_password(logged_in_client):
    resp = logged_in_client.post(
        '/users/new', data={'username': 'mallory', 'password': 'short'}
    )
    assert resp.status_code == 200
    assert b'at least 12 characters' in resp.data


def test_create_admin_cli_rejects_a_short_password(app):
    runner = app.test_cli_runner()
    result = runner.invoke(args=['create-admin', 'nate'], input='short\nshort\n')
    assert 'at least 12 characters' in result.output
    with app.app_context():
        from nethub.models import User

        assert User.query.filter_by(username='nate').first() is None
