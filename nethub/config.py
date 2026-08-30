import os

from .credentials import read_credential

# Project root (one level up from this package), not this file's own
# directory -- database.db and instance/ live beside the package, matching
# Flask's own instance-folder convention and the existing .gitignore entries.
basedir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Off by default now that a login form exists (credential path) -- a
# debugger that renders frame locals would render a submitted password
# right along with them. Set DEBUG=1 for local dev only.
DEBUG = os.getenv('DEBUG', '') == '1'

# Secret key for session management. A systemd credential named 'secret_key'
# (LoadCredential=/SetCredential=) takes priority over the plaintext
# SECRET_KEY env var, same idea as the ADMIN_PASSWORD/admin_password
# credential in nethub/bootstrap.py -- see quadlet/nethub.container.
SECRET_KEY = read_credential('secret_key') or os.getenv('SECRET_KEY')
if SECRET_KEY is None:
    raise ValueError("SECRET_KEY cannot be empty, please generate a random string and supply it through an env variable")

# Bare SQLite file path -- not a full SQLAlchemy URL. Overridable so a
# container can point it at a mounted volume (e.g. /app/data/database.db).
DATABASE_PATH = os.getenv('DATABASE_PATH', os.path.join(basedir, 'database.db'))
SQLALCHEMY_DATABASE_URI = 'sqlite:///' + DATABASE_PATH
SQLALCHEMY_TRACK_MODIFICATIONS = False

# Cap request size so an upload can't exhaust disk/memory (1.5 GB, comfortably
# above the largest IOS-XE images in ansible/inventory/rendered's example registry).
MAX_CONTENT_LENGTH = 1_500 * 1024 * 1024

# Root directory an admin bind-mounts registry files into (e.g. a real
# Ansible group_vars/os_iosxe.yml) so NetHub can list and adopt them --
# every Registry.file_path is resolved relative to this, and validated to
# stay inside it (see nethub/registry.py). Each registry's own search_dir
# (where its images live) is a separate, admin-supplied absolute path --
# there's no shared "images root", since that's typically a much larger,
# independently-mounted volume.
REGISTRIES_ROOT = os.getenv('REGISTRIES_ROOT', os.path.join(basedir, 'instance', 'registries'))
