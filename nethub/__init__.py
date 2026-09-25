#----------------------------------------------------------------------------#
# Imports
#----------------------------------------------------------------------------#

import logging

from flask import Flask, render_template

from .extensions import csrf, db, login_manager, migrate


def create_app():
    # Imported here, not at module scope: config.py raises without SECRET_KEY,
    # and importing nethub.devices shouldn't require Flask's settings.
    from . import config as web_config
    from . import shared_config

    app = Flask(__name__)
    app.config.from_object(shared_config)
    app.config.from_object(web_config)

    db.init_app(app)
    login_manager.init_app(app)
    csrf.init_app(app)

    from . import schema
    migrate.init_app(app, db, directory=str(schema.MIGRATIONS_DIR))

    from . import models  # noqa: F401 -- registers the user_loader; needed before first request
    from .artifact_routes import artifacts_bp
    from .artifact_routes import register_cli as register_artifact_cli
    from .auth import auth_bp, register_cli
    from .bootstrap import bootstrap_admin
    from .upgrade_routes import hostkeys_bp, upgrade_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(artifacts_bp)
    app.register_blueprint(upgrade_bp)
    app.register_blueprint(hostkeys_bp)
    register_cli(app)
    register_artifact_cli(app)

    # Device credentials are sealed to the sibling's public key and stored in
    # the job row (nethub/sealed_credentials.py). Refuse to start without a
    # valid key rather than accept approvals that could never run, and do it
    # before migrating, so a misconfigured deployment changes nothing.
    from .sealed_credentials import load_public_key
    app.extensions['credential_public_key'] = load_public_key(
        app.config['NETHUB_CREDENTIAL_PUBLIC_KEY']
    )

    # The web process is the only one that migrates (nethub/schema.py): an
    # upgrade is a new image and a restart, and the sibling waits for this.
    schema.upgrade_database(app.config['SQLALCHEMY_DATABASE_URI'])
    with app.app_context():
        bootstrap_admin()

    #------------------------------------------------------------------------#
    # Controllers.
    #------------------------------------------------------------------------#

    @app.route('/')
    def home():
        return render_template('pages/home.html')

    @app.errorhandler(500)
    def internal_error(error):
        return render_template('errors/500.html'), 500

    @app.errorhandler(404)
    def not_found_error(error):
        return render_template('errors/404.html'), 404

    @app.errorhandler(413)
    def too_large_error(error):
        # MAX_CONTENT_LENGTH's default (~1.5 GB) is comfortably above a real
        # IOS-XE image, so a real 413 is almost always a mistaken upload
        # rather than a legitimately oversized one -- but Werkzeug's bare
        # default error page doesn't say why the request failed or what the
        # limit is, which reads as a broken upload rather than an explained
        # refusal (WS-5.6).
        return render_template(
            'errors/413.html', max_bytes=app.config['MAX_CONTENT_LENGTH']
        ), 413

    # Under gunicorn, hand Flask's logger gunicorn's own handlers so errors
    # go to stdout (captured by `podman logs`/journald) instead of a
    # relative-path error.log file -- that path doesn't exist, and wouldn't
    # be writable, under the container's read-only root filesystem (see
    # Containerfile / quadlet/nethub.container). Not running under gunicorn
    # (e.g. `flask run` in dev) -- gunicorn.error has no handlers yet, so
    # this is a no-op and Flask's default logging stands.
    if not app.debug:
        gunicorn_logger = logging.getLogger('gunicorn.error')
        if gunicorn_logger.handlers:
            app.logger.handlers = gunicorn_logger.handlers
            app.logger.setLevel(gunicorn_logger.level)

    return app
