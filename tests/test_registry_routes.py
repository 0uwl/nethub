"""HTTP tests for registry_routes.py -- the /registries/<id>/entries
routes that list, add, delete and check one registry's entries.
Registry-row (list/new/delete) routes are covered in
tests/test_registries_routes.py.
"""
import hashlib
import io
import os

import yaml


def test_list_entries_requires_login(client, make_registry):
    registry = make_registry()
    assert client.get(f'/registries/{registry.id}/entries').status_code == 302


def test_new_entry_get_requires_login(client, make_registry):
    registry = make_registry()
    assert client.get(f'/registries/{registry.id}/entries/new').status_code == 302


def test_entries_route_404s_for_unknown_registry(logged_in_client):
    assert logged_in_client.get('/registries/999/entries').status_code == 404


def test_list_entries_page_loads_when_logged_in(logged_in_client, make_registry):
    registry = make_registry()
    assert logged_in_client.get(f'/registries/{registry.id}/entries').status_code == 200


def test_new_entry_upload_flow(logged_in_client, make_registry):
    registry = make_registry()
    content = b'image-bytes'
    sha512 = hashlib.sha512(content).hexdigest()
    resp = logged_in_client.post(
        f'/registries/{registry.id}/entries/new',
        data={
            'name': 'iosxe-17.9',
            'sha512': sha512,
            'image': (io.BytesIO(content), 'image.bin'),
        },
        content_type='multipart/form-data',
    )
    assert resp.status_code == 302
    assert resp.headers['Location'] == f'/registries/{registry.id}/entries'

    listing = logged_in_client.get(f'/registries/{registry.id}/entries')
    assert b'iosxe-17.9' in listing.data


def test_new_entry_bad_checksum_reshows_form_with_error(logged_in_client, make_registry):
    registry = make_registry()
    resp = logged_in_client.post(
        f'/registries/{registry.id}/entries/new',
        data={
            'name': 'bad',
            'sha512': 'not-a-real-hash',
            'image': (io.BytesIO(b'x'), 'image.bin'),
        },
        content_type='multipart/form-data',
    )
    assert resp.status_code == 200
    assert b'128-character hex' in resp.data


def test_delete_entry_requires_login(client, make_registry):
    registry = make_registry()
    assert client.post(f'/registries/{registry.id}/entries/whatever/delete').status_code == 302


def test_delete_entry_removes_it_from_the_list(logged_in_client, make_registry):
    registry = make_registry()
    content = b'image-bytes'
    sha512 = hashlib.sha512(content).hexdigest()
    logged_in_client.post(
        f'/registries/{registry.id}/entries/new',
        data={
            'name': 'iosxe-17.9',
            'sha512': sha512,
            'image': (io.BytesIO(content), 'image.bin'),
        },
        content_type='multipart/form-data',
    )

    resp = logged_in_client.post(f'/registries/{registry.id}/entries/iosxe-17.9/delete')
    assert resp.status_code == 302
    assert resp.headers['Location'] == f'/registries/{registry.id}/entries'

    # a bare substring check would also match the "Deleted ..." flash
    # message, so check the table row itself is gone
    listing = logged_in_client.get(f'/registries/{registry.id}/entries')
    assert b'<td>iosxe-17.9</td>' not in listing.data
    assert b'No registry entries yet' in listing.data


def test_delete_entry_unknown_name_flashes_error(logged_in_client, make_registry):
    registry = make_registry()
    resp = logged_in_client.post(f'/registries/{registry.id}/entries/nope/delete', follow_redirects=True)
    assert resp.status_code == 200
    assert b'No entry named' in resp.data


def test_check_entries_requires_login(client, make_registry):
    registry = make_registry()
    assert client.post(f'/registries/{registry.id}/entries/check').status_code == 302


def test_check_entries_clean_registry_flashes_no_issues(logged_in_client, make_registry):
    registry = make_registry()
    resp = logged_in_client.post(f'/registries/{registry.id}/entries/check', follow_redirects=True)
    assert resp.status_code == 200
    assert b'no issues' in resp.data


def test_check_entries_flags_broken_entry(logged_in_client, app, make_registry):
    registry = make_registry()
    with app.app_context():
        path = os.path.join(app.config['REGISTRIES_ROOT'], registry.file_path)
        with open(path, 'w') as f:
            yaml.safe_dump(
                {'software_registry': {'ghost': {'file_name': 'nope.bin', 'sha512': 'a' * 128, 'file_size': 1}}},
                f,
            )

    resp = logged_in_client.post(f'/registries/{registry.id}/entries/check', follow_redirects=True)
    assert resp.status_code == 200
    assert b'not found' in resp.data


def test_list_entries_survives_hand_broken_yaml(logged_in_client, app, make_registry):
    registry = make_registry()
    with app.app_context():
        path = os.path.join(app.config['REGISTRIES_ROOT'], registry.file_path)
        with open(path, 'w') as f:
            f.write('software_registry: [this is not valid: yaml')

    resp = logged_in_client.get(f'/registries/{registry.id}/entries')
    assert resp.status_code == 200
    assert b'not valid YAML' in resp.data
