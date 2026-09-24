# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

**Active plan: read `PLAN.md` before starting any work.** It is the agreed
direction from the 2026-09-23 review, split into workstreams that each get
their own branch. Where it conflicts with this file, `PLAN.md` wins: several
hard rules below are scheduled to change, and parts of this file are known to
be wrong (listed under the plan's WS-0).

## Project status

**The Ansible-to-Netmiko migration is finished.** Device work is ordinary
Python driving Netmiko in `nethub/devices/`; `ansible/` is gone entirely,
along with every trace of `ansible-runner`, the EE, and the rendered
inventory it used. The device layer is validated end-to-end against real
hardware (two live upgrades, plus a live pre-check driven through the
Flask app), so there is only one layer in the tree now, and it has been
exercised, not just written. The one thing not yet done on the day-2 path
is a full app-driven run past pre-check — stage/activate/verify/cleanup
are each hardware-validated through the device layer, but not yet in one
run started from the UI. The migration's own handoff document
(`netmiko.md`) is gone too, once its build order and reasoning had all
either landed in code or been folded into this file and
`design-document.md` — read the hard rules below directly rather than
looking for a supersession table.

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
first slice of the Software Lifecycle module: local username/password auth
(everyone who can log in is an admin — no roles, no OIDC), admin-driven user
creation (`nethub/auth.py`), an artifact store, and the day-2 upgrade path.
This file is now the sole record of that slice's deliberate deviations from
`design-document.md` — no OIDC, no roles, and sessions are Flask-Login's
signed cookie rather than a `sessions` row (§4.5) — superseding the earlier
`alpha.md`/`HANDOFF.md`, both deleted once their content moved here and into
`design-document.md` §10, since a completed planning/remediation doc left in
the tree is exactly the stale-and-conflicting risk this file exists to avoid.

**The login path is hardened but the mechanism has a schema cost worth
knowing.** `nethub/auth.py` verifies a password on *every* attempt — an
absent username is compared against a module-level `_ABSENT_USER_HASH` —
because the control flow was itself the oracle: a missing user returned
before any hashing and answered in ~1.4 ms against ~104 ms for a real one, a
74× gap, on a route whose CSRF token `GET /login` hands out
unauthenticated. Don't add an early return for a missing user, and don't
replace that constant with a cheaper hash: it must keep the same KDF
parameters, which it does by going through the same
`generate_password_hash`. A locked account gets the same single message as
a wrong password for the same reason — saying "locked" re-opens the oracle.
The online-guessing budget (`users.failed_logins`, `users.locked_until`) is
keyed on the **row, not the submitted username**, because §4.2 argues a
counter keyed on attacker-chosen input is unbounded growth in the shared
SQLite file; attempts against usernames that don't exist are therefore not
counted, which is only safe *because* the timing oracle is closed.
**`db.create_all()` does not ALTER an existing table**, and there is no
Alembic in this project, so those two columns arrive only on a fresh
database — the same limitation `users.device_username` already had. Migrate
by hand or recreate.
Provisioning (day-0) is entirely unimplemented.
The `ansible/` tree is gone (build step 6) along with `.ansible-lint` and
the `ansible-lint` CI job. Nothing in the repo runs Ansible any more; the
device layer below replaced it. Treat
`design-document.md` as the design document / target architecture, not
a description of current code — always verify a described component
actually exists before assuming it's implemented.

NetHub containerizes as a single-process image (`Containerfile` —
`python:3.12-slim`, gunicorn, one worker) plus a dev image
(`Containerfile.dev` — Flask's own dev server with reload, run via
`dev.sh` against a bind-mounted repo for live edits). **A deployment is
three units, not one**: `quadlet/nethub.container` (the UI),
`quadlet/nethub-sibling.container` (all device work — the same image at a
different entrypoint), and `quadlet/nethub-credential.socket` (§9.1's
channel between them, a plain systemd unit because Quadlet has no `.socket`
generator, so it installs to a different directory). See "Container" below
for the full environment-variable list and the systemd-credential
mechanism for `SECRET_KEY`/`ADMIN_PASSWORD`.

## Commands

```bash
pip install -r requirements.txt   # Flask, Flask-SQLAlchemy, Flask-Login,
                                   # Flask-WTF, gunicorn, pytest, netmiko,
                                   # ntc-templates -- all pinned to exact
                                   # versions, because CI resolves this file
                                   # fresh on every push to main and publishes
                                   # the result. PyYAML was dropped with the
                                   # YAML registry (build step 7); yamllint is
                                   # a CI tool, not a runtime dep.

export SECRET_KEY=$(openssl rand -hex 32)   # required; config.py rejects an absent
                                   # key, a known placeholder, or anything under 32 chars
flask --app nethub run            # runs the dev server (DEBUG defaults off; --debug to override)

flask --app nethub create-admin <username>   # bootstrap the first login user --
                                              # there is no self-registration route

pytest                             # runs tests/ -- see tests/conftest.py for the
                                    # app/client fixtures (temp DB + artifact store per test)

python scripts/check_device_facts.py <ip> --user <name> \
    [--fingerprint "<type> SHA256:..."]                      # capture from a real device
                                    # (a pinned, confirmed connection -- an IP literal only,
                                    # no TOFU) and report what parsed; the password comes
                                    # from NETHUB_DEVICE_PASSWORD or an interactive prompt.
                                    # Reads a confirmed pin from the database if reachable,
                                    # otherwise needs --fingerprint (get one via
                                    # `upgrade_cli.py --scan`) -- same resolution upgrade_cli.py uses.
python scripts/check_device_facts.py --replay <dir>         # re-parse a capture, no device

ruff check .                       # Python lint (pyproject.toml: 100-char lines,
                                    # N999 ignored for nethub/gunicorn.conf.py --
                                    # gunicorn requires that exact filename)
yamllint .                         # YAML lint (.yamllint.yaml). Only two YAML files
                                    # are left -- this config and the CI workflow -- but
                                    # document-start/truthy stay disabled for new
                                    # reasons: ci.yml has no `---`, and Actions' `on:`
                                    # key is a YAML 1.1 boolean.

python -m nethub.sibling           # the dispatcher; reads NETHUB_CREDENTIAL_SOCKET
                                    # and NETHUB_SEARCH_DIR (exits immediately naming both
                                    # if either is unset -- and needs SECRET_KEY too, since
                                    # it loads the same config.py). Runs as its own unit
                                    # (quadlet/nethub-sibling.container), never inside the
                                    # Flask process. NOTHING IS DISPATCHED WITHOUT IT:
                                    # scans and phase jobs sit at `queued` forever, with no
                                    # staleness story -- sweep() only reclaims `running`.

python -m nethub.upgrade_cli --scan <host>          # the manual escape hatch: print a
python -m nethub.upgrade_cli --host <host> --user <name> \
    --fingerprint "<type> SHA256:..." --image <path> --sha512 <hex> \
    --version <v> --phases all                      # ...fingerprint, or upgrade a device
                                    # with NetHub switched off entirely. Writes nothing.
                                    # See "Manual escape hatch" below before using it.
```

**`tests/test_templates.py` renders every page and is the only thing guarding
the templates.** There were no rendering tests at all — two route tests
checked a status code and nothing inspected a body — so a broken `url_for`, a
renamed context variable, or a CSRF token deleted from a form would all have
passed. It builds its own `WTF_CSRF_ENABLED=True` app, because the shared
`app` fixture disables CSRF and that is precisely what made a missing token
invisible; the check was verified by deleting a token and watching it fail,
not assumed. It also owns its `make_run` fixture rather than adding one to
`conftest.py`, since `conftest.py` is the file concurrent branches collide in.
Flash categories are asserted both ways: a success must not render
`alert-error`, and an *uncategorised* flash must still read as an error —
the default category is deliberately an error, because the remaining
uncategorised calls are refusals and downgrading them to a neutral notice
would mis-style real failures.

Tests cover `nethub/{config,credentials,models,bootstrap,auth,artifacts,artifact_routes,upgrade_routes}.py`
end-to-end through Flask's test client (login flow and lockout, CSRF disabled in
the `app` fixture — see the template-test note above for why that matters —
artifact ingest and its two uniqueness constraints, host-key confirm, submit and
approve refusals), plus
`nethub/devices/{facts,connection,transfer,install,phases}.py`, `nethub/sibling.py` and
`nethub/credential_socket.py` — most of which need neither those fixtures nor a
device, parsing the real output under `tests/captures/` and exercising the
host-key policy against the same device's public host key. `tests/conftest.py`
carries `make_user`, `make_artifact` (real bytes on disk, mirroring
`make_user`) and a fresh `ARTIFACT_STORE` per test; `make_run` lives in
`tests/test_templates.py` instead, because `conftest.py` is the file
concurrent branches collide in. `pyproject.toml`'s
`[tool.pytest.ini_options] pythonpath = ["."]` is why bare `pytest` can
`import nethub` — without it only `python -m pytest` (which puts the cwd on
`sys.path` itself) could.

`.github/workflows/ci.yml` runs on every push/PR against `main`: a `lint`
job (`ruff`/`yamllint`, plus `Containerfile` — not `Containerfile.dev`, which
is dev-only — via the `immanuwell/dockerfile-roast` action also used by
Drawbridge), a `test` job (`pytest -v`), and a `publish` job that builds and
pushes `ghcr.io/<repo>:latest` (linux/amd64+arm64) on push to `main` once both
prior jobs pass.

**The lint tools are pinned (`ruff==0.16.7`, `yamllint==1.38.0`) and the
reason is not tidiness.** They were installed unpinned, and ruff 0.16 widened
its *default* rule set to include isort and part of pylint/flake8-simplify —
so `ruff check .` passed locally against 0.15 and failed in CI on byte-identical
files, on three branches at once. A linter that changes what it enforces with
no commit blocks merges at random. **`ruff check .` locally therefore only
matches CI if your ruff is that version** — check `ruff --version` before
trusting a green local run, and when you do bump the pin, expect new findings
and fix them rather than unpinning. `publish` fires only on push to `main`, so
a PR gets `lint` and `test` as a free dry run before any image is built.

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
the single-worker hard rule below both assume exactly one process), sets
`worker_class = 'gthread'` with `threads = 4` — the *other* half of that
hard rule, and required rather than optional: gunicorn's defaults are
`sync`/`threads=1`, so leaving them unset made production single-threaded
and a 1.5 GB upload held the whole server, the exact §3.2 failure arriving
through ingest — and sets `control_socket_disable = True`: gunicorn ≥25.1 otherwise tries to
create `$HOME/.gunicorn/gunicorn.ctl` for a control socket nothing here
uses, which throws under the Quadlet unit's `ReadOnly=true`. Discovered
by actually running the built image read-only, not by inspection — if
gunicorn's default behavior changes again, re-check by running the
container rather than trusting this note.

`entrypoint.sh` is baked in as the image's ENTRYPOINT and exists for one
reason: to stop gunicorn's arbiter from mistaking the credential socket for
an HTTP listener. It is a pass-through for anything not socket-activated
(`dev.sh`, a plain `podman run`, the sibling unit, every test). The
mechanism and the failure it prevents are written out under "Dispatch"
below — read that before changing either the ENTRYPOINT line or
`systemd_socket()`.

**Running `nethub.container` alone gets you a UI that accepts work and
performs none of it.** Flask writes a `queued` row and stops there by design
(§9), so with no sibling unit a host-key scan and an upgrade both hang at
"Still working" indefinitely — and nothing surfaces it, because `sweep()`
only reclaims `running` rows and the scan result page renders `queued` and
`running` identically. If something never starts, check that unit first.
Start order matters once: systemd refuses to start the socket while
`nethub.service` is already running, so start the socket first (or just
start `nethub`, whose `Requires=` pulls it in). All three units, the
`Sockets=` hand-over, and a `systemctl restart` of each were verified
against a real `podman build`/`podman run` on podman 5.4.1.

`quadlet/nethub.container` is the reference Podman Quadlet unit (see
Drawbridge's own `quadlet/drawbridge.container` for the sibling
project's version of the same pattern). Verified end-to-end against a
real `podman build`/`podman run` — including `UserNS=keep-id`,
`ReadOnly=true`+`Tmpfs=`, and the systemd-credential path — not just
written from the Drawbridge example and assumed to work. **But that
verification predates build step 7 and the review fixes**: the unit still
set the deleted `REGISTRIES_ROOT` and never set `ARTIFACT_STORE`, so the
store defaulted onto the read-only image layer and the artifacts page
500'd on load. It now sets `ARTIFACT_STORE=/app/artifacts` with a volume
behind it, carries commented `DEVICE_TARGET_CIDRS`/`IMAGE_TRANSPORT`
lines, and adds `LimitCORE=0` plus `ProtectProc=invisible` to `[Service]`.
Those two *are* now verified against a real `podman run` under the unit's
own property set, and `LimitCORE=0` was confirmed to reach the container
process itself (`/proc/self/limits` reads `Max core file size 0`) rather
than stopping at the podman client. The rest of the post-step-7 shape
— `ARTIFACT_STORE` and its volume, the commented settings lines — has
still not been re-verified end-to-end. Three things worth knowing if this
file gets edited:

- **`NoNewPrivileges=` and `RestrictSUIDSGID=` must not be added to
  `[Service]`, and both were tried.** Everything in `[Service]` sandboxes
  the **podman client process on the host**, not the container — the
  container's hardening is `ReadOnly=`, the user namespace and the image's
  unprivileged UID, none of which these reach. What they do reach is the
  setuid-root binaries rootless Podman's own startup depends on, and each
  fails differently:
  - `RestrictSUIDSGID=true` seccomp-denies any `chmod` carrying
    `S_ISUID`/`S_ISGID`. Where the overlay store reports `Supports
    shifting: false` (`podman info`), `UserNS=keep-id` cannot use a native
    idmapped mount, so Podman materialises a *chowned copy* of every image
    layer — and restoring the setuid bit on that layer's own setuid files
    (`/usr/bin/chfn`, `/usr/bin/chsh`, shipped in `python:3.12-slim` via
    `passwd`) is denied. The unit dies before the container exists, with
    `storage-chown-by-maps: chmod usr/bin/chfn: operation not permitted`.
  - `NoNewPrivileges=true` stops `execve` granting privilege from the
    setuid bit, so `newuidmap`/`newgidmap` run unprivileged and cannot
    write `uid_map`. **This one is latent, which is why it is the more
    dangerous of the two**: once a rootless pause process exists Podman
    joins its namespace and never re-runs `newuidmap`, so the unit starts
    fine until the next reboot or `podman system migrate`, then fails with
    an error naming neither systemd nor the unit file.

  A store that supports shifting would sidestep the first; the second
  survives it regardless. Reasoning is written out in the unit file too,
  since that is where someone would reach for the directives.

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
| `SECRET_KEY` | none — required | Flask/Flask-Login session-signing key. A systemd credential named `secret_key` takes priority over this env var (`nethub/credentials.py`) — see the unit file's `[Service]` block. **`config.py` also refuses a known placeholder or anything under 32 characters**, not just an absent value: alpha has no server-side `sessions` row (§4.5), so the cookie signature is the only thing authenticating a user and a published key is a forged admin session. The reference unit ships the line commented out rather than filled in, for the same reason. |
| `DATABASE_PATH` | `<repo root>/database.db` | Bare SQLite file path, not a URL — set to a path under the `/app/data` volume in the container. |
| `ARTIFACT_STORE` | `<repo root>/instance/artifacts` | Where NetHub keeps the image bytes it was given, and what `Artifact.storage_path` points inside. NetHub owns it (§3.3), unlike the `REGISTRIES_ROOT` it replaced at build step 7. One flat directory: both transports address it by filename. Usually a large mounted volume. |
| `IMAGE_TRANSPORT` | `push_scp` | Deployment-level, never request-level — choosing the transport chooses whose credential is spent (§4.3.1). |
| `DEVICE_TARGET_CIDRS` | none — empty refuses every submit | Comma-separated CIDRs a submitted target address must fall inside. Fail-closed: an unset security setting is not "allow all". |
| `SESSION_COOKIE_INSECURE` | unset — cookie is `Secure` | Local HTTP dev only. `config.py` sets `SESSION_COOKIE_SECURE` on by default, plus `SameSite=Strict` (§4.5: a cross-site "approve: reload" is a fleet outage) and an explicit `HttpOnly`. Set to `1` to serve over plain HTTP locally — `dev.sh` does. `PERMANENT_SESSION_LIFETIME` (12h) works *because* `auth.py` sets `session.permanent` at login; the two halves landed on separate branches and neither is effective alone, so removing that line turns the lifetime back into dead configuration with no error. Verified live: a real login emits `Expires=` ~12h out. |
| `MAX_CONTENT_LENGTH` | `1_500 * 1024 * 1024` | Upload size cap, bytes. An oversize request gets a real `413` page (`nethub/__init__.py`'s `too_large_error`) stating the configured limit, not Werkzeug's bare default (WS-5.6). |
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
has no such field and no reset flow to force into, so
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

**Publishing and installing are two separate operations, and only one
touches a device** (design doc §6, §8/§8.1). Publishing is
`artifacts.ingest()`, run synchronously in the upload request: stream to a
temp file while hashing, compare against the submitted SHA-512, `os.link`
into place, commit the row as `published`. There is no publish job, no
rendered registry file, no git commit, no lock and no `render_state`; build
step 7 deleted all of that (see "Artifact store" below). Installing runs the
phase model in `nethub/devices/phases.py`, dispatched to the sibling.
Publishing touches no device and installing is the only dispatched work.

**NetHub requires one thing of a device at login, and the list should
stay short** (design doc §4.3): **privilege level 15**. NetHub otherwise
writes no configuration outside the upgrade itself, and asserts privilege
15 at pre-check (`phases.phase_precheck`). One exception is deliberate,
bracketed rather than
standing, **and belongs to the push transport alone**: the stage phase
enables the device's own SCP server for the duration of the push and
restores whatever it found (enabled or not) in a `finally:` block in
`transfer._push_scp`, confirmed by re-reading the running-config rather than trusted from the
adapter's exit status (design doc §4.3.1). A host whose restore can't be
confirmed is failed outright, because an unconfirmed enable would otherwise
ride into startup-config on `write memory`. Under the
pull transport nothing on the device is reconfigured at all, so the
exception doesn't arise. The pre-check asserts no
`ip ssh source-interface` in either mode — it was a pull-era carryover
and push confirmed it doesn't gate the device's SCP server, an unrelated
service. It's relevant again *for a deployment running pull* (the device
is an SSH client again), but as a device-side prerequisite an operator
satisfies rather than a check NetHub makes — `phase_precheck` asserts
privilege, boot mode and free space, and deliberately nothing about it.

**On privilege 15 specifically — there is no `enable` escalation
anywhere** (design doc §4.3). Every command an upgrade runs —
`write memory`, `copy` to flash, `install add … activate commit` — needs
level 15, so an account that can't reach it can't upgrade anything.
Asking for it at login rather than via `enable` removes a second secret
(no `become_password` is ever collected) and removes the shared-enable-
secret problem, which is the same shared-credential objection §4.3 raises
about service accounts. Don't reintroduce an escalation step, and don't add
an enable secret to the credential path — `connection.connect()` passes no
`secret` and nothing calls Netmiko's `.enable()`. It's also a *property* rather than a tax: §4.3 says
NetHub's `role` gates NetHub's screens and never substitutes for what a
credential authorizes on the device — the privilege requirement makes
that concrete, since the refusal comes from the device. Pre-check asserts it
so an under-privileged account fails once and clearly
(`failure_stage: privilege`) rather than part-way through a wave.

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
either NetHub pushing over SCP (a second session to the same pinned
address, under the submitter's device credential — the default, and the mode
with no distribution credential at all) or the device pulling over SFTP from
a distribution daemon. **Only push can actually run today**: nothing sets
the sibling's `pull_target`, so `IMAGE_TRANSPORT=pull_sftp` fails every
stage (PLAN.md WS-5 removes it). One `artifacts`
table backs both days; a `kind` discriminator (script/config/image)
distinguishes rows rather than splitting into separate tables. The SHA-512
is computed once at ingest and then read, never recomputed: by day-0
verification (not built), by the snapshot a run takes onto
`upgrade_run_hosts`, and by the device's own `verify /sha512` step during a
day-2 upgrade.

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
  in the table at ingest compared against the device. The row's digest is
  the authority, and a swapped file on the mount is one of the things
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

**Everything a phase acts on is read from rows, not supplied** (design doc
§3.5). There is no longer anything rendered to a directory — the per-host
filename, digest, size and version are snapshotted onto `upgrade_run_hosts`
at submit, so a run is self-contained and a mid-run change cannot re-target
it. Where an artifact row and the bytes on disk disagree, the row is the
authority: `artifacts.check_store()` reports a missing file or a changed
digest and never rewrites the recorded SHA-512. **Kea's reservations
(day-0, not built) are a derived store and follow the same rule** (§7.4):
rendered whole from `allowlist_entries` and reconciled, because a
reservation outliving a consumed entry silently re-opens §4.1's first gate.

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
the IdP's decision is inert. But a device SSH session needs a
password neither auth
backend will yield, so an upgrade run collects the submitter's device
credential at each approval gate, holds it in memory only for the life
of the *phase execution* that approval releases (design doc §4.3/§9.1 —
not for the life of the run, since a run can park at a gate for days),
and never writes it to a row, a log or the session. The device
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
doc §2, §4.3, §5). An SSH session sends the password after key exchange
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
names this exception explicitly so nobody "fixes" it later. All of this
is built: the check (`nethub/devices/connection.py`, see "Device layer"),
the `device_host_keys` table, and the scan-then-confirm screens under
`/hostkeys` (see "Upgrade routes"). There is no rendered `known_hosts`
file; under Netmiko the pin is a paramiko policy object.

**Failure, concurrency, and staleness semantics live in design doc §7** and
are load-bearing rather than aspirational — phase executions are dispatched
out-of-band by a sibling process (never synchronously in a Flask request
handler), and job status is a state machine with
explicit terminal states
(`succeeded`/`failed`/`timed_out`/`abandoned`/`cancelled`/`expired`), not
a success flag. Serialization applies to a *phase execution*, not to a
whole run: a run parked at an approval gate holds no running process. Consult
§7 before implementing anything that writes to the database or the
artifact store.

Five things in §7.3 are easy to get wrong:

- **`failure_stage` has its own vocabulary, separate from phase names.**
  §8.1's phases are named `stage` and `verify`, so reusing phase names as
  failure stages would make `phase='activate', failure_stage='stage'`
  ambiguous on its face. `upgrade_phase_jobs` is the only job table;
  `host_key_scans` reuses its claim and sweep shape with a smaller status
  vocabulary and no `failure_stage`.
- **Cancel is a column the sibling polls** (`cancel_requested_at`), not a
  signal or a kill — §9's rule that the job row is the only control
  channel is what forces that. Checked between hosts and at phase
  boundaries only. `abandoned` is for crashes; `cancelled` and `expired`
  are for humans and TTLs, and merging them costs the word its
  diagnostic value.
- **The sweep keys on `runner_instance_id`** (a UUID minted per sibling
  start), never a PID — a PID is reused across container restarts and
  meaningless across PID namespaces.
- **Nothing renders "stalled" yet.** The sweep lives in the sibling so it
  can't fire against a healthy run, which means a sibling that dies and
  stays dead is swept by nobody. The design answer is for Flask to read
  `heartbeat_at` and show "stalled" without changing the row, but today
  the run page only prints the raw timestamp, and the sibling updates it
  only between hosts, so a healthy 15-minute host would look stalled
  anyway (PLAN.md WS-9 and WS-11 build both halves).
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
file. **No retention purge exists yet**; nothing deletes old runs, rows or
bytes. When one is built it runs in the *sibling*, because it deletes
files and rows and that is not work for the web process.

**Provisioning log (not built) and upgrade run/phase rows share
plumbing (row shape, retention-purge helper, log viewer) but stay
semantically separate** — distinctly labeled, not merged into one
timeline, and never rolled up into a persistent per-device current-state
view. That roll-up is the line between a job record and an inventory, and
NetHub is explicitly not an inventory system (design doc §2 Non-goals, §7.4).

**§5 specifies day-0 and settings tables that do not exist yet**:
`allowlist_entries` (with the per-role artifact FKs that make §3.4's
serial→artifact mapping representable at all, plus `mac` for the Kea
gate), `provisioning_log` with an `outcome` enum and a
`provisioning_log_artifacts` junction, `settings` + append-only
`settings_audit`, `user_admin_audit` and `sessions`. Of the upgrade
tables it describes, all exist, including `upgrade_host_phase_results`.
`upgrade_phase_jobs` carries three columns §7/§9 depend on: `created_at`
(the queue has nothing else to order by, since `started_at` is null until
dispatch), `deadline_at` and `runner_instance_id`. Two constraints
are load-bearing rather than tidy: `UNIQUE(run_id, phase, attempt)` is
what makes two admins clicking "approve: reload" a `409` instead of two
reloads (§8.1's serialization is scoped to *execution*, so it stops
overlapping processes, not duplicate rows), and `PRIMARY KEY (run_id,
hostname)` stops a request naming a host twice from reloading it twice.

**Snapshots must carry every field the thing they snapshot carries.**
`upgrade_run_hosts` snapshots `filename`, `sha512`, `version` and
`file_size` from the artifact at submit. Without `file_size` a phase would
have to re-read `artifacts` at dispatch, which would let a mid-run
supersede silently re-target the run. With it, a run is self-contained and never reads `artifacts` at
dispatch. There is no `remote_dir` column at all anymore — it addressed a
device's own `copy sftp://…` path under the old pull design, a remnant
from when the distribution host could have been a separate remote
machine, and push reads straight from a fixed local mount by filename
(design doc §5). Don't reintroduce it. `upgrade_runs` also snapshots
`shared_account_mode` beside `device_username_used`, or an auditor can't
tell "jsmith ran this" from "everyone runs as jsmith". Shared account mode
is not implemented, so that column is always `False` (PLAN.md WS-5 removes
it).

**§6.1 specifies the target API surface**: routes, methods, the `202` +
job-id polling contract, and an error envelope that returns §7.3's
`failure_stage`/`error_summary` enums so the dashboard and the log agree
on words. Today's routes are server-rendered pages that redirect after a
POST; there is no JSON API. `POST /provision` (not built) is the only unauthenticated route and never
returns `403` or a distinguishable body for a known serial (§4.1's
enumeration-oracle rule).

Vendor scope is IOS-XE only for now, but the Software Lifecycle schema and
job model carry a `platform` field throughout so a second platform is an
addition, not a rework.

## Hard rules — do not implement these

These are settled decisions with reasoning in the design doc. If a change
seems to require one, the design is what needs revisiting, not the rule.

- **No user-supplied playbooks — now a structural fact rather than a rule.**
  There are no playbooks. Device work is `nethub/devices/`, closed at build
  time because it is code in this repo. The rule existed because accepting
  one was arbitrary code execution with the live device credential in reach,
  an authenticated RCE primitive sitting beside the unauthenticated route §4
  spends its length on. Nothing offers that surface now — but the property is
  worth remembering the next time something proposes an "advanced" hook that
  executes submitted text, because the credential §9.1 injects for the phase
  execution is exactly what it would reach.
- **No user-supplied inventories, and no user-supplied Jinja — the
  mechanism is gone, the intent is enforced in code.** There is no inventory
  to upload and no template to render. A submitter sends a *request
  document* — hosts, a bundle key, a closed set of typed knobs — which
  `nethub/upgrades.py` validates and compiles into rows; the contract is
  tabulated in that module's docstring, where it moved when
  `ansible/inventory/README.md` was deleted. The reason it stays closed is
  unchanged: a submitted registry would let a request name any filename
  against any SHA-512 and bypass whatever owns those values.
- **No user-settable connection vars — except the target address, which is
  validated rather than trusted.** The device username is the submitter's
  own `users.device_username`, read server-side (or, only under shared
  account mode, one admin-configured value — never something a request
  supplies either way). An identity the submitter can type is not evidence
  of anything, and §4.3's audit property depends on it. The same goes for
  credentials and any escalation setting. The address is different, because
  a request has to name its targets: it is accepted under two constraints,
  both implemented in `nethub/upgrades.py` — a deployment-level target CIDR
  (`DEVICE_TARGET_CIDRS`, standing in for §5's `settings` row) and a
  fail-closed check against a *confirmed* `device_host_keys` row. The line
  is between vars asserting *who someone is* (never submitted) and the
  address of the thing being acted on (necessarily submitted).
- **No device work in the Flask process; the sibling owns dispatch.** Half
  of this rule's original reasoning dissolved with the EE — there is no
  Podman socket to mount into Flask, so "a Flask RCE becomes host-level
  container control" no longer applies. What survives is §3.2's plainer
  reason and it is enough: Flask holds the only unauthenticated route, and a
  handler that opened a device session would hold it for minutes. So Flask
  writes a `queued` row and nothing else; `nethub/sibling.py` does the rest,
  and the job row is the only *control* channel between them — which is also
  why there is no terminal streamed to a browser. There is exactly one other
  channel and it is not general: a **sibling-initiated** Unix socket carrying
  per-execution credentials and nothing else (§9.1, `nethub/credential_socket.py`).
  Don't widen it into RPC, don't let control or status onto it, and don't let
  Flask be the side that connects — the reason is window minimisation (a
  deposit endpoint leaves the sibling holding secrets for executions it hasn't
  started, and can be flooded). Three construction details are easy to get
  wrong: the **mount** is the authenticator, not `SO_PEERCRED` — under one
  shared rootless user a uid check discriminates nothing, so don't write one
  and believe it means something; the listening socket comes from a systemd
  `.socket` unit, never from `unlink()`+`bind()` in either container; and the
  sibling parses bytes Flask chose, so the response needs framing, caps,
  deadlines and a character allowlist on the credential before it reaches any
  variable or command string.
- **Flask runs exactly one worker — and more than one thread.**
  `gunicorn` with `workers = 2` silently breaks the credential path: a
  credential submitted to worker A is invisible to worker B (design doc
  §9.2). It fails closed but intermittently, and the error says nothing
  about workers. Threads are the other half of the same rule and are
  *required*, not optional: §3.2 now notes that a 1.2 GB image upload
  plus its SHA-512 pass is the longest operation in the system after the
  device work, and single-threaded it would hold the phone-home route for
  the whole window — the exact failure §3.2 legislated against, arriving
  through ingest rather than through a dispatch call. Hash
  incrementally over the chunks as they're written so "hashed once at
  ingest" survives. The socket is served on its own thread with
  deadlines, for the same reason. §3.2 also now names the budget the
  whole design is calibrated against: **2 s p99 on phone-home, 15 min of
  device backoff.** Check changes to the request path against those
  numbers.
- **The device credential never reaches disk, and this is now easy rather
  than delicate.** The whole `private_data_dir` problem is gone: nothing
  renders a directory for an execution to read, so there is no `extravars`,
  no `env/passwords`, and no `podman run -e` showing up in
  `/proc/<pid>/cmdline`. The credential is a Python attribute on
  `phases.PhaseContext`, held for the life of one phase execution. What still
  has to be *engineered* is the other end: `error_summary` is retained for a
  year (§7.4), so `phases._summarise` reads an exception's `summary`
  attribute rather than its `str()` (WS-4.2). Every exception the device
  layer raises sets `summary` explicitly in `__init__`, defaulting to its own
  `message` for the many call sites that never wrap a foreign exception —
  but a wrapper that interpolates a foreign exception's text (e.g.
  `TransferError(f"SCP push of {image} failed: {exc}", ...)`) must pass an
  explicit `summary=` that omits it, since `message` may still carry that
  text for `__cause__`/traceback context. This replaced an earlier
  type-level allowlist (`_OUR_EXCEPTIONS`) that trusted *any* message from an
  allowed exception type, including one of ours that had interpolated a
  foreign exception's text wholesale — the allowlist is gone; an exception
  with no `summary` attribute (anything foreign, and anything of ours that
  forgot to set one) reduces to its type name. A stray `str(exc)` reaching
  `summary` is a durable credential leak with no other symptom (§7.3).
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
  blast radius. Same class: `LimitCORE=0` **is** now set in the Quadlet
  unit's `[Service]`, which covers the deployed path — otherwise a crash
  writes the heap, live credential included, to
  `/var/lib/systemd/coredump`. Confirmed to propagate: the container
  process's own `/proc/self/limits` reads `Max core file size 0`, so this
  is not merely capping the podman client (unlike the two `[Service]`
  hardening directives the Container section records as removed). Still
  not addressed: a non-dumpable process
  (`PR_SET_DUMPABLE`), and neither applies to a bare `flask run` or to
  `upgrade_cli.py`.
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
  account mode** (design doc §4.4), which fixes the device username to one
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
  confirmed must fail, not warn.** `transfer.py`'s push adapter captures the device's prior
  `ip scp server enable` state before touching it, changes it only if not
  already enabled, and restores it in a `finally:` block regardless of
  whether the push succeeded (design doc §4.3.1). The restore is
  *confirmed* by re-reading the running-config, not trusted from a module
  exit status — an unconfirmed restore fails the host outright, because an
  unconfirmed enable would otherwise ride into
  startup-config on the activate phase's own `write memory`. Don't relax
  this to a logged warning: a device left with its SCP server on and no
  record of it is exactly the "cannot enumerate afterward" failure the
  design used to reject push over. Known, accepted, and *not* covered by
  this mechanism: a killed process, an abandoned run, or a crashed
  sibling never reaches the `finally:` block at all (design doc §4.3.1,
  §10) — don't claim this is fully closed. All of this is scoped to
  `transfer.py`'s push adapter; the pull adapter reconfigures nothing and has no
  bracket, which is why `scp_restore_confirmed` is null for a
  pull-transport stage row and why that null must be read together with
  `upgrade_runs.image_transport_used` rather than alone (design doc §5).
- **Day-2 transfer runs in whichever direction `image_transport` says,
  and the adapters are not interchangeable in their costs** (design doc
  §4.3.1). Both live in `nethub/devices/transfer.py`; `stage_image()`
  dispatches between them and owns the shared `verify /sha512`.
  - `push_scp` is the default: `CiscoIosFileTransfer` over Netmiko, which is
    Paramiko underneath. IOS-XE has no SFTP *server* (client only), so a push
    has no SFTP option — SCP is the only wire protocol available in that
    direction.
  - `pull_sftp` is the alternative: one `copy sftp://…` on the device's own
    CLI, prompts answered on the channel.
  - **`paramiko` is not an incidental choice.** Under Ansible, `net_put` with
    the `libssh` connection type was tested against a real device and found
    unusable — the SSH connection broke repeatedly at image size, with nothing
    useful in the debug log. Netmiko is Paramiko-based, which is why push works
    here; validated at 471 MB. Paramiko is deprecated upstream, so if it is
    ever swapped, re-test at image size rather than assuming a smaller transfer
    generalises.
  - Shelling out to OpenSSH `scp` was considered and rejected — it needs the
    device password on an interface the library API doesn't, with no secure way
    to hand a credential to a subprocess without it landing on a command line
    or on disk (design doc §4.3.1, §10). Don't add an OpenSSH-`scp` fallback
    without revisiting that.
  - The pull password is answered at the device's own `Password:` prompt and
    **never** embedded as `sftp://user:pass@host/` — that form lands in the
    device's command history and AAA command accounting, which is the record
    §4.3's attribution property depends on. `_pull_sftp` also refuses to put
    exception text in its error, because a channel read can quote what was
    written to that channel. Don't enable Netmiko's `session_log` on a
    pull-transport phase for the same reason.
  - **The pull adapter's prompt sequence is still unverified against a real
    device** (design doc §10) — the one item from the deleted playbooks that
    outlived them. Naming only the filesystem as the destination is
    deliberate: it forces the `Destination filename` prompt so both prompts
    appear in a known order. A wrong list hangs until the read timeout rather
    than failing fast.
- **Transport is deployment-level and never request-level, and its
  credential never lives in the `settings` table** (design doc §4.3.1,
  §5). `settings` holds `image_transport`, `distribution_host`,
  `distribution_user`, `distribution_credential_source` — no password.
  Under `same_as_device` the distribution credential is the submitter's
  own, already crossing §9.1's socket; under `dedicated` it is a systemd
  credential in the sibling's unit that Flask never sees. A request may
  not select the transport, because selecting the transport selects whose
  credential gets spent. And **don't reach for Ansible Vault**: the file
  it would protect is a Python variable that exists for the life of one
  phase execution, so vault would protect the least-exposed copy using a
  second secret delivered by the same means — the key-disposal problem this
  design already removed once. Repointing the distribution host would be
  the highest-yield settings write in the system, and nothing yet protects
  settings or other security-relevant rows against a Flask-side rewrite
  (design doc §7.2, §10).
- **NetHub is the sole source of the image bytes, and that is not
  modular** (design doc §2 Non-goals, §3.3). The *source* is fixed even
  though the *direction* is configurable: both adapters read the same
  published subtree on the NetHub host — pushed from it directly, or served
  from it by the pull transport's SFTP daemon. There is
  no remote distribution target, no mirror, and no second store — both
  adapters read `PhaseContext.search_dir`. The cost
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

Ordinary Python driving Netmiko. This replaced `ansible/`, which the
migration deleted outright. The five modules:

- `facts.py` — `show version`, `dir` and `show privilege`, parsed with
  ntc-templates where a template exists.
- `connection.py` — the only way any NetHub process opens a device session.
- `transfer.py` — `stage_image()` plus the two transport adapters.
- `install.py` — the activate/reload/verify/cleanup half.
- `phases.py` — the per-host loop, the exception→`failure_stage` mapping, and
  the rows.

`nethub/sibling.py` dispatches them and `nethub/upgrade_routes.py` creates the
rows they work from.

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
`cleanup`. That also validates the reconnect loop against real hardware for
the first time — a spike the original migration plan called for and never
carried out before the code was written.

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
device.** `check_device_facts.py <ip>` captures `show version` / `dir` /
`show privilege` to a directory and reports what parsed; `--replay <dir>`
re-parses one offline. That split is what makes `tests/captures/` possible,
and validating a new IOS-XE release means capturing it and replaying it, not
reading the template. **`check_device_facts.py` connects through the same
fail-closed path as everything else now** (WS-6.1): it takes an IP literal,
not a hostname, and calls `connection.connect()` with a pinned `HostKey` --
read from a confirmed `device_host_keys` row when the database is reachable,
or from an explicit `--fingerprint` when it is not, with no TOFU fallback
either way. It used to connect with stock `ConnectHandler`, whose default
`ssh_strict=False` installs `paramiko.AutoAddPolicy()` and auto-accepts any
host key -- the one place in the tree still doing that. `resolve_pin` is
imported straight from `upgrade_cli.py` rather than reimplemented, so the
two tools' pin resolution can't drift apart.

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
  §4.3 exists to refuse. It is called from the sibling now, not from Flask
  (WS-6.2b) — it is device I/O like everything else this module does, so it
  moved to the process that does all other device I/O rather than blocking a
  Flask request. `connection.py` itself is unchanged either way; only the
  caller moved.
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
- **Skip-if-already-staged is a digest, not a `dir` presence check.**
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

**A `connection.DeviceConnectionError` from that second session must reach
`phases.failure_stage_for` as itself, not as a generic transfer failure**
(WS-4.3). `_push_scp`'s `except Exception` wraps everything into
`TransferError` by default — correct for real SCP failures, wrong for a
`HostKeyError` from the pin catching a changed key on the second session,
which is exactly the signal `failure_stage='hostkey'` exists to surface
distinctly rather than burying among ordinary flaky-SCP failures. `_push_scp`
re-raises `connection.DeviceConnectionError` (alongside the existing
`TransferError` re-raise) before the generic handler.

`install.py` is driven by the phase model rather than written as one
procedure, and these things in it are load-bearing:

- **`wait_for_device()` takes a connection *factory*, not a connection.** The
  reload takes the session with it, so there is nothing to reuse — and the
  factory is where the caller supplies the credential and the pinned host key,
  which keeps both out of this module.
- **A device answering SSH is not a device that is ready.** IOS-XE accepts
  connections while it is still coming up, so every reconnect attempt runs a
  real command before the connection is accepted, and one that answers but
  cannot be used is closed rather than returned.
- **`wait_for_device()`'s reconnect loop does not treat every failure as
  transient** (WS-4.1). It re-raises `connection.AuthenticationError`
  immediately rather than retrying: with the default reload wait this loop
  would otherwise present the same credential roughly 28 times in 15 minutes,
  and a device that comes back with AAA unreachable and falls back to a local
  database the submitter isn't in would trip a fleet-wide TACACS+/RADIUS
  lockout. `connection.HostKeyError` is deliberately **not** carved out the
  same way yet — see design doc §10: whether an IOS-XE
  upgrade can legitimately regenerate a device's host key is an open hardware
  question, and re-raising here on an unverified assumption could turn a
  successful upgrade into a hard failure instead of a transient reconnect.
- **A lost session during `install add` is the expected ending, not an
  error** — but a *clean* return that does not say `SUCCESS` is a failure.
  IOS-XE reports some install failures in-band with the session still up, and
  treating that as "rebooting" would burn the whole reload deadline before
  reporting something visible immediately.
- **The pre-activate image check is a `verify /sha512`, never a `dir`.** This
  is the §8.1 requirement the old playbook missed: a host whose staged image
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

**`assert_ready_to_activate` normalises `sha512` through the same
`transfer._normalise_digest` `stage_image` already uses** (WS-4.4). Before
this it passed the caller's value straight to `verify_sha512`, which
lower-cases only the digest it parses *from the device* — so an uppercase or
whitespace-padded digest staged successfully and was then refused at
activate, with an error whose two halves differed only in case. Both call
sites now share one normalisation point rather than each having its own.

## Dispatch (`nethub/sibling.py`, `nethub/credential_socket.py`)

The schema is in `nethub/models.py` — `device_host_keys`, `upgrade_runs`,
`upgrade_run_hosts`, `upgrade_phase_jobs`, `upgrade_host_phase_results`, with
§7.3's vocabularies as module constants. Two deviations from §5, both because
the EE is gone: there is no `private_data_dir`, and `playbook_log_path` is
`log_path`. `upgrade_run_hosts.artifact_id` is a bare integer, not a foreign
key: it was declared before the `artifacts` table existed (build step 7)
and never converted, so nothing at the database level stops an artifact a
run still needs from being deleted (PLAN.md WS-2).

**Two more tables joined the schema for WS-6.2b/6.3/6.4, and neither is a
job table in §7.3's sense.** `host_key_scans` is a scan dispatched to the
sibling exactly like a phase job — same conditional-claim shape, same FIFO
queue, same NULL-safe sweep predicate — but with a deliberately smaller
status vocabulary (`HOSTKEY_SCAN_STATUSES`: `queued`/`running`/`succeeded`/
`failed`/`abandoned`, no `cancelled`/`expired`/`timed_out`): a scan has no
approval gate and nothing to time out against beyond
`connection.CONNECT_TIMEOUT`. Its `consumed_at` column is what makes a
succeeded scan confirm at most once (see "Upgrade routes" below).
`device_host_key_audit` is unrelated to dispatch — a plain append-only log
of `confirmed`/`deleted` events, keyed on the address string rather than a
foreign key to `device_host_keys.id` specifically so it outlives a deleted
pin. Don't add either kind of relationship to the other table by accident:
a scan is not a job, and an audit row is not a pin.

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
`str(exc)` there is a durable credential leak with no other symptom.
`phases._summarise` copies only an exception's explicit `summary`
attribute, which every exception this codebase raises sets; anything
without one (every foreign exception) contributes its type name and
nothing more. There is a test that raises a
`RuntimeError` containing the password and asserts it reaches no column.

**Cancel and the deadline are checked between hosts, never mid-host** — there
is no safe place to stop inside an activation, and polling more finely would
not create one. **Be precise about what that means the deadline bounds**: not
a single wedged device, which still burns `TRANSFER_READ_TIMEOUT` (7200s) with
only that timeout above it, but the **wave** — a 40-host stage that would
otherwise occupy the sibling's single FIFO queue with no limit of any kind.
`deadline_at` is written by `upgrades.phase_deadline()` at both creation
points (`submit` and `approve`); it was previously declared, read in two
places, and **written by nothing**, so `timed_out` and `expired` were
unreachable states and both existing deadline tests set the column by hand —
green over inert machinery. The budgets are derived from the hardware timings
in "Device layer" times a safety factor of 2, and they are a first cut from
*single-device* measurements: re-derive them from a real multi-host wave when
there is one rather than trusting the arithmetic.

**`phase_activate` owns the reconnect**, not `phase_verify`: a device that
never returns is a `reload` failure, a different `failure_stage` and a
different conversation than a wrong version.

**An abandoned phase is parked at its gate, not failed — and only if someone
can approve it.** §7.3 grants an abandoned device-touching phase a fresh
approval (a new row with an incremented `attempt`), and that was unreachable:
`sweep()` called `_fail_run`, so the run went terminal and `approve()`'s first
guard refused forever, while `models.py` documented `attempt` as existing to
permit the retry and `approve()` computed `1 + count(abandoned)` — an
expression that had never returned anything but 1. `sweep()` now calls
`_abandon_run`, which returns the run to `awaiting_approval` at that phase.
**The `APPROVABLE` test is load-bearing**: `precheck` has no gate by design
and `verify` follows `activate` without one, so parking either would leave
the run at `awaiting_approval` with a phase `approve()` refuses as "not a
phase anyone approves" — stuck rather than failed, which is worse because it
looks recoverable. Those stay terminal. `APPROVABLE` moved to `models.py`
with the other vocabularies so the sibling can read it without importing
`upgrades` (and the artifact store behind it); `upgrades.APPROVABLE` still
resolves, by import.

**`run_once` catches `OSError` as well as `CredentialError`.**
`fetch_credential` does not wrap `connect_socket()`, and `connect_to()._open()`
calls a bare `socket.connect(path)` — so a Flask unit restarting as the
sibling picks a job up raises `ConnectionRefusedError`/`FileNotFoundError`,
which escaped to `main()`'s `except Exception` *after* `claim()` had committed
`running` under this instance's own `runner_instance_id`. `sweep()` only
matches rows whose id differs from its own, so the instance that stranded the
row was structurally incapable of recovering it and the run sat `running`
forever. The rarer failure was handled cleanly and the common one was not. The
`error_summary` for that branch names the exception *class* rather than
copying `str(exc)`: the message is chosen by the OS and the path, and that
column is retained for a year.

**`sweep()`'s predicate is `or_(runner_instance_id.is_(None), != self)`.**
`!=` alone evaluates to NULL — not true — for a NULL column, so a `running`
row with no runner id was invisible to every sweep forever. Latent rather than
live, since `claim()` sets both in one atomic UPDATE, but the sweep is the
only mechanism that un-sticks a crashed execution and the explicit predicate
does not depend on that invariant holding for every future writer.

**SQLite returns naive datetimes for values written aware.** Comparing
`now()` against a stored `deadline_at` raises `TypeError` for any row read
back from the database — always in production, never in a test that skips the
round trip. `phases._aware()` and `sibling._aware()` are the fix. This is
latent everywhere else timestamps are stored and only bites where something
compares; don't add a comparison without routing it through one of them.

**The sibling's claim is the conditional update itself.** `UPDATE ... WHERE
id=? AND status='queued'` with the rowcount as the answer: a read-then-write
lets two readers both see `queued` and both claim it, and nothing enforces
that only one sibling runs (§9.1). (SQLite is not in WAL mode yet, despite
design doc §5 requiring it; PLAN.md WS-2 fixes that.) `runner_instance_id` is a UUID minted per start and never a PID.

**The credential socket: Flask serves, the sibling connects.** Not the
reverse — a deposit endpoint would leave the sibling holding secrets for
executions it has not started, and could be flooded. These things about
`nethub/credential_socket.py` are load-bearing:

- **Nothing in it calls `bind()`.** `systemd_socket()` adopts the descriptor
  a `.socket` unit handed over, and returns `None` when the process was not
  socket-activated — the caller then skips serving rather than creating a
  path with the wrong ownership. `quadlet/nethub-credential.socket` is that
  unit, and the path is now exercised end-to-end rather than only argued: a
  real `podman run` under it answers a request on the socket. A dev run, a
  plain `podman run` and most tests still take the `None` branch.
- **The descriptor reaches Flask under a *renamed* pair of variables, and
  that is not stylistic.** gunicorn's arbiter reads `LISTEN_FDS`/`LISTEN_PID`
  itself and, when the pid matches, **clears the configured `bind` and serves
  HTTP on whatever systemd handed over** — so passing this socket in the
  obvious way breaks NetHub twice: gunicorn speaks HTTP on the credential
  socket, and nothing listens on `NETHUB_PORT` at all. It also unsets both
  variables, so the forked worker — where `create_app()` and the store
  actually live — would find nothing left to read. The image's
  `entrypoint.sh` therefore re-exports the count as `NETHUB_LISTEN_FDS`
  (`credential_socket.HANDOVER_FDS_ENV`) and unsets systemd's own before
  exec'ing gunicorn; the descriptor itself is untouched and survives the exec
  and the fork. Observed, not inferred: gunicorn logged `Listening at:
  unix:/…/credential.sock` with nothing on the HTTP port.
- **The `LISTEN_PID` check cannot apply to that handover, so the descriptor
  is interrogated instead.** It is read in a forked worker, so the pid never
  matches by construction. `_adopt_listening_fd` accepts fd 3 only if it is
  really a listening `AF_UNIX` socket (`SO_ACCEPTCONN`), declines otherwise,
  and never closes a descriptor that isn't ours — a narrower question than
  "was this environment meant for me", and a live one under gunicorn, whose
  arbiter opens its own listener during startup.
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
- **The store is bounded by its TTL, not only by use, and that took wiring.**
  `purge_expired()` and `discard()` existed with **no caller anywhere in
  `nethub/`** — the TTL was enforced only inside `release()`, i.e. only if the
  sibling eventually asked for that exact job. An approval whose job never ran
  (sibling down, run cancelled, job abandoned) left a named human's plaintext
  AAA password in the worker that also serves the only unauthenticated route,
  which with one worker and a long-lived unit is weeks. `hold()` and
  `release()` now sweep on every use, and `upgrade_routes.cancel` discards the
  credentials of `queued` jobs. Two details are load-bearing: the sweep in
  `release()` runs **after** the target is popped, or an expired credential
  reports "no credential held" instead of "expired; the phase needs
  re-approval" — the same refusal a never-approved job gets, and a worse
  diagnosis (a test pins this). And cancel sweeps only `queued` rows: a
  `running` job has already fetched, and keying the discard by run rather than
  job would be exactly the mistake §9.1 forbids. Deliberately **not** a
  background timer — this store has no supervisor, and a thread outliving a
  request is worse to reason about than a sweep on each use.
- **`type(job_id) is not int`, not `isinstance`.** `bool` is a subclass of
  `int` and `hash(True) == hash(1)`, so `{"job_id": true}` released the
  credential held under key 1 — and `db.session.get(UpgradePhaseJob, True)`
  would have bound to 1 as well. Bounded by the mount and by the sibling never
  sending a bool, but this function's entire purpose is validating a message
  before it selects a secret.
- **Every field holding the credential is `field(repr=False)`** — on `_Held`,
  `phases.PhaseContext` and `transfer.PullTarget`. No path renders any of them
  today (tracebacks carry no frame locals, `DEBUG` is off, neither netmiko nor
  paramiko defines a repr that would pull one in), so this is a structural
  guard rather than a fix. `PhaseContext` is the object this file names as the
  credential's entire lifetime container, and one `log.debug("ctx=%r", ctx)`
  added while debugging writes it to journald or a CI log that outlives the
  phase. Don't drop the flag when adding a field beside them.

**`upgrade_cli.py` warns when it takes the password from
`NETHUB_DEVICE_PASSWORD`** rather than the prompt. The var is kept because a
scripted recovery needs it, but an env var sits in `/proc/<pid>/environ` for
the whole run — up to ~20 minutes for `--phases all` — is inherited by every
child, and lands in shell history if set inline. `scripts/check_device_facts.py`
reads the same var and now carries the same warning (WS-6.1/WS-2.3) — it no
longer connects with `AutoAddPolicy` either, so both of that script's
`upgrade_cli.py`-mirroring gaps closed in the same change.

**`verify_running` runs on the serving thread and needs its own app
context.** Flask-SQLAlchemy's session is thread-local, so without one every
request fails with a generic refusal that says nothing about the cause. The
wiring in `nethub/__init__.py` does this; a test that forgot it is how it was
found.

The sibling is `python -m nethub.sibling`, reading `NETHUB_CREDENTIAL_SOCKET`
and `NETHUB_SEARCH_DIR`. It builds a bare Flask app for SQLAlchemy's context
rather than calling `create_app()` — that would register routes and run the
first-boot admin bootstrap, and the sibling must do neither.

**Design doc §10 records the `known_hosts` question as resolved.** Under
Ansible it was unclear which `known_hosts` file a connection plugin
actually read. Under Netmiko the paramiko client is ours: there is no
rendered `known_hosts`, and the check is a policy object in
`connection.py`.

## Artifact store (`nethub/artifacts.py`, `nethub/artifact_routes.py`)

The single ingest record behind both days (design doc §3.4, §5). Build step 7
replaced the `software_registry` YAML store with this: `nethub/registry.py`,
`registry_routes.py`, the `Registry` pointer rows, `REGISTRIES_ROOT`, file
adoption, `search_dir`, the sibling-key-preserving atomic save, the
path-escape guard and `check_registry()` are all gone, along with 69 tests.
That block existed so an Ansible playbook could read it; the maintainer
confirmed nothing outside NetHub reads those files, so it was a deletion
rather than an export path.

**`ARTIFACT_STORE` is NetHub's own directory**, which is the difference from
the `REGISTRIES_ROOT` it replaced: that was a place an admin bind-mounted
*their* files into for NetHub to point at. It is one flat directory because
both transports address the source by filename (§3.3).

**Two constraints live in the schema rather than in the code that writes it.**

- `UNIQUE(filename) WHERE state IN ('staged','published')`. Without it two
  uploads sharing an original filename promote to the same path and silently
  overwrite each other's bytes — and *every downstream hash check still
  passes*, because each compares a row's own `sha512` against whatever now
  sits at that path, never confirming the row and the disk agree on which
  artifact this is. That would quietly break the "hashed once, consumed three
  times" chain of custody §3.4 is built on.
- `UNIQUE(platform, bundle_key)` over published images. One published image
  per key, enforced by the database rather than by whatever writes the row
  remembering to check.

Both are also checked in `artifacts.ingest()` so the UI gets a message rather
than an `IntegrityError`; the schema is the backstop, and there are tests
against each independently.

**Ingest hashes over the chunks as they are written**, not by re-reading the
file. A 1.2 GB upload plus its digest is the longest operation in the system
after a device transfer and Flask holds the only unauthenticated route, so a
second pass doubles a window §3.2 spends its length bounding. Bytes stream to
a temp file in the same directory and are linked into place only after the
digest matches — a failed or interrupted upload cannot leave a
half-written image under a name something would later push. The submitted
checksum is the *claim being verified* and is never what gets recorded.

**The final move is `os.link`, never `os.replace`, and that is the fix for a
real corruption.** Every check at the top of `ingest()` runs *before* the
upload streams, so under concurrency they prove nothing minutes later when
the bytes are moved. Two uploads sharing a filename both reached
`os.replace`, and the loser overwrote the winner's already-committed bytes
*before* hitting its own `IntegrityError` — leaving the winner's row
recording one artifact's SHA-512 against the other's content, silently
breaking the chain of custody §3.4 is built on. The cleanup path only ever
removed the temp file, so nothing restored the winner's bytes. `os.link`
raises `FileExistsError` instead of overwriting; both paths are in the store
by construction, so a hard link is always available. The commit is
wrapped too: the schema fires *after* the bytes are in place, and a file no
row accounts for would block that filename for every later upload, so losing
that race removes its own bytes. Don't switch either back to a silent
overwrite, and note the impact was bounded rather than catastrophic only
because the device-side `verify /sha512` compares against the row's digest —
a corrupted store failed upgrades, it did not install wrong bytes.

**`state`, `superseded_by_id` and `bytes_state` exist but only `published`
and `present` are ever written.** There is no promotion step to reach
`staged` through and no supersede flow — the same no-supersede stance the
YAML store had, and delete is still a hard removal of row and bytes. The
columns are there because the partial indexes are defined over them and §7.4's
retention story references them. Don't add `superseded_by_id` handling without
the flow that reads it.

**`check_store()` keeps the drift check's one rule**: re-derive what is cheap
and always recoverable (a stale `file_size`), flag what is not (a missing file,
or a digest that no longer matches). It never guesses at a wrong checksum, and
it is a button rather than a page load because it hashes every image.

## Upgrade routes (`nethub/upgrade_routes.py`, `nethub/upgrades.py`)

Thin routes over a service module — the same shape `artifact_routes.py` has
over `artifacts.py`. `/upgrades` submits and approves; `/hostkeys` is the separate
confirmation flow §4.3 requires before any address may be named.

**The store is the `artifacts` table (build step 7).** Submit resolves a
bundle key to a row and snapshots `filename`/`sha512`/`version`/`file_size`
onto `upgrade_run_hosts`, so a run is self-contained and a later change to the
artifact cannot re-target it. A request names a *key* and never a filename or
a digest — a submitted pair would name any bytes against any checksum and
bypass the table that owns both.

Four deployment settings §5 puts in a `settings` table are env vars in
`config.py`, because alpha has neither that table nor its audit:
`ARTIFACT_STORE`, `IMAGE_TRANSPORT`, `DEVICE_TARGET_CIDRS`,
`SHARED_ACCOUNT_MODE`. **An empty
`DEVICE_TARGET_CIDRS` refuses every submit** rather than allowing any address
— an unset security setting is not "allow all". `SHARED_ACCOUNT_MODE` is
hardcoded False and must not be inferred from anything (§4.4).

`users.device_username` is new. It is read server-side and a request can
never assert it, because §4.3's two-sided attribution depends on the device
seeing a name NetHub chose. A user without one cannot submit. **`GET /profile`
is where it is set** — that route existed as POST-only with no template
referencing it, so there was no way to set one through the web UI at all and
a fresh deployment could not submit anything until the row was edited out of
band. The form posts to the same `POST /profile/device-username`; setting your
own through an authenticated form is the intended mechanism and does not
conflict with the never-submitted rule, which is about asserting *someone
else's* identity. That route also no longer redirects to `request.referrer` —
it was the only unvalidated redirect in the app, and there is a real page to
return to now.

Things that are refusals rather than validation niceties:

- **A submitted target must be an IP literal inside a configured CIDR**, and
  a hostname is refused rather than resolved: the CIDR check and the eventual
  connection would resolve it at different times, and the pin would end up
  keyed on a string whose meaning can change afterwards (§4.3).
- **An address with no *confirmed* `device_host_keys` row cannot be
  submitted**, and a row that exists but is unconfirmed counts as absent.
  Scanning writes a `host_key_scans` row (WS-6.2b — see below); only the
  confirm step writes a `device_host_keys` row.
- **Re-confirming a *changed* key is refused** on the confirm route — it is
  indistinguishable from the attack the pin exists to catch, so an admin has
  to delete the pin first. That friction is the point.
- **A second approval of the same gate is refused** before it reaches
  `UNIQUE(run_id, phase, attempt)`, so two admins clicking "approve: reload"
  get a message rather than an `IntegrityError` — and never two reloads.

**Pre-check is the phase with no approver, and that broke the credential
interlock once.** §9.1 has Flask cross-check the phase job's `approved_by`
against the identity that supplied the credential — but §8.1 gives pre-check
no gate, so its `approved_by` is null by design. A `verify_running` that
refused a null failed *every* pre-check, and each half looked correct alone.
The rule is "the identity that supplied it": the approver for a gated phase,
the submitter for pre-check. Don't tighten that back to a non-null check.

**A credential release that fails its checks destroys the credential**
(`CredentialStore.release` pops before validating). That makes replay
worthless at the cost that a stray request forces a re-approval — acceptable
only because the mount is what decides who can reach the socket at all.

**Known gap, not yet decided:** `upgrade_runs.device_username_used` snapshots
the *submitter's* device username, but each gate collects the credential of
whoever is standing at it. If a different person approves a phase, the device
sees the approver's username while the run row records the submitter's, which
breaks the attribution §4.3 is built for. Closing it means either restricting
approval to the submitter or recording the device username per phase; both
are design decisions rather than fixes.

**Host-key scanning is dispatched to the sibling, and confirming is bound to
a scan NetHub itself performed (WS-6.2b/6.3).** `scan_hostkey` (POST) used
to call `connection.scan_host_key()` inline in the request handler — the one
place in the tree still doing blocking device I/O in Flask, and with no
`DEVICE_TARGET_CIDRS` check either. It now validates the address through the
same `upgrades.check_target()` the submit path uses, inserts a `queued`
`host_key_scans` row, and redirects to `GET /hostkeys/scan/<id>` — a plain
"reload to check" result page, since this app has no client-side polling
anywhere else. The sibling's dispatch loop checks for a queued scan *before*
a queued phase job on every iteration (`sibling.py`'s `main()`): an admin
watching a confirm screen shouldn't queue behind a phase job that might be a
15-minute stage already in flight. `confirm_hostkey` takes a `scan_id`
rather than raw `key_type`/`fingerprint_sha256` form fields — those two, and
the address, now come from the referenced `host_key_scans` row, never from
the request body, and the route refuses unless the scan `succeeded`, was
requested by `current_user`, and has not already backed a confirmation
(`consumed_at`, a one-shot flag mirroring §4.1's allowlist pattern) — plus a
15-minute freshness window past the scan's `finished_at`
(`SCAN_CONFIRM_WINDOW`). **This does not fully close §4.3's separation-of-duty
gap** — the same person can still scan and then confirm, since alpha has no
roles — it only proves a confirmation corresponds to a key NetHub itself
observed at some specific prior moment rather than to whatever a form
claims. The real fix is role-based access control.

**`confirm_hostkey`/`delete_hostkey` write a `device_host_key_audit` row
(WS-6.4).** Neither used to leave any record of who acted or what the pin
was before the action — `delete_hostkey` was the sharper gap, since it is
the "deliberate friction" step before a *changed* key can be re-accepted,
and left zero evidence. The audit row is written with the **pre-image**:
the fingerprint being removed, for a delete, or newly confirmed, for a
confirm, captured before the mutation. There is no "changed" action —
`confirm_hostkey`'s existing guard already refuses to overwrite a confirmed
row in place, so the only way to change a pinned key is delete-then-
reconfirm, which is a `deleted` row followed by a fresh `confirmed` one.
`GET /hostkeys/history/<address>` shows it, linked from the host-keys list
page; note it's keyed on the address string and reachable only for
addresses currently listed there, so a fully-deleted address's history has
no link pointing at it today.

**Operational note:** an `AF_UNIX` path is capped near 108 bytes. The socket
path the Quadlet mount produces has to stay under it, and the failure is an
`OSError` at bind time rather than anything about length.

## Manual escape hatch (`nethub/upgrade_cli.py`)

`python -m nethub.upgrade_cli` upgrades a device with NetHub switched off.
Build step 8, and it exists to pay back a cost the migration knowingly
incurred: the playbooks it replaced were deliberately standalone, so an
operator could run them by hand against a fleet with nothing else running.
Folding device work into `nethub/devices/` took that away — an accepted
loss, not an unnoticed one.

It needs **no Flask app, no sibling, no credential socket and no job rows**,
because `nethub/devices/` never depended on any of them. That is why this is a
few hundred lines rather than a parallel implementation, and it is worth
protecting: a change that makes the device layer need the database would cost
this tool as a side effect.

**It writes nothing, and that is deliberate rather than lazy.** §7.3 assigns
every job-row edge to Flask or the sibling. A third writer would put rows in
the database that no approval and no `runner_instance_id` accounts for —
worse than an honest gap, because they would look like a normal run. So a
manual run is *absent from the audit trail*, the tool says so on every
invocation and again when it finishes, and the answer to "we need this
recorded" is to fix NetHub and use it.

**It is a bypass of the web app, not of §4.3.** The one thing it must never
become is a way around the host-key pin:

- No confirmed pin and no `--fingerprint` is a **refusal**, never a silent
  first-contact accept. The error tells the operator to `--scan`, compare
  against the device, and pass what they saw.
- A `--fingerprint` the operator typed *is* a confirmation — they compared it
  out of band, which is exactly what the web flow asks of them. Someone with
  host access could edit `device_host_keys` anyway, so this grants nothing
  host access did not already imply (the same argument §4.4 makes for the
  break-glass CLI); what it does not do is let a mismatch through quietly.
- It reads a confirmed pin from the database when one is reachable, and
  treats an unreachable database as ordinary — NetHub being down is the
  reason the tool exists.

**The confirmations are §8.1's gates collapsed onto a terminal**, because
there is no second person to ask. Every phase in `MUTATING`
(`stage`/`activate`/`cleanup`) prompts; `precheck` and `verify` are read-only
and do not. `--phases` defaults to `precheck` alone, so a bare invocation
cannot change anything; `--phases all` runs the lot. `--yes` skips the
prompts, which is what a scripted recovery wants and what an interactive
operator should not reach for.

Two error-path details that matter more here than in a library:

- **Netmiko's own exceptions are caught alongside ours.** A device that hangs
  raises `NetmikoBaseException`, and an operator recovering a fleet should get
  a sentence rather than a traceback.
- **The first `connect()` is inside the `try`.** A host-key mismatch is raised
  there, and it is the single message this tool most needs to print as prose:
  `FAILED: HostKeyError: ... Refusing to send a credential.`

Verified against the lab switch: `--scan` prints the same fingerprint
`ssh-keygen -lf` does, a `precheck` runs to completion with the database
deliberately unreachable, and a deliberately wrong `--fingerprint` fails
closed without sending the credential.

## Keeping this file current

This file is derived from `design-document.md` plus the actual state of the repo,
and it goes stale silently. When a change lands that alters what a future
session needs to know — a design decision, a command, a dependency, a new
directory, a rule about what must not be built — update this file in the
same pass. The `design-doc-sync` skill in `.claude/skills/` covers what to
check and what belongs here.
