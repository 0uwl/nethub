# NetHub

> NetHub is in very early development and not guaranteed to work. 
> See [Project status](#project-status) below for what's actually implemented.

NetHub is a small, self-hosted dashboard that unifies both halves of a
network device's lifecycle in one Flask app, one database, one admin UI:

- **Provisioning (day-0)** - a new device phones home, is checked against
  a serial allowlist, and receives its initial config/image.
- **Software Lifecycle (day-2)** - admins onboard IOS-XE software images
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
Software Lifecycle exists: local username/password auth with two roles
(admin and operator) and optional two-person rules, an artifact store NetHub owns, a
fail-closed SSH host-key pin, and the day-2 upgrade path.

Device work is ordinary Python over [Netmiko](https://github.com/ktbyers/netmiko) in
`nethub/devices/`, dispatched out-of-band by a sibling process rather
than from a request handler. The device layer is validated against real
hardware (a Catalyst 9200CX on IOS-XE 17.12.06, including two live
upgrades); a full run driven end-to-end from the UI has not been done
yet.

How it works, and why, is in [docs/](docs/index.md): one file per area of
the code, describing what is built today. What is planned but not built is
in [docs/future.md](docs/future.md).

## Deploying NetHub

NetHub is deployed as two rootless Podman Quadlet units on one host:
`quadlet/nethub.container` serves the UI, and
`quadlet/nethub-sibling.container` does all of the device work. Both run
the same image, published to
[`ghcr.io/0uwl/nethub`](https://github.com/0uwl/nethub/pkgs/container/nethub)
for each release. Every environment variable is documented inline in the
units.

```bash
# 1. Pull the image and give it the name both units run.
podman pull ghcr.io/0uwl/nethub:latest

# 2. Make the volumes and the sibling's key pair. Device passwords are sealed
#    with the public key, and only the private key can open them, so the
#    private key is only mounted into the sibling unit.
mkdir -p ~/.local/share/nethub/{data,artifacts} ~/.config/nethub
podman run --rm --userns=keep-id:uid=1000,gid=1000 -v ~/.config/nethub:/keys:z \
  --entrypoint python ghcr.io/0uwl/nethub:latest \
  -m nethub.sealed_credentials keygen --out /keys/credential_private_key

# 3. Create the unit directory and install the units
mkdir -p ~/.config/containers/systemd/
cp quadlet/nethub.container quadlet/nethub-sibling.container \
  ~/.config/containers/systemd/
```

1. Edit both copies in `~/.config/containers/systemd/`:
   - **`nethub.container`:** set `SECRET_KEY` (`openssl rand -hex 32`),
     preferably as a systemd credential rather than plaintext. Also set
     `NETHUB_CREDENTIAL_PUBLIC_KEY`, `DEVICE_TARGET_CIDRS` (every run submission
     is refused while it's unset) and `ADMIN_USERNAME`.
   - **`nethub-sibling.container`:** use the same
     `NETHUB_CREDENTIAL_PUBLIC_KEY`, and uncomment the private key's
     `Volume=` and `NETHUB_CREDENTIAL_KEY_FILE=` lines.
2. Start both units:
   `systemctl --user daemon-reload && systemctl --user start nethub nethub-sibling`.

On first boot, NetHub creates the admin account named by `ADMIN_USERNAME`
and prints a random password once to `journalctl --user -u nethub`, unless
you supplied `ADMIN_PASSWORD`. If `ADMIN_USERNAME` is unset, NetHub starts
with no users. Create the admin with
`podman exec -it nethub flask --app nethub create-admin <name>`.

You need both units. Without the sibling, the UI accepts work and never
performs it, so scans and upgrades sit at "Still working".

The session cookie is `Secure` by default, so a deployment reached over plain
HTTP needs a TLS terminator in front of it. `http://localhost:8080` works
(browsers treat localhost as a trustworthy origin). Over a LAN address
without TLS, the browser silently drops the cookie, and login bounces back
to the form. Set `SESSION_COOKIE_INSECURE=1` to prevent that, but only for
local testing.**

### Building from source 
To run your own build instead of the published image, build it with a local
tag and change the units to use the newly built image, then restart them:
```bash
podman build -t localhost/nethub:latest -f Containerfile .
scripts/smoke_test.sh                  # optional: runs tests the same way the units do
systemctl --user restart nethub nethub-sibling
```

### Running it locally

```bash
uv sync                            # the runtime set plus pytest and CI's own
                                   # linters, at CI's versions, from the hashed
                                   # uv.lock into .venv. Activate it
                                   # (source .venv/bin/activate) or prefix the
                                   # commands below with `uv run`.

export SECRET_KEY=$(openssl rand -hex 32)   # required; nethub/config.py rejects an absent
                                   # key, a known placeholder, or anything under 32 chars
flask --app nethub run            # runs the dev server (DEBUG defaults off; --debug to override)

flask --app nethub create-admin <username>   # bootstrap the first login user --
                                              # there is no self-registration route
```

The database is created and kept up to date by NetHub itself: every start
applies any pending migrations before serving (see
[Upgrading NetHub](#upgrading-nethub)). `flask --app nethub db current` and
`flask --app nethub db history` show where a database stands.

Uploaded image bytes land in `instance/artifacts/` (next to the repo
root). Set `ARTIFACT_STORE` to point it elsewhere, and do set it in a
container, where the default falls on a read-only layer. Submitting an
upgrade also needs `DEVICE_TARGET_CIDRS`, which has no default and
refuses every submit while unset. See [Using it](#using-it) below for the
admin flow.

## Using it

There are two roles. An **operator** uploads and deletes artifacts, scans
and confirms host keys, and submits, approves, retries and cancels runs. An
**admin** does all of that and also manages users and **Settings**. The user
`create-admin` or first-boot bootstrap creates is an admin; a user an admin
creates on the **Users** page is an operator unless the admin picks admin.

Once you're logged in:

1. **Set your profile** - set your *device* username: the name NetHub logs into
   switches with on your behalf. It's separate from your NetHub login
   and an upgrade cannot be submitted without it.
2. **Upload an Artifact** - publish an image: a *bundle key* (the name
   which an upgrade request uses to select the image), a version, the 
   image's SHA-512 checksum, and the file. NetHub rejects the upload if
   the checksum doesn't match what you claimed.
3. **Scan host keys** - for each device address you want to use, 
   fetch its SSH host key and compare the fingerprint against the device itself,
   then **Confirm** it. An address with no confirmed key cannot
   be used in a run at all.
4. **Start a new upgrade run** - pick a published bundle, list
   `hostname, address` pairs (IP literals inside a configured CIDR), and
   enter your device password. Pre-check runs immediately: privilege 15,
   boot mode, free space. If a run says **No
   worker has picked this up**, or marks a phase **stalled**, the
   `nethub-sibling` unit is down or stuck.

### The upgrade run
- **Approval gates** - The run parks at an **approval gate** before each
   phase that touches a device: stage, activate, cleanup. Each gate prompts for 
   your device password again for that one phase. It's encrypted by a key that
   only the dispatch process holds, and only until that phase starts.  
- **The canary** - The activation phase reloads the devices and verifies runs after
   they come back. The **first host you listed is the canary**: It's reloaded 
   alone and checked for the new version before any other device starts, so you should
   choose a representative one, and put stacks and standalone switches in separate runs.
- **Upgrade order** - After the canary comes up and is verified, the approval
   form lets you reload several devices at a time (default one, at most
   `PHASE_CONCURRENCY`). NetHub doesn't know which devices back each other
   up, so don't reload both halves of a redundant pair at once.
- **Upgrade failures** - A host that fails a phase doesn't stop the others so
   the run carries on with the hosts that passed. The next gate offers to
   **retry** the failed ones (collecting your password again). What does stop
   a phase is a refused password (tried on one device first so a typo
   cannot lock your account out), an error in NetHub itself, a canary that
   fails, or an image missing from or altered in NetHub's store. The run then
   comes back to the same gate with nothing marked failed that wasn't the device's
   fault: approve it again, with the right password, to carry on.
- **Scheduling runs** - Each gate also takes an optional **start at** time, 
   in UTC, so a fleet can be staged during the day and reloaded in a maintenance
   window that night with nobody at the gate. The approval work as normal -
   it names you, and your password is sealed into that job - the dispatch
   process simply doesn't pick the job up until the set time. You can schedule a 
   run a maximum of 72 hours ahead.
- **Re-hashing the store** - `flask --app nethub check-store` re-hashes every
   published image against what is on disk and flags a missing file or a digest
   that no longer matches. it's a command rather than a GUI button since it hashes
   everything at once which could take multiple blocking minutes.

### Admin only actions
- **Users** (admins only) - create accounts, choose each one's role, reset
   someone's password, disable or re-enable an account, and unlock one that
   too many failed logins locked. The last active admin cannot be disabled
   or made an operator.
   Disabling someone or resetting their password ends their open sessions
   on their next click. Change your own password on your **Profile**; that
   ends your other sessions. Every one of these is recorded, and each
   user's **History** shows who did what.
- **Settings** (admins only) - three **two-person rules**, each off until
   an admin turns it on. They bind operators only; an admin may act alone,
   and the records say an admin did.
   - *Artifacts*: an upload waits, unusable, until someone other than the
     uploader publishes it, and deleting a published image is a request that
     someone else confirms. The uploader, or an admin, can withdraw a
     waiting upload.
   - *Host keys*: a scan is confirmed by someone other than whoever
     requested it, within 15 minutes of the scan (the page shows the
     deadline, and **Host keys** lists every scan still waiting), and
     removing a pin is a request someone else confirms.
     With this rule off, only whoever requested a scan can confirm it.
   - *Runs*: every gate approval and every retry, scheduled ones included,
     comes from someone other than the submitter. Cancelling and declining
     cleanup never need a second person.

   A rule is checked when the second action happens, so turning one off
   releases anything waiting on it. Every change is recorded on the page.
   Each phase on a run page shows the device username it ran under: the
   approver's, which with the run rule on is never the submitter's.

### From the terminal
The artifact and host-key pages can also be used from a shell where the web
process runs. For a Quadlet deployment, prefix each command with
`podman exec -it nethub`, or enter the container's shell with
`podman exec -it nethub bash` before continuing. These commands go through
the same checks as the web pages, two-person rules included, and a scan is
still queued for the sibling to run.

```bash
flask --app nethub artifacts list [--state staged]
flask --app nethub artifacts add image.bin --bundle iosxe-17-12-06 \
    --version 17.12.06 --sha512 <hex> --as jsmith
flask --app nethub artifacts publish <id> --as jsmith
flask --app nethub artifacts delete <id> --as jsmith      # withdraws a staged upload

flask --app nethub hostkeys scan 192.0.2.10 --as jsmith --wait 60
flask --app nethub hostkeys show-scan <scan-id>
flask --app nethub hostkeys confirm <scan-id> --fingerprint "ssh-rsa SHA256:..." --as jsmith
flask --app nethub hostkeys list
flask --app nethub hostkeys history 192.0.2.10
flask --app nethub hostkeys delete 192.0.2.10 --as jsmith
```

- Every command that changes something takes `--as <username>` and asks
  for that user's **NetHub** password, so the records name who acted.
  Reading commands need no login.
- To confirm a host key, type its fingerprint back in. It must match the
  scan, so compare it against the device first.
- Deletes ask before they act; `--yes` skips the question.
- Runs and users are managed only in the browser for now.

## Upgrading NetHub

> NOTE: Only upgrade while no upgrade phase is running.
>
> The online backup for local deployments requires the `sqlite3` command-line tool.

Whichever way you deployed NetHub, back up the database first. NetHub has no
downgrades, so restoring a backup is the only way back. SQLite's online backup
is safe while NetHub is running:
`sqlite3 <db> ".backup '/path/to/nethub-$(date +%F).db'"`, where `<db>` is
`~/.local/share/nethub/data/database.db` for Quadlet or `database.db` at the
repo root locally. Or stop NetHub and copy `database.db` together with any
`database.db-wal`/`database.db-shm` beside it.

### Quadlet deployment

1. Pull the new image with `podman pull ghcr.io/0uwl/nethub:latest`. If you
   pinned a version in `Image=`, change it in both units first and pull that tag instead.
2. Restart both units:
   `systemctl --user daemon-reload && systemctl --user restart nethub nethub-sibling`.

### Local deployments


1. `git pull` the new code.
2. Re-run `uv sync`, then restart
   `flask --app nethub run` with the same `SECRET_KEY`.

### Database migrations
The web unit migrates the database at startup and logs
`migrating the database from <old> to <new>`; the sibling logs that it's
waiting for this, then starts. A migration that fails changes nothing and the
web unit doesn't start: check `journalctl --user -u nethub`, fix or go back.

To go back to an older version, restore the backup and the older image
together. An older image refuses to start on a database a newer one has
migrated, rather than run against a schema it doesn't understand.

A database created before NetHub had migrations is adopted automatically on
the first start of a version that has them, including repairing the columns
earlier versions could not add or remove. If it differs in any other way,
NetHub refuses to start and lists the differences.

## Upgrading a fleet with nhtui

`nhtui` is a terminal UI that upgrades a set of switches with no NetHub
server at all: no containers, no web app, no database. It runs the same
phase engine as the dispatcher, with the same gates, canary and retries,
so it works over SSH on any host that can reach the devices. Install it
without the containers:

```bash
uv tool install 'nethub[tui] @ git+https://github.com/0uwl/nethub@<tag>'

nhtui --hosts hosts.txt --image /path/to/image.bin --sha512 <hex> \
    --version 17.12.08 --cidr 10.0.0.0/8 --user jsmith
```

- `hosts.txt` has one `hostname, address` per line, and the first host is
  the canary.
- Press `s` to scan each host's key and confirm it against the device, or
  bring the keys you've already checked with `--pins` (one
  `address key-type SHA256:...` per line).
- Press `a` for the next step: pre-check first, then each gate's approve,
  retry or decline. The device password is asked for once, at pre-check,
  and again only if a switch refuses it.
- Press `x` to cancel and `q` to quit. You can't quit while a phase is
  running.

The `nhtui` process does all the device work itself, so run it inside
`tmux` or `screen`: if your SSH session drops during a reload, the run
stops. Nothing is stored, and the run isn't in NetHub's audit trail.
`--report run.json` saves the results when you quit. See
[docs/tui.md](docs/tui.md).

## AI usage

Large parts of this codebase and its documentation are developed with AI
assistance (Claude Code). Every change is reviewed by a human maintainer
before being merged.

## Learn more

- [docs/index.md](docs/index.md): architecture, security model, data
  model, deployment, and where to look for each area of the code.
- [docs/future.md](docs/future.md): what is not built, and the open
  questions.
- [CLAUDE.md](CLAUDE.md): the short version, with the hard rules.
- [LICENSE](LICENSE)
- [CONTRIBUTING.md](CONTRIBUTING.md): How to contribute to NetHub's development.
