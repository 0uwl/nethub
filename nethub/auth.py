import click
from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user

from .extensions import db
from .models import User

auth_bp = Blueprint('auth', __name__)


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('registries.list_registries'))

    if request.method == 'POST':
        username = request.form.get('username', '')
        password = request.form.get('password', '')
        user = User.query.filter_by(username=username).first()
        if user and user.check_password(password):
            login_user(user)
            return redirect(url_for('registries.list_registries'))
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

        user = User(username=username)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        flash('User created.')
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
            if User.query.filter_by(username=username).first():
                click.echo(f'User "{username}" already exists.')
                return
            user = User(username=username)
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            click.echo(f'Created user "{username}".')
