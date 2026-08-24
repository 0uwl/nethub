# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project status

NetHub is in early bootstrap. `app.py` currently only serves a static
homepage and error pages — none of the Provisioning or Software Lifecycle
functionality described in `README.md` exists yet. The `ansible/` playbook
references roles (`iosxe_facts`, `batch_summary`) that are not yet present
in this repo. Treat `README.md` as the design document / target
architecture, not a description of current code — always verify a
described component actually exists before assuming it's implemented.

## Commands

```bash
pip install -r requirements.txt   # Flask, gunicorn — no dev/test deps yet

export SECRET_KEY=<any-string>    # required; app.py raises ValueError without it
python app.py                     # runs the dev server (config.py sets DEBUG = True)
```

There are no tests, linter config, or CI in this repo yet.

The Ansible playbook (`ansible/upgrade_iosxe.yml`) is invoked separately,
against a network device inventory (not present in this repo yet):
```bash
ansible-playbook ansible/upgrade_iosxe.yml -e upgrade_serial=1
```
It targets Cisco IOS-XE devices (`cisco.ios` collection, `network_cli`
connection) and expects each host to define an `image_bundle` var
(filename, sha512, version, remote_dir, file_size) — see §5/§8 of
`README.md`. `upgrade_serial` controls how many hosts upgrade per wave
(defaults to 1); with `serial > 1`, hosts in the same wave share this
terminal's stdin for the interactive `pause` prompts, so they can interleave.

## Architecture (target design — see README.md for full detail)

NetHub unifies two device-lifecycle halves in **one Flask backend, one
database, one admin frontend**:

- **Provisioning (day-0)**: unauthenticated phone-home endpoint — a new
  device is checked against a serial allowlist and receives its initial
  config/image over plain HTTP. Plain HTTP is a deliberate choice, not an
  oversight (README §4): a first-contact device has no trust anchor to
  validate TLS against, so the real protection comes from VLAN isolation,
  serial allowlisting, and payload hash verification instead.
- **Software Lifecycle (day-2)**: authenticated admin flow for onboarding
  IOS-XE images — upload → SHA-512 verify → staged publish → registry
  update → per-job Ansible EE invocation (via `ansible-runner`) that
  triggers `upgrade_iosxe.yml` — → audit trail. Every route in this module
  requires an authenticated session; the phone-home route is the system's
  only unauthenticated entry point.

**Single artifact pipeline, two egress adapters** (README §3.4): one
ingest path (upload → hash → size → store → record) feeds both days —
day-0 devices pull over HTTP, day-2 devices pull over SCP (credentialed,
via the vaulted Ansible inventory). One `artifacts` table backs both; a
`kind` discriminator (script/config/image) distinguishes rows rather than
splitting into separate tables. The SHA-512 is computed once at ingest and
consumed three times: day-0 verification, the rendered registry entry, and
the device's own `verify /sha512` step during a day-2 upgrade.

**SHA-512 is the only hash algorithm in the system, day-0 included, and
there is deliberately no `hash_algo` column** (README §3.4) — IOS-XE's
`verify /sha512` fixes the algorithm at the device end, so a second one
would mean storing two digests or recomputing at egress. Don't add
per-artifact algorithm support speculatively; it arrives with a platform
that actually requires it.

**`image_registry.yml` is a rendered projection, not a source of truth.**
Once the Software Lifecycle module exists, it becomes the sole writer of
this file — hand-editing it would drift it from the `artifacts` table.
The whole file is re-rendered from the table on every publish rather than
patched in place, which is what makes README §7.2's startup reconcile
possible: on any inconsistency between table and file, the table wins.

**Failure, concurrency, and staleness semantics live in README §7** and
are load-bearing rather than aspirational — the EE run is dispatched
out-of-band by a sibling process (never synchronously in a Flask request
handler, which would stall the phone-home route), registry writes are
serialized by an advisory `flock`, and `registry_jobs.status` is a state
machine with explicit terminal states (`succeeded`/`failed`/`timed_out`/
`abandoned`), not a success flag. Consult §7 before implementing anything
that writes to the database, the registry file, or git.

**Provisioning log and `registry_jobs` share plumbing (row shape,
retention-purge helper, log viewer) but stay semantically separate** —
distinctly labeled, not merged into one timeline, and never
cross-correlated into a persistent device record (NetHub is explicitly not
an inventory system — see README §2 Non-goals).

Vendor scope is IOS-XE only for now, but the Software Lifecycle schema and
job model carry a `platform` field throughout so a second platform is an
addition, not a rework.

## Ansible playbook notes (`ansible/upgrade_iosxe.yml`)

- Two plays run before the main upgrade play: an ungathered preview play
  (prints the full upgrade plan for every targeted host before any
  connections are made) and the main `serial`-gated upgrade play; a final
  summary play runs after all serial waves complete, using the
  `batch_summary` role.
- `upgrade_result` is initialized early (`status: not_started`) so the
  summary play's lookup never hits an undefined key, even for a host that
  fails a pre-check and never reaches the "record result" task.
- Image size resolution is a three-tier cascade: use `image_bundle.file_size`
  if the registry already recorded it → reuse a value another host in this
  same run already discovered (cached on `localhost` via `delegate_facts`)
  → fall back to a remote SCP size lookup via
  `files/remote_image_size.py` (referenced but not present in this repo).
  Per README §8, once NetHub always renders `file_size` into the registry,
  this whole cascade becomes dead code and can be deleted.
- The SCP copy task uses `no_log: true` because `scp_pass` is embedded
  directly in the `copy scp://...` command string with no finer-grained way
  to redact it.
