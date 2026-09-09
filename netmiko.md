# NetHub — replacing Ansible with Netmiko

**Status: build order steps 1–5 done, 6–8 not started. Handoff document
for a fresh session.**

This file exists so a new Claude Code session can pick up a decided-but-
unstarted migration without re-deriving it. It records what was
investigated, what was decided (and by whom), what is deliberately still
open, and — most importantly — **which of `CLAUDE.md`'s hard rules this
plan supersedes**, since `CLAUDE.md` loads automatically into every
session and currently reads as though Ansible is permanent.

Read in this order: this file → `CLAUDE.md` "Hard rules" (with the
supersession table below in hand) → `design-document.md` §8/§8.1 and §9
only if you need the reasoning behind a specific rule.

## Where things stand

- Branch: `netmiko`, forked from `alpha` at `267217c`. Build order steps
  1–5 are committed: `nethub/devices/*.py`, the upgrade schema in
  `nethub/models.py`, `nethub/sibling.py` and `nethub/credential_socket.py`,
  with tests. Steps 6–8 are untouched, so `ansible/` is still in the tree.
- **Step 5 has a missing half, and it is route work rather than device
  work.** Nothing creates a job row: there is no submit route, no approval
  route, and nothing puts a credential into the store. The sibling works a
  queue only a test fills, so the whole path is tested but has never run
  against a device. Closing it needs §6.1's API surface and §8.1's approval
  gates — decide whether that is step 5's remainder or a step of its own.
- `facts.py`, `connection.py` and `transfer.py` have been exercised against
  a real Catalyst 9200CX on IOS-XE 17.12.06, including a 408 MB SCP push
  through the full enable/restore bracket. `tests/captures/` holds the
  verbatim output the parser tests run against.
- **`install.py` is validated end-to-end, and step 1 is now fully closed.**
  A round trip was run on the lab switch on 2026-09-09 — 17.12.6 → 17.12.08
  → 17.12.6 — through `stage_image` → `activate` → `wait_for_device` →
  `verify_upgrade` → `cleanup`, in both directions. Step 1's outstanding half
  (spike the reconnect loop against a real switch) is covered by that.
  Measured: stage ~370s for 471 MB, install 605–622s, reload 228–238s
  against a 900s deadline, cleanup ~5s. Still untested: a switch stack, and
  any device slower than this one.
- The Flask side is real and tested: 92 of the suite's tests cover
  `nethub/{auth,bootstrap,credentials,models,registry,registry_routes}.py`;
  the device layer adds the rest. `pytest -q` from the repo root, venv at
  `.venv`.
- The Ansible side is hand-invoked scaffolding: 795 lines of YAML in
  `ansible/playbooks/` (2 playbooks + 6 task files) plus a design-sketch
  inventory in `ansible/inventory/`. **Zero test coverage** — CI runs
  `ansible-lint` and nothing else against it.
- `requirements.txt` does not contain Ansible at all. Ansible enters only
  through CI (`.github/workflows/ci.yml` lines 19–30) and the local venv.
- The `ansible/` tree was restructured recently (playbooks moved under
  `ansible/playbooks/`, `ansible/inventory/rendered/` flattened into
  `ansible/inventory/`). Paths in older docs may be stale; trust `find
  ansible -type f`.

## The decision (settled — do not relitigate)

The maintainer decided, explicitly:

1. **Remove all traces of Ansible from NetHub.** Playbooks, task files,
   inventory, `group_vars`, EE, `ansible-runner`. Device work moves into
   ordinary Python in the `nethub/` package, driven by Netmiko.
2. **`group_vars` goes too.** This is not just an execution-engine swap —
   the YAML registry store is in scope. See "The registry store" below,
   because it has a consequence that is easy to miss.
3. **Netmiko alone, not Nornir.** Rationale in "Library choice" below.
4. **TextFSM/ntc-templates for fact parsing is accepted**, with the
   maintainer validating it against real hardware. Treat the parser
   choice as made; treat *specific template output* as unverified until
   that validation lands.

The trade being accepted, stated because it is a real cost and a future
session should not be surprised by it: **the upgrade path stops being
runnable without NetHub.** Today `ansible/playbooks/*.yml` are
deliberately standalone (`CLAUDE.md` forbids even naming NetHub inside
`ansible/`) and are run by hand against a real fleet. Afterwards,
upgrading a device requires NetHub healthy. Build order step 8 replaces
that escape hatch; don't drop it.

## CLAUDE.md rules this supersedes

`CLAUDE.md` is authoritative for the repo and is loaded into every
session, but it predates this decision. Work through it with this table.
**Nothing here weakens a security property** — the rules that dissolve
are ones whose *mechanism* disappears, and the properties they protected
are re-listed under "What survives" with their new implementation.

| CLAUDE.md rule | Fate |
|---|---|
| "No user-supplied playbooks" | **Dissolves as a rule, survives as a fact.** There is no playbook to supply. The closed-set property becomes structural. |
| "No user-supplied inventories, and no user-supplied Jinja" | **Mechanism dissolves, intent survives.** No inventory, no Jinja. The request document is still validated and compiled — now into Python objects, not YAML. |
| "No user-settable connection vars — except `ansible_host`" | **Survives, renamed.** `ansible_host` becomes an ordinary target-address field; the CIDR check and fail-closed host-key check are unchanged and still required. |
| "No EE invocation from the Flask process" | **Half dissolves.** No EE, no Podman socket — that threat model goes. The sibling still owns dispatch, the job row is still the only control channel, and the §9.1 credential socket still exists. |
| "No secrets in the `private_data_dir`" | **Fully dissolves.** No `private_data_dir`. The credential becomes a Python variable passed to `ConnectHandler`. |
| "Day-2 transfer runs in whichever direction `image_transport` says" | **Direction survives; module detail replaced.** Everything about `net_put`/`paramiko`/`cli_command` and `tasks/*.yml` paths is obsolete. |
| Entire "Ansible playbook notes" section + "Planned improvements" | **Superseded wholesale**, including "never name NetHub inside `ansible/`" and "shared logic lives in `ansible/playbooks/tasks/`". |
| `ansible-lint` in the Commands block | **Removed** along with `.ansible-lint`. |

Unaffected and still binding: one Flask worker with threads; `DEBUG` off;
one host / separate Quadlet units; no shared service account; the
SCP-server bracket rule; transport is deployment-level and its credential
never lives in `settings`; NetHub is the sole source of image bytes; push
is the default; all §7.3 job-state rules.

**Update `CLAUDE.md` in the same pass as the code**, per its own
"Keeping this file current" section. That is a large edit and should not
be deferred to the end — a half-migrated `CLAUDE.md` actively misleads
the next session.

## What the investigation found

Re-derive any of this rather than trusting it:

```bash
cat ansible/playbooks/*.yml ansible/playbooks/tasks/*.yml | wc -l   # 795
grep -ohE '^\s+(ansible\.builtin\.[a-z_]+|ansible\.netcommon\.[a-z_]+|cisco\.ios\.[a-z_]+):' \
  ansible/playbooks/*.yml ansible/playbooks/tasks/*.yml | sed 's/[: ]//g' | sort | uniq -c | sort -rn
```

Of 83 task invocations, **only 17 touch a device**:

| Module | Uses | Replacement |
|---|---|---|
| `cisco.ios.ios_command` | 8 | `send_command` |
| `cisco.ios.ios_config` | 4 | `send_config_set` — one real config change (`ip scp server enable`) |
| `cisco.ios.ios_facts` | 2 | `show version` / `dir` + TextFSM |
| `ansible.netcommon.net_put` | 1 | `netmiko.file_transfer(...)`, SCP |
| `ansible.netcommon.cli_command` | 1 | `send_command_timing` / `expect_string` |
| `ansible.builtin.wait_for_connection` | 1 | hand-rolled reconnect loop |

The other 66 — `set_fact` (27), `meta` (10), `debug` (10), `assert` (9),
`include_tasks` (8), `pause`/`fail`/`stat` (5) — are ceremony for things
Python has natively: variables, `if`, `raise`, `try/finally`, `logging`.
That is where four-fifths of the YAML goes.
`tasks/resolve_target_bundle.yml` is 90 lines of Jinja to `stat` a file
and compare two integers; roughly 25 lines of Python.

**Estimated replacement: ~350–450 lines of Python for device-logic
parity, plus ~150–250 for phase/batch/result plumbing** — and most of the
second number is owed regardless of engine, since §7.3 demands
`upgrade_host_phase_results` rows and a `failure_stage` enum. Today that
means parsing them back out of `job_events`; with Netmiko the function
returns a dict that gets inserted. **Net line count goes down.**

Also deleted: the rendered inventory layer, the EE container image, the
`ansible-runner` integration, and ~24 MB of Ansible packages plus ~23 MB
of collections, against ~15 MB for Netmiko and ntc-templates.

## The registry store — the consequence most easily missed

The `software_registry` YAML block exists **so an Ansible playbook can
read it**. With no playbook, nothing in NetHub reads it.

That means the multi-registry alpha — `nethub/registry.py`, the
`Registry` model, `REGISTRIES_ROOT`, file adoption, `search_dir`, the
sibling-key-preserving atomic save, the path-escape guard, the
`secure_filename` revalidation on read, `check_registry()`'s drift
report, and a large share of those 92 tests — **loses its reason to
exist.** It was built to manage an Ansible setup NetHub will no longer
use.

Decision: fold it into the `artifacts` table (`design-document.md` §5),
which is what the target design always said. This deletes real, working,
tested code, and that is correct when the thing it defended against
(hand-edited YAML owned by someone else) no longer exists.

**Sequencing recommendation: do this as its own pass, after the execution
rewrite works.** Get Netmiko driving upgrades off the existing registry
first, then migrate the store, so only one thing is unproven at a time.
The build order below reflects that.

## What survives unchanged

Device and trust-boundary reality, not Ansible artifacts:

- **Transport is deployment-level, never request-level** (§4.3.1).
  `push_scp` default, `pull_sftp` alternative — selecting the transport
  selects whose credential is spent.
- **The SCP-server bracket** — capture prior state, enable only if
  needed, restore in a `finally:`, confirm by re-reading the
  running-config, fail the host outright if the restore is unconfirmed.
  `try/finally` expresses this better than `block/rescue/always` did.
  Note the known gap survives too: a killed process never reaches
  `finally` either.
- **Privilege 15 at login, no `enable` escalation, no `become`.**
- **Host-key pinning, fail-closed**, against `device_host_keys` (§4.3).
  Netmiko exposes this through Paramiko's host-key policy — set it
  explicitly, never `AutoAddPolicy`.
- **SHA-512 verified on the device** after transfer, either direction —
  the third consumption of the ingest digest (§3.4). Match the digest
  itself, never the word "Verified".
- **The phase split and approval gates** (§8.1): pre-check / stage /
  activate / verify / cleanup, credential collected per phase.
- **The job row as the only control channel**, the state machine, the
  startup sweep, `cancel_requested_at` (§7.3).
- **The §9.1 credential socket.** Flask collects the password at an
  approval gate; the sibling needs it. Unchanged by the engine.
- **One Flask worker, more than one thread** (§3.2).

## What genuinely gets simpler

1. **§9's central security argument dissolves.** The sibling-vs-nested-
   container decision exists because mounting the Podman socket into
   Flask would let the process behind the only unauthenticated route
   start arbitrary containers. No EE, no Podman socket. The sibling still
   exists, but for the mundane §3.2 reason: don't occupy the process that
   owns the phone-home route.
2. **The `private_data_dir` hygiene problem disappears.** No
   `env/extravars`, no `env/passwords`, no `podman run -e` in
   `/proc/<pid>/cmdline`, no tmpfs directory to engineer and destroy, no
   scrubbing `stdout`/`job_events` before retention.
3. **The upgrade path becomes testable.** 92 tests cover Flask; **zero**
   cover the code that reloads production switches. Netmiko logic
   unit-tests against a mocked connection. For the most dangerous code in
   the system this is the strongest single argument in this document —
   and the reason step 1 of the build order is a spike, not a rewrite.
4. **Logging is ours.** The design currently warns a pull-transport phase
   must not run at `-vvv` because `no_log` doesn't redact
   connection-plugin debug output. That caveat goes away.

## What gets harder

- **Fact parsing.** `ios_facts` returns `net_filesystems_info`,
  `net_version`, `net_serialnum` structured and free from a certified
  collection. Netmiko returns a string; ntc-templates (`use_textfsm=True`)
  covers `show version` and `dir`, but templates are community-maintained
  and more version-fragile. **Decision accepted; hardware validation
  pending with the maintainer.** The free-space number from `dir` gates
  the stage phase, so a parse miss is not cosmetic — fail loudly on an
  unparseable result rather than defaulting.
- **Post-reload reconnect.** `wait_for_connection` is free; a poll loop
  with backoff and a hard deadline is not hard to write but is exactly
  the code you do not want subtly wrong inside a maintenance window.
- **Command construction.** §8.1's character allowlist on submitted
  fields matters at least as much when building `send_command` strings by
  hand — there is no module argument handling between us and the CLI.

## Library choice: Netmiko alone, not Nornir

Nornir's value is its inventory model and threaded runner. NetHub already
owns both: inventory is the request document compiled against the
database, batching is `serial` in the phase model (§8.1), results go to
`upgrade_host_phase_results` rows. Adding Nornir means maintaining a
second inventory representation to feed it.

Start with Netmiko plus `concurrent.futures.ThreadPoolExecutor` bounded
by the phase's serial setting. Revisit only if batching grows complex
enough to justify it.

## Proposed module layout

Start flat; split only when a file earns it (the repo's YAGNI norm).

```
nethub/devices/
  connection.py   -- ConnectHandler factory: host-key policy, timeouts, credential
  facts.py        -- show version / dir via TextFSM
  transfer.py     -- push_scp and pull_sftp adapters, plus the shared verify /sha512
  install.py      -- write memory, install add/activate/commit, reconnect, cleanup
  phases.py       -- pre-check / stage / activate / verify / cleanup, results to rows
```

`errors.py` (exception → `failure_stage` enum) folds into `phases.py`
until it doesn't fit.

## Build order

1. **Spike `facts.py` and the reconnect loop against one real switch.**
   The two genuinely uncertain pieces; resolve before writing anything
   that depends on their shape. Throwaway script, not production code.
2. `connection.py` with fail-closed host-key checking.
3. `transfer.py` — push adapter first (the default), with the SCP bracket
   and its confirmed restore. Pull adapter after.
4. `install.py`.
5. `phases.py` plus the sibling that calls it, writing job/phase rows.
6. Delete `ansible/`; remove `ansible`/`ansible-lint` from CI
   (`.github/workflows/ci.yml` lines 19–30) and delete `.ansible-lint`;
   drop the Ansible references in `.yamllint.yaml`'s comments; add
   `netmiko` and `ntc-templates` to `requirements.txt`. **Rewrite
   `CLAUDE.md`'s Ansible sections in this same pass.**
7. Migrate the registry store to `artifacts` as its own pass.
8. Restore the manual escape hatch — CLI entry point (`nethub-upgrade`)
   or a documented procedure.

## Open questions

- Does anything **outside** NetHub still read the rendered
  `software_registry` YAML? If yes, step 7 needs an export path rather
  than a deletion. Ask the maintainer before step 7, not before step 1.
- TextFSM parity for `dir` across the IOS-XE versions actually in the
  fleet (validation pending with the maintainer).
- **Resolved (2026-09-09): SCP at image size works, but it is slow.**
  408,739,840 bytes pushed to a Catalyst 9200CX with `transfer.py`'s push
  adapter completed in 319.9s end-to-end, restore confirmed, digest
  verified. Subtracting the `verify /sha512` pass (33.9s, measured
  separately on a file of the same size) puts the transfer itself at
  roughly 280s — about **1.4 MB/s**. No connection break of the kind
  `libssh` produced under Ansible; the direction is sound.

  Two consequences. A full image is ~15 min per device at 1.2 GB, which
  is what §8's per-host stage bound has to be calibrated against — it is
  minutes, not seconds, and a bound derived from a guess will be wrong.
  And the rate is device-bound rather than link-bound, which cuts in
  favour of §8.1's plan to drop the serial gate from staging: twenty
  devices at 1.4 MB/s each is ~28 MB/s off NetHub's link, comfortably
  within the concurrency cap that section calls for. The 500 MB figure
  in the original question was the right order of magnitude; what was
  unknown was the rate, not the feasibility.

  Not answered: the same test on a device across a constrained WAN link,
  where the bottleneck moves and the transfer stops being device-bound.
- The design doc's own §8/§8.1/§9 still describe an EE. They are not
  updated by this plan. Decide whether `design-document.md` gets revised
  or whether this file stands as its acknowledged supersession.
