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

See [alpha.md](alpha.md) for that slice's exact scope and its deliberate
deviations from the design below, and [HANDOFF.md](HANDOFF.md) for the
open remediation work. The full target architecture — data model,
security model, failure/concurrency semantics — is written up in
[design-document.md](design-document.md). Treat that document as the
design target, not a description of current code.

## Running it

```bash
pip install -r requirements.txt   # Flask, Flask-SQLAlchemy, Flask-Login,
                                   # Flask-WTF, gunicorn, pytest, netmiko,
                                   # ntc-templates -- pinned to exact versions

export SECRET_KEY=$(openssl rand -hex 32)   # required; nethub/config.py rejects an absent
                                   # key, a known placeholder, or anything under 32 chars
flask --app nethub run            # runs the dev server (DEBUG defaults off; --debug to override)

flask --app nethub create-admin <username>   # bootstrap the first login user --
                                              # there is no self-registration route
```

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

`.github/workflows/ci.yml` lints (`ruff`, `yamllint`, plus the
`Containerfile` itself), runs the test suite, and publishes a container
image to `ghcr.io` on every push to `main` once both pass.

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
   device password again, holds it in memory for that one phase
   execution, and never writes it to a row, a log, or the session.
   Activate reloads the device; verify runs after it comes back.
6. **Check store** re-hashes every published image against what is on
   disk and flags a missing file or a digest that no longer matches. It
   is a button rather than a page load, because it hashes everything.
7. Deleting an artifact removes the row and its image file.

## Running it in a container

```bash
podman build -t localhost/nethub:latest .
podman run --rm -p 8080:8080 \
  -e SECRET_KEY="$(openssl rand -hex 32)" \
  -e ADMIN_PASSWORD=<initial-admin-password> \
  -e ARTIFACT_STORE=/app/artifacts \
  localhost/nethub:latest
```

The session cookie is `Secure` by default, so a deployment reached over plain
HTTP needs a TLS terminator in front of it. `http://localhost:8080` works as
shown (browsers treat localhost as a trustworthy origin), but reaching the
same container over a LAN address without TLS means the browser silently
drops the cookie and login bounces back to the form. Add
`-e SESSION_COOKIE_INSECURE=1` for that case, and only for local testing.

`quadlet/nethub.container` is a reference Podman Quadlet unit for
running it as a systemd user service, with every environment variable
documented inline (including loading `SECRET_KEY`/`ADMIN_PASSWORD` as
systemd credentials instead of plaintext, and the `ARTIFACT_STORE` volume
that has to be set explicitly -- its default lands on the image's read-only
layer).
Copy it to `~/.config/containers/systemd/`, fill in the required values,
then `systemctl --user daemon-reload && systemctl --user start nethub`.

For local development with live edits (no rebuild on every change):

```bash
./dev.sh
```

This builds `Containerfile.dev` (Flask's own dev server, debug + reload)
and runs it with the repo bind-mounted in, at `http://localhost:8080`.

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
- [HANDOFF.md](HANDOFF.md) — the open remediation work from a four-lane
  review of the Netmiko migration, with the reasoning behind each item.
- [CLAUDE.md](CLAUDE.md) — the settled decisions, and the list of things
  that deliberately must not be built.
- [LICENSE](LICENSE)
