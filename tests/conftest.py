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
os.environ['ARTIFACT_STORE'] = os.path.join(_tmp_dir, 'artifacts')

from nethub import create_app
from nethub.extensions import db
from nethub.models import Artifact, User


@pytest.fixture
def app():
    # artifacts.py writes image bytes under ARTIFACT_STORE, outside the db,
    # so the store needs the same fresh-per-test treatment as the database.
    shutil.rmtree(os.environ['ARTIFACT_STORE'], ignore_errors=True)
    os.makedirs(os.environ['ARTIFACT_STORE'])
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
def make_artifact(app):
    """Publish an artifact with real bytes on disk, mirroring make_user."""
    def _make(bundle_key='iosxe-17-12-06', version='17.12.06',
              filename='cat9k_lite_iosxe.17.12.06.SPA.bin', content=b'image-bytes'):
        import hashlib
        with app.app_context():
            store = os.environ['ARTIFACT_STORE']
            os.makedirs(store, exist_ok=True)
            path = os.path.join(store, filename)
            with open(path, 'wb') as handle:
                handle.write(content)
            artifact = Artifact(
                kind='image', platform='iosxe', bundle_key=bundle_key,
                filename=filename, sha512=hashlib.sha512(content).hexdigest(),
                file_size=len(content), storage_path=path, version=version,
                state='published', bytes_state='present',
            )
            db.session.add(artifact)
            db.session.commit()
            return SimpleNamespace(
                id=artifact.id, bundle_key=bundle_key, filename=filename,
                version=version, sha512=artifact.sha512, path=path,
                file_size=len(content),
            )
    return _make
