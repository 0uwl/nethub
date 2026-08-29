import os
import shutil
import tempfile

import pytest

# Config reads these env vars at import time, so they must be set before
# `nethub` (or anything under it) is ever imported.
_tmp_dir = tempfile.mkdtemp()
os.environ['SECRET_KEY'] = 'test-secret'
os.environ['DATABASE_PATH'] = os.path.join(_tmp_dir, 'test.db')
os.environ['IMAGE_DIR'] = os.path.join(_tmp_dir, 'registry', 'images')
os.environ['REGISTRY_FILE'] = os.path.join(_tmp_dir, 'registry', 'software_registry.yml')

from nethub import create_app  # noqa: E402
from nethub.extensions import db  # noqa: E402
from nethub.models import User  # noqa: E402


@pytest.fixture
def app():
    # registry.py writes files under IMAGE_DIR/REGISTRY_FILE directly,
    # outside the db, so they need the same fresh-per-test treatment.
    shutil.rmtree(os.environ['IMAGE_DIR'], ignore_errors=True)
    if os.path.exists(os.environ['REGISTRY_FILE']):
        os.remove(os.environ['REGISTRY_FILE'])
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
