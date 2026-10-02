# CLAUDE.md

NetHub upgrades Cisco IOS-XE switches. It has two parts: a Flask web app
where people upload images and approve each phase, and a separate
**sibling** process that does all the device work over Netmiko and SCP.

**The docs live in `docs/`. Start at [docs/index.md](docs/index.md)**,
which routes a task to its topic file. Each topic file follows HADS:
- read the `[SPEC]` and `[BUG]` blocks;
- skip `[NOTE]` unless you need the reasoning;
- every rule has a `[slug]` and names the tests that enforce it.

## Status

- **Built:**
  - local login (admin and operator roles), user management and the
    optional two-person rules;
  - the artifact store and host-key pinning;
  - the day-2 upgrade path: pre-check → stage → activate (with a canary)
    → verify → cleanup, with gates, retries and scheduled approvals.
- **Validated** on a Catalyst 9200CX running IOS-XE 17.12.06, including
  full upgrade round trips.
- **Not built:** day-0 provisioning, OIDC, server-side sessions,
  retention and a JSON API. See [docs/future.md](docs/future.md), and
  check the code before assuming anything there exists.

## Where to look

| Touching… | Read |
|---|---|
| anything, for the first time | [architecture.md](docs/architecture.md) |
| `sibling.py`, `devices/phases.py`, gates, retry, cancel, scheduling | [dispatch.md](docs/dispatch.md) |
| the device password, `sealed_credentials.py`, `error_summary` | [credentials.md](docs/credentials.md) |
| `devices/{connection,transfer,install,facts}.py`, `upgrade_cli.py` | [device-layer.md](docs/device-layer.md) |
| `/hostkeys`, pins, scans, target addresses | [host-keys.md](docs/host-keys.md) |
| `artifacts.py`, uploads, `check-store` | [artifacts.md](docs/artifacts.md) |
| `auth.py`, `settings.py`, roles, sessions | [auth-and-roles.md](docs/auth-and-roles.md) |
| `models.py`, migrations, SQLite | [schema.md](docs/schema.md) |
| Containerfile, Quadlet units, env vars, CI | [deployment.md](docs/deployment.md) |
| templates, CSS, `web.py`, `worker_status.py` | [frontend.md](docs/frontend.md) |
| tests, fixtures, captures | [testing.md](docs/testing.md) |

## Commands

```bash
pip install --require-hashes -r requirements-dev.txt   # CI's exact tool versions
pytest                                                  # all tests
ruff check . && yamllint .                              # lint as CI does

export SECRET_KEY=$(openssl rand -hex 32)               # ≥32 chars, required
export NETHUB_CREDENTIAL_PUBLIC_KEY=...                 # from the keygen below
flask --app nethub run                                  # dev server (DEBUG off)
flask --app nethub create-admin <username>
flask --app nethub check-store                          # re-hash the store; exit 1 on drift
flask --app nethub db current | db history | db migrate -m "..."   # see docs/schema.md
python -m nethub.sealed_credentials keygen --out <file>
python -m nethub.sibling                                # needs NETHUB_SEARCH_DIR + private key
python -m nethub.upgrade_cli --scan <host>              # manual escape hatch (docs/device-layer.md)
python scripts/check_device_facts.py <ip> --user <name> | --replay <dir>
scripts/smoke_test.sh [image]                           # ENGINE=docker if no podman

pip-compile --generate-hashes --allow-unsafe --strip-extras --output-file=requirements.txt requirements.in
pip-compile --generate-hashes --allow-unsafe --strip-extras --output-file=requirements-dev.txt requirements-dev.in
# Python 3.12, runtime lock first; CI fails on any diff
```

## Hard rules

These are settled. If a change seems to need breaking one, read the
linked rule and its reasoning first. The design is what needs revisiting,
not the rule.

- **No device I/O in Flask.** Flask writes a `queued` row, and the
  sibling does the rest; the job row is the only channel.
  [architecture.md](docs/architecture.md) [no-device-io-in-flask],
  [dispatch.md](docs/dispatch.md) [flask-writes-only-queued]
- **No user-supplied code, inventories, templates, filenames, digests or
  usernames.** A request is hosts plus one bundle key.
  [architecture.md](docs/architecture.md) [request-is-a-closed-document]
- **The device username is read server-side** from the supplier's
  `users.device_username`. The target address is the only connection
  field a request supplies, and it must be an IP literal inside
  `DEVICE_TARGET_CIDRS`. [credentials.md](docs/credentials.md)
  [device-username-is-server-side], [host-keys.md](docs/host-keys.md)
  [target-is-an-ip-in-a-cidr]
- **No trust on first use.** `connect()` requires a pin, and an address
  with no *confirmed* pin cannot be submitted.
  [device-layer.md](docs/device-layer.md) [connect-requires-a-pin],
  [host-keys.md](docs/host-keys.md) [no-confirmed-pin-no-run]
- **The device password reaches disk only sealed, and only while its job
  is queued.** `error_summary` copies only an exception's `summary`,
  never `str(exc)`. [credentials.md](docs/credentials.md)
  [ciphertext-only-while-queued], [error-summary-reads-summary-attr]
- **Privilege 15 at login. No `enable`, no enable secret.**
  [credentials.md](docs/credentials.md) [privilege-15-at-login]
- **The SCP-server toggle is always bracketed by a confirmed restore.**
  An unconfirmed restore fails the host.
  [device-layer.md](docs/device-layer.md) [scp-bracket-confirmed]
- **SHA-512 is the only hash, with no `hash_algo` column. MD5 was
  rejected.** [artifacts.md](docs/artifacts.md) §5
- **Push over SCP is the only transport, and NetHub is the only source of
  the bytes.** No OpenSSH `scp` subprocess.
  [device-layer.md](docs/device-layer.md) §7
- **A wave stops only on NetHub's own faults (`credential`, `internal`,
  `store`) and a failed canary.** An activate host that got past its
  login is never sent `install add` again. [dispatch.md](docs/dispatch.md)
  [wave-stops-on-nethub-faults], [activated-host-never-kept]
- **Every claim is a conditional update, and the rowcount is the
  answer.** This covers jobs, gates, publish, withdraw and delete
  requests. [dispatch.md](docs/dispatch.md) [claim-is-conditional]
- **One host, two separate Quadlet units, one SQLite file.** Never a
  shared `Pod=`. Never add `NoNewPrivileges=` or `RestrictSUIDSGID=`.
  [deployment.md](docs/deployment.md) §1, §3
- **`DEBUG` stays off unless the environment explicitly sets it.**
  [credentials.md](docs/credentials.md) [debug-off]
- **Only the web process migrates.** Migrations use literals, and
  `tests/test_schema.py` keeps them equal to the models.
  [schema.md](docs/schema.md) §2
- **No JavaScript anywhere. The CSP is `script-src 'none'`.**
  [frontend.md](docs/frontend.md) [no-javascript]
- **Login always pays for a password hash, and every failure gets one
  message.** [auth-and-roles.md](docs/auth-and-roles.md)
  [login-always-pays-for-a-hash]
- **`tests/captures/` and `tests/fixtures/schemas/` are evidence. Never
  edit them.** [testing.md](docs/testing.md)
- **NetHub is not an inventory.** Nothing combines runs into a
  per-device current state. [architecture.md](docs/architecture.md)
  [not-an-inventory]

## Keeping the docs true

- When a change alters behaviour that a `docs/` rule describes, update the
  rule in the same commit.
- A new rule needs a slug and a `Pinned by:` test (or `none`, listed under
  the file's Known gaps).
- When a feature is removed, delete its rules; never mark them obsolete.
- No history in docs or code comments; git holds it.
- `docs/index.md` §2 is the format spec. The `docs-sync` skill
  (`.claude/skills/docs-sync`) is the checklist, and `tests/test_docs.py`
  checks the citations.
