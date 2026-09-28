# NetHub

> NetHub is in early bootstrap — see [Project status](#project-status)
> below for what's actually implemented.

NetHub is a small, self-hosted dashboard that unifies both halves of a
network device's lifecycle in one Flask app, one database, one admin UI:

- **Provisioning (day-0)** — a new device phones home, is checked against
  a serial allowlist, and receives its initial config/image.
- **Software Lifecycle (day-2)** — admins onboard IOS-XE software images
  and install them on devices, one approval gate per phase.

It's aimed at smaller teams who want ZTP + upgrade management without
standing up an enterprise-scale vendor platform. Vendor scope is Cisco
IOS-XE only for now.

NetHub is a new, standalone project extending
[Drawbridge](https://github.com/0uwl/drawbridge); it combines Drawbridge's
provisioning system with a previously-standalone "Network Software Depot"
concept into one unified tool. Drawbridge will be deprecated in favor of
this project once its provisioning module reaches parity.

## Project status

Provisioning (day-0) is entirely unimplemented. A first slice of
Software Lifecycle exists: local username/password auth (everyone who
logs in is an admin — no roles yet), an artifact store NetHub owns, a
fail-closed SSH host-key pin, and the day-2 upgrade path.

There is no Ansible anywhere in this repo any more. Device work is
ordinary Python over [Netmiko](https://github.com/ktbyers/netmiko) in
`nethub/devices/`, dispatched out-of-band by a sibling process rather
than from a request handler. The device layer is validated against real
hardware (a Catalyst 9200CX on IOS-XE 17.12.06, including two live
upgrades); a full run driven end-to-end from the UI has not been done
yet.

See [CLAUDE.md](CLAUDE.md) for that slice's exact scope and its deliberate
deviations from the design below. The full target architecture — data
model, security model, failure/concurrency semantics — is written up in
[design-document.md](design-document.md). Treat that document as the
design target, not a description of current code.

## Running it

```bash
pip install --require-hashes -r requirements-dev.txt   # the runtime set plus
                                   # pytest and CI's own linters, at CI's versions.
                                   # Both files are hashed lockfiles compiled from
                                   # requirements.in / requirements-dev.in on
                                   # Python 3.12; requirements.txt alone is what
                                   # the image installs.

export SECRET_KEY=$(openssl rand -hex 32)   # required; nethub/config.py rejects an absent
                                   # key, a known placeholder, or anything under 32 chars
flask --app nethub run            # runs the dev server (DEBUG defaults off; --debug to override)

flask --app nethub create-admin <username>   # bootstrap the first login user --
                                              # there is no self-registration route
```

The database is created, and kept up to date, by NetHub itself: every start
applies any pending migrations before serving (see
[Upgrading NetHub](#upgrading-nethub)). `flask --app nethub db current` and
`flask --app nethub db history` show where a database stands.

Uploaded image bytes land in `instance/artifacts/` (next to the repo
root) — set `ARTIFACT_STORE` to point it elsewhere, and do set it in a
container, where the default falls on a read-only layer. Submitting an
upgrade also needs `DEVICE_TARGET_CIDRS`, which has no default and
refuses every submit while unset. See [Using it](#using-it) below for the
admin flow.

```bash
pytest                            # runs tests/ -- see tests/conftest.py for the
                                   # app/client fixtures (temp DB + artifact store per test)
```

`.github/workflows/cicd.yml` runs on every pull request and push to `main`:
lint (`ruff`, `yamllint`, `shellcheck`, the `Containerfile`, `zizmor` over
the workflow itself, and a check that the lockfiles match their `.in`
files), the test suite, and an image build that is smoke-tested
(`scripts/smoke_test.sh`: web and sibling containers started the way the
Quadlet units start them) and scanned by Trivy for fixable CRITICAL and HIGH
CVEs. It also scans the published image weekly. Dependabot proposes updates to
the pinned actions, Python packages and base image. A merge to `main` publishes
nothing; see [Releasing](#releasing).

To change a dependency, edit `requirements.in` (or `requirements-dev.in`) and
regenerate both lockfiles on Python 3.12 with the commands at the top of
`requirements.in`. CI fails a pull request whose lockfiles are stale.

To run the smoke test locally against an image you built:
`scripts/smoke_test.sh localhost/nethub:latest` (podman, as the units use;
`ENGINE=docker` also works).

### Releasing

Push a version tag: `git tag v1.4.0 && git push origin v1.4.0`. CI lints and
tests, builds the image, runs the smoke test and the Trivy scan, and only then
pushes `ghcr.io/<owner>/nethub` as `:1.4.0`, `:1.4` and `:latest` (amd64 and
arm64), then creates the GitHub release with generated notes. A tag with a
hyphen (`v1.4.0-rc1`) is a prerelease: it publishes `:1.4.0-rc1` only, never
`:latest`. A release drafted by hand in the web UI works too; its notes are
left alone.

## Using it

Once you're logged in as an admin:

1. **Profile** — set your *device* username: the name NetHub logs into
   switches with on your behalf. It is separate from your NetHub login,
   is read server-side so a request can never assert it, and an upgrade
   cannot be submitted without it.
2. **Artifacts → New artifact** — publish an image: a bundle key (what
   an upgrade request names to select it), a version, the image's
   SHA-512, and the file. NetHub hashes the bytes as it receives them
   and rejects the upload if the digest doesn't match what you claimed;
   the submitted checksum is the claim being verified, never what gets
   recorded.
3. **Host keys → Scan** — for each device address, fetch its SSH host
   key and compare the fingerprint against the device itself, out of
   band. Then **Confirm** it. This spends no credential, and an address
   with no confirmed key cannot be named by a run at all — first contact
   is a deliberate admin action rather than trust-on-first-use.
4. **Upgrades → New run** — pick a published bundle, list
   `hostname, address` pairs (IP literals inside a configured CIDR), and
   enter your device password. Pre-check runs immediately: privilege 15,
   boot mode, free space.
5. The run then parks at an **approval gate** before each phase that
   touches a device — stage, activate, cleanup. Each gate collects your
   device password again for that one phase. It is stored only encrypted,
   sealed to a key only the dispatch process holds, and only until that
   phase starts; never in the clear in a row, a log, or the session.
   Activate reloads the device; verify runs after it comes back, on the
   password the activate approval supplied.
   A host that fails a phase does not stop the others: the run carries on
   with the hosts that passed, and the next gate offers to **retry** the
   failed ones (collecting your password again). If your password is
   refused, the phase stops at that host instead of trying it on every
   device, so a typo cannot lock your account out.
6. `flask --app nethub check-store` re-hashes every published image against
   what is on disk and flags a missing file or a digest that no longer
   matches. It is a command rather than a page, because it hashes
   everything.
7. Deleting an artifact removes the row and its image file.
8. **Users** — create accounts, reset someone's password, disable or
   re-enable an account, and unlock one that too many failed logins locked.
   Disabling someone or resetting their password ends their open sessions
   on their next click. Change your own password on your **Profile**; that
   ends your other sessions. Every one of these is recorded, and each
   user's **History** shows who did what.

Anything that deletes, reloads or disables something asks you to tick a box
stating what will happen; the server refuses the request without it. A run
or scan page reloads itself while NetHub has work in flight. If it says **No
worker has picked this up**, or marks a phase **stalled**, the
`nethub-sibling` unit is down or stuck: the web unit only queues work, and the
sibling does all of it.

## Running it in a container

```bash
podman build -t localhost/nethub:latest .

# The dispatch process's key pair: device passwords are sealed to the public
# half and only the private half can open them. Keep the private key for the
# sibling unit only.
mkdir -p ~/.config/nethub
podman run --rm --userns=keep-id:uid=1000,gid=1000 -v ~/.config/nethub:/keys:z \
  --entrypoint python localhost/nethub:latest \
  -m nethub.sealed_credentials keygen --out /keys/credential_private_key
# ...prints NETHUB_CREDENTIAL_PUBLIC_KEY=<key>

podman run --rm -p 8080:8080 \
  -e SECRET_KEY="$(openssl rand -hex 32)" \
  -e ADMIN_USERNAME=<first-user-name> \
  -e ADMIN_PASSWORD=<initial-admin-password> \
  -e ARTIFACT_STORE=/app/artifacts \
  -e NETHUB_CREDENTIAL_PUBLIC_KEY=<key> \
  localhost/nethub:latest
```

That serves the UI but performs no device work; that is the sibling's job
(`quadlet/nethub-sibling.container`). `ADMIN_USERNAME` has no default: without
it, NetHub starts with no users and logs that you need to run
`flask --app nethub create-admin <name>` inside the container.

The session cookie is `Secure` by default, so a deployment reached over plain
HTTP needs a TLS terminator in front of it. `http://localhost:8080` works as
shown (browsers treat localhost as a trustworthy origin), but reaching the
same container over a LAN address without TLS means the browser silently
drops the cookie and login bounces back to the form. Add
`-e SESSION_COOKIE_INSECURE=1` for that case, and only for local testing.

`quadlet/nethub.container` (the UI) and `quadlet/nethub-sibling.container`
(all device work) are reference Podman Quadlet units for running NetHub as
systemd user services, with every environment variable documented inline:
loading `SECRET_KEY`/`ADMIN_PASSWORD` as systemd credentials instead of
plaintext, the `ARTIFACT_STORE` volume that has to be set explicitly (its
default lands on the image's read-only layer), the public key in both units
and the private key mounted into the sibling only. Copy both to
`~/.config/containers/systemd/`, fill in the required values, then
`systemctl --user daemon-reload && systemctl --user start nethub nethub-sibling`.

For local development with live edits (no rebuild on every change):

```bash
./dev.sh
```

This builds `Containerfile.dev` (Flask's own dev server, debug + reload)
and runs it with the repo bind-mounted in, at `http://localhost:8080`. On
first run it also creates a local key pair in `instance/credential_private_key`
(gitignored) and passes the public half to the app, which refuses to start
without one.

### Upgrading NetHub

Upgrade while no phase is running (the runs list shows none `running`).
Restarting the sibling mid-phase stops that phase, and its next start marks
it abandoned: a gated phase goes back to its gate for re-approval, while an
interrupted pre-check or verify fails the run. Either way a reload cut short
is not something to cause on purpose.

1. **Back up the database.** NetHub has no downgrades; the backup is the way
   back. With the units running, SQLite's online backup is safe:
   `sqlite3 ~/.local/share/nethub/data/database.db ".backup '/path/to/nethub-$(date +%F).db'"`.
   Or stop both units and copy `database.db` together with any
   `database.db-wal`/`database.db-shm` beside it.
2. **Put the new image in place.** The reference units both run
   `localhost/nethub:latest`, so rebuild that tag, or pull a release and tag
   it (`podman pull ghcr.io/<owner>/nethub:1.4.0` then
   `podman tag ghcr.io/<owner>/nethub:1.4.0 localhost/nethub:latest`); or
   point `Image=` at the release in both `nethub.container` and
   `nethub-sibling.container`. Either way, both units must run the same image.
3. **Restart:** `systemctl --user daemon-reload && systemctl --user restart nethub nethub-sibling`.

The web unit migrates the database at startup and logs
`migrating the database from <old> to <new>`; the sibling logs that it is
waiting for this, then starts. A migration that fails changes nothing and the
web unit does not start: check `journalctl --user -u nethub`, fix or go back.

To go back to an older version, restore the backup and the older image
together. An older image refuses to start on a database a newer one has
migrated, rather than run against a schema it does not understand.

A database created before NetHub had migrations is adopted automatically on
the first start of a version that has them, including repairing the columns
earlier versions could not add or remove. If it differs in any other way,
NetHub refuses to start and lists the differences.

### Upgrading a device with NetHub switched off

`nethub/upgrade_cli.py` is a deliberate escape hatch: the Ansible
playbooks it replaced were standalone, so an operator could run them by
hand against a fleet with nothing else up. It needs no Flask app, no
sibling, and no database.

```bash
python -m nethub.upgrade_cli --scan 192.0.2.10      # print the host key fingerprint

python -m nethub.upgrade_cli --host 192.0.2.10 --user jsmith \
    --fingerprint "ssh-rsa SHA256:..." \
    --image /path/to/image.bin --sha512 <hex> \
    --version 17.12.06 --phases all
```

It **writes nothing** — a manual run is absent from the audit trail, and
the tool says so on every invocation. It is a bypass of the web app and
not of the host-key pin: with no confirmed pin and no `--fingerprint` it
refuses rather than accepting first contact. A bare invocation runs
`precheck` only, and every mutating phase prompts.

## AI usage

Parts of this codebase and its documentation are developed with AI
assistance (Claude Code). Every change is reviewed by a human maintainer
before being merged.

## Learn more

- [design-document.md](design-document.md) — full architecture, security
  model, data model, and open questions.
- [CLAUDE.md](CLAUDE.md) — the settled decisions, and the list of things
  that deliberately must not be built.
- [LICENSE](LICENSE)
