"""Roles and the two-person rules.

Each user gets their own client, standing for their own browser. alice and
bob are operators, root is an admin. Everything goes through the routes,
since the routes are where a refusal has to hold.
"""

import hashlib
import io
import os
import sqlite3

import pytest
from flask import current_app

from nethub import settings, upgrades
from nethub.extensions import db
from nethub.models import (
    Artifact,
    ArtifactAudit,
    DeviceHostKey,
    DeviceHostKeyAudit,
    HostKeyScan,
    SettingsAudit,
    UpgradePhaseJob,
    UpgradeRun,
    User,
    UserAdminAudit,
)

PASSWORD = 'long-enough-password'
DEVICE_PASSWORD = 'd3vice-pass'
CONFIRM = {'confirm': 'yes'}
CONTENT = b'image-bytes'
DIGEST = hashlib.sha512(CONTENT).hexdigest()


@pytest.fixture
def app(app):
    app.config.update(DEVICE_TARGET_CIDRS=['192.0.2.0/24'])
    return app


@pytest.fixture
def people(app, make_user):
    """Three logged-in clients: alice and bob are operators, root an admin."""
    clients = {}
    for name, role in (('root', 'admin'), ('alice', 'operator'), ('bob', 'operator')):
        make_user(name, PASSWORD, role=role)
        with app.app_context():
            User.query.filter_by(username=name).one().device_username = f'{name}-dev'
            db.session.commit()
        client = app.test_client()
        client.post('/login', data={'username': name, 'password': PASSWORD})
        clients[name] = client
    return clients


def uid(name):
    return User.query.filter_by(username=name).one().id


def rule(app, key, on, by='root'):
    with app.app_context():
        settings.change(key, on, User.query.filter_by(username=by).one())
        db.session.commit()


def flashed(response):
    return response.get_data(as_text=True)


# -- roles ----------------------------------------------------------------------

ADMIN_ONLY = [
    ('get', '/users'), ('get', '/users/new'), ('post', '/users/new'),
    ('get', '/users/{id}/reset-password'), ('post', '/users/{id}/reset-password'),
    ('post', '/users/{id}/disable'), ('post', '/users/{id}/enable'),
    ('post', '/users/{id}/unlock'), ('post', '/users/{id}/role'),
    ('get', '/users/{id}/history'), ('get', '/settings'), ('post', '/settings'),
]


class TestRoles:
    @pytest.mark.parametrize('method,path', ADMIN_ONLY)
    def test_an_operator_is_refused_every_admin_only_route(self, app, people, method, path):
        with app.app_context():
            target = uid('bob')
        data = {'role': 'admin', 'confirm': 'yes', 'two_person_runs': 'on',
                'username': 'mallory', 'password': PASSWORD}
        response = getattr(people['alice'], method)(path.format(id=target), data=data)
        assert response.status_code == 403
        with app.app_context():
            assert User.query.filter_by(username='bob').one().role == 'operator'
            assert User.query.filter_by(username='mallory').first() is None
            assert not settings.enabled('two_person_runs')

    def test_an_operator_sees_no_admin_links(self, people):
        page = flashed(people['alice'].get('/upgrades'))
        assert '/users' not in page and '/settings' not in page
        assert '/settings' in flashed(people['root'].get('/upgrades'))

    def test_a_user_created_in_the_ui_is_an_operator_unless_picked(self, app, people):
        people['root'].post('/users/new', data={'username': 'carol', 'password': PASSWORD})
        people['root'].post('/users/new', data={'username': 'dave', 'password': PASSWORD,
                                                'role': 'admin'})
        with app.app_context():
            assert User.query.filter_by(username='carol').one().role == 'operator'
            assert User.query.filter_by(username='dave').one().role == 'admin'

    def test_a_role_change_is_audited_and_applies_on_the_next_request(self, app, people):
        with app.app_context():
            alice = uid('alice')
        people['root'].post(f'/users/{alice}/role', data={'role': 'admin', **CONFIRM})
        assert people['alice'].get('/users').status_code == 200
        people['root'].post(f'/users/{alice}/role', data={'role': 'operator', **CONFIRM})
        assert people['alice'].get('/users').status_code == 403
        with app.app_context():
            assert [(e.action, e.detail) for e in UserAdminAudit.query.filter_by(
                target_user_id=alice, action='role_changed').order_by(UserAdminAudit.id)] == [
                ('role_changed', 'operator to admin'), ('role_changed', 'admin to operator')]

    def test_you_cannot_change_your_own_role(self, app, people):
        with app.app_context():
            root = uid('root')
        response = people['root'].post(f'/users/{root}/role',
                                       data={'role': 'operator', **CONFIRM},
                                       follow_redirects=True)
        assert 'cannot change your own role' in flashed(response)
        with app.app_context():
            assert db.session.get(User, root).role == 'admin'

    def test_an_unticked_role_change_changes_nothing(self, app, people):
        """Promoting exempts someone from every rule: it takes the box."""
        with app.app_context():
            alice = uid('alice')
        response = people['root'].post(f'/users/{alice}/role', data={'role': 'admin'},
                                       follow_redirects=True)
        assert 'Tick the box' in flashed(response)
        with app.app_context():
            assert db.session.get(User, alice).role == 'operator'
            assert UserAdminAudit.query.filter_by(action='role_changed').count() == 0

    def test_the_last_active_admin_cannot_be_demoted(self, app, people, make_user,
                                                     monkeypatch):
        """Two admins demoting each other at once: root's request passed its
        checks, then root was demoted elsewhere before the UPDATE ran."""
        from nethub import auth
        make_user('second', PASSWORD, role='admin')
        with app.app_context():
            root, second = uid('root'), uid('second')
        real = auth._target

        def concurrent(target_id):
            target = real(target_id)
            db.session.execute(db.text("UPDATE user SET role = 'operator' WHERE id = :id"),
                               {'id': root})
            return target

        monkeypatch.setattr(auth, '_target', concurrent)
        response = people['root'].post(f'/users/{second}/role',
                                       data={'role': 'operator', **CONFIRM},
                                       follow_redirects=True)
        assert 'last active admin' in flashed(response)
        with app.app_context():
            assert db.session.get(User, second).role == 'admin'

    def test_the_only_admin_can_still_disable_an_operator(self, app, people):
        """The last-admin check is about admins. Disabling the last active
        admin is covered in test_user_management, since only a race reaches
        it: the admin doing the disabling is always another active admin."""
        with app.app_context():
            alice = uid('alice')
        people['root'].post(f'/users/{alice}/disable', data=CONFIRM)
        with app.app_context():
            assert not db.session.get(User, alice).is_active

    def test_the_migration_made_existing_users_admins(self, tmp_path):
        """Nobody loses access on upgrade: everyone was an admin before roles."""
        from alembic import command

        from nethub import schema
        path = tmp_path / 'old.db'
        url = f'sqlite:///{path}'
        command.upgrade(schema.alembic_config(url), '0007_scheduled_approvals')
        with sqlite3.connect(path) as c:
            c.execute("INSERT INTO user (id, username, password_hash, failed_logins) "
                      "VALUES (1, 'alice', 'x', 0)")
            c.execute("INSERT INTO artifacts (id, kind, platform, bundle_key, filename, "
                      "sha512, file_size, storage_path, version, state, bytes_state, "
                      "uploaded_by, uploaded_at) VALUES (1, 'image', 'iosxe', 'k', 'f', "
                      "'x', 1, '/f', 'v', 'published', 'present', 1, '2026-09-01')")
        schema.upgrade_database(url)
        with sqlite3.connect(path) as c:
            assert c.execute('SELECT role FROM user').fetchall() == [('admin',)]
            assert c.execute('SELECT published_by, published_at FROM artifacts'
                             ).fetchall() == [(1, '2026-09-01')]
            with pytest.raises(sqlite3.IntegrityError):
                c.execute("UPDATE user SET role = 'root'")


# -- settings -------------------------------------------------------------------

def settings_form(shown='off', **ticked):
    """The settings form as a page showing two_person_runs at `shown` (and
    the other rules off) would post it, with `ticked` boxes."""
    form = {f'shown_{key}': 'off' for key in settings.TWO_PERSON_RULES}
    form['shown_two_person_runs'] = shown
    return {**form, **ticked}


class TestSettings:
    def test_each_change_writes_an_audit_row(self, app, people):
        people['root'].post('/settings', data=settings_form(two_person_runs='on'))
        people['root'].post('/settings', data=settings_form(
            shown='on', two_person_runs='on'))  # no change
        people['root'].post('/settings', data=settings_form(shown='on'))
        with app.app_context():
            assert not settings.enabled('two_person_runs')
            assert [(e.key, e.old_value, e.new_value, e.changed_by)
                    for e in SettingsAudit.query.order_by(SettingsAudit.id)] == [
                ('two_person_runs', None, 'on', uid('root')),
                ('two_person_runs', 'on', 'off', uid('root')),
            ]

    def test_a_form_from_before_a_change_changes_nothing(self, app, people):
        """An old tab, loaded while the rule was off, must not turn it back off
        by posting its unticked box."""
        rule(app, 'two_person_runs', True)
        response = people['root'].post('/settings', data=settings_form(shown='off'),
                                       follow_redirects=True)
        assert 'changed since you loaded this page' in flashed(response)
        with app.app_context():
            assert settings.enabled('two_person_runs')
            assert SettingsAudit.query.count() == 1

    @pytest.mark.parametrize('statement', ['UPDATE settings_audit SET new_value = \'off\'',
                                           'DELETE FROM settings_audit'])
    def test_the_audit_table_is_append_only(self, app, people, statement):
        rule(app, 'two_person_runs', True)
        with app.app_context():
            with pytest.raises(Exception, match='append-only'):
                db.session.execute(db.text(statement))
            db.session.rollback()
            assert SettingsAudit.query.count() == 1


# -- artifacts ------------------------------------------------------------------

def upload(client, bundle_key='iosxe-17-12-06', filename='cat9k.17.12.06.SPA.bin'):
    return client.post('/artifacts/new', content_type='multipart/form-data', data={
        'bundle_key': bundle_key, 'version': '17.12.06', 'sha512': DIGEST,
        'image': (io.BytesIO(CONTENT), filename)}, follow_redirects=True)


def the_artifact():
    return Artifact.query.one()


def artifact_trail():
    return [(e.action, e.actor_id, e.actor_role)
            for e in ArtifactAudit.query.order_by(ArtifactAudit.id)]


class TestArtifactRule:
    def test_with_the_rule_off_an_operator_publishes_and_deletes_alone(self, app, people):
        upload(people['alice'])
        with app.app_context():
            a = the_artifact()
            assert (a.state, a.published_by) == ('published', uid('alice'))
            artifact_id = a.id
        people['alice'].post(f'/artifacts/{artifact_id}/delete', data=CONFIRM)
        with app.app_context():
            assert Artifact.query.count() == 0
            assert [a for a, _, _ in artifact_trail()] == ['uploaded', 'published', 'deleted']

    def test_with_it_on_an_upload_waits_for_someone_else(self, app, people):
        rule(app, 'two_person_artifacts', True)
        assert 'Someone else has to publish it' in flashed(upload(people['alice']))
        with app.app_context():
            a = the_artifact()
            assert a.state == 'staged'
            artifact_id = a.id
            with pytest.raises(upgrades.RequestError, match='No published image'):
                upgrades.resolve_bundle('iosxe-17-12-06')
        response = people['alice'].post(f'/artifacts/{artifact_id}/publish',
                                        follow_redirects=True)
        assert 'someone else has to publish it' in flashed(response)
        with app.app_context():
            assert the_artifact().state == 'staged'
        people['bob'].post(f'/artifacts/{artifact_id}/publish')
        with app.app_context():
            a = the_artifact()
            assert (a.state, a.uploaded_by, a.published_by) == (
                'published', uid('alice'), uid('bob'))
            assert artifact_trail() == [('uploaded', uid('alice'), 'operator'),
                                        ('published', uid('bob'), 'operator')]

    def test_the_bundle_key_is_checked_when_it_is_published(self, app, people):
        rule(app, 'two_person_artifacts', True)
        upload(people['alice'])
        upload(people['alice'], filename='second.bin')
        with app.app_context():
            first, second = (a.id for a in Artifact.query.order_by(Artifact.id))
        people['bob'].post(f'/artifacts/{first}/publish')
        response = people['bob'].post(f'/artifacts/{second}/publish', follow_redirects=True)
        assert 'already published under' in flashed(response)
        with app.app_context():
            assert db.session.get(Artifact, second).state == 'staged'

    def test_the_uploader_can_withdraw_their_staged_upload_alone(self, app, people):
        rule(app, 'two_person_artifacts', True)
        upload(people['alice'])
        with app.app_context():
            a = the_artifact()
            artifact_id, path = a.id, a.storage_path
        people['alice'].post(f'/artifacts/{artifact_id}/delete', data=CONFIRM)
        with app.app_context():
            assert Artifact.query.count() == 0
            assert [a for a, _, _ in artifact_trail()] == ['uploaded', 'withdrawn']
        assert not os.path.exists(path)

    def test_only_the_uploader_or_an_admin_can_withdraw(self, app, people):
        """The second check cannot erase an upload alone."""
        rule(app, 'two_person_artifacts', True)
        upload(people['alice'])
        with app.app_context():
            artifact_id = the_artifact().id
        page = flashed(people['bob'].get('/artifacts'))
        assert 'Withdraw upload' not in page
        response = people['bob'].post(f'/artifacts/{artifact_id}/delete', data=CONFIRM,
                                      follow_redirects=True)
        assert 'Only the uploader or an admin' in flashed(response)
        with app.app_context():
            assert the_artifact().state == 'staged'
        people['root'].post(f'/artifacts/{artifact_id}/delete', data=CONFIRM)
        with app.app_context():
            assert Artifact.query.count() == 0
            assert artifact_trail()[-1] == ('withdrawn', uid('root'), 'admin')

    def test_a_withdraw_racing_a_publish_deletes_nothing(self, app, people):
        """The publish commits after the withdraw read the row: the withdraw
        must not remove what is now published."""
        from sqlalchemy.orm.attributes import set_committed_value

        from nethub import artifacts
        rule(app, 'two_person_artifacts', True)
        upload(people['alice'])
        with app.app_context():
            a = the_artifact()
            db.session.execute(db.text("UPDATE artifacts SET state = 'published'"))
            db.session.commit()
            set_committed_value(a, 'state', 'staged')  # what the withdraw read
            with pytest.raises(artifacts.ArtifactError, match='not waiting'):
                artifacts.withdraw(a, User.query.filter_by(username='alice').one())
            assert Artifact.query.one().state == 'published'
            assert os.path.exists(a.storage_path)
            assert [x for x, _, _ in artifact_trail()] == ['uploaded']

    def test_a_publish_racing_another_under_the_same_key_is_a_message(self, app, people,
                                                                     monkeypatch):
        """The other publish committed after this one's pre-check: SQLite
        refuses the UPDATE itself, and that must not be a 500."""
        from nethub import artifacts
        rule(app, 'two_person_artifacts', True)
        upload(people['alice'])
        with app.app_context():
            staged = the_artifact()
            db.session.execute(db.text(
                "INSERT INTO artifacts (kind, platform, bundle_key, filename, sha512, "
                "file_size, storage_path, version, state, bytes_state, uploaded_at) VALUES "
                "('image', 'iosxe', 'iosxe-17-12-06', 'other.bin', 'x', 1, '/other', "
                "'v', 'published', 'present', '2026-10-01')"))
            db.session.commit()
            # The other publish committed after this one's pre-check.
            monkeypatch.setattr(artifacts, '_published_under', lambda *a: None)
            with pytest.raises(artifacts.ArtifactError, match='already published'):
                artifacts.publish(staged, User.query.filter_by(username='bob').one())
            assert db.session.get(Artifact, staged.id).state == 'staged'

    def test_the_rule_is_read_when_the_upload_is_recorded(self, app, people, monkeypatch):
        """Turned on while the bytes streamed: the upload lands staged."""
        from werkzeug.datastructures import FileStorage

        from nethub import artifacts

        class TurnsTheRuleOn(io.BytesIO):
            def read(self, *args):
                if self.tell() == 0:
                    with app.app_context():
                        rule(app, 'two_person_artifacts', True)
                return super().read(*args)

        with app.app_context():
            a = artifacts.ingest(
                file_storage=FileStorage(stream=TurnsTheRuleOn(CONTENT), filename='x.bin'),
                bundle_key='k', version='v', sha512=DIGEST,
                user=User.query.filter_by(username='alice').one(),
                store=current_app.config['ARTIFACT_STORE'])
            assert (a.state, a.published_by) == ('staged', None)

    def test_a_deleted_artifacts_id_is_never_reused(self, app, people):
        """`artifact_audit.artifact_id` outlives the row it names."""
        upload(people['alice'])
        with app.app_context():
            first = the_artifact().id
        people['alice'].post(f'/artifacts/{first}/delete', data=CONFIRM)
        upload(people['alice'])
        with app.app_context():
            assert the_artifact().id != first

    def test_two_people_requesting_one_delete_make_one_request(self, app, people):
        """bob's request committed after alice's delete read the row: hers
        must not become a confirmation of a request she never saw."""
        from sqlalchemy.orm.attributes import set_committed_value

        from nethub import artifacts
        upload(people['alice'])
        rule(app, 'two_person_artifacts', True)
        with app.app_context():
            a = the_artifact()
            db.session.execute(db.text('UPDATE artifacts SET delete_requested_by = :id'),
                               {'id': uid('bob')})
            db.session.commit()
            set_committed_value(a, 'delete_requested_by', None)  # what alice read
            with pytest.raises(artifacts.ArtifactError, match='at the same moment'):
                artifacts.delete(a, User.query.filter_by(username='alice').one())
            assert the_artifact().delete_requested_by == uid('bob')

    def test_with_it_on_a_delete_is_a_request_someone_else_confirms(self, app, people):
        upload(people['alice'])
        rule(app, 'two_person_artifacts', True)
        with app.app_context():
            artifact_id = the_artifact().id
        people['alice'].post(f'/artifacts/{artifact_id}/delete', data=CONFIRM)
        people['alice'].post(f'/artifacts/{artifact_id}/delete', data=CONFIRM)
        with app.app_context():
            a = the_artifact()
            assert a.delete_requested_by == uid('alice')
            upgrades.resolve_bundle('iosxe-17-12-06')  # still usable
        people['bob'].post(f'/artifacts/{artifact_id}/delete', data=CONFIRM)
        with app.app_context():
            assert Artifact.query.count() == 0
            assert artifact_trail()[-2:] == [('delete_requested', uid('alice'), 'operator'),
                                              ('deleted', uid('bob'), 'operator')]

    def test_an_admin_acts_alone_and_the_record_says_so(self, app, people):
        rule(app, 'two_person_artifacts', True)
        upload(people['root'])
        with app.app_context():
            a = the_artifact()
            assert a.state == 'published'
            artifact_id = a.id
        people['root'].post(f'/artifacts/{artifact_id}/delete', data=CONFIRM)
        with app.app_context():
            assert Artifact.query.count() == 0
            root = uid('root')
            assert artifact_trail() == [('uploaded', root, 'admin'),
                                        ('published', root, 'admin'),
                                        ('deleted', root, 'admin')]

    def test_turning_the_rule_off_releases_a_waiting_upload(self, app, people):
        rule(app, 'two_person_artifacts', True)
        upload(people['alice'])
        rule(app, 'two_person_artifacts', False)
        with app.app_context():
            artifact_id = the_artifact().id
        people['alice'].post(f'/artifacts/{artifact_id}/publish')
        with app.app_context():
            assert the_artifact().state == 'published'

    def test_the_artifact_audit_is_append_only(self, app, people):
        upload(people['alice'])
        with app.app_context():
            for statement in ("UPDATE artifact_audit SET actor_role = 'admin'",
                              'DELETE FROM artifact_audit'):
                with pytest.raises(Exception, match='append-only'):
                    db.session.execute(db.text(statement))
                db.session.rollback()


# -- host keys ------------------------------------------------------------------

def scan_by(name, address='192.0.2.10'):
    scan = HostKeyScan(ansible_host=address, requested_by=uid(name), status='succeeded',
                       key_type='ssh-rsa', fingerprint_sha256='SHA256:x',
                       finished_at=upgrades._utcnow())
    db.session.add(scan)
    db.session.commit()
    return scan.id


def pin_trail():
    return [(e.action, e.actor_id, e.requested_by, e.actor_role)
            for e in DeviceHostKeyAudit.query.order_by(DeviceHostKeyAudit.id)]


def confirm(client, scan_id):
    return client.post('/hostkeys/confirm', data={'scan_id': scan_id}, follow_redirects=True)


def pinned():
    return DeviceHostKey.query.filter_by(ansible_host='192.0.2.10').first()


class TestHostKeyRule:
    def test_with_the_rule_off_the_scanner_confirms_alone(self, app, people):
        with app.app_context():
            scan = scan_by('alice')
        confirm(people['bob'], scan)
        with app.app_context():
            assert pinned() is None
        confirm(people['alice'], scan)
        with app.app_context():
            assert pinned().confirmed_by == uid('alice')
            alice = uid('alice')
            assert pin_trail() == [('confirmed', alice, alice, 'operator')]

    def test_with_it_on_someone_else_confirms(self, app, people):
        rule(app, 'two_person_hostkeys', True)
        with app.app_context():
            scan = scan_by('alice')
        assert 'someone other than' in flashed(confirm(people['alice'], scan))
        with app.app_context():
            assert pinned() is None
        confirm(people['bob'], scan)
        with app.app_context():
            assert pinned().confirmed_by == uid('bob')
            assert pin_trail() == [('confirmed', uid('bob'), uid('alice'), 'operator')]

    def test_the_scan_page_says_who_has_to_confirm(self, app, people):
        rule(app, 'two_person_hostkeys', True)
        with app.app_context():
            scan = scan_by('alice')
        assert 'someone other than you' in flashed(people['alice'].get(
            f'/hostkeys/scan/{scan}'))
        assert '/hostkeys/confirm' in flashed(people['bob'].get(f'/hostkeys/scan/{scan}'))

    def test_the_confirmer_is_told_the_deadline_and_finds_the_scan_listed(self, app,
                                                                         people):
        from nethub.upgrade_routes import confirm_by
        rule(app, 'two_person_hostkeys', True)
        with app.app_context():
            scan = scan_by('alice')
            deadline = confirm_by(db.session.get(HostKeyScan, scan)).strftime('%H:%M')
        assert deadline in flashed(people['alice'].get(f'/hostkeys/scan/{scan}'))
        listing = flashed(people['bob'].get('/hostkeys'))
        assert f'/hostkeys/scan/{scan}' in listing and deadline in listing

    def test_a_stale_or_used_scan_is_not_listed(self, app, people):
        from datetime import timedelta
        with app.app_context():
            old = db.session.get(HostKeyScan, scan_by('alice'))
            old.finished_at = upgrades._utcnow() - timedelta(minutes=16)
            used = db.session.get(HostKeyScan, scan_by('alice'))
            used.consumed_at = upgrades._utcnow()
            db.session.commit()
            ids = (old.id, used.id)
        listing = flashed(people['bob'].get('/hostkeys'))
        assert all(f'/hostkeys/scan/{i}' not in listing for i in ids)

    def test_turning_the_rule_off_releases_a_waiting_scan(self, app, people):
        rule(app, 'two_person_hostkeys', True)
        with app.app_context():
            scan = scan_by('alice')
        confirm(people['alice'], scan)
        rule(app, 'two_person_hostkeys', False)
        confirm(people['alice'], scan)
        with app.app_context():
            assert pinned().is_confirmed

    def test_with_it_on_removing_a_pin_needs_a_second_person(self, app, people):
        with app.app_context():
            confirm(people['alice'], scan_by('alice'))
            key_id = pinned().id
        rule(app, 'two_person_hostkeys', True)
        people['alice'].post(f'/hostkeys/{key_id}/delete', data=CONFIRM)
        people['alice'].post(f'/hostkeys/{key_id}/delete', data=CONFIRM)
        with app.app_context():
            assert pinned().is_confirmed  # still in force
            upgrades.confirmed_key('192.0.2.10')
        people['bob'].post(f'/hostkeys/{key_id}/delete', data=CONFIRM)
        with app.app_context():
            alice, bob = uid('alice'), uid('bob')
            assert pinned() is None
            assert pin_trail()[1:] == [('delete_requested', alice, alice, 'operator'),
                                       ('deleted', bob, alice, 'operator')]

    def test_an_admin_confirms_their_own_scan_and_removes_alone(self, app, people):
        rule(app, 'two_person_hostkeys', True)
        with app.app_context():
            confirm(people['root'], scan_by('root'))
            key_id = pinned().id
        people['root'].post(f'/hostkeys/{key_id}/delete', data=CONFIRM)
        with app.app_context():
            root = uid('root')
            assert pinned() is None
            assert pin_trail() == [('confirmed', root, root, 'admin'),
                                   ('deleted', root, root, 'admin')]


# -- runs -----------------------------------------------------------------------

@pytest.fixture
def run_at_stage(app, people, make_artifact):
    """A run alice submitted, parked at the stage gate."""
    make_artifact()
    with app.app_context():
        db.session.add(DeviceHostKey(ansible_host='192.0.2.10', key_type='ssh-rsa',
                                     fingerprint_sha256='SHA256:x',
                                     confirmed_by=uid('root'),
                                     confirmed_at=upgrades._utcnow()))
        db.session.commit()
        run, _ = upgrades.submit(
            user=User.query.filter_by(username='alice').one(), bundle='iosxe-17-12-06',
            hosts_raw='sw01, 192.0.2.10', cidrs=['192.0.2.0/24'], password=DEVICE_PASSWORD,
            public_key=current_app.extensions['credential_public_key'])
        for job in run.phase_jobs:
            job.status, job.sealed_credential = 'succeeded', None
        run.state, run.awaiting_phase = 'awaiting_approval', 'stage'
        run.hosts[0].state = 'precheck_ok'
        db.session.commit()
        return run.id


def approve(client, run_id, phase='stage'):
    return client.post(f'/upgrades/{run_id}/approve', follow_redirects=True, data={
        'phase': phase, 'device_password': DEVICE_PASSWORD, 'confirm': 'yes'})


def gate_job(run_id, phase='stage'):
    return UpgradePhaseJob.query.filter_by(run_id=run_id, phase=phase).one_or_none()


class TestRunRule:
    def test_with_the_rule_off_the_submitter_approves(self, app, people, run_at_stage):
        approve(people['alice'], run_at_stage)
        with app.app_context():
            job = gate_job(run_at_stage)
            assert (job.approved_by, job.approved_by_role) == (uid('alice'), 'operator')

    def test_with_it_on_the_submitter_cannot_approve_and_someone_else_can(
            self, app, people, run_at_stage):
        rule(app, 'two_person_runs', True)
        page = flashed(people['alice'].get(f'/upgrades/{run_at_stage}'))
        assert 'someone else' in page and 'approve_device_password' not in page
        assert 'two-person rule for runs is on' in flashed(approve(people['alice'],
                                                                   run_at_stage))
        with app.app_context():
            assert gate_job(run_at_stage) is None
            assert db.session.get(UpgradeRun, run_at_stage).state == 'awaiting_approval'
        approve(people['bob'], run_at_stage)
        with app.app_context():
            assert gate_job(run_at_stage).approved_by == uid('bob')

    def test_with_it_on_the_submitter_cannot_retry(self, app, people, run_at_stage):
        with app.app_context():
            run = db.session.get(UpgradeRun, run_at_stage)
            run.hosts[0].state, run.hosts[0].last_phase = 'failed', 'precheck'
            db.session.commit()
        rule(app, 'two_person_runs', True)
        data = {'phase': 'precheck', 'device_password': DEVICE_PASSWORD, 'confirm': 'yes'}
        response = people['alice'].post(f'/upgrades/{run_at_stage}/retry', data=data,
                                        follow_redirects=True)
        assert 'two-person rule for runs is on' in flashed(response)
        people['bob'].post(f'/upgrades/{run_at_stage}/retry', data=data)
        with app.app_context():
            job = UpgradePhaseJob.query.filter_by(run_id=run_at_stage, is_retry=True).one()
            assert job.approved_by == uid('bob')

    def test_an_admin_approves_their_own_run_and_the_job_says_admin(
            self, app, people, run_at_stage):
        rule(app, 'two_person_runs', True)
        with app.app_context():
            db.session.get(UpgradeRun, run_at_stage).submitted_by = uid('root')
            db.session.commit()
        approve(people['root'], run_at_stage)
        with app.app_context():
            job = gate_job(run_at_stage)
            assert (job.approved_by, job.approved_by_role) == (uid('root'), 'admin')

    def test_cancel_needs_no_second_person(self, app, people, run_at_stage):
        rule(app, 'two_person_runs', True)
        people['alice'].post(f'/upgrades/{run_at_stage}/cancel', data=CONFIRM)
        with app.app_context():
            assert db.session.get(UpgradeRun, run_at_stage).state == 'cancelled'

    def test_declining_cleanup_needs_no_second_person(self, app, people, run_at_stage):
        rule(app, 'two_person_runs', True)
        with app.app_context():
            db.session.get(UpgradeRun, run_at_stage).awaiting_phase = 'cleanup'
            db.session.commit()
        people['alice'].post(f'/upgrades/{run_at_stage}/decline-cleanup')
        with app.app_context():
            assert db.session.get(UpgradeRun, run_at_stage).state == 'completed'

    def test_turning_the_rule_off_releases_a_waiting_run(self, app, people, run_at_stage):
        rule(app, 'two_person_runs', True)
        approve(people['alice'], run_at_stage)
        rule(app, 'two_person_runs', False)
        approve(people['alice'], run_at_stage)
        with app.app_context():
            assert gate_job(run_at_stage).approved_by == uid('alice')

    def test_each_job_records_the_device_username_its_supplier_used(
            self, app, people, run_at_stage):
        """The run row keeps the submitter's; the device saw the approver's."""
        approve(people['bob'], run_at_stage)
        with app.app_context():
            run = db.session.get(UpgradeRun, run_at_stage)
            assert run.device_username_used == 'alice-dev'
            assert gate_job(run_at_stage, 'precheck').device_username_used == 'alice-dev'
            assert gate_job(run_at_stage).device_username_used == 'bob-dev'
        page = flashed(people['root'].get(f'/upgrades/{run_at_stage}'))
        assert 'bob-dev' in page and 'alice-dev' in page
