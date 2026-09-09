#----------------------------------------------------------------------------#
# Imports
#----------------------------------------------------------------------------#

import logging

from flask import Flask, render_template

from .extensions import csrf, db, login_manager


def create_app():
    # Imported here, not at module scope: config.py raises without SECRET_KEY,
    # and importing nethub.devices shouldn't require Flask's settings.
    from . import config as config_module

    app = Flask(__name__)
    app.config.from_object(config_module)

    db.init_app(app)
    login_manager.init_app(app)
    csrf.init_app(app)

    from . import models  # noqa: F401 -- registers the user_loader; needed before first request
    from .artifact_routes import artifacts_bp
    from .auth import auth_bp, register_cli
    from .bootstrap import bootstrap_admin
    from .upgrade_routes import hostkeys_bp, upgrade_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(artifacts_bp)
    app.register_blueprint(upgrade_bp)
    app.register_blueprint(hostkeys_bp)
    register_cli(app)

    with app.app_context():
        db.create_all()
        bootstrap_admin()

    _serve_credential_socket(app)

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


def _serve_credential_socket(app):
    """Serve §9.1's secret channel, if a systemd `.socket` unit handed one over.

    Flask serves and the sibling connects -- never the reverse. A deposit
    endpoint would leave the sibling holding secrets for executions it has not
    started, and would let anything that can reach it flood the holder.

    Served on its own thread with deadlines because Flask must stay
    multi-threaded (§3.2): a blocking accept on the request path would hold the
    phone-home route, which is the exact failure §3.2 legislates against.

    Returns None when the process was not socket-activated -- which is every
    dev run and every test. Nothing here creates a socket.
    """
    import threading

    from .credential_socket import CredentialError, CredentialStore, serve, systemd_socket
    from .models import UpgradePhaseJob

    listening = systemd_socket()
    store = CredentialStore()
    app.extensions['credential_store'] = store
    if listening is None:
        return None

    def verify_running(job_id):
        """The interlock, not an authorization check (§9.1).

        The sibling sets `running` and then asks us to confirm it, so the
        precondition is controlled by the requester and constrains a
        compromised sibling not at all. What it buys is that a stray or
        duplicated request cannot drain credentials for jobs nobody started.
        """
        with app.app_context():
            job = db.session.get(UpgradePhaseJob, job_id)
            if job is None or job.status != 'running':
                raise CredentialError("no running execution with that id")
            # §9.1 cross-checks the identity that *supplied* the credential,
            # which is the approver for every gated phase -- but pre-check has
            # no gate by design (§8.1), so its `approved_by` is null and the
            # supplying identity is the submitter. Refusing a null here would
            # fail every pre-check ever dispatched, which is how this was
            # found: the two halves each looked right alone.
            return job.approved_by if job.approved_by is not None else job.run.submitted_by

    thread = threading.Thread(
        target=serve, args=(listening, store, verify_running),
        name='credential-socket', daemon=True,
    )
    thread.start()
    return thread
