from datetime import timedelta

import click
from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_login import current_user, login_required, login_user, logout_user
from sqlalchemy.orm import aliased
from werkzeug.security import check_password_hash, generate_password_hash

from .extensions import db
from .models import User, UserAdminAudit, record_user_action

auth_bp = Blueprint('auth', __name__)

#: Shortest password `new_user` and `create-admin` will accept. There was no
#: policy at all before this -- a one-character password was fine.
MIN_PASSWORD_LENGTH = 12

#: How many consecutive failures lock an account, and for how long. See the
#: comment on `User.failed_logins` for why the budget is per-row.
MAX_FAILED_LOGINS = 10
LOCKOUT_DURATION = timedelta(minutes=15)

#: Verified against when the submitted username does not exist, so that both
#: branches of the login path pay one scrypt verification.
#:
#: Without this the control flow was the oracle: a missing user returned before
#: `check_password` ran, so /login answered in ~1.4 ms for an unknown username
#: against ~104 ms for a known one -- a 74x gap that survives any amount of
#: network jitter, and `GET /login` hands out the CSRF token unauthenticated.
#: Every login user is an admin in this alpha, so enumerating usernames is the
#: whole first half of an attack.
#:
#: Computed once at import (one scrypt pass at startup) and never compared for
#: its result, only for its cost. It must keep the same KDF parameters as real
#: hashes, which it does by going through the same `generate_password_hash`.
_ABSENT_USER_HASH = generate_password_hash('nethub-absent-user-timing-equaliser')


def _password_too_short(password):
    return len(password) < MIN_PASSWORD_LENGTH


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('artifacts.list_artifacts'))

    if request.method == 'POST':
        username = request.form.get('username', '')
        password = request.form.get('password', '')
        user = User.query.filter_by(username=username).first()

        # Always pay for exactly one password verification, whatever happens
        # next -- see `_ABSENT_USER_HASH`. A locked account is verified too, so
        # "locked" and "wrong password" cost the same.
        if user is None:
            check_password_hash(_ABSENT_USER_HASH, password)
            correct = False
        else:
            correct = user.check_password(password)

        locked = user is not None and user.is_locked()
        inactive = user is not None and not user.is_active
        if correct and not locked and not inactive:
            user.clear_failed_logins()
            db.session.commit()
            # Marks the session for PERMANENT_SESSION_LIFETIME. Flask applies
            # that setting only to a permanent session; without this line it is
            # configured and inert, and the cookie carries no expiry of its own.
            session.permanent = True
            login_user(user)
            return redirect(url_for('artifacts.list_artifacts'))

        # There was no record at all that an attempt happened before this.
        # Username is logged because the account is the thing being attacked
        # and an operator needs to know which one; the password never is.
        if user is not None and not correct:
            now_locked = user.register_failed_login(
                limit=MAX_FAILED_LOGINS, lockout=LOCKOUT_DURATION
            )
            db.session.commit()
            current_app.logger.warning(
                'failed login for %r from %s (%s)',
                username, request.remote_addr,
                'now locked out' if now_locked else f'{user.failed_logins} consecutive',
            )
        elif locked:
            current_app.logger.warning(
                'login attempt for locked account %r from %s',
                username, request.remote_addr,
            )
        elif inactive:
            current_app.logger.warning(
                'login attempt for disabled account %r from %s',
                username, request.remote_addr,
            )
        else:
            current_app.logger.warning(
                'failed login for unknown username %r from %s',
                username, request.remote_addr,
            )

        # One message for every failure, including a locked or disabled
        # account: saying either would confirm the username exists and hand
        # back the oracle the constant-time path above just closed.
        flash('Invalid username or password.')

    return render_template('pages/login.html')


@auth_bp.route('/logout', methods=['POST'])
@login_required
def logout():
    logout_user()
    return redirect(url_for('auth.login'))


@auth_bp.route('/users')
@login_required
def list_users():
    users = User.query.order_by(User.username).all()
    return render_template('pages/users_list.html', users=users)


@auth_bp.route('/users/new', methods=['GET', 'POST'])
@login_required
def new_user():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        if not username or not password:
            flash('Username and password are both required.')
            return render_template('pages/users_new.html')
        if User.query.filter_by(username=username).first():
            flash('That username is already taken.')
            return render_template('pages/users_new.html')
        if _password_too_short(password):
            flash(f'Password must be at least {MIN_PASSWORD_LENGTH} characters.')
            return render_template('pages/users_new.html')

        user = User(username=username)
        user.set_password(password)
        db.session.add(user)
        db.session.flush()
        record_user_action('created', user, actor=current_user)
        db.session.commit()
        flash('User created.', 'success')
        return redirect(url_for('auth.list_users'))

    return render_template('pages/users_new.html')


def _target(user_id):
    user = db.session.get(User, user_id)
    if user is None:
        flash('No such user.')
    return user


@auth_bp.route('/users/<int:user_id>/reset-password', methods=['GET', 'POST'])
@login_required
def reset_password(user_id):
    """Set another user's password. It ends every session they hold, since a
    reset is what an admin does when an account may be in the wrong hands.
    Your own password is changed on your profile, which asks for the current
    one; this page does not."""
    user = _target(user_id)
    if user is None:
        return redirect(url_for('auth.list_users'))
    if user.id == current_user.id:
        flash('Change your own password on your profile page.')
        return redirect(url_for('upgrades.profile'))
    if request.method == 'POST':
        password = request.form.get('password', '')
        if _password_too_short(password):
            flash(f'Password must be at least {MIN_PASSWORD_LENGTH} characters.')
            return render_template('pages/users_reset.html', user=user)
        user.set_password(password)
        user.clear_failed_logins()
        user.revoke_sessions()
        record_user_action('password_reset', user, actor=current_user)
        db.session.commit()
        flash(f'Password for {user.username} reset; their sessions are ended.', 'success')
        return redirect(url_for('auth.list_users'))
    return render_template('pages/users_reset.html', user=user)


@auth_bp.route('/users/<int:user_id>/disable', methods=['POST'])
@login_required
def disable_user(user_id):
    """Disable an account and end its sessions.

    Never yourself, and never the last active user, or nobody could log in
    to undo it. The last-user check is part of the UPDATE itself (the claim
    pattern again): two people disabling each other at the same moment both
    pass a check made beforehand, and the database serialises the two
    statements, so the second one finds nobody else active and changes
    nothing.
    """
    user = _target(user_id)
    if user is None:
        return redirect(url_for('auth.list_users'))
    if user.id == current_user.id:
        flash('You cannot disable your own account.')
        return redirect(url_for('auth.list_users'))
    other = aliased(User)
    others = (db.session.query(db.func.count(other.id))
              .filter(other.is_active.is_(True), other.id != user_id)
              .correlate(None).scalar_subquery())
    changed = (
        db.session.query(User)
        .filter(User.id == user_id, User.is_active.is_(True), others > 0)
        .update({'is_active': False, 'session_epoch': User.session_epoch + 1},
                synchronize_session='fetch')
    )
    if changed != 1:
        db.session.rollback()
        flash(f'{user.username} is already disabled, or is the last active user.')
        return redirect(url_for('auth.list_users'))
    record_user_action('disabled', user, actor=current_user)
    db.session.commit()
    flash(f'Disabled {user.username}; their sessions are ended.', 'success')
    return redirect(url_for('auth.list_users'))


@auth_bp.route('/users/<int:user_id>/enable', methods=['POST'])
@login_required
def enable_user(user_id):
    user = _target(user_id)
    if user is None:
        return redirect(url_for('auth.list_users'))
    if user.is_active:
        flash(f'{user.username} is already active.')
        return redirect(url_for('auth.list_users'))
    user.is_active = True
    record_user_action('enabled', user, actor=current_user)
    db.session.commit()
    flash(f'Enabled {user.username}.', 'success')
    return redirect(url_for('auth.list_users'))


@auth_bp.route('/users/<int:user_id>/unlock', methods=['POST'])
@login_required
def unlock_user(user_id):
    """Clear a lockout before it expires. The lockout is 15 minutes, but with
    a well-known username anyone on the network can keep re-arming it, and
    this is how an admin gets the account back in the meantime."""
    user = _target(user_id)
    if user is None:
        return redirect(url_for('auth.list_users'))
    user.clear_failed_logins()
    record_user_action('unlocked', user, actor=current_user)
    db.session.commit()
    flash(f'Unlocked {user.username}.', 'success')
    return redirect(url_for('auth.list_users'))


@auth_bp.route('/users/<int:user_id>/history')
@login_required
def user_history(user_id):
    user = _target(user_id)
    if user is None:
        return redirect(url_for('auth.list_users'))
    entries = (UserAdminAudit.query.filter_by(target_user_id=user.id)
               .order_by(UserAdminAudit.occurred_at.desc(), UserAdminAudit.id.desc()).all())
    return render_template('pages/user_history.html', user=user, entries=entries,
                           users={u.id: u.username for u in User.query.all()})


@auth_bp.route('/profile/password', methods=['POST'])
@login_required
def change_password():
    """Change your own password, which needs the current one.

    A wrong current password counts toward the lockout, as a failed login
    would: a session left open on someone else's screen must not become an
    unlimited guessing oracle for the password behind it. Every other session
    you hold ends; this one carries on under the new epoch.
    """
    user = current_user._get_current_object()
    current = request.form.get('current_password', '')
    new = request.form.get('new_password', '')
    confirm = request.form.get('confirm_password', '')
    if user.is_locked() or not user.check_password(current):
        if not user.is_locked():
            user.register_failed_login(limit=MAX_FAILED_LOGINS, lockout=LOCKOUT_DURATION)
            db.session.commit()
        flash('Your current password was not accepted.')
        return redirect(url_for('upgrades.profile'))
    if new != confirm:
        flash('The new passwords do not match.')
        return redirect(url_for('upgrades.profile'))
    if _password_too_short(new):
        flash(f'Password must be at least {MIN_PASSWORD_LENGTH} characters.')
        return redirect(url_for('upgrades.profile'))
    user.set_password(new)
    user.clear_failed_logins()
    user.revoke_sessions()
    record_user_action('password_changed', user, actor=user)
    db.session.commit()
    # The epoch moved, so this session's cookie names the old one; log in
    # again under the new id so only the *other* sessions end.
    session.permanent = True
    login_user(user)
    flash('Password changed. Your other sessions are ended.', 'success')
    return redirect(url_for('upgrades.profile'))


def register_cli(app):
    @app.cli.command('create-admin')
    @click.argument('username')
    def create_admin(username):
        """Create the first (or another) login user. Everyone who can log in is an admin."""
        password = click.prompt(
            'Password', hide_input=True, confirmation_prompt=True
        )
        with app.app_context():
            # Existence first: "that user already exists" is the more useful
            # answer, and it does not depend on the password being acceptable.
            if User.query.filter_by(username=username).first():
                click.echo(f'User "{username}" already exists.')
                return
            if _password_too_short(password):
                click.echo(
                    f'Password must be at least {MIN_PASSWORD_LENGTH} characters.'
                )
                return
            user = User(username=username)
            user.set_password(password)
            db.session.add(user)
            db.session.flush()
            record_user_action('created', user, detail='create-admin command')
            db.session.commit()
            click.echo(f'Created user "{username}".')
