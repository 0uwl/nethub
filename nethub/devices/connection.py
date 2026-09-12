"""Netmiko sessions to IOS-XE devices: host-key pinned, fail-closed.

Every device connection in NetHub goes through `connect()`, and `connect()`
cannot be called without a pinned host key. That is the point: `network_cli`
sends the password after key exchange, so an unverified address is one where
a submitter who names a machine they control is handed someone else's AAA
credential (design doc §4.3).

Pinning alone does not close that -- it fails closed only on a *changed* key,
and naming your own machine is always a *first* contact. So there is no TOFU
path here at all: `scan_host_key()` is the separate, deliberate admin action
that fetches a fingerprint for a human to confirm out-of-band, and until a
confirmed `device_host_keys` row exists no run may name the address.

The pinned key is the *device's* host key -- how the device proves its
identity to us. It is not an SSH client key and NetHub does not use one:
authentication is the submitter's own username and password, answered at the
device's keyboard-interactive prompt. `use_keys` and `allow_agent` stay off,
so nothing in this host's `~/.ssh` or in a running agent is ever offered.

This layer holds no database and no policy. It takes an address, a credential
and a pinned key, and either yields a session or raises. Its three exceptions
are the three §7.3 `failure_stage` values a connection can produce --
`hostkey`, `credential`, `connect` -- so mapping them in phases.py is a lookup
rather than a judgement.
"""

from __future__ import annotations

import base64
import hashlib
import socket
from dataclasses import dataclass
from typing import Any, Literal

import paramiko
from netmiko.cisco import CiscoIosSSH
from netmiko.exceptions import NetmikoAuthenticationException

#: Netmiko's own defaults (10s TCP, 15s banner) are tuned for a responsive
#: lab device, not a switch mid-upgrade with a slow banner.
CONNECT_TIMEOUT = 30.0
BANNER_TIMEOUT = 30.0
AUTH_TIMEOUT = 30.0

#: IOS-XE with `aaa new-model` advertises `publickey,keyboard-interactive` and
#: not `password`. See _SSHClientKeyboardInteractive for why this is a choice
#: the caller states rather than something we detect.
AuthMethod = Literal["keyboard-interactive", "password"]
DEFAULT_AUTH: AuthMethod = "keyboard-interactive"


class DeviceConnectionError(Exception):
    """A device session could not be established.

    Deliberately not a subclass of `paramiko.SSHException`: Netmiko catches
    that around its own connect and re-raises it as a *timeout*, which would
    turn a host-key mismatch into a misleading "increase conn_timeout".
    """


class HostKeyError(DeviceConnectionError):
    """The device did not present the key pinned for its address."""


class AuthenticationError(DeviceConnectionError):
    """The device refused the credential."""


@dataclass(frozen=True)
class HostKey:
    """What an address is pinned to -- the two `device_host_keys` columns that
    matter at connect time (design doc §5).

    `fingerprint_sha256` is written the way OpenSSH writes it, so an admin can
    compare it against `ssh-keygen -lf` output on a terminal they trust
    without transcribing between formats. The rest of the row --
    `first_seen_at`, `confirmed_by`, `confirmed_at` -- is the audit trail and
    is not this layer's business.
    """

    key_type: str
    fingerprint_sha256: str

    @classmethod
    def of(cls, key: paramiko.PKey) -> HostKey:
        digest = hashlib.sha256(key.asbytes()).digest()
        return cls(
            # `ssh-rsa`, not the negotiated `rsa-sha2-512`: signature-algorithm
            # negotiation changes between connections and the key does not.
            key_type=key.get_name(),
            fingerprint_sha256="SHA256:" + base64.b64encode(digest).decode().rstrip("="),
        )


def scan_host_key(
    host: str,
    port: int = 22,
    timeout: float = CONNECT_TIMEOUT,
) -> HostKey:
    """Fetch an address's host key without authenticating.

    The key is exchanged before authentication, so this costs no device
    credential -- which is what lets confirming a new address be its own admin
    action, decoupled from any run's submit or approval flow (design doc §4.3).

    It deliberately writes nothing. A human comparing this fingerprint against
    the device out-of-band is the entire value; storing it here would rebuild
    the silent TOFU pin this design exists to refuse.
    """
    # The socket is ours rather than Paramiko's: `Transport((host, port))`
    # connects inside its constructor, with no timeout and raising an
    # SSHException that would escape this function untranslated.
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except OSError as exc:
        raise DeviceConnectionError(f"could not reach {host}:{port}: {exc}") from exc

    transport = paramiko.Transport(sock)
    try:
        transport.start_client(timeout=timeout)
        key = transport.get_remote_server_key()
    except paramiko.SSHException as exc:
        raise DeviceConnectionError(f"no host key from {host}:{port}: {exc}") from exc
    except OSError as exc:
        raise DeviceConnectionError(f"could not reach {host}:{port}: {exc}") from exc
    finally:
        transport.close()
    return HostKey.of(key)


def connect(
    host: str,
    username: str,
    password: str,
    host_key: HostKey,
    *,
    port: int = 22,
    auth: AuthMethod = DEFAULT_AUTH,
    conn_timeout: float = CONNECT_TIMEOUT,
    banner_timeout: float = BANNER_TIMEOUT,
    auth_timeout: float = AUTH_TIMEOUT,
) -> IosXeSSH:
    """Open a session to a device, or raise.

    `host_key` is positional and required so a caller cannot reach a device
    without having looked one up. Use the result as a context manager.

    No `secret` is passed and `.enable()` is never called: NetHub requires
    privilege 15 at login precisely so there is no second secret to hold
    (design doc §4.3). `facts.get_privilege` is how a phase checks it got one.
    """
    try:
        return IosXeSSH(
            device_type="cisco_ios",
            host=host,
            port=port,
            username=username,
            password=password,
            host_key=host_key,
            auth=auth,
            conn_timeout=conn_timeout,
            banner_timeout=banner_timeout,
            auth_timeout=auth_timeout,
        )
    except HostKeyError:
        raise
    except NetmikoAuthenticationException as exc:
        # Netmiko's message is its own boilerplate plus paramiko's, and neither
        # carries the password -- but §7.3 keeps `error_summary` for a year, so
        # state the fault here rather than forwarding a library string into it.
        raise AuthenticationError(f"{host}:{port} rejected the credential") from exc
    except Exception as exc:  # netmiko raises several unrelated types here
        raise DeviceConnectionError(f"could not connect to {host}:{port}: {exc}") from exc


class _PinnedHostKeyPolicy(paramiko.MissingHostKeyPolicy):
    """Compare the offered host key against the pin. Returning accepts.

    Paramiko consults a policy only for a host it has no loaded key for, so
    the client below loads none at all -- every connection reaches this
    method, and the comparison is ours rather than paramiko's known_hosts
    matching. That also means no file on this host can quietly pre-approve an
    address (design doc §10 asks which `known_hosts` is actually in force
    under Ansible's connection plugins; owning the check removes the question).
    """

    def __init__(self, pinned: HostKey) -> None:
        self.pinned = pinned

    def missing_host_key(
        self, client: paramiko.SSHClient, hostname: str, key: paramiko.PKey
    ) -> None:
        offered = HostKey.of(key)
        if offered == self.pinned:
            return
        # Netmiko will not clean up after an exception it does not expect.
        client.close()
        if offered.key_type != self.pinned.key_type:
            raise HostKeyError(
                f"{hostname} offered a {offered.key_type} host key but is pinned to "
                f"{self.pinned.key_type}. A device whose key algorithm genuinely "
                f"changed needs an explicit admin re-accept, not a second pin."
            )
        raise HostKeyError(
            f"{hostname} host key changed: pinned {self.pinned.fingerprint_sha256}, "
            f"offered {offered.fingerprint_sha256}. Refusing to send a credential."
        )


class _SSHClientKeyboardInteractive(paramiko.SSHClient):
    """Answer the device's keyboard-interactive prompts with the password.

    IOS-XE running `aaa new-model` offers `publickey,keyboard-interactive` and
    not `password`. Paramiko falls back to keyboard-interactive only when no
    password was supplied at all, so Netmiko's password auth is rejected
    outright -- and because the device drops the session after one failed
    attempt, a try-password-then-fall-back is not available. The method is
    therefore stated by the caller, not discovered.
    """

    def _auth(self, username: str, password: str, *args: Any) -> None:
        transport = self.get_transport()
        assert transport is not None
        transport.auth_interactive(
            username,
            handler=lambda title, instructions, prompts: [password for _ in prompts],
        )


class IosXeSSH(CiscoIosSSH):
    """Netmiko's cisco_ios driver with NetHub's host-key policy and auth."""

    def __init__(self, *args: Any, host_key: HostKey, auth: AuthMethod, **kwargs: Any) -> None:
        self._host_key = host_key
        self._auth_method = auth
        super().__init__(*args, **kwargs)  # connects

    def _get_ssh_client_instance(self) -> paramiko.SSHClient:
        if self._auth_method == "keyboard-interactive":
            return _SSHClientKeyboardInteractive()
        return paramiko.SSHClient()

    def _build_ssh_client(self) -> paramiko.SSHClient:
        """Netmiko's version, minus every way a key could arrive from elsewhere.

        No `load_system_host_keys`, no alternate key file: a key loaded from
        this host's `~/.ssh/known_hosts` would make paramiko skip the policy
        entirely, which is the fail-closed check being switched off by a file
        nobody looked at.
        """
        client = self._get_ssh_client_instance()
        client.set_missing_host_key_policy(_PinnedHostKeyPolicy(self._host_key))
        return client
