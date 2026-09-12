"""`config.py` validates SECRET_KEY at import time, so these reload the module.

The checks live at module scope rather than in a function, which is why there
is nothing to call directly -- `importlib.reload` under a patched environment is
the only way to exercise them. `read_credential` is neutralised in each case so
a real systemd credential on the host running the tests cannot mask the env var.
"""
import importlib

import pytest

import nethub.config


def _reload(monkeypatch, value):
    """Re-import config.py with SECRET_KEY set to `value` (None = unset)."""
    monkeypatch.setattr('nethub.credentials.read_credential', lambda name: None)
    if value is None:
        monkeypatch.delenv('SECRET_KEY', raising=False)
    else:
        monkeypatch.setenv('SECRET_KEY', value)
    return importlib.reload(nethub.config)


@pytest.fixture(autouse=True)
def _restore_config():
    """Leave the module as the rest of the suite expects to find it."""
    yield
    importlib.reload(nethub.config)


def test_absent_key_is_refused(monkeypatch):
    with pytest.raises(ValueError, match='cannot be empty'):
        _reload(monkeypatch, None)


def test_the_quadlet_placeholder_is_refused(monkeypatch):
    # The exact string the reference unit used to ship. Refusing an absent key
    # was never enough: a published placeholder is a forgeable admin session.
    with pytest.raises(ValueError, match='known placeholder'):
        _reload(monkeypatch, 'CHANGE_ME_use_openssl_rand_hex_32')


@pytest.mark.parametrize('value', ['CHANGE_ME', 'changeme', 'secret', 'dev', 'development'])
def test_other_placeholders_are_refused(monkeypatch, value):
    with pytest.raises(ValueError, match='known placeholder'):
        _reload(monkeypatch, value)


def test_a_short_key_is_refused(monkeypatch):
    with pytest.raises(ValueError, match='at least 32 characters'):
        _reload(monkeypatch, 'a' * 31)


def test_a_real_key_is_accepted(monkeypatch):
    key = 'f' * 64  # what `openssl rand -hex 32` produces
    assert _reload(monkeypatch, key).SECRET_KEY == key


def test_the_boundary_length_is_accepted(monkeypatch):
    key = 'a' * 32
    assert _reload(monkeypatch, key).SECRET_KEY == key
