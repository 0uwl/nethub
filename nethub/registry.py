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
        data = yaml.safe_load(f)
    # On-disk shape matches an Ansible group_vars file: entries live under a
    # top-level `software_registry` key (see ansible/inventory/rendered),
    # not at the file's root.
    registry = (data or {}).get('software_registry') or {}
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
