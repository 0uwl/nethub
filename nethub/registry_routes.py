"""/registries/<id>/entries -- list, add, and delete entries for one
tracked registry. The Registry row itself (which files NetHub tracks)
lives in registries_routes.py.
"""
from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import login_required

from . import registry as registry_store
from .extensions import db
from .models import Registry

registry_bp = Blueprint('registry', __name__)


def _get_registry(registry_id):
    return db.get_or_404(Registry, registry_id)


@registry_bp.route('/registries/<int:registry_id>/entries')
@login_required
def list_entries(registry_id):
    registry = _get_registry(registry_id)
    try:
        entries = registry_store.list_entries(registry)
    except registry_store.RegistryError as e:
        # A hand-edited registry file can be unparsable -- show the error
        # instead of a bare 500, same as the other registry routes.
        flash(str(e))
        entries = {}
    return render_template('pages/registry_list.html', registry=registry, entries=entries)


@registry_bp.route('/registries/<int:registry_id>/entries/new', methods=['GET', 'POST'])
@login_required
def new_entry(registry_id):
    registry = _get_registry(registry_id)
    if request.method == 'POST':
        name = request.form.get('name', '')
        sha512 = request.form.get('sha512', '')
        image = request.files.get('image')
        try:
            registry_store.add_entry(registry, name, sha512, image)
        except registry_store.RegistryError as e:
            flash(str(e))
            return render_template(
                'pages/registry_new.html', registry=registry, name=name, sha512=sha512
            )
        flash(f'Added registry entry "{name}".')
        return redirect(url_for('registry.list_entries', registry_id=registry.id))
    return render_template('pages/registry_new.html', registry=registry, name='', sha512='')


@registry_bp.route('/registries/<int:registry_id>/entries/<name>/delete', methods=['POST'])
@login_required
def delete_entry(registry_id, name):
    registry = _get_registry(registry_id)
    try:
        registry_store.delete_entry(registry, name)
    except registry_store.RegistryError as e:
        flash(str(e))
    else:
        flash(f'Deleted registry entry "{name}".')
    return redirect(url_for('registry.list_entries', registry_id=registry.id))


@registry_bp.route('/registries/<int:registry_id>/entries/check', methods=['POST'])
@login_required
def check_entries(registry_id):
    registry = _get_registry(registry_id)
    # Hashes every registered image on disk -- deliberately a manual,
    # admin-triggered action rather than something that runs on every
    # /registries/<id>/entries page load, since that could mean hashing
    # gigabytes of images on every view.
    issues = registry_store.check_registry(registry)
    if issues:
        for issue in issues:
            flash(issue)
    else:
        flash('Registry check found no issues.')
    return redirect(url_for('registry.list_entries', registry_id=registry.id))
