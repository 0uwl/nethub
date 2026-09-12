from nethub import bootstrap
from nethub.models import User


def test_credential_beats_env_and_generated(app, monkeypatch):
    monkeypatch.setattr(bootstrap, 'read_credential', lambda name: 'from-credential')
    monkeypatch.setenv('ADMIN_PASSWORD', 'from-env')
    password, source = bootstrap._initial_admin_password()
    assert (password, source) == ('from-credential', 'credential')


def test_env_beats_generated_when_no_credential(app, monkeypatch):
    monkeypatch.setattr(bootstrap, 'read_credential', lambda name: None)
    monkeypatch.setenv('ADMIN_PASSWORD', 'from-env')
    password, source = bootstrap._initial_admin_password()
    assert (password, source) == ('from-env', 'env')


def test_generated_when_nothing_set(app, monkeypatch):
    monkeypatch.setattr(bootstrap, 'read_credential', lambda name: None)
    monkeypatch.delenv('ADMIN_PASSWORD', raising=False)
    password, source = bootstrap._initial_admin_password()
    assert source == 'generated'
    assert len(password) == bootstrap.ADMIN_PASSWORD_LENGTH


def test_bootstrap_admin_skips_when_users_exist(app):
    with app.app_context():
        count_before = User.query.count()
        assert count_before == 1  # create_app() already bootstrapped one
        bootstrap.bootstrap_admin()
        assert User.query.count() == count_before
