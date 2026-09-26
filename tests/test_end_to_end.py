"""End to end: the web app and the sibling take one run through every phase.

Everything is real except the switch. The web requests go through Flask's test
client. The sibling is the real `Sibling` on the app `main()` builds
(`_database_app()`, shared settings only). The credential is really sealed
into the job row by the web side and really opened by the sibling with the
test key pair's private half (PLAN.md WS-7), and every command goes through
the real device layer. The switch is `FakeSwitch`, which sits behind
`Sibling.connect` and keeps its state across sessions, so a reload, the SCP
bracket and the staged bytes carry from one phase to the next the way they do
on hardware.

This is the safety net for the workstreams that rewrite the dispatch path
(PLAN.md WS-7, WS-8, WS-9). Only the `credential_channel` fixture knows how a
credential gets from Flask to the sibling; WS-7 swapped it from the socket to
the sealed column, and the scenarios stayed apart from the ones about the
socket's own behaviour.
"""

import hashlib
import io
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from nethub import artifacts
from nethub import sibling as S
from nethub.devices import connection, install, phases, transfer
from nethub.extensions import db
from nethub.models import (
    UpgradeHostPhaseResult,
    UpgradePhaseJob,
    UpgradeRun,
)
from tests.test_connection import device_key
from tests.test_install import CLEANUP_ACCEPTED, CLEANUP_PROMPT_OUTPUT

CAPTURE = Path(__file__).parent / "captures" / "c9200cx-12p-2x2g-17.12.06"

ADDRESS = "192.0.2.10"
HOSTNAME = "sw01"
BUNDLE = "iosxe-17-12-08"
TARGET = "17.12.08"
IMAGE = "cat9k_lite_iosxe.17.12.08.SPA.bin"
#: Synthetic. What matters is that the digest the device computes is taken over
#: the bytes that were actually pushed.
IMAGE_BYTES = b"synthetic IOS-XE image for the end-to-end test\n" * 2048
DEVICE_USER = "jsmith"
DEVICE_PASS = "d3vice-pa55"


# ---------------------------------------------------------------------------
# The switch
# ---------------------------------------------------------------------------

def _capture(name: str) -> str:
    return (CAPTURE / name).read_text()


#: Synthetic, in the shape a 17.12 device prints. The tail line is the one
#: `install.activate` actually tests.
INSTALL_OUTPUT = """install_add_activate_commit: START Thu Sep 24 10:00:00 UTC 2026
install_add_activate_commit: Adding PACKAGE
install_add_activate_commit: Checking whether new add is allowed ....
--- Starting Add ---
Performing Add on all members
  [1] Add package(s) on switch 1
  [1] Finished Add on switch 1
Checks passed: [1]
Finished Add
install_add_activate_commit: Activating PACKAGE
--- Starting Activate ---
  [1] Activate package(s) on switch 1
  [1] Finished Activate on switch 1
--- Starting Commit ---
  [1] Finished Commit on switch 1
Send model notification for install_add_activate_commit before reload
Install will reload the system now!
SUCCESS: install_add_activate_commit  Thu Sep 24 10:10:05 UTC 2026
"""


class FakeSwitch:
    """One Catalyst 9200CX, as the device layer sees it across a whole run.

    `show version`, `dir flash:` and `show privilege` are the verbatim captures
    under `tests/captures/`. The only edits are made in memory, at the points a
    real device's output would change: the version strings after the reload,
    and extra `dir` lines (with the free-space figure reduced to match) for
    files this switch has been sent. Everything else it answers is synthetic
    and says so.

    Anything it does not recognise goes into `unexpected` rather than raising
    straight out: `run_host` would turn an AssertionError into an ordinary
    failed host, and the test would then fail for a reason that looks like a
    device fault. Each test asserts `unexpected` is empty instead.
    """

    def __init__(self, *, privilege: int = 15):
        self.version = "17.12.06"
        self.privilege = privilege
        self.flash: dict[str, bytes] = {}
        self.scp_server = False
        #: Whether `ip scp server enable` was in the running-config at each
        #: `write memory` -- the thing §4.3.1's confirmed restore protects.
        self.saved_with_scp: list[bool] = []
        self.images = {IMAGE: TARGET}
        #: Connection attempts refused while the switch reloads.
        self.down_for = 0
        self.key = connection.HostKey.of(device_key())
        self.commands: list[str] = []
        self.logins: list[tuple[str, str]] = []
        self.unexpected: list[str] = []

    # -- what phases.default_connect and connection.scan_host_key would reach
    def scan(self, host: str) -> connection.HostKey:
        assert host == ADDRESS
        return self.key

    def connect(self, host: phases.HostTarget, username: str, password: str):
        """Checks in the order a real session meets them: TCP, host key, auth."""
        if self.down_for:
            self.down_for -= 1
            raise connection.DeviceConnectionError(f"could not reach {host.ansible_host}:22")
        # The pin `default_connect` uses: looked up from the confirmed row
        # before the host reached this worker thread, so a pin the UI never
        # confirmed fails before here just as it would against hardware.
        if host.host_key != self.key:
            raise connection.HostKeyError("host key does not match the pin")
        self.logins.append((username, password))
        if (username, password) != (DEVICE_USER, DEVICE_PASS):
            raise connection.AuthenticationError("authentication failed")
        return _Session(self)

    # -- the CLI
    def run(self, command: str) -> str:
        if command == "show version":
            return self._show_version()
        if command == "show privilege":
            if self.privilege == 15:
                return _capture("show_privilege.txt")
            return f"Current privilege level is {self.privilege}\n"  # synthetic
        if command == "dir flash:":
            return self._dir()
        if command == transfer._SCP_SHOW:
            return "ip scp server enable\n" if self.scp_server else ""
        if command.startswith("verify /sha512 flash:"):
            name = command.removeprefix("verify /sha512 flash:")
            if name not in self.flash:
                return f"%Error opening flash:{name} (No such file or directory)\n"
            digest = hashlib.sha512(self.flash[name]).hexdigest()
            return f".....Done!\nverify /sha512 (flash:{name}) = {digest}\n\n"
        if command == "show running-config":
            scp = "ip scp server enable\n" if self.scp_server else ""
            return f"Building configuration...\n\nhostname {HOSTNAME}\n{scp}end\n"
        if command == "write memory":
            self.saved_with_scp.append(self.scp_server)
            return "Building configuration...\n[OK]\n"
        match = re.fullmatch(r"install add file flash:(\S+) activate commit prompt-level none",
                             command)
        if match and match.group(1) in self.flash:
            # The session survives the whole install and the switch reboots
            # only afterwards (CLAUDE.md, "Device layer"). Two refused
            # connections exercise wait_for_device's retry loop.
            self.version = self.images[match.group(1)]
            self.down_for = 2
            return INSTALL_OUTPUT
        if command == "install remove inactive":
            return CLEANUP_PROMPT_OUTPUT
        if command == "y":
            self.flash = {k: v for k, v in self.flash.items() if self.images.get(k) != self.version}
            return CLEANUP_ACCEPTED
        self.unexpected.append(command)
        return "% Invalid input detected at '^' marker.\n"

    def configure(self, line: str) -> None:
        if line == "ip scp server enable":
            self.scp_server = True
        elif line == "no ip scp server enable":
            self.scp_server = False
        else:
            self.unexpected.append(line)

    def _show_version(self) -> str:
        # The capture names the release twice: zero-padded in the banner and
        # unpadded in the IOS line. A device on 17.12.08 prints 17.12.8 in the
        # second, which is why facts.same_version exists.
        short = ".".join(str(int(part)) for part in self.version.split("."))
        return (_capture("show_version.txt")
                .replace("Version 17.12.06", f"Version {self.version}")
                .replace("Version 17.12.6,", f"Version {short},"))

    def _dir(self) -> str:
        lines = _capture("dir.txt").rstrip("\n").splitlines()
        total_line = lines.pop()
        while lines and not lines[-1].strip():
            lines.pop()
        total, free = map(int, re.findall(r"\d+", total_line))
        for inode, (name, data) in enumerate(sorted(self.flash.items()), start=9000):
            lines.append(f"{inode}    -rw-  {len(data):>15}  Sep 24 2026 10:00:00 +00:00  {name}")
            free -= len(data)
        return "\n".join([*lines, "", f"{total} bytes total ({free} bytes free)"]) + "\n"


class _Session:
    """One SSH session: dead once disconnected, or once the switch reloads."""

    def __init__(self, switch: FakeSwitch):
        self.switch = switch
        self.alive = True

    def send_command(self, command, **kwargs):
        if not self.alive:
            raise OSError("Socket is closed")
        self.switch.commands.append(command)
        return self.switch.run(command)

    def send_config_set(self, lines):
        for line in lines:
            self.switch.commands.append(line)
            self.switch.configure(line)
        return ""

    def disconnect(self):
        self.alive = False


def _scp_put(conn, *, source, image, file_system):
    """The second session's SCP put, landing the real published bytes on flash.

    Replaces `transfer._scp_put` only; the bracket around it, the digest check
    after it and the skip-if-staged test before it all run for real.
    """
    assert file_system == "flash:"
    if not conn.switch.scp_server:
        raise OSError("SCP server refused the connection")
    conn.switch.flash[image] = Path(source).read_bytes()


# ---------------------------------------------------------------------------
# The two processes
# ---------------------------------------------------------------------------

class Clock:
    """The sibling's clock. Wall time plus however far a test moves it."""

    def __init__(self):
        self.offset = timedelta(0)

    def __call__(self) -> datetime:
        return datetime.now(timezone.utc) + self.offset


@pytest.fixture
def credential_channel(app, credential_private_key):
    """How a credential gets from Flask to the sibling: sealed into the job
    row to the app's public key (conftest's key pair), opened by the sibling
    with the private half. `held()` counts rows still carrying ciphertext.
    `clock` is the sibling's, so a scenario can move it past a deadline."""
    def held():
        with app.app_context():
            return UpgradePhaseJob.query.filter(
                UpgradePhaseJob.sealed_credential.isnot(None)).count()

    return type("Channel", (), {"private_key": credential_private_key,
                                "clock": Clock(), "held": staticmethod(held)})


@pytest.fixture
def switch(monkeypatch):
    switch = FakeSwitch()
    monkeypatch.setattr(connection, "scan_host_key", switch.scan)
    monkeypatch.setattr(transfer, "_scp_put", _scp_put)
    return switch


@pytest.fixture
def sibling(app, credential_channel, switch):
    """The sibling as `main()` builds it: its own app on `shared_config` alone,
    against the same database file, swept once at start."""
    sibling_app = S._database_app()
    worker = S.Sibling(
        private_key=credential_channel.private_key,
        now=credential_channel.clock,
        search_dir=artifacts.store_dir(app.config),
        reload_wait=install.ReloadWait(delay=0, interval=0, timeout=10),
        connect=switch.connect,
    )
    with sibling_app.app_context():
        worker.sweep()
    yield sibling_app, worker
    with sibling_app.app_context():
        db.session.remove()
        db.engine.dispose()


@pytest.fixture
def work(sibling, caplog):
    """Run the sibling's loop body until the queue is empty. Returns what each
    pass did.

    `tick()` logs and swallows an unexpected exception, then fails whatever was
    running. That is right for production and hides the cause in a test, so
    any such log line fails the test here.
    """
    sibling_app, worker = sibling

    def _work() -> list[str]:
        done = []
        with caplog.at_level(logging.ERROR, logger=S.log.name), sibling_app.app_context():
            while (status := worker.tick()) is not None:
                done.append(status)
        assert "raised" not in caplog.text, caplog.text
        return done

    return _work


# ---------------------------------------------------------------------------
# The web side
# ---------------------------------------------------------------------------

@pytest.fixture
def web(app, client, make_user):
    """Log in and set up everything a run needs, through the routes."""
    app.config["DEVICE_TARGET_CIDRS"] = ["192.0.2.0/24"]
    username, password = make_user()
    response = client.post("/login", data={"username": username, "password": password})
    assert response.status_code == 302
    client.post("/profile/device-username", data={"device_username": DEVICE_USER})
    return client


def _location_id(response) -> int:
    assert response.status_code == 302, response.status_code
    return int(response.headers["Location"].rstrip("/").rsplit("/", 1)[-1])


def confirm_host_key(client, work) -> None:
    scan_id = _location_id(client.post("/hostkeys/scan", data={"address": ADDRESS}))
    assert work() == ["succeeded"]
    response = client.post("/hostkeys/confirm", data={"scan_id": str(scan_id)})
    assert response.headers["Location"].endswith("/hostkeys")


def publish_image(client) -> None:
    response = client.post(
        "/artifacts/new",
        data={
            "bundle_key": BUNDLE,
            "version": TARGET,
            "sha512": hashlib.sha512(IMAGE_BYTES).hexdigest(),
            "image": (io.BytesIO(IMAGE_BYTES), IMAGE),
        },
        content_type="multipart/form-data",
    )
    assert response.headers["Location"].endswith("/artifacts")


def submit(client) -> int:
    return _location_id(client.post("/upgrades/new", data={
        "bundle": BUNDLE,
        "hosts": f"{HOSTNAME}, {ADDRESS}",
        "device_password": DEVICE_PASS,
    }))


def approve(client, run_id: int, phase: str) -> None:
    client.post(f"/upgrades/{run_id}/approve",
                data={"phase": phase, "device_password": DEVICE_PASS})


@pytest.fixture
def submitted(web, work, switch):
    """A run submitted through the UI, before the sibling has touched it."""
    confirm_host_key(web, work)
    publish_image(web)
    return submit(web)


def state(app, run_id):
    """The run, its jobs and its one host, as plain values."""
    with app.app_context():
        run = db.session.get(UpgradeRun, run_id)
        jobs = (UpgradePhaseJob.query.filter_by(run_id=run_id)
                .order_by(UpgradePhaseJob.created_at, UpgradePhaseJob.id).all())
        host = run.hosts[0]
        return {
            "run": (run.state, run.awaiting_phase),
            "jobs": [(j.phase, j.status) for j in jobs],
            "failure": [(j.phase, j.failure_stage) for j in jobs if j.failure_stage],
            "host": host.state,
            "versions": (host.reported_version_pre, host.reported_version_post),
        }


# ---------------------------------------------------------------------------
# The scenarios
# ---------------------------------------------------------------------------

class TestCleanRun:
    def test_every_phase_runs_and_the_run_completes(
        self, app, web, work, switch, submitted, credential_channel
    ):
        run_id = submitted

        assert work() == ["succeeded"]
        assert state(app, run_id)["run"] == ("awaiting_approval", "stage")
        assert state(app, run_id)["host"] == "precheck_ok"

        approve(web, run_id, "stage")
        assert work() == ["succeeded"]
        assert state(app, run_id)["run"] == ("awaiting_approval", "activate")
        assert switch.flash[IMAGE] == IMAGE_BYTES
        assert switch.scp_server is False, "the SCP server was restored"

        approve(web, run_id, "activate")
        # One pass: verify has no gate and runs straight after activate, on the
        # credential activate's approval supplied. Nobody holds one for it.
        assert work() == ["succeeded"]
        assert state(app, run_id)["run"] == ("awaiting_approval", "cleanup")
        assert switch.version == TARGET

        approve(web, run_id, "cleanup")
        assert work() == ["succeeded"]

        final = state(app, run_id)
        assert final["run"] == ("completed", None)
        assert final["jobs"] == [
            ("precheck", "succeeded"),
            ("stage", "succeeded"),
            ("activate", "succeeded"),
            ("verify", "succeeded"),
            ("cleanup", "succeeded"),
        ]
        assert final["failure"] == []
        assert final["host"] == "verified"
        # Recorded as the device prints them, unpadded: 17.12.8 for a 17.12.08
        # target, which only facts.same_version treats as equal.
        assert final["versions"] == ("17.12.6", "17.12.8")

        with app.app_context():
            stage = UpgradeHostPhaseResult.query.filter_by(run_id=run_id, phase="stage").one()
            assert stage.scp_restore_confirmed is True
        assert switch.saved_with_scp == [False], "write memory never saved the SCP server on"
        assert IMAGE not in switch.flash, "cleanup removed the installed package file"
        assert switch.unexpected == []
        # Each phase logged in once with the submitter's credential, apart from
        # activate, which logs in again after the reload.
        assert set(switch.logins) == {(DEVICE_USER, DEVICE_PASS)}
        assert credential_channel.held() == 0, "every sealed credential was taken off its row"

        assert web.get(f"/upgrades/{run_id}").status_code == 200

    def test_the_password_reaches_no_row(self, app, web, work, submitted):
        """Checked against the database file itself, WAL included, rather
        than against the columns someone remembered to list."""
        for phase in ("stage", "activate", "cleanup"):
            work()
            approve(web, submitted, phase)
        work()
        assert state(app, submitted)["run"] == ("completed", None)

        path = Path(app.config["SQLALCHEMY_DATABASE_URI"].removeprefix("sqlite:///"))
        on_disk = b"".join(p.read_bytes() for p in path.parent.glob(path.name + "*"))
        assert on_disk, "found the database file"
        assert DEVICE_PASS.encode() not in on_disk


class TestPrecheckFailure:
    def test_an_under_privileged_account_fails_the_run_before_any_change(
        self, app, web, work, switch, submitted
    ):
        switch.privilege = 1

        assert work() == ["failed"]

        final = state(app, submitted)
        assert final["run"] == ("failed", None)
        assert final["jobs"] == [("precheck", "failed")]
        assert final["failure"] == [("precheck", "privilege")]
        assert final["host"] == "failed"
        assert switch.commands == ["show version", "show privilege"]

        approve(web, submitted, "stage")
        assert work() == []
        assert state(app, submitted)["jobs"] == [("precheck", "failed")]


class TestCancel:
    def test_cancel_at_a_gate_closes_the_run_and_nothing_else_runs(
        self, app, web, work, switch, submitted
    ):
        assert work() == ["succeeded"]
        seen = len(switch.commands)

        web.post(f"/upgrades/{submitted}/cancel")
        assert state(app, submitted)["run"] == ("cancelled", None)

        approve(web, submitted, "stage")
        assert work() == []
        final = state(app, submitted)
        assert final["run"] == ("cancelled", None)
        assert final["jobs"] == [("precheck", "succeeded")]
        assert switch.commands[seen:] == []

    def test_cancel_after_approval_stops_the_queued_phase_and_drops_its_credential(
        self, app, web, work, switch, submitted, credential_channel
    ):
        assert work() == ["succeeded"]
        approve(web, submitted, "stage")
        assert credential_channel.held() == 1
        seen = len(switch.commands)

        web.post(f"/upgrades/{submitted}/cancel")
        assert credential_channel.held() == 0, "cancel dropped the ciphertext"

        assert work() == ["cancelled"]
        final = state(app, submitted)
        assert final["run"] == ("cancelled", None)
        assert final["jobs"] == [("precheck", "succeeded"), ("stage", "cancelled")]
        assert switch.commands[seen:] == []


class TestExpiredCredential:
    def test_a_phase_not_started_by_its_deadline_expires_without_touching_the_device(
        self, app, web, work, switch, submitted, credential_channel
    ):
        """A sealed credential lives exactly as long as its job may wait, so
        there is no separate TTL to run out: the job reaches its deadline
        unclaimed and ends `expired`. It records failure_stage='credential'
        (maintainer decision, PLAN.md WS-7), so the run page says the
        approval's credential was never used."""
        assert work() == ["succeeded"]
        approve(web, submitted, "stage")
        seen = len(switch.commands)

        # The sibling does not get to this job until well past its deadline --
        # down for a day, say.
        credential_channel.clock.offset = timedelta(days=1)

        assert work() == ["expired"]
        final = state(app, submitted)
        assert final["run"] == ("expired", None)
        assert final["jobs"] == [("precheck", "succeeded"), ("stage", "expired")]
        assert final["failure"] == [("stage", "credential")]
        assert switch.commands[seen:] == []
        assert credential_channel.held() == 0, "the unused ciphertext was dropped"


class TestWebRestart:
    def test_a_web_restart_between_approval_and_claim_costs_nothing(
        self, app, web, work, switch, submitted, credential_channel
    ):
        """PLAN.md WS-7's done-when. Under the socket the approved credential
        lived only in the web process's memory, so a restart before the
        sibling claimed the job failed the phase with `credential`. Now it is
        in the row: a fresh web app over the same database is all a restart
        is, and the phase still runs."""
        from nethub import create_app

        assert work() == ["succeeded"]
        approve(web, submitted, "stage")
        restarted = create_app()  # the old app's memory is gone as far as this run knows
        assert restarted is not app

        assert work() == ["succeeded"]
        assert state(app, submitted)["run"] == ("awaiting_approval", "activate")
        assert switch.flash[IMAGE] == IMAGE_BYTES


# ---------------------------------------------------------------------------
# Two switches: one host failing a phase (PLAN.md WS-8)
# ---------------------------------------------------------------------------

ADDRESS2 = "192.0.2.11"
HOSTNAME2 = "sw02"


class Fleet:
    """Two `FakeSwitch`es behind one `connect`, picked by address."""

    def __init__(self):
        self.switches = {ADDRESS: FakeSwitch(), ADDRESS2: FakeSwitch()}

    def __getitem__(self, address):
        return self.switches[address]

    def scan(self, host: str) -> connection.HostKey:
        return self.switches[host].key

    def connect(self, host: phases.HostTarget, username: str, password: str):
        return self.switches[host.ansible_host].connect(host, username, password)


class TestOneHostFailing:
    @pytest.fixture
    def switch(self, monkeypatch):
        fleet = Fleet()
        monkeypatch.setattr(connection, "scan_host_key", fleet.scan)
        monkeypatch.setattr(transfer, "_scp_put", _scp_put)
        return fleet

    @pytest.fixture
    def run_id(self, web, work):
        for address in (ADDRESS, ADDRESS2):
            scan_id = _location_id(web.post("/hostkeys/scan", data={"address": address}))
            assert work() == ["succeeded"]
            web.post("/hostkeys/confirm", data={"scan_id": str(scan_id)})
        publish_image(web)
        run_id = _location_id(web.post("/upgrades/new", data={
            "bundle": BUNDLE,
            "hosts": f"{HOSTNAME}, {ADDRESS}\n{HOSTNAME2}, {ADDRESS2}",
            "device_password": DEVICE_PASS,
        }))
        assert work() == ["succeeded"]
        return run_id

    @staticmethod
    def hosts(app, run_id):
        with app.app_context():
            return {h.hostname: h.state for h in db.session.get(UpgradeRun, run_id).hosts}

    def test_a_host_that_failed_stage_is_retried_and_the_run_completes(
        self, app, web, work, switch, run_id, credential_channel
    ):
        switch[ADDRESS2].down_for = 1  # unreachable for the stage session only
        approve(web, run_id, "stage")
        assert work() == ["partial"]
        assert state(app, run_id)["run"] == ("awaiting_approval", "activate")
        assert self.hosts(app, run_id) == {HOSTNAME: "staged", HOSTNAME2: "failed"}
        assert "Retry stage on 1 host(s)" in web.get(f"/upgrades/{run_id}").get_data(
            as_text=True)

        web.post(f"/upgrades/{run_id}/retry",
                 data={"phase": "stage", "device_password": DEVICE_PASS})
        assert work() == ["succeeded"]
        assert state(app, run_id)["run"] == ("awaiting_approval", "activate")
        assert self.hosts(app, run_id) == {HOSTNAME: "staged", HOSTNAME2: "staged"}

        approve(web, run_id, "activate")
        assert work() == ["succeeded"]
        approve(web, run_id, "cleanup")
        assert work() == ["succeeded"]

        final = state(app, run_id)
        assert final["run"] == ("completed", None)
        assert final["jobs"] == [
            ("precheck", "succeeded"),
            ("stage", "partial"),
            ("stage", "succeeded"),
            ("activate", "succeeded"),
            ("verify", "succeeded"),
            ("cleanup", "succeeded"),
        ]
        assert self.hosts(app, run_id) == {HOSTNAME: "verified", HOSTNAME2: "verified"}
        for address in (ADDRESS, ADDRESS2):
            assert switch[address].version == TARGET
            assert switch[address].unexpected == []
            assert switch[address].saved_with_scp == [False]
        assert credential_channel.held() == 0

    def test_without_a_retry_the_others_finish_and_the_failed_host_is_left_alone(
        self, app, web, work, switch, run_id
    ):
        switch[ADDRESS2].down_for = 1
        approve(web, run_id, "stage")
        assert work() == ["partial"]
        approve(web, run_id, "activate")
        assert work() == ["succeeded"]
        approve(web, run_id, "cleanup")
        assert work() == ["succeeded"]

        assert state(app, run_id)["run"] == ("completed", None)
        assert self.hosts(app, run_id) == {HOSTNAME: "verified", HOSTNAME2: "failed"}
        assert switch[ADDRESS].version == TARGET
        assert switch[ADDRESS2].version == "17.12.06", "never reloaded"
        assert not any(c.startswith("install add") for c in switch[ADDRESS2].commands)

    def test_a_mistyped_password_is_tried_on_one_switch_not_every_one(
        self, app, web, work, switch, run_id
    ):
        """Each refused login counts toward the AAA server's lockout."""
        web.post(f"/upgrades/{run_id}/approve",
                 data={"phase": "stage", "device_password": "typo"})
        assert work() == ["failed"]
        wrong = [login for address in (ADDRESS, ADDRESS2)
                 for login in switch[address].logins if login[1] == "typo"]
        assert wrong == [(DEVICE_USER, "typo")]
        assert state(app, run_id)["failure"] == [("stage", "credential")]
        assert switch[ADDRESS2].flash == {}
