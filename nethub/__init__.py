#----------------------------------------------------------------------------#
# Imports
#----------------------------------------------------------------------------#

import logging

from flask import Flask, render_template

from . import config as config_module
from .extensions import csrf, db, login_manager


def create_app():
    app = Flask(__name__)
    app.config.from_object(config_module)

    db.init_app(app)
    login_manager.init_app(app)
    csrf.init_app(app)

    from . import models  # noqa: F401 -- registers the user_loader; needed before first request
    from .auth import auth_bp, register_cli
    from .bootstrap import bootstrap_admin
    from .registry_routes import registries_bp, registry_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(registries_bp)
    app.register_blueprint(registry_bp)
    register_cli(app)

    with app.app_context():
        db.create_all()
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
