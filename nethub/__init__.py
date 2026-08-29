#----------------------------------------------------------------------------#
# Imports
#----------------------------------------------------------------------------#

import logging
from logging import Formatter, FileHandler

from flask import Flask, render_template

from . import config as config_module
from .extensions import csrf, db, login_manager

#----------------------------------------------------------------------------#
# App Config.
#----------------------------------------------------------------------------#

app = Flask(__name__)
app.config.from_object(config_module)

db.init_app(app)
login_manager.init_app(app)
csrf.init_app(app)

from . import models  # noqa: E402  (registers the user_loader; needed before first request)
from .auth import auth_bp, register_cli  # noqa: E402
from .registry_routes import registry_bp  # noqa: E402

app.register_blueprint(auth_bp)
app.register_blueprint(registry_bp)
register_cli(app)

with app.app_context():
    db.create_all()

#----------------------------------------------------------------------------#
# Controllers.
#----------------------------------------------------------------------------#


@app.route('/')
def home():
    return render_template('pages/home.html')


@app.errorhandler(500)
def internal_error(error):
    return render_template('errors/500.html'), 500


@app.errorhandler(404)
def not_found_error(error):
    return render_template('errors/404.html'), 404


if not app.debug:
    file_handler = FileHandler('error.log')
    file_handler.setFormatter(
        Formatter('%(asctime)s %(levelname)s: %(message)s [in %(pathname)s:%(lineno)d]')
    )
    app.logger.setLevel(logging.INFO)
    file_handler.setLevel(logging.INFO)
    app.logger.addHandler(file_handler)

#----------------------------------------------------------------------------#
# Launch.
#----------------------------------------------------------------------------#

if __name__ == '__main__':
    app.run()
