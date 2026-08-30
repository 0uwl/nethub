"""HTTP tests for registries_routes.py -- the /registries routes that
list, create, and delete Registry rows (which files NetHub tracks).
Entry-scoped routes are covered in tests/test_registry_routes.py.
"""
import os

import yaml


def _write_file(app, filename, content=''):
    path = os.path.join(app.config['REGISTRIES_ROOT'], filename)
    with open(path, 'w') as f:
        f.write(content)
    return path


def test_list_registries_requires_login(client):
    assert client.get('/registries').status_code == 302


def test_list_registries_page_loads_when_logged_in(logged_in_client, make_registry):
    make_registry(name='iosxe')
    resp = logged_in_client.get('/registries')
    assert resp.status_code == 200
    assert b'iosxe' in resp.data


def test_new_registry_get_requires_login(client):
    assert client.get('/registries/new').status_code == 302


def test_new_registry_get_shows_discovered_files(logged_in_client, app):
    with app.app_context():
        _write_file(app, 'unclaimed.yml')
    resp = logged_in_client.get('/registries/new')
    assert resp.status_code == 200
    assert b'unclaimed.yml' in resp.data


def test_new_registry_post_creates_row_for_plain_file(logged_in_client, app, tmp_path):
    with app.app_context():
        _write_file(app, 'plain.yml')
    search_dir = str(tmp_path)

    resp = logged_in_client.post('/registries/new', data={
        'name': 'iosxe',
        'file_path': 'plain.yml',
        'search_dir': search_dir,
    })
    assert resp.status_code == 302

    listing = logged_in_client.get('/registries')
    assert b'iosxe' in listing.data
    with open(os.path.join(app.config['REGISTRIES_ROOT'], 'plain.yml')) as f:
        on_disk = yaml.safe_load(f)
    assert on_disk['software_registry']['search_dir'] == search_dir


def test_new_registry_post_rejects_nonexistent_search_dir(logged_in_client, app):
    with app.app_context():
        _write_file(app, 'plain2.yml')

    resp = logged_in_client.post('/registries/new', data={
        'name': 'iosxe',
        'file_path': 'plain2.yml',
        'search_dir': '/no/such/directory',
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert b'not a writable directory' in resp.data


def test_new_registry_post_auto_adopts_existing_registry(logged_in_client, app, tmp_path):
    with app.app_context():
        _write_file(app, 'existing.yml', yaml.safe_dump({
            'software_registry': {
                'search_dir': str(tmp_path),
                'iosxe-17.9': {'file_name': 'x.bin', 'sha512': 'a' * 128, 'file_size': 1},
            }
        }))

    resp = logged_in_client.post('/registries/new', data={
        'name': 'adopted',
        'file_path': 'existing.yml',
        'search_dir': '',
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert b'1 existing entry found' in resp.data


def test_new_registry_post_rejects_relative_search_dir(logged_in_client, app):
    with app.app_context():
        _write_file(app, 'plain3.yml')

    resp = logged_in_client.post('/registries/new', data={
        'name': 'iosxe',
        'file_path': 'plain3.yml',
        'search_dir': 'relative/path',
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert b'must be an absolute path' in resp.data


def test_new_registry_post_rejects_file_already_tracked_via_symlink(logged_in_client, app, tmp_path):
    with app.app_context():
        root = app.config['REGISTRIES_ROOT']
        _write_file(app, 'real.yml')
        os.symlink(os.path.join(root, 'real.yml'), os.path.join(root, 'alias.yml'))

    search_dir = str(tmp_path)
    resp = logged_in_client.post('/registries/new', data={
        'name': 'first', 'file_path': 'real.yml', 'search_dir': search_dir,
    })
    assert resp.status_code == 302

    resp = logged_in_client.post('/registries/new', data={
        'name': 'second', 'file_path': 'alias.yml', 'search_dir': search_dir,
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert b'already tracked' in resp.data


def test_new_registry_post_rejects_untracked_file_path(logged_in_client, app):
    with app.app_context():
        _write_file(app, 'real.yml')

    resp = logged_in_client.post('/registries/new', data={
        'name': 'iosxe',
        'file_path': '../../etc/passwd',
        'search_dir': '/tmp',
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert b'Choose one of the listed files' in resp.data


def test_new_registry_post_rejects_duplicate_name(logged_in_client, app, make_registry, tmp_path):
    make_registry(name='dup')
    with app.app_context():
        _write_file(app, 'other.yml')

    resp = logged_in_client.post('/registries/new', data={
        'name': 'dup',
        'file_path': 'other.yml',
        'search_dir': str(tmp_path),
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert b'already exists' in resp.data


def test_delete_registry_requires_login(client, make_registry):
    registry = make_registry()
    assert client.post(f'/registries/{registry.id}/delete').status_code == 302


def test_delete_registry_removes_row_but_leaves_file_and_images(logged_in_client, app, make_registry):
    registry = make_registry(name='to-delete')
    file_path = os.path.join(app.config['REGISTRIES_ROOT'], registry.file_path)
    image_path = os.path.join(registry.search_dir, 'kept.bin')
    with open(image_path, 'wb') as f:
        f.write(b'keep-me')

    resp = logged_in_client.post(f'/registries/{registry.id}/delete')
    assert resp.status_code == 302
    assert resp.headers['Location'] == '/registries'

    # a bare substring check would also match the "Stopped tracking ..."
    # flash message, so check the table row itself is gone
    listing = logged_in_client.get('/registries')
    assert b'<td>to-delete</td>' not in listing.data
    assert os.path.exists(file_path)
    assert os.path.exists(image_path)


def test_delete_registry_unknown_id_404s(logged_in_client):
    assert logged_in_client.post('/registries/999/delete').status_code == 404
