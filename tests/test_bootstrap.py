from nethub import bootstrap
from nethub.models import User, UserAdminAudit


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


def test_bootstrap_admin_skips_when_users_exist(app, make_user, monkeypatch):
    make_user()
    monkeypatch.setenv('ADMIN_USERNAME', 'someone')
    with app.app_context():
        bootstrap.bootstrap_admin()
        assert [u.username for u in User.query.all()] == ['alice']


def test_there_is_no_default_admin_username(app, monkeypatch, capsys):
    """PLAN.md WS-10: a well-known `admin` let anyone keep it locked out."""
    monkeypatch.delenv('ADMIN_USERNAME', raising=False)
    with app.app_context():
        assert User.query.count() == 0, 'create_app() created nobody'
        bootstrap.bootstrap_admin()
        assert User.query.count() == 0
    assert 'create-admin' in capsys.readouterr().out


def test_admin_username_creates_the_first_user_and_records_it(app, monkeypatch):
    monkeypatch.setenv('ADMIN_USERNAME', 'ops-lead')
    monkeypatch.setenv('ADMIN_PASSWORD', 'ops-lead-long-password')
    monkeypatch.setattr(bootstrap, 'read_credential', lambda name: None)
    with app.app_context():
        bootstrap.bootstrap_admin()
        user = User.query.one()
        assert user.username == 'ops-lead'
        assert user.check_password('ops-lead-long-password')
        entry = UserAdminAudit.query.one()
        assert (entry.action, entry.target_user_id, entry.actor_user_id, entry.detail) == (
            'created', user.id, None, 'first-boot bootstrap')
