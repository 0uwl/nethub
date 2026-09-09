# NetHub — replacing Ansible with Netmiko

Plan for removing Ansible from NetHub entirely and moving the upgrade
logic into NetHub's own Python. Supersedes the Ansible half of
`design-document.md` §8/§8.1 and most of §9; the device-facing decisions
those sections rest on are unchanged and are listed below so nobody
re-derives them.

This is a plan not a description of committed code. Nothing here is
built yet.

## The decision

NetHub stops outsourcing device work to Ansible and talks to devices
directly with Netmiko. The playbooks, the task files, the rendered
inventory, the execution environment, and `ansible-runner` all go away.
The upgrade logic becomes ordinary Python in the `nethub/` package,
called by the sibling process.

The trade this accepts, stated up front because it is the real cost:
**the upgrade path stops being runnable without NetHub.** Today
`ansible/playbooks/*.yml` are deliberately standalone — CLAUDE.md forbids
even naming NetHub inside `ansible/` — and they are hand-invoked against
a real fleet right now. After this change, upgrading a device requires
NetHub to be running and healthy. That escape hatch is worth replacing
with something: at minimum a documented manual procedure, ideally a
small CLI entry point (`nethub-upgrade`) that drives the same Python
without the web layer.

## What this deletes

The Ansible layer is 795 lines of YAML across 8 files, but the module
surface is much smaller than that suggests. Of 83 task invocations, only
**17 touch a device**:

| Module | Uses | Replacement |
|---|---|---|
| `cisco.ios.ios_command` | 8 | `send_command` |
| `cisco.ios.ios_config` | 4 | `send_config_set` — one real config change (`ip scp server enable`) |
| `cisco.ios.ios_facts` | 2 | `show version` / `dir` + TextFSM (see "What gets harder") |
| `ansible.netcommon.net_put` | 1 | `netmiko.file_transfer(...)`, SCP |
| `ansible.netcommon.cli_command` | 1 | `send_command_timing` / `expect_string` |
| `ansible.builtin.wait_for_connection` | 1 | hand-rolled reconnect loop |

The other 66 — `set_fact` (27), `meta` (10), `debug` (10), `assert` (9),
`include_tasks` (8), `pause`/`fail`/`stat` (5) — are Ansible ceremony for
things Python has natively: variables, `if`, `raise`, `try/finally`,
`logging`. That is where four-fifths of the YAML goes.
`resolve_target_bundle.yml` is 90 lines of Jinja to `stat` a file and
compare two integers; it is roughly 25 lines of Python.

Also deleted: the rendered inventory layer (§3.5 — `hosts.yml`,
`upgrade_batch.yml`, `group_vars/`), the EE container image, the
`ansible-runner` integration, and ~24 MB of Ansible packages plus ~23 MB
of collections, against ~15 MB for Netmiko and ntc-templates.

**Estimated replacement: ~350–450 lines of Python for device-logic
parity, plus ~150–250 for phase/batch/result plumbing** — and most of
that second number is owed anyway, since §7.3 demands
`upgrade_host_phase_results` rows and a `failure_stage` enum regardless
of engine. Today that means parsing them back out of `job_events`; with
Netmiko the function returns a dict that gets inserted. Net line count
goes **down**.

## The registry store question — the biggest consequence

The `software_registry` YAML block exists so an Ansible playbook can read
it. With no playbook, nothing reads it, and the registry becomes purely
internal to NetHub — which is what `design-document.md` §5 always said it
should be (the `artifacts` table, with the file as a rendered projection).

That means the alpha slice built in `nethub/registry.py` and the
`Registry` model — `REGISTRIES_ROOT`, file adoption, `search_dir`, the
sibling-key-preserving save — loses its original purpose. Three options,
in order of preference:

1. **Move the registry into the database** (`artifacts`, §5) and drop the
   YAML store. Cleanest, matches the target design, and removes the whole
   class of hand-edited-file defenses currently in `registry.py`
   (path-escape guard, `secure_filename` revalidation on read, the
   check-registry drift report). Deletes real, working, tested code —
   which is the right call when the thing it defends against no longer
   exists.
2. **Keep the YAML as an export**, written by NetHub, read by nobody in
   NetHub. Only worth it if something outside NetHub still consumes it.
3. **Keep it as-is** and let NetHub manage group_vars for an Ansible
   setup it no longer uses itself. Rejected — this is the duplication
   this whole change exists to remove.

Recommendation: (1), but not in the same pass as the execution rewrite.
Get Netmiko working against the existing registry first, migrate the
store second, so only one thing is unproven at a time.

## What survives unchanged

None of this is Ansible-specific. It is device and trust-boundary
reality, and it carries over verbatim:

- **Transport is deployment-level, never request-level** (§4.3.1).
  `push_scp` default, `pull_sftp` alternative, and the reason is
  unchanged: selecting the transport selects whose credential is spent.
- **The SCP-server bracket** — capture prior state, enable only if
  needed, restore in a `finally:`, confirm by re-reading the
  running-config, fail the host outright if the restore is unconfirmed.
  `try/finally` expresses this better than `block/rescue/always` did.
- **Privilege 15 at login, no `enable` escalation, no `become`.**
- **Host-key pinning, fail-closed**, against `device_host_keys` (§4.3).
  Netmiko exposes this through Paramiko's host-key policy — set it
  explicitly, never `AutoAddPolicy`.
- **SHA-512 verified on the device** after transfer, in either direction
  — the third consumption of the ingest digest (§3.4). Match the digest
  itself, never the word "Verified".
- **The phase split and approval gates** (§8.1): pre-check / stage /
  activate / verify / cleanup, with the credential collected per phase.
- **The job row as the only control channel**, `registry_jobs`-style
  state machine, the startup sweep, `cancel_requested_at` (§7.3).
- **The §9.1 credential socket.** Flask collects the password at an
  approval gate; the sibling needs it. That is unchanged by the engine.
- **One Flask worker, more than one thread** (§3.2).

## What genuinely gets simpler

1. **§9's central security argument dissolves.** The sibling-vs-nested-
   container decision exists because mounting the Podman socket into
   Flask would let the process behind the only unauthenticated route
   start arbitrary containers. With no EE there is no Podman socket in
   the picture. The sibling still exists, but only for the mundane §3.2
   reason: don't occupy the process that owns the phone-home route.
2. **The "no secrets in the `private_data_dir`" hard rule disappears.**
   No `env/extravars`, no `env/passwords`, no `podman run -e` landing in
   `/proc/<pid>/cmdline`, no tmpfs `private_data_dir` to engineer and
   destroy, no scrubbing `stdout`/`job_events` before retention. The
   credential is a Python variable passed as a `ConnectHandler` kwarg and
   never touches a filesystem.
3. **The upgrade path becomes testable.** There are currently 92 tests
   covering the Flask side and **zero** covering the code that reloads
   production switches; CI runs `ansible-lint` and stops. Netmiko logic
   unit-tests against a mocked connection. For the most dangerous code in
   the system this is the strongest single argument in this document.
4. **"No user-supplied playbooks" stops being a rule and becomes a
   structural fact.**
5. **Logging is ours.** The design currently warns that a pull-transport
   phase must not run at `-vvv` because `no_log` does not redact
   connection-plugin debug output. That caveat goes away.

## What gets harder

- **Fact parsing.** `ios_facts` returns `net_filesystems_info`,
  `net_version`, `net_serialnum` structured and free, from a vendor-
  adjacent certified collection. Netmiko returns a string; ntc-templates
  (TextFSM, `use_textfsm=True`) covers `show version` and `dir`, but the
  templates are community-maintained and more version-fragile than
  `cisco.ios`. **To be validated on real hardware** before this is
  considered settled.
- **Post-reload reconnect.** `wait_for_connection` is free; a poll loop
  with backoff and a hard deadline is not hard to write but is exactly
  the code you do not want subtly wrong inside a maintenance window.
- **Command construction.** §8.1's character allowlist on submitted
  fields matters at least as much when building `send_command` strings by
  hand — there is no module argument handling between us and the CLI.

## Library choice: Netmiko alone, not Nornir

Nornir's value is its inventory model and its threaded runner. NetHub
already owns both: the inventory is the request document compiled against
the database (§3.5's argument survives even though its *output format*
does not), batching is `serial` in the phase model (§8.1), and results go
to `upgrade_host_phase_results` rows. Adding Nornir would mean
maintaining a second inventory representation to feed it.

Start with Netmiko plus `concurrent.futures.ThreadPoolExecutor` bounded
by the phase's serial setting. Revisit Nornir only if batching grows
complex enough to justify it.

## Proposed module layout

Start flat; split only when a file earns it.

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

## Dependencies

Add `netmiko` and `ntc-templates`. Remove `ansible`, `ansible-lint`, and
the `cisco.ios`/`ansible.netcommon` collections — including the
`ansible-lint` job and the collection install step in
`.github/workflows/ci.yml`.

## Rough build order

1. Spike `facts.py` and the reconnect loop against one real switch.
   These are the two genuinely uncertain pieces; resolve them before
   writing anything that depends on their shape.
2. `connection.py` with fail-closed host-key checking.
3. `transfer.py` — push adapter first (the default), with the SCP
   bracket and its confirmed restore. Pull adapter after.
4. `install.py`.
5. `phases.py` and the sibling that calls it, writing job/phase rows.
6. Delete `ansible/`, the EE references, and the CI Ansible steps.
7. Migrate the registry store to `artifacts` (see above) as its own pass.
8. Add the manual escape hatch — CLI entry point or documented procedure.

## Open questions

- Does anything outside NetHub still read the rendered
  `software_registry` YAML? Decides the registry-store option above.
- TextFSM parity for `dir` across the IOS-XE versions actually in the
  fleet — the free-space number gates the stage phase, so a parse miss
  is not cosmetic.
- Does `netmiko.file_transfer` behave acceptably pushing ~500 MB to a
  Cat9K-lite? The Ansible path needed `paramiko` specifically because
  `libssh` broke at image size; Netmiko is Paramiko-based, so this
  should be the better-tested direction, but it is unverified here.
