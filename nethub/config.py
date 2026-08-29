import os

# Project root (one level up from this package), not this file's own
# directory -- database.db and instance/ live beside the package, matching
# Flask's own instance-folder convention and the existing .gitignore entries.
basedir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Off by default now that a login form exists (credential path) -- a
# debugger that renders frame locals would render a submitted password
# right along with them. Set DEBUG=1 for local dev only.
DEBUG = os.getenv('DEBUG', '') == '1'

# Secret key for session management
SECRET_KEY = os.getenv('SECRET_KEY')
if SECRET_KEY is None:
    raise ValueError("SECRET_KEY cannot be empty, please generate a random string and supply it through an env variable")

# Connect to the database
SQLALCHEMY_DATABASE_URI = 'sqlite:///' + os.path.join(basedir, 'database.db')
SQLALCHEMY_TRACK_MODIFICATIONS = False

# Cap request size so an upload can't exhaust disk/memory (1.5 GB, comfortably
# above the largest IOS-XE images in ansible/inventory/rendered's example registry).
MAX_CONTENT_LENGTH = 1_500 * 1024 * 1024

# Where alpha's registry YAML and uploaded images live. Deliberately outside
# ansible/inventory/rendered, which is a design sketch, not a real output path.
REGISTRY_ROOT = os.getenv('REGISTRY_ROOT', os.path.join(basedir, 'instance', 'registry'))
IMAGES_DIR = os.path.join(REGISTRY_ROOT, 'images')
