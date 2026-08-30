from datetime import datetime, timezone

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from .extensions import db, login_manager


class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class Registry(db.Model):
    """A tracked `software_registry` block inside some file under
    REGISTRIES_ROOT. NetHub owns this row, not the file -- deleting a
    Registry only forgets the pointer (see nethub/registry.py).
    """
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), unique=True, nullable=False)
    # Relative to REGISTRIES_ROOT -- never store an absolute path here, it's
    # re-joined against the (possibly redeployed) root at every use.
    file_path = db.Column(db.String(255), unique=True, nullable=False)
    # Absolute path where this registry's images live -- may be outside
    # REGISTRIES_ROOT entirely (see nethub/registry.py's path-safety note).
    search_dir = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))
