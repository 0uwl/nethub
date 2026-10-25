#!/usr/bin/env python3
"""Run nhtui against the test suite's fake switches: no hardware needed.

    uv run python scripts/fake_nhtui.py

Only the switches are fake (`tests/test_end_to_end.FakeSwitch`); the TUI,
`LocalRun` and the phase engine are the real ones. See docs/tui.md §2.

Three fake Catalyst 9200CXs at 192.0.2.10-12, running 17.12.06. The device
password is d3vice-pa55 for user jsmith. Set FAIL=192.0.2.11 to make that
switch unreachable for one connection, to see a partial phase and a retry.
"""
import hashlib
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nethub.devices import connection, install, transfer
from nethub.local_run import LocalRun, MemoryLedger, load_hosts
from nethub.tui import NetHubTUI, _summary
from tests.test_end_to_end import DEVICE_USER, IMAGE, IMAGE_BYTES, TARGET, Fleet, _scp_put

ADDRESSES = ('192.0.2.10', '192.0.2.11', '192.0.2.12')
fleet = Fleet(ADDRESSES)
connection.scan_host_key = fleet.scan
transfer._scp_put = _scp_put
if os.environ.get('FAIL'):
    fleet[os.environ['FAIL']].down_for = 1

image = os.path.join(tempfile.mkdtemp(), IMAGE)
with open(image, 'wb') as handle:
    handle.write(IMAGE_BYTES)
hosts = load_hosts('\n'.join(f'sw{i + 1:02}, {a}' for i, a in enumerate(ADDRESSES)),
                   ['192.0.2.0/24'], image=image,
                   sha512=hashlib.sha512(IMAGE_BYTES).hexdigest(), version=TARGET)
local = LocalRun(hosts, image=image, ledger=MemoryLedger(), username=DEVICE_USER,
                 concurrency=4, connect=fleet.connect,
                 reload_wait=install.ReloadWait(delay=0, interval=0, timeout=10))
NetHubTUI(local).run()
print(_summary(local))
for address in ADDRESSES:
    print(address, 'now runs', fleet[address].version)
