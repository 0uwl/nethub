# NetHub — Design Document

> NOTE: NetHub is in active development, expect nothing to work as expected for now. Features mentioned in
this document might or might not be actually implemented yet. A roadmap will be developed as soon as
the core architecture has been properly established and a scope has been defined.

*NetHub is a new, standalone project which extends my previous project [Drawbridge](https://github.com/0uwl/drawbridge). 
It takes architectural lessons and components from both the existing
ZTP system in Drawbridge and the earlier standalone "Network Software Depot" 
concept which this repo was supposed to be previously and combines them into 
one unified device lifecycle dashboard. Drawbridge will be deprecated in favor of 
this project once its provisioning module reaches parity.*

## 1. Overview

NetHub covers two halves of a device's lifecycle in one
system:

- **Provisioning** (day-0): a new device phones home, is checked
  against a serial allowlist, and receives its initial config/image.
- **Software Lifecycle** (day-2): onboarding new IOS-XE software
  images into the `image_registry` group_vars structure, staging them
  to a distribution server, and triggering fleet upgrades via Ansible,
  all through the same admin UI and backend.

One Flask backend, one database, one admin frontend, one deployment
unit. The device-facing provisioning endpoint is the only
unauthenticated route in the system; the admin UI (including all
Software Lifecycle functionality) sits behind an authenticated session.

NetHub is intended for a smaller scale, not enterprise level
inventories. There are already official platforms from network vendors
which deliver this kind of service for enterprise scales. NetHub aims to be a much 
smaller scale, FOSS-version of those dashboards to accelerate smaller teams'
effectiveness at provisioning and upgrading their inventory.

## 2. Goals / Non-Goals

**Goals**
- Single dashboard for provisioning new devices and maintaining
  software on devices already in the fleet.
- Image upload → checksum verification → staged publish → registry
  update → audit trail.
- A provisioning security model that's honestly documented rather than
  one that implies more protection than it delivers.

**Non-goals**
- Not a general job scheduler / orchestration platform (not an AWX/AAP
  replacement). NetHub dispatches a closed set of curated playbooks
  against an inventory it renders itself. It does not run user-supplied
  playbooks or accept user-supplied inventories, so the set of things it
  can do to a device is fixed at build time rather than at submit time
  (§8.1).
- Not an inventory management system. Device/serial data is retained
  only as long as operationally needed (allowlist, bounded provisioning
  log, job audit trail), not as a persistent asset database.
- Does not perform the device upgrade's actual command sequence itself;
  that logic stays in the existing Ansible playbook.
- No image transformation/repackaging.
- Not multi-vendor on day one. See Vendor Scope.

## 2.1 Vendor Scope

Only Cisco IOS-XE is supported initially. The Software Lifecycle module
carries an explicit `platform` field through its schema and job model
(even with one valid value today) and selects its publish playbook/role
per platform, so a second platform is an addition rather than a
rework.

## 3. Architecture

### 3.1 Frontend
Reuses Drawbridge's existing frontend architecture and codebase
wholesale, extended rather than rebuilt: the existing views (device
allowlist management, provisioning log) stay as-is, and new views are
added on top of the same framework/component structure for the
Software Lifecycle module, namely an image upload form, a registry
browser, and the software-lifecycle side of the unified log viewer.
Within that viewer, provisioning history and software-lifecycle job
history stay as separate, distinctly labeled logs rather than being
merged into one timeline. Each entry is a single row describing one
event (a provisioning attempt, a registry job). The system does not
correlate entries with each other or attach them to a persistent device
record; that would make it an inventory manager, which it explicitly
isn't (see Non-Goals).

### 3.2 Backend
One Flask service, organized as two logical modules:

- **Provisioning module**: phone-home endpoint, allowlist checks,
  DHCP integration, provisioning log.
- **Software Lifecycle module**: upload handling and SHA-512
  verification, bundle-key validation, per-job Ansible EE invocation via
  `ansible-runner`, registry file update under a lock, job audit
  logging.

Every Software Lifecycle route requires an authenticated admin session.
The phone-home route stays the system's sole unauthenticated entry
point, scoped narrowly to its existing contract.

Runs as a single process, with no multi-worker/concurrency handling in
the Flask app itself. The traffic volume here (occasional device
phone-homes, occasional admin-triggered uploads) doesn't warrant that
complexity, and the heavy lifting for both modules happens inside the
Ansible EE containers the backend invokes rather than in the Flask
process.

That justification only holds if EE invocation never blocks a request
handler. A publish job runs for minutes. A synchronous
`ansible_runner.run()` inside a route would stall the entire process for
its duration, including the day-0 phone-home endpoint, which has to stay
responsive because a device booting on the provisioning VLAN can't wait
or retry indefinitely. Publish requests therefore return as soon as the
job row is written: the EE run is dispatched out-of-band by a sibling
process that owns the queue (§9), and the dashboard reports progress by
polling `registry_jobs.status` instead of holding a request open. Job
execution is serialized, one EE run at a time. That is sufficient at
this scale, and it is what makes the registry write path in §7
tractable.

The phone-home endpoint is additionally rate-limited per source IP and
per claimed serial (§4.2). It is the only unauthenticated route, so it
is also the only one where an unbounded request rate is a
denial-of-service concern for a single-process app.

### 3.3 Distribution — day-0 vs. day-2
Both days serve bytes out of the **same staging tree on the same
distribution host** (see §3.4). They differ only in the access method
bolted onto it, and in who is trusted to use it:

- **Day-0**: ZTP script and image/config delivered to a new device as
  part of the phone-home flow, over plain HTTP (see §4 for the transport
  decision). No separate dedicated bootstrap container as it was in Drawbridge.
- **Day-2**: images pulled by an already-enrolled device over SSH
  (`copy scp://…` in `upgrade_iosxe.yml`; the same sshd serves SFTP for
  staging), authenticated with credentials from the vaulted inventory.

The distribution target stays modular. It can be a local bundled
container or an existing remote host, chosen via backend configuration.
Whichever is used, it exposes one directory tree through two daemons
rather than maintaining two separate stores.

The two daemons do not expose the same subtree. "One directory tree"
means one store with one ingest path, which is not the same as one
docroot: the HTTP daemon serves only the day-0 subtree (artifacts of
`kind` script/config), with directory indexing disabled, while day-2
images live under a subtree only the SSH/SCP daemon can reach. Without
that split, the HTTP adapter would be an unauthenticated read path over
the image store, and the SCP credentials §4 calls the real
access-control gate for day-2 would be bypassable by anyone who can
guess a filename. The point of the shared store is to make the protocol
split cheap; a shared docroot would make that split meaningless.

### 3.4 The artifact pipeline — one ingest, two egress adapters
NetHub's two halves are one subsystem at the storage layer and diverge
only at the very last step. Everything is a single pipeline:

```
ingest (upload → hash → size → store → record)
   │
   ├─ egress A (day-0): device pulls, HTTP, allowlist-gated, device-initiated
   └─ egress B (day-2): device pulls, SCP,  credentialed,   admin-initiated via Ansible
```

There is one artifact record. A day-0 config, the ZTP script itself, and
a day-2 IOS-XE image are the same shape (blob, SHA-512, size, platform,
uploader, timestamp), differing only by a `kind` discriminator. One
upload path, one hash function, one table (§5). The protocol split is an
access-method detail in the egress adapters rather than a division into
two subsystems.

The hash is computed once and consumed three times. Ingest computes the
SHA-512; it is then read by (a) day-0 payload verification, (b) the
rendered registry entry, and (c) the device's own `verify /sha512` step
during a day-2 upgrade. No component recomputes it.

SHA-512 is the single algorithm system-wide, day-0 included, and the
schema deliberately has no `hash_algo` column. The fixed point is the
device: IOS-XE verifies with `verify /sha512`, so the far end of the
day-2 path dictates the algorithm regardless of what NetHub would
prefer. Choosing anything else for day-0 would mean either storing two
digests per artifact or recomputing one at egress, and both break the
"computed once, consumed three times" invariant above for no gain. The
day-0 check is performed by a ZTP script NetHub itself ships (§4.1), so
there is no third-party client whose algorithm support has to be
negotiated.

This does have a cost: the schema cannot express two artifacts hashed
differently, so migrating off SHA-512 later means a schema change and a
re-ingest rather than a config flag. That is the right trade at this
scope. A second algorithm should arrive with a platform that requires
one, and adding the column speculatively means carrying branching logic
in all three consumers for a case that may never exist.

A hash binds bytes, and it binds nothing about identity or freshness. A
digest confirms that the file a device received is the file NetHub
stored. It says nothing about whether that was the *right* file for that
device, or a current one: an artifact that was valid a year ago still
verifies today, which for an image store means a superseded IOS-XE build
carrying a correct NetHub hash. Both egress adapters therefore resolve
what to serve server-side rather than honoring a client-supplied path.
Day-0 maps an allowlisted serial to a specific artifact row, and day-2
renders a specific artifact row into the registry the playbook reads.
The `artifacts.id` served is recorded on the log entry (§5), so "which
bytes did this device get" is answerable after the fact instead of
inferred from a filename.

The two logs share a mechanism and deliberately do not share semantics.
Provisioning history and software-lifecycle job history share a row
shape, a retention-purge helper, and a viewer component, with a
discriminator column keeping them as the separate, distinctly-labeled
logs §3.1 requires. Shared plumbing; no merged timeline, no cross-entry
correlation.

Three things stay explicitly un-unified. Day-0 does not route through
Ansible, since it is Kea plus phone-home and needs no EE. The two logs
are not merged into one timeline. The two egress protocols stay
distinct, because the shared store beneath them is what makes that split
cheap.

## 4. Security Model

**Decision: the device-facing provisioning endpoint serves over plain
HTTP, not HTTPS.** (This applies specifically to the phone-home /
image-and-config delivery path to devices. The admin web dashboard is
a separate, browser-facing surface where a normal CA-validated or
internally-trusted certificate provides real protection, and should
keep HTTPS as usual, e.g. behind the existing Tailscale/Caddy proxy
pattern.)

Rationale for the device-facing path: a brand-new device has no
pre-existing trust anchor to validate a server certificate against on
first contact, so the certificate it receives is delivered over the
same unauthenticated channel it's meant to secure. An attacker capable
of intercepting the plaintext bootstrap request is equally capable of
intercepting or substituting the TLS handshake. The certificate adds
operational complexity (issuance, rotation, config) without adding real
protection in this specific bootstrap scenario.

The system's protection comes from three things, unchanged by this
decision:
- **Network isolation**: the provisioning VLAN, restricted to
  attached devices via 802.1X / port security / DHCP snooping.
- **Serial allowlisting**: unregistered devices get nothing beyond a
  normal DHCP lease and the generic script.
- **Payload hash verification**: SHA-512 checks (matching the Software
  Lifecycle registry, §3.4) on delivered images/configs.

Those three are not independent, and presenting them as if they were
would undercut this section's own premise. Remove network isolation and
the other two go with it. A serial number is an unauthenticated
self-assertion carried over the same channel, and the ZTP script that
performs the hash check is itself fetched over that channel, so an
attacker who has defeated isolation has substantially defeated all three
at once. Listed separately, they describe defense in depth against
*accidents*: a mistyped serial, a corrupted transfer, a stale file on
the distribution host. Against an adversary the layering buys something
narrower. It raises the bar from "reach the VLAN" to "reach the VLAN
*and* know a valid serial *and* be positioned to answer before the real
server does."

This must be documented prominently for anyone deploying the system,
in the same spirit as Drawbridge's existing warning banner: **classic
ZTP is inherently less secure than Secure ZTP (RFC 8572)/MASA-based
provisioning**, and this system does not attempt to close that gap. It
only hardens classic ZTP with infrastructure the operator already
controls.

One residual nuance is worth writing down rather than leaving implicit.
If the hash accompanies the payload over the same unauthenticated
channel, an active on-path attacker who can already tamper with the
plaintext response could in principle alter both the payload and its
hash together. Hash verification here protects against transfer
corruption, a truncated or partially-written file on the distribution
host, and a device that silently received fewer bytes than it asked for.
It is not an independent control against an attacker already on the
provisioning VLAN.

It also does not protect against NetHub itself being wrong, which is the
easiest thing for a section like this to overclaim. The same server
supplies both the payload and the digest it is checked against, so a
misconfigured NetHub that serves the wrong artifact serves a matching
hash for it, and the device verifies it happily. That class of error is
caught by the server-side artifact resolution and served-artifact
logging in §3.4/§5, not by the device's own check; the verifier and the
thing being verified come from the same origin. All of this was equally
true before the plain-HTTP decision, since the prior TLS layer wasn't
closing these gaps either. It's just now stated directly rather than
left to imply otherwise.

### 4.1 What the allowlist can and can't do

Serials are identifiers rather than secrets. A Cisco serial is printed
on the chassis and the packing slip, appears on the purchase order, and
is readable over CDP/LLDP from an adjacent port. Treating the allowlist
as though a serial were a shared secret would overstate it considerably.
It is a statement about which devices the operator *expects*, and not
proof of which device is calling.

Two things follow, and both are cheap because they use infrastructure
the operator already runs:

- **Enforce the allowlist at DHCP, not only at phone-home.** Kea hands
  out option 67 only for MAC reservations corresponding to allowlisted
  devices, so an unregistered device on the VLAN is never pointed at the
  provisioning endpoint in the first place. The phone-home check then
  becomes the second gate rather than the only one, and the two gates key
  on different attributes (MAC and serial) that an attacker has to get
  right simultaneously.
- **Allowlist entries are one-shot and time-bounded.** An entry is
  created for an expected enrollment, expires on a TTL if it goes unused,
  and is consumed on first successful provision rather than staying
  permanently armed. A serial that leaked from a packing slip is only
  useful inside the window the operator opened for it; outside that
  window there is nothing to spoof. This is also the retention story
  §2's non-goals ask for, keeping the allowlist a short-lived work queue
  rather than an accumulating device database.

The generic script must not become an enumeration oracle. If an
allowlisted serial gets a different response *shape* than an unknown one
(a different status code, a different body length, a noticeably
different response time), the endpoint answers "is this serial
provisionable?" for anyone who cares to ask, and serials are guessable
enough to make asking worthwhile. Known and unknown serials therefore
receive the same response shape, and the generic script carries no
hostnames, artifact paths, or registry keys. It is a boilerplate no-op;
everything that differs per device lives in the artifact the allowlist
maps to.

### 4.2 Abuse visibility on the unauthenticated route

The phone-home endpoint is rate-limited per source IP and per claimed
serial. Denial of service is a real concern for a single-process app
(§3.2), but the more important reason is evidentiary. The provisioning
log is retention-bounded by design (§7.4), so a flood of failed attempts
can push the record of a genuine attack out of the log that was supposed
to show it. Denied attempts are therefore also counted in a small,
separate, never-purged tally per serial and per source. It survives log
rotation, costs a few integers to keep indefinitely, and is what an
alert fires on.

Silence is also a signal. A rogue DHCP relay or a redirected option 67
shows up as a device that simply never arrives: no failed provisioning
attempt, no log entry, nothing to look at. Because §4.1 makes allowlist
entries time-bounded, NetHub already knows which enrollments it is
expecting, so an allowlisted serial that doesn't phone home before its
window closes raises a notice. Without that, the entry just expires and
nobody learns anything. This is the only detection NetHub has for an
attack that succeeds by keeping the device away from it.

Day-2 image delivery deliberately stays credentialed (SCP), unlike
day-0. The `upgrade_iosxe.yml` playbook has the device pull its image
via `copy scp://...`, authenticated with credentials from the vaulted
inventory, rather than the plain HTTP used for the day-0 script fetch
(§3.3). The two paths are in different trust situations, so this is not
an inconsistency. A day-0 device has no credentials to offer regardless
of transport, so plain HTTP costs nothing extra there. A day-2 device is
already enrolled, and SCP's credentials are the access-control gate on
who can pull an image rather than merely transport encryption. Dropping
them for protocol uniformity would remove real authorization that hash
verification does not replace: hash verification confirms the fetched
bytes weren't tampered with, and says nothing about who was allowed to
fetch them in the first place.

## 5. Data Model

The engine is SQLite. That's a deliberate fit for the scale in §1 rather
than a placeholder, given one Flask process (§3.2), one job runner, and
one writer at a time (§7.1). It does need three non-default pragmas set
on every connection, because SQLite's defaults are wrong for a service
written to from a request handler and a job worker at once:
`journal_mode=WAL`, `busy_timeout` (a few seconds, so a concurrent
reader waits instead of raising), and `foreign_keys=ON`, which is off by
default and would otherwise silently turn every reference below into a
suggestion.

- `artifacts` table, the single ingest record behind both days (§3.4):
  `id`, `kind` (script/config/image), `platform`, `bundle_key`,
  `filename`, `sha512`, `file_size`, `remote_dir`, `version`, `state`,
  `superseded_by_id`, `uploaded_by`, `uploaded_at`. Every byte NetHub
  serves, on either day, has exactly one row here.
  - `bundle_key` is what makes the registry renderable: it is the
    key an `image_bundle` entry appears under, so rendering is a
    projection of rows rather than a merge against whatever the file
    already said. `UNIQUE(platform, bundle_key)` over rows where
    `kind = 'image'` and `state = 'published'` gives one published image
    per bundle key per platform, enforced by the database rather than by
    the publish job remembering to check.
  - `state` runs `staged` → `published` → `superseded`, with
    `superseded_by_id` pointing at the row that replaced it. This is what
    §6's "overwrite requires confirmation" does: confirming an overwrite
    supersedes the old row instead of updating it in place. Updating in
    place would rewrite history underneath the `registry_jobs` rows
    referencing it, leaving the audit trail describing artifacts that no
    longer exist as described. Only `published` rows render into the
    registry.
  - Indexed on `(kind, platform)` for the browse views and on `sha512`
    for duplicate detection at ingest.
- `image_registry.yml` (git-tracked, unchanged shape), **a rendered
  projection of the `artifacts` table rather than an independent source
  of truth.** Its `image_bundle` entries are serialized artifact records
  field-for-field (`filename`, `sha512`, `version`, `remote_dir`,
  `file_size`), and the publish job is its sole writer. It remains the
  file the upgrade playbook reads from; it is simply no longer
  hand-maintained, so the two cannot drift. Because it is derived, it is
  also *re-derivable*: the whole file is rendered from the table on
  every publish rather than patched in place, which is what makes the
  recovery path in §7.2 possible.
- `registry_jobs` table: `id`, `artifact_id`, `platform`, `bundle_key`,
  `version`, `filename`, `sha512`, `submitted_by`, `distribution_mode`
  (local/remote), `status`, `failure_stage`, `error_summary`,
  `render_state`, `started_at`, `heartbeat_at`, `finished_at`,
  `playbook_log_path`, `registry_commit_sha`.
  - `artifact_id` is the foreign key to the row being published. The
    `version`/`filename`/`sha512` columns sitting alongside it duplicate
    it on purpose: they are an immutable snapshot of what this job
    published *at the time it ran*, which has to survive the artifact
    later being superseded. The FK answers "which artifact"; the snapshot
    answers "what did we publish that day".
  - `status`, `failure_stage`, `error_summary`, and `render_state` are
    specified in §7.3 and §7.2.
  - Indexed on `(status, started_at)`, which is what the dashboard's
    default view and the startup sweep both query.
- Provisioning-side tables (allowlist, bounded provisioning log) carried
  forward from the existing ZTP design, retention-bounded as before. The
  provisioning log and `registry_jobs` share a row shape and retention
  helper per §3.4, distinguished by kind rather than merged. Two
  additions: each provisioning log row records the `artifacts.id` it
  served (§3.4), and allowlist entries carry the TTL and one-shot
  consumption state described in §4.1.
- `users`, the admin accounts backing the authenticated session §3.2
  requires. `uploaded_by` and `submitted_by` reference it. Without it
  they are free text that decays as people join and leave, which is a
  poor foundation for something whose stated purpose is an audit trail.

## 6. Workflow

**Day-0**: device phones home over HTTP → allowlist check → DHCP hooks
→ image/config delivery → hash verification → provisioning logged.

**Day-2** (behind admin auth): admin submits bundle key/version/file/
checksum → backend verifies SHA-512 against staged bytes → artifact row
written as `staged` → bundle key checked against the currently published
row (overwrite requires confirmation, and supersedes rather than
overwrites, §5) → job row written and the request returns → per-job
inventory built for the active distribution target → `publish_image.yml`
run via `ansible-runner` against the pinned EE image → on success,
artifact promoted to `published`, registry re-rendered from the table
under lock and committed → job result and log recorded and surfaced in
the dashboard.

Both flows above are the success path. What happens when an individual
step fails is §7. Because the day-2 flow spans a database, a working
tree, and a git repository, "what if it fails here" has a different
answer at almost every arrow.

## 7. Failure, Concurrency, and Staleness

§6 describes what happens when everything works. This section describes
what happens when it doesn't, which for a system writing to three places
that can't be committed together is most of the design.

### 7.1 One writer, one lock, named explicitly

Registry writes are serialized by a single advisory `flock` on the
registry repository, taken by the publish job for the whole
render-commit sequence and released only at the end. Saying "the
registry is locked" without naming the mechanism leaves the important
part unspecified. An in-process mutex is the obvious default and the
wrong choice here, because the things that can concurrently touch that
tree are not all inside one process: a retention purge, an operator's
shell, a second NetHub started by accident mid-deploy, and the EE
container itself are all outside it. A file lock is the smallest
mechanism covering all of them.

The job runner executes one EE run at a time (§3.2). Concurrency at this
scale buys nothing, since publishes are occasional and admin-initiated,
and it costs the entire class of interleaved-write bugs. The queue is
serial by construction rather than by locking discipline.

Before rendering, the publish job checks that the registry working tree
is clean. A dirty tree means something outside NetHub edited the file
NetHub is supposed to be sole writer of. The correct response is to stop
and say so, rather than render over it and destroy the evidence.

### 7.2 The registry write isn't atomic, so it's made re-derivable

The day-2 publish touches three stores with no transaction spanning
them: the database row, the rendered `image_registry.yml`, and the git
commit recording it. A crash between any two steps leaves a visible
inconsistency, either a registry entry with no job row, or a job row
naming a `registry_commit_sha` for a commit that was never made. The
latter is guaranteed by ordering alone, since the SHA can't be recorded
until after the commit it names exists.

The design doesn't try to make the sequence atomic. It makes it
*recoverable*, by keeping the file fully derivable from the table:

- **Render whole, never patch.** Every publish regenerates the entire
  `image_registry.yml` from all `published` artifact rows. A patched file
  depends on its own prior contents being correct; a rendered one depends
  only on the database, so any inconsistency is corrected by rendering
  again.
- **`render_state` on the job row** advances `pending` → `written` →
  `committed`, so an interrupted publish stays identifiable afterwards
  instead of looking like a completed one.
- **Reconcile on startup.** NetHub re-renders the registry from the table
  and compares against the file on disk. Equal is the normal case and
  costs a hash comparison. Unequal means a publish was interrupted, and
  the table wins: it is the source of truth by definition (§5), and the
  file is a projection of it.

Git is an audit copy rather than a synchronization partner. Its job is
to answer "what did this file look like on Tuesday", and a missing or
extra commit is a gap in the audit trail rather than a corruption of
state. The database commits first, deliberately, so the failure mode is
always "the table knows something the file doesn't", which the reconcile
pass fixes. The reverse case it could not fix.

### 7.3 Job lifecycle and failure semantics

`registry_jobs.status` is a state machine with explicit terminal states
rather than a success flag:

| status | meaning |
| --- | --- |
| `queued` | job row written, EE run not yet started |
| `running` | EE run in progress; `heartbeat_at` is being updated |
| `succeeded` | playbook completed, artifact published, registry committed |
| `failed` | playbook ran and returned non-zero |
| `timed_out` | exceeded the per-job wall-clock limit and was killed |
| `abandoned` | was `running` when the process died; assigned by the startup sweep |

`failure_stage` records *where* it stopped (`stage`, `verify`, `render`,
`commit`), and `error_summary` carries a short operator-facing reason.
Without them, a failed job in the dashboard says only that something
went wrong, leaving the operator to go reading `playbook_log_path` by
hand at exactly the moment they need an answer quickly.

Two rules fall out of this:

- **A crashed job doesn't stay `running` forever.** `heartbeat_at` is
  updated during the run, and a sweep at startup moves any `running` job
  whose process no longer exists to `abandoned`. A job stuck at `running`
  is otherwise indistinguishable from a slow one, which means nobody
  investigates it.
- **A failed publish doesn't promote the artifact.** The artifact stays
  `staged`, the previously published row stays published, and the
  registry is never rendered, so a failed job leaves the fleet on the
  last known-good registry instead of in a partial state. Staged bytes
  already copied to the distribution host are left in place and collected
  by retention (§7.4). Deleting them on failure would destroy evidence of
  what went wrong, and nothing references them in the meantime.

### 7.4 Retention, and what staleness means here

§2 commits to bounded retention as part of not being an inventory
system, which requires numbers rather than an adjective:

- **Provisioning log**: 90 days. Long enough to answer questions about a
  deployment wave after the fact, short enough that it isn't a device
  history database.
- **Denied-attempt counters** (§4.2): never purged. They are a few
  integers per serial and exist precisely to outlive log rotation.
- **`registry_jobs`**: 365 days, matching the operational question they
  answer ("what did we deploy last year, and when").
- **Allowlist entries**: expire on their per-entry TTL (§4.1), not on a
  global schedule.
- **Artifacts**: retained while referenced. An artifact that is
  `published`, or that any non-purged job row points at, is never
  collected regardless of age. That's a hard constraint rather than a
  policy knob. Collecting a published artifact would break the registry
  referencing it, and collecting a referenced one would leave the audit
  trail pointing at nothing.

Purging a `registry_jobs` row also removes its `playbook_log_path` file
in the same operation. A retention helper that deletes rows and leaves
logs behind produces an ever-growing directory of orphans nothing can
attribute.

Staleness is a property of the fleet, not of NetHub. The registry
records what a device *should* run. Nothing in NetHub records what it
*does* run, and per §2 nothing should, because that is inventory. The
consequence is that NetHub cannot tell an operator which devices are
behind, only what the current target is and which jobs ran against it.
Anything resembling fleet drift reporting has to come from the Ansible
side, where facts are gathered.

## 8. Ansible EE Integration

Reuse a pinned EE image, invoke via the `ansible-runner` Python API, a
minimal per-job `private_data_dir` and single-host inventory rather
than the full fleet inventory, and a dedicated `publish_image.yml`
selected by `platform`. That is the publish dispatch; §8.1 covers the
second kind, which installs what publishing produced.

Every run carries a wall-clock timeout and is killed on expiry instead
of being allowed to hang indefinitely (`timed_out`, §7.3). An EE run
that never returns is the failure mode a serial queue handles worst,
since one stuck job blocks every subsequent publish, so the bound
matters more here than it would with a concurrent runner. The per-job
`private_data_dir` is retained alongside `playbook_log_path` and purged
with the job row (§7.4), which is what makes a failed run diagnosable
after the fact.

NetHub always populates `file_size` in the rendered registry entry.
Because ingest measures the file in the same pass that hashes it (§3.4),
the size is authoritative before any device is ever contacted. This
retires `upgrade_iosxe.yml`'s remote-size discovery cascade: the
per-run localhost size cache, the `scp_server` stat fallback, and the
`files/remote_image_size.py` helper it shells out to (referenced by the
playbook but never written). Those exist only to answer a question
NetHub already knows the answer to; with a NetHub-rendered registry they
are dead paths and can be deleted from the playbook.

### 8.1 Upgrade dispatch and the phase split

Publishing an image and installing it are two different dispatches. §6's
day-2 flow ends at a published artifact and a committed registry;
`upgrade_iosxe.yml` is a second job kind, dispatched by the same sibling
against the same pinned EE, that installs a published image onto a set
of devices.

What a user submits is a request document, not an inventory: hosts, one
bundle key per host, and a small closed set of typed knobs. NetHub
validates it and compiles the real inventory around it. The two things
it will not take are the reason for that indirection. A user-supplied
playbook is arbitrary code inside the EE with the vaulted distribution
credentials in reach, which would put an authenticated RCE primitive
beside the unauthenticated route §4 spends its length reasoning about. A
user-supplied inventory carries `image_registry`, which would let a
request name any filename against any SHA-512 and bypass the `artifacts`
table entirely, breaking §3.4's "hashed once at ingest, consumed three
times" at the third consumption. Connection vars are withheld for a
third reason: `ansible_user` is the submitter's own device identity, and
an identity the submitter can type is not evidence of anything.

**The playbook's `pause` prompts assume a terminal that no longer
exists.** Run by hand, `upgrade_iosxe.yml` stops four times to ask
permission. Dispatched out-of-band by the sibling, there is no stdin to
answer on. Streaming a PTY to the browser would buy the interactivity
back at the cost of a second IPC channel between Flask and the sibling,
which §9 rules out — the job row is the channel, and a PTY stream does
not fit through one. So the interactivity is removed rather than
transported. The run is split into phases, each dispatched as its own EE
execution, and the confirmations become approval gates in the UI between
them:

| phase | device impact | gate before it |
| --- | --- | --- |
| plan | none | *is the submit form* |
| pre-check | read-only | none; runs on submit |
| stage | writes flash, non-disruptive | approve: copy image |
| activate | reload, traffic loss | approve: reload |
| verify | read-only | none; runs on completion |
| cleanup | removes inactive packages | approve: cleanup |

An approval is a row rather than a keystroke, which is the point of
doing it this way rather than with a terminal. "Who authorized the
reload of this device, and when" is a question the phase model answers
by construction; a terminal transcript is not an audit record.

Five things follow from the split:

- **The plan phase dissolves into the UI.** The preview play reads only
  `image_bundle` inventory vars and opens no connections — and NetHub
  wrote that inventory, so it already holds every host and target. The
  plan renders from the database and the gate is the submit button. The
  same argument retires the summary play at the other end: per-host
  results are job rows, so `batch_summary` has nothing left to compute.
- **`serial` narrows to the one phase that needs it.** Today
  `upgrade_serial=1` makes host 2's *image copy* wait on host 1's full
  reboot, because one play covers both. Copying to flash drops no
  traffic, so pre-check and stage run across all hosts at once and only
  activation stays serialized. That is the correct granularity for what
  `serial` is protecting, it is strictly faster, and it makes staging
  separable in time — images copied on Monday, activated in Saturday's
  window.
- **Almost no state has to cross a phase boundary.** Facts flow through
  one play today via `set_fact`, which separate executions break. But
  filename, `sha512`, `remote_dir` and `version` come from the rendered
  inventory, and `file_size` from the registry NetHub always populates
  (above). What is left is device state — current version, free space —
  which is re-gathered per phase and *should* be: a pre-check from three
  days ago must not authorize today's reload. The split is cheap
  precisely because the registry already carries the expensive facts.
- **Activation re-checks what staging established.** Between the two
  phases a device may have been upgraded by hand or had its flash
  cleaned, so activation reopens with the "not already on target version"
  assertion and a confirmation that the image is still present and still
  verifies. This is §7.4's staleness question in its concrete form: the
  gap between phases is exactly the window in which a prior phase's
  findings expire.
- **The serialization lock is held per phase execution, not per run.** A
  run parked at a gate holds no EE process, so §3.2's one-run-at-a-time
  rule applies to phases rather than to runs. Otherwise a run awaiting
  approval overnight would block every other upgrade and every publish.
  Phase jobs reuse `registry_jobs`' status vocabulary and its startup
  sweep (§7.3) unchanged; awaiting approval is a state of the parent run,
  which by definition has no process to sweep.

The split moves work rather than eliminating it. The `rescue` and
`always` blocks currently keep a host's failure local to the run; across
separate executions that becomes NetHub's per-host tracking, so the code
migrates from the playbook into the backend. Verification currently
lives inside activation's `always` block, so activation keeps a minimal
result-recording step and the verify phase becomes an independent
re-read, with some duplication between them. And six phases against N
hosts is considerably more job rows than one run, which §7.4's retention
numbers have to absorb.

## 9. Deployment

- Podman Quadlet units for the Flask app, plus DHCP integration
  running natively as its own service, plus (if local distribution
  mode is used) a Quadlet unit for the bundled SFTP container, dual-
  purposed to also serve the day-0 ZTP script over plain HTTP with no
  separate bootstrap container. The HTTP and SFTP daemons are pointed
  at different subtrees of the same store, per §3.3.
- **The nested-container question is settled in favor of the sibling
  process**, and it is a security decision rather than an operational
  one. Mounting the host's rootless Podman API socket into the Flask
  container would give the process behind the only unauthenticated route
  in the system (§3.2) the ability to start arbitrary containers on the
  host. That is a privilege far wider than anything NetHub needs, and it
  turns any Flask-side RCE into host-level container control. The
  EE-invocation step therefore runs as a sibling: a separate Quadlet unit
  that owns the job queue, watches for `queued` rows, and is the only
  component that talks to Podman. The Flask app never invokes an EE
  directly.
- That split has to be paid for in error propagation, which is why it's
  settled here rather than left to deployment time. The sibling and the
  Flask app communicate only through the database, so the job row *is*
  the IPC channel. The sibling writes `status`, `heartbeat_at`,
  `failure_stage`, and `error_summary` (§7.3); Flask only reads them. A
  sibling that dies mid-run leaves a stale `running` row, which is
  exactly the case §7.3's startup sweep exists to resolve. That sweep
  runs in the sibling rather than in Flask so it can never fire while a
  healthy run is in progress under another process.

## 10. Open Questions

- New repository, built clean for the backend, reusing applicable
  existing pieces (e.g. Kea configuration) rather than forking the repo
  wholesale. The frontend is the exception: it's carried over from
  Drawbridge as-is and extended in place (see §3.1), not rebuilt.
