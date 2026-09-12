"""Parsing checks for nethub.devices.facts.

The primary samples are real captures under tests/captures/, taken with
scripts/check_device_facts.py against hardware. The synthetic samples below
cover shapes this hardware has not produced -- a BUNDLE-mode device, a
smaller flash -- and are labelled synthetic where they appear.
"""

from pathlib import Path

import pytest

from nethub.devices import facts

CAPTURES = Path(__file__).parent / "captures"
C9200CX = CAPTURES / "c9200cx-12p-2x2g-17.12.06"


def capture(name: str) -> str:
    return (C9200CX / name).read_text()


# Synthetic: no BUNDLE-mode device has been captured. Trimmed to the lines the
# template reads, so it is obvious this is not a real capture.
BUNDLE_SHOW_VERSION = """Cisco IOS XE Software, Version 17.12.04
Cisco IOS Software [Dublin], Catalyst L3 Switch Software (CAT9K_LITE_IOSXE), \
Version 17.12.4, RELEASE SOFTWARE (fc2)
Copyright (c) 1986-2024 by Cisco Systems, Inc.

ROM: IOS-XE ROMMON

sw01 uptime is 12 weeks, 3 days, 4 hours, 22 minutes
System returned to ROM by Reload Command
System image file is "flash:cat9k_lite_iosxe.17.12.04.SPA.bin"
Last reload reason: Reload Command

Configuration register is 0x102
"""

# Synthetic: a small flash with one image and one directory.
SMALL_DIR = """Directory of flash:/

11  drwx            4096  Mar 20 2024 10:22:41 +00:00  .installer
12  -rw-       471177027  Mar 20 2024 10:35:02 +00:00  cat9k_lite_iosxe.17.12.08.SPA.bin
13  -rw-            3096  Mar 20 2024 10:36:00 +00:00  vlan.dat

11353194496 bytes total (8129712128 bytes free)
"""


class TestRealCapture:
    def test_parse_version(self):
        f = facts.parse_version(capture("show_version.txt"))
        assert f.version == "17.12.6"
        assert f.hostname == "Switch"
        assert f.model == "C9200CX-12P-2X2G"
        assert f.serial == "FJC283114LN"
        assert f.running_image == "packages.conf"
        assert f.boot_mode == "INSTALL"

    def test_parse_dir(self):
        fs = facts.parse_dir(capture("dir.txt"))
        assert fs.name == "flash:/"
        assert fs.total_bytes == 4967505920
        assert fs.free_bytes == 3723296768
        assert fs.size_of("cat9k_lite-rpbase.17.12.06.SPA.pkg") == 408739840
        assert fs.size_of("packages.conf") == 4905

    def test_dir_omits_directories(self):
        """`core` is a directory on this device; size_of answers about files."""
        assert "core" in capture("dir.txt")
        assert facts.parse_dir(capture("dir.txt")).size_of("core") is None

    def test_parse_privilege(self):
        assert facts.parse_privilege(capture("show_privilege.txt")) == 15

    def test_device_version_matches_the_registry_spelling(self):
        """The registry writes 17.12.06; this device reports 17.12.6."""
        f = facts.parse_version(capture("show_version.txt"))
        assert facts.same_version(f.version, "17.12.06")


def test_bundle_mode_is_distinguished_from_install():
    f = facts.parse_version(BUNDLE_SHOW_VERSION)
    assert f.running_image == "cat9k_lite_iosxe.17.12.04.SPA.bin"
    assert f.boot_mode == "BUNDLE"


def test_unknown_boot_mode_is_empty_not_bundle():
    """install add/activate/commit must refuse, not guess, on an odd boot file."""
    assert facts._boot_mode("") == ""
    assert facts._boot_mode("flash:something-unexpected") == ""


def test_parse_dir_synthetic():
    fs = facts.parse_dir(SMALL_DIR)
    assert fs.free_bytes == 8129712128
    assert fs.total_bytes == 11353194496
    assert fs.size_of("cat9k_lite_iosxe.17.12.08.SPA.bin") == 471177027
    assert fs.size_of("nope.bin") is None


def test_registry_and_device_version_formats_agree():
    assert facts.same_version("17.12.06", "17.12.6")
    assert facts.same_version("17.09.4a", "17.9.4a")
    assert not facts.same_version("17.12.06", "17.12.08")


@pytest.mark.parametrize("version", ["", "  ", "17..12", "17.12.x"])
def test_unparseable_version_raises(version):
    with pytest.raises(facts.FactsError):
        facts.version_tuple(version)


@pytest.mark.parametrize("output", ["", "% Invalid input detected at '^' marker.", "\n\n"])
def test_unparseable_output_raises_rather_than_defaulting(output):
    with pytest.raises(facts.FactsError):
        facts.parse_version(output)
    with pytest.raises(facts.FactsError):
        facts.parse_dir(output)
    with pytest.raises(facts.FactsError):
        facts.parse_privilege(output)


def test_dir_command_rejects_injection():
    assert facts.dir_command("bootflash:") == "dir bootflash:"
    with pytest.raises(ValueError):
        facts.dir_command("flash: | do something")


class FakeConn:
    """Netmiko's send_command, enough of it to check what we send and read."""

    def __init__(self, replies):
        self.replies = replies
        self.calls = []

    def send_command(self, command_string, **kwargs):
        self.calls.append((command_string, kwargs))
        return self.replies[command_string]


def test_getters_send_the_expected_commands():
    conn = FakeConn(
        {
            "show version": capture("show_version.txt"),
            "dir flash:": capture("dir.txt"),
            "show privilege": capture("show_privilege.txt"),
        }
    )
    assert facts.get_facts(conn).version == "17.12.6"
    assert facts.get_filesystem(conn).free_bytes == 3723296768
    assert facts.get_privilege(conn) == 15
    assert [c[0] for c in conn.calls] == ["show version", "dir flash:", "show privilege"]


def test_dir_is_given_a_timeout_longer_than_netmikos_default():
    """A multi-gigabyte flash listing outruns the ~10s default."""
    conn = FakeConn({"dir flash:": capture("dir.txt")})
    facts.get_filesystem(conn)
    assert conn.calls[0][1]["read_timeout"] == facts.DIR_READ_TIMEOUT
    assert facts.DIR_READ_TIMEOUT >= 60
