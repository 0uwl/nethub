# Deployment
Code: `Containerfile`, `Containerfile.dev`, `dev.sh`, `nethub/gunicorn.conf.py`, `quadlet/nethub.container`, `quadlet/nethub-sibling.container`, `nethub/shared_config.py`, `nethub/config.py`, `nethub/credentials.py`, `.github/workflows/cicd.yml`, `.github/actions/trivy-scan/`, `.github/dependabot.yml`, `scripts/smoke_test.sh`, `scripts/trivy_report.sh`, `requirements*.in`/`.txt` · Tests: `tests/test_config.py`, `tests/test_credentials.py`, `scripts/smoke_test.sh` (run in CI)

---

## AI READING INSTRUCTION

Read `[SPEC]` and `[BUG]` blocks for authoritative facts.
Read `[NOTE]` only if additional context is needed.
`[?]` blocks are unverified; treat them with lower confidence.
§1–§4 are rules: each H3 heading is a rule's slug. §5 explains how it
works (the environment variable table, commands), §6 decisions, §7
pitfalls, §8 known gaps.

---

## 1. Processes and units

### 1.1 [two-units-one-image]
**[SPEC]**
A deployment is two Quadlet units built from the same image:
- `nethub.container` runs gunicorn (the UI).
- `nethub-sibling.container` runs `python -m nethub.sibling`, which does
  all device work.

They share only the data volume (the database) and the artifact store.
Pinned by: `scripts/smoke_test.sh`

### 1.2 [separate-units-not-a-pod]
**[SPEC]**
The two must stay separate units in separate PID namespaces; never put
them in a shared `Pod=`. Separate namespaces are what stop same-uid
`ptrace` and `/proc/<pid>/mem` access between Flask and the sibling.
Pinned by: none

### 1.3 [everything-on-one-host]
**[SPEC]**
Both units run on one host under one rootless user. SQLite is written by
both processes, and its locking is unreliable on network filesystems.
Pinned by: none

### 1.4 [sibling-needs-no-secret-key]
**[SPEC]**
The sibling loads only `shared_config.py` (`sibling._database_app()`),
never `config.py`, so it needs no `SECRET_KEY`, and its unit carries none.
It cannot forge a session.
Pinned by: `tests/test_sibling.py::TestNoSecretKey::test_the_sibling_starts_without_a_key`, `tests/test_sibling.py::TestNoSecretKey::test_the_database_app_loads_without_a_key`

### 1.5 [no-sibling-no-work]
**[SPEC]**
Without the sibling unit, scans and phase jobs sit at `queued` forever.
`sweep()` reclaims only `running` rows, and after a minute the page asks
"Is nethub-sibling running?" (frontend.md). Neither unit depends on the
other being up, and a web restart between an approval and its phase
costs nothing.
Pinned by: `tests/test_end_to_end.py::TestWebRestart::test_a_web_restart_between_approval_and_claim_costs_nothing`

---

## 2. gunicorn and the image

### 2.1 [one-worker-four-threads]
**[SPEC]**
`gunicorn.conf.py` sets `workers = 1`, `worker_class = 'gthread'` and
`threads = 4`. Threads are required: with gunicorn's defaults (`sync`,
one thread), a 1.5 GB upload holds the whole server. `timeout = 120` is
safe for long uploads only under `gthread`.
Pinned by: none

**[NOTE]**
One worker is a tuning choice made for SQLite, not a correctness rule.
Raising it means measuring SQLite with two web writers plus the sibling
first.

### 2.2 [gunicorn-app-is-a-call]
**[SPEC]**
The entrypoint is
`gunicorn -c nethub/gunicorn.conf.py "nethub:create_app()"`, a call
expression, because gunicorn 26.x has no `--factory` flag. Check
`gunicorn --help` against the locked version before changing it.
Pinned by: `scripts/smoke_test.sh`

### 2.3 [control-socket-disabled]
**[SPEC]**
`control_socket_disable = True`. gunicorn ≥25.1 otherwise creates
`$HOME/.gunicorn/gunicorn.ctl`, which fails under the units' read-only
root (`ReadOnly=true`).
Pinned by: `scripts/smoke_test.sh`

### 2.4 [image-shape]
**[SPEC]**
- The base is `python:3.12-slim`, pinned by digest.
- The app runs as non-root UID 1000.
- Dependencies are installed with `pip install --require-hashes -r
  requirements.txt`; pytest and the linters are not in the image.
- `/app/data` and `/app/artifacts` are mount points, never baked in.

Pinned by: none

### 2.5 [dev-image-is-dev-only]
**[SPEC]**
`Containerfile.dev` and `dev.sh` run Flask's dev server with `--debug`
(reloader and interactive debugger) against the repo bind-mounted at
`/app`. Never use them for anything but local development.
Pinned by: none

---

## 3. Quadlet units

### 3.1 [no-nonewprivileges-or-restrictsuidsgid]
**[SPEC]**
Never add `NoNewPrivileges=` or `RestrictSUIDSGID=` to `[Service]`.
`[Service]` directives constrain the podman client on the host, and both
break the setuid binaries rootless Podman needs.
Pinned by: none

**[NOTE]**
- `RestrictSUIDSGID=true` fails at once with `storage-chown-by-maps:
  chmod usr/bin/chfn: operation not permitted`. This happens when the
  overlay store cannot shift, so Podman makes a chowned copy of the
  image's setuid files.
- `NoNewPrivileges=true` stops `newuidmap`/`newgidmap` from gaining
  privilege, so they cannot write `uid_map`. It is latent: things work
  until the next reboot or `podman system migrate`, then fail with an
  error that names neither systemd nor the unit.

### 3.2 [keep-id-required]
**[SPEC]**
`UserNS=keep-id:uid=1000,gid=1000` is required, or the bind mounts fail
with `unable to open database file`. Rootless Podman's default user
namespace does not map UID 1000 one-to-one.
Pinned by: `scripts/smoke_test.sh`

### 3.3 [shared-volumes-lowercase-z]
**[SPEC]**
The data and artifact directories, which both units mount, use `:z`
(shared label), not `:Z`. On an enforcing SELinux host, `:Z` would let
the second unit to start relabel the directory and lock the first out.
The sibling's private-key mount (`/run/nethub-key`) is `:Z` on purpose:
nothing else should read it.
Pinned by: none

### 3.4 [credentials-in-service-section]
**[SPEC]**
`LoadCredential=`/`SetCredential=` go in `[Service]`, not `[Container]`.
Credentials NetHub reads by name are `secret_key`, `admin_password` and
`credential_private_key`. Whether Podman forwards `$CREDENTIALS_DIRECTORY`
into the container depends on its version; check it on the deployed
Podman.
Pinned by: `tests/test_credentials.py`

### 3.5 [core-dumps-off]
**[SPEC]**
Both units set `LimitCORE=0` and `ProtectProc=invisible`, and
`Restart=on-failure`; the sibling also sets `RestartSec=5`. `LimitCORE=0`
has been confirmed to reach the container process: its `/proc/self/limits`
reads 0.
Pinned by: none

### 3.6 [sibling-has-no-run-tmpfs]
**[SPEC]**
Only the web unit mounts `Tmpfs=/run`. The sibling omits it, because a
tmpfs over `/run` would hide the `/run/nethub-key` mount.
Pinned by: `scripts/smoke_test.sh`

---

## 4. CI and releases (`.github/workflows/cicd.yml`)

### 4.1 [ci-jobs]
**[SPEC]**
On pull requests and pushes to `main`, three jobs run:
- `lint`: ruff, yamllint, shellcheck on `scripts/*.sh`, zizmor on
  `.github/`, the lockfile check, and droast on `Containerfile`.
- `test`: `pytest -v`.
- `image`: an amd64 build, then `scripts/smoke_test.sh`, then Trivy.

Weekly (cron `23 5 * * 1`) and on demand, `scan-published` scans
`ghcr.io/<repo>:latest`.
Pinned by: none

### 4.2 [only-a-tag-publishes]
**[SPEC]**
Only a `v*` tag publishes.
- `publish` needs `lint` and `test`. It builds amd64, smoke-tests and
  scans that image, then pushes amd64+arm64 as `:X.Y.Z`, `:X.Y` and
  `:latest`.
- A hyphenated prerelease tag gets only its own tag. This relies on
  `docker/metadata-action`'s semver patterns and `latest=auto`, not on
  anything written in the workflow.
- The GitHub release is created last.
- A merge to `main` publishes nothing.

Pinned by: none

### 4.3 [actions-pinned-by-sha]
**[SPEC]**
- Every `uses:` is pinned to a commit SHA, with its tag in a comment
  separated by two spaces. zizmor fails a new unpinned one.
- droast is pinned twice: the action by SHA, and its `image-tag` input
  by tag and digest.
- The local action is called as `uses: $/.github/actions/trivy-scan`.
  Dependabot lists `/.github/actions/*` so it also sees that action's
  own pins.

Pinned by: none

### 4.4 [trivy-reports-never-fails]
**[SPEC]**
- Every scan goes through `.github/actions/trivy-scan`, with
  `SCAN_SEVERITY: 'CRITICAL,HIGH'` and `limit-severities-for-sarif: true`.
- There is no `exit-code`, so a finding never fails a job. A scan that
  produces no SARIF at all does fail it.
- Results go to the job summary (`scripts/trivy_report.sh`), a warning
  annotation and GitHub code scanning.

Pinned by: none

**[NOTE]**
Without `limit-severities-for-sarif`, the SARIF output includes every
LOW and MEDIUM finding: 46 against 7 on the pinned base. The scans are
advisory because almost every finding is in the Debian base image, which
NetHub cannot patch. A finding in NetHub's own pinned Python packages is
fixed by bumping the pin.

### 4.5 [least-permission-and-no-shared-cache-on-release]
**[SPEC]**
- Permissions default to `contents: read`:
  - `publish` gets `packages: write` and `contents: write`;
  - `scan-published` gets `packages: read`;
  - the scanning jobs get `security-events: write`.
- Checkouts use `persist-credentials: false`, and untrusted values reach
  the shell only through `env:`.
- Nothing on the tag path reads a cache another run wrote: `lint` and
  `test` use no pip cache, and `publish` builds with `no-cache`,
  passing layers on through a job-local `type=local` cache.

Pinned by: none

### 4.6 [linters-and-locks-hashed]
**[SPEC]**
- `requirements.in` lists the direct runtime dependencies.
- `requirements.txt` is compiled from it by `pip-compile --generate-hashes`
  with every transitive package pinned.
- `requirements-dev.in` pulls in the runtime set with `-c requirements.txt`
  and adds pytest, pip-tools and the linters.
- Compile on Python 3.12, runtime lock first. CI recompiles both and
  fails on any diff.

Pinned by: none

**[NOTE]**
The linters are pinned in the dev lockfile so that local runs match CI.
An unpinned ruff once widened its default rules and failed CI on files
that passed locally. When a pin is bumped, fix the new findings; do not
unpin.

---

## 5. How it works

### 5.1 Environment variables
**[SPEC]**
| variable | read by | default | notes |
|---|---|---|---|
| `SECRET_KEY` | web | none, required | credential `secret_key` wins; ≥32 chars, no placeholders (auth-and-roles.md) |
| `DATABASE_PATH` | both | `<repo>/database.db` | units: `/app/data/database.db` |
| `ARTIFACT_STORE` | loaded by both, used by web | `<repo>/instance/artifacts` | units: `/app/artifacts` |
| `NETHUB_SEARCH_DIR` | sibling | none, required | where the push reads images; units: `/app/artifacts`. Nothing checks that it equals `ARTIFACT_STORE`; keep them the same |
| `NETHUB_CREDENTIAL_PUBLIC_KEY` | both | none, required | base64; web refuses to start without it |
| `NETHUB_CREDENTIAL_KEY_FILE` | sibling | none | private key file; credential `credential_private_key` wins |
| `PHASE_CONCURRENCY` | both | `4` (also when blank) | otherwise a whole number ≥1, or the process refuses to start; set the same in both units |
| `DEVICE_TARGET_CIDRS` | web (the sibling unit leaves it unset on purpose) | empty, so every target is refused | comma-separated |
| `SESSION_COOKIE_INSECURE` | web | unset (cookie `Secure`) | `1` for local HTTP only |
| `NETHUB_PORT` | gunicorn | `8080` | match `PublishPort=` |
| `ADMIN_USERNAME` | web | none | first boot only; no default name |
| `ADMIN_PASSWORD` | web | random, printed | first boot only; credential `admin_password` wins |
| `DEBUG` | web (Flask) | off | `1` enables; local dev only (credentials.md [debug-off]) |
| `NETHUB_DEVICE_PASSWORD` | `upgrade_cli`, `check_device_facts` | none | warns when used |

`MAX_CONTENT_LENGTH` is a constant in `config.py` (1500 MiB); it is
**not** read from the environment.

### 5.2 Commands
**[SPEC]**
```
podman build -t localhost/nethub:latest -f Containerfile .
scripts/smoke_test.sh [image]          # ENGINE=docker if no podman; SMOKE_PORT, SMOKE_TIMEOUT
./dev.sh                               # dev container with reload; needs SECRET_KEY, DEVICE_TARGET_CIDRS
python -m nethub.sealed_credentials keygen --out <file>
pip-compile --generate-hashes --allow-unsafe --strip-extras --output-file=requirements.txt requirements.in
pip-compile --generate-hashes --allow-unsafe --strip-extras --output-file=requirements-dev.txt requirements-dev.in
```

### 5.3 Upgrading NetHub
**[SPEC]**
1. Back up the database.
2. Change the image tag in both units.
3. Restart.

The web unit migrates at startup, and the sibling waits for it
(schema.md). To go back, restore the backup: an older image refuses a
newer database.

### 5.4 What the smoke test covers
**[SPEC]**
`scripts/smoke_test.sh` runs both containers with the units' flags:
read-only root, tmpfs, UID 1000, `keep-id` under podman, and shared `:z`
volumes. It checks:
- the image's keygen;
- that the sibling waits for the migration;
- `GET /login` returns 200 with its CSP;
- the admin bootstrap;
- the sibling's start line.

It does **not** cover SELinux (GitHub's runners use AppArmor) and does
not run the units under systemd or Quadlet.

---

## 6. Decisions

**[NOTE]**
- **Two units, not three.** The device credential travels sealed in the
  job row, so no socket unit or entrypoint shim is needed.
- **DHCP (day-0, not built) stays native.** It binds a privileged
  broadcast port and is outside the credential trust argument.
- **Trivy is advisory (CRITICAL,HIGH).** The maintainer chose this because
  a gate kept blocking merges on Debian fixes NetHub cannot ship.
- **The smoke test does not cover SELinux.** The maintainer accepted
  this; don't claim otherwise.

---

## 7. Pitfalls

**[BUG] The artifacts page returns 500 in the container**
- Symptom: a 500 on load, or a write error to `instance/artifacts`.
- Cause: `ARTIFACT_STORE` is not set, so it defaults onto the read-only
  image layer.
- Fix: set `ARTIFACT_STORE=/app/artifacts` with a volume behind it, as
  both units do.

**[BUG] Startup fails under `ReadOnly=true` creating `.gunicorn/gunicorn.ctl`**
- Symptom: gunicorn exits at start in the read-only container.
- Cause: gunicorn ≥25.1's control socket.
- Fix: rule 2.3.

**[BUG] `unable to open database file`**
- Symptom: either container fails on its first database access.
- Cause: no `UserNS=keep-id:uid=1000,gid=1000`, or a data directory the
  container user cannot write.
- Fix: rule 3.2. Check the host directory's owner.

---

## 8. Known gaps

**[SPEC]**
- `quadlet/nethub.container` has a commented
  `MAX_CONTENT_LENGTH=1610612736` line (1536 MiB), but nothing reads that
  variable, and the real cap is 1500 MiB. Uncommenting it changes
  nothing.
- When Python moves past 3.12, update the workflow's `python-version` and
  recompile the locks by hand. Dependabot sends digest updates for the
  base image only.
- The `:z` volumes have not been tested on an enforcing SELinux host.
- The two-unit layout has been run with docker and podman by hand. It
  has not been run under Quadlet since the `ARTIFACT_STORE` volume was
  added.
- `PR_SET_DUMPABLE` is not set (credentials.md).
- Most rules here are pinned only by the CI smoke test or by inspection.
