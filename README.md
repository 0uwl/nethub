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
logs in is an admin — no roles yet) and a registry-publish flow where an
uploaded image plus a typed-in checksum become a new `software_registry`
entry — see [alpha.md](alpha.md) for that slice's exact scope and its
deliberate deviations from the design below. The full target
architecture — data model, security model, Ansible integration,
failure/concurrency semantics — is written up in
[design-document.md](design-document.md). Treat that document as the
design target, not a description of current code.

## Running it

```bash
pip install -r requirements.txt   # Flask, Flask-SQLAlchemy, Flask-Login,
                                   # Flask-WTF, PyYAML, gunicorn -- no test deps yet

export SECRET_KEY=<any-string>    # required; nethub/config.py raises ValueError without it
flask --app nethub run            # runs the dev server (DEBUG defaults off; --debug to override)

flask --app nethub create-admin <username>   # bootstrap the first login user
```

There are no tests, linter config, or CI yet.

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
systemd credentials instead of plaintext). Copy it to
`~/.config/containers/systemd/`, fill in the required values, then
`systemctl --user daemon-reload && systemctl --user start nethub`.

For local development with live edits (no rebuild on every change):

```bash
./dev.sh
```

This builds `Containerfile.dev` (Flask's own dev server, debug + reload)
and runs it with the repo bind-mounted in, at `http://localhost:8080`.

The Ansible playbook (`ansible/upgrade_iosxe.yml`) targets Cisco IOS-XE
devices and is currently invoked by hand, against a network device
inventory not present in this repo:

```bash
ansible-playbook ansible/upgrade_iosxe.yml -e upgrade_serial=1
```

See [design-document.md](design-document.md) for what it expects and how
NetHub will eventually dispatch it.

## AI usage

Parts of this codebase and its documentation are developed with AI
assistance (Claude Code). Every change is reviewed by a human maintainer
before being merged.

## Learn more

- [design-document.md](design-document.md) — full architecture, security
  model, data model, and open questions.
- [example_inventory/](example_inventory/) — a design sketch of the
  ownership boundary between a user-uploaded upgrade request and the
  inventory NetHub renders around it.
- [LICENSE](LICENSE)
