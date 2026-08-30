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


def _write_raw_registry(app, registry, data):
    path = os.path.join(app.config['REGISTRIES_ROOT'], registry.file_path)
    with open(path, 'w') as f:
        yaml.safe_dump(data, f)


# -- load_registry / list_entries -------------------------------------------

def test_list_entries_empty_for_fresh_registry(app, make_registry):
    registry = make_registry()
    with app.app_context():
        assert registry_store.list_entries(registry) == {}


def test_load_registry_search_dir_reflects_db_row(app, make_registry):
    registry = make_registry()
    with app.app_context():
        registry_store.add_entry(registry, 'iosxe-17.9', _sha512(), _file(), '17.9.1')
        assert registry_store.load_registry(registry)['search_dir'] == registry.search_dir


def test_load_registry_db_search_dir_wins_over_file(app, make_registry):
    """search_dir is DB-authoritative: a hand-edited file claiming a
    different search_dir must never override registry.search_dir.
    """
    registry = make_registry()
    with app.app_context():
        _write_raw_registry(app, registry, {
            'software_registry': {'search_dir': '/somewhere/else'},
        })
        assert registry_store.load_registry(registry)['search_dir'] == registry.search_dir
        assert registry_store.load_registry(registry)['search_dir'] != '/somewhere/else'


def test_load_registry_raises_when_file_missing(app, make_registry):
    registry = make_registry()
    os.remove(os.path.join(app.config['REGISTRIES_ROOT'], registry.file_path))
    with app.app_context(), pytest.raises(registry_store.RegistryError, match='was not found'):
        registry_store.load_registry(registry)


# -- add_entry ----------------------------------------------------------------

def test_add_entry_success(app, make_registry):
    registry = make_registry()
    with app.app_context():
        registry_store.add_entry(registry, 'iosxe-17.9', _sha512(), _file(), '17.9.1')
        entries = registry_store.list_entries(registry)
        assert entries['iosxe-17.9']['sha512'] == _sha512()
        assert entries['iosxe-17.9']['file_name'] == 'image.bin'
        assert entries['iosxe-17.9']['file_size'] == len(b'fake-image-bytes')
        assert entries['iosxe-17.9']['version'] == '17.9.1'


def test_add_entry_rejects_missing_name(app, make_registry):
    registry = make_registry()
    with app.app_context(), pytest.raises(registry_store.RegistryError, match='Name is required'):
        registry_store.add_entry(registry, '', _sha512(), _file(), '17.9.1')


def test_add_entry_rejects_missing_version(app, make_registry):
    """version is required, not cosmetic -- ansible/playbooks/tasks/resolve_target_bundle.yml
    sets target_version from it, and install_cisco_upgrade.yml depends on
    target_version throughout its pre-check, install, and post-verify steps.
    """
    registry = make_registry()
    with app.app_context(), pytest.raises(registry_store.RegistryError, match='Version is required'):
        registry_store.add_entry(registry, 'iosxe-17.9', _sha512(), _file(), '')


def test_add_entry_rejects_duplicate_name(app, make_registry):
    registry = make_registry()
    with app.app_context():
        registry_store.add_entry(registry, 'dup', _sha512(), _file(), '17.9.1')
        with pytest.raises(registry_store.RegistryError, match='already exists'):
            registry_store.add_entry(registry, 'dup', _sha512(b'other'), _file(b'other'), '17.9.1')


def test_add_entry_rejects_bad_checksum_format(app, make_registry):
    registry = make_registry()
    with app.app_context(), pytest.raises(registry_store.RegistryError, match='128-character hex'):
        registry_store.add_entry(registry, 'bad-hash', 'not-a-hash', _file(), '17.9.1')


def test_add_entry_rejects_checksum_mismatch_and_cleans_up_file(app, make_registry):
    registry = make_registry()
    with app.app_context():
        wrong_hash = _sha512(b'not-the-real-content')
        with pytest.raises(registry_store.RegistryError, match='Checksum mismatch'):
            registry_store.add_entry(registry, 'bad-checksum', wrong_hash, _file(), '17.9.1')
        # rejected upload must not leave an orphaned file or registry entry
        assert registry_store.list_entries(registry) == {}
        assert os.listdir(registry.search_dir) == []


def test_add_entry_refuses_to_write_through_dangling_symlink(app, make_registry, tmp_path):
    """A broken symlink named image.bin in search_dir must not make the
    upload write through to wherever the link points (os.path.exists
    would report False for a dangling symlink and let the save proceed).
    """
    registry = make_registry()
    target = tmp_path / 'outside-search-dir.txt'
    with app.app_context():
        os.symlink(str(target), os.path.join(registry.search_dir, 'image.bin'))
        with pytest.raises(registry_store.RegistryError, match='already exists'):
            registry_store.add_entry(registry, 'iosxe-17.9', _sha512(), _file(), '17.9.1')
    assert not target.exists()


def test_add_entry_rejects_missing_file(app, make_registry):
    registry = make_registry()
    with app.app_context(), pytest.raises(registry_store.RegistryError, match='image file is required'):
        registry_store.add_entry(registry, 'no-file', _sha512(), None, '17.9.1')


def test_on_disk_file_nests_entries_under_software_registry_key(app, make_registry):
    """The written YAML must match the shape Ansible group_vars expects
    (ansible/inventory/group_vars/os_iosxe.yml): entries live
    under a top-level `software_registry` key, not at the file root.
    """
    registry = make_registry()
    with app.app_context():
        registry_store.add_entry(registry, 'iosxe-17.9', _sha512(), _file(), '17.9.1')
        with open(os.path.join(app.config['REGISTRIES_ROOT'], registry.file_path)) as f:
            on_disk = yaml.safe_load(f)
        assert set(on_disk.keys()) == {'software_registry'}
        assert on_disk['software_registry']['iosxe-17.9']['sha512'] == _sha512()


def test_add_entry_preserves_sibling_keys(app, make_registry):
    """Registry files are real Ansible group_vars files with other
    top-level keys -- a save must read-modify-write, never clobber them.
    """
    registry = make_registry()
    with app.app_context():
        _write_raw_registry(app, registry, {
            'image_transport': 'push_scp',
            'distribution_host': 'dist.example.com',
            'software_registry': {},
        })
        registry_store.add_entry(registry, 'iosxe-17.9', _sha512(), _file(), '17.9.1')
        with open(os.path.join(app.config['REGISTRIES_ROOT'], registry.file_path)) as f:
            on_disk = yaml.safe_load(f)
        assert on_disk['image_transport'] == 'push_scp'
        assert on_disk['distribution_host'] == 'dist.example.com'
        assert 'iosxe-17.9' in on_disk['software_registry']


def test_delete_entry_preserves_sibling_keys(app, make_registry):
    registry = make_registry()
    with app.app_context():
        registry_store.add_entry(registry, 'iosxe-17.9', _sha512(), _file(), '17.9.1')
        # list_entries (unlike load_registry) already excludes search_dir,
        # so writing it back raw doesn't create a fake entry named that
        _write_raw_registry(app, registry, {
            'image_transport': 'pull_sftp',
            'distribution_host': 'dist.example.com',
            'software_registry': registry_store.list_entries(registry),
        })

        registry_store.delete_entry(registry, 'iosxe-17.9')

        with open(os.path.join(app.config['REGISTRIES_ROOT'], registry.file_path)) as f:
            on_disk = yaml.safe_load(f)
        assert on_disk['image_transport'] == 'pull_sftp'
        assert on_disk['distribution_host'] == 'dist.example.com'
        assert 'iosxe-17.9' not in on_disk['software_registry']


# -- delete_entry ---------------------------------------------------------

def test_delete_entry_removes_entry_and_file(app, make_registry):
    registry = make_registry()
    with app.app_context():
        registry_store.add_entry(registry, 'iosxe-17.9', _sha512(), _file(), '17.9.1')
        image_path = os.path.join(registry.search_dir, 'image.bin')
        assert os.path.exists(image_path)

        registry_store.delete_entry(registry, 'iosxe-17.9')

        assert registry_store.list_entries(registry) == {}
        assert not os.path.exists(image_path)


def test_delete_entry_rejects_unknown_name(app, make_registry):
    registry = make_registry()
    with app.app_context(), pytest.raises(registry_store.RegistryError, match='No entry named'):
        registry_store.delete_entry(registry, 'nope')


def test_delete_entry_refuses_path_traversal_file_name(app, make_registry, tmp_path):
    """A hand-edited registry file is untrusted -- file_name must not be
    able to walk delete_entry outside search_dir.
    """
    registry = make_registry()
    victim = tmp_path / 'victim.txt'
    victim.write_text('do not delete me')
    with app.app_context():
        _write_raw_registry(app, registry, {
            'software_registry': {
                'evil': {'file_name': str(victim), 'sha512': 'a' * 128, 'file_size': 1},
            }
        })
        with pytest.raises(registry_store.RegistryError, match='unsafe'):
            registry_store.delete_entry(registry, 'evil')
    assert victim.exists()


def test_delete_entry_refuses_non_mapping_entry(app, make_registry):
    registry = make_registry()
    with app.app_context():
        _write_raw_registry(app, registry, {'software_registry': {'oops': 'just-a-string'}})
        with pytest.raises(registry_store.RegistryError, match='not a mapping'):
            registry_store.delete_entry(registry, 'oops')


# -- check_registry ---------------------------------------------------------

def test_check_registry_clean_registry_has_no_issues(app, make_registry):
    registry = make_registry()
    with app.app_context():
        registry_store.add_entry(registry, 'iosxe-17.9', _sha512(), _file(), '17.9.1')
        assert registry_store.check_registry(registry) == []


def test_check_registry_flags_missing_file(app, make_registry):
    registry = make_registry()
    with app.app_context():
        _write_raw_registry(app, registry, {
            'software_registry': {
                'ghost': {'file_name': 'nope.bin', 'sha512': 'a' * 128, 'file_size': 1, 'version': '1.0'},
            }
        })
        issues = registry_store.check_registry(registry)
        assert len(issues) == 1
        assert 'not found' in issues[0]


def test_check_registry_flags_checksum_mismatch_without_touching_it(app, make_registry):
    registry = make_registry()
    with app.app_context():
        registry_store.add_entry(registry, 'iosxe-17.9', _sha512(), _file(), '17.9.1')
        # simulate a hand-edit that swaps in a wrong (but well-formed) checksum
        raw = registry_store.load_registry(registry)
        raw.pop('search_dir', None)
        raw['iosxe-17.9']['sha512'] = 'b' * 128
        registry_store._save_registry(registry, raw)

        issues = registry_store.check_registry(registry)
        assert len(issues) == 1
        assert 'does not match' in issues[0]
        # checksum mismatches are reported, never silently rewritten
        assert registry_store.list_entries(registry)['iosxe-17.9']['sha512'] == 'b' * 128


def test_check_registry_flags_missing_fields(app, make_registry):
    registry = make_registry()
    with app.app_context():
        _write_raw_registry(app, registry, {
            'software_registry': {'broken': {'file_name': 'x.bin'}}
        })
        issues = registry_store.check_registry(registry)
        assert len(issues) == 1
        assert 'missing' in issues[0]
        assert 'sha512' in issues[0]
        assert 'file_size' in issues[0]
        assert 'version' in issues[0]


def test_check_registry_flags_path_traversal_file_name_without_reading_it(app, make_registry, tmp_path):
    """check_registry must not become a read/hash oracle for files outside
    search_dir when file_name has been hand-edited into a path.
    """
    secret = tmp_path / 'secret.key'
    secret.write_text('sensitive material')
    registry = make_registry()
    with app.app_context():
        _write_raw_registry(app, registry, {
            'software_registry': {
                'evil': {'file_name': str(secret), 'sha512': 'a' * 128, 'file_size': 1, 'version': '1.0'},
            }
        })
        issues = registry_store.check_registry(registry)
        assert len(issues) == 1
        assert 'not a safe bare filename' in issues[0]
        assert 'sensitive material' not in ' '.join(issues)


def test_check_registry_flags_entry_that_is_not_a_mapping(app, make_registry):
    registry = make_registry()
    with app.app_context():
        _write_raw_registry(app, registry, {'software_registry': {'oops': 'just-a-string'}})
        issues = registry_store.check_registry(registry)
        assert len(issues) == 1
        assert 'not a mapping' in issues[0]


def test_check_registry_flags_invalid_yaml(app, make_registry):
    registry = make_registry()
    with app.app_context():
        path = os.path.join(app.config['REGISTRIES_ROOT'], registry.file_path)
        with open(path, 'w') as f:
            f.write('software_registry: [this is not valid: yaml')
        issues = registry_store.check_registry(registry)
        assert len(issues) == 1
        assert 'not valid YAML' in issues[0]


def test_check_registry_silently_fixes_stale_file_size(app, make_registry):
    registry = make_registry()
    with app.app_context():
        registry_store.add_entry(registry, 'iosxe-17.9', _sha512(), _file(), '17.9.1')
        image_path = os.path.join(registry.search_dir, 'image.bin')
        # simulate a hand-edit typo in file_size -- the file itself (and its
        # real checksum) is untouched, so this is safely re-derivable
        raw = registry_store.load_registry(registry)
        raw.pop('search_dir', None)
        raw['iosxe-17.9']['file_size'] = 999999
        registry_store._save_registry(registry, raw)

        issues = registry_store.check_registry(registry)

        assert issues == []
        assert registry_store.list_entries(registry)['iosxe-17.9']['file_size'] == os.path.getsize(image_path)


# -- _save_registry ------------------------------------------------------------

def test_save_registry_refuses_non_mapping_document(app, make_registry):
    registry = make_registry()
    with app.app_context():
        path = os.path.join(app.config['REGISTRIES_ROOT'], registry.file_path)
        with open(path, 'w') as f:
            yaml.safe_dump(['this', 'is', 'a', 'list'], f)
        with pytest.raises(registry_store.RegistryError, match='not a mapping'):
            registry_store.add_entry(registry, 'iosxe-17.9', _sha512(), _file(), '17.9.1')
        # refused, not silently replaced -- original content survives
        with open(path) as f:
            assert yaml.safe_load(f) == ['this', 'is', 'a', 'list']


def test_save_registry_does_not_leave_tmp_file_behind(app, make_registry):
    registry = make_registry()
    with app.app_context():
        registry_store.add_entry(registry, 'iosxe-17.9', _sha512(), _file(), '17.9.1')
        path = os.path.join(app.config['REGISTRIES_ROOT'], registry.file_path)
        assert not os.path.exists(f'{path}.tmp')


# -- discover_files -----------------------------------------------------------

def test_discover_files_lists_files_under_root(app, make_registry):
    with app.app_context():
        root = app.config['REGISTRIES_ROOT']
        with open(os.path.join(root, 'a.yml'), 'w') as f:
            f.write('')
        with open(os.path.join(root, 'b.yml'), 'w') as f:
            f.write('')
        assert registry_store.discover_files(root) == ['a.yml', 'b.yml']


def test_discover_files_excludes_given_names(app):
    with app.app_context():
        root = app.config['REGISTRIES_ROOT']
        with open(os.path.join(root, 'a.yml'), 'w') as f:
            f.write('')
        with open(os.path.join(root, 'b.yml'), 'w') as f:
            f.write('')
        assert registry_store.discover_files(root, exclude=['a.yml']) == ['b.yml']


def test_discover_files_nonexistent_root_returns_empty(app):
    with app.app_context():
        assert registry_store.discover_files('/no/such/directory') == []


def test_discover_files_does_not_recurse(app):
    with app.app_context():
        root = app.config['REGISTRIES_ROOT']
        os.makedirs(os.path.join(root, 'subdir'))
        with open(os.path.join(root, 'subdir', 'nested.yml'), 'w') as f:
            f.write('')
        with open(os.path.join(root, 'top.yml'), 'w') as f:
            f.write('')
        assert registry_store.discover_files(root) == ['top.yml']


# -- inspect_file -------------------------------------------------------------

def test_inspect_file_no_registry_key(app):
    with app.app_context():
        root = app.config['REGISTRIES_ROOT']
        with open(os.path.join(root, 'plain.yml'), 'w') as f:
            f.write('')
        info = registry_store.inspect_file(root, 'plain.yml')
        assert info == {'has_registry': False, 'search_dir': None, 'entry_names': []}


def test_inspect_file_existing_registry(app):
    with app.app_context():
        root = app.config['REGISTRIES_ROOT']
        with open(os.path.join(root, 'existing.yml'), 'w') as f:
            yaml.safe_dump({
                'software_registry': {
                    'search_dir': '/opt/images',
                    'iosxe-17.9': {'file_name': 'x.bin', 'sha512': 'a' * 128, 'file_size': 1},
                }
            }, f)
        info = registry_store.inspect_file(root, 'existing.yml')
        assert info == {
            'has_registry': True,
            'search_dir': '/opt/images',
            'entry_names': ['iosxe-17.9'],
        }


def test_inspect_file_rejects_invalid_yaml(app):
    with app.app_context():
        root = app.config['REGISTRIES_ROOT']
        with open(os.path.join(root, 'bad.yml'), 'w') as f:
            f.write('software_registry: [this is not valid: yaml')
        with pytest.raises(registry_store.RegistryError, match='not valid YAML'):
            registry_store.inspect_file(root, 'bad.yml')


def test_inspect_file_rejects_non_mapping_software_registry(app):
    with app.app_context():
        root = app.config['REGISTRIES_ROOT']
        with open(os.path.join(root, 'wrong.yml'), 'w') as f:
            yaml.safe_dump({'software_registry': 'not-a-mapping'}, f)
        with pytest.raises(registry_store.RegistryError, match='not a mapping'):
            registry_store.inspect_file(root, 'wrong.yml')


def test_inspect_file_rejects_relative_path_escape(app):
    with app.app_context():
        root = app.config['REGISTRIES_ROOT']
        with pytest.raises(registry_store.RegistryError, match='outside the registries root'):
            registry_store.inspect_file(root, '../../etc/passwd')


def test_inspect_file_rejects_absolute_path_escape(app):
    with app.app_context():
        root = app.config['REGISTRIES_ROOT']
        with pytest.raises(registry_store.RegistryError, match='outside the registries root'):
            registry_store.inspect_file(root, '/etc/passwd')


def test_inspect_file_raises_for_missing_file(app):
    with app.app_context():
        root = app.config['REGISTRIES_ROOT']
        with pytest.raises(registry_store.RegistryError, match='was not found'):
            registry_store.inspect_file(root, 'nope.yml')


# -- sync_registry --------------------------------------------------------

def test_sync_registry_creates_key_with_search_dir(app, make_registry):
    registry = make_registry()
    with app.app_context():
        registry_store.sync_registry(registry)
        with open(os.path.join(app.config['REGISTRIES_ROOT'], registry.file_path)) as f:
            on_disk = yaml.safe_load(f)
        assert on_disk['software_registry']['search_dir'] == registry.search_dir
