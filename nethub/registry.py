"""software_registry.yml store: load/list/add, guarded by a process-wide lock.

Alpha's stand-in for §7.1's flock (see alpha.md "Registry storage") --
sufficient because Flask alpha runs single-process/single-worker.
"""
import hashlib
import os
import re
import threading
import yaml

from flask import current_app
from werkzeug.utils import secure_filename

_lock = threading.Lock()

_SHA512_RE = re.compile(r'^[0-9a-fA-F]{128}$')


class RegistryError(Exception):
    """Raised with a human-readable message for any validation failure."""


def _registry_path():
    path = current_app.config['REGISTRY_FILE']
    dirname = os.path.dirname(path)
    if dirname:
        os.makedirs(dirname, exist_ok=True)
    return path


def load_registry():
    path = _registry_path()
    if not os.path.exists(path):
        return {'search_dir': current_app.config['IMAGE_DIR']}
    with open(path, 'r') as f:
        try:
            data = yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise RegistryError(f'Registry file is not valid YAML: {e}')
    # On-disk shape matches an Ansible group_vars file: entries live under a
    # top-level `software_registry` key (see ansible/inventory/rendered),
    # not at the file's root.
    registry = (data or {}).get('software_registry', {}) if isinstance(data, dict) else {}
    if not isinstance(registry, dict):
        raise RegistryError('Registry file\'s "software_registry" key is not a mapping.')
    registry.setdefault('search_dir', current_app.config['IMAGE_DIR'])
    return registry


def list_entries():
    data = dict(load_registry())
    data.pop('search_dir', None)
    return data


def _save_registry(data):
    path = _registry_path()
    with open(path, 'w') as f:
        yaml.safe_dump({'software_registry': data}, f, default_flow_style=False)


def _sha512_of_file(path):
    h = hashlib.sha512()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def add_entry(name, sha512, file_storage):
    with _lock:
        registry = load_registry()

        name = (name or '').strip()
        if not name:
            raise RegistryError('Name is required.')
        if name in registry:
            raise RegistryError(f'An entry named "{name}" already exists.')

        sha512 = (sha512 or '').strip().lower()
        if not _SHA512_RE.match(sha512):
            raise RegistryError('Checksum must be a 128-character hex SHA-512 value.')

        if not file_storage or not file_storage.filename:
            raise RegistryError('An image file is required.')

        IMAGE_DIR = current_app.config['IMAGE_DIR']
        os.makedirs(IMAGE_DIR, exist_ok=True)

        filename = secure_filename(file_storage.filename)
        if not filename:
            raise RegistryError('Uploaded file has an unusable name.')

        dest_path = os.path.join(IMAGE_DIR, filename)
        if os.path.exists(dest_path):
            raise RegistryError(f'A file named "{filename}" already exists.')

        file_storage.save(dest_path)

        computed = _sha512_of_file(dest_path)
        if computed != sha512:
            os.remove(dest_path)
            raise RegistryError(
                f'Checksum mismatch: submitted {sha512}, computed {computed}. '
                'Upload rejected.'
            )

        registry[name] = {
            'file_name': filename,
            'sha512': sha512,
            'file_size': os.path.getsize(dest_path),
        }
        _save_registry(registry)


def delete_entry(name):
    with _lock:
        registry = load_registry()

        entry = registry.get(name)
        if entry is None:
            raise RegistryError(f'No entry named "{name}" exists.')

        image_path = os.path.join(current_app.config['IMAGE_DIR'], entry['file_name'])
        if os.path.exists(image_path):
            os.remove(image_path)

        del registry[name]
        _save_registry(registry)


def check_registry():
    """Validate every entry against the images on disk -- a hand-edited
    registry file is the expected way this drifts. Silently re-derives
    file_size (cheap, non-security, always recoverable from the file
    itself); everything else -- a missing file, a wrong checksum, a
    malformed entry -- is left untouched and reported instead, since
    silently "fixing" any of those would mean guessing at the truth.
    Returns a list of human-readable issue strings, empty if clean.
    """
    with _lock:
        try:
            registry = load_registry()
        except RegistryError as e:
            return [str(e)]

        images_dir = current_app.config['IMAGE_DIR']
        issues = []
        changed = False

        for name, entry in registry.items():
            if name == 'search_dir':
                continue
            try:
                if not isinstance(entry, dict):
                    issues.append(f'Entry "{name}": not a mapping, skipped.')
                    continue

                missing = [f for f in ('file_name', 'sha512', 'file_size') if f not in entry]
                if missing:
                    issues.append(f'Entry "{name}": missing {", ".join(missing)}.')
                    continue

                image_path = os.path.join(images_dir, str(entry['file_name']))
                if not os.path.isfile(image_path):
                    issues.append(
                        f'Entry "{name}": file "{entry["file_name"]}" not found in {images_dir}.'
                    )
                    continue

                actual_size = os.path.getsize(image_path)
                if entry['file_size'] != actual_size:
                    entry['file_size'] = actual_size
                    changed = True

                sha512 = str(entry['sha512'])
                if not _SHA512_RE.match(sha512):
                    issues.append(f'Entry "{name}": sha512 is not a 128-character hex value.')
                    continue

                actual_sha512 = _sha512_of_file(image_path)
                if sha512.lower() != actual_sha512:
                    issues.append(
                        f'Entry "{name}": sha512 does not match the file on disk '
                        f'(registry: {sha512}, computed: {actual_sha512}).'
                    )
            except (TypeError, OSError) as e:
                issues.append(f'Entry "{name}": could not be checked ({e}).')

        if changed:
            _save_registry(registry)

        return issues
