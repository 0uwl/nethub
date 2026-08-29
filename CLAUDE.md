# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project status

NetHub is in early bootstrap. The Flask app lives in the `nethub/`
package, built as an application factory (`nethub.create_app()`) rather
than a module-level `app`. Flask's CLI autodetects it directly
(`flask --app nethub ...`), so there's no separate `wsgi.py`
entry-point file. Gunicorn (production; see "Container" below) takes it
as `nethub:create_app()` — a call expression, not a bare name — because
the pinned gunicorn version (26.x) parses its app argument as a Python
expression and only invokes it if written as a call; there is no
`--factory` flag in this version (older gunicorn releases used one —
don't assume it still exists without checking `gunicorn --help` against
whatever version `requirements.txt` actually resolves). It implements a
first slice of the Software Lifecycle module: local username/password
auth (everyone who can log in is an admin — no roles, no OIDC),
admin-driven user creation (`nethub/auth.py`), and a registry-publish
flow (`nethub/registry.py`, `nethub/registry_routes.py`) where an
uploaded image plus a typed-in checksum become a new `software_registry`
entry in a NetHub-owned YAML file, deletable from the same list page. A
delete is a hard, unaudited removal (the entry and its image file) — no
`state`/`superseded_by_id` machinery, matching alpha's existing
no-supersede stance on adds. Because the registry YAML is hand-editable
on disk (there's no `artifacts` table behind it — see below), every
route that reads it treats a broken file as recoverable, not fatal:
`registry.load_registry()` raises `RegistryError` (flashed, not a 500)
on invalid YAML or a `software_registry` key that isn't a mapping, and
`registry.check_registry()` — wired to a "Check registry" button, not
run implicitly on page load, since it hashes every registered image on
disk — walks every entry, silently re-deriving a stale `file_size`
(cheap, non-security, always recoverable from the file itself) and
flagging anything it can't safely fix on its own: a missing file, a
checksum that no longer matches the bytes on disk, a malformed or
incomplete entry. It never guesses at a wrong checksum. `alpha.md` is
that slice's plan and
records its deliberate deviations from `design-document.md` — no
`artifacts` table, no Ansible dispatch, no `registry_jobs`/git-committed
registry, sessions are Flask-Login's signed cookie rather than a
`sessions` row (§4.5). Provisioning (day-0) is entirely unimplemented.
The playbooks under `ansible/` are hand-invoked scaffolding, not yet
wired to anything NetHub provides — see "Ansible playbook notes" below
for their current shape and where they're headed. Treat
`design-document.md` as the design document / target architecture, not
a description of current code — always verify a described component
actually exists before assuming it's implemented.

NetHub containerizes as a single-process image (`Containerfile` —
`python:3.12-slim`, gunicorn, one worker) plus a dev image
(`Containerfile.dev` — Flask's own dev server with reload, run via
`dev.sh` against a bind-mounted repo for live edits) and a reference
Podman Quadlet unit at `quadlet/nethub.container`. See "Container" below
for the full environment-variable list and the systemd-credential
mechanism for `SECRET_KEY`/`ADMIN_PASSWORD`.

`ansible/inventory/` is a design sketch, not working config. It shows
the ownership boundary between the user-uploaded upgrade request and the
inventory NetHub renders around it (design doc §3.5/§8.1); its
`rendered/` tree illustrates output NetHub does not yet produce.

## Commands

```bash
pip install -r requirements.txt   # Flask, Flask-SQLAlchemy, Flask-Login,
                                   # Flask-WTF, PyYAML, gunicorn, pytest

export SECRET_KEY=<any-string>    # required; nethub/config.py raises ValueError without it
flask --app nethub run            # runs the dev server (DEBUG defaults off; --debug to override)

flask --app nethub create-admin <username>   # bootstrap the first login user --
                                              # there is no self-registration route

pytest                            # runs tests/ -- see tests/conftest.py for the
                                   # app/client fixtures (temp DB + registry root per test)
```

Tests cover `nethub/{credentials,models,bootstrap,auth,registry,registry_routes}.py`
end-to-end through Flask's test client (login flow, CSRF disabled in the `app`
fixture, registry upload/checksum validation). No linter config or CI yet.

The Ansible playbooks (`ansible/stage_cisco_upgrade.yml`,
`ansible/install_cisco_upgrade.yml`) are currently invoked by hand,
against a network device inventory not present in this repo:
```bash
ansible-playbook ansible/stage_cisco_upgrade.yml -e stage_serial=1
ansible-playbook ansible/install_cisco_upgrade.yml -e install_serial=1
```
They target Cisco IOS-XE devices (`cisco.ios` collection, `network_cli`
connection) and expect each host to define a `software_bundle` var
(filename, sha512, version, file_size) — see §5/§8 of
`design-document.md`. The staging playbook also reads `image_transport`
(`push_scp` default, or `pull_sftp` plus `distribution_host` /
`distribution_user` and a password from
`DISTRIBUTION_PASSWORD`); it lives in
`ansible/inventory/rendered/group_vars/all.yml` and is deployment-level,
never per-request. `stage_serial`/`install_serial` control how many
hosts run per wave in each playbook (both default to 1); with a value
>1, hosts in the same wave share this terminal's stdin for the
interactive `pause` prompts, so they can interleave.

That manual, interactive, two-file form is what is committed today. The
split previews the idea behind design doc §8.1's phase split (stage vs.
activate/verify/cleanup) without implementing the rest of it — both
playbooks still run by hand, still pause for confirmation, and NetHub
dispatches neither. §8.1 supersedes this in the target design: NetHub
dispatches each phase with no TTY, and the `pause` prompts become UI
approval gates. Don't "fix" either playbook's prompts without
implementing the phase model that replaces them.

## Container

`Containerfile` builds `localhost/nethub:latest` — single stage,
`python:3.12-slim`, non-root UID 1000, gunicorn as PID 1 via
`nethub:create_app()` (see the "Project status" note above on why that's
a call expression, not `--factory`). `Containerfile.dev` is dev-only:
Flask's own dev server with `--debug` (reload + interactive debugger),
application code deliberately *not* baked in (only `requirements.txt`
is), so `dev.sh` can bind-mount the repo at `/app` and get live edits
with no rebuild. Never run `Containerfile.dev` as anything but a local
dev convenience — same reasoning as the `DEBUG` hard rule below.

`nethub/gunicorn.conf.py` fixes `workers = 1` (not a knob — SQLite plus
the single-worker hard rule below both assume exactly one process) and
sets `control_socket_disable = True`: gunicorn ≥25.1 otherwise tries to
create `$HOME/.gunicorn/gunicorn.ctl` for a control socket nothing here
uses, which throws under the Quadlet unit's `ReadOnly=true`. Discovered
by actually running the built image read-only, not by inspection — if
gunicorn's default behavior changes again, re-check by running the
container rather than trusting this note.

`quadlet/nethub.container` is the reference Podman Quadlet unit (see
Drawbridge's own `quadlet/drawbridge.container` for the sibling
project's version of the same pattern). Verified end-to-end against a
real `podman build`/`podman run` — including `UserNS=keep-id`,
`ReadOnly=true`+`Tmpfs=`, and the systemd-credential path — not just
written from the Drawbridge example and assumed to work. Two things
worth knowing if this file gets edited:

- **`LoadCredential=`/`SetCredential=` belong in `[Service]`, not
  `[Container]`.** They're plain systemd unit directives; Quadlet passes
  a literal `[Service]` section straight through to the generated
  `.service` unit unchanged (`man podman-systemd.unit`), but they are
  *not* recognized `[Container]` keys. Whether Podman then forwards the
  resulting `$CREDENTIALS_DIRECTORY` into the container automatically is
  version-dependent and not something to assert confidently without
  checking the Podman version in the field — the unit file's own
  comments say so; don't strengthen that claim without re-verifying it.
- **`UserNS=keep-id:uid=1000,gid=1000` is required for the volumes to be
  writable, not optional hardening.** Confirmed by testing: the
  container's bind mounts fail with `unable to open database file`
  without it, even when the host directory and the image's fixed UID
  1000 numerically match — rootless Podman's default user namespace
  doesn't map that 1:1 on its own.

Environment variables the unit (or a plain `podman run`) can set:

| Variable | Default | Notes |
|---|---|---|
| `SECRET_KEY` | none — required | Flask/Flask-Login session-signing key. A systemd credential named `secret_key` takes priority over this env var (`nethub/credentials.py`) — see the unit file's `[Service]` block. |
| `DATABASE_PATH` | `<repo root>/database.db` | Bare SQLite file path, not a URL — set to a path under the `/app/data` volume in the container. |
| `IMAGE_DIR` | `<repo root>/instance/registry/images` | Where uploaded images are stored — set to a path under the `/app/registry` volume in the container. |
| `REGISTRY_FILE` | `<repo root>/instance/registry/software_registry.yml` | Full path to the registry YAML file — independent of `IMAGE_DIR` (no shared "root" var), so it can point at a bind-mounted host file (e.g. a real Ansible `group_vars/os_iosxe.yml`) and have publishing write straight into it. |
| `MAX_CONTENT_LENGTH` | `1_500 * 1024 * 1024` | Upload size cap, bytes. |
| `NETHUB_PORT` | `8080` | Read by `nethub/gunicorn.conf.py`'s `bind`; update the Quadlet `PublishPort=` to match if changed. |
| `ADMIN_USERNAME` | `admin` | First-boot only — ignored once the `users` table is non-empty (`nethub/bootstrap.py`). |
| `ADMIN_PASSWORD` | none — random, printed once, if unset | First-boot only, same gate as above. A systemd credential named `admin_password` takes priority over this env var, same mechanism as `SECRET_KEY`. |
| `DEBUG` | off | Local dev only — see the hard rule below. No effect on the production image; gunicorn never reads it. |

The `ADMIN_USERNAME`/`ADMIN_PASSWORD`/credential priority order in
`nethub/bootstrap.py` is deliberately modeled on Drawbridge's
`_initial_admin_password()` (`drawbridge/db.py`) — credential beats env
var beats generated-and-printed — but does **not** carry over
Drawbridge's forced-password-reset-on-first-login behavior for the
env/generated tiers. That's not an oversight: this alpha's `User` model
has no such field and no reset flow to force into (see `alpha.md`), so
replicating the label without the mechanism behind it would just be a
UI claim nothing enforces. Don't add a `must_reset_password` column
without building the flow that reads it.

## Architecture (target design — see design-document.md for full detail)

NetHub unifies two device-lifecycle halves in **one Flask backend, one
database, one admin frontend**:

- **Provisioning (day-0)**: unauthenticated phone-home endpoint — a new
  device is checked against a serial allowlist and receives its initial
  config/image over plain HTTP. Plain HTTP is a deliberate choice, not an
  oversight (design doc §4): a first-contact device has no trust anchor to
  validate TLS against, so the real protection comes from VLAN isolation,
  serial allowlisting, and payload hash verification instead. That
  argument is about *authentication* only — §4 also states plainly that a
  passive listener on the provisioning VLAN reads the bootstrap config
  (TACACS+/RADIUS secrets, SNMP communities, enable hashes), which is why
  §3.3 keeps per-device artifacts off any guessable path.
- **Software Lifecycle (day-2)**: authenticated admin flows for onboarding
  IOS-XE images and installing them. Every route in this module requires
  an authenticated session; the phone-home route is the system's only
  unauthenticated entry point.

**Publishing and installing are two separate operations, and only one is
an EE dispatch** (design doc §6, §8/§8.1). Publishing is local to NetHub
now that NetHub owns the store (§3.3): upload → SHA-512 verify → promote
bytes and row → registry re-render → git commit → audit trail. It never
touches a network device and no longer runs a playbook. Installing runs
`upgrade_iosxe.yml` against devices, split into phases, and is the only
thing in the system that dispatches an EE. Two things that did *not*
change when publish stopped being a dispatch: it stays in the **sibling**
(§3.2's argument is unchanged — it must not run in a Flask handler), and
it keeps its `registry_jobs` row, the serial queue, `render_state`, the
`flock`, the startup sweep, and every state in §7.3. Conflating publish
with install is still the easiest mistake to make here; they're now
separate in kind rather than two instances of one mechanism.

**NetHub requires one thing of a device at login, and the list should
stay short** (design doc §4.3): **privilege level 15**. NetHub otherwise
writes no configuration outside the upgrade itself, and asserts privilege
15 at pre-check. One exception is deliberate, bracketed rather than
standing, **and belongs to the push transport alone**: the stage phase
enables the device's own SCP server for the duration of the push and
restores whatever it found — enabled or not — in an `always:` block,
confirmed by re-reading the running-config rather than trusted from the
module's exit status (design doc §4.3.1). A host whose restore can't be
confirmed is failed outright (`end_host`), because an unconfirmed enable
would otherwise ride into startup-config on `write memory`. Under the
pull transport nothing on the device is reconfigured at all, so the
exception doesn't arise. The pre-check asserts no
`ip ssh source-interface` in either mode — it was a pull-era carryover
and push confirmed it doesn't gate the device's SCP server, an unrelated
service. It's relevant again *for a deployment running pull* (the device
is an SSH client again), but as a device-side prerequisite an operator
satisfies, not as a check NetHub makes; there is no pre-check assertion
task file in the playbooks to hang one on today.

**On privilege 15 specifically — there is no `enable` escalation
anywhere** (design doc §4.3). Every command an upgrade runs —
`write memory`, `copy` to flash, `install add … activate commit` — needs
level 15, so an account that can't reach it can't upgrade anything.
Asking for it at login rather than via `enable` removes a second secret
(no `become_password` is ever collected) and removes the shared-enable-
secret problem, which is the same shared-credential objection §4.3 raises
about service accounts. Don't add `ansible_become`/`ansible_become_method`
back to the rendered inventory, and don't add a `become_password` to the
credential path. It's also a *property* rather than a tax: §4.3 says
NetHub's `role` gates NetHub's screens and never substitutes for what a
credential authorizes on the device — the privilege requirement makes
that concrete, since the refusal comes from the device. The playbook
asserts it at pre-check so an under-privileged account fails once and
clearly (`failure_stage: privilege`) rather than part-way through a wave.

**The day-0 HTTP daemon authorizes nothing, and must never be asked to**
(design doc §3.3/§3.4). A static file server is path-addressed: it cannot
check the allowlist, consume a one-shot entry, or write a log row, so
leaving per-device configs on an open docroot puts the network's
credentials behind a guessable filename. The docroot holds one fixed path
(the generic no-op script DHCP option 67 points at, served by the
*distribution host*, not Flask) plus a `mint/` subtree of one-shot
symlinks. The sequence is: script fetched → script POSTs serial to
Flask's phone-home → Flask claims the allowlist entry and mints
high-entropy, TTL-bounded, one-shot paths → device fetches those. All
day-0 authorization lives in the phone-home handler. NetHub sees the
mint, not the boot, so the log records what was *offered*, never
"served".

**Single artifact pipeline, two egress adapters** (design doc §3.4): one
ingest path (upload → hash → size → store → record) feeds both days —
day-0 devices pull over HTTP; day-2 goes in whichever direction the
deployment's `image_transport` setting selects (design doc §4.3.1),
either NetHub pushing over SCP (`net_put`, riding the same `network_cli`
session already authenticated with the submitter's device credential —
the default, and the mode with no distribution credential at all) or the
device pulling over SFTP from a distribution daemon. One `artifacts`
table backs both days; a `kind` discriminator (script/config/image)
distinguishes rows rather than splitting into separate tables. The SHA-512
is computed once at ingest and consumed three times: day-0 verification,
the rendered registry entry, and the device's own `verify /sha512` step
during a day-2 upgrade, run again against the transferred bytes in either
direction.

**SHA-512 is the only hash algorithm in the system, day-0 included, and
there is deliberately no `hash_algo` column** (design doc §3.4) — IOS-XE's
`verify /sha512` fixes the algorithm at the device end, so a second one
would mean storing two digests or recomputing at egress. Don't add
per-artifact algorithm support speculatively; it arrives with a platform
that actually requires it.

**Everything the EE reads is rendered, not supplied** (design doc §3.5).
`software_registry.yml`, the per-job inventory, and the connection vars are
all projections of the `artifacts` and `users` tables, written at dispatch
into a `private_data_dir` that is discarded with the job. On any
inconsistency between table and file, the table wins (§7.2). The whole
registry is re-rendered on every publish rather than patched in place,
which is what makes that reconcile possible. **Kea's reservations are the
fourth store and follow the same rule** (§7.4): rendered whole from
`allowlist_entries` and reconciled, because a reservation outliving a
consumed entry silently re-opens §4.1's first gate.

**"Table wins" and "stop, don't destroy the evidence" fire on the same
signal, and `render_state` is what separates them** (§7.1/§7.2). A
registry file that no longer matches the table means either an
interrupted publish (a `registry_jobs` row at `written`, not `committed`
— re-render and commit the correction) or an outside edit (no such row —
preserve it, refuse to publish, surface it, and let an admin explicitly
adopt or discard). Don't implement one of those behaviours without the
`render_state` test in front of it; each is the wrong response to the
other case.

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
a deliberate gap, not a missing feature. **But an absent claim is not a
demotion** (§4.4): claim present without the admin group demotes; the
claim missing from the token entirely is a misconfiguration — preserve
the prior role, refuse the write, raise a deployment fault. Treating the
two alike demotes every admin at once, with the fix behind a settings
page that now needs an admin nobody has. Three companions to that rule:
a role computation that would leave zero active admins is refused; a
`nethub-admin` CLI running as the service user is the documented
break-glass (it grants nothing host access didn't already imply); and
first run seeds a `local` admin with a one-shot enrollment token, since
a fresh database otherwise has no way in. Local accounts can also be
disabled deployment-wide (`local_accounts_enabled`) — without that, an
OIDC admin about to lose their group just creates a local admin row and
the IdP's decision is inert. But `network_cli` needs a
password neither auth
backend will yield, so an upgrade run collects the submitter's device
credential at each approval gate, holds it in memory only for the life
of the *phase execution* that approval releases (design doc §4.3/§9.1 —
not for the life of the run, since a run can park at a gate for days),
and never writes it to the `private_data_dir` or the session. The device
username comes from `users.device_username`, mapped server-side and
never read from a submitted request. The property this buys is
two-sided attribution: the same human in `upgrade_runs.submitted_by` and
in the device's own AAA accounting, recorded by two systems that share
no trust domain — which is also why local-account enrollment reuses the
allowlist's TTL/one-shot pattern (§4.1) rather than letting an admin set
someone else's password directly. That property has a precondition the
design now states: **centralized AAA with command accounting.** With
per-human accounts configured device-locally, both records come from
NetHub's own deployment and "two systems, no shared trust domain" is no
longer true — read the claim at that weaker strength there.

**The session is a row, because otherwise `is_active` revokes nothing**
(design doc §4.5, a new section). A client-side signed cookie makes both
deactivation and §4.4's role re-derivation advisory — neither has
anything to act on until the cookie expires by itself, and a login *is*
the caching event §4.4 says it avoids. So: server-side `sessions` table,
`is_active` and `role` re-read from `users` on every request, absolute
plus idle timeouts, other sessions invalidated on password reset / role
change / deactivation, and CSRF on every write with `SameSite=Strict` on
approvals — a cross-site "approve: reload" is a fleet outage. Use a
maintained OIDC library rather than hand-rolling `state`/nonce,
redirect-URI allowlisting, and ID-token validation.

**The allowlist burn is a conditional claim, not a flag write** (design
doc §4.1). "Consumed on first successful provision" names an event
NetHub cannot observe, so it is restated as what it can: the entry is
claimed at phone-home inside `BEGIN IMMEDIATE` with **the rowcount as
the authorization result** (`... WHERE serial=:s AND state='armed' AND
expires_at > :now`) — zero rows changed is denied exactly as an unknown
serial is. One-shot scopes to a bounded *provisioning window* (minutes),
not to a single request, because a real ZTP flow is several fetches;
uniqueness is `UNIQUE(serial) WHERE state='armed'` so an RMA'd chassis
can be re-enrolled; and re-arming is an explicit attributable admin
action (a new row via `rearmed_from_id`), never an automatic retry.

**§4.2's never-purged counters need a bounded key space first.** Both
keys are attacker-chosen on an unauthenticated route, so "a few integers,
never purged" is otherwise an unbounded-growth attack on the SQLite file
everything else shares. Per-serial counters only for serials that exist
or existed in the allowlist; everything else aggregates per source (/32,
/64) with a top-N and an `__overflow__` bucket. Accumulate in memory and
flush periodically — a synchronous durable write on the denial path is
the one write the attacker times. Denied and successful attempts get
separate rate budgets, or burning a serial's budget locks out its real
enrollment. And add the alert §4.2 was missing: **a denial for a serial
consumed inside its own TTL window** is the highest-signal event the
endpoint can produce, and a successful spoof otherwise looks like
nothing.

**Device host keys are pinned, and that is not an inventory** (design
doc §2, §4.3, §5). `network_cli` sends the password after key exchange
and `ansible_host` is submitter-supplied, so without verification an
operator can name a machine they control and be handed a colleague's
AAA credential. A `device_host_keys` table (keyed on address, not on a
device identity) backs a rendered `known_hosts` and a fail-closed
check; first contact is TOFU with the fingerprint shown at the submit
gate and recorded against the approver. These rows are deliberately
exempt from §7.4's retention purge — expiring one silently downgrades a
fail-closed mismatch back to a first-contact prompt. §2's non-goal
names this exception explicitly so nobody "fixes" it later.

**Failure, concurrency, and staleness semantics live in design doc §7** and
are load-bearing rather than aspirational — EE runs are dispatched
out-of-band by a sibling process (never synchronously in a Flask request
handler, which would stall the phone-home route), registry writes are
serialized by an advisory `flock`, and job status is a state machine with
explicit terminal states
(`succeeded`/`failed`/`timed_out`/`abandoned`/`cancelled`/`expired`), not
a success flag. Serialization applies to a *phase execution*, not to a
whole run: a run parked at an approval gate holds no EE process. Consult
§7 before implementing anything that writes to the database, the registry
file, or git.

Five things in §7.3 are easy to get wrong and are now specified:

- **`status` is shared across job kinds; `failure_stage` is not.** A
  publish stops at `credential`/`stage`/`verify`/`render`/`commit`, and
  §8.1's phases are *also* named `stage` and `verify` — so `phase=
  'activate', failure_stage='stage'` would be ambiguous on its face.
  Phase jobs get their own vocabulary.
- **Cancel is a column the sibling polls** (`cancel_requested_at`), not a
  signal or a kill — §9's rule that the job row is the only control
  channel is what forces that. Checked between hosts and at phase
  boundaries only. `abandoned` is for crashes; `cancelled` and `expired`
  are for humans and TTLs, and merging them costs the word its
  diagnostic value.
- **The sweep keys on `runner_instance_id`** (a UUID minted per sibling
  start), never a PID — a PID is reused across container restarts and
  meaningless across PID namespaces.
- **Flask *reads* `heartbeat_at` and renders "stalled".** The sweep lives
  in the sibling so it can't fire against a healthy run, which means a
  sibling that dies and stays dead is swept by nobody. Noticing doesn't
  have to be the sibling's job; changing the row still is.
- **An `abandoned` device-touching phase needs a fresh approval**, not an
  auto-retry — the approval is what supplies the credential and names the
  human. The retry is a new row with an incremented `attempt`.

**All three state machines are enumerated in §7.3 with the actor on every
edge** (Flask / sibling / sweep). Flask writes exactly one job edge — the
one that creates a `queued` row. If a change has Flask writing job state
after dispatch, it's violating §9, and the table is where to check that.

**Retention has two axes, and §7.4 used to conflate them.** "Retained
while referenced, regardless of age" is right for an artifact *row* and
wrong for a ~500 MB blob pinned forever by one surviving job row —
`bytes_state`/`bytes_pruned_at` let the audit row honestly outlive the
file. The purge runs in the *sibling* (it takes the registry lock and
deletes on the distribution host; neither is work for the process holding
the unauthenticated route).

**Provisioning log, `registry_jobs`, and upgrade run/phase rows share
plumbing (row shape, retention-purge helper, log viewer) but stay
semantically separate** — distinctly labeled, not merged into one
timeline, and never rolled up into a persistent per-device current-state
view. That roll-up is the line between a job record and an inventory, and
NetHub is explicitly not an inventory system (design doc §2 Non-goals, §7.4).

**§5 now specifies the day-0 and settings tables it used to discharge in
a sentence.** New since the review: `allowlist_entries` (with the
per-role artifact FKs that make §3.4's serial→artifact mapping
representable at all, plus `mac` for the Kea gate), `provisioning_log`
with an `outcome` enum and a `provisioning_log_artifacts` junction,
`settings` + append-only `settings_audit`, `user_admin_audit`, `sessions`,
and `upgrade_host_phase_results`. Four columns recur on both job tables
because §7/§9 assume them and none existed: `created_at` (the queue has
nothing else to order by — `started_at` is null until dispatch),
`deadline_at`, `runner_instance_id`, `private_data_dir`. Two constraints
are load-bearing rather than tidy: `UNIQUE(run_id, phase, attempt)` is
what makes two admins clicking "approve: reload" a `409` instead of two
reloads (§8.1's serialization is scoped to *execution*, so it stops
overlapping processes, not duplicate rows), and `PRIMARY KEY (run_id,
hostname)` stops a request naming a host twice from reloading it twice.

**Snapshots must carry every field the thing they snapshot carries.**
`registry_jobs` and `upgrade_run_hosts` needed `file_size` added: without
it the row that answers "what did we publish that day" can't reproduce
the registry entry, and — worse, because it's operational — a phase
either can't render its inventory from the run's own rows or has to
re-read `artifacts`, which lets a mid-run supersede silently re-target
the run. With it, a run is self-contained and never reads `artifacts` at
dispatch. There is no `remote_dir` column at all anymore — it addressed a
device's own `copy sftp://…` path under the old pull design, a remnant
from when the distribution host could have been a separate remote
machine, and push reads straight from a fixed local mount by filename
(design doc §5). Don't reintroduce it. `upgrade_runs` also snapshots
`shared_account_mode` beside `device_username_used`, or an auditor can't
tell "jsmith ran this" from "everyone runs as jsmith".

**§6.1 specifies the API surface** — routes, methods, the `202` + job-id
polling contract, and an error envelope that returns §7.3's
`failure_stage`/`error_summary` enums so the dashboard and the log agree
on words. `POST /provision` is the only unauthenticated route and never
returns `403` or a distinguishable body for a known serial (§4.1's
enumeration-oracle rule).

Vendor scope is IOS-XE only for now, but the Software Lifecycle schema and
job model carry a `platform` field throughout so a second platform is an
addition, not a rework.

## Hard rules — do not implement these

These are settled decisions with reasoning in the design doc. If a change
seems to require one, the design is what needs revisiting, not the rule.

- **No user-supplied playbooks.** The playbook set is closed at build
  time and selected by `platform` (design doc §2 Non-goals, §8.1). Accepting
  one is arbitrary code execution inside the EE with the live device
  credential in reach — an authenticated RCE primitive sitting beside the
  unauthenticated route §4 spends its length on. There is no longer a
  separate distribution credential for a playbook to reach for (push
  removed it, design doc §4.3.1); the device credential §9.1 injects for
  the phase execution is the one that matters, and per-phase collection
  bounds what it is worth after the phase ends. It does nothing about a
  playbook running while it is still valid, so the rule is unaffected.
- **No user-supplied inventories, and no user-supplied Jinja.** A user
  submits a *request document* — hosts, one bundle key each, a small
  closed set of typed knobs — which NetHub validates and compiles. An
  uploaded inventory carries `software_registry`, which would let a request
  name any filename against any SHA-512 and bypass the `artifacts` table.
  A `{{ ... }}` expression in a submitted field is the playbook hole in a
  different costume.
- **No user-settable connection vars — except `ansible_host`, which is
  validated rather than trusted.** `ansible_user` is the submitter's own
  device identity (or, only under shared account mode, one
  admin-configured value — never something a submitter's request
  supplies either way). An identity the submitter can type is not
  evidence of anything, and the audit property in §4.3 depends on it.
  The same goes for credentials and `become` settings. `ansible_host` is
  different and the rule used to be wrong about it: a request has to
  name its targets, and `ansible/inventory/README.md` has always listed
  it as an allowed field. It is accepted under two constraints — a
  deployment-level target CIDR (a `settings` row, §5), and a fail-closed
  host-key check against `device_host_keys` (design doc §4.3/§8.1). The line is between vars
  asserting *who someone is* (never submitted) and the address of the
  thing being acted on (necessarily submitted).
- **No EE invocation from the Flask process.** Flask holds the only
  unauthenticated route; giving it Podman access turns any Flask RCE into
  host-level container control (design doc §9). The sibling owns dispatch
  and the job row is the only *control* channel between them — which is
  also why there is no PTY streamed to the browser. There is exactly one
  other channel and it is not a general one: a sibling-initiated Unix
  socket carrying per-execution credentials and nothing else (design doc
  §9.1). Don't widen it into RPC, don't let control information or
  status onto it, and don't let Flask be the side that connects — the
  reason is window minimisation (a deposit endpoint means the sibling
  holds secrets for executions it hasn't started, and can be flooded),
  not that a deposit socket would be "the Podman socket in miniature".
  The construction is §9.2, and three things about it are easy to get
  wrong: the **mount** is the
  authenticator, not `SO_PEERCRED` — under one shared rootless user a
  uid check discriminates nothing, so don't write one and believe it
  means something; the socket is created by a systemd `.socket` unit,
  never by `unlink()`+`bind()` in either container; and the sibling
  parses bytes Flask chose, so the response surface needs framing,
  caps, deadlines, and a character allowlist on the credential before
  it reaches any variable or command string.
- **Flask runs exactly one worker — and more than one thread.**
  `gunicorn` with `workers = 2` silently breaks the credential path: a
  credential submitted to worker A is invisible to worker B (design doc
  §9.2). It fails closed but intermittently, and the error says nothing
  about workers. Threads are the other half of the same rule and are
  *required*, not optional: §3.2 now notes that a 1.2 GB image upload
  plus its SHA-512 pass is the longest operation in the system after the
  EE, and single-threaded it would hold the phone-home route for the
  whole window — the exact failure §3.2 legislated against, arriving
  through ingest instead of through `ansible_runner.run()`. Hash
  incrementally over the chunks as they're written so "hashed once at
  ingest" survives. The socket is served on its own thread with
  deadlines, for the same reason. §3.2 also now names the budget the
  whole design is calibrated against: **2 s p99 on phone-home, 15 min of
  device backoff.** Check changes to the request path against those
  numbers.
- **No secrets in the `private_data_dir`, and don't trust
  `ansible-runner` to keep them out.** `extravars` lands in
  `env/extravars`, `passwords` in `env/passwords`, `envvars` becomes
  `podman run -e` and shows up in `/proc/<pid>/cmdline`. The design says
  credentials live only in memory (design doc §4.3/§4.3.1/§9.2), and
  that is something you have to engineer: tmpfs-backed
  `private_data_dir`, `env/` and `inventory/` destroyed with the
  execution, and only scrubbed `stdout`/`job_events` retained as
  `playbook_log_path`. Retaining the directory wholesale would park
  credentials on disk for §7.4's full 365 days.
- **`DEBUG` must be off wherever a credential path exists** (and wherever
  §4.5's session model applies — the same debugger renders a session
  cookie alongside a device password). It already applies to the local
  username/password login alpha added (`nethub/auth.py`): `nethub/config.py`
  reads `DEBUG` from an env var and defaults it to off, rather than
  hardcoding `True`, precisely because that login form now exists.
  Werkzeug's interactive debugger renders frame locals — including a
  submitted password — into an HTTP response, and the reloader runs two
  processes; don't flip the default back to `True` or make it easier to
  turn on than the current env var opt-in. It remains a release blocker
  for the full approval flow's device credentials, same reasoning, wider
  blast radius. Same class, not yet addressed: `LimitCORE=0` and
  non-dumpable process, or a crash writes the heap to
  `/var/lib/systemd/coredump`.
- **Everything runs on one host, in separate Quadlet units, under one
  rootless user — DHCP being the deliberate exception** (it stays
  native; it binds a privileged broadcast-facing port and is outside
  §9.1's trust argument entirely). This is a deployment constraint, not
  a conclusion to re-derive: SQLite is written by *both* Flask and the
  sibling, and its locking is unreliable on network filesystems, so
  distributing components means §7 rebuilt around a different database.
  Don't repeat the claim that `flock` is unavailable over NFS — Linux
  has emulated it over NFSv4 since ~2.6.37, and the SQLite argument
  carries this on its own. *Separate* units matter too: distinct PID
  namespaces are what prevent same-uid `ptrace` and `/proc/<pid>/mem`
  between Flask and the sibling, so never put them in a shared `Pod=`.
- **No shared service account for device login — except one explicit,
  deployment-level opt-in.** Per-user credentials are the default; a
  deployment with no per-human device logins can turn on **shared
  account mode** (design doc §4.4), which fixes `ansible_user` to one
  admin-configured value for every run instead of reading
  `users.device_username`. It is a knowingly-made deployment setting, not
  a per-user choice and not a fallback that engages itself when an IdP
  is missing — don't wire it up as a default or infer it from the
  absence of OIDC. The pull transport's `dedicated` distribution account
  (design doc §4.3.1) is not a counterexample and must not become one:
  it is an account on the *distribution host*, read-only over one
  directory, never a device login. No shared account ever authenticates
  to a device except under shared account mode.
- **The device-side SCP-server toggle (push transport only) must always
  be bracketed by a confirmed restore, and a host whose restore can't be
  confirmed must fail, not warn.** The push adapter captures the device's prior
  `ip scp server enable` state before touching it, changes it only if not
  already enabled, and restores it in an `always:` block regardless of
  whether the push succeeded (design doc §4.3.1). The restore is
  *confirmed* by re-reading the running-config, not trusted from a module
  exit status — an unconfirmed restore fails the host outright
  (`end_host`), because an unconfirmed enable would otherwise ride into
  startup-config on the activate phase's own `write memory`. Don't relax
  this to a logged warning: a device left with its SCP server on and no
  record of it is exactly the "cannot enumerate afterward" failure the
  design used to reject push over. Known, accepted, and *not* covered by
  this mechanism: a killed process, an abandoned run, or a crashed EE
  container never reaches the `always:` block at all (design doc §4.3.1,
  §10) — don't claim this is fully closed. All of this is scoped to
  `push_image_scp.yml`; the pull adapter reconfigures nothing and has no
  bracket, which is why `scp_restore_confirmed` is null for a
  pull-transport stage row and why that null must be read together with
  `upgrade_runs.image_transport_used` rather than alone (design doc §5).
- **Day-2 transfer runs in whichever direction `image_transport` says,
  and the adapters are not interchangeable in their costs** (design doc
  §4.3.1). `push_scp` is the default: `ansible.netcommon.net_put` under
  the `paramiko` connection type, in `ansible/tasks/push_image_scp.yml`.
  IOS-XE has no SFTP *server* (client only), so a push has no SFTP
  option — SCP is the only wire protocol available in that direction.
  `pull_sftp` is the alternative: one `copy sftp://…` on the device's own
  CLI in `ansible/tasks/pull_image_sftp.yml`, driven with
  `ansible.netcommon.cli_command` prompt/answer. `tasks/transfer_image.yml`
  dispatches between them and owns the shared `verify /sha512`.
  - The isolation layer is now *required*, reversing the previous rule
    against a `push_image_net_put.yml`. That rule's reason — "which
    connection type to use is a settled decision, not something to keep
    isolated for a later swap" — doesn't survive: the split now serves a
    live configurable choice, not a speculative future one. Don't
    re-inline either adapter.
  - `net_put` under `libssh` was tested against a real device and found
    unusable (SSH connection broke repeatedly, nothing useful in the
    debug log). `paramiko` is deprecated upstream but is the only thing
    that works, so that's what's committed for push. Pull touches
    neither library, which is one of its arguments (design doc §10).
  - Shelling out to OpenSSH `scp` inside the EE was considered and
    rejected — it needs the device password on an interface `net_put`
    doesn't, with no secure way yet to hand a credential to a subprocess
    without it landing on a command line or on disk (design doc §3.3,
    §10). Don't add an OpenSSH-`scp` fallback without revisiting that.
  - The pull password is answered at the device's own `Password:`
    prompt with `no_log`, **never** embedded as `sftp://user:pass@host/`
    — that form lands in the device's command history and AAA command
    accounting, which is the record §4.3's attribution property depends
    on. `no_log` does not redact connection-plugin debug logging, so a
    pull-transport phase must not run at `-vvv` or with
    `ansible_persistent_log_messages`.
  - The pull adapter's prompt sequence is **unverified against a real
    device** (design doc §10). Naming only the filesystem as the copy
    destination is deliberate: it forces the `Destination filename`
    prompt so both prompts always appear in a known order. A wrong
    prompt list hangs until the task timeout rather than failing fast.
- **Transport is deployment-level and never request-level, and its
  credential never lives in the `settings` table** (design doc §4.3.1,
  §5). `settings` holds `image_transport`, `distribution_host`,
  `distribution_user`, `distribution_credential_source` — no password.
  Under `same_as_device` the distribution credential is the submitter's
  own, already crossing §9.1's socket; under `dedicated` it is a systemd
  credential in the sibling's unit that Flask never sees. A request may
  not select the transport, because selecting the transport selects whose
  credential gets spent. And **don't reach for Ansible Vault**: the file
  the EE would read is on a tmpfs `private_data_dir` destroyed with the
  execution, so vault would protect the least-exposed copy using a second
  secret delivered by the same means — the key-disposal problem this
  design already removed once. `image_transport` and `distribution_host`
  belong in §7.2's signed set: repointing the distribution host is the
  highest-yield settings write in the system.
- **NetHub is the sole source of the image bytes, and that is not
  modular** (design doc §2 Non-goals, §3.3). The *source* is fixed even
  though the *direction* is configurable: both adapters read the same
  published subtree on the NetHub host — pushed off a read-only EE
  mount, or served from it by the pull transport's SFTP daemon. There is
  no remote distribution target, no mirror, and no second store. The cost
  is written down: every byte crosses whatever link separates NetHub from
  the device either way, which bites first on a branch site behind a
  narrow link. Reopening it (§10) is now two questions — a push mirror
  needs a process near the devices that authenticates to them, a pull
  mirror needs only a verified copy of the subtree behind a daemon.
  Also: don't serve day-2 images from the day-0 HTTP docroot to avoid
  running the SFTP daemon. That puts the image store behind a guessable
  filename on an unauthenticated read path, which §3.3 exists to forbid;
  HTTP pull is tracked in §10 and would need `mint/`-style capability
  paths, not a shared docroot.
- **Push is the *default*, not the only mode, and the reason it is the
  default is a hard fact about networks** (design doc §4.3.1). Pull
  requires the device to open an outbound connection to NetHub, which a
  nontrivial fraction of real deployments block at the perimeter — where
  that rule is in force pull doesn't degrade, it doesn't work. So:
  don't make pull the default, don't infer it from anything, and don't
  propose it as a fix for a push problem without confirming the
  deployment's devices can actually reach the distribution host. Two
  costs are pull's alone and must not be argued away: an inbound SFTP
  daemon on the NetHub host, and **no host-key verification of the
  distribution host by the device** — IOS-XE's SSH client doesn't do it,
  so a redirected session collects the distribution credential.
  `verify /sha512` catches substituted *bytes*; nothing catches the
  substituted *host*. That asymmetry is why the transport is an admin's
  decision and never a submitter's.

## Ansible playbook notes (`ansible/stage_cisco_upgrade.yml`, `ansible/install_cisco_upgrade.yml`)

Describes the playbooks as committed — hand-invoked scaffolding, not
final. They're deliberately split in two to preview the idea behind
design doc §8.1's phase split (stage vs. activate/verify/cleanup), not
to implement it: neither playbook is dispatched by NetHub, both still
`pause` for confirmation, and per-host results are still `debug` output
rather than rows. The image moves in whichever direction
`image_transport` selects (design doc §4.3.1) — `push_scp` is the
default, `pull_sftp` the alternative — so don't describe the staging
playbook as doing only one of the two.

- **Never name NetHub or the design document inside `ansible/`.** No
  "NetHub renders this", no section numbers, no `artifacts` table — in
  comments, `name:` strings, or `fail_msg` text. The playbooks must read
  as a standalone Ansible project that happens to take its inventory from
  somewhere; rationale belongs in `design-document.md`, not in the layer
  that is meant to have no dependency on it. `ansible/inventory/`'s docs
  are the exception — they describe the contract with NetHub, so they
  name it.
- **Keep comments to 2–3 lines.** Say what the file does and what would
  bite someone changing it; leave the argument in the design doc. The
  `## in`/`## out` headers at the top of each task file stay — they are
  the contract between task files and are worth the lines.
- **Shared logic lives in `ansible/tasks/`, not roles.** There is no
  `iosxe_facts`/`batch_summary` role anymore:
  `tasks/collect_iosxe_facts.yml`, `tasks/resolve_target_bundle.yml`,
  `tasks/check_disk_space.yml`, `tasks/transfer_image.yml`,
  `tasks/push_image_scp.yml`, and `tasks/pull_image_sftp.yml` are plain
  `include_tasks` files, each with an `in`/`out` comment naming the vars
  it reads and sets. `tasks/stage_image.yml` is gone — it was split into
  the three transfer files. Both playbooks still duplicate their own
  preview/summary plays rather than sharing them — see "Planned
  improvements" below.
- **`tasks/transfer_image.yml` is the dispatcher and owns everything
  transport-independent.** It asserts `image_transport` is one of the
  two known values (an unrecognised value would skip both adapters and
  leave the host with no image, which the `verify` would then report as
  a confusing verification failure), includes one adapter, and then runs
  `verify /sha512` — the third consumption of the ingest digest (design
  doc §3.4), unchanged by direction. The `wait_for`/`assert` pair checks
  for the expected SHA-512 digest itself, not the presence of the word
  "Verified" — a bare substring match on one English word would pass on
  any device message containing it for an unrelated reason, silently
  turning that third consumption into a no-op. Don't revert to a
  `contains Verified` check.
- **`tasks/push_image_scp.yml` brackets the whole push in one
  `block`/`rescue`/`always`, and an unconfirmed restore now fails the
  host.** It reads `show running-config | include ^ip scp server` before
  touching anything, enables the server only if it wasn't already,
  pushes with `net_put` (`protocol: scp`, `paramiko`), and restores the
  captured state in `always:`, confirmed by re-reading the
  running-config rather than trusted from the config module's exit
  status. An unconfirmed restore sets `staging_result.success: false`
  with status `scp_not_restored` and calls `end_host` — the earlier
  debug-only warning was a known gap against the hard rule and is
  closed. `transfer_image.yml`'s final success task is additionally
  gated on `scp_restore_confirmed | default(true)` as a backstop.
- **`tasks/pull_image_sftp.yml` is one `cli_command` and no device
  config change.** It issues `copy sftp://user@host/dir/file <flash_dir>`
  and answers the device's `Destination filename` and `Password:`
  prompts, `no_log: true`. Naming only the filesystem as the destination
  is deliberate (forces the first prompt so the pair is deterministic).
  The prompt sequence is **unverified against a real device** — treat it
  like `net_put`'s connection type before it was tested, and record the
  answer when someone runs it. For hand runs the password comes from
  `DISTRIBUTION_PASSWORD` in the environment rather than `-e`,
  which would put it on the command line.
- Each playbook has exactly one `pause` now, in its own preview play,
  before any device connection is opened. The old single-file playbook
  also paused immediately before touching the SCP server, calling out
  that this step mutates and later restores device config; that
  second pause didn't carry over into the split and isn't in either
  file today. Worth restoring in the stage playbook specifically, since
  it's still the one step here that changes something on the device
  besides the upgrade itself.
- Each playbook runs its own preview play, its own `serial`-gated main
  play (`stage_serial`/`install_serial`), and its own inline summary
  play — `staging_result`/`install_result` are set early enough that the
  summary's per-host lookup never hits an undefined key, even for a host
  that fails a pre-check and never reaches the "record result" task.
  Per-host state moves to `upgrade_run_hosts` under the phase model,
  which is what eventually retires all of this.
- **`install_cisco_upgrade.yml` checks image presence via `dir`, not a
  fresh `verify /sha512`, before `install add`.** Design doc §8.1 is
  explicit that "still verifies" for a later phase has to mean
  re-running `verify /sha512`, not stat-ing the file — a file of the
  right name isn't the file staging checked. Since staging and install
  are now genuinely separate playbook runs (not just separate blocks in
  one play), a host whose staged image failed verification but is still
  sitting in flash under the target filename can pass the install
  playbook's presence check and get installed anyway. Tracked below.
- **`tasks/resolve_target_bundle.yml` measures the image itself; a
  rendered `file_size` is a cross-check, not a dependency.** One
  `stat` (`delegate_to: localhost`, `connection: local`,
  `get_checksum: false` — a sha1 pass over ~500 MB to learn a size is
  not free) against `software_registry.search_dir`. If the registry also
  declares `file_size` and the two disagree, the run stops before any
  transfer: the bytes on the mount aren't the bytes the registry
  describes, and the device-side `verify /sha512` would only catch that
  after moving the whole image. If only one is available, it's used; if
  neither, it fails. The motivation is decoupling — a task file that
  can't resolve a size without NetHub having rendered one is a NetHub
  dependency in the layer that's meant to have none.
  - **This is not the old three-tier cascade coming back**, and don't
    let it grow back into one. That was `software_bundle.file_size` → a
    per-run `localhost` cache via `delegate_facts` → a remote stat
    shelling out to `files/remote_image_size.py` (referenced but never
    present in this repo), preferring the declaration and discovering
    only in its absence. This is one local stat plus one fallback, in
    the opposite order, with no cache, no helper script and no remote
    execution.
  - **There is no remote tier because there is no remote filesystem**,
    for the same reason there's no `remote_dir`. Push reads the image
    off the EE's read-only mount; pull has the SFTP daemon export that
    same path. "The image is on the distribution host" names the address
    the *device* dials, not a second filesystem. If §10's mirror
    question is ever answered yes, that stops being true and the
    declared-`file_size` fallback becomes load-bearing again.
  - NetHub still computes `file_size` at ingest and still renders and
    snapshots it (design doc §5, §8): §8's per-host stage bound is
    derived from it **at dispatch**, before any play runs, so the
    playbook measuring the file itself doesn't remove NetHub's need for
    the value.
- **Removed: `remote_dir`, end to end.** It addressed a device's own
  `copy sftp://…` path under the old pull design — a remnant from when
  the distribution host could have been a separate remote machine (design
  doc §3.3). Both transports address the source by filename under
  `software_registry.search_dir` — push off the EE's mount, pull via the
  path the SFTP daemon exposes, which must be that same path — so
  there's still no per-artifact directory to name.
  `software_bundle.remote_dir` no longer exists in the schema or the
  rendered registry, and the playbooks no longer resolve it. Don't
  reintroduce it; the return of a pull mode is not a reason to.
- There is no `become` in either playbook and there must not be one.
  NetHub requires privilege 15 at login (design doc §4.3). Note the
  design says pre-check asserts this via `show privilege`; **the
  committed playbooks assert nothing of the kind** — there is no
  pre-check assertion task file at all, which is also why the
  pull-transport `ip ssh source-interface` prerequisite (design doc
  §4.3.1) has nowhere to live and is documented as an operator
  prerequisite instead. Don't add a lone `ip ssh source-interface`
  check. Adding `ansible_become: true` back
  would reintroduce an enable secret the credential path deliberately
  does not carry.

### Planned improvements

These playbooks are expected to keep changing; the two-file split is a
step toward §8.1, not the end state. Known gaps, roughly in order of
how much they matter:

- Have `install_cisco_upgrade.yml` re-run `verify /sha512` on the staged
  image before `install add`, instead of trusting a `dir` presence
  check.
- Extract the per-host summary/report logic (`Build per-host status
  map` → `Print batch summary` → `Fail the run if any host failed`),
  currently duplicated verbatim between both playbooks, into a shared
  task file.
- Extract the "is the image already present in flash" check into a
  shared task file — both playbooks run a near-identical `dir`/
  `regex_search` check for a different purpose (skip-if-staged vs.
  fail-if-missing).
- Run `tasks/pull_image_sftp.yml` against a real device and correct its
  prompt list from what IOS-XE actually emits (design doc §10). A wrong
  list hangs until the task timeout instead of failing fast.
- Restore the `pause` that used to sit immediately before the SCP server
  is touched — it didn't carry over into the split, and under the push
  transport it's the one step that mutates device config outside the
  upgrade itself. Not needed under pull, which mutates nothing.

## Keeping this file current

This file is derived from `design-document.md` plus the actual state of the repo,
and it goes stale silently. When a change lands that alters what a future
session needs to know — a design decision, a command, a dependency, a new
directory, a rule about what must not be built — update this file in the
same pass. The `design-doc-sync` skill in `.claude/skills/` covers what to
check and what belongs here.
