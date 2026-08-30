import os
import shutil
import tempfile
from types import SimpleNamespace

import pytest

# Config reads these env vars at import time, so they must be set before
# `nethub` (or anything under it) is ever imported.
_tmp_dir = tempfile.mkdtemp()
os.environ['SECRET_KEY'] = 'test-secret'
os.environ['DATABASE_PATH'] = os.path.join(_tmp_dir, 'test.db')
os.environ['REGISTRIES_ROOT'] = os.path.join(_tmp_dir, 'registries')

from nethub import create_app
from nethub.extensions import db
from nethub.models import Registry, User


@pytest.fixture
def app():
    # registry.py writes files under REGISTRIES_ROOT directly, outside the
    # db, so it needs the same fresh-per-test treatment.
    shutil.rmtree(os.environ['REGISTRIES_ROOT'], ignore_errors=True)
    os.makedirs(os.environ['REGISTRIES_ROOT'])
    flask_app = create_app()
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    yield flask_app
    with flask_app.app_context():
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def make_user(app):
    """Create a user with a known password, inside the app's db."""
    def _make(username='alice', password='hunter2'):
        with app.app_context():
            user = User(username=username)
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
        return username, password
    return _make


@pytest.fixture
def logged_in_client(client, make_user):
    username, password = make_user()
    client.post('/login', data={'username': username, 'password': password})
    return client


@pytest.fixture
def make_registry(app):
    """Create a tracked Registry row backed by a real file under
    REGISTRIES_ROOT and a writable search_dir.

    Returns a plain namespace (id, name, file_path, search_dir) rather than
    the ORM object itself -- accessing the ORM object outside the
    app_context that created it hits a detached-session error, same reason
    make_user returns username/password rather than the User row.
    """
    def _make(filename='os_iosxe.yml', content='', name='iosxe', search_dir=None):
        root = app.config['REGISTRIES_ROOT']
        with open(os.path.join(root, filename), 'w') as f:
            f.write(content)
        if search_dir is None:
            search_dir = tempfile.mkdtemp()
        with app.app_context():
            registry = Registry(name=name, file_path=filename, search_dir=search_dir)
            db.session.add(registry)
            db.session.commit()
            registry_id = registry.id
        return SimpleNamespace(id=registry_id, name=name, file_path=filename, search_dir=search_dir)
    return _make
