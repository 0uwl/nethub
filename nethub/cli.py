"""`flask --app nethub artifacts|hostkeys ...` -- the web app, from a terminal.

A second front end on the same service layer the routes call
(docs/cli.md [cli-is-a-flask-front-end]): it writes what a route would
write and nothing else, and does no device I/O -- a scan is a `queued` row
for the sibling, exactly as from the browser. Not `upgrade_cli`, which is
for when NetHub is down and writes nothing.

Every command that writes takes `--as <username>` and prompts for that
user's NetHub password, checked as `/login` checks it
([cli-acts-as-an-authenticated-user]). That is attribution, not a
boundary: a shell that can write the SQLite file can forge rows anyway.
"""

from __future__ import annotations

import os
import time

import click
from flask import current_app
from flask.cli import AppGroup
from werkzeug.datastructures import FileStorage

from . import artifacts as artifact_store
from . import upgrades, worker_status
from .auth import authenticate
from .extensions import db
from .models import Artifact, DeviceHostKey, DeviceHostKeyAudit, HostKeyScan, User

artifacts_cli = AppGroup('artifacts', help='List, upload, publish and delete images.')
hostkeys_cli = AppGroup('hostkeys', help='List, scan, confirm and remove host-key pins.')

as_option = click.option('--as', 'as_user', required=True, metavar='USERNAME',
                         help='act as this NetHub user (prompts for their password)')
yes_option = click.option('--yes', is_flag=True, help='skip the confirmation prompt')


def _actor(username):
    password = click.prompt(f'NetHub password for {username}', hide_input=True,
                            default='', show_default=False)
    user = authenticate(username, password, source='cli')
    if user is None:
        raise click.ClickException('Invalid username or password.')
    return user


def _usernames():
    return {u.id: u.username for u in User.query.all()}


def _confirm(text, yes):
    if not yes and not click.confirm(text):
        raise click.ClickException('Nothing was changed.')


# -- artifacts ---------------------------------------------------------------

@artifacts_cli.command('list')
@click.option('--state', type=click.Choice(['published', 'staged']), default='published')
def list_artifacts(state):
    users = _usernames()
    for a in artifact_store.list_artifacts(state=state):
        pending = '  (delete requested)' if a.delete_requested_by else ''
        click.echo(f'{a.id:>4}  {a.bundle_key:<24} {a.version:<12} {a.filename}  '
                   f'{a.file_size:,} bytes  by {users.get(a.uploaded_by, "?")}{pending}')


@artifacts_cli.command('add')
@click.argument('path', type=click.Path(exists=True, dir_okay=False))
@click.option('--bundle', required=True, help='bundle key a run will name')
@click.option('--version', required=True, help='version the device reports afterwards')
@click.option('--sha512', required=True, help='expected SHA-512 of the image')
@as_option
def add_artifact(path, bundle, version, sha512, as_user):
    """Upload PATH into the store, as the upload form does."""
    user = _actor(as_user)
    with open(path, 'rb') as stream:
        try:
            artifact = artifact_store.ingest(
                file_storage=FileStorage(stream=stream, filename=os.path.basename(path)),
                bundle_key=bundle, version=version, sha512=sha512, user=user,
                store=artifact_store.store_dir(current_app.config),
            )
        except artifact_store.ArtifactError as exc:
            raise click.ClickException(str(exc)) from None
    if artifact.state == 'staged':
        click.echo(f'Uploaded "{artifact.bundle_key}" as #{artifact.id}. Someone else has '
                   f'to publish it before a run can use it.')
    else:
        click.echo(f'Published "{artifact.bundle_key}" as #{artifact.id}.')


@artifacts_cli.command('publish')
@click.argument('artifact_id', type=int)
@as_option
def publish_artifact(artifact_id, as_user):
    user = _actor(as_user)
    artifact = db.session.get(Artifact, artifact_id)
    if artifact is None:
        raise click.ClickException('No such artifact.')
    try:
        artifact_store.publish(artifact, user)
    except artifact_store.ArtifactError as exc:
        raise click.ClickException(str(exc)) from None
    click.echo(f'Published "{artifact.bundle_key}".')


@artifacts_cli.command('delete')
@click.argument('artifact_id', type=int)
@as_option
@yes_option
def delete_artifact(artifact_id, as_user, yes):
    """Withdraw a staged upload, or delete (or ask to delete) a published one,
    as the web page's Withdraw and Delete buttons do."""
    user = _actor(as_user)
    artifact = db.session.get(Artifact, artifact_id)
    if artifact is None:
        raise click.ClickException('No such artifact.')
    key = artifact.bundle_key
    staged = artifact.state == 'staged'
    _confirm(f'Removes the upload of {key} and its image file. Nothing has used it.'
             if staged else f'Removes {key} and its image file from the store.', yes)
    try:
        if staged:
            artifact_store.withdraw(artifact, user)
            click.echo(f'Withdrew "{key}" and its image file.')
        elif artifact_store.delete(artifact, user):
            click.echo(f'Deleted "{key}" and its image file.')
        else:
            click.echo(f'Delete of "{key}" requested. It stays usable until someone '
                       f'else confirms the delete.')
    except artifact_store.ArtifactError as exc:
        raise click.ClickException(str(exc)) from None


# -- host keys ---------------------------------------------------------------

@hostkeys_cli.command('list')
def list_hostkeys():
    users = _usernames()
    for k in DeviceHostKey.query.order_by(DeviceHostKey.ansible_host):
        status = (f'confirmed by {users.get(k.confirmed_by, "?")} on {k.confirmed_at}'
                  if k.is_confirmed else 'not confirmed, unusable')
        pending = '  (removal requested)' if k.delete_requested_by else ''
        click.echo(f'{k.id:>4}  {k.ansible_host:<16} {k.key_type} {k.fingerprint_sha256}  '
                   f'{status}{pending}')


def _show_scan(scan):
    if scan.status == 'succeeded':
        click.echo(f'Scan #{scan.id}: {scan.ansible_host}  {scan.key_type} '
                   f'{scan.fingerprint_sha256}')
        click.echo(f'Compare that against the device itself, then confirm before '
                   f'{upgrades.confirm_by(scan):%Y-%m-%d %H:%M} UTC with:')
        click.echo(f'  hostkeys confirm {scan.id} --fingerprint '
                   f'"{scan.key_type} {scan.fingerprint_sha256}" --as <username>')
    elif scan.status == 'failed':
        click.echo(f'Scan #{scan.id} failed: {scan.error_summary}')
    else:
        click.echo(f'Scan #{scan.id} is {scan.status}.')
        if scan.status == 'queued' and worker_status.no_worker():
            click.echo('Nothing is picking up work: is the sibling running?')


@hostkeys_cli.command('scan')
@click.argument('address')
@as_option
@click.option('--wait', 'wait_seconds', type=int, default=0, metavar='SECONDS',
              help='poll for the result for up to this long')
def scan_hostkey(address, as_user, wait_seconds):
    """Queue a host-key scan of ADDRESS for the sibling."""
    user = _actor(as_user)
    try:
        scan = upgrades.queue_scan(address.strip(), user=user,
                                   cidrs=current_app.config['DEVICE_TARGET_CIDRS'])
    except upgrades.RequestError as exc:
        raise click.ClickException(str(exc)) from None
    deadline = time.monotonic() + wait_seconds
    while scan.status in ('queued', 'running') and time.monotonic() < deadline:
        time.sleep(2)
        db.session.refresh(scan)
    _show_scan(scan)


@hostkeys_cli.command('show-scan')
@click.argument('scan_id', type=int)
def show_scan(scan_id):
    scan = db.session.get(HostKeyScan, scan_id)
    if scan is None:
        raise click.ClickException('No such scan.')
    _show_scan(scan)


@hostkeys_cli.command('confirm')
@click.argument('scan_id', type=int)
@click.option('--fingerprint', required=True,
              help='"<key-type> SHA256:..." as you checked it against the device')
@as_option
def confirm_hostkey(scan_id, fingerprint, as_user):
    """Pin the key scan SCAN_ID found.

    The pin comes from the scan row; --fingerprint only has to match it. A
    terminal has no page to read the key off, so restating it is what makes
    the confirmation a comparison (docs/cli.md [cli-confirm-restates-fingerprint]).
    """
    user = _actor(as_user)
    scan = db.session.get(HostKeyScan, scan_id)
    key_type, _, digest = fingerprint.strip().rpartition(' ')
    if scan is not None and scan.status == 'succeeded' and (
            digest != scan.fingerprint_sha256 or key_type.strip() not in ('', scan.key_type)):
        raise click.ClickException(
            f'Scan #{scan_id} found {scan.key_type} {scan.fingerprint_sha256}, not '
            f'{fingerprint.strip()}. Nothing was pinned.')
    try:
        row = upgrades.confirm_scan(scan, user)
    except upgrades.RequestError as exc:
        raise click.ClickException(str(exc)) from None
    click.echo(f'Confirmed {row.ansible_host} ({row.key_type}).')


@hostkeys_cli.command('delete')
@click.argument('address')
@as_option
@yes_option
def delete_hostkey(address, as_user, yes):
    """Remove the pin for ADDRESS, or ask for its removal."""
    user = _actor(as_user)
    row = DeviceHostKey.query.filter_by(ansible_host=address).first()
    if row is None:
        raise click.ClickException(f'No pin for {address}.')
    _confirm(f'Runs naming {address} are refused until it is scanned and confirmed again.',
             yes)
    try:
        removed = upgrades.delete_pin(row, user)
    except upgrades.RequestError as exc:
        raise click.ClickException(str(exc)) from None
    if removed:
        click.echo(f'Removed the pin for {address}.')
    else:
        click.echo(f'Removal of the pin for {address} requested. It stays in force '
                   f'until someone else confirms the removal.')


@hostkeys_cli.command('history')
@click.argument('address')
def hostkey_history(address):
    users = _usernames()
    for e in (DeviceHostKeyAudit.query.filter_by(ansible_host=address)
              .order_by(DeviceHostKeyAudit.at.desc())):
        click.echo(f'{e.at}  {e.action:<17} {e.key_type} {e.fingerprint_sha256}  '
                   f'by {users.get(e.actor_id, "?")} ({e.actor_role})')


def register_cli(app):
    app.cli.add_command(artifacts_cli)
    app.cli.add_command(hostkeys_cli)
