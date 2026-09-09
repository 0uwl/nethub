# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project status

**A decided migration is in progress: Ansible is being replaced by
Netmiko.** `netmiko.md` is the handoff document, and it should be read
before acting on "Ansible playbook notes" below or on any of the
Ansible-specific hard rules — it records which of them dissolve, which
survive under a different mechanism, and which are untouched. It is a plan
with a numbered build order, not a description of finished work: steps 1–5
are built and tested — the device layer
(`nethub/devices/{facts,connection,transfer,install,phases}.py`) validated
end-to-end against real hardware including two live upgrades, and step 5's
schema, dispatcher and credential socket tested but never yet run against a
device. Steps 6–8 are not started, and **nothing creates a job row**: there
is no submit or approval route, so the queue the sibling works is one only a
test fills. That is the missing half of step 5 and it is route work. Nothing under `ansible/` has been deleted — that
is step 6 — so both layers are in the tree at once and this file describes
both. See "Device layer" below for what exists on the Netmiko side.

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
admin-driven user creation (`nethub/auth.py`), and a multi-registry
publish flow. NetHub tracks any number of `software_registry`-bearing
files rather than one hardcoded file: an admin bind-mounts each file
somewhere under `REGISTRIES_ROOT`, then registers it from the
Registries settings page (`nethub/registry_routes.py`'s `registries_bp`)
— NetHub reads the file for an existing `software_registry` key
(adopting its entries if there are any) or collects a `search_dir` and
writes a fresh one in. Each registered file is a `Registry` row
(`nethub/models.py`) — a pointer NetHub owns, not a copy of the data;
the file and its images stay the admin's. Entry-level publish/delete
(`nethub/registry.py`, `nethub/registry_routes.py`'s `registry_bp`,
scoped under `/registries/<id>/entries`) is otherwise the same flow as before: an
uploaded image plus a typed-in checksum become a new entry, deletable
from the same list page. Entry delete is a hard, unaudited removal (the
entry and its image file) — no `state`/`superseded_by_id` machinery,
matching alpha's existing no-supersede stance on adds; *registry* delete
(forgetting a `Registry` row) is deliberately the opposite — it never
touches the file or its images, only NetHub's own pointer to them.
Because the registry YAML is hand-editable on disk (there's no
`artifacts` table behind it — see below) and, per the settings flow
above, may not even originate from NetHub, every route that reads or
writes it treats both a broken file and a hostile one as recoverable,
not fatal: `registry.load_registry()` raises `RegistryError` (flashed,
not a 500) on invalid YAML or a `software_registry` key that isn't a
mapping; `registry._save_registry()` is a path-escape-guarded,
read-modify-write, atomic (temp file + `os.replace`) save that preserves
every sibling key in the file (`image_transport`, `distribution_host`,
...) rather than overwriting the whole document; a `file_name` read back
out of the file is revalidated through `secure_filename` before
`delete_entry`/`check_registry` will touch a path built from it, since a
hand-edited value there is exactly as untrusted as one a submitter typed
in; and `registry.check_registry()` — wired to a "Check registry"
button, not run implicitly on page load, since it hashes every
registered image on disk — walks every entry, silently re-deriving a
stale `file_size` (cheap, non-security, always recoverable from the file
itself) and flagging anything it can't safely fix on its own: a missing
file, a checksum that no longer matches the bytes on disk, a malformed
or incomplete entry. It never guesses at a wrong checksum. `alpha.md` is
that slice's plan and
records its deliberate deviations from `design-document.md` — no
`artifacts` table, no Ansible dispatch, no `registry_jobs`/git-committed
registry, sessions are Flask-Login's signed cookie rather than a
`sessions` row (§4.5); the multi-registry `Registry` table is an alpha
addition with no design-doc counterpart, not a stand-in for one.
Provisioning (day-0) is entirely unimplemented.
The playbooks under `ansible/` are hand-invoked scaffolding, never wired
to anything NetHub provides — "Ansible playbook notes" below describes
them as committed, but they are now a layer being retired rather than
one being finished, and `netmiko.md` is where they are headed. Treat
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
                                   # Flask-WTF, PyYAML, gunicorn, pytest,
                                   # netmiko, ntc-templates

export SECRET_KEY=<any-string>    # required; nethub/config.py raises ValueError without it
flask --app nethub run            # runs the dev server (DEBUG defaults off; --debug to override)

flask --app nethub create-admin <username>   # bootstrap the first login user --
                                              # there is no self-registration route

pytest                             # runs tests/ -- see tests/conftest.py for the
                                    # app/client fixtures (temp DB + registry root per test)

python scripts/check_device_facts.py <host> --user <name>   # capture from a real
                                    # device and report what parsed; the password comes
                                    # from NETHUB_DEVICE_PASSWORD or an interactive prompt
python scripts/check_device_facts.py --replay <dir>         # re-parse a capture, no device

ruff check .                       # Python lint (pyproject.toml: 100-char lines,
                                    # N999 ignored for nethub/gunicorn.conf.py --
                                    # gunicorn requires that exact filename)
yamllint .                         # YAML lint (.yamllint.yaml: default ruleset minus
                                    # document-start/truthy, which the Ansible content
                                    # doesn't follow; line length capped at 150)
ansible-lint ansible/              # Ansible lint, gated at `profile: min` (.ansible-lint)
                                    # -- `basic` flags stylistic choices that are
                                    # deliberate here (see "Ansible playbook notes")
```

Tests cover `nethub/{credentials,models,bootstrap,auth,registry,registry_routes}.py`
(`registry_routes.py` holds both the `registries_bp`/`registry_bp` blueprints)
end-to-end through Flask's test client (login flow, CSRF disabled in the `app`
fixture, registry-row creation/adoption, entry upload/checksum validation), plus
`nethub/devices/{facts,connection,transfer,install,phases}.py`, `nethub/sibling.py` and
`nethub/credential_socket.py` — most of which need neither those fixtures nor a
device, parsing the real output under `tests/captures/` and exercising the
host-key policy against the same device's public host key. The
`make_registry` fixture in `tests/conftest.py` (mirrors `make_user`) writes a
file under a temp `REGISTRIES_ROOT` and creates its `Registry` row. `pyproject.toml`'s
`[tool.pytest.ini_options] pythonpath = ["."]` is why bare `pytest` can
`import nethub` — without it only `python -m pytest` (which puts the cwd on
`sys.path` itself) could.

`.github/workflows/ci.yml` runs on every push/PR against `main`: a `lint`
job (`ruff`/`yamllint`/`ansible-lint` from the block above, plus `Containerfile` — not
`Containerfile.dev`, which is dev-only — via the `immanuwell/dockerfile-roast`
action also used by Drawbridge), a `test` job (`pytest -v`), and a `publish`
job that builds and pushes `ghcr.io/<repo>:latest` (linux/amd64+arm64) on
push to `main` once both prior jobs pass. `ansible-lint` needs
`cisco.ios`/`ansible.netcommon` installed to resolve the playbooks' FQCN
modules (no `requirements.yml` declares them), so the lint job installs
both explicitly before running it.

The Ansible playbooks (`ansible/playbooks/stage_cisco_upgrade.yml`,
`ansible/playbooks/install_cisco_upgrade.yml`) are currently invoked by hand,
against a network device inventory not present in this repo:
```bash
ansible-playbook ansible/playbooks/stage_cisco_upgrade.yml -e stage_serial=1
ansible-playbook ansible/playbooks/install_cisco_upgrade.yml -e install_serial=1
```
They target Cisco IOS-XE devices (`cisco.ios` collection, `network_cli`
connection) and expect each host to define a `software_bundle` var
(filename, sha512, version, file_size) — see §5/§8 of
`design-document.md`. The staging playbook also reads `image_transport`
(`push_scp` default, or `pull_sftp` plus `distribution_host` /
`distribution_user` and a password from
`DISTRIBUTION_PASSWORD`); it lives in
`ansible/inventory/group_vars/all.yml` and is deployment-level,
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
| `REGISTRIES_ROOT` | `<repo root>/instance/registries` | Root directory an admin bind-mounts registry files into — every `Registry.file_path` is resolved (and path-escape-checked) relative to this. Replaces the old `IMAGE_DIR`/`REGISTRY_FILE` pair; there is no separate images root, since each registry's own `search_dir` (set from the Registries settings page, not an env var) is typically a much larger, independently-mounted volume. |
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

**Swapping SHA-512 for MD5 was considered on 2026-09-09 and rejected;
this is settled** (design doc §3.4, and a resolved entry at the head of
§10). It is worth repeating here because the rule above answers
a *different* question — it forbids a second algorithm, and says nothing
about changing which single one is used, so the swap reads as permitted
on a first pass. Three findings, in the order they mattered:

- **The apparent benefit does not exist.** Netmiko ships `compare_md5()` /
  `verify_file()`, so MD5 looks like it would let NetHub delete its own
  verification code. It would not: `compare_md5` compares the file *on
  NetHub's mount* against the device, and NetHub needs the digest recorded
  in the table at ingest compared against the device. §7.2 says the table
  wins over the file, and a swapped file on the mount is one of the things
  this check exists to catch — netmiko's built-in verify trusts exactly the
  thing being verified. The compare stays ours either way, so the swap buys
  no code. (This is also why `_scp_put` passes `hash_supported=False` — see
  "Device layer".)
- **The speed argument is real but small.** Measured on a C9200CX against
  the 408 MB `cat9k_lite-rpbase.17.12.06.SPA.pkg`: `verify /md5` 18.5s,
  `verify /sha512` 33.9s — about 1.8×. Extrapolated to a 1.2 GB image
  that is roughly 55s versus 100s, and an upgrade verifies twice (after
  staging, again before `install add`), so ~1.5 min per device against an
  activate-and-reload of 5–10 min. Don't re-measure this to re-open the
  question; re-measure it only if the numbers stop being plausible.
- **The security difference is specific rather than generic, and the
  generic version of the argument is wrong.** Substituting bytes to match
  an already-recorded digest is a *preimage* attack, and MD5's preimage
  resistance is intact — so "MD5 is broken" does not on its own decide
  this. What decides it is *chosen-prefix collisions* (practical since
  2019): an attacker who supplies the image to an admin can hand over a
  benign image prepared to collide with a malicious one, let NetHub ingest
  the benign one, and substitute later. Two places in this design have no
  backstop if that works — under pull the device does not verify the
  distribution host at all, so the digest is the only control (§4.3.1),
  and day-0 names payload hash verification as one of three compensations
  for deliberate plain HTTP (§4). Large binaries with slack space are good
  collision carriers.

The reopening condition is unchanged and is the one the rule above already
states: a second platform that only offers MD5. That would be an *added*
algorithm with the column §3.4 refuses today — a different decision from
this one, argued on its own terms.

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
device identity) backs a fail-closed check. **First contact is not
TOFU** — pinning fails closed only on a *changed* key, and "an operator
names a machine they control" is always a *first* contact, so an address
with no already-confirmed row cannot be named by a run at all.
Confirming one is a separate, explicit admin action — connect with no
device credential, show the fingerprint, record `confirmed_by` —
decoupled from any run's submit or approval flow, which pre-check
(running on submit, with no gate) would otherwise swallow. §4.3 says so
in as many words; an earlier version of it put first contact at the
submit gate, and that wording is superseded. These rows are deliberately
exempt from §7.4's retention purge — expiring one silently downgrades a
fail-closed mismatch back to a first-contact prompt. §2's non-goal
names this exception explicitly so nobody "fixes" it later. The check
itself is built — `nethub/devices/connection.py`, see "Device layer" —
but the table, the admin confirmation screen, and the rendered
`known_hosts` §4.3 describes are not; under Netmiko the last of those is
a policy object rather than a file.

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
  the `paramiko` connection type, in `ansible/playbooks/tasks/push_image_scp.yml`.
  IOS-XE has no SFTP *server* (client only), so a push has no SFTP
  option — SCP is the only wire protocol available in that direction.
  `pull_sftp` is the alternative: one `copy sftp://…` on the device's own
  CLI in `ansible/playbooks/tasks/pull_image_sftp.yml`, driven with
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

## Device layer (`nethub/devices/`)

Ordinary Python driving Netmiko — what replaces the playbooks (`netmiko.md`).
Four of that plan's five modules exist:

- `facts.py` — `show version`, `dir` and `show privilege`, parsed with
  ntc-templates where a template exists.
- `connection.py` — the only way any NetHub process opens a device session.
- `transfer.py` — `stage_image()` plus the two transport adapters.
- `install.py` — the activate/reload/verify/cleanup half.

- `phases.py` — the per-host loop, the exception→`failure_stage` mapping, and
  the rows.

All five of the plan's modules now exist, and `nethub/sibling.py` dispatches
them. What does not exist is anything that *creates* a job: no submit route,
no approval route, and nothing that puts a credential into the store. The
sibling works a queue only a test fills.

`facts.py`, `connection.py` and `transfer.py` have been exercised against a
real Catalyst 9200CX on IOS-XE 17.12.06, the push adapter included: enable, transfer, confirmed
restore, `verify /sha512` against the ingest digest, and the
skip-if-already-staged path. **Push throughput is the number to plan
against: ~1.4 MB/s**, measured pushing 408,739,840 bytes in 319.9s
end-to-end (less a 33.9s verify pass). That is device-bound, not
link-bound — a full 1.2 GB image is roughly 15 minutes per device, which
is what §8's per-host stage bound has to be calibrated against, and it is
also why staging many devices at once costs NetHub's link little (twenty
concurrent devices is ~28 MB/s). Untested: the same push across a
constrained WAN link, where the bottleneck moves. The pull adapter has
not been run against a device at all — its prompt sequence remains the
open item.

**`install.py` is fully validated too, including the reload.** A round trip
was run on the lab switch — 17.12.6 → 17.12.08 → 17.12.6, both directions
through `stage_image` → `activate` → `wait_for_device` → `verify_upgrade` →
`cleanup`. That also closes `netmiko.md` build step 1, which had asked for
the reconnect loop to be spiked against real hardware and never was.

Timings, consistent across both runs and worth planning against:

| step | duration |
|---|---|
| stage 471 MB over SCP | ~370s (1.28 MB/s) |
| `install add … activate commit` | 605–622s |
| reload → CLI serving again | 228–238s |
| `install remove inactive` | ~5s |

Three things the real runs settled that the ported code had guessed at:

- **`install add … activate commit` does not drop the session.** It runs the
  whole add/activate/commit and returns `SUCCESS` with the session still up,
  about ten minutes in, and only *then* reboots. `activate()` handles a lost
  session as well, but that branch is unobserved — it exists because a reload
  taking the session is indistinguishable from a network drop, not because
  that is what happens.
- **The reload deadline has ample headroom.** 228–238s used against a 900s
  default. Untested: a stack, or a device slower than this one.
- **Version normalisation is load-bearing, not cosmetic.** The device reports
  `17.12.8` where the target was `17.12.08`. A string comparison in
  `verify_upgrade` would have failed a *successful* upgrade with
  `wrong_version`; `facts.same_version` is why it did not.

**Parsing is split from connecting so device output can be re-parsed with no
device.** `check_device_facts.py <host>` captures `show version` / `dir` /
`show privilege` to a directory and reports what parsed; `--replay <dir>`
re-parses one offline. That split is what makes `tests/captures/` possible,
and validating a new IOS-XE release means capturing it and replaying it, not
reading the template.

**`tests/captures/` is evidence, not fixtures.** Each directory is verbatim
output from real hardware (`c9200cx-12p-2x2g-17.12.06`: a Catalyst 9200CX on
IOS-XE 17.12.06, INSTALL mode, plus that device's public SSH host key and the
fingerprint `ssh-keygen -lf` prints for it). Editing a capture to make a test
pass destroys the only evidence that ntc-templates parses what this fleet
actually runs. Add a directory per release; synthetic samples belong in the
test file, labelled there as synthetic.

Before changing `facts.py`:

- **`boot_mode` is derived, not read.** ntc-templates does not expose `show
  version`'s per-switch `Mode` column, so INSTALL/BUNDLE comes from the boot
  file — `packages.conf` versus `.bin`. An unrecognised boot file yields `""`,
  which means *refuse*, not BUNDLE: `install add … activate commit` is an
  INSTALL-mode procedure and a wrong guess runs the wrong one.
- **`parse_dir` excludes directories.** They carry a size too, and `size_of`
  answers "is the staged image here, at the right length" — a directory must
  not be able to answer that. On the captured device it is 20 of 48 entries.
- **Nothing defaults on a parse miss.** The free-space number gates the stage
  phase and a silently-wrong one fills a device's flash, so a parse failure
  raises `FactsError` rather than returning a zero or a guess.
- **`facts.py` parses; it does not police.** `get_privilege` returns the
  number, and the "must be 15" refusal (design doc §4.3) belongs to the phase.

`connection.py` carries the security properties, and each of these is easy to
undo by accident:

- **`connect()` takes the pinned host key as a required positional argument.**
  That signature *is* the enforcement of design doc §4.3's rule that an address
  with no confirmed `device_host_keys` row cannot be named by any run: there is
  no path to a device without a pin and no TOFU branch in the module.
  `scan_host_key()` is the separate admin confirmation action — key exchange
  only, so it spends no device credential — and it deliberately **writes
  nothing**, because persisting what it fetched would rebuild the silent pin
  §4.3 exists to refuse.
- **The client loads no host keys from disk, and that is the mechanism.**
  Paramiko consults a missing-host-key policy only for a host it has no loaded
  key for, so `_build_ssh_client` loads none: every connection reaches our
  policy and the comparison is ours. Restoring `load_system_host_keys()` would
  let a line in this host's `~/.ssh/known_hosts` switch the fail-closed check
  off silently, with no error anywhere.
- **`HostKeyError` must not subclass `paramiko.SSHException`.** Netmiko catches
  that around its own connect and re-raises it as `NetmikoTimeoutException`, so
  a host-key mismatch would reach an operator as "try increasing conn_timeout".
- **The device credential is a password; NetHub uses no SSH client key.** The
  pinned key is the *device's* identity, not ours. `use_keys` and `allow_agent`
  stay off and `connect()` takes no `**kwargs`, so there is no way to turn them
  on — verified by connecting with `HOME` pointed at an empty directory and no
  agent running.
- **IOS-XE with `aaa new-model` offers `publickey,keyboard-interactive`, not
  `password`.** Paramiko falls back to keyboard-interactive only when no
  password was supplied at all, so netmiko's ordinary password auth is rejected
  outright — and the error is a misleading `transport shut down or saw EOF`
  rather than anything about auth methods.
  `_SSHClientKeyboardInteractive` answers the prompt instead. **There is no
  auto-detection and there must not be one**: the device drops the session
  after a single failed attempt — a bare `auth_none` probe is enough — so
  try-then-fall-back cannot work on one connection. The method is a parameter
  the caller states (`DEFAULT_AUTH`).
- **No `secret` is passed and `.enable()` is never called.** Privilege 15 at
  login is the point (design doc §4.3); an enable secret reintroduces the
  second credential the design removes.
- The module's three exceptions are the three §7.3 `failure_stage` values a
  connection can produce — `hostkey`, `credential`, `connect` — so mapping them
  in `phases.py` is a lookup rather than a judgement.

`transfer.py` ports the three `tasks/*.yml` transfer files, with four
differences that are deliberate:

- **`verify_sha512` asks the device for the digest instead of handing it the
  expected one.** `verify /sha512 <file> <digest>` echoes the digest back, so
  a substring test against that output can pass on the echo alone — the same
  class of no-op as matching the word "Verified". Comparing in Python also
  lets a mismatch report what the device actually computed. Real output format
  is `verify /sha512 (flash:packages.conf) = <128 hex>`.
- **`_scp_server_enabled` matches whole lines.** The Ansible original tested
  `'ip scp server enable' in stdout`, and `no ip scp server enable` contains
  that string — an explicitly negated config read as enabled, and the bracket
  would have restored it backwards. It never bit because IOS omits the default
  from running-config, but don't reintroduce the substring test.
- **The unconfirmed-restore error is raised from `finally`,** so it supersedes
  an in-flight push failure and keeps it as `__context__`. A device left
  changed is the more urgent of the two facts. `_restore_scp_server` never
  raises: a restore that could not be attempted *is* an unconfirmed restore.
- **Skip-if-already-staged is a digest, not a `dir` presence check** — which
  is the first item on "Planned improvements" below, now moot for this path.
  A file of the right name is not the file staging checked (design doc §8.1).

Two things about it that look like they could be simplified and cannot:

- **`_scp_put` passes `hash_supported=False`, and that is not tuning.**
  Netmiko's transfer class MD5s the source file in its constructor whenever
  that flag is left on — including under `file_transfer(disable_md5=True)`,
  which only skips the *comparison*. Left on, every push does a ~1.2 GB MD5
  pass computing a digest nothing reads, in a system with one hash algorithm
  (see the SHA-512 rule above, which records why MD5 stays rejected).
- **`netmiko.file_transfer()` is not used at all**, for the same reason plus
  its verification comparing the mount against the device rather than the
  table against the device.

The SCP put opens a *second* SSH session, and `SCPConn.establish_scp_conn`
builds it with `ssh_conn._build_ssh_client()` — our override — so the pinned
host-key policy and keyboard-interactive auth cover both connections. Design
doc §10 asks whether the host-key question applies to both independently;
under Netmiko it does, and both are answered by construction.

`install.py` is driven by the phase model rather than written as one
procedure, and five things in it are load-bearing:

- **`wait_for_device()` takes a connection *factory*, not a connection.** The
  reload takes the session with it, so there is nothing to reuse — and the
  factory is where the caller supplies the credential and the pinned host key,
  which keeps both out of this module.
- **A device answering SSH is not a device that is ready.** IOS-XE accepts
  connections while it is still coming up, so every reconnect attempt runs a
  real command before the connection is accepted, and one that answers but
  cannot be used is closed rather than returned.
- **A lost session during `install add` is the expected ending, not an
  error** — but a *clean* return that does not say `SUCCESS` is a failure.
  IOS-XE reports some install failures in-band with the session still up, and
  treating that as "rebooting" would burn the whole reload deadline before
  reporting something visible immediately.
- **The pre-activate image check is a `verify /sha512`, never a `dir`.** This
  is the §8.1 requirement the playbook missed: a host whose staged image
  failed verification is still sitting in flash under the target filename.
  There is a test asserting no `install add` is issued in that case.
- **A declined `install remove inactive` still prints `SUCCESS:
  install_remove`.** Found by answering `n` to a real device before trusting
  the accept path: the marker means "the command finished", not "files were
  removed", and the transcript carries `User Rejected Deletion` instead.
  `cleanup()` tests for the rejection *before* the success marker, and returns
  the parsed list of what actually went. Don't reorder those two checks.
- **`write memory` comes first, and that is why the stage phase's SCP restore
  has to be *confirmed*.** This is the write that would carry an un-restored
  `ip scp server enable` into startup-config (design doc §4.3.1) — the two
  rules are one mechanism seen from two phases, so don't weaken either alone.

Guards run before anything is written, which is what makes a refusal safe to
re-run: `assert_ready_to_activate` issues only reads.

## Dispatch (`nethub/sibling.py`, `nethub/credential_socket.py`)

The schema is in `nethub/models.py` — `device_host_keys`, `upgrade_runs`,
`upgrade_run_hosts`, `upgrade_phase_jobs`, `upgrade_host_phase_results`, with
§7.3's vocabularies as module constants. Two deviations from §5, both because
the EE is gone: there is no `private_data_dir`, and `playbook_log_path` is
`log_path`. `upgrade_run_hosts.artifact_id` is a bare integer, not a foreign
key — there is no `artifacts` table until step 7.

**Two constraints were silently absent and are easy to lose again.**

- **SQLite ignores `FOREIGN KEY` unless asked, per connection.** §5 leans on
  the two keys on `upgrade_host_phase_results` rather than treating them as
  documentation, so `nethub/extensions.py` sets `PRAGMA foreign_keys=ON` on
  the generic SQLAlchemy `Engine` connect event — on the engine and not
  per-app, so the test fixtures get it too. A database that enforces less
  than production passes tests production would fail. There is a test
  asserting the pragma is on, because every other FK test passes vacuously
  without it.
- **`Enum(create_constraint=...)` has defaulted to False since SQLAlchemy
  1.4.** Found by reading the emitted DDL, not by trusting the declaration:
  every vocabulary was a plain `VARCHAR` with no `CHECK`. `_enum()` passes
  `create_constraint=True`. If a column starts accepting a typo'd value,
  check the DDL rather than the model.

The terminal-status trigger (§7.3) is real DDL attached to the table's
`after_create`, and it raises `IntegrityError` — not `OperationalError`.

**`phases.py` never lets a foreign exception message reach a row.**
`error_summary` is retained for a year (§7.4), and §7.3 warns that a stray
`str(exc)` there is a durable credential leak with no other symptom. Only
exceptions this codebase raised itself get their message copied; anything
else contributes its type and nothing more. There is a test that raises a
`RuntimeError` containing the password and asserts it reaches no column.

**Cancel and the deadline are checked between hosts, never mid-host** — there
is no safe place to stop inside an activation, and polling more finely would
not create one.

**`phase_activate` owns the reconnect**, not `phase_verify`: a device that
never returns is a `reload` failure, a different `failure_stage` and a
different conversation than a wrong version.

**SQLite returns naive datetimes for values written aware.** Comparing
`now()` against a stored `deadline_at` raises `TypeError` for any row read
back from the database — always in production, never in a test that skips the
round trip. `phases._aware()` and `sibling._aware()` are the fix. This is
latent everywhere else timestamps are stored and only bites where something
compares; don't add a comparison without routing it through one of them.

**The sibling's claim is the conditional update itself.** `UPDATE ... WHERE
id=? AND status='queued'` with the rowcount as the answer: a read-then-write
double-claims under WAL, and nothing enforces that only one sibling runs
(§9.1). `runner_instance_id` is a UUID minted per start and never a PID.

**The credential socket: Flask serves, the sibling connects.** Not the
reverse — a deposit endpoint would leave the sibling holding secrets for
executions it has not started, and could be flooded. Four things about
`nethub/credential_socket.py` are load-bearing:

- **Nothing in it calls `bind()`.** `systemd_socket()` adopts the descriptor
  a `.socket` unit handed over, and returns `None` when the process was not
  socket-activated — the caller then skips serving rather than creating a
  path with the wrong ownership. Every dev run and every test takes that
  branch; the tests make their own socket, which is why none of this is
  exercised in-process by `create_app`.
- **There is no `SO_PEERCRED` check and there should not be one.** Under one
  rootless user a uid check tells Flask only "the peer shares my uid", which
  anything a Flask compromise spawns also satisfies. The mount is the
  authenticator (§9.2). Don't add a uid check and believe it discriminates.
- **The store is keyed by `upgrade_phase_jobs.id`, never `run_id`**, and the
  approving identity is cross-checked on release. Keying by run would
  eventually hand one person's password to another person's approved
  execution against an address that person chose.
- **The reply is untrusted input on the sibling's side.** A compromised Flask
  chooses those bytes and the sibling is the privileged side, so
  `fetch_credential` caps, deadlines and allowlists what comes back before it
  reaches any variable or command string.

**`verify_running` runs on the serving thread and needs its own app
context.** Flask-SQLAlchemy's session is thread-local, so without one every
request fails with a generic refusal that says nothing about the cause. The
wiring in `nethub/__init__.py` does this; a test that forgot it is how it was
found.

The sibling is `python -m nethub.sibling`, reading `NETHUB_CREDENTIAL_SOCKET`
and `NETHUB_SEARCH_DIR`. It builds a bare Flask app for SQLAlchemy's context
rather than calling `create_app()` — that would register routes and run the
first-boot admin bootstrap, and the sibling must do neither.

**This closes one of design doc §10's open questions, which has not been
revised to say so.** §10 asks where NetHub's rendered `known_hosts` actually
takes effect, since Ansible's connection plugins don't all read one from the
same place. Under Netmiko the paramiko client is ours: there is no rendered
`known_hosts` and no question — the check is a policy object in
`connection.py`.

## Ansible playbook notes (`ansible/playbooks/stage_cisco_upgrade.yml`, `ansible/playbooks/install_cisco_upgrade.yml`)

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
- **Shared logic lives in `ansible/playbooks/tasks/`, not roles.** There is no
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

**Read this against `netmiko.md` first.** These were written when the
playbooks were the plan; the decision to replace them with Netmiko means
most of this list is work on a layer scheduled for deletion at build step
6. Nothing here should be started without deciding it is still worth
doing — the pull-adapter prompt list is the one item whose answer
outlives the playbooks, since `transfer.py` needs it too.

Known gaps, roughly in order of how much they mattered:

- Have `install_cisco_upgrade.yml` re-run `verify /sha512` on the staged
  image before `install add`, instead of trusting a `dir` presence
  check. (Done on the Netmiko path — `transfer.py`'s skip-if-staged test
  is a digest. Only the playbook still has the gap.)
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
