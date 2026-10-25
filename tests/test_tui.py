"""`nethub.tui`: the gates and the pins, driven through Textual's pilot."""

import asyncio

from textual.widgets import Input

from nethub.devices import connection
from nethub.tui import ChoiceScreen, NetHubTUI
from tests.test_end_to_end import ADDRESS, ADDRESS2, DEVICE_PASS, TARGET
from tests.test_local_run import fleet, image, make_run  # noqa: F401 -- fixtures


def drive(app, scenario):
    async def run():
        async with app.run_test() as pilot:
            await scenario(app, pilot)
    asyncio.run(run())


async def settle(app, pilot):
    await app.workers.wait_for_complete()
    await pilot.pause()


async def pin_every_host(app, pilot):
    for row in range(len(app.local.run.hosts)):
        app.query_one('DataTable').move_cursor(row=row)
        await pilot.press('s')
        await settle(app, pilot)
        assert isinstance(app.screen, ChoiceScreen)
        await pilot.click('#pin')
        await pilot.pause()


def test_precheck_waits_for_every_pin_and_then_stops_at_the_stage_gate(
        make_run, fleet, monkeypatch):  # noqa: F811 -- fixtures
    monkeypatch.setattr(connection, 'scan_host_key', fleet.scan)
    app = NetHubTUI(make_run(pinned=()))

    async def scenario(app, pilot):
        await pilot.press('a')
        await pilot.pause()
        assert not isinstance(app.screen, ChoiceScreen), 'no run without confirmed pins'

        await pin_every_host(app, pilot)
        assert set(app.local.ledger.pins) == {ADDRESS, ADDRESS2}

        await pilot.press('a')
        await pilot.pause()
        app.screen.query_one('#password', Input).value = DEVICE_PASS
        await pilot.click('#precheck')
        await settle(app, pilot)
        run = app.local.run
        assert (run.state, run.awaiting_phase) == ('awaiting_approval', 'stage')

    drive(app, scenario)


def test_closing_the_scan_dialog_pins_nothing(make_run, fleet, monkeypatch):  # noqa: F811
    monkeypatch.setattr(connection, 'scan_host_key', fleet.scan)
    app = NetHubTUI(make_run(pinned=()))

    async def scenario(app, pilot):
        await pilot.press('s')
        await settle(app, pilot)
        await pilot.click('#close')
        await pilot.pause()
        assert app.local.ledger.pins == {}

    drive(app, scenario)


def test_quitting_while_a_phase_runs_is_refused(make_run, monkeypatch):  # noqa: F811
    app = NetHubTUI(make_run())
    exits = []

    async def scenario(app, pilot):
        monkeypatch.setattr(app, 'exit', lambda *a, **kw: exits.append(a))
        app.busy = True
        await pilot.press('q')
        assert exits == []
        app.busy = False
        await pilot.press('q')
        assert exits == [()]

    drive(app, scenario)


def test_a_whole_run_goes_through_every_gate_dialog(make_run, fleet):  # noqa: F811
    """Each gate's buttons are real widget ids, pressing them runs the phase, and
    only pre-check asks for the device password."""
    app = NetHubTUI(make_run())

    async def press(app, pilot, button):
        await pilot.press('a')
        await pilot.pause()
        fields = app.screen.query('#password')
        if button == 'precheck':
            fields.first(Input).value = DEVICE_PASS
        else:
            assert not fields, 'the password is asked for once, at pre-check'
        await pilot.click(f'#{button}')
        await settle(app, pilot)

    async def scenario(app, pilot):
        await press(app, pilot, 'precheck')
        await press(app, pilot, 'approve-stage')
        await press(app, pilot, 'approve-activate')
        assert app.local.run.awaiting_phase == 'cleanup', 'verify chained after activate'
        await press(app, pilot, 'approve-cleanup')
        assert app.local.run.state == 'completed'
        assert [j.phase for j in app.local.jobs] == [
            'precheck', 'stage', 'activate', 'verify', 'cleanup']

    drive(app, scenario)
    assert all(fleet[a].version == TARGET for a in (ADDRESS, ADDRESS2))


def test_verified_hosts_are_green_and_failed_ones_red(make_run):  # noqa: F811
    local = make_run()
    local.run.hosts[0].state, local.run.hosts[1].state = 'verified', 'failed'
    app = NetHubTUI(local)

    async def scenario(app, pilot):
        app.refresh_view()
        table = app.query_one('DataTable')
        assert {cell.style for cell in table.get_row_at(0)} == {'bold green'}
        assert {cell.style for cell in table.get_row_at(1)} == {'bold red'}

    drive(app, scenario)
