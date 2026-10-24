# Contributing

## Before you start

- Read [CLAUDE.md](CLAUDE.md) for the hard rules, then
  [docs/index.md](docs/index.md), which sends each area of the code to its
  topic file. Read the topic file for the code you're about to touch. A
  change that seems to need breaking a hard rule needs a design discussion
  first, not a workaround.
- Install [uv](https://docs.astral.sh/uv/) and run `uv sync`. It installs
  Python 3.12 and CI's exact tool versions into `.venv`; run the commands
  below with `uv run` or after activating it.

## Development server

For local development with live edits:

```bash
export SECRET_KEY=$(openssl rand -hex 32)
export DEVICE_TARGET_CIDRS=192.0.2.0/24   # the CIDRs your lab switches are in
./dev.sh
```

This builds `Containerfile.dev` (Flask's own dev server, debug + reload)
and runs it with the repo bind-mounted in, at `http://localhost:8080`. On
first run it also creates a local key pair in `instance/credential_private_key`
(gitignored) and passes the public half to the app, which refuses to start
without one.

- Log in as `admin` / `dev-admin` unless you set `ADMIN_USERNAME` or
  `ADMIN_PASSWORD`. The admin is only created while the database has no
  users.
- Dev data lives in the repo: `database.db` and `instance/artifacts/`, both
  gitignored. Delete them to start over.
- `dev.sh` sets `SESSION_COOKIE_INSECURE=1`, so you can reach it over a LAN
  address. Never set that on a real deployment.

`dev.sh` runs only the web app. Without the sibling, scans and runs sit at
"Still working". To do device work, run the sibling on the host against the
same database and key pair:

```bash
export NETHUB_CREDENTIAL_KEY_FILE=instance/credential_private_key
export NETHUB_CREDENTIAL_PUBLIC_KEY=$(python -m nethub.sealed_credentials public-key --key instance/credential_private_key)
NETHUB_SEARCH_DIR=instance/artifacts python -m nethub.sibling
```

## Testing

```bash
pytest                            # runs tests/ -- see tests/conftest.py for the
                                   # app/client fixtures (temp DB + artifact store per test)
pytest tests/test_schema.py -q    # one file
ruff check . && yamllint .        # lint as CI does
```

[docs/testing.md](docs/testing.md) has the fixtures and pitfalls. The ones
that catch people most often:

- Read the fixtures at the top of a test file before adding to it. Several
  change the environment for the whole file, such as `DEVICE_TARGET_CIDRS`
  in `test_upgrade_routes.py` and `test_roles.py`.
- `make_user` makes an admin with no `device_username`. A bare `User(...)`
  is an operator. Tests that submit or approve a run set a device username
  by hand.
- Add every new admin-only route to `ADMIN_ONLY` in `tests/test_roles.py`.
- Some test files import helpers from others. Grep for `from tests.` before
  renaming one.
- `tests/captures/` and `tests/fixtures/schemas/` are evidence. Never edit
  them to make a test pass.

`.github/workflows/ci.yml` runs on every pull request and push to `main`:
lint (`ruff`, `yamllint`, `shellcheck`, the `Containerfile`, `zizmor` over
the workflow itself, and a check that `uv.lock` matches
`pyproject.toml`), the test suite, and an image build that is smoke-tested
(`scripts/smoke_test.sh`: web and sibling containers started the way the
Quadlet units start them) and scanned by Trivy for fixable CRITICAL and HIGH
CVEs. It also scans the published image weekly. The scans report and do not
fail the build: findings are listed in the job summary, flagged as a warning
on the run, and uploaded to the repository's Security tab (Code scanning).
Most are in the Debian base image and are fixed by Dependabot's base-image
update; one in a pinned Python package is fixed by bumping the pin. Dependabot proposes updates to
the pinned actions, Python packages and base image. A merge to `main` publishes
nothing; see [Releasing](#releasing).

To change a dependency, edit `pyproject.toml` (`dependencies` for the
runtime, the `dev` group for tools), run `uv lock` and commit `uv.lock`. CI
fails a pull request whose lock is stale.

To run the smoke test locally against an image you built:
`scripts/smoke_test.sh localhost/nethub:latest` (podman, as the units use;
`ENGINE=docker` also works).

## Changing the database schema

Only the web process migrates, at startup. To change a table:

1. Change `nethub/models.py`.
2. Draft a migration with `flask --app nethub db migrate -m "what changed"`.
3. Edit the draft by hand. Replace every model import with literal values,
   and name the file `NNNN_what_it_does`.
4. Run `pytest tests/test_schema.py`. It checks that the migrated database
   is identical to the models.

[docs/schema.md](docs/schema.md) §4.3 lists what autogenerate misses here:
CHECK constraint changes and the terminal-status trigger.

## Validating a new IOS-XE release

Capture real output from a switch running it, then replay the capture offline:

```bash
python scripts/check_device_facts.py <ip> --user <name>
python scripts/check_device_facts.py --replay <capture-dir>
```

Add the capture as a new `tests/captures/<device>-<release>/` directory.
[docs/device-layer.md](docs/device-layer.md) covers the device layer.

## Keeping the docs true

When a change alters behaviour a `docs/` rule describes, update the rule
in the same commit. A new rule needs a `[slug]` and a `Pinned by:` test,
or `none` listed under the file's Known gaps. Write docs and comments in the
present tense with no history; git holds it. `tests/test_docs.py` fails
when a cited test or slug no longer exists. The `docs-sync` skill
(`.claude/skills/docs-sync`) is the full checklist.

## Releasing

Push a version tag: `git tag v1.4.0 && git push origin v1.4.0`. CI lints and
tests, builds the image, runs the smoke test and the Trivy scan (which reports
findings but does not stop the release), and only then pushes `ghcr.io/<owner>/nethub` as `:1.4.0`, `:1.4` and `:latest` (amd64 and
arm64), then creates the GitHub release with generated notes. A tag with a
hyphen (`v1.4.0-rc1`) is a prerelease: it publishes `:1.4.0-rc1` only, never
`:latest`. A release drafted by hand in the web UI works too; its notes are
left alone.
