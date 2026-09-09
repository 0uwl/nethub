"""Checks for nethub.devices.connection -- the fail-closed host-key path.

No device is needed. The pinned key fixture is the real switch's public host
key (tests/captures/), and its expected fingerprint is what `ssh-keygen -lf`
printed for the same file -- so a change to how we compute a fingerprint fails
against OpenSSH rather than against our own arithmetic.
"""

import base64
from pathlib import Path

import paramiko
import pytest

from nethub.devices import connection as C

CAPTURE = Path(__file__).parent / "captures" / "c9200cx-12p-2x2g-17.12.06"

#: From `ssh-keygen -lf ssh_host_key.pub`, not from this codebase.
DEVICE_FINGERPRINT = "SHA256:C8F5CyUEoG/8/+TwtcUTvXpMpq/7+0sBXfa2VoG+/xg"


def device_key() -> paramiko.PKey:
    key_type, blob = CAPTURE.joinpath("ssh_host_key.pub").read_text().split()
    assert key_type == "ssh-rsa"
    return paramiko.RSAKey(data=base64.b64decode(blob))


@pytest.fixture
def pinned() -> C.HostKey:
    return C.HostKey.of(device_key())


class FakeClient:
    """Enough paramiko.SSHClient for the policy: it only ever gets closed."""

    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def test_fingerprint_matches_what_ssh_keygen_prints(pinned):
    assert pinned.fingerprint_sha256 == DEVICE_FINGERPRINT
    assert pinned.key_type == "ssh-rsa"


def test_fingerprint_has_no_base64_padding():
    """OpenSSH strips it; a trailing '=' would fail every comparison."""
    assert not device_key().get_name().endswith("=")
    assert not C.HostKey.of(device_key()).fingerprint_sha256.endswith("=")


def test_policy_accepts_the_pinned_key(pinned):
    client = FakeClient()
    policy = C._PinnedHostKeyPolicy(pinned)
    assert policy.missing_host_key(client, "192.0.2.10", device_key()) is None
    assert not client.closed


def test_policy_refuses_a_changed_fingerprint(pinned):
    client = FakeClient()
    policy = C._PinnedHostKeyPolicy(C.HostKey(pinned.key_type, "SHA256:" + "A" * 43))
    with pytest.raises(C.HostKeyError, match="host key changed"):
        policy.missing_host_key(client, "192.0.2.10", device_key())
    assert client.closed, "the socket must not be left open on refusal"


def test_policy_refuses_a_different_key_type(pinned):
    """A device offering an unpinned algorithm must not earn a second pin."""
    policy = C._PinnedHostKeyPolicy(C.HostKey("ssh-ed25519", pinned.fingerprint_sha256))
    with pytest.raises(C.HostKeyError, match="explicit admin re-accept"):
        policy.missing_host_key(FakeClient(), "192.0.2.10", device_key())


def test_host_key_errors_are_not_paramiko_exceptions():
    """Netmiko turns paramiko.SSHException into a *timeout*; a mismatch must
    not arrive at an operator disguised as one."""
    assert not issubclass(C.HostKeyError, paramiko.SSHException)
    assert issubclass(C.HostKeyError, C.DeviceConnectionError)
    assert issubclass(C.AuthenticationError, C.DeviceConnectionError)


def test_client_is_built_with_our_policy_and_no_loaded_keys(pinned):
    driver = C.IosXeSSH.__new__(C.IosXeSSH)  # no connection
    driver._host_key = pinned
    driver._auth_method = "keyboard-interactive"

    client = driver._build_ssh_client()

    assert isinstance(client._policy, C._PinnedHostKeyPolicy)
    assert client._policy.pinned == pinned
    # A key from anywhere else would make paramiko skip the policy entirely.
    assert len(client.get_host_keys()) == 0
    assert len(client._system_host_keys) == 0


def test_keyboard_interactive_is_the_default_client(pinned):
    driver = C.IosXeSSH.__new__(C.IosXeSSH)
    driver._auth_method = C.DEFAULT_AUTH
    assert C.DEFAULT_AUTH == "keyboard-interactive"
    assert isinstance(driver._get_ssh_client_instance(), C._SSHClientKeyboardInteractive)

    driver._auth_method = "password"
    assert type(driver._get_ssh_client_instance()) is paramiko.SSHClient


def test_keyboard_interactive_answers_every_prompt_with_the_password():
    client = C._SSHClientKeyboardInteractive()
    captured = {}

    class FakeTransport:
        def auth_interactive(self, username, handler):
            captured["username"] = username
            captured["answers"] = handler("title", "instructions", [("Password: ", False)])

    client.get_transport = lambda: FakeTransport()
    client._auth("admin", "s3cret")

    assert captured == {"username": "admin", "answers": ["s3cret"]}


def test_connect_cannot_be_called_without_a_pinned_key():
    """The signature is the guarantee: no reachable device without a pin."""
    with pytest.raises(TypeError):
        C.connect("192.0.2.10", "admin", "s3cret")


def test_scan_host_key_reports_an_unreachable_address():
    with pytest.raises(C.DeviceConnectionError):
        C.scan_host_key("127.0.0.1", port=1, timeout=2)


def test_no_ssh_key_material_is_ever_offered(monkeypatch, pinned):
    """NetHub authenticates with a credential, never an SSH key.

    The pinned host key is the *device's* identity, not a client key. Netmiko
    defaults use_keys/allow_agent off; assert that here so a library upgrade
    flipping a default, or a stray kwarg, surfaces as a failing test rather
    than as this host's ~/.ssh being silently in the auth path.
    """
    import inspect

    from netmiko.base_connection import BaseConnection

    sent = {}
    monkeypatch.setattr(C, "IosXeSSH", lambda **kwargs: sent.update(kwargs))
    C.connect("192.0.2.10", "admin", "s3cret", pinned)

    assert sent["password"] == "s3cret"
    for key_param in ("use_keys", "allow_agent", "key_file", "pkey", "passphrase"):
        assert key_param not in sent

    defaults = inspect.signature(BaseConnection.__init__).parameters
    assert defaults["use_keys"].default is False
    assert defaults["allow_agent"].default is False
    assert defaults["key_file"].default is None


def test_connect_exposes_no_way_to_turn_key_auth_on():
    """No **kwargs passthrough: a caller cannot reach netmiko's key options."""
    import inspect

    params = inspect.signature(C.connect).parameters
    assert not any(p.kind is p.VAR_KEYWORD for p in params.values())
    assert "use_keys" not in params
