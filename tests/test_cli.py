"""`nethub/cli.py`: the `artifacts` and `hostkeys` command groups (docs/cli.md)."""

import hashlib
import pathlib

import pytest

from nethub import settings, upgrades
from nethub.extensions import db
from nethub.models import Artifact, DeviceHostKey, HostKeyScan, User

CONTENT = b'cli-image-bytes'
DIGEST = hashlib.sha512(CONTENT).hexdigest()


@pytest.fixture
def cli(app, make_user):
    app.config.update(DEVICE_TARGET_CIDRS=['192.0.2.0/24'])
    make_user('root', 'root-password-long', role='admin')
    make_user('alice', 'alice-password-long', role='operator')
    make_user('bob', 'bob-password-long', role='operator')
    runner = app.test_cli_runner()

    def invoke(*args, password=None, user='alice', extra_input=''):
        args = list(args)
        if password is not False:
            args += ['--as', user]
            extra_input = f'{password or user + "-password-long"}\n{extra_input}'
        return runner.invoke(args=args, input=extra_input)
    return invoke


def rule(app, key, on=True):
    with app.app_context():
        settings.change(key, on, User.query.filter_by(username='root').one())
        db.session.commit()


def uid(name):
    return User.query.filter_by(username=name).one().id


def scan_by(app, name, fingerprint='SHA256:x'):
    with app.app_context():
        scan = HostKeyScan(ansible_host='192.0.2.10', requested_by=uid(name),
                           status='succeeded', key_type='ssh-rsa',
                           fingerprint_sha256=fingerprint, finished_at=upgrades._utcnow())
        db.session.add(scan)
        db.session.commit()
        return scan.id


@pytest.fixture
def image(tmp_path):
    path = tmp_path / 'cat9k_cli.bin'
    path.write_bytes(CONTENT)
    return str(path)


class TestFrontEnd:
    def test_the_cli_imports_nothing_from_the_device_layer(self):
        source = pathlib.Path('nethub/cli.py').read_text()
        imports = [line for line in source.splitlines() if line.startswith(('import', 'from'))]
        assert not [line for line in imports if 'devices' in line]

    def test_a_scan_is_queued_and_opens_no_session(self, app, cli, monkeypatch):
        from nethub.devices import connection

        def hang(*a, **kw):
            raise AssertionError('the CLI must not scan inline')
        monkeypatch.setattr(connection, 'scan_host_key', hang)
        result = cli('hostkeys', 'scan', '192.0.2.10')
        assert result.exit_code == 0, result.output
        with app.app_context():
            scan = HostKeyScan.query.one()
            assert (scan.status, scan.requested_by) == ('queued', uid('alice'))

    @pytest.mark.parametrize('address', ['198.51.100.7', 'sw01.example.net'])
    def test_a_target_outside_the_rules_is_refused(self, app, cli, address):
        result = cli('hostkeys', 'scan', address)
        assert result.exit_code == 1
        with app.app_context():
            assert HostKeyScan.query.count() == 0


class TestActsAsAnAuthenticatedUser:
    @pytest.mark.parametrize('user,password', [('alice', 'wrong-password-long'),
                                               ('nobody', 'whatever-password')])
    def test_a_bad_login_gets_the_login_message_and_writes_nothing(
            self, app, cli, user, password):
        result = cli('hostkeys', 'scan', '192.0.2.10', user=user, password=password)
        assert result.exit_code == 1
        assert 'Invalid username or password.' in result.output
        with app.app_context():
            assert HostKeyScan.query.count() == 0

    def test_a_disabled_account_gets_the_same_message(self, app, cli):
        with app.app_context():
            User.query.filter_by(username='alice').one().is_active = False
            db.session.commit()
        result = cli('hostkeys', 'scan', '192.0.2.10')
        assert result.exit_code == 1
        assert 'Invalid username or password.' in result.output

    def test_read_commands_need_no_login(self, cli):
        assert cli('hostkeys', 'list', password=False).exit_code == 0
        assert cli('artifacts', 'list', password=False).exit_code == 0


class TestArtifacts:
    def test_add_lands_published_with_the_rule_off(self, app, cli, image):
        result = cli('artifacts', 'add', image, '--bundle', 'k1', '--version', '17.12.06',
                     '--sha512', DIGEST)
        assert result.exit_code == 0, result.output
        with app.app_context():
            artifact = Artifact.query.one()
            assert (artifact.state, artifact.uploaded_by) == ('published', uid('alice'))
            assert pathlib.Path(artifact.storage_path).read_bytes() == CONTENT

    def test_under_the_rule_the_uploader_cannot_publish_but_someone_else_can(
            self, app, cli, image):
        rule(app, 'two_person_artifacts')
        cli('artifacts', 'add', image, '--bundle', 'k1', '--version', '1', '--sha512', DIGEST)
        with app.app_context():
            artifact = Artifact.query.one()
            assert artifact.state == 'staged'
            artifact_id = str(artifact.id)
        refused = cli('artifacts', 'publish', artifact_id)
        assert refused.exit_code == 1 and 'someone else' in refused.output
        assert cli('artifacts', 'publish', artifact_id, user='bob').exit_code == 0
        with app.app_context():
            assert Artifact.query.one().state == 'published'

    def test_delete_asks_first_and_declining_changes_nothing(self, app, cli, image):
        cli('artifacts', 'add', image, '--bundle', 'k1', '--version', '1', '--sha512', DIGEST)
        result = cli('artifacts', 'delete', '1', extra_input='n\n')
        assert result.exit_code == 1 and 'Nothing was changed.' in result.output
        assert cli('artifacts', 'delete', '1', '--yes').exit_code == 0
        with app.app_context():
            assert Artifact.query.count() == 0


class TestHostkeys:
    def test_confirm_with_the_restated_fingerprint_pins(self, app, cli):
        scan_id = scan_by(app, 'alice')
        result = cli('hostkeys', 'confirm', str(scan_id), '--fingerprint', 'ssh-rsa SHA256:x')
        assert result.exit_code == 0, result.output
        with app.app_context():
            row = DeviceHostKey.query.one()
            assert row.is_confirmed and row.confirmed_by == uid('alice')

    @pytest.mark.parametrize('fingerprint', ['SHA256:other', 'ssh-ed25519 SHA256:x'])
    def test_a_fingerprint_that_differs_from_the_scan_is_refused(self, app, cli, fingerprint):
        scan_id = scan_by(app, 'alice')
        result = cli('hostkeys', 'confirm', str(scan_id), '--fingerprint', fingerprint)
        assert result.exit_code == 1 and 'Nothing was pinned' in result.output
        with app.app_context():
            assert DeviceHostKey.query.count() == 0
            assert db.session.get(HostKeyScan, scan_id).consumed_at is None

    def test_the_two_person_rule_applies(self, app, cli):
        rule(app, 'two_person_hostkeys')
        scan_id = str(scan_by(app, 'alice'))
        refused = cli('hostkeys', 'confirm', scan_id, '--fingerprint', 'SHA256:x')
        assert refused.exit_code == 1 and 'two-person rule' in refused.output
        assert cli('hostkeys', 'confirm', scan_id, '--fingerprint', 'SHA256:x',
                   user='bob').exit_code == 0
