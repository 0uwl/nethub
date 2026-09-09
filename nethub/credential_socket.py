"""The secret channel: per-execution device credentials, and nothing else.

Design doc §9.1/§9.2. The job row is the only *control* channel between Flask
and the sibling; this is the single narrow exception, and it carries secrets in
one direction with no control information in either.

Four things about the construction are easy to get wrong:

- **The sibling connects; Flask serves.** Not the other way round. A deposit
  endpoint would leave the sibling holding secrets for executions it has not
  started, and would let anything that can reach it flood the holder. The
  reason is window minimisation, not that a deposit socket resembles the
  Podman socket.
- **The mount is the authenticator, not `SO_PEERCRED`.** Every unit runs under
  one rootless user, so a uid check tells Flask only "the peer shares my uid",
  which anything a Flask compromise spawns also satisfies. What decides who
  may open this is the filesystem: the socket volume is mounted into exactly
  two units, mode 0600. Don't write a uid check and believe it discriminates.
  Authorizing on the peer *pid* is wrong in any case -- it is a snapshot, so a
  later `/proc/<pid>/...` lookup is a pid-reuse race, and across PID
  namespaces it arrives as 0.
- **The listening socket comes from a systemd `.socket` unit**, never from
  `unlink()` + `bind()` in either container. `serve()` therefore takes an
  already-listening socket rather than a path.
- **The response surface needs framing, caps, deadlines, and an allowlist.**
  The sibling parses bytes Flask chose, so a compromised Flask is parsing
  input to the privileged side. `fetch_credential` enforces all four.

The store is keyed by `upgrade_phase_jobs.id`, never by `run_id`: keying by
run would eventually hand one person's password to another person's approved
execution, against an address that person chose.
"""

from __future__ import annotations

import json
import os
import socket
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

#: The credential sits in Flask's memory from approval until the sibling picks
#: the row up, and one execution runs at a time -- so a phase approved into a
#: long queue can wait. Bounded in minutes; on expiry the phase fails
#: `credential` and the operator re-approves (§9.1).
DEFAULT_TTL = timedelta(minutes=30)

#: Framing and caps. A request is one JSON object on one line.
MAX_REQUEST_BYTES = 4096
MAX_RESPONSE_BYTES = 8192
SOCKET_DEADLINE = 10.0

#: What a device credential may contain before it reaches a variable or a
#: command string. Printable ASCII only -- a control character here would
#: reach a CLI session.
_ALLOWED = frozenset(chr(c) for c in range(0x20, 0x7F))
MAX_CREDENTIAL_LEN = 128


class CredentialError(Exception):
    """No credential was released for this execution."""


@dataclass
class _Held:
    username: str
    password: str
    approved_by: int
    expires_at: datetime


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def check_credential(value: str) -> str:
    """Allowlist a credential before it is used anywhere."""
    if not value or len(value) > MAX_CREDENTIAL_LEN:
        raise CredentialError("credential is empty or over the length cap")
    if not set(value) <= _ALLOWED:
        raise CredentialError("credential contains characters outside the allowlist")
    return value


class CredentialStore:
    """Flask's side: hold approved credentials in memory, release once.

    Nothing here is written to disk, to a row, or to a log. The process holding
    this is the one behind the unauthenticated route, so the store is bounded
    by a TTL and emptied by use.
    """

    def __init__(self, ttl: timedelta = DEFAULT_TTL, now=_utcnow):
        self._held: dict[int, _Held] = {}
        self._lock = threading.Lock()
        self._ttl = ttl
        self._now = now

    def hold(self, job_id: int, username: str, password: str, approved_by: int) -> None:
        check_credential(password)
        with self._lock:
            self._held[job_id] = _Held(
                username, password, approved_by, self._now() + self._ttl
            )

    def release(self, job_id: int, approved_by: int) -> tuple[str, str]:
        """Hand over the credential for a job, once.

        `approved_by` is cross-checked rather than trusted: the caller says
        which identity approved the execution, and a mismatch means the job
        row and the held credential disagree about whose secret this is.
        """
        # Popped before it is validated, deliberately: a release that fails
        # its checks destroys the credential rather than leaving it for a
        # retry. That makes replay worthless, at the cost that a stray request
        # forces a re-approval -- acceptable, since only the sibling can reach
        # this socket at all (§9.2's mount argument).
        with self._lock:
            held = self._held.pop(job_id, None)
        if held is None:
            raise CredentialError("no credential held for this execution")
        if self._now() >= held.expires_at:
            raise CredentialError("held credential expired; the phase needs re-approval")
        if held.approved_by != approved_by:
            raise CredentialError("the approving identity does not match the held credential")
        return held.username, held.password

    def discard(self, job_id: int) -> None:
        with self._lock:
            self._held.pop(job_id, None)

    def purge_expired(self) -> int:
        with self._lock:
            now = self._now()
            stale = [k for k, v in self._held.items() if now >= v.expires_at]
            for key in stale:
                del self._held[key]
        return len(stale)

    def __len__(self) -> int:
        with self._lock:
            return len(self._held)


def handle_request(raw: bytes, store: CredentialStore, verify_running) -> bytes:
    """One request, one response. The only message type there is.

    `verify_running(job_id) -> approved_by` is the interlock: the sibling sets
    `running` and then asks Flask to confirm it, so the precondition is
    controlled by the requester and constrains a compromised sibling not at
    all. That is accepted (a sibling holding Podman already owns the host).
    What it buys is that a stray or duplicated request cannot drain
    credentials for jobs nobody started, and one-shot release makes replay
    worthless.
    """
    try:
        if len(raw) > MAX_REQUEST_BYTES:
            raise CredentialError("request over the size cap")
        message = json.loads(raw.decode("utf-8"))
        if not isinstance(message, dict) or set(message) != {"job_id"}:
            raise CredentialError("unrecognised message")
        job_id = message["job_id"]
        if not isinstance(job_id, int):
            raise CredentialError("unrecognised message")

        approved_by = verify_running(job_id)
        username, password = store.release(job_id, approved_by)
        body = {"ok": True, "username": username, "password": password}
    except CredentialError as exc:
        body = {"ok": False, "error": str(exc)}
    except Exception:  # noqa: BLE001 -- deliberate: see below
        # Never echo an unexpected failure back across this socket: the reply
        # is parsed by the privileged side, and a library message here could
        # quote whatever was being handled.
        body = {"ok": False, "error": "request refused"}
    return json.dumps(body).encode("utf-8") + b"\n"


def serve_once(conn: socket.socket, store: CredentialStore, verify_running) -> None:
    conn.settimeout(SOCKET_DEADLINE)
    try:
        raw = _read_line(conn, MAX_REQUEST_BYTES)
        conn.sendall(handle_request(raw, store, verify_running))
    finally:
        conn.close()


def serve(listening: socket.socket, store: CredentialStore, verify_running,
          stop: threading.Event | None = None) -> None:
    """Accept on an already-listening socket. Runs on its own thread.

    The socket is not created here -- see the module docstring. Served on a
    thread with deadlines because Flask must stay multi-threaded (§3.2): a
    blocking accept on the request path would hold the phone-home route.
    """
    listening.settimeout(0.5)
    while stop is None or not stop.is_set():
        try:
            conn, _ = listening.accept()
        except TimeoutError:
            continue
        except OSError:
            break
        try:
            serve_once(conn, store, verify_running)
        except Exception:  # noqa: BLE001, S110 -- one bad peer must not stop the server
            pass


def _read_line(conn: socket.socket, cap: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = conn.recv(1024)
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
        if size > cap:
            raise CredentialError("request over the size cap")
        if b"\n" in chunk:
            break
    return b"".join(chunks).split(b"\n", 1)[0]


def fetch_credential(connect_socket, job_id: int) -> tuple[str, str]:
    """The sibling's side: ask for this execution's credential.

    Everything the reply says is treated as input from an untrusted peer --
    capped, deadlined, and allowlisted -- because a compromised Flask chooses
    these bytes and the sibling is the privileged side.
    """
    conn = connect_socket()
    try:
        conn.settimeout(SOCKET_DEADLINE)
        conn.sendall(json.dumps({"job_id": job_id}).encode("utf-8") + b"\n")
        raw = _read_line(conn, MAX_RESPONSE_BYTES)
    finally:
        conn.close()

    try:
        reply = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise CredentialError("unparseable reply from the credential socket") from exc
    if not isinstance(reply, dict) or not reply.get("ok"):
        raise CredentialError("credential was not released for this execution")
    username, password = reply.get("username"), reply.get("password")
    if not isinstance(username, str) or not isinstance(password, str):
        raise CredentialError("malformed credential in reply")
    return check_credential(username), check_credential(password)


#: systemd hands inherited descriptors over starting at fd 3.
SD_LISTEN_FDS_START = 3


def systemd_socket() -> socket.socket | None:
    """The listening socket a systemd `.socket` unit handed over, or None.

    Nothing here calls `bind()`. Returning None when the process was not
    socket-activated is deliberate: the caller skips serving rather than
    falling back to creating a socket itself, so a misconfigured unit fails
    visibly instead of quietly opening a path with the wrong ownership.
    """
    listen_pid = os.environ.get("LISTEN_PID")
    if listen_pid is not None and listen_pid != str(os.getpid()):
        return None
    try:
        count = int(os.environ.get("LISTEN_FDS", "0") or 0)
    except ValueError:
        return None
    if count < 1:
        return None
    # Already listening -- systemd did that. Adopt the descriptor as-is.
    return socket.socket(socket.AF_UNIX, socket.SOCK_STREAM,
                         fileno=SD_LISTEN_FDS_START)


def connect_to(path: str):
    """The sibling's side of the mount: open the socket it was given."""
    def _open():
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(path)
        return client
    return _open
