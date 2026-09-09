"""Parsing checks for nethub.devices.facts against captured IOS-XE output.

These samples are hand-written and not yet confirmed against real hardware --
scripts/check_device_facts.py is how that happens. Replace them with real
captures once there are some.
"""

import pytest

from nethub.devices import facts

SHOW_VERSION = """Cisco IOS XE Software, Version 17.12.04
Cisco IOS Software [Dublin], Catalyst L3 Switch Software (CAT9K_LITE_IOSXE), \
Version 17.12.4, RELEASE SOFTWARE (fc2)
Technical Support: http://www.cisco.com/techsupport
Copyright (c) 1986-2024 by Cisco Systems, Inc.
Compiled Wed 24-Jul-24 03:07 by mcpre

ROM: IOS-XE ROMMON
BOOTLDR: System Bootstrap, Version 8.5(2r), RELEASE SOFTWARE (P)

sw01 uptime is 12 weeks, 3 days, 4 hours, 22 minutes
System returned to ROM by Reload Command
System restarted at 09:12:33 UTC Mon Jun 10 2024
System image file is "flash:cat9k_lite_iosxe.17.12.04.SPA.bin"
Last reload reason: Reload Command

Configuration register is 0x102
"""

DIR = """Directory of flash:/

11  drwx            4096  Mar 20 2024 10:22:41 +00:00  .installer
12  -rw-       471177027  Mar 20 2024 10:35:02 +00:00  cat9k_lite_iosxe.17.12.08.SPA.bin
13  -rw-            3096  Mar 20 2024 10:36:00 +00:00  vlan.dat

11353194496 bytes total (8129712128 bytes free)
"""


def test_parse_version():
    f = facts.parse_version(SHOW_VERSION)
    assert f.version == "17.12.4"
    assert f.hostname == "sw01"
    assert f.running_image == "cat9k_lite_iosxe.17.12.04.SPA.bin"


def test_parse_dir():
    fs = facts.parse_dir(DIR)
    assert fs.free_bytes == 8129712128
    assert fs.total_bytes == 11353194496
    assert fs.size_of("cat9k_lite_iosxe.17.12.08.SPA.bin") == 471177027
    assert fs.size_of("nope.bin") is None


def test_registry_and_device_version_formats_agree():
    """The registry writes 17.12.06, the device reports 17.12.6."""
    assert facts.same_version("17.12.06", "17.12.6")
    assert facts.same_version("17.09.4a", "17.9.4a")
    assert not facts.same_version("17.12.06", "17.12.08")


@pytest.mark.parametrize("output", ["", "% Invalid input detected at '^' marker.", "\n\n"])
def test_unparseable_output_raises_rather_than_defaulting(output):
    with pytest.raises(facts.FactsError):
        facts.parse_version(output)
    with pytest.raises(facts.FactsError):
        facts.parse_dir(output)


def test_dir_command_rejects_injection():
    assert facts.dir_command("bootflash:") == "dir bootflash:"
    with pytest.raises(ValueError):
        facts.dir_command("flash: | do something")
