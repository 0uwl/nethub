"""software_registry.yml store: load/list/add, guarded by a process-wide lock.

Alpha's stand-in for §7.1's flock (see alpha.md "Registry storage") --
sufficient because Flask alpha runs single-process/single-worker. Multiple
Registry rows may point at files under REGISTRIES_ROOT; the lock is still
global rather than per-registry, matching that same alpha simplification.
`lock` is public (not `_lock`) because registries_routes.py's row-creation
flow (duplicate-name/symlink-alias checks, then insert) needs the same
serialization -- it's not otherwise touching this module's functions.
"""
import hashlib
import os
import re
import threading

import yaml
from flask import current_app
from werkzeug.utils import secure_filename

lock = threading.Lock()

_SHA512_RE = re.compile(r'^[0-9a-fA-F]{128}$')


class RegistryError(Exception):
    """Raised with a human-readable message for any validation failure."""


def _resolve_under_root(root, relative_path):
    """Join relative_path onto root and refuse anything that escapes root
    (via `..` or a symlink) -- REGISTRIES_ROOT is the only directory the
    web UI may read/write registry files from, and file_path is admin-chosen
    but never trusted blindly.
    """
    root_real = os.path.realpath(root)
    candidate_real = os.path.realpath(os.path.join(root, relative_path))
    try:
        inside = os.path.commonpath([root_real, candidate_real]) == root_real
    except ValueError:
        inside = False
    if not inside:
        raise RegistryError(f'"{relative_path}" is outside the registries root.')
    return candidate_real


def _registry_path(registry):
    root = current_app.config['REGISTRIES_ROOT']
    return _resolve_under_root(root, registry.file_path)


def load_registry(registry):
    path = _registry_path(registry)
    if not os.path.isfile(path):
        raise RegistryError(
            f'Registry file "{registry.file_path}" was not found under the registries '
            'root -- it may have been unmounted.'
        )
    with open(path, 'r') as f:
        try:
            data = yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise RegistryError(f'Registry file is not valid YAML: {e}')
    # On-disk shape matches an Ansible group_vars file: entries live under a
    # top-level `software_registry` key (see ansible/inventory/rendered),
    # not at the file's root.
    entries = (data or {}).get('software_registry', {}) if isinstance(data, dict) else {}
    if not isinstance(entries, dict):
        raise RegistryError('Registry file\'s "software_registry" key is not a mapping.')
    entries = dict(entries)
    # search_dir is now DB-authoritative: it's both where NetHub itself
    # reads/writes this registry's image bytes and the value exported into
    # the file for whatever Ansible control host reads it, so always
    # reflect registry.search_dir here rather than trusting (and letting a
    # hand-edited file silently diverge from) whatever the file has.
    entries['search_dir'] = registry.search_dir
    return entries


def list_entries(registry):
    data = dict(load_registry(registry))
    data.pop('search_dir', None)
    return data


def _save_registry(registry, data):
    path = _registry_path(registry)
    try:
        with open(path, 'r') as f:
            try:
                document = yaml.safe_load(f)
            except yaml.YAMLError as e:
                raise RegistryError(f'Registry file is not valid YAML: {e}')
    except FileNotFoundError:
        raise RegistryError(
            f'Registry file "{registry.file_path}" was not found under the registries '
            'root -- it may have been unmounted.'
        )
    # These files are real Ansible group_vars files with other top-level
    # keys (image_transport, distribution_host, ...) that must survive a
    # registry edit -- read-modify-write the whole document, never just
    # dump {'software_registry': data} over the file's existing contents.
    # A non-mapping document (e.g. a YAML list) can't have a key merged
    # into it -- refuse rather than silently replacing it with one.
    if document is None:
        document = {}
    elif not isinstance(document, dict):
        raise RegistryError(
            f'"{registry.file_path}"\'s top level is not a mapping -- refusing to overwrite it.'
        )
    document['software_registry'] = data
    # Write-then-rename so a crash or full disk mid-write can't leave the
    # admin's real group_vars file truncated -- os.replace is atomic
    # within the same directory/filesystem.
    tmp_path = f'{path}.tmp'
    with open(tmp_path, 'w') as f:
        yaml.safe_dump(document, f, default_flow_style=False)
    os.replace(tmp_path, path)


def _safe_entry_filename(entry):
    """Return entry['file_name'] if it's a bare filename that survives
    secure_filename() unchanged, None otherwise. The registry file is
    hand-editable (see module docstring), so a `file_name` read back from
    it is untrusted -- without this, a `../` or absolute path there would
    let delete/check operate outside search_dir.
    """
    file_name = entry.get('file_name')
    if not isinstance(file_name, str) or secure_filename(file_name) != file_name:
        return None
    return file_name


def _sha512_of_file(path):
    h = hashlib.sha512()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def add_entry(registry, name, sha512, file_storage):
    with lock:
        data = load_registry(registry)

        name = (name or '').strip()
        if not name:
            raise RegistryError('Name is required.')
        if name in data:
            raise RegistryError(f'An entry named "{name}" already exists.')

        sha512 = (sha512 or '').strip().lower()
        if not _SHA512_RE.match(sha512):
            raise RegistryError('Checksum must be a 128-character hex SHA-512 value.')

        if not file_storage or not file_storage.filename:
            raise RegistryError('An image file is required.')

        search_dir = registry.search_dir
        os.makedirs(search_dir, exist_ok=True)

        filename = secure_filename(file_storage.filename)
        if not filename:
            raise RegistryError('Uploaded file has an unusable name.')

        dest_path = os.path.join(search_dir, filename)
        # lexists, not exists -- a *broken* symlink already at dest_path
        # would make exists() report False, and file_storage.save() below
        # would then write through the link to wherever it points.
        if os.path.lexists(dest_path):
            raise RegistryError(f'A file named "{filename}" already exists.')

        file_storage.save(dest_path)

        computed = _sha512_of_file(dest_path)
        if computed != sha512:
            os.remove(dest_path)
            raise RegistryError(
                f'Checksum mismatch: submitted {sha512}, computed {computed}. '
                'Upload rejected.'
            )

        data[name] = {
            'file_name': filename,
            'sha512': sha512,
            'file_size': os.path.getsize(dest_path),
        }
        _save_registry(registry, data)


def delete_entry(registry, name):
    with lock:
        data = load_registry(registry)

        entry = data.get(name)
        if entry is None:
            raise RegistryError(f'No entry named "{name}" exists.')
        if not isinstance(entry, dict):
            raise RegistryError(f'Entry "{name}" is not a mapping -- fix it by hand first.')

        file_name = _safe_entry_filename(entry)
        if file_name is None:
            raise RegistryError(f'Entry "{name}" has a missing or unsafe file_name -- fix it by hand first.')

        image_path = os.path.join(registry.search_dir, file_name)
        if os.path.lexists(image_path):
            os.remove(image_path)

        del data[name]
        _save_registry(registry, data)


def check_registry(registry):
    """Validate every entry against the images on disk -- a hand-edited
    registry file is the expected way this drifts. Silently re-derives
    file_size (cheap, non-security, always recoverable from the file
    itself); everything else -- a missing file, a wrong checksum, a
    malformed entry -- is left untouched and reported instead, since
    silently "fixing" any of those would mean guessing at the truth.
    Returns a list of human-readable issue strings, empty if clean.
    """
    with lock:
        try:
            data = load_registry(registry)
        except RegistryError as e:
            return [str(e)]

        search_dir = registry.search_dir
        issues = []
        changed = False

        for name, entry in data.items():
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

                file_name = _safe_entry_filename(entry)
                if file_name is None:
                    issues.append(f'Entry "{name}": file_name is missing or not a safe bare filename.')
                    continue

                image_path = os.path.join(search_dir, file_name)
                if not os.path.isfile(image_path):
                    issues.append(f'Entry "{name}": file "{file_name}" not found in {search_dir}.')
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
            _save_registry(registry, data)

        return issues


def sync_registry(registry):
    """Force-write registry's current state back to disk -- used right
    after a Registry row is created, so its `software_registry` key (and
    search_dir) shows up in the file immediately instead of waiting for a
    first entry to be added.
    """
    with lock:
        _save_registry(registry, load_registry(registry))


def discover_files(root, exclude=()):
    """Flat (non-recursive) listing of regular files directly under root,
    for the "adopt a registry file" picker -- admins bind-mount files
    straight into REGISTRIES_ROOT, not into subdirectories.
    """
    if not os.path.isdir(root):
        return []
    excluded = set(exclude)
    return sorted(
        name for name in os.listdir(root)
        if name not in excluded and os.path.isfile(os.path.join(root, name))
    )


def inspect_file(root, relative_path):
    """Peek at a candidate file under root before a Registry row exists,
    for the "create registry" form's prefill. Returns a dict with
    has_registry, search_dir (from the file, or None) and entry_names.
    """
    path = _resolve_under_root(root, relative_path)
    if not os.path.isfile(path):
        raise RegistryError(f'"{relative_path}" was not found under {root}.')

    with open(path, 'r') as f:
        try:
            document = yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise RegistryError(f'"{relative_path}" is not valid YAML: {e}')

    if not isinstance(document, dict) or 'software_registry' not in document:
        return {'has_registry': False, 'search_dir': None, 'entry_names': []}

    entries = document['software_registry']
    if not isinstance(entries, dict):
        raise RegistryError(f'"{relative_path}"\'s "software_registry" key is not a mapping.')

    return {
        'has_registry': True,
        'search_dir': entries.get('search_dir'),
        'entry_names': sorted(k for k in entries if k != 'search_dir'),
    }
