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
from werkzeug.security import check_password_hash, generate_password_hash

from .extensions import db
from .models import User

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
        if correct and not locked:
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
        else:
            current_app.logger.warning(
                'failed login for unknown username %r from %s',
                username, request.remote_addr,
            )

        # One message for every failure, including a locked account: saying
        # "locked" would confirm the username exists and hand back the oracle
        # the constant-time path above just closed.
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
    users = User.query.all()
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
        db.session.commit()
        flash('User created.', 'success')
        return redirect(url_for('auth.list_users'))

    return render_template('pages/users_new.html')


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
            db.session.commit()
            click.echo(f'Created user "{username}".')
