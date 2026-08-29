import hashlib
import io
import os

import yaml


def test_list_entries_requires_login(client):
    assert client.get('/registry').status_code == 302


def test_new_entry_get_requires_login(client):
    assert client.get('/registry/new').status_code == 302


def test_list_entries_page_loads_when_logged_in(logged_in_client):
    assert logged_in_client.get('/registry').status_code == 200


def test_new_entry_upload_flow(logged_in_client):
    content = b'image-bytes'
    sha512 = hashlib.sha512(content).hexdigest()
    resp = logged_in_client.post(
        '/registry/new',
        data={
            'name': 'iosxe-17.9',
            'sha512': sha512,
            'image': (io.BytesIO(content), 'image.bin'),
        },
        content_type='multipart/form-data',
    )
    assert resp.status_code == 302
    assert resp.headers['Location'] == '/registry'

    listing = logged_in_client.get('/registry')
    assert b'iosxe-17.9' in listing.data


def test_new_entry_bad_checksum_reshows_form_with_error(logged_in_client):
    resp = logged_in_client.post(
        '/registry/new',
        data={
            'name': 'bad',
            'sha512': 'not-a-real-hash',
            'image': (io.BytesIO(b'x'), 'image.bin'),
        },
        content_type='multipart/form-data',
    )
    assert resp.status_code == 200
    assert b'128-character hex' in resp.data


def test_delete_entry_requires_login(client):
    assert client.post('/registry/whatever/delete').status_code == 302


def test_delete_entry_removes_it_from_the_list(logged_in_client):
    content = b'image-bytes'
    sha512 = hashlib.sha512(content).hexdigest()
    logged_in_client.post(
        '/registry/new',
        data={
            'name': 'iosxe-17.9',
            'sha512': sha512,
            'image': (io.BytesIO(content), 'image.bin'),
        },
        content_type='multipart/form-data',
    )

    resp = logged_in_client.post('/registry/iosxe-17.9/delete')
    assert resp.status_code == 302
    assert resp.headers['Location'] == '/registry'

    # a bare substring check would also match the "Deleted ..." flash
    # message, so check the table row itself is gone
    listing = logged_in_client.get('/registry')
    assert b'<td>iosxe-17.9</td>' not in listing.data
    assert b'No registry entries yet' in listing.data


def test_delete_entry_unknown_name_flashes_error(logged_in_client):
    resp = logged_in_client.post('/registry/nope/delete', follow_redirects=True)
    assert resp.status_code == 200
    assert b'No entry named' in resp.data


def test_check_entries_requires_login(client):
    assert client.post('/registry/check').status_code == 302


def test_check_entries_clean_registry_flashes_no_issues(logged_in_client):
    resp = logged_in_client.post('/registry/check', follow_redirects=True)
    assert resp.status_code == 200
    assert b'no issues' in resp.data


def test_check_entries_flags_broken_entry(logged_in_client, app):
    with app.app_context():
        path = app.config['REGISTRY_FILE']
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            yaml.safe_dump(
                {'software_registry': {'ghost': {'file_name': 'nope.bin', 'sha512': 'a' * 128, 'file_size': 1}}},
                f,
            )

    resp = logged_in_client.post('/registry/check', follow_redirects=True)
    assert resp.status_code == 200
    assert b'not found' in resp.data


def test_list_entries_survives_hand_broken_yaml(logged_in_client, app):
    with app.app_context():
        path = app.config['REGISTRY_FILE']
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            f.write('software_registry: [this is not valid: yaml')

    resp = logged_in_client.get('/registry')
    assert resp.status_code == 200
    assert b'not valid YAML' in resp.data
