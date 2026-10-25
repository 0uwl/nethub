"""`nethub.local_run`: a whole run in memory, through the sibling's own engine.

The switches are `test_end_to_end`'s `FakeSwitch`, behind `connect`, so every
command goes through the real device layer. No app, no database.
"""

import hashlib
import subprocess
import sys

import pytest

from nethub.devices import install, transfer
from nethub.local_run import LocalRun, LocalRunError, MemoryLedger, load_hosts
from tests.test_end_to_end import (
    ADDRESS,
    ADDRESS2,
    DEVICE_PASS,
    DEVICE_USER,
    HOSTNAME,
    HOSTNAME2,
    IMAGE,
    IMAGE_BYTES,
    TARGET,
    Fleet,
    _scp_put,
)

CIDRS = ['192.0.2.0/24']
DIGEST = hashlib.sha512(IMAGE_BYTES).hexdigest()
HOSTS = f'{HOSTNAME}, {ADDRESS}\n{HOSTNAME2}, {ADDRESS2}'


@pytest.fixture
def fleet(monkeypatch):
    fleet = Fleet()
    monkeypatch.setattr(transfer, '_scp_put', _scp_put)
    return fleet


@pytest.fixture
def image(tmp_path):
    path = tmp_path / IMAGE
    path.write_bytes(IMAGE_BYTES)
    return str(path)


@pytest.fixture
def make_run(fleet, image):
    def _make(pinned=(ADDRESS, ADDRESS2), concurrency=4):
        hosts = load_hosts(HOSTS, CIDRS, image=image, sha512=DIGEST, version=TARGET)
        ledger = MemoryLedger(pins={a: fleet[a].key for a in pinned})
        return LocalRun(hosts, image=image, ledger=ledger, username=DEVICE_USER,
                        concurrency=concurrency, connect=fleet.connect,
                        reload_wait=install.ReloadWait(delay=0, interval=0, timeout=10))
    return _make


def states(local):
    return {h.hostname: h.state for h in local.run.hosts}


class TestSetup:
    def test_an_image_that_does_not_match_its_digest_is_refused(self, image):
        with pytest.raises(LocalRunError, match='does not match'):
            load_hosts(HOSTS, CIDRS, image=image, sha512='0' * 128, version=TARGET)

    @pytest.mark.parametrize('hosts,cidrs,message', [
        ('sw01, sw01.example.net', CIDRS, 'not an IP literal'),
        ('sw01, 198.51.100.7', CIDRS, 'outside the configured'),
        (HOSTS, [], 'No DEVICE_TARGET_CIDRS'),
    ])
    def test_targets_get_the_web_submit_checks(self, image, hosts, cidrs, message):
        with pytest.raises(LocalRunError, match=message):
            load_hosts(hosts, cidrs, image=image, sha512=DIGEST, version=TARGET)


class TestWholeRun:
    def test_every_phase_runs_and_the_run_completes(self, make_run, fleet):
        local = make_run()
        assert local.precheck(DEVICE_PASS) == 'succeeded'
        assert (local.run.state, local.run.awaiting_phase) == ('awaiting_approval', 'stage')
        assert local.approve('stage', DEVICE_PASS) == 'succeeded'
        assert local.approve('activate', DEVICE_PASS, reload_count='1') == 'succeeded'
        assert local.run.awaiting_phase == 'cleanup', 'verify chained after activate'
        assert local.approve('cleanup', DEVICE_PASS) == 'succeeded'

        assert local.run.state == 'completed'
        assert [j.phase for j in local.jobs] == [
            'precheck', 'stage', 'activate', 'verify', 'cleanup']
        for address in (ADDRESS, ADDRESS2):
            assert fleet[address].version == TARGET
            assert fleet[address].unexpected == []
            assert fleet[address].saved_with_scp == [False], 'the SCP bracket restored'

    def test_the_first_host_is_the_canary(self, make_run):
        local = make_run()
        local.precheck(DEVICE_PASS)
        local.approve('stage', DEVICE_PASS)
        local.approve('activate', DEVICE_PASS, reload_count='2')
        activated = [r['hostname'] for r in local.ledger.results if r['phase'] == 'activate']
        assert activated == [HOSTNAME, HOSTNAME2]

    def test_a_gate_must_be_the_one_the_run_is_at(self, make_run):
        local = make_run()
        local.precheck(DEVICE_PASS)
        with pytest.raises(LocalRunError, match='activate gate'):
            local.approve('activate', DEVICE_PASS)

    def test_no_password_is_kept_in_the_record(self, make_run):
        local = make_run()
        local.precheck(DEVICE_PASS)
        assert DEVICE_PASS not in repr(local.ledger.results)
        assert DEVICE_PASS not in repr(vars(local))


class TestOnePassword:
    def test_the_password_is_asked_once(self, make_run, fleet):
        local = make_run()
        assert local.needs_password
        local.precheck(DEVICE_PASS)
        assert not local.needs_password
        local.approve('stage')
        local.approve('activate')
        local.approve('cleanup')
        assert local.run.state == 'completed'
        assert all(fleet[a].version == TARGET for a in (ADDRESS, ADDRESS2))

    def test_a_refused_password_is_dropped_and_asked_for_again(self, make_run, fleet):
        local = make_run()
        local.precheck(DEVICE_PASS)
        assert local.approve('stage', 'typo') == 'failed'
        assert [login for a in (ADDRESS, ADDRESS2)
                for login in fleet[a].logins if login[1] == 'typo'] == [(DEVICE_USER, 'typo')]
        assert local.needs_password, 'a refused password is never offered again'
        with pytest.raises(LocalRunError, match='Enter the device password'):
            local.approve('stage')
        assert local.approve('stage', DEVICE_PASS) == 'succeeded'

    def test_a_refusal_at_precheck_ends_the_run(self, make_run):
        """Pre-check has no gate to return to, as in the web app; nothing has
        been changed on a device, so restarting is the recovery."""
        local = make_run()
        assert local.precheck('typo') == 'failed'
        assert (local.run.state, local.needs_password) == ('failed', True)

    def test_close_drops_the_password(self, make_run):
        local = make_run()
        local.precheck(DEVICE_PASS)
        local.close()
        assert local.needs_password


class TestFailures:
    def test_a_mistyped_password_is_tried_once_and_the_run_waits_at_the_gate(
            self, make_run, fleet):
        local = make_run()
        local.precheck(DEVICE_PASS)
        assert local.approve('stage', 'typo') == 'failed'
        wrong = [login for a in (ADDRESS, ADDRESS2)
                 for login in fleet[a].logins if login[1] == 'typo']
        assert wrong == [(DEVICE_USER, 'typo')]
        assert (local.run.state, local.run.awaiting_phase) == ('awaiting_approval', 'stage')
        assert local.approve('stage', DEVICE_PASS) == 'succeeded'
        assert states(local) == {HOSTNAME: 'staged', HOSTNAME2: 'staged'}

    def test_a_host_that_failed_stage_is_retried(self, make_run, fleet):
        local = make_run()
        local.precheck(DEVICE_PASS)
        fleet[ADDRESS2].down_for = 1
        assert local.approve('stage', DEVICE_PASS) == 'partial'
        assert states(local) == {HOSTNAME: 'staged', HOSTNAME2: 'failed'}
        assert local.retry('stage', DEVICE_PASS) == 'succeeded'
        assert states(local) == {HOSTNAME: 'staged', HOSTNAME2: 'staged'}
        assert local.run.awaiting_phase == 'activate'

    def test_an_address_with_no_pin_fails_that_host_alone(self, make_run, fleet):
        local = make_run(pinned=(ADDRESS,))
        assert local.precheck(DEVICE_PASS) == 'partial'
        failed = [r for r in local.ledger.results if r['hostname'] == HOSTNAME2]
        assert failed[0]['failure_stage'] == 'hostkey'
        assert fleet[ADDRESS2].logins == [], 'never connected'


class TestEnding:
    def test_cancel_at_a_gate_ends_the_run(self, make_run, fleet):
        local = make_run()
        local.precheck(DEVICE_PASS)
        local.cancel()
        assert local.run.state == 'cancelled'
        with pytest.raises(LocalRunError):
            local.approve('stage', DEVICE_PASS)
        assert fleet[ADDRESS].flash == {}

    def test_declining_cleanup_completes_the_run(self, make_run):
        local = make_run()
        local.precheck(DEVICE_PASS)
        local.approve('stage', DEVICE_PASS)
        local.approve('activate', DEVICE_PASS)
        local.decline_cleanup()
        assert local.run.state == 'completed'
        assert 'cleanup' not in [j.phase for j in local.jobs]


def test_the_standalone_path_loads_no_flask_or_sqlalchemy():
    """docs/tui.md [tui-imports-no-flask]: `nhtui` installs and runs apart
    from the web app."""
    code = ('import sys, nethub.local_run, nethub.tui; '
            'print(sorted({m.split(".")[0] for m in sys.modules} & '
            '{"flask", "flask_sqlalchemy", "flask_login", "sqlalchemy", "werkzeug", "alembic"}))')
    out = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True,
                         check=True).stdout
    assert out.strip() == '[]'

