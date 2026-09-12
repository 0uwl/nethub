"""Upgrade routes: host-key confirmation, submit, the approval gates.

Thin over `nethub/upgrades.py`, the way `registry_routes.py` is over
`registry.py`. Two things these handlers must not do, both structural:

- **Never dispatch.** Creating a `queued` row is the only job edge Flask
  writes (design doc §7.3); the sibling owns everything after it. A handler
  that waited for a device would also hold the process behind the phone-home
  route (§3.2).
- **Never persist the device credential.** It goes into the in-memory store
  keyed by the phase job, and from there over §9.1's socket to the sibling.
  Not into a row, not into the session, not into a log.
"""
import ipaddress

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
from . import upgrades
from .credential_socket import CredentialError
from .devices import connection
from .extensions import db
from .models import (
    DeviceHostKey,
    UpgradeHostPhaseResult,
    UpgradePhaseJob,
    UpgradeRun,
    User,
)

upgrade_bp = Blueprint('upgrades', __name__)
hostkeys_bp = Blueprint('hostkeys', __name__)


def _store():
    return current_app.extensions['credential_store']


def _hold(job, password):
    """Put the credential where the sibling can fetch it exactly once."""
    _store().hold(job.id, current_user.device_username, password,
                  approved_by=job.approved_by or current_user.id)


# -- host keys ---------------------------------------------------------------

@hostkeys_bp.route('/hostkeys')
@login_required
def list_hostkeys():
    keys = DeviceHostKey.query.order_by(DeviceHostKey.ansible_host).all()
    return render_template('pages/hostkeys_list.html', keys=keys,
                           users={u.id: u.username for u in User.query.all()})


@hostkeys_bp.route('/hostkeys/scan', methods=['GET', 'POST'])
@login_required
def scan_hostkey():
    """Fetch a fingerprint for a human to compare, out of band.

    This spends no device credential -- the host key is exchanged before
    authentication -- which is exactly what lets confirming an address be its
    own action, decoupled from any run's submit or approval flow (§4.3).
    Scanning stores nothing; only the confirm step below writes a row.
    """
    address = request.form.get('address', '').strip()
    scanned = None
    if request.method == 'POST':
        try:
            ipaddress.ip_address(address)
        except ValueError:
            flash('Enter an IP literal -- a pin keyed on a name means nothing.')
            return render_template('pages/hostkeys_scan.html', address=address)
        try:
            scanned = connection.scan_host_key(address)
        except connection.DeviceConnectionError as exc:
            flash(str(exc))
    return render_template('pages/hostkeys_scan.html', address=address, scanned=scanned)


@hostkeys_bp.route('/hostkeys/confirm', methods=['POST'])
@login_required
def confirm_hostkey():
    address = request.form.get('address', '').strip()
    key_type = request.form.get('key_type', '').strip()
    fingerprint = request.form.get('fingerprint', '').strip()
    if not (address and key_type and fingerprint):
        flash('Nothing to confirm.')
        return redirect(url_for('hostkeys.scan_hostkey'))

    row = DeviceHostKey.query.filter_by(ansible_host=address).first()
    if row is None:
        row = DeviceHostKey(ansible_host=address, key_type=key_type,
                            fingerprint_sha256=fingerprint)
        db.session.add(row)
    elif row.is_confirmed and (row.key_type, row.fingerprint_sha256) != (key_type, fingerprint):
        # Re-accepting a *changed* key is the one thing this screen must not
        # make casual: it is indistinguishable from the attack the pin exists
        # to catch. Deleting the row is deliberate friction.
        flash(f'{address} is already pinned to a different key. Remove the '
              f'existing pin first if the device genuinely changed.')
        return redirect(url_for('hostkeys.list_hostkeys'))
    else:
        row.key_type, row.fingerprint_sha256 = key_type, fingerprint

    row.confirmed_by = current_user.id
    row.confirmed_at = upgrades._utcnow()
    db.session.commit()
    flash(f'Confirmed {address} ({key_type}).', 'success')
    return redirect(url_for('hostkeys.list_hostkeys'))


@hostkeys_bp.route('/hostkeys/<int:key_id>/delete', methods=['POST'])
@login_required
def delete_hostkey(key_id):
    row = db.session.get(DeviceHostKey, key_id)
    if row is not None:
        db.session.delete(row)
        db.session.commit()
        flash(f'Removed the pin for {row.ansible_host}.', 'success')
    return redirect(url_for('hostkeys.list_hostkeys'))


# -- runs --------------------------------------------------------------------

@upgrade_bp.route('/upgrades')
@login_required
def list_runs():
    runs = UpgradeRun.query.order_by(UpgradeRun.created_at.desc()).all()
    return render_template('pages/upgrades_list.html', runs=runs,
                           users={u.id: u.username for u in User.query.all()})


@upgrade_bp.route('/upgrades/new', methods=['GET', 'POST'])
@login_required
def new_run():
    available = artifact_store.list_artifacts()
    form = {
        'bundle': request.form.get('bundle', '').strip(),
        'hosts': request.form.get('hosts', ''),
    }

    if request.method == 'POST':
        password = request.form.get('device_password', '')
        try:
            run, job = upgrades.submit(
                user=current_user,
                bundle=form['bundle'],
                hosts_raw=form['hosts'],
                transport=current_app.config['IMAGE_TRANSPORT'],
                cidrs=current_app.config['DEVICE_TARGET_CIDRS'],
                shared_account_mode=current_app.config['SHARED_ACCOUNT_MODE'],
            )
            # Pre-check has no gate but still opens a session, so the
            # credential is collected here (§8.1's table: "none; runs on
            # submit"). If the store refuses it the run must not exist.
            try:
                _hold(job, password)
            except CredentialError as exc:
                db.session.delete(job)
                db.session.delete(run)
                db.session.commit()
                raise upgrades.RequestError(str(exc)) from None
            flash(f'Submitted run #{run.id}; pre-check is queued.', 'success')
            return redirect(url_for('upgrades.show_run', run_id=run.id))
        except upgrades.RequestError as exc:
            flash(str(exc))

    return render_template('pages/upgrades_new.html', available=available, form=form)


@upgrade_bp.route('/upgrades/<int:run_id>')
@login_required
def show_run(run_id):
    run = db.session.get(UpgradeRun, run_id)
    if run is None:
        flash('No such run.')
        return redirect(url_for('upgrades.list_runs'))
    jobs = (UpgradePhaseJob.query.filter_by(run_id=run.id)
            .order_by(UpgradePhaseJob.created_at, UpgradePhaseJob.id).all())
    results = (UpgradeHostPhaseResult.query.filter_by(run_id=run.id)
               .order_by(UpgradeHostPhaseResult.started_at).all())
    return render_template(
        'pages/upgrade_detail.html', run=run, jobs=jobs, results=results,
        approvable=upgrades.APPROVABLE,
        users={u.id: u.username for u in User.query.all()},
    )


@upgrade_bp.route('/upgrades/<int:run_id>/approve', methods=['POST'])
@login_required
def approve(run_id):
    run = db.session.get(UpgradeRun, run_id)
    phase = request.form.get('phase', '')
    password = request.form.get('device_password', '')
    if run is None:
        flash('No such run.')
        return redirect(url_for('upgrades.list_runs'))
    try:
        job = upgrades.approve(run=run, phase=phase, user=current_user)
        try:
            _hold(job, password)
        except CredentialError as exc:
            # The approval is what supplies the credential, so an approval
            # whose credential is refused must not leave a queued row behind.
            db.session.delete(job)
            run.state, run.awaiting_phase = 'awaiting_approval', phase
            db.session.commit()
            raise upgrades.RequestError(str(exc)) from None
        flash(f'Approved {phase}; queued as job #{job.id}.', 'success')
    except upgrades.RequestError as exc:
        flash(str(exc))
    return redirect(url_for('upgrades.show_run', run_id=run_id))


@upgrade_bp.route('/upgrades/<int:run_id>/decline-cleanup', methods=['POST'])
@login_required
def decline_cleanup(run_id):
    run = db.session.get(UpgradeRun, run_id)
    if run is not None:
        try:
            upgrades.decline_cleanup(run=run)
            flash('Cleanup declined; the run is closed.', 'info')
        except upgrades.RequestError as exc:
            flash(str(exc))
    return redirect(url_for('upgrades.show_run', run_id=run_id))


@upgrade_bp.route('/upgrades/<int:run_id>/cancel', methods=['POST'])
@login_required
def cancel(run_id):
    run = db.session.get(UpgradeRun, run_id)
    if run is not None:
        try:
            upgrades.request_cancel(run=run, user=current_user)
            flash('Cancel requested. A running phase stops between hosts.', 'info')
        except upgrades.RequestError as exc:
            flash(str(exc))
    return redirect(url_for('upgrades.show_run', run_id=run_id))


@upgrade_bp.route('/profile')
@login_required
def profile():
    """The page that sets `users.device_username`.

    This existed as a POST-only route with no template referencing it, so
    there was no way to set a device username through the web UI at all --
    and `upgrades.submit` refuses every run without one, with a message
    telling the submitter to "set it on your profile first". On a fresh
    deployment nobody could submit anything until the row was edited out of
    band.
    """
    return render_template('pages/profile.html')


@upgrade_bp.route('/profile/device-username', methods=['POST'])
@login_required
def set_device_username():
    name = request.form.get('device_username', '').strip()
    current_user.device_username = name or None
    db.session.commit()
    flash('Device username updated.' if name else 'Device username cleared.',
          'success')
    # `request.referrer` was the previous target: attacker-influenced, and the
    # only unvalidated redirect in the app. There is a real page to go back to
    # now, so it is no longer needed for anything.
    return redirect(url_for('upgrades.profile'))
