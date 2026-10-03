"""Artifact routes: the ingest UI, plus the `check-store` CLI command.

Thin over `nethub/artifacts.py` (docs/artifacts.md).
"""
import click
from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required

from . import artifacts as artifact_store
from . import settings
from .extensions import db
from .models import Artifact, User
from .web import confirmed

artifacts_bp = Blueprint('artifacts', __name__)


@artifacts_bp.route('/artifacts')
@login_required
def list_artifacts():
    return render_template(
        'pages/artifacts_list.html',
        artifacts=artifact_store.list_artifacts(),
        staged=artifact_store.list_artifacts(state='staged'),
        two_person=settings.applies('two_person_artifacts', current_user),
        store=current_app.config['ARTIFACT_STORE'],
        users={u.id: u.username for u in User.query.all()},
    )


@artifacts_bp.route('/artifacts/new', methods=['GET', 'POST'])
@login_required
def new_artifact():
    form = {
        'bundle_key': request.form.get('bundle_key', '').strip(),
        'version': request.form.get('version', '').strip(),
        'sha512': request.form.get('sha512', '').strip(),
    }
    if request.method == 'POST':
        try:
            artifact = artifact_store.ingest(
                file_storage=request.files.get('image'),
                bundle_key=form['bundle_key'],
                version=form['version'],
                sha512=form['sha512'],
                user=current_user,
                store=artifact_store.store_dir(current_app.config),
            )
            if artifact.state == 'staged':
                flash(f'Uploaded "{artifact.bundle_key}" ({artifact.file_size:,} bytes). '
                      f'Someone else has to publish it before a run can use it.', 'success')
            else:
                flash(f'Published "{artifact.bundle_key}" ({artifact.file_size:,} bytes).',
                      'success')
            return redirect(url_for('artifacts.list_artifacts'))
        except artifact_store.ArtifactError as exc:
            flash(str(exc))
    return render_template('pages/artifacts_new.html', form=form,
                           two_person=settings.applies('two_person_artifacts', current_user))


@artifacts_bp.route('/artifacts/<int:artifact_id>/publish', methods=['POST'])
@login_required
def publish_artifact(artifact_id):
    artifact = db.session.get(Artifact, artifact_id)
    if artifact is not None:
        try:
            artifact_store.publish(artifact, current_user)
            flash(f'Published "{artifact.bundle_key}".', 'success')
        except artifact_store.ArtifactError as exc:
            flash(str(exc))
    return redirect(url_for('artifacts.list_artifacts'))


@artifacts_bp.route('/artifacts/<int:artifact_id>/delete', methods=['POST'])
@login_required
def delete_artifact(artifact_id):
    if not confirmed('the delete'):
        return redirect(url_for('artifacts.list_artifacts'))
    artifact = db.session.get(Artifact, artifact_id)
    if artifact is not None:
        key = artifact.bundle_key
        try:
            if artifact.state == 'staged':
                artifact_store.withdraw(artifact, current_user)
                flash(f'Withdrew "{key}" and its image file.', 'success')
            elif artifact_store.delete(artifact, current_user):
                flash(f'Deleted "{key}" and its image file.', 'success')
            else:
                flash(f'Delete of "{key}" requested. It stays usable until someone '
                      f'else confirms the delete.', 'info')
        except artifact_store.ArtifactError as exc:
            flash(str(exc))
    return redirect(url_for('artifacts.list_artifacts'))


def register_cli(app):
    @app.cli.command('check-store')
    def check_store():
        """Report artifacts whose bytes are missing or no longer match their
        recorded SHA-512. Exits 1 if there are any.

        A command rather than a button: it hashes every image in the store,
        which is minutes of work at image sizes, and long work does not belong
        in a request handler.
        """
        with app.app_context():
            issues = artifact_store.check_store(artifact_store.store_dir(app.config))
        for issue in issues:
            click.echo(issue, err=True)
        if issues:
            raise SystemExit(1)
        click.echo('Every artifact matches its recorded SHA-512.')
