"""/registry, /registry/new -- list and add software_registry.yml entries."""
from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import login_required

from . import registry as registry_store

registry_bp = Blueprint('registry', __name__)


@registry_bp.route('/registry')
@login_required
def list_entries():
    try:
        entries = registry_store.list_entries()
    except registry_store.RegistryError as e:
        # A hand-edited registry file can be unparsable -- show the error
        # instead of a bare 500, same as the other registry routes.
        flash(str(e))
        entries = {}
    return render_template('pages/registry_list.html', entries=entries)


@registry_bp.route('/registry/new', methods=['GET', 'POST'])
@login_required
def new_entry():
    if request.method == 'POST':
        name = request.form.get('name', '')
        sha512 = request.form.get('sha512', '')
        image = request.files.get('image')
        try:
            registry_store.add_entry(name, sha512, image)
        except registry_store.RegistryError as e:
            flash(str(e))
            return render_template('pages/registry_new.html', name=name, sha512=sha512)
        flash(f'Added registry entry "{name}".')
        return redirect(url_for('registry.list_entries'))
    return render_template('pages/registry_new.html', name='', sha512='')


@registry_bp.route('/registry/<name>/delete', methods=['POST'])
@login_required
def delete_entry(name):
    try:
        registry_store.delete_entry(name)
    except registry_store.RegistryError as e:
        flash(str(e))
    else:
        flash(f'Deleted registry entry "{name}".')
    return redirect(url_for('registry.list_entries'))


@registry_bp.route('/registry/check', methods=['POST'])
@login_required
def check_entries():
    # Hashes every registered image on disk -- deliberately a manual,
    # admin-triggered action rather than something that runs on every
    # /registry page load, since that could mean hashing gigabytes of
    # images on every view.
    issues = registry_store.check_registry()
    if issues:
        for issue in issues:
            flash(issue)
    else:
        flash('Registry check found no issues.')
    return redirect(url_for('registry.list_entries'))
