import hashlib
import io


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
