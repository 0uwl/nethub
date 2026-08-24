# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project status

NetHub is in early bootstrap. `app.py` currently only serves a static
homepage and error pages — none of the Provisioning or Software Lifecycle
functionality described in `design-document.md` exists yet. The `ansible/` playbook
references roles (`iosxe_facts`, `batch_summary`) and a helper
(`files/remote_image_size.py`) that are not present in this repo. Treat
`design-document.md` as the design document / target architecture, not a
description of current code — always verify a described component
actually exists before assuming it's implemented.

`example_inventory/` is likewise a design sketch, not working config. It
shows the ownership boundary between the user-uploaded upgrade request
and the inventory NetHub renders around it (design doc §3.5/§8.1); its
`rendered/` tree illustrates output NetHub does not yet produce.

## Commands

```bash
pip install -r requirements.txt   # Flask, gunicorn — no dev/test deps yet

export SECRET_KEY=<any-string>    # required; app.py raises ValueError without it
python app.py                     # runs the dev server (config.py sets DEBUG = True)
```

There are no tests, linter config, or CI in this repo yet.

The Ansible playbook (`ansible/upgrade_iosxe.yml`) is currently invoked
by hand, against a network device inventory not present in this repo:
```bash
ansible-playbook ansible/upgrade_iosxe.yml -e upgrade_serial=1
```
It targets Cisco IOS-XE devices (`cisco.ios` collection, `network_cli`
connection) and expects each host to define an `image_bundle` var
(filename, sha512, version, remote_dir, file_size) — see §5/§8 of
`design-document.md`. `upgrade_serial` controls how many hosts upgrade per wave
(defaults to 1); with `serial > 1`, hosts in the same wave share this
terminal's stdin for the interactive `pause` prompts, so they can
interleave.

That manual, interactive form is what is committed today. design doc §8.1
supersedes it in the target design: NetHub dispatches the playbook in
phases with no TTY, and the `pause` prompts become UI approval gates.
Both descriptions are correct about different things — don't "fix" the
playbook's prompts without implementing the phase model that replaces
them.

## Architecture (target design — see design-document.md for full detail)

NetHub unifies two device-lifecycle halves in **one Flask backend, one
database, one admin frontend**:

- **Provisioning (day-0)**: unauthenticated phone-home endpoint — a new
  device is checked against a serial allowlist and receives its initial
  config/image over plain HTTP. Plain HTTP is a deliberate choice, not an
  oversight (design doc §4): a first-contact device has no trust anchor to
  validate TLS against, so the real protection comes from VLAN isolation,
  serial allowlisting, and payload hash verification instead.
- **Software Lifecycle (day-2)**: authenticated admin flows for onboarding
  IOS-XE images and installing them. Every route in this module requires
  an authenticated session; the phone-home route is the system's only
  unauthenticated entry point.

**Day-2 is two separate EE dispatches, not one** (design doc §6, §8/§8.1).
Publishing runs `publish_image.yml` against the distribution host: upload
→ SHA-512 verify → staged publish → registry re-render → audit trail. It
never touches a network device. Installing runs `upgrade_iosxe.yml`
against devices, split into phases. Conflating them is the easiest
mistake to make in this codebase.

**Single artifact pipeline, two egress adapters** (design doc §3.4): one
ingest path (upload → hash → size → store → record) feeds both days —
day-0 devices pull over HTTP, day-2 devices pull over SCP. One `artifacts`
table backs both; a `kind` discriminator (script/config/image)
distinguishes rows rather than splitting into separate tables. The SHA-512
is computed once at ingest and consumed three times: day-0 verification,
the rendered registry entry, and the device's own `verify /sha512` step
during a day-2 upgrade.

**SHA-512 is the only hash algorithm in the system, day-0 included, and
there is deliberately no `hash_algo` column** (design doc §3.4) — IOS-XE's
`verify /sha512` fixes the algorithm at the device end, so a second one
would mean storing two digests or recomputing at egress. Don't add
per-artifact algorithm support speculatively; it arrives with a platform
that actually requires it.

**Everything the EE reads is rendered, not supplied** (design doc §3.5).
`image_registry.yml`, the per-job inventory, and the connection vars are
all projections of the `artifacts` and `users` tables, written at dispatch
into a `private_data_dir` that is discarded with the job. On any
inconsistency between table and file, the table wins (§7.2). The whole
registry is re-rendered on every publish rather than patched in place,
which is what makes that reconcile possible.

**Admin identity is OIDC by default, with local username/password as a
second backend; device credentials are a separate mechanism again**
(design doc §4.3/§4.4). `users.auth_backend` picks OIDC (keyed on the
`sub` claim) or `local` (password hash, admin-issued one-shot enrollment
token instead of an admin-set password, forced reset before first
session) per row — either way the row is checked against the local
`users` table, which decides *what* a verified *who* may do, including
its `role` (`admin` manages users/deployment settings, `operator` runs
day-to-day work). **Who sets that role depends on the backend and isn't
symmetric**: for `local` rows an admin picks it and can edit it later
from the settings page; for `oidc` rows it's computed at every login
from a configured group claim (`oidc_group_claim`/`oidc_admin_group`)
and shown read-only in NetHub — changing an OIDC user's role means
changing their IdP group membership, never a NetHub edit (design doc
§4.4). Don't add a role dropdown/edit path for OIDC-backed users; that's
a deliberate gap, not a missing feature. But `network_cli` needs a
password neither auth
backend will yield, so an upgrade run collects the submitter's device
credential at submit time, holds it in memory for the life of the run,
and never writes it to the `private_data_dir` or the session. The device
username comes from `users.device_username`, mapped server-side and
never read from a submitted request. The property this buys is
two-sided attribution: the same human in `upgrade_runs.submitted_by` and
in the device's own AAA accounting, recorded by two systems that share
no trust domain — which is also why local-account enrollment reuses the
allowlist's TTL/one-shot pattern (§4.1) rather than letting an admin set
someone else's password directly.

**Failure, concurrency, and staleness semantics live in design doc §7** and
are load-bearing rather than aspirational — EE runs are dispatched
out-of-band by a sibling process (never synchronously in a Flask request
handler, which would stall the phone-home route), registry writes are
serialized by an advisory `flock`, and job status is a state machine with
explicit terminal states (`succeeded`/`failed`/`timed_out`/`abandoned`),
not a success flag. Serialization applies to a *phase execution*, not to a
whole run: a run parked at an approval gate holds no EE process. Consult
§7 before implementing anything that writes to the database, the registry
file, or git.

**Provisioning log, `registry_jobs`, and upgrade run/phase rows share
plumbing (row shape, retention-purge helper, log viewer) but stay
semantically separate** — distinctly labeled, not merged into one
timeline, and never rolled up into a persistent per-device current-state
view. That roll-up is the line between a job record and an inventory, and
NetHub is explicitly not an inventory system (design doc §2 Non-goals, §7.4).

Vendor scope is IOS-XE only for now, but the Software Lifecycle schema and
job model carry a `platform` field throughout so a second platform is an
addition, not a rework.

## Hard rules — do not implement these

These are settled decisions with reasoning in the design doc. If a change
seems to require one, the design is what needs revisiting, not the rule.

- **No user-supplied playbooks.** The playbook set is closed at build
  time and selected by `platform` (design doc §2 Non-goals, §8.1). Accepting
  one is arbitrary code execution inside the EE with the vaulted
  distribution credentials in reach — an authenticated RCE primitive
  sitting beside the unauthenticated route §4 spends its length on.
- **No user-supplied inventories, and no user-supplied Jinja.** A user
  submits a *request document* — hosts, one bundle key each, a small
  closed set of typed knobs — which NetHub validates and compiles. An
  uploaded inventory carries `image_registry`, which would let a request
  name any filename against any SHA-512 and bypass the `artifacts` table.
  A `{{ ... }}` expression in a submitted field is the playbook hole in a
  different costume.
- **No user-settable connection vars.** `ansible_user` is the submitter's
  own device identity (or, only under shared account mode, one
  admin-configured value — never something a submitter's request
  supplies either way). An identity the submitter can type is not
  evidence of anything, and the audit property in §4.3 depends on it.
- **No EE invocation from the Flask process.** Flask holds the only
  unauthenticated route; giving it Podman access turns any Flask RCE into
  host-level container control (design doc §9). The sibling owns dispatch and
  the job row is the only IPC channel between them — which is also why
  there is no PTY streamed to the browser.
- **No shared service account for device login — except one explicit,
  deployment-level opt-in.** Per-user credentials are the default; a
  deployment with no per-human device logins can turn on **shared
  account mode** (design doc §4.4), which fixes `ansible_user` to one
  admin-configured value for every run instead of reading
  `users.device_username`. It is a knowingly-made deployment setting, not
  a per-user choice and not a fallback that engages itself when an IdP
  is missing — don't wire it up as a default or infer it from the
  absence of OIDC. The SCP account on the distribution host is a
  separate case and stays shared unconditionally: no human is in that
  session (design doc §4.3).

## Ansible playbook notes (`ansible/upgrade_iosxe.yml`)

Describes the playbook as committed. design doc §8.1 retires several of these.

- Two plays run before the main upgrade play: an ungathered preview play
  (prints the full upgrade plan for every targeted host before any
  connections are made) and the main `serial`-gated upgrade play; a final
  summary play runs after all serial waves complete, using the
  `batch_summary` role. Under the phase model both the preview and summary
  plays dissolve into the UI — NetHub already holds the plan it would
  print and the per-host results it would tally.
- `upgrade_result` is initialized early (`status: not_started`) so the
  summary play's lookup never hits an undefined key, even for a host that
  fails a pre-check and never reaches the "record result" task. Per-host
  state moves to `upgrade_run_hosts` under the phase model, which is what
  retires this and the `rescue`/`upgrade_stage_failed` bookkeeping.
- Image size resolution is a three-tier cascade: use `image_bundle.file_size`
  if the registry already recorded it → reuse a value another host in this
  same run already discovered (cached on `localhost` via `delegate_facts`)
  → fall back to a remote SCP size lookup via
  `files/remote_image_size.py` (referenced but not present in this repo).
  Per design doc §8, once NetHub always renders `file_size` into the registry,
  this whole cascade becomes dead code and can be deleted.
- The SCP copy task uses `no_log: true` because `scp_pass` is embedded
  directly in the `copy scp://...` command string with no finer-grained way
  to redact it.
- `ansible_user` currently does double duty: the SSH login to the device
  *and* the account the device uses to pull from `scp_server`. Splitting
  it into `ansible_user` (per-human) and `scp_user` (service account) is a
  prerequisite for the per-user credential model, not a follow-up —
  otherwise the distribution host needs a local account per engineer.

## Keeping this file current

This file is derived from `design-document.md` plus the actual state of the repo,
and it goes stale silently. When a change lands that alters what a future
session needs to know — a design decision, a command, a dependency, a new
directory, a rule about what must not be built — update this file in the
same pass. The `design-doc-sync` skill in `.claude/skills/` covers what to
check and what belongs here.
