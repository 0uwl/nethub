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
        '/users/new', data={'username': 'carol', 'password': 'sekrit'}
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
        args=['create-admin', 'dave'], input='pw123\npw123\n'
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
