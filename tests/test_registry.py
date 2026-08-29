import hashlib
import io
import os

import pytest
import yaml
from werkzeug.datastructures import FileStorage

from nethub import registry as registry_store


def _file(content=b'fake-image-bytes'):
    return FileStorage(stream=io.BytesIO(content), filename='image.bin')


def _sha512(content=b'fake-image-bytes'):
    return hashlib.sha512(content).hexdigest()


def test_list_entries_empty_when_no_registry_file(app):
    with app.app_context():
        assert registry_store.list_entries() == {}


def test_add_entry_success(app):
    with app.app_context():
        registry_store.add_entry('iosxe-17.9', _sha512(), _file())
        entries = registry_store.list_entries()
        assert entries['iosxe-17.9']['sha512'] == _sha512()
        assert entries['iosxe-17.9']['file_name'] == 'image.bin'
        assert entries['iosxe-17.9']['file_size'] == len(b'fake-image-bytes')


def test_add_entry_rejects_missing_name(app):
    with app.app_context():
        with pytest.raises(registry_store.RegistryError, match='Name is required'):
            registry_store.add_entry('', _sha512(), _file())


def test_add_entry_rejects_duplicate_name(app):
    with app.app_context():
        registry_store.add_entry('dup', _sha512(), _file())
        with pytest.raises(registry_store.RegistryError, match='already exists'):
            registry_store.add_entry('dup', _sha512(b'other'), _file(b'other'))


def test_add_entry_rejects_bad_checksum_format(app):
    with app.app_context():
        with pytest.raises(registry_store.RegistryError, match='128-character hex'):
            registry_store.add_entry('bad-hash', 'not-a-hash', _file())


def test_add_entry_rejects_checksum_mismatch_and_cleans_up_file(app):
    with app.app_context():
        wrong_hash = _sha512(b'not-the-real-content')
        with pytest.raises(registry_store.RegistryError, match='Checksum mismatch'):
            registry_store.add_entry('bad-checksum', wrong_hash, _file())
        # rejected upload must not leave an orphaned file or registry entry
        assert registry_store.list_entries() == {}


def test_add_entry_rejects_missing_file(app):
    with app.app_context():
        with pytest.raises(registry_store.RegistryError, match='image file is required'):
            registry_store.add_entry('no-file', _sha512(), None)


def test_delete_entry_removes_entry_and_file(app):
    with app.app_context():
        registry_store.add_entry('iosxe-17.9', _sha512(), _file())
        image_path = os.path.join(app.config['IMAGE_DIR'], 'image.bin')
        assert os.path.exists(image_path)

        registry_store.delete_entry('iosxe-17.9')

        assert registry_store.list_entries() == {}
        assert not os.path.exists(image_path)


def test_delete_entry_rejects_unknown_name(app):
    with app.app_context():
        with pytest.raises(registry_store.RegistryError, match='No entry named'):
            registry_store.delete_entry('nope')


def test_on_disk_file_nests_entries_under_software_registry_key(app):
    """The written YAML must match the shape Ansible group_vars expects
    (ansible/inventory/rendered/group_vars/os_iosxe.yml): entries live
    under a top-level `software_registry` key, not at the file root.
    """
    with app.app_context():
        registry_store.add_entry('iosxe-17.9', _sha512(), _file())
        with open(app.config['REGISTRY_FILE']) as f:
            on_disk = yaml.safe_load(f)
        assert set(on_disk.keys()) == {'software_registry'}
        assert on_disk['software_registry']['iosxe-17.9']['sha512'] == _sha512()


def test_registry_file_env_var_points_at_a_specific_path(app, tmp_path):
    """REGISTRY_FILE lets a deployment bind-mount and update one specific
    host file (e.g. a real Ansible group_vars file) instead of always
    writing its own default path.
    """
    custom_path = tmp_path / 'os_iosxe.yml'
    app.config['REGISTRY_FILE'] = str(custom_path)
    with app.app_context():
        registry_store.add_entry('iosxe-17.9', _sha512(), _file())
    assert os.path.exists(custom_path)
    with open(custom_path) as f:
        on_disk = yaml.safe_load(f)
    assert on_disk['software_registry']['iosxe-17.9']['sha512'] == _sha512()
