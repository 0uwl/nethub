# NetHub

> NetHub is in early bootstrap — see [Project status](#project-status)
> below for what's actually implemented.

NetHub is a small, self-hosted dashboard that unifies both halves of a
network device's lifecycle in one Flask app, one database, one admin UI:

- **Provisioning (day-0)** — a new device phones home, is checked against
  a serial allowlist, and receives its initial config/image.
- **Software Lifecycle (day-2)** — admins onboard IOS-XE software images,
  publish them to a distribution host, and dispatch fleet upgrades via
  Ansible.

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
logs in is an admin — no roles yet) and a multi-registry publish flow.
NetHub doesn't own a single registry file — instead, an admin points it
at any number of existing `software_registry`-bearing files (typically
real Ansible `group_vars/*.yml` files, e.g. one per platform), tracks
each as a "registry," and can adopt entries a file already has or add
new ones (an uploaded image plus a typed-in checksum and version become
a new entry). See [alpha.md](alpha.md) for that slice's exact scope and
its deliberate deviations from the design below. The full target
architecture — data model, security model, Ansible integration,
failure/concurrency semantics — is written up in
[design-document.md](design-document.md). Treat that document as the
design target, not a description of current code.

## Running it

```bash
pip install -r requirements.txt   # Flask, Flask-SQLAlchemy, Flask-Login,
                                   # Flask-WTF, PyYAML, gunicorn, pytest

export SECRET_KEY=<any-string>    # required; nethub/config.py raises ValueError without it
flask --app nethub run            # runs the dev server (DEBUG defaults off; --debug to override)

flask --app nethub create-admin <username>   # bootstrap the first login user --
                                              # there is no self-registration route
```

By default NetHub looks for registry files under `instance/registries/`
(next to the repo root) — set `REGISTRIES_ROOT` to point it elsewhere.
See [Using it](#using-it) below for the actual admin flow once it's
running.

```bash
pytest                            # runs tests/ -- see tests/conftest.py for the
                                   # app/client fixtures (temp DB + registries root per test)
```

`.github/workflows/ci.yml` lints (`ruff`, `yamllint`, `ansible-lint`,
plus the `Containerfile` itself), runs the test suite, and publishes a
container image to `ghcr.io` on every push to `main`.

## Using it

Once you're logged in as an admin:

1. **Bind-mount or otherwise place** one or more registry files under
   `REGISTRIES_ROOT` — each is typically an existing Ansible
   `group_vars/*.yml` file for a platform (e.g. `os_iosxe.yml`), but a
   brand-new empty file works too.
2. Go to **Registries → New registry**, pick one of those files, and
   give it a name. If the file already has a top-level
   `software_registry` key, NetHub adopts it (any entries it already
   describes show up immediately); if not, you'll be asked for a
   `search_dir` (an absolute, writable path — where this registry's
   images live) and NetHub writes a fresh `software_registry` block into
   the file.
3. From a registry's **entries** page, **Add new** to publish an image:
   a name, a version (auto-suggested from the filename once you pick a
   file, but always editable), the image's SHA-512 checksum, and the
   file itself. NetHub verifies the checksum against the bytes it just
   received before accepting the entry.
4. **Check registry** re-hashes every entry's image against what's on
   disk and flags anything that's drifted (a hand-edited registry file
   is expected, not an error condition) — it doesn't run automatically
   on page load, since that means re-hashing every image.
5. Deleting an **entry** removes it and its image file. Deleting a
   **registry** only forgets NetHub's own tracking of it — the
   underlying file and any images are left untouched.

## Running it in a container

```bash
podman build -t localhost/nethub:latest .
podman run --rm -p 8080:8080 \
  -e SECRET_KEY=<any-string> \
  -e ADMIN_PASSWORD=<initial-admin-password> \
  localhost/nethub:latest
```

`quadlet/nethub.container` is a reference Podman Quadlet unit for
running it as a systemd user service, with every environment variable
documented inline (including loading `SECRET_KEY`/`ADMIN_PASSWORD` as
systemd credentials instead of plaintext, and how to bind-mount a real
`group_vars` file plus its images directory into `REGISTRIES_ROOT`).
Copy it to `~/.config/containers/systemd/`, fill in the required values,
then `systemctl --user daemon-reload && systemctl --user start nethub`.

For local development with live edits (no rebuild on every change):

```bash
./dev.sh
```

This builds `Containerfile.dev` (Flask's own dev server, debug + reload)
and runs it with the repo bind-mounted in, at `http://localhost:8080`.

The Ansible playbooks (`ansible/playbooks/stage_cisco_upgrade.yml`,
`ansible/playbooks/install_cisco_upgrade.yml`) target Cisco IOS-XE devices and are
currently invoked by hand, against a network device inventory not
present in this repo:

```bash
ansible-playbook ansible/playbooks/stage_cisco_upgrade.yml -e stage_serial=1
ansible-playbook ansible/playbooks/install_cisco_upgrade.yml -e install_serial=1
```

See [design-document.md](design-document.md) for what they expect and
how NetHub will eventually dispatch them.

## AI usage

Parts of this codebase and its documentation are developed with AI
assistance (Claude Code). Every change is reviewed by a human maintainer
before being merged.

## Learn more

- [design-document.md](design-document.md) — full architecture, security
  model, data model, and open questions.
- [ansible/inventory/](ansible/inventory/) — a design sketch of the
  ownership boundary between a user-uploaded upgrade request and the
  inventory NetHub renders around it.
- [LICENSE](LICENSE)
