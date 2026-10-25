# nhtui
Code: `nethub/tui.py`, `nethub/local_run.py`, `nethub/flow.py`, `nethub/devices/phases.py` (`Ledger`, `execute_phase`), `nethub/vocab.py`, `nethub/targets.py`, `pyproject.toml` (`[project.scripts]`, the `tui` extra), `scripts/fake_nhtui.py` · Tests: `tests/test_local_run.py`, `tests/test_tui.py`

---

## AI READING INSTRUCTION

Read `[SPEC]` and `[BUG]` blocks for authoritative facts.
Read `[NOTE]` only if additional context is needed.
`[?]` blocks are unverified; treat them with lower confidence.
§1 holds the rules, each H3 heading being a rule's slug. §2 says how it
works, §3 decisions, §4 known gaps.

`nhtui` is a standalone upgrade driver: one run, in the foreground, from a
terminal, with no web app, no sibling and no database. It is not the
`flask artifacts|hostkeys` CLI (cli.md), which is a front end on a running
NetHub.

---

## 1. Rules

### 1.1 [tui-shares-the-phase-engine]
**[SPEC]**
`LocalRun` runs every phase through `phases.execute_phase` and decides
what follows with `flow.next_step`, the same functions the sibling uses,
over in-memory `Run`/`Job`/`Host` objects and a `MemoryLedger`. The
login gate, the canary, the wave stops and an activated host never being
kept therefore hold here by construction; never reimplement any of them
in `local_run.py` or `tui.py`.
Pinned by: `tests/test_local_run.py::TestWholeRun::test_every_phase_runs_and_the_run_completes`, `tests/test_local_run.py::TestWholeRun::test_the_first_host_is_the_canary`, `tests/test_local_run.py::TestFailures::test_a_mistyped_password_is_tried_once_and_the_run_waits_at_the_gate`, `tests/test_local_run.py::TestFailures::test_a_host_that_failed_stage_is_retried`

**[NOTE]**
A second implementation of the engine would drift from the one that was
validated on hardware. Keeping `execute_phase` database-free, behind a
`Ledger` (`add_result`, `commit`, `pin_for`), is what makes sharing it
possible; `sibling.SqlLedger` is the database ledger.

### 1.2 [tui-is-the-dispatcher]
**[SPEC]**
The `nhtui` process runs the phases itself, in a Textual thread worker,
and there is no queue. Quitting is refused while a phase runs: the
operator cancels, which stops new hosts, and the running hosts finish.
Pinned by: `tests/test_tui.py::test_quitting_while_a_phase_runs_is_refused`, `tests/test_tui.py::test_a_whole_run_goes_through_every_gate_dialog`, `tests/test_local_run.py::TestEnding::test_cancel_at_a_gate_ends_the_run`

**[NOTE]**
There is no safe place to stop inside an activation. A dropped SSH
session still kills the process, so the TUI warns when it is not inside
`tmux` or `screen`.

### 1.3 [tui-stores-nothing]
**[SPEC]**
`nhtui` writes no file and no database row. The one exception is
`--report FILE`, which is written at quit when the operator asks for it.
The device password is collected at each gate, as in the web app. It
lives only in that phase's `PhaseContext`, never in a result, the run
object or the report.
Pinned by: `tests/test_local_run.py::TestWholeRun::test_no_password_is_kept_in_the_record`

**[NOTE]**
A run made here is outside NetHub's audit trail, and the TUI and its
summary say so. Writing NetHub's tables from here would be the third
writer device-layer.md §7 rejects.

### 1.4 [tui-never-tofu]
**[SPEC]**
A pin comes only from `--pins FILE` or from a scan the operator confirms
in a dialog that shows the fingerprint. Pre-check refuses to start until
every address has a pin, and an address without one fails as `hostkey`
without a connection.
Pinned by: `tests/test_tui.py::test_precheck_waits_for_every_pin_and_then_stops_at_the_stage_gate`, `tests/test_tui.py::test_closing_the_scan_dialog_pins_nothing`, `tests/test_local_run.py::TestFailures::test_an_address_with_no_pin_fails_that_host_alone`

### 1.5 [tui-targets-get-submit-checks]
**[SPEC]**
`load_hosts` applies `targets.parse_hosts` and `targets.check_target`, as
a web submit does: IP literals inside `--cidr`/`DEVICE_TARGET_CIDRS`
only, and an empty list refuses every address. The image's SHA-512 must
equal the one claimed, as an upload's must.
Pinned by: `tests/test_local_run.py::TestSetup::test_targets_get_the_web_submit_checks`, `tests/test_local_run.py::TestSetup::test_an_image_that_does_not_match_its_digest_is_refused`

### 1.6 [tui-imports-no-flask]
**[SPEC]**
Importing `nethub.tui` or `nethub.local_run` loads no Flask, SQLAlchemy,
Werkzeug or Alembic, so `nhtui` runs with no web configuration at all.
Keep `nethub/__init__.py`, `devices/`, `flow.py`, `vocab.py` and
`targets.py` free of those imports.
Pinned by: `tests/test_local_run.py::test_the_standalone_path_loads_no_flask_or_sqlalchemy`

---

## 2. How it works

**[SPEC]**
```
uv tool install 'nethub[tui] @ git+https://github.com/0uwl/nethub@<tag>'
nhtui --hosts hosts.txt --image cat9k_lite_iosxe.17.12.08.SPA.bin \
      --sha512 <hex> --version 17.12.08 --cidr 10.0.0.0/8 [--pins pins.txt] \
      [--user jsmith] [--concurrency 4] [--report run.json]
```
- `hosts.txt` has `hostname, address` per line, the canary first.
- `pins.txt` has `address key-type SHA256:...` per line.
- Keys: `s` scans the selected host's key, `a` takes the next step
  (pre-check, or the current gate's approve, retry or decline), `x`
  cancels, `q` quits.
- The gates and their consequence text match the run page: pre-check,
  stage, activate (with the reload count, capped at `--concurrency`),
  then cleanup, with verify chained after activate. Retry of failed
  hosts is offered at each gate (`flow.retryable_phases`).
- The image is pushed from its own directory (`search_dir`). Stage still
  re-hashes it first (`phases.check_source`).
- Exit status: 0 if the run completed, 1 otherwise, 2 for a bad setup.
- `scripts/fake_nhtui.py` starts the real TUI against three of the test
  suite's fake switches (`192.0.2.10`-`12`, user `jsmith`, password
  `d3vice-pa55`), for trying it by hand with no hardware. `FAIL=<address>`
  makes that switch unreachable for one connection.

---

## 3. Decisions

**[NOTE]**
- **No database, not even an optional one.** The tool is for when there
  is no NetHub server, so nothing it does depends on one. Pins come from
  the session or a file instead.
- **Setup is command-line arguments, not a form.** A run can then be
  prepared and repeated from shell history. Add a form if operators ask
  for one.
- **One package, with Textual as an extra.** `uv tool install` also
  installs the web dependencies, but nothing imports them (rule 1.6).
  Split them into a `server` extra if the install size matters.
- **No deadlines, no scheduling, no two-person rules.** Someone is at the
  terminal. A scheduled or second-person run is the web app's job.

---

## 4. Known gaps

**[SPEC]**
- `nhtui` has not been run against hardware. Only the engine it shares
  has (device-layer.md §6).
- There is no non-interactive mode: every gate needs someone at the
  terminal.
