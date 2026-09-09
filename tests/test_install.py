"""Checks for nethub.devices.install.

No device, and no reload. The guards and the reconnect loop are what is
testable here; the install command itself is exercised only through its
output handling, which is where the ported playbook had no coverage at all.
"""

import pytest

from nethub.devices import facts, install
from tests.test_transfer import DIGEST, OTHER_DIGEST, dir_output

IMAGE = "cat9k_lite_iosxe.17.12.06.SPA.bin"
TARGET = "17.12.06"

SHOW_VERSION = """Cisco IOS XE Software, Version 17.09.04
Cisco IOS Software [Cupertino], Catalyst L3 Switch Software (CAT9K_LITE_IOSXE), \
Version 17.9.4, RELEASE SOFTWARE (fc4)
Copyright (c) 1986-2023 by Cisco Systems, Inc.

ROM: IOS-XE ROMMON

sw01 uptime is 3 days
System returned to ROM by Reload Command
System image file is "flash:packages.conf"
Last reload reason: Reload Command

Configuration register is 0x102
"""


def show_version(version: str, image: str = "packages.conf") -> str:
    return (
        SHOW_VERSION.replace("Version 17.9.4, RELEASE", f"Version {version}, RELEASE")
        .replace('flash:packages.conf', f"flash:{image}")
    )


class FakeDevice:
    def __init__(self, *, version="17.9.4", boot_image="packages.conf", privilege=15,
                 files=None, digest=DIGEST, install_output="SUCCESS: install_add_activate_commit"):
        self.version = version
        self.boot_image = boot_image
        self.privilege = privilege
        default = {IMAGE: 500_000_000}
        self.files = {"packages.conf": 4905, **(default if files is None else files)}
        self.digest = digest
        self.install_output = install_output
        self.commands = []

    def send_command(self, command, **kwargs):
        self.commands.append(command)
        if command == "show version":
            return show_version(self.version, self.boot_image)
        if command == "show privilege":
            return f"Current privilege level is {self.privilege}"
        if command.startswith("dir "):
            return dir_output(self.files)
        if command.startswith("verify /sha512"):
            return f".Done!\nverify /sha512 ({command.split()[-1]}) = {self.digest}\n"
        if command == "write memory":
            return "Building configuration...\n[OK]"
        if command.startswith("install add"):
            if isinstance(self.install_output, Exception):
                raise self.install_output
            return self.install_output
        if command == "show running-config":
            return "hostname sw01\n"
        raise AssertionError(f"unexpected command {command!r}")

    def disconnect(self):
        pass


class TestGuards:
    def test_happy_path_returns_the_facts_it_gathered(self):
        device = FakeDevice()
        got = install.assert_ready_to_activate(
            device, image=IMAGE, sha512=DIGEST, target_version=TARGET
        )
        assert got.version == "17.9.4"

    def test_already_running_the_target_is_refused(self):
        """17.12.06 and 17.12.6 are the same release."""
        device = FakeDevice(version="17.12.6")
        with pytest.raises(install.InstallError) as excinfo:
            install.assert_ready_to_activate(
                device, image=IMAGE, sha512=DIGEST, target_version="17.12.06"
            )
        assert excinfo.value.status == "already_current"

    def test_bundle_mode_is_refused(self):
        device = FakeDevice(boot_image="cat9k_lite_iosxe.17.09.04.SPA.bin")
        with pytest.raises(install.InstallError, match="not INSTALL") as excinfo:
            install.assert_ready_to_activate(
                device, image=IMAGE, sha512=DIGEST, target_version=TARGET
            )
        assert excinfo.value.status == "wrong_boot_mode"

    def test_unknown_boot_mode_is_refused_rather_than_attempted(self):
        device = FakeDevice(boot_image="something-odd")
        with pytest.raises(install.InstallError, match="unknown"):
            install.assert_ready_to_activate(
                device, image=IMAGE, sha512=DIGEST, target_version=TARGET
            )

    def test_under_privileged_account_is_refused(self):
        device = FakeDevice(privilege=1)
        with pytest.raises(install.InstallError) as excinfo:
            install.assert_ready_to_activate(
                device, image=IMAGE, sha512=DIGEST, target_version=TARGET
            )
        assert excinfo.value.status == "privilege"

    def test_missing_image_is_refused(self):
        device = FakeDevice(files={})
        with pytest.raises(install.InstallError) as excinfo:
            install.assert_ready_to_activate(
                device, image=IMAGE, sha512=DIGEST, target_version=TARGET
            )
        assert excinfo.value.status == "image_missing"

    def test_present_image_with_the_wrong_bytes_is_refused(self):
        """The gap in the playbook: a `dir` check passes a failed staging."""
        device = FakeDevice(digest=OTHER_DIGEST)
        with pytest.raises(Exception) as excinfo:
            install.assert_ready_to_activate(
                device, image=IMAGE, sha512=DIGEST, target_version=TARGET
            )
        assert "hashes to" in str(excinfo.value)
        assert "install add" not in " ".join(device.commands)


class TestActivate:
    def test_saves_configuration_before_installing(self):
        device = FakeDevice()
        install.activate(device, image=IMAGE, sha512=DIGEST, target_version=TARGET)
        assert device.commands.index("write memory") < next(
            i for i, c in enumerate(device.commands) if c.startswith("install add")
        )

    def test_issues_the_non_interactive_install_command(self):
        device = FakeDevice()
        out = install.activate(device, image=IMAGE, sha512=DIGEST, target_version=TARGET)
        assert (
            f"install add file flash:{IMAGE} activate commit prompt-level none"
            in device.commands
        )
        assert out.status == "activating"
        assert out.version_before == "17.9.4"

    def test_a_lost_session_is_the_expected_ending(self):
        """The reload takes the session with it."""
        device = FakeDevice(install_output=OSError("socket closed"))
        out = install.activate(device, image=IMAGE, sha512=DIGEST, target_version=TARGET)
        assert out.status == "activating"
        assert "socket closed" in out.transcript

    def test_a_clean_return_without_success_is_a_failure(self):
        """IOS-XE reports some install failures in-band and stays up."""
        device = FakeDevice(install_output="FAILED: install_add_activate_commit\nreason: no space")
        with pytest.raises(install.InstallError) as excinfo:
            install.activate(device, image=IMAGE, sha512=DIGEST, target_version=TARGET)
        assert excinfo.value.status == "install_failed"

    def test_silence_is_also_a_failure_not_a_reboot(self):
        device = FakeDevice(install_output="")
        with pytest.raises(install.InstallError, match="did not report success"):
            install.activate(device, image=IMAGE, sha512=DIGEST, target_version=TARGET)

    def test_guards_run_before_anything_is_written(self):
        device = FakeDevice(version="17.12.6")
        with pytest.raises(install.InstallError):
            install.activate(device, image=IMAGE, sha512=DIGEST, target_version="17.12.06")
        assert "write memory" not in device.commands


class TestWaitForDevice:
    def make_clock(self):
        now = {"t": 0.0}
        return now, (lambda: now["t"]), (lambda s: now.__setitem__("t", now["t"] + s))

    def test_waits_the_delay_before_the_first_attempt(self):
        now, clock, sleep = self.make_clock()
        attempts = []

        def connect():
            attempts.append(now["t"])
            return FakeDevice(version="17.12.6")

        install.wait_for_device(connect, sleep=sleep, clock=clock)
        assert attempts == [60.0], "no attempt before the delay"

    def test_retries_until_the_device_answers(self):
        now, clock, sleep = self.make_clock()
        results = [OSError("refused"), OSError("refused"), FakeDevice(version="17.12.6")]

        def connect():
            item = results.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        conn = install.wait_for_device(connect, sleep=sleep, clock=clock)
        assert conn.version == "17.12.6"
        assert now["t"] == pytest.approx(60.0 + 30.0 * 2)

    def test_a_device_answering_ssh_but_not_serving_cli_is_not_ready(self):
        """IOS-XE accepts connections before it is done booting."""
        now, clock, sleep = self.make_clock()
        half_up = FakeDevice()
        half_up.send_command = lambda command, **kw: (_ for _ in ()).throw(OSError("not ready"))
        results = [half_up, FakeDevice(version="17.12.6")]

        conn = install.wait_for_device(lambda: results.pop(0), sleep=sleep, clock=clock)
        assert conn.version == "17.12.6"

    def test_a_half_up_connection_is_closed_rather_than_leaked(self):
        now, clock, sleep = self.make_clock()
        half_up = FakeDevice()
        half_up.send_command = lambda command, **kw: (_ for _ in ()).throw(OSError("not ready"))
        closed = []
        half_up.disconnect = lambda: closed.append(True)
        results = [half_up, FakeDevice(version="17.12.6")]

        install.wait_for_device(lambda: results.pop(0), sleep=sleep, clock=clock)
        assert closed == [True]

    def test_gives_up_at_the_deadline(self):
        now, clock, sleep = self.make_clock()

        def connect():
            raise OSError("refused")

        with pytest.raises(install.ReloadTimeout) as excinfo:
            install.wait_for_device(
                connect, wait=install.ReloadWait(delay=60, interval=30, timeout=180),
                sleep=sleep, clock=clock,
            )
        assert excinfo.value.status == "reload_timeout"
        assert "refused" in str(excinfo.value)
        assert now["t"] <= 180 + 30


class TestVerifyUpgrade:
    def test_accepts_the_registry_spelling_of_the_same_release(self):
        device = FakeDevice(version="17.12.6")
        assert install.verify_upgrade(device, target_version="17.12.06").version == "17.12.6"

    def test_a_device_back_on_the_old_release_is_a_failure(self):
        device = FakeDevice(version="17.9.4")
        with pytest.raises(install.PostCheckError) as excinfo:
            install.verify_upgrade(device, target_version="17.12.06")
        assert excinfo.value.status == "wrong_version"


# Verbatim from a real 17.12.08 device (the tail; the scan output above it is
# elided). Both the prompt and the decline path were captured by answering `n`.
CLEANUP_PROMPT_OUTPUT = """install_remove: START Wed Sep 09 10:47:00 UTC 2026
install_remove: Removing IMG
Cleaning up unnecessary package files

The following files will be deleted:
    [R0]: /flash/cat9k_lite_iosxe.17.12.08.SPA.bin
    [R0]: /flash/cat9k_lite-rpbase.17.12.06.SPA.pkg

Do you want to remove the above files? [y/n]"""

CLEANUP_ACCEPTED = """
Deleting file flash:cat9k_lite-rpbase.17.12.06.SPA.pkg ... done.
SUCCESS: install_remove Wed Sep 09 10:47:20 UTC 2026"""

CLEANUP_DECLINED = """
 [1] Switch 1 Add succeed with reason: User Rejected Deletion
SUCCESS: install_remove Wed Sep 09 10:47:20 UTC 2026"""


class TestCleanup:
    def replies(self, device, second):
        seen = []

        def send_command(command, **kwargs):
            seen.append(command)
            return CLEANUP_PROMPT_OUTPUT if len(seen) == 1 else second

        device.send_command = send_command
        return seen

    def test_answers_the_confirmation_prompt_and_reports_what_went(self):
        device = FakeDevice()
        seen = self.replies(device, CLEANUP_ACCEPTED)
        out = install.cleanup(device)
        assert seen == ["install remove inactive", "y"]
        assert out.removed == (
            "/flash/cat9k_lite_iosxe.17.12.08.SPA.bin",
            "/flash/cat9k_lite-rpbase.17.12.06.SPA.pkg",
        )

    def test_a_declined_deletion_is_not_a_success(self):
        """The device prints `SUCCESS: install_remove` either way."""
        device = FakeDevice()
        self.replies(device, CLEANUP_DECLINED)
        with pytest.raises(install.InstallError, match="declined") as excinfo:
            install.cleanup(device)
        assert excinfo.value.status == "not_clean"

    def test_nothing_to_remove_does_not_wait_for_a_prompt(self):
        device = FakeDevice()
        seen = []

        def send_command(command, **kwargs):
            seen.append(command)
            return "install_remove: START\nSUCCESS: install_remove Wed Sep 09"

        device.send_command = send_command
        assert install.cleanup(device).removed == ()
        assert seen == ["install remove inactive"], "no answer sent, no second read"

    def test_no_success_is_a_failure(self):
        device = FakeDevice()
        device.send_command = lambda c, **kw: "FAILED: install_remove\n%Error: busy"
        with pytest.raises(install.InstallError) as excinfo:
            install.cleanup(device)
        assert excinfo.value.status == "not_clean"
