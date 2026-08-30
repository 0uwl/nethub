"""/registries -- list, create, and delete Registry rows (which files
NetHub tracks). Entry-level routes for one registry's contents live in
registry_routes.py, under /registries/<id>/entries.
"""
import os

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from flask_login import login_required

from . import registry as registry_store
from .extensions import db
from .models import Registry

registries_bp = Blueprint('registries', __name__)


@registries_bp.route('/registries')
@login_required
def list_registries():
    registries = Registry.query.order_by(Registry.name).all()
    return render_template('pages/registries_list.html', registries=registries)


@registries_bp.route('/registries/new', methods=['GET', 'POST'])
@login_required
def new_registry():
    root = current_app.config['REGISTRIES_ROOT']
    existing = Registry.query.all()
    claimed = [r.file_path for r in existing]
    available = registry_store.discover_files(root, exclude=claimed)

    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        file_path = request.form.get('file_path', '')
        search_dir = request.form.get('search_dir', '').strip()
        form_state = {'available': available, 'name': name, 'file_path': file_path, 'search_dir': search_dir}

        if not name:
            flash('Name is required.')
            return render_template('pages/registries_new.html', **form_state)
        if file_path not in available:
            flash('Choose one of the listed files.')
            return render_template('pages/registries_new.html', **form_state)

        try:
            info = registry_store.inspect_file(root, file_path)
        except registry_store.RegistryError as e:
            flash(str(e))
            return render_template('pages/registries_new.html', **form_state)

        effective_search_dir = search_dir or info['search_dir']
        if not effective_search_dir or not isinstance(effective_search_dir, str):
            flash('This file has no search_dir yet -- enter one to continue.')
            return render_template('pages/registries_new.html', **form_state)
        if not os.path.isabs(effective_search_dir):
            flash(f'"{effective_search_dir}" must be an absolute path.')
            return render_template('pages/registries_new.html', **form_state)
        if not os.path.isdir(effective_search_dir) or not os.access(effective_search_dir, os.W_OK):
            flash(f'"{effective_search_dir}" is not a writable directory NetHub can reach.')
            return render_template('pages/registries_new.html', **form_state)

        # Re-check name/file uniqueness under the same lock registry.py's
        # own writes use, and hold it through the insert -- otherwise two
        # concurrent submits (two admins, or a double form-submit) could
        # both pass these checks before either commits, defeating the
        # symlink-alias check above (it only compares against rows that
        # existed when this request started) or crashing on the DB's
        # unique constraint instead of a flashed error.
        with registry_store.lock:
            if Registry.query.filter_by(name=name).first():
                flash(f'A registry named "{name}" already exists.')
                return render_template('pages/registries_new.html', **form_state)
            candidate_real = os.path.realpath(os.path.join(root, file_path))
            already_tracked = any(
                os.path.realpath(os.path.join(root, r.file_path)) == candidate_real
                for r in Registry.query.all()
            )
            if already_tracked:
                flash('This file is already tracked by another registry.')
                return render_template('pages/registries_new.html', **form_state)

            registry = Registry(name=name, file_path=file_path, search_dir=effective_search_dir)
            db.session.add(registry)
            db.session.commit()
        # Outside the lock -- sync_registry() takes it itself (not
        # reentrant), and the DB insert above is already what the lock
        # was protecting; a crash in this narrow window just leaves the
        # row's file un-synced until the next entry add, same as before.
        registry_store.sync_registry(registry)

        n = len(info['entry_names'])
        found = f' ({n} existing {"entry" if n == 1 else "entries"} found)' if n else ''
        flash(f'Registry "{name}" created{found}.')
        return redirect(url_for('registry.list_entries', registry_id=registry.id))

    return render_template(
        'pages/registries_new.html', available=available, name='', file_path='', search_dir=''
    )


@registries_bp.route('/registries/<int:registry_id>/delete', methods=['POST'])
@login_required
def delete_registry(registry_id):
    # Row only -- the file and any images under search_dir are the admin's
    # own, not NetHub's to delete (see nethub/models.py's Registry docstring).
    registry = db.get_or_404(Registry, registry_id)
    db.session.delete(registry)
    db.session.commit()
    flash(f'Stopped tracking registry "{registry.name}". The file and its images were left untouched.')
    return redirect(url_for('registries.list_registries'))
