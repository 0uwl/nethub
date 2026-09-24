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
from datetime import timedelta

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
from .devices.phases import _aware
from .extensions import db
from .models import (
    DeviceHostKey,
    DeviceHostKeyAudit,
    HostKeyScan,
    UpgradeHostPhaseResult,
    UpgradePhaseJob,
    UpgradeRun,
    User,
)

upgrade_bp = Blueprint('upgrades', __name__)
hostkeys_bp = Blueprint('hostkeys', __name__)

#: How long a succeeded scan stays confirmable (WS-6.3). Not strictly
#: required by the design, but cheap: without it a HostKeyScan row would stay
#: "confirmable" forever, and a scan from days ago backing a confirmation of a
#: device that has since changed hands on that address is exactly the kind of
#: staleness a fresh scan is supposed to rule out.
SCAN_CONFIRM_WINDOW = timedelta(minutes=15)


def _store():
    return current_app.extensions['credential_store']


def _hold(job, password):
    """Put the credential where the sibling can fetch it exactly once."""
    _store().hold(job.id, current_user.device_username, password,
                  approved_by=job.approved_by or current_user.id)


def _commit_holding(job, password):
    """Hold the credential for a flushed job, then commit it. In that order.

    The sibling claims any committed `queued` row on its next poll. Committing
    first left a window in which it claimed the job, found no credential and
    failed the run, and the route's cleanup then deleted rows the sibling
    already owned. A flushed row is invisible to the sibling, so holding
    first closes the window and a refusal is a plain rollback.

    If the commit fails, the credential is discarded before the rollback. The
    job id came from the flush, and the rollback frees it for the next insert;
    a credential left under it would be released to someone else's job.
    """
    try:
        _hold(job, password)
    except CredentialError as exc:
        db.session.rollback()
        raise upgrades.RequestError(str(exc)) from None
    try:
        db.session.commit()
    except Exception:
        _store().discard(job.id)
        db.session.rollback()
        raise


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
    """Queue a scan for the sibling to run, and hand back a result page.

    Scanning is device I/O, so it is dispatched to the sibling like a phase
    job rather than run inline in this request (WS-6.2b) -- the same reason
    Flask never opens a device session anywhere else. `check_target` folds in
    WS-5.4's fix (the CIDR check `scan_hostkey` never had): a rejected address
    never becomes a row, and never reaches the sibling at all.
    """
    address = request.form.get('address', '').strip()
    if request.method == 'POST':
        try:
            address = upgrades.check_target(
                address, current_app.config['DEVICE_TARGET_CIDRS']
            )
        except upgrades.RequestError as exc:
            flash(str(exc))
            return render_template('pages/hostkeys_scan.html', address=address)
        scan = HostKeyScan(ansible_host=address, requested_by=current_user.id)
        db.session.add(scan)
        db.session.commit()
        return redirect(url_for('hostkeys.scan_result', scan_id=scan.id))
    return render_template('pages/hostkeys_scan.html', address=address)


@hostkeys_bp.route('/hostkeys/scan/<int:scan_id>')
@login_required
def scan_result(scan_id):
    """Poll a queued scan's outcome. No client-side polling in this app
    (WS-6.2b) -- reload to check, the same as everything else here."""
    scan = db.session.get(HostKeyScan, scan_id)
    if scan is None:
        flash('No such scan.')
        return redirect(url_for('hostkeys.scan_hostkey'))
    return render_template('pages/hostkeys_scan_result.html', scan=scan)


@hostkeys_bp.route('/hostkeys/confirm', methods=['POST'])
@login_required
def confirm_hostkey():
    """Confirm a pin from a scan NetHub itself performed (WS-6.3).

    `key_type`/`fingerprint_sha256`/`address` all come from the referenced
    `HostKeyScan` row, never from the request body -- a POST here carries
    only `scan_id`. Binding to `requested_by == current_user.id` is the
    closest primitive alpha has to "the same session": there is no
    server-side `sessions` row yet (§4.5, a known alpha deviation), so this
    is "the same authenticated user" rather than literally the same session,
    and it does not fully close the separation-of-duty gap -- the same
    person can still scan and then confirm. The real fix is role-based
    access control, out of scope for this alpha (see `CLAUDE.md`).
    """
    raw_scan_id = request.form.get('scan_id', '')
    scan = db.session.get(HostKeyScan, int(raw_scan_id)) if raw_scan_id.isdigit() else None
    if scan is None or scan.status != 'succeeded' or scan.requested_by != current_user.id:
        flash('No matching scan to confirm. Scan the address again.')
        return redirect(url_for('hostkeys.scan_hostkey'))
    if scan.consumed_at is not None:
        # A succeeded scan confirms at most once -- the same one-shot pattern
        # §4.1 uses for the provisioning allowlist. Without this, one scan
        # could back two different confirmations later, reopening the gap
        # this whole route exists to close.
        flash('That scan has already been used to confirm a pin. Scan the address again.')
        return redirect(url_for('hostkeys.scan_hostkey'))
    if upgrades._utcnow() - _aware(scan.finished_at) > SCAN_CONFIRM_WINDOW:
        flash('That scan is too old to confirm. Scan the address again.')
        return redirect(url_for('hostkeys.scan_hostkey'))

    address, key_type, fingerprint = scan.ansible_host, scan.key_type, scan.fingerprint_sha256
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
    scan.consumed_at = upgrades._utcnow()
    db.session.add(DeviceHostKeyAudit(
        ansible_host=address, action='confirmed', key_type=key_type,
        fingerprint_sha256=fingerprint, actor_id=current_user.id,
    ))
    db.session.commit()
    flash(f'Confirmed {address} ({key_type}).', 'success')
    return redirect(url_for('hostkeys.list_hostkeys'))


@hostkeys_bp.route('/hostkeys/<int:key_id>/delete', methods=['POST'])
@login_required
def delete_hostkey(key_id):
    row = db.session.get(DeviceHostKey, key_id)
    if row is not None:
        # Captured before the delete, obviously, not after (WS-6.4) -- this
        # is the pre-image the "deliberate friction" before re-accepting a
        # changed key used to leave no evidence for.
        db.session.add(DeviceHostKeyAudit(
            ansible_host=row.ansible_host, action='deleted',
            key_type=row.key_type, fingerprint_sha256=row.fingerprint_sha256,
            actor_id=current_user.id,
        ))
        db.session.delete(row)
        db.session.commit()
        flash(f'Removed the pin for {row.ansible_host}.', 'success')
    return redirect(url_for('hostkeys.list_hostkeys'))


@hostkeys_bp.route('/hostkeys/history/<address>')
@login_required
def hostkey_history(address):
    """The confirm/delete trail for one address (WS-6.4).

    Keyed on the address string, not on `DeviceHostKey.id` -- the row this
    history is about can be deleted and recreated, and the whole point of
    `DeviceHostKeyAudit` is that it outlives that.
    """
    entries = (DeviceHostKeyAudit.query.filter_by(ansible_host=address)
              .order_by(DeviceHostKeyAudit.at.desc()).all())
    return render_template(
        'pages/hostkey_history.html', address=address, entries=entries,
        users={u.id: u.username for u in User.query.all()},
    )


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
                commit=False,
            )
            # Pre-check has no gate but still opens a session, so the
            # credential is collected here (§8.1's table: "none; runs on
            # submit"). If the store refuses it the run must not exist.
            _commit_holding(job, password)
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
        job = upgrades.approve(run=run, phase=phase, user=current_user,
                               commit=False)
        # The approval is what supplies the credential, so an approval whose
        # credential is refused must not leave a queued row behind.
        _commit_holding(job, password)
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
            # A `queued` job's credential will never be fetched now, so it
            # would otherwise sit in this worker until its TTL or a restart.
            # `discard()` had no callers anywhere in nethub/ before this.
            #
            # Only `queued`: a `running` job already fetched its credential,
            # and `release()` pops before it validates, so there is nothing
            # left to discard. Queried here rather than returned from
            # `request_cancel` -- the store belongs to Flask, and the service
            # module should not grow a signature to serve it.
            for job in UpgradePhaseJob.query.filter_by(run_id=run.id, status='queued'):
                _store().discard(job.id)
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
