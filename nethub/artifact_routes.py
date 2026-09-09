"""Artifact routes: the ingest UI and the store's drift check.

Thin over `nethub/artifacts.py`. This replaced `registry_routes.py` at build
step 7; there is no longer a file to adopt or a pointer row to manage, so the
two-blueprint split that layer needed (`registries` for files, `registry` for
their entries) collapses into one.
"""
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
from .extensions import db
from .models import Artifact, User

artifacts_bp = Blueprint('artifacts', __name__)


@artifacts_bp.route('/artifacts')
@login_required
def list_artifacts():
    return render_template(
        'pages/artifacts_list.html',
        artifacts=artifact_store.list_artifacts(),
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
                uploaded_by=current_user.id,
                store=artifact_store.store_dir(current_app.config),
            )
            flash(f'Published "{artifact.bundle_key}" ({artifact.file_size:,} bytes).')
            return redirect(url_for('artifacts.list_artifacts'))
        except artifact_store.ArtifactError as exc:
            flash(str(exc))
    return render_template('pages/artifacts_new.html', form=form)


@artifacts_bp.route('/artifacts/<int:artifact_id>/delete', methods=['POST'])
@login_required
def delete_artifact(artifact_id):
    artifact = db.session.get(Artifact, artifact_id)
    if artifact is not None:
        key = artifact.bundle_key
        artifact_store.delete(artifact)
        flash(f'Deleted "{key}" and its image file.')
    return redirect(url_for('artifacts.list_artifacts'))


@artifacts_bp.route('/artifacts/check', methods=['POST'])
@login_required
def check_artifacts():
    """Not run on page load: it hashes every image in the store."""
    issues = artifact_store.check_store(artifact_store.store_dir(current_app.config))
    for issue in issues:
        flash(issue)
    if not issues:
        flash('Every artifact matches its recorded SHA-512.')
    return redirect(url_for('artifacts.list_artifacts'))
