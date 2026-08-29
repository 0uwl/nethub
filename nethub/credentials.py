import os
from pathlib import Path


def read_credential(name):
    """Read a systemd credential by name from $CREDENTIALS_DIRECTORY (set by
    systemd/Podman when a unit uses LoadCredential=/SetCredential=). Returns
    the stripped file content, or None if no credentials directory is set,
    the named file doesn't exist, or it's empty.
    """
    creds_dir = os.getenv('CREDENTIALS_DIRECTORY')
    if not creds_dir:
        return None
    path = Path(creds_dir) / name
    if not path.is_file():
        return None
    return path.read_text().strip() or None
