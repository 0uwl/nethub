"""Upgrade routes: host-key confirmation, submit, the approval gates.

Thin over `nethub/upgrades.py`. Two things these handlers must not do:

- **Never dispatch.** Creating a `queued` row is the only job edge Flask
  writes; the sibling owns everything after it
  (docs/architecture.md [no-device-io-in-flask]).
- **Never keep the device credential readable.** It is sealed to the
  sibling's public key into the job row it was collected for
  (`upgrades._seal_into`): Flask can write it and never read it back. Not
  into the session, not into a log.
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
from . import settings, upgrades, worker_status
from .devices.phases import _aware
from .extensions import db
from .models import (
    STATE_BEFORE,
    DeviceHostKey,
    DeviceHostKeyAudit,
    HostKeyScan,
    UpgradeHostPhaseResult,
    UpgradePhaseJob,
    UpgradeRun,
    User,
)
from .web import confirmed

upgrade_bp = Blueprint('upgrades', __name__)
hostkeys_bp = Blueprint('hostkeys', __name__)

#: How long a succeeded scan stays confirmable. Not strictly
#: required by the design, but cheap: without it a HostKeyScan row would stay
#: "confirmable" forever, and a scan from days ago backing a confirmation of a
#: device that has since changed hands on that address is exactly the kind of
#: staleness a fresh scan is supposed to rule out.
SCAN_CONFIRM_WINDOW = timedelta(minutes=15)


def may_confirm(scan, user):
    """Whether `user` may confirm a pin from `scan`.

    With the two-person rule for host keys off, only whoever requested the
    scan confirms it. With it on, the confirmer must be
    someone else, except that an admin may confirm any scan,
    their own included: admins are exempt, and the audit row records the
    role they acted in.
    """
    own = scan.requested_by == user.id
    if settings.enabled('two_person_hostkeys'):
        return not own or user.is_admin
    return own


def confirm_by(scan):
    """The moment `scan` stops being confirmable (`SCAN_CONFIRM_WINDOW`)."""
    return _aware(scan.finished_at) + SCAN_CONFIRM_WINDOW


def _when(job):
    """What to add to an approval's flash: nothing, or the window it waits
    for. UTC, like the form and the page (`upgrades.start_time`)."""
    if job.not_before is None:
        return ''
    return f', scheduled for {job.not_before:%Y-%m-%d %H:%M} UTC'


# -- host keys ---------------------------------------------------------------

@hostkeys_bp.route('/hostkeys')
@login_required
def list_hostkeys():
    keys = DeviceHostKey.query.order_by(DeviceHostKey.ansible_host).all()
    # Scans someone could still confirm, so the person who has to confirm one
    # finds it here rather than depending on a forwarded link.
    waiting = [(scan, confirm_by(scan)) for scan in HostKeyScan.query.filter(
        HostKeyScan.status == 'succeeded', HostKeyScan.consumed_at.is_(None),
        HostKeyScan.finished_at > upgrades._utcnow() - SCAN_CONFIRM_WINDOW,
    ).order_by(HostKeyScan.finished_at)]
    return render_template('pages/hostkeys_list.html', keys=keys, waiting=waiting,
                           two_person=settings.applies('two_person_hostkeys', current_user),
                           users={u.id: u.username for u in User.query.all()})


@hostkeys_bp.route('/hostkeys/scan', methods=['GET', 'POST'])
@login_required
def scan_hostkey():
    """Queue a scan for the sibling to run, and hand back a result page.

    Scanning is device I/O, so it is dispatched to the sibling like a phase
    job rather than run inline in this request -- the same reason
    Flask never opens a device session anywhere else. `check_target` applies
    the same CIDR check as submit: a rejected address never becomes a row,
    and never reaches the sibling at all.
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
    """A queued scan's outcome. There is no JavaScript in this app, so the
    page reloads itself with a meta refresh while the scan is queued or
    running, and says so if nothing is picking up work."""
    scan = db.session.get(HostKeyScan, scan_id)
    if scan is None:
        flash('No such scan.')
        return redirect(url_for('hostkeys.scan_hostkey'))
    return render_template(
        'pages/hostkeys_scan_result.html', scan=scan,
        can_confirm=may_confirm(scan, current_user),
        confirm_by=confirm_by(scan) if scan.status == 'succeeded' else None,
        no_worker=scan.status == 'queued' and worker_status.no_worker(),
    )


@hostkeys_bp.route('/hostkeys/confirm', methods=['POST'])
@login_required
def confirm_hostkey():
    """Confirm a pin from a scan NetHub itself performed.

    `key_type`/`fingerprint_sha256`/`address` all come from the referenced
    `HostKeyScan` row, never from the request body -- a POST here carries
    only `scan_id`. Who may confirm which scan is `may_confirm`: the scanner
    alone with the two-person rule off, anyone else with it on. The rule is
    read now, so turning it off releases a scan waiting for a second person.
    """
    raw_scan_id = request.form.get('scan_id', '')
    scan = db.session.get(HostKeyScan, int(raw_scan_id)) if raw_scan_id.isdigit() else None
    if scan is None or scan.status != 'succeeded':
        flash('No matching scan to confirm. Scan the address again.')
        return redirect(url_for('hostkeys.scan_hostkey'))
    if not may_confirm(scan, current_user):
        if scan.requested_by == current_user.id:
            flash('The two-person rule for host keys is on: someone other than '
                  'whoever requested the scan has to confirm it.')
            return redirect(url_for('hostkeys.scan_result', scan_id=scan.id))
        flash('No matching scan to confirm. Scan the address again.')
        return redirect(url_for('hostkeys.scan_hostkey'))
    if scan.consumed_at is not None:
        # A succeeded scan confirms at most once. Without this, one scan
        # could back two different confirmations later.
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
        requested_by=scan.requested_by, actor_role=current_user.role,
    ))
    db.session.commit()
    flash(f'Confirmed {address} ({key_type}).', 'success')
    return redirect(url_for('hostkeys.list_hostkeys'))


@hostkeys_bp.route('/hostkeys/<int:key_id>/delete', methods=['POST'])
@login_required
def delete_hostkey(key_id):
    if not confirmed('removing the pin'):
        return redirect(url_for('hostkeys.list_hostkeys'))
    row = db.session.get(DeviceHostKey, key_id)
    if row is None:
        return redirect(url_for('hostkeys.list_hostkeys'))
    address = row.ansible_host
    try:
        if upgrades.delete_pin(row, current_user):
            flash(f'Removed the pin for {address}.', 'success')
        else:
            flash(f'Removal of the pin for {address} requested. It stays in force '
                  f'until someone else confirms the removal.', 'info')
    except upgrades.RequestError as exc:
        flash(str(exc))
    return redirect(url_for('hostkeys.list_hostkeys'))


@hostkeys_bp.route('/hostkeys/history/<address>')
@login_required
def hostkey_history(address):
    """The confirm/delete trail for one address.

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
            run, _job = upgrades.submit(
                user=current_user,
                bundle=form['bundle'],
                hosts_raw=form['hosts'],
                cidrs=current_app.config['DEVICE_TARGET_CIDRS'],
                password=password,
                public_key=current_app.extensions['credential_public_key'],
            )
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
    # Reload while the sibling has work on this run, say so
    # when it has stopped beating or nobody is taking work, and tell an
    # approver what their approval would queue behind. All reads.
    # A job approved for a maintenance window is queued but not waiting for a
    # worker, so it neither refreshes the page every five seconds all
    # day nor trips the "no worker" notice; it gets its own line instead.
    live = [j for j in jobs
            if j.status == 'running' or worker_status.is_waiting(j)]
    scheduled = [j for j in jobs
                 if j.status == 'queued' and not worker_status.is_waiting(j)]
    queued_ahead, running_now = worker_status.queue_depth()
    gate_hosts = []
    if run.state == 'awaiting_approval' and run.awaiting_phase in STATE_BEFORE:
        before = STATE_BEFORE[run.awaiting_phase]
        gate_hosts = [h for h in run.hosts if h.state == before]
    return render_template(
        'pages/upgrade_detail.html', run=run, jobs=jobs, results=results,
        retryable=upgrades.retryable_phases(run),
        users={u.id: u.username for u in User.query.all()},
        reload_cap=current_app.config['PHASE_CONCURRENCY'],
        refresh=bool(live),
        stalled_ids={j.id for j in live if worker_status.is_stalled(j)},
        no_worker=any(j.status == 'queued' for j in live) and worker_status.no_worker(),
        queued_ahead=queued_ahead, running_now=running_now,
        gate_hosts=gate_hosts, scheduled=scheduled,
        needs_other_approver=upgrades.needs_second_person(run, current_user),
        max_schedule_hours=int(upgrades.MAX_SCHEDULE_AHEAD.total_seconds() // 3600),
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
    if not confirmed(f'approving {phase}'):
        return redirect(url_for('upgrades.show_run', run_id=run_id))
    try:
        job = upgrades.approve(
            run=run, phase=phase, user=current_user, password=password,
            public_key=current_app.extensions['credential_public_key'],
            concurrency=request.form.get('concurrency'),
            cap=current_app.config['PHASE_CONCURRENCY'],
            start_at=request.form.get('start_at'),
        )
        flash(f'Approved {phase}; queued as job #{job.id}{_when(job)}.', 'success')
    except upgrades.RequestError as exc:
        flash(str(exc))
    return redirect(url_for('upgrades.show_run', run_id=run_id))


@upgrade_bp.route('/upgrades/<int:run_id>/retry', methods=['POST'])
@login_required
def retry(run_id):
    """Run a phase again on the hosts that failed it. An
    approval like any other: it collects the retrier's device credential."""
    run = db.session.get(UpgradeRun, run_id)
    phase = request.form.get('phase', '')
    password = request.form.get('device_password', '')
    if run is None:
        flash('No such run.')
        return redirect(url_for('upgrades.list_runs'))
    if not confirmed(f'retrying {phase}'):
        return redirect(url_for('upgrades.show_run', run_id=run_id))
    try:
        job = upgrades.retry(
            run=run, phase=phase, user=current_user, password=password,
            public_key=current_app.extensions['credential_public_key'],
            concurrency=request.form.get('concurrency'),
            cap=current_app.config['PHASE_CONCURRENCY'],
            start_at=request.form.get('start_at'),
        )
        flash(f'Retrying {phase} on the hosts that failed it; '
              f'queued as job #{job.id}{_when(job)}.', 'success')
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
    if run is not None and confirmed('the cancel'):
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
    # Never `request.referrer`: it is attacker-influenced, and an unvalidated
    # redirect.
    return redirect(url_for('upgrades.profile'))
