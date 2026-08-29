import os
import secrets
import string

from .credentials import read_credential
from .extensions import db
from .models import User

ADMIN_CREDENTIAL_NAME = 'admin_password'
ADMIN_PASSWORD_LENGTH = 12


def _initial_admin_password():
    """Resolve the bootstrap admin's password, in priority order:

    1. A systemd credential named 'admin_password' (LoadCredential=, read via
       $CREDENTIALS_DIRECTORY) -- never logged, never sits in plaintext
       anywhere durable.
    2. ADMIN_PASSWORD, a plaintext env var -- has to persist at rest
       somewhere (a Quadlet unit, a .env file) to survive restarts.
    3. A freshly generated random password, printed once. There's no
       forced-password-reset mechanism in this alpha (see alpha.md), unlike
       Drawbridge's equivalent -- whatever password is set here is the one
       that stays live until someone changes it.
    """
    credential = read_credential(ADMIN_CREDENTIAL_NAME)
    if credential:
        return credential, 'credential'

    env_password = os.getenv('ADMIN_PASSWORD')
    if env_password:
        return env_password, 'env'

    alphabet = string.ascii_letters + string.digits
    password = ''.join(secrets.choice(alphabet) for _ in range(ADMIN_PASSWORD_LENGTH))
    return password, 'generated'


def bootstrap_admin():
    """Create the first login user if the database has none yet. Everyone
    who can log in is an admin in this alpha -- there are no roles to pick
    between (see alpha.md).
    """
    if User.query.count() > 0:
        return

    username = os.getenv('ADMIN_USERNAME', 'admin')
    password, source = _initial_admin_password()

    user = User(username=username)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()

    if source == 'credential':
        print(
            f"NetHub: created initial admin user '{username}' from the "
            f"'{ADMIN_CREDENTIAL_NAME}' systemd credential."
        )
    elif source == 'env':
        print(f"NetHub: created initial admin user '{username}' from ADMIN_PASSWORD.")
    else:
        print(
            f"NetHub: created initial admin user '{username}', password: {password} "
            "-- record this now, it will not be shown again."
        )
