"""The single ingest path: upload, hash, size, store, record.

Design doc §3.4. This replaced the `software_registry` YAML store at build
step 7 -- that block existed so an Ansible playbook could read it, and with
the playbooks gone nothing in NetHub read it any more.

Three properties this layer is responsible for:

- **The digest is computed once, over the bytes as they are written**, not by
  re-reading the file afterwards. A 1.2 GB upload plus its hash is the longest
  operation in the system after a device transfer, and Flask holds the only
  unauthenticated route -- doing it in two passes doubles a window §3.2 spends
  its length bounding. It is then consumed three times without recomputation
  (§3.4).
- **The submitted checksum is checked against what we computed**, and a
  mismatch means the bytes never enter the store. The uploader's claim is the
  thing being verified, so it is never what gets recorded.
- **Nothing lands at its final path until it has verified, and the move that
  puts it there cannot overwrite.** Bytes stream to a temporary file in the
  same directory and are linked into place with `os.link` only
  after the digest matches, so a failed or interrupted upload cannot leave a
  half-written image under a name something else would later push.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError
from werkzeug.utils import secure_filename

from .extensions import db
from .models import Artifact

_SHA512_RE = re.compile(r'^[0-9a-f]{128}$')
#: What a bundle key may contain. It reaches no shell and no CLI, but it is
#: user-chosen and used to look rows up, so keep it boring.
_BUNDLE_KEY_RE = re.compile(r'^[\w.+-]{1,80}$')
_CHUNK = 1 << 20


class ArtifactError(Exception):
    """An upload was refused, or the store and the table disagree."""


def _utcnow():
    return datetime.now(timezone.utc)


def store_dir(app_config) -> str:
    path = app_config['ARTIFACT_STORE']
    os.makedirs(path, exist_ok=True)
    return path


def list_artifacts(kind: str = 'image'):
    return (Artifact.query.filter_by(kind=kind, state='published')
            .order_by(Artifact.bundle_key).all())


def get_published(bundle_key: str, platform: str = 'iosxe') -> Artifact:
    """Resolve a request's bundle key server-side.

    A request names a key; it never names a filename or a digest. That is the
    whole point of the key existing (§5) -- a submitted filename against a
    submitted checksum would bypass this table entirely.
    """
    artifact = Artifact.query.filter_by(
        bundle_key=bundle_key, platform=platform, kind='image', state='published'
    ).first()
    if artifact is None:
        raise ArtifactError(f'No published image is registered under "{bundle_key}".')
    if artifact.bytes_state != 'present':
        raise ArtifactError(
            f'"{bundle_key}" was published but its bytes have been pruned; '
            f'it cannot be installed.'
        )
    return artifact


def ingest(*, file_storage, bundle_key, version, sha512, uploaded_by,
           store, kind='image', platform='iosxe') -> Artifact:
    """Take an uploaded file into the store and record it."""
    bundle_key = (bundle_key or '').strip()
    if not _BUNDLE_KEY_RE.match(bundle_key):
        raise ArtifactError('Bundle key must be 1-80 characters of letters, digits, . + - _')
    version = (version or '').strip()
    if not version:
        # Snapshotted onto UpgradeRunHost.version and compared against the
        # device before installing and after the reload.
        raise ArtifactError('Version is required.')
    claimed = (sha512 or '').strip().lower()
    if not _SHA512_RE.match(claimed):
        raise ArtifactError('Checksum must be a 128-character hex SHA-512 value.')
    if not file_storage or not file_storage.filename:
        raise ArtifactError('An image file is required.')
    filename = secure_filename(file_storage.filename)
    if not filename:
        raise ArtifactError('Uploaded file has an unusable name.')

    if Artifact.query.filter_by(bundle_key=bundle_key, platform=platform,
                                kind='image', state='published').first():
        raise ArtifactError(f'An artifact is already published under "{bundle_key}".')
    if Artifact.query.filter(Artifact.filename == filename,
                             Artifact.state.in_(('staged', 'published'))).first():
        raise ArtifactError(f'A live artifact already uses the filename "{filename}".')

    os.makedirs(store, exist_ok=True)
    final_path = os.path.join(store, filename)
    # lexists, not exists: a *broken* symlink here reports False from exists()
    # and the write would then follow the link to wherever it points.
    if os.path.lexists(final_path):
        raise ArtifactError(f'A file named "{filename}" is already in the store.')

    digest = hashlib.sha512()
    size = 0
    fd, temp_path = tempfile.mkstemp(dir=store, prefix='.incoming-')
    try:
        with os.fdopen(fd, 'wb') as out:
            while True:
                chunk = file_storage.stream.read(_CHUNK)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
                out.write(chunk)
        computed = digest.hexdigest()
        if computed != claimed:
            raise ArtifactError(
                f'Checksum mismatch: submitted {claimed}, computed {computed}. '
                f'Upload rejected.'
            )
        if size == 0:
            raise ArtifactError('Uploaded file is empty.')
        # `os.link`, not `os.replace`: link fails with FileExistsError if the
        # target is taken, and replace silently overwrites. The three checks
        # above all ran minutes ago -- before the upload streamed -- so under
        # concurrency they prove nothing by the time we get here. Two uploads
        # sharing a filename used to both reach `os.replace`, and the loser
        # overwrote the winner's already-committed bytes *before* hitting its
        # own IntegrityError at commit. The row then recorded one artifact's
        # SHA-512 against the other's bytes, which is exactly the chain of
        # custody §3.4 is built on, broken silently. The cleanup below only
        # ever removed the temp file, so nothing put the winner's bytes back.
        #
        # Both paths are in `store` by construction (tempfile.mkstemp(dir=store)),
        # so they are on one filesystem and a hard link is available.
        try:
            os.link(temp_path, final_path)
        except FileExistsError:
            raise ArtifactError(
                f'A file named "{filename}" is already in the store. Another '
                f'upload of the same filename finished first.'
            ) from None
        os.unlink(temp_path)
    except Exception:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise

    artifact = Artifact(
        kind=kind, platform=platform, bundle_key=bundle_key, filename=filename,
        sha512=computed, file_size=size, storage_path=final_path, version=version,
        state='published', bytes_state='present', uploaded_by=uploaded_by,
        uploaded_at=_utcnow(),
    )
    db.session.add(artifact)
    try:
        db.session.commit()
    except IntegrityError:
        # The schema is the backstop behind the checks at the top of this
        # function (see the UNIQUE notes in models.py), and it fires here --
        # after the bytes are already at their final path. Losing this race
        # must not leave an orphan file that no row accounts for and that
        # blocks the filename for every later upload.
        db.session.rollback()
        if os.path.exists(final_path):
            os.remove(final_path)
        raise ArtifactError(
            f'Could not record "{filename}": another artifact claimed the same '
            f'filename or bundle key first.'
        ) from None
    return artifact


def delete(artifact: Artifact) -> None:
    """Hard removal of the row and its bytes.

    Deliberately the same no-supersede stance the YAML store had: there is no
    `state` transition here and no audit row, because alpha has neither a
    promotion flow to reverse nor a job table on the publish side to record it.
    Don't add `superseded_by_id` handling without the flow that reads it.
    """
    path = artifact.storage_path
    db.session.delete(artifact)
    db.session.commit()
    if path and os.path.isfile(path):
        os.remove(path)


def check_store(store: str) -> list[str]:
    """Report where the table and the bytes on disk disagree.

    The successor to the YAML store's drift check, and it keeps that check's
    one rule: re-derive what is cheap and always recoverable, flag what is not.
    A wrong `file_size` is re-derived silently; a missing file or a digest that
    no longer matches is reported and never guessed at.
    """
    issues = []
    for artifact in Artifact.query.order_by(Artifact.bundle_key).all():
        if artifact.bytes_state == 'pruned':
            continue
        path = artifact.storage_path
        if not os.path.isfile(path):
            issues.append(f'"{artifact.bundle_key}": {path} is missing.')
            continue
        actual_size = os.path.getsize(path)
        if actual_size != artifact.file_size:
            artifact.file_size = actual_size
        digest = hashlib.sha512()
        with open(path, 'rb') as handle:
            for chunk in iter(lambda: handle.read(_CHUNK), b''):
                digest.update(chunk)
        if digest.hexdigest() != artifact.sha512:
            issues.append(
                f'"{artifact.bundle_key}": {artifact.filename} no longer matches its '
                f'recorded SHA-512. Do not install it.'
            )
    db.session.commit()
    return issues
