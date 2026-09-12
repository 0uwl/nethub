# NetHub Alpha — Implementation Plan

Scope: day-2 only, and only the registry-publish slice of it. No
provisioning, no upgrade execution, no Ansible invocation of any kind.
This is `design-document.md`'s target architecture cut down to the
smallest thing that lets an admin log in, upload an image, and have it
show up as a `software_registry` entry a human (or, later, a real
publish job) can read.

## In scope

- Local username/password auth. Everyone who can log in is an admin;
  there is no role column, no OIDC, no operator distinction yet.
- Admins can create new users (username + password only).
- One page: upload a form with an image file, a checksum (typed in,
  not computed against anything external), and a name for the new
  registry entry.
- Submitting the form appends an entry to `software_registry` and
  stores the uploaded file on disk.
- A page listing current registry entries (free to build alongside the
  form, needed to show the admin what already exists / catch duplicate
  names).

## Out of scope

Everything below is real design-doc scope that alpha does not touch.
Listed so nobody mistakes alpha's shortcuts for the target design:

- Provisioning / day-0 (§3.3, §4.1, §4.2) — entirely absent.
- Any Ansible dispatch, EE, sibling process, job queue, `registry_jobs`
  table, `flock`, git commit of the registry (§3.2, §7, §9). Alpha
  writes a YAML file directly from the request handler.
- SHA-512 computed at ingest and treated as ground truth (§3.4). Alpha
  takes the checksum as a plain text field — see "Deviations" below.
- OIDC, roles, sessions-as-a-row, CSRF-on-approvals, enrollment tokens
  (§4.3–§4.5).
- The `artifacts` table, `bundle_key` uniqueness enforced by the
  database, superseding/versioning of registry entries (§5).
- Rate limiting, audit logging beyond what Flask's own request log
  gives you.

## Dependencies to add

Everything else is either stdlib or already pulled in by Flask.

- `Flask-SQLAlchemy` — `config.py` already declares
  `SQLALCHEMY_DATABASE_URI` but nothing uses it yet; alpha is what
  wires it up, for the `User` table.
- `Flask-Login` — session cookie + `login_required` + `current_user`.
  Not worth hand-rolling.
- `Flask-WTF` — `CSRFProtect` for the two POST forms (upload, create
  user). Also gives you form file-upload handling for free.
- `PyYAML` — read/modify/write `software_registry.yml`.

`werkzeug.security.generate_password_hash` / `check_password_hash`
(ships with Flask) covers password hashing — no bcrypt/argon2 package
needed at this scale.

## Data model

Two tables, via Flask-SQLAlchemy:

```
User
  id            int, pk
  username      str, unique
  password_hash str
  created_at    datetime

Registry
  id            int, pk
  name          str, unique
  file_path     str, unique -- relative to REGISTRIES_ROOT
  search_dir    str         -- absolute path; DB-authoritative (see below)
  created_at    datetime
```

No `role` column — everyone who authenticates is an admin. Add it back
when a second role exists to distinguish (design doc §4.4); a column
with one always-true value is a column that does nothing yet.

No `artifacts` table in alpha. Each registry's YAML file *is* the store
for its entries — see below. `Registry` is a pointer table (which files
NetHub tracks, and where their images live), not a copy of artifact
data, and has no design-doc counterpart of its own — the absence of
`artifacts` is still the biggest deviation from §5 and is called out
explicitly in "Deviations."

## Registry storage

Originally one hardcoded `software_registry.yml`; superseded by
multi-registry support (an admin can now track any number of
`software_registry`-bearing files, e.g. several existing Ansible
`group_vars/*.yml` files at once). An admin bind-mounts each file
somewhere under `REGISTRIES_ROOT` (PyYAML, same shape as
`ansible/inventory/group_vars/os_iosxe.yml` throughout — one
top-level `software_registry` key, sibling keys like `image_transport`
left untouched), then registers it from the Registries settings page:
NetHub reads the file for an existing `software_registry` key (adopting
its entries if present) or collects a `search_dir` from the admin and
writes a fresh one in. Each registered file becomes a `Registry` row.
Uploaded images for a given registry go in *that registry's own*
`search_dir` — no shared images directory, since each registry's
`search_dir` is independently admin-supplied and typically a much
larger, separately-mounted volume than `REGISTRIES_ROOT` itself.

Entry shape written per submission:

```yaml
software_registry:
  search_dir: "<config-driven path>"
  '<name>':
    file_name: <secure_filename(upload.filename)>
    version: <the text-field value>
    sha512: <the text-field value, lowercased>
    file_size: <os.path.getsize of the saved file>
```

`version` is a required field, not an omission — this was wrong in an
earlier draft of this document, which claimed nothing committed needed
it. `ansible/playbooks/tasks/resolve_target_bundle.yml` sets
`target_version: "{{ target_bundle.version }}"`, and
`install_cisco_upgrade.yml` depends on `target_version` throughout its
pre-check (`current_version != target_version`), its install step, and
its post-install verification (`ansible_net_version == target_version`)
— an entry without one breaks the install playbook the moment a host is
pointed at it. `check_registry()` flags any existing entry missing it,
same as a missing `sha512`/`file_size`.

Write path, in the upload route:

1. Validate: name is non-empty and not already a key in the registry
   (reject outright — no supersede/overwrite flow in alpha, that's
   §6/§5's `state`/`superseded_by_id` machinery and it isn't built
   yet); version is non-empty; checksum matches `^[0-9a-fA-F]{128}$`
   (SHA-512 is 128 hex chars); a file was actually uploaded.
2. Save the upload to the images directory under
   `secure_filename(file.filename)` — never trust the client-supplied
   name as a path. Reject if a file of that name already exists on
   disk, same reasoning as the registry-key check.
3. Compute the SHA-512 of the bytes just written and compare against
   the submitted checksum. Mismatch → delete the file, reject the
   request with the computed value shown back. This is cheap
   (`hashlib.sha512`, one pass, already have the bytes) and it's the
   one piece of §3.4's "hash gates trust" idea that costs nothing to
   keep even though alpha doesn't compute the hash *authoritatively* —
   it just stops a fat-fingered checksum or a corrupted upload from
   silently entering the registry.
4. Read the current YAML, add the entry, write it back (path-escape
   checked against `REGISTRIES_ROOT`, read-modify-write so sibling keys
   survive, written atomically via a temp file + `os.replace`), all
   under a single process-wide `threading.Lock()` (`registry.lock` —
   public, not `_lock`, since the registries settings routes' row-
   creation flow shares it too). That's the alpha-sized substitute for
   §7.1's `flock` — sufficient because Flask alpha runs
   single-process/single-worker anyway (same constraint the full design
   already imposes for unrelated reasons, §3.2), and still global rather
   than per-registry even with multiple registries now in play; two
   admins submitting at once are the only concurrent writers that exist.
   A `file_name` read back out of the file is revalidated through
   `secure_filename` before delete/check will touch a path built from
   it — the file is hand-editable (and, now, may be an admin's
   pre-existing file NetHub never wrote), so it's untrusted on the way
   in exactly like a submitted upload filename is.

## Auth flow

- `/login` (GET/POST) — username + password against `User`, Flask-Login
  session on success. Unauthenticated `GET` anywhere else redirects
  here.
- `/logout` (POST) — Flask-Login `logout_user()`.
- `/users` (GET) — list existing users.
- `/users/new` (GET/POST) — create a user (username + password, hashed
  on write). No self-registration route exists; every account is
  created by an already-logged-in admin.
- Bootstrap problem: a fresh database has no users, so nobody can log
  in to create the first one. Simplest fix at this scale: a `flask`
  CLI command (`flask create-admin <username>`, prompts for password)
  run once by whoever deploys it. No enrollment-token flow — that's
  §4.4's answer to a problem (OIDC/local coexistence, forced reset)
  alpha doesn't have yet.
- Every route except `/login` and the static home page gets
  `@login_required`.

## Routes summary

| Route | Method | Auth | Purpose |
|---|---|---|---|
| `/login` | GET/POST | none | authenticate |
| `/logout` | POST | required | end session |
| `/registries` | GET | required | list tracked registries |
| `/registries/new` | GET/POST | required | adopt/create a registry from a file under `REGISTRIES_ROOT` |
| `/registries/<id>/delete` | POST | required | forget a registry (row only — file/images untouched) |
| `/registries/<id>/entries` | GET | required | list one registry's current entries |
| `/registries/<id>/entries/new` | GET/POST | required | the upload form |
| `/registries/<id>/entries/<name>/delete` | POST | required | delete one entry (and its image) |
| `/registries/<id>/entries/check` | POST | required | hash-check one registry's entries against disk |
| `/users` | GET | required | list users |
| `/users/new` | GET/POST | required | create a user |

## Config changes

- `config.py`: `DEBUG` flips to `False` by default (env-overridable for
  local dev only). CLAUDE.md is explicit that `DEBUG` must be off
  "before the credential path exists" — alpha is exactly the change
  that introduces one (a login form posting a password to a Werkzeug
  dev server whose debugger, if it ever fires, renders frame locals
  including that password into the response).
- Add `MAX_CONTENT_LENGTH` so an unbounded upload isn't a free DoS
  against disk/memory — this is a trust-boundary input, not a
  hypothetical.
- Add `REGISTRIES_ROOT`, read from env with a sane local default —
  the one directory an admin bind-mounts registry files into. (Originally
  a separate `IMAGE_DIR`/`REGISTRY_FILE` pair for the single-registry
  design; superseded once multiple registries needed a shared root to
  discover files under, each with its own admin-supplied `search_dir`
  instead of one shared images directory.)

## File/module layout

Built as an actual package rather than a flat `app.py`, since a later
pass restructured it that way, and then a further pass converted it to
an application factory (no `wsgi.py` -- Flask's CLI autodetects
`create_app()` directly: `flask --app nethub run`. Gunicorn, used only
in the production container, needs it written as a call expression --
`nethub:create_app()`, not `nethub:create_app` -- since the pinned
gunicorn version parses its app argument as Python and only invokes it
if it's a call; there is no `--factory` flag in that version):

```
nethub/
  __init__.py               -- create_app(): builds the Flask app, extension init
                                 (SQLAlchemy, LoginManager, CSRFProtect), blueprint
                                 registration, admin bootstrap, error handlers, the `/` route
  config.py                 -- moved as-is; basedir resolves to the repo root
                                 (one level up from the package) so database.db/instance/
                                 land beside the package, not inside it
  credentials.py             -- read_credential(): $CREDENTIALS_DIRECTORY lookup for
                                 systemd LoadCredential=/SetCredential= (SECRET_KEY, ADMIN_PASSWORD)
  bootstrap.py               -- bootstrap_admin(): creates the first user on an empty
                                 database from ADMIN_USERNAME/ADMIN_PASSWORD/credential/random
  extensions.py             -- shared db/login_manager/csrf instances
  models.py                 -- User, Registry
  registry.py               -- per-registry load/save, the write-lock (public: `lock`),
                                 discover_files/inspect_file/sync_registry, hash check
  auth.py                   -- auth_bp: login/logout/user-management routes, register_cli(app)
  registry_routes.py        -- registries_bp (/registries, /registries/new,
                                 /registries/<id>/delete) and registry_bp
                                 (/registries/<id>/entries/*), one file, two scopes
  gunicorn.conf.py           -- production-only: workers=1, bind from NETHUB_PORT,
                                 control_socket_disable=True
  templates/pages/login.html
  templates/pages/registries_list.html
  templates/pages/registries_new.html
  templates/pages/registry_list.html
  templates/pages/registry_new.html
  templates/pages/users_list.html
  templates/pages/users_new.html
  static/                   -- moved from the repo root unchanged
```

Containerization (`Containerfile`, `Containerfile.dev`, `dev.sh`,
`quadlet/nethub.container`) is documented in CLAUDE.md's "Container"
section rather than here -- it's packaging around this slice, not a
change to what the slice does.

`auth.py` and `registry_routes.py` are Flask blueprints (`auth_bp`, plus
`registry_routes.py`'s own `registry_bp`/`registries_bp` pair) rather
than routes hung directly off `app` — needed once
two people (or two agents) were touching the route layer in parallel,
and kept afterward since it's what let the whole thing move into a
package without `nethub/__init__.py` becoming a dumping ground.

## Deviations from the design document (read before extending)

These are alpha shortcuts, not corrections to the design doc. Anyone
building the next increment toward the full design should know exactly
what alpha skipped and why it's safe to skip *for now*:

- **Checksum is user-supplied, not computed at ingest.** §3.4's whole
  hash story assumes NetHub is the one that computes the SHA-512.
  Alpha lets the admin type one in and only uses it as a corruption
  check against the bytes just uploaded — it is not treated as an
  authoritative, tamper-evident value the way the design doc's
  `artifacts.sha512` is. Don't reuse alpha's registry rows as if they
  carry that guarantee once the real `artifacts` table exists.
- **No `artifacts` table, no `state`/`superseded_by_id`.** The YAML
  file is the only store. A second upload under an existing name is
  simply rejected — there is no overwrite-with-confirmation flow, no
  history of superseded entries. Anything that needs "what did we
  publish and when" needs the real table.
- **No `registry_jobs`, no git commit of the registry file, no
  `render_state` reconciliation (§7.2).** If someone hand-edits alpha's
  YAML file, nothing detects it. Fine at this scale, wrong once a real
  publish pipeline exists alongside it.
- **Sessions are Flask-Login's signed cookie, not a `sessions` row
  (§4.5).** Deactivating a user in alpha doesn't invalidate a session
  already issued to them until it naturally expires/is cleared. There
  is only one role so there's nothing to re-derive per request either.
- **No rate limiting, no audit trail beyond app logs.** §4.2's
  per-serial/per-IP counters don't apply (no provisioning route exists
  yet), but the general principle — don't add durable-write-per-request
  on a path an attacker controls — doesn't bite here either, since
  every route requires a session.

## Rough build order

1. `Flask-SQLAlchemy` + `User` model + `flask create-admin` CLI command.
2. `Flask-Login` wiring + `/login` + `/logout` + `@login_required`.
3. `/users` + `/users/new`.
4. `registry.py`: load/save YAML, the lock, the hash-check helper.
5. `/registry` (list) + `/registry/new` (form, wired to `registry.py`).
6. Flip `DEBUG` off, add `MAX_CONTENT_LENGTH`, wire `CSRFProtect`.
