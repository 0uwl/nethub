# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project status

NetHub is in early bootstrap. `app.py` currently only serves a static
homepage and error pages — none of the Provisioning or Software Lifecycle
functionality described in `design-document.md` exists yet. The `ansible/` playbook
references roles (`iosxe_facts`, `batch_summary`) and a helper
(`files/remote_image_size.py`) that are not present in this repo. Treat
`design-document.md` as the design document / target architecture, not a
description of current code — always verify a described component
actually exists before assuming it's implemented.

`example_inventory/` is likewise a design sketch, not working config. It
shows the ownership boundary between the user-uploaded upgrade request
and the inventory NetHub renders around it (design doc §3.5/§8.1); its
`rendered/` tree illustrates output NetHub does not yet produce.

## Commands

```bash
pip install -r requirements.txt   # Flask, gunicorn — no dev/test deps yet

export SECRET_KEY=<any-string>    # required; config.py raises ValueError without it
python app.py                     # runs the dev server (config.py sets DEBUG = True)
```

There are no tests, linter config, or CI in this repo yet.

The Ansible playbook (`ansible/upgrade_iosxe.yml`) is currently invoked
by hand, against a network device inventory not present in this repo:
```bash
ansible-playbook ansible/upgrade_iosxe.yml -e upgrade_serial=1
```
It targets Cisco IOS-XE devices (`cisco.ios` collection, `network_cli`
connection) and expects each host to define an `image_bundle` var
(filename, sha512, version, remote_dir, file_size) — see §5/§8 of
`design-document.md`. `upgrade_serial` controls how many hosts upgrade per wave
(defaults to 1); with `serial > 1`, hosts in the same wave share this
terminal's stdin for the interactive `pause` prompts, so they can
interleave.

That manual, interactive form is what is committed today. design doc §8.1
supersedes it in the target design: NetHub dispatches the playbook in
phases with no TTY, and the `pause` prompts become UI approval gates.
Both descriptions are correct about different things — don't "fix" the
playbook's prompts without implementing the phase model that replaces
them.

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

**Publishing and installing are two separate operations, and only one is
an EE dispatch** (design doc §6, §8/§8.1). Publishing is local to NetHub
now that NetHub owns the store (§3.3): upload → SHA-512 verify → promote
bytes and row → registry re-render → git commit → audit trail. It never
touches a network device and no longer runs a playbook. Installing runs
`upgrade_iosxe.yml` against devices, split into phases, and is the only
thing in the system that dispatches an EE. Two things that did *not*
change when publish stopped being a dispatch: it stays in the **sibling**
(§3.2's argument is unchanged — it must not run in a Flask handler), and
it keeps its `registry_jobs` row, the serial queue, `render_state`, the
`flock`, the startup sweep, and every state in §7.3. Conflating publish
with install is still the easiest mistake to make here; they're now
separate in kind rather than two instances of one mechanism.

**NetHub requires exactly two things of a device, and the list should
stay short** (design doc §4.3): **privilege level 15 at login**, and
`ip ssh source-interface` configured for the SFTP client. Nothing else —
NetHub enables no services and writes no configuration outside the
upgrade itself. Both are asserted at pre-check. That short list is also
an argument in itself: push was rejected partly because it would have
added a third item NetHub had to set and then un-set.

**On privilege 15 specifically — there is no `enable` escalation
anywhere** (design doc §4.3). Every command an upgrade runs —
`write memory`, `copy` to flash, `install add … activate commit` — needs
level 15, so an account that can't reach it can't upgrade anything.
Asking for it at login rather than via `enable` removes a second secret
(no `become_password` is ever collected) and removes the shared-enable-
secret problem, which is the same shared-credential objection §4.3 raises
about service accounts. Don't add `ansible_become`/`ansible_become_method`
back to the rendered inventory, and don't add a `become_password` to the
credential path. It's also a *property* rather than a tax: §4.3 says
NetHub's `role` gates NetHub's screens and never substitutes for what a
credential authorizes on the device — the privilege requirement makes
that concrete, since the refusal comes from the device. The playbook
asserts it at pre-check so an under-privileged account fails once and
clearly (`failure_stage: privilege`) rather than part-way through a wave.

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
day-0 devices pull over HTTP, day-2 devices pull over SCP. One `artifacts`
table backs both; a `kind` discriminator (script/config/image)
distinguishes rows rather than splitting into separate tables. The SHA-512
is computed once at ingest and consumed three times: day-0 verification,
the rendered registry entry, and the device's own `verify /sha512` step
during a day-2 upgrade.

**SHA-512 is the only hash algorithm in the system, day-0 included, and
there is deliberately no `hash_algo` column** (design doc §3.4) — IOS-XE's
`verify /sha512` fixes the algorithm at the device end, so a second one
would mean storing two digests or recomputing at egress. Don't add
per-artifact algorithm support speculatively; it arrives with a platform
that actually requires it.

**Everything the EE reads is rendered, not supplied** (design doc §3.5).
`image_registry.yml`, the per-job inventory, and the connection vars are
all projections of the `artifacts` and `users` tables, written at dispatch
into a `private_data_dir` that is discarded with the job. On any
inconsistency between table and file, the table wins (§7.2). The whole
registry is re-rendered on every publish rather than patched in place,
which is what makes that reconcile possible. **Kea's reservations are the
fourth store and follow the same rule** (§7.4): rendered whole from
`allowlist_entries` and reconciled, because a reservation outliving a
consumed entry silently re-opens §4.1's first gate.

**"Table wins" and "stop, don't destroy the evidence" fire on the same
signal, and `render_state` is what separates them** (§7.1/§7.2). A
registry file that no longer matches the table means either an
interrupted publish (a `registry_jobs` row at `written`, not `committed`
— re-render and commit the correction) or an outside edit (no such row —
preserve it, refuse to publish, surface it, and let an admin explicitly
adopt or discard). Don't implement one of those behaviours without the
`render_state` test in front of it; each is the wrong response to the
other case.

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
the IdP's decision is inert. But `network_cli` needs a
password neither auth
backend will yield, so an upgrade run collects the submitter's device
credential at each approval gate, holds it in memory only for the life
of the *phase execution* that approval releases (design doc §4.3/§9.1 —
not for the life of the run, since a run can park at a gate for days),
and never writes it to the `private_data_dir` or the session. The device
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
doc §2, §4.3, §5). `network_cli` sends the password after key exchange
and `ansible_host` is submitter-supplied, so without verification an
operator can name a machine they control and be handed a colleague's
AAA credential. A `device_host_keys` table (keyed on address, not on a
device identity) backs a rendered `known_hosts` and a fail-closed
check; first contact is TOFU with the fingerprint shown at the submit
gate and recorded against the approver. These rows are deliberately
exempt from §7.4's retention purge — expiring one silently downgrades a
fail-closed mismatch back to a first-contact prompt. §2's non-goal
names this exception explicitly so nobody "fixes" it later.

**Failure, concurrency, and staleness semantics live in design doc §7** and
are load-bearing rather than aspirational — EE runs are dispatched
out-of-band by a sibling process (never synchronously in a Flask request
handler, which would stall the phone-home route), registry writes are
serialized by an advisory `flock`, and job status is a state machine with
explicit terminal states
(`succeeded`/`failed`/`timed_out`/`abandoned`/`cancelled`/`expired`), not
a success flag. Serialization applies to a *phase execution*, not to a
whole run: a run parked at an approval gate holds no EE process. Consult
§7 before implementing anything that writes to the database, the registry
file, or git.

Five things in §7.3 are easy to get wrong and are now specified:

- **`status` is shared across job kinds; `failure_stage` is not.** A
  publish stops at `credential`/`stage`/`verify`/`render`/`commit`, and
  §8.1's phases are *also* named `stage` and `verify` — so `phase=
  'activate', failure_stage='stage'` would be ambiguous on its face.
  Phase jobs get their own vocabulary.
- **Cancel is a column the sibling polls** (`cancel_requested_at`), not a
  signal or a kill — §9's rule that the job row is the only control
  channel is what forces that. Checked between hosts and at phase
  boundaries only. `abandoned` is for crashes; `cancelled` and `expired`
  are for humans and TTLs, and merging them costs the word its
  diagnostic value.
- **The sweep keys on `runner_instance_id`** (a UUID minted per sibling
  start), never a PID — a PID is reused across container restarts and
  meaningless across PID namespaces.
- **Flask *reads* `heartbeat_at` and renders "stalled".** The sweep lives
  in the sibling so it can't fire against a healthy run, which means a
  sibling that dies and stays dead is swept by nobody. Noticing doesn't
  have to be the sibling's job; changing the row still is.
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
file. The purge runs in the *sibling* (it takes the registry lock and
deletes on the distribution host; neither is work for the process holding
the unauthenticated route).

**Provisioning log, `registry_jobs`, and upgrade run/phase rows share
plumbing (row shape, retention-purge helper, log viewer) but stay
semantically separate** — distinctly labeled, not merged into one
timeline, and never rolled up into a persistent per-device current-state
view. That roll-up is the line between a job record and an inventory, and
NetHub is explicitly not an inventory system (design doc §2 Non-goals, §7.4).

**§5 now specifies the day-0 and settings tables it used to discharge in
a sentence.** New since the review: `allowlist_entries` (with the
per-role artifact FKs that make §3.4's serial→artifact mapping
representable at all, plus `mac` for the Kea gate), `provisioning_log`
with an `outcome` enum and a `provisioning_log_artifacts` junction,
`settings` + append-only `settings_audit`, `user_admin_audit`, `sessions`,
and `upgrade_host_phase_results`. Four columns recur on both job tables
because §7/§9 assume them and none existed: `created_at` (the queue has
nothing else to order by — `started_at` is null until dispatch),
`deadline_at`, `runner_instance_id`, `private_data_dir`. Two constraints
are load-bearing rather than tidy: `UNIQUE(run_id, phase, attempt)` is
what makes two admins clicking "approve: reload" a `409` instead of two
reloads (§8.1's serialization is scoped to *execution*, so it stops
overlapping processes, not duplicate rows), and `PRIMARY KEY (run_id,
hostname)` stops a request naming a host twice from reloading it twice.

**Snapshots must carry every field the thing they snapshot carries.**
`registry_jobs` and `upgrade_run_hosts` needed `remote_dir` and
`file_size` added: without them the row that answers "what did we publish
that day" can't reproduce the registry entry, and — worse, because it's
operational — a phase either can't render its inventory from the run's
own rows or has to re-read `artifacts`, which lets a mid-run supersede
silently re-target the run. With them complete, a run is self-contained
and never reads `artifacts` at dispatch. `upgrade_runs` also snapshots
`shared_account_mode` beside `device_username_used`, or an auditor can't
tell "jsmith ran this" from "everyone runs as jsmith".

**§6.1 specifies the API surface** — routes, methods, the `202` + job-id
polling contract, and an error envelope that returns §7.3's
`failure_stage`/`error_summary` enums so the dashboard and the log agree
on words. `POST /provision` is the only unauthenticated route and never
returns `403` or a distinguishable body for a known serial (§4.1's
enumeration-oracle rule).

Vendor scope is IOS-XE only for now, but the Software Lifecycle schema and
job model carry a `platform` field throughout so a second platform is an
addition, not a rework.

## Hard rules — do not implement these

These are settled decisions with reasoning in the design doc. If a change
seems to require one, the design is what needs revisiting, not the rule.

- **No user-supplied playbooks.** The playbook set is closed at build
  time and selected by `platform` (design doc §2 Non-goals, §8.1). Accepting
  one is arbitrary code execution inside the EE with the live
  distribution credential in reach — an authenticated RCE primitive
  sitting beside the unauthenticated route §4 spends its length on.
  Per-run minting (design doc §4.3.1) bounds what that credential is
  worth after the phase ends; it does nothing about a playbook running
  while it is still valid, so the rule is unaffected.
- **No user-supplied inventories, and no user-supplied Jinja.** A user
  submits a *request document* — hosts, one bundle key each, a small
  closed set of typed knobs — which NetHub validates and compiles. An
  uploaded inventory carries `image_registry`, which would let a request
  name any filename against any SHA-512 and bypass the `artifacts` table.
  A `{{ ... }}` expression in a submitted field is the playbook hole in a
  different costume.
- **No user-settable connection vars — except `ansible_host`, which is
  validated rather than trusted.** `ansible_user` is the submitter's own
  device identity (or, only under shared account mode, one
  admin-configured value — never something a submitter's request
  supplies either way). An identity the submitter can type is not
  evidence of anything, and the audit property in §4.3 depends on it.
  The same goes for credentials and `become` settings. `ansible_host` is
  different and the rule used to be wrong about it: a request has to
  name its targets, and `example_inventory/README.md` has always listed
  it as an allowed field. It is accepted under two constraints — a
  deployment-level target CIDR (a `settings` row, §5), and a fail-closed
  host-key check against `device_host_keys` (design doc §4.3/§8.1). The line is between vars
  asserting *who someone is* (never submitted) and the address of the
  thing being acted on (necessarily submitted).
- **No EE invocation from the Flask process.** Flask holds the only
  unauthenticated route; giving it Podman access turns any Flask RCE into
  host-level container control (design doc §9). The sibling owns dispatch
  and the job row is the only *control* channel between them — which is
  also why there is no PTY streamed to the browser. There is exactly one
  other channel and it is not a general one: a sibling-initiated Unix
  socket carrying per-execution credentials and nothing else (design doc
  §9.1). Don't widen it into RPC, don't let control information or
  status onto it, and don't let Flask be the side that connects — the
  reason is window minimisation (a deposit endpoint means the sibling
  holds secrets for executions it hasn't started, and can be flooded),
  not that a deposit socket would be "the Podman socket in miniature".
  The construction is §9.2, and three things about it are easy to get
  wrong: the **mount** is the
  authenticator, not `SO_PEERCRED` — under one shared rootless user a
  uid check discriminates nothing, so don't write one and believe it
  means something; the socket is created by a systemd `.socket` unit,
  never by `unlink()`+`bind()` in either container; and the sibling
  parses bytes Flask chose, so the response surface needs framing,
  caps, deadlines, and a character allowlist on the credential before
  it reaches any variable or command string.
- **Flask runs exactly one worker — and more than one thread.**
  `gunicorn` with `workers = 2` silently breaks the credential path: a
  credential submitted to worker A is invisible to worker B (design doc
  §9.2). It fails closed but intermittently, and the error says nothing
  about workers. Threads are the other half of the same rule and are
  *required*, not optional: §3.2 now notes that a 1.2 GB image upload
  plus its SHA-512 pass is the longest operation in the system after the
  EE, and single-threaded it would hold the phone-home route for the
  whole window — the exact failure §3.2 legislated against, arriving
  through ingest instead of through `ansible_runner.run()`. Hash
  incrementally over the chunks as they're written so "hashed once at
  ingest" survives. The socket is served on its own thread with
  deadlines, for the same reason. §3.2 also now names the budget the
  whole design is calibrated against: **2 s p99 on phone-home, 15 min of
  device backoff.** Check changes to the request path against those
  numbers.
- **No secrets in the `private_data_dir`, and don't trust
  `ansible-runner` to keep them out.** `extravars` lands in
  `env/extravars`, `passwords` in `env/passwords`, `envvars` becomes
  `podman run -e` and shows up in `/proc/<pid>/cmdline`. The design says
  credentials live only in memory (design doc §4.3/§4.3.1/§9.2), and
  that is something you have to engineer: tmpfs-backed
  `private_data_dir`, `env/` and `inventory/` destroyed with the
  execution, and only scrubbed `stdout`/`job_events` retained as
  `playbook_log_path`. Retaining the directory wholesale would park
  credentials on disk for §7.4's full 365 days.
- **`DEBUG` must be off before the credential path exists** (and before
  §4.5's session model — the same debugger renders a session cookie
  alongside a device password).
  `config.py` currently sets `DEBUG = True`, which is fine for the
  bootstrap the repo is in today and is a release blocker for the
  approval flow: Werkzeug's interactive debugger renders frame locals —
  including a device password — into an HTTP response, and the reloader
  runs two processes. Same class: `LimitCORE=0` and non-dumpable
  process, or a crash writes the heap to `/var/lib/systemd/coredump`.
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
  account mode** (design doc §4.4), which fixes `ansible_user` to one
  admin-configured value for every run instead of reading
  `users.device_username`. It is a knowingly-made deployment setting, not
  a per-user choice and not a fallback that engages itself when an IdP
  is missing — don't wire it up as a default or infer it from the
  absence of OIDC. The SCP *account* on the distribution host is a
  separate case and stays shared unconditionally: no human is in that
  session (design doc §4.3). Its *password* is not shared over time — see
  the rule above.
- **No standing password for the distribution host, and the password
  never goes in the command string.** NetHub mints a fresh password per
  phase execution and expires it at a terminal state (design doc §4.3.1).
  Two disclosures drove that and they are not alike. The command-string
  one is *fixable* and is fixed: the playbook supplies the password as an
  answer to the device's `Password:` prompt, because accounting records
  commands and not prompt responses — putting it back in a
  `copy sftp://user:pass@host/…` string would write a live credential to
  the AAA server via the very setting §4.3's attribution property depends
  on. The submitter-chosen-`ansible_host` one is *not* fixable, so it is
  priced instead: the credential is short-lived and opens a purpose-built
  account, which makes what leaks nearly worthless. Don't reintroduce a
  long-lived `dist_pass`, don't put it in a vault, and don't write the
  minted one into the `private_data_dir` — it is injected in memory like
  the device credential. **The account hardening is load-bearing, not
  hygiene**: a shell on that account turns a near-worthless credential
  into SSH access to the host holding the only unauthenticated route.
- **Day-2 transfer is SFTP, and that choice is about the server side.**
  `copy sftp://` and `copy scp://` are interchangeable for what NetHub
  does — it renders an exact path and never lists a directory, and
  neither offers IOS-XE's `copy` a resume. They are not interchangeable
  where it matters: `ForceCommand internal-sftp` + `ChrootDirectory` is a
  complete answer to the hardening above (no shell, no binaries in the
  chroot), while serving legacy SCP needs both inside the chroot and is
  broken outright by `ForceCommand`. OpenSSH has deprecated legacy SCP
  too. The cost is one device prerequisite — `ip ssh source-interface`,
  documented as required for the SFTP client — which the playbook asserts
  at pre-check. Don't switch back to SCP for symmetry with anything.
- **NetHub is the distribution host, and that is no longer modular**
  (design doc §2 Non-goals, §3.3). This is what makes the rule above
  work: a host NetHub doesn't administer is one whose account model it
  can't constrain, so it could neither issue the credential nor bound
  what it opens. There is no remote distribution target and no
  `distribution_mode`. The cost is written down — every device fetches
  across whatever link separates it from NetHub, which bites first on a
  branch site behind a narrow link. Reopening it (§10) means reopening
  §4.3.1, not just adding a config option.
- **Push was evaluated and rejected on cost, not principle** (design doc
  §4.3.1, "Why not push instead"). Inverting the transfer would delete
  the distribution credential outright, and it requires NetHub to mutate
  production running-config and put it back — a restore that is skipped
  on any kill, abandon, or mid-host cancel, leaving devices with a
  transfer service enabled that NetHub cannot enumerate without building
  the per-device view §2/§7.4 refuse. It also moves every image's crypto
  into NetHub's own process (§3.2) and depends on `net_put`, whose SCP
  path is reported broken against IOS-XE under libssh with the paramiko
  fallback deprecated. **SFTP does not rescue it: IOS-XE has no SFTP
  server** (Cisco documents the client as always enabled and the server
  as unsupported), so under push SCP is the only wire protocol available
  and its Ansible path is the broken one. Don't re-propose push without
  new facts on those points.

## Ansible playbook notes (`ansible/upgrade_iosxe.yml`)

Describes the playbook as committed. design doc §8.1 retires several of these.

- **Fixed: the staging rescue now ends the host.** It previously printed
  a message and set `upgrade_stage_failed`, a fact read only by the
  summary play at the very end — nothing between it and `install add`
  consulted it, and the install block carries **no `when:` of its own**
  (the version comparison sits on the interactive `pause` before it). A
  failed `verify /sha512` therefore fell through and installed an image
  whose digest did not match, making §3.4's third consumption of the
  ingest hash advisory. The rescue now ends with `meta: end_host`; the
  flag is kept for the summary. Note this rescue also catches the benign
  "image already in flash" assertion, which under §8.1's phase split
  becomes the *normal* state at activation time — that's the phase model's
  problem to solve, not this playbook's.
- Two plays run before the main upgrade play: an ungathered preview play
  (prints the full upgrade plan for every targeted host before any
  connections are made) and the main `serial`-gated upgrade play; a final
  summary play runs after all serial waves complete, using the
  `batch_summary` role. Under the phase model both the preview and summary
  plays dissolve into the UI — NetHub already holds the plan it would
  print and the per-host results it would tally.
- `upgrade_result` is initialized early (`status: not_started`) so the
  summary play's lookup never hits an undefined key, even for a host that
  fails a pre-check and never reaches the "record result" task. Per-host
  state moves to `upgrade_run_hosts` under the phase model, which is what
  retires this and the `rescue`/`upgrade_stage_failed` bookkeeping.
- **Removed: the three-tier image-size cascade.** It used
  `image_bundle.file_size` → a per-run `localhost` cache via
  `delegate_facts` → a remote stat shelling out to
  `files/remote_image_size.py` (referenced but never present in this
  repo). Design doc §8 always intended it to die once NetHub renders
  `file_size`; the SFTP move settled it independently, since the
  distribution account is chrooted under `ForceCommand internal-sftp`
  with no shell and there is nothing on the far end to run a stat with.
  `file_size` is now required and asserted. Don't reintroduce discovery.
- **Fixed: SFTP, and the password is a prompt answer rather than part of
  the command.** The copy task used to read
  `copy scp://{{ ansible_user }}:{{ scp_pass }}@…`, which put a live
  credential into the device's command history and into TACACS+ command
  accounting — via the exact setting §4.3's attribution property needs
  enabled. It now runs `copy sftp://{{ dist_user }}@…` and answers the
  device's `Password:` prompt, because accounting records commands and
  not prompt responses (design doc §4.3.1). `no_log: true` stays on the
  task, since `answer` still carries the secret and Ansible offers no
  finer granularity — so a failed transfer is still opaque. Don't move
  the password back into the command string for debuggability.
  **Two strings in that task are unverified against a real device** and
  are commented as such: the `Password:` prompt and the "N bytes copied"
  `wait_for` condition, both carried over from the SCP form. The
  `wait_for` is what turns a truncated transfer into a failure, so it
  matters.
- **Fixed: `ansible_user` no longer does double duty.** It used to be
  both the SSH login to the device *and* the account the device pulled
  with, which breaks the moment it becomes a human's AAA name — the
  distribution host would need a local account per engineer, and the
  human's own password would be the one embedded in the copy command. The
  playbook now carries a separate `dist_user` service account (design doc
  §4.3). The vars are `dist_*` rather than `sftp_*` on purpose: the
  protocol is an implementation detail of one task, the identity of the
  host is not.
- The `verify /sha512` task (L273–281) is what §8.1's activation gate
  means by "still verifies" — a re-run of this command, not a stat of the
  file. A phase split that downgraded it to a presence check would break
  §3.4's third consumption at the one point it binds. It survives the
  push change unchanged: the device still verifies the digest, whichever
  direction the bytes arrived from.
- **There is no `become` in this playbook and there must not be one.**
  NetHub requires privilege 15 at login (design doc §4.3); the playbook
  asserts it in pre-check via `show privilege`, alongside a second
  assertion that `ip ssh source-interface` is configured, each failing
  with a message naming the requirement. Adding `ansible_become: true` back would
  reintroduce an enable secret the credential path deliberately does not
  carry.

## Keeping this file current

This file is derived from `design-document.md` plus the actual state of the repo,
and it goes stale silently. When a change lands that alters what a future
session needs to know — a design decision, a command, a dependency, a new
directory, a rule about what must not be built — update this file in the
same pass. The `design-doc-sync` skill in `.claude/skills/` covers what to
check and what belongs here.
