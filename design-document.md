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
  images into the `image_registry` group_vars structure, publishing them
  into NetHub's own store, and triggering fleet upgrades via Ansible,
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
  log, job audit trail), not as a persistent asset database. One
  deliberate exception, stated here so the non-goal doesn't quietly veto
  it: NetHub retains SSH host-key fingerprints for devices it has
  connected to (§4.3, §5). A fingerprint is a security control — it is
  what stops a submitter-named address from collecting an engineer's
  device password — and refusing to keep it would trade a real
  authentication property for architectural tidiness. It is also not an
  inventory in any useful sense: it records that a key was seen at an
  address, answers no operational question about the fleet, and is never
  rolled up into a per-device view (§7.4).
- Does not perform the device upgrade's actual command sequence itself;
  that logic stays in the existing Ansible playbook.
- No image transformation/repackaging.
- **Not a distributed content-delivery system.** NetHub is the
  distribution host (§3.3); there is no support for image mirrors sited
  near remote fleets, so every byte of a day-2 upgrade is fetched from
  the NetHub host. This is a deliberate narrowing rather than an
  omission: owning the store outright is what lets §4.3.1 constrain the
  distribution credential to something nearly worthless, and what closed
  the question of how it is issued. It is also a real limitation for
  multi-site deployments, and §10 records where it would come back.
- Not multi-vendor on day one. See Vendor Scope.

## 2.1 Vendor Scope

Only Cisco IOS-XE is supported initially. The Software Lifecycle module
carries an explicit `platform` field through its schema and job model
(even with one valid value today) and selects its upgrade playbook per
platform, so a second platform is an addition rather than a rework.
Publishing no longer varies by platform at all — with NetHub owning the
store (§3.3) it is a local file operation, and a file is a file.

Two things a second platform inherits, worth checking early: §4.3.1
assumes the device can fetch a file over SFTP with the credential
supplied at a prompt rather than in the command, and §4.3 assumes a
level-15-equivalent authorization exists at login. Neither is universal.

## 3. Architecture

### 3.1 Frontend
Reuses Drawbridge's existing frontend architecture and codebase
wholesale, extended rather than rebuilt: the existing views (device
allowlist management, provisioning log) stay as-is, and new views are
added on top of the same framework/component structure for the
Software Lifecycle module, namely an image upload form, a registry
browser, an upgrade request form with the per-phase approval gates §8.1
requires, and the software-lifecycle side of the unified log viewer.
Within that viewer, provisioning history and software-lifecycle job
history stay as separate, distinctly labeled logs rather than being
merged into one timeline. Each entry is a single row describing one
event (a provisioning attempt, a registry job, an upgrade phase). The
system does not correlate entries with each other or attach them to a
persistent device record; that would make it an inventory manager, which
it explicitly isn't (see Non-Goals).

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

Runs as a single *worker* — one OS process, threaded within it — with no
multi-process/shared-nothing concurrency handling in the Flask app
itself. The traffic volume here (occasional device phone-homes,
occasional admin-triggered uploads) doesn't warrant that complexity, and
the heavy lifting for both modules happens inside the Ansible EE
containers the backend invokes rather than in the Flask process. The
one-process constraint is not a scale judgement and cannot be relaxed
later as one: §9.2 shows two workers silently breaking the credential
path. Threads within that process are how the long operations below are
kept from blocking each other.

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

The same argument applies to ingest, and it is easy to miss because
ingest never enters an EE. A 1.2 GB image upload occupies a request
handler for the length of the transfer plus a full SHA-512 pass, which
is longer than most publish jobs spend inside Podman. On a
single-threaded process the phone-home route would return nothing for
that whole window — the exact failure the paragraph above legislates
against, arriving through the other half of the same design. So the
process is single-*worker* and multi-*threaded*: `gunicorn` with
`--workers 1` and a threaded worker class. One worker is a hard
requirement rather than a tuning choice, for reasons §9.2 gives — the
in-memory credential store and the socket fd both assume one process —
while the thread count is what keeps a long upload from being a global
stall. The digest is computed incrementally over the chunks as they are
written to the staging tree, so §3.4's "hashed once at ingest" survives
unchanged and nothing re-reads the file.

**The phone-home budget is a number, because everything above is
calibrated against it.** The endpoint answers within two seconds at the
99th percentile, measured accept to last byte, and the ZTP script
retries with exponential backoff for fifteen minutes before giving up
and leaving the device sitting on its DHCP lease unprovisioned. Those
two figures are what "can't wait or retry indefinitely" means
concretely. They are the check any future change to the request path has
to pass, and they are why the ingest path above is threaded and why
§4.2's counters are kept off the request's write path. A device that
exhausts its backoff is a silent non-arrival, which §4.2 already treats
as a signal rather than as nothing.

The phone-home endpoint is additionally rate-limited per source IP and
per claimed serial (§4.2). It is the only unauthenticated route, so it
is also the only one where an unbounded request rate is a
denial-of-service concern for an app that will never be scaled out
horizontally (§9.2).

### 3.3 Distribution — day-0 vs. day-2
Both days draw bytes from the **same store on the NetHub host** (see
§3.4). They differ in how those bytes reach a device, and in who is
trusted to ask:

- **Day-0**: ZTP script and image/config delivered to a new device as
  part of the phone-home flow, over plain HTTP (see §4 for the transport
  decision). No separate dedicated bootstrap container as it was in
  Drawbridge. What the HTTP daemon serves and what NetHub decides are
  two different things — see "the day-0 fetch sequence" below.
- **Day-2**: images pulled by an already-enrolled device over SFTP
  (`copy sftp://…` in `upgrade_iosxe.yml`), authenticated with a
  credential NetHub mints for that phase execution and expires when it
  ends (§4.3.1).

**NetHub is the distribution host, and this is no longer modular.**
Earlier revisions let the target be either a local bundled container or
an existing remote host, chosen by configuration. That flexibility was
expensive in a way that only became clear when §4.3.1 tried to price it:
a distribution host NetHub does not administer is one whose account model
it cannot constrain, so it can neither issue the credential nor bound
what that credential opens. Owning the store outright turns both of those
from open questions into ordinary implementation. There is exactly one
distribution host, it is the NetHub host, and the transfer account on it
is NetHub's to define.

The cost is stated rather than hidden. A deployment with branch sites
cannot put a copy of the image near its devices; every device fetches
across whatever link separates it from NetHub. That is a real limitation
for multi-site fleets, it is out of scope for now consistent with §1's
stated scale, and §10 records where it would come back.

The two daemons do not expose the same subtree. "One store" means one
ingest path and one `artifacts` table, which is not the same as one
docroot: the HTTP daemon serves only the day-0 subtree (artifacts of
`kind` script/config, and only in the narrow arrangement the next section
specifies), with directory indexing disabled, while day-2 images live
under a subtree only the SSH/SFTP daemon can reach. Without that split
the HTTP adapter would be an unauthenticated read path over the image
store, and the credential §4.2 calls the authorization gate for day-2
would be bypassable by anyone who can guess a filename. The point of the shared
store is to make the protocol split cheap; a shared docroot would make
that split meaningless.

**A static daemon cannot enforce day-0's controls, so it is never asked
to.** §3.4 requires that day-0 resolve what to serve from an allowlisted
serial server-side, and §5 requires each provisioning log row to name
the `artifacts.id` it served. A file daemon is path-addressed by
definition: it cannot consult the allowlist, cannot consume a one-shot
entry, and cannot write a log row. Leaving per-device configs on an open
docroot with indexing disabled would put them in exactly the position
the paragraph above refuses for images — reachable by anyone who can
guess a filename — and a day-0 config is not a neutral file. It
routinely carries TACACS+/RADIUS shared secrets, SNMP communities, and
enable password hashes. The argument that keeps images off that docroot
applies with more force to the thing carrying credentials for the rest
of the network.

**The day-0 fetch sequence.** The open docroot therefore holds exactly
one thing at one fixed path: the generic boilerplate script §4.1
describes, identical for every device and carrying nothing. DHCP option
67 points at that URL — the HTTP daemon on the distribution host, not
the Flask service. The document never said so before, and every claim
about day-0 depends on which host answers first. Everything after that
fetch is resolved by NetHub:

1. The device fetches the generic script over plain HTTP at the
   option-67 URL. No decision about this device has been made yet.
2. The script POSTs its serial to the phone-home endpoint on the Flask
   service. That is the allowlist gate, the log write, and the one-shot
   consumption (§4.1), performed by the one component that can perform
   them.
3. Flask answers with per-device fetch URLs: high-entropy, TTL-bounded,
   one-shot paths minted for that provisioning attempt and recorded on
   the allowlist row. Minting writes a symlink into a `mint/` subtree of
   the day-0 docroot pointing at the artifact blob, which is §3.5's
   render-don't-accept rule applied to a filesystem — the subtree is a
   projection of live allowlist rows, reaped on consumption, on TTL
   expiry, and on the startup reconcile.
4. The device fetches those paths from the same HTTP daemon, which is
   now serving an unguessable path it was handed rather than deciding
   who may read what.

The daemon stays a daemon; the authorization stays in NetHub. The minted
path is a capability with the same TTL/one-shot shape §4.1 already
defines for the allowlist entry, so this is a reuse rather than a fourth
pattern. Known and unknown serials receive the same response shape at
step 3 (§4.1): an unknown serial is answered with syntactically
identical URLs that were never minted and resolve to nothing.

One honesty note the sequence forces, because it is the same gap §4.1
names. NetHub observes step 3, not step 4. The provisioning log records
which `artifacts.id` was *resolved and offered* to a serial, and a
separate flag records whether the minted path was subsequently fetched
from the daemon. Neither is proof the device booted the file. Recording
"offered" as "served" would be exactly the kind of overclaim §4 exists
to avoid.

### 3.4 The artifact pipeline — one ingest, two egress adapters
NetHub's two halves are one subsystem at the storage layer and diverge
only at the very last step. Everything is a single pipeline:

```
ingest (upload → hash → size → store → record)
   │
   ├─ egress A (day-0): device pulls, HTTP, allowlist-gated, device-initiated
   └─ egress B (day-2): device pulls, SFTP, credentialed,   admin-initiated via Ansible
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
Day-0 maps an allowlisted serial to a set of artifact rows — a config,
optionally an image, each in a distinct *role* — and day-2 renders a
specific artifact row into the registry the playbook reads. The
`artifacts.id` values resolved are recorded on the log entry (§5), so
"which bytes was this device offered" is answerable after the fact
instead of inferred from a filename.

Day-0's half of that is not free, because the daemon handing over the
bytes is a static file server with no view of the allowlist. §3.3's
minted-path sequence is what makes the claim true rather than
aspirational: the resolution happens in Flask, the daemon serves only an
unguessable path it was handed, and the mapping is a set of columns
rather than a naming convention (§5). Stating "resolved server-side"
while an open docroot answered on filename would have been the claim
without the mechanism.

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

### 3.5 Everything the EE reads is rendered

§3.4's registry file is the first instance of a pattern the rest of the
system follows: **NetHub renders the inputs its playbooks consume rather
than accepting them.** The registry file is a projection of the
`artifacts` table (§7.2). So is the per-job inventory — hosts,
`image_bundle` references, connection variables — written at dispatch
into a `private_data_dir` that is discarded with the job. So is the
playbook, in the weaker sense that it ships with NetHub and is selected
by `platform` rather than supplied.

What a user submits is a request: which devices, and which published
bundle each should end up on. NetHub validates it and compiles the rest.
The reasoning is in §8.1, and the rule is the one §7.2 already states
for the registry — the table is the source of truth, the file is derived
from it, and there is no second place for the two to disagree.

This is also why the rendered inventory carries no credentials.
Connection variables come from the submitting admin's identity (§4.3),
and the one secret a run needs is passed in memory rather than written
into the directory the EE mounts.

The image bytes are the one thing the EE never handles, and that falls
out of the same rule rather than being an exception to it. The device
fetches its own image directly from the store (§3.3), so what NetHub
renders is the *reference* — filename, digest, size, directory — and the
bytes stay where they were ingested. Nothing in the `private_data_dir`
is ever larger than a few kilobytes.

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

**The transport decision is about authentication, and the
confidentiality half has to be said out loud.** Everything above argues
that a certificate would authenticate nothing on first contact, which is
correct and is only half of what TLS would have provided. The other half
is that the bootstrap payload crosses the provisioning VLAN in
plaintext, and a day-0 config is among the most sensitive files the
operator owns: TACACS+/RADIUS shared secrets, SNMP communities, enable
password hashes, often a management-plane ACL that describes the
topology. A *passive* listener on that VLAN reads all of it — no
interception, no need to answer before the real server. That is a cost
of the decision rather than a residual nuance, and for a section whose
stated goal is not implying more protection than it delivers, leaving it
implicit would have been the largest remaining gap. It is also why §3.3
keeps per-device artifacts off an open docroot: network isolation is
carrying this payload's confidentiality by itself, so anything widening
who may request the file widens who reads those secrets.

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

**"Consumed on first successful provision" names an event NetHub cannot
observe, so the rule has to be restated in terms of what it can.** Day-0
is fire-and-forget HTTP: §3.3 makes plain that NetHub sees the
phone-home and the minted-path fetches, and never learns whether the
device booted the config. Three questions follow, and an attacker aims
at all three.

- **When is the entry burned?** At the phone-home, not at the last
  fetch. Burning late leaves the entry armed for a racing attacker who
  fails halfway through; burning early means a device that dies mid-
  provision needs the entry put back. NetHub takes the second cost
  deliberately and pays it with an explicit action: an admin can
  **re-arm** a consumed entry, which is an attributable operation with
  its own TTL rather than an automatic retry.
- **Consumption is a claim, not a flag write.** A genuine device and an
  attacker presenting the same serial concurrently would both read
  `armed` under a read-then-write. So the burn is a conditional update
  inside `BEGIN IMMEDIATE`, and the *rowcount is the authorization
  result*: `UPDATE allowlist_entries SET state='consumed', consumed_at=…
  WHERE serial=:s AND state='armed' AND expires_at > :now`. One changed
  row means this caller won the claim; zero means it lost, and it is
  denied exactly as an unknown serial would be. §3.2's single process is
  incidental protection here, not the mechanism — and the threaded
  worker class it now specifies removes even that.
- **One-shot is scoped to an attempt, not to a request.** A real ZTP
  flow is several fetches (script, config, sometimes an image), so
  strict single-request consumption would break provisioning outright.
  The claim opens a short bounded **provisioning window** — minutes, not
  hours — during which that attempt's minted paths (§3.3) resolve. When
  it closes, the paths are reaped whether or not they were used. The
  window, not the individual GET, is the thing that happens once.

One schema consequence, easy to get wrong: uniqueness is scoped to
*armed* entries rather than to the serial. A chassis that comes back
from RMA and needs re-enrolling must be able to have a new armed entry
while the old consumed one is still in the log.

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
serial. Denial of service is a real concern for a single-worker app
(§3.2), but the more important reason is evidentiary. The provisioning
log is retention-bounded by design (§7.4), so a flood of failed attempts
can push the record of a genuine attack out of the log that was supposed
to show it. Denied attempts are therefore also counted in a small,
separate, never-purged tally. It survives log rotation, costs a few
integers to keep indefinitely, and is what an alert fires on.

**"A few integers, never purged" assumes a scarce key space, and both
keys are attacker-chosen.** §4.1 has already established that serials
are guessable, on an unauthenticated route; an adversary on the VLAN
mints unbounded distinct serials, and from a single /64 mints unbounded
distinct sources. Keyed naively, the evidence table becomes an
unbounded-growth attack on the SQLite file every other subsystem shares
— an availability bug introduced by a control whose entire purpose is
availability of *evidence*. So the key space is bounded before the
retention promise is made:

- Per-serial counters are kept, never purged, only for serials that
  exist or have existed in the allowlist. Those are operator-created and
  therefore finite.
- Everything else — denials for serials NetHub has never heard of —
  aggregates into a per-source tally keyed by /32 for IPv4 and /64 for
  IPv6, capped at a top-N with an `__overflow__` bucket absorbing the
  tail. A flood then shows up as a large overflow count, which is itself
  the signal, rather than as N million rows.
- The counters are accumulated in memory and flushed periodically rather
  than written synchronously per request. §3.2 gives phone-home a
  two-second budget and WAL serializes writers, so incrementing a
  durable counter inside the hostile path is the one write the attacker
  controls the rate of. A flush loses at most the last interval's
  increments, which is an acceptable trade for keeping the denial path
  free of a write the attacker times.
- Denied and successful attempts get separate rate budgets. Rate-
  limiting a serial on denials alone lets an attacker lock out a genuine
  enrollment by burning that serial's budget before the device boots.

**A successful spoof looks like nothing, and that is the event worth
alerting on.** An attacker who wins the claim in §4.1 is answered
normally: the entry is consumed, a log row is written, no counter moves.
The genuine device then arrives minutes later and is denied — one
ordinary "unknown or already-consumed serial" row among however many the
flood produced. But *a denial for a serial that was consumed inside its
own TTL window* is the highest-signal event this endpoint can produce,
it costs one indexed query on the denial path, and nothing else in the
system will ever notice it. NetHub raises it as its own alert, distinct
from the volume-based ones. This is the same shape as the silence case
below: the attack that succeeds is the one that generates no failure.

Silence is also a signal. A rogue DHCP relay or a redirected option 67
shows up as a device that simply never arrives: no failed provisioning
attempt, no log entry, nothing to look at. Because §4.1 makes allowlist
entries time-bounded, NetHub already knows which enrollments it is
expecting, so an allowlisted serial that doesn't phone home before its
window closes raises a notice. Without that, the entry just expires and
nobody learns anything. This is the only detection NetHub has for an
attack that succeeds by keeping the device away from it.

Day-2 image delivery deliberately stays credentialed (SFTP), unlike
day-0. The `upgrade_iosxe.yml` playbook has the device pull its image via
`copy sftp://...`, authenticated with a credential minted for that phase
execution (§4.3.1), rather than the plain HTTP used for the day-0 script
fetch (§3.3). The two paths are in different trust situations, so this is
not an inconsistency. A day-0 device has no credentials to offer
regardless of transport, so plain HTTP costs nothing extra there. A day-2
device is already enrolled, and the transfer credential is the
access-control gate on who can pull an image rather than merely transport
encryption.
Dropping them for protocol uniformity would remove real authorization
that hash verification does not replace: hash verification confirms the
fetched bytes weren't tampered with, and says nothing about who was
allowed to fetch them in the first place.

What that gate is worth depends entirely on how the credential is
issued and what it opens, which is §4.3.1's subject rather than this
section's. The short version is that it is minted per phase execution
against an account NetHub itself defines — no shell, read-only, confined
to the image subtree — so the authorization is real while the credential
is nearly worthless to steal.

### 4.3 Admin identity, and who the device sees

Everything above concerns the unauthenticated day-0 route. The
authenticated side has its own model, and it turns on one awkward fact:
the protocol that authenticates an admin to NetHub cannot authenticate
that admin to a switch.

The admin session is OIDC — authorization code with PKCE against the
organization's existing IdP, keyed on the `sub` claim rather than on
email, which changes when people marry or teams get renamed. What the
IdP returns is an identity, not a decision. A directory that
authenticates the whole organization is not an authorization statement
about who may publish an IOS-XE image, so a successful login is checked
against the local `users` table and refused if there is no active row.
That is the shape of §4.1's serial allowlist reused: an external
assertion establishes *who*, a local list decides *what*, and neither is
trusted to do the other's job.

**The device credential is a separate mechanism, deliberately.** OIDC's
value is that NetHub never sees a password; `network_cli` needs a
password to send. Both are true, so the upgrade dispatch (§8.1) collects
the submitter's device credential and hands it to `ansible-runner` in
memory. It is never written into the `private_data_dir`, never kept in
the session, and never persisted.

It belongs to a *phase execution* rather than to the session or to the
run. The session is the wrong owner because a serial activation wave
outlives any reasonable session lifetime. The run is the wrong owner
because §8.1 lets a run park at an approval gate for days, and "held for
the life of the run" would mean a plaintext password resident in some
process's memory across a weekend. So it is collected with each approval
and dropped when the execution that approval released reaches a terminal
state — the same lifetime §4.3.1 gives the distribution password, for
the same reason. §9.1–§9.2 cover how it crosses from the browser to the EE
without touching disk, and what that costs the operator.

Every `users` row also carries a `role` (`admin` | `operator`). An admin
manages other users, the allowlist's steady-state config, and any
deployment-level settings (§4.4); an operator submits upgrades, uploads
images, and works the allowlist day to day. The distinction only gates
NetHub's own screens — it says nothing about, and never substitutes for,
what a person's device credential actually authorizes on the device
itself. Who gets to set that role, and whether NetHub or the IdP owns
the decision, differs by auth backend — see §4.4.

The username is not collected. It is `users.device_username`, mapped
from the OIDC identity server-side and set by an administrator rather
than by its owner. A username the submitter can type is not evidence of
anything, and evidence is the entire point of the arrangement:

**Two-sided attribution.** Under a shared `ansible` service account,
every change on every device is attributed to "ansible", and who
actually made it is answerable only from NetHub's own records — that is,
from the one system that would also be wrong if it were compromised.
Under per-user credentials the same human appears in
`upgrade_runs.submitted_by` on NetHub's side and in the device's own AAA
accounting and syslog on the other, recorded by two systems that share
no trust domain. Neither record is forgeable from the other's side,
which is a stronger property than either half provides alone.

That property has a precondition the rest of this section quietly
assumes: **centralized AAA with command accounting enabled.** With
per-human accounts configured locally on each device, both records —
NetHub's and the device's — are ultimately statements NetHub's own
deployment made, and "two systems that share no trust domain" stops
being true. The arrangement still beats a shared `ansible` account, but
it is corroboration by a second copy rather than by a second authority.
Deployments without central AAA should read the attribution claim at
that weaker strength, and the fact that command accounting has to be on
is also what makes §4.3.1's disclosure structural rather than
incidental.

What it costs, stated directly rather than left to imply otherwise:
NetHub handles the plaintext device password of every admin who runs an
upgrade, which an OIDC-only design would have avoided entirely. The
compensation is not encryption, it is scope. The credential is
job-scoped, memory-resident, and belongs to someone who already holds
enable on the devices in question — NetHub is mediating access its user
already has rather than manufacturing new access. That is what makes the
trade acceptable here, and it would not make it acceptable anywhere the
operator lacked that access already.

Two operational consequences follow:

- **A wrong credential must not become a lockout.** A stale password
  against a fifty-host activation wave is fifty failed authentications
  at the AAA server, which is how an engineer loses access to the entire
  estate. The credential is validated once before dispatch and the run
  refuses to start on failure, rather than discovering the problem one
  device at a time.
- **The device proves who it is before the credential is sent.**
  `network_cli` authenticates by sending the password after key
  exchange, and `hosts[].ansible_host` is submitter-supplied (§8.1), so
  without host-key verification an operator can name a machine they
  control and be handed a colleague's AAA credential — or an on-path
  attacker on the management network can take it. §4.3.1 concludes that
  the *distribution* password's exposure is structural and mitigates it
  with lifetime; this one is not structural and does not get that
  excuse. NetHub renders a `known_hosts` into the `private_data_dir`
  from the fingerprints in `device_host_keys` (§5) and fails closed on
  mismatch. First contact is trust-on-first-use with the fingerprint
  shown at the submit gate and recorded against the approver, which is
  weaker than out-of-band verification and stronger than nothing; a
  changed key thereafter stops the run rather than prompting.
- **NetHub requires privilege level 15, and that is a policy rather than
  a platform quirk.** Every command an upgrade runs — `write memory`,
  `copy` to flash, `install add … activate commit` — needs level 15 on
  IOS-XE. NetHub asks for it *at login* rather than reaching it through
  `enable`, so there is no escalation step in the playbook and no
  `become_password` to collect. That removes a second secret from a
  design whose whole §4.3 argument is about minimising them, and it
  removes the shared-enable-secret problem, which is the same
  shared-credential objection §4.3 raises about a service account wearing
  different clothes.

  It is also worth stating as a property rather than apologising for as a
  requirement. §4.3 says NetHub's own `role` gates NetHub's screens and
  "never substitutes for what a person's device credential actually
  authorizes on the device itself." The privilege requirement is what
  makes that concrete: someone who cannot hold level 15 on a fleet cannot
  upgrade it, and the refusal comes from the device rather than from
  NetHub's UI. A deployment where a level-1 account could drive a fleet
  upgrade through NetHub was relying on NetHub's screens as the only
  gate, which is the arrangement §4.3 exists to refuse. NetHub asserts
  the level at pre-check, so an under-privileged account fails once and
  clearly instead of part-way through a wave.

  **The full list of what NetHub requires of a device is short, and it
  should stay short.** Privilege 15 at login, and `ip ssh
  source-interface` configured for the SFTP client (§4.3.1). That is all.
  NetHub does not enable services, does not write configuration outside
  the upgrade itself, and asserts both requirements at pre-check rather
  than discovering them mid-wave. Anything that would lengthen this list
  should be weighed against what it buys — the push transfer model was
  rejected partly because it would have added a third item that NetHub
  had to set and then un-set (§4.3.1).
- **The distribution account stays shared; its password does not.** The
  device pulls its own image from the distribution host; no human is in
  that session. §4.2 calls that credential an authorization gate on the image
  store, which is a statement about the store and not about a person. So
  the account stays shared and stays separate from `ansible_user` —
  reusing one for both would require a local account on the distribution
  host for every engineer. What changes is its lifetime, and what makes
  that change cheap is that NetHub owns the distribution host outright
  (§3.3). §4.3.1 is why.

#### 4.3.1 Why the distribution password is per-run, and what it is worth

`upgrade_iosxe.yml` has the device fetch its own image, authenticating to
the distribution host as it goes. The obvious way to write that task —
and the way it was written — embeds the password in the command:

```
- command: 'copy sftp://{{ dist_user }}:{{ dist_pass }}@{{ dist_server }}/...'
  no_log: true   # suppresses Ansible's log — not the wire
```

`no_log` keeps it out of NetHub's job log. It cannot keep it out of
anywhere else, and there are two places it goes.

**The device, and this one is fixable.** A string that arrives as a
command lands in command history and in TACACS+/RADIUS command
accounting, once per host per run. That is not an incidental leak: §4.3's
two-sided attribution property *depends* on per-user AAA with command
accounting enabled, so the very setting that produces the audit evidence
would also write a live credential in cleartext to the AAA server.
Accounting records commands and not prompt responses, so moving the
password out of the command string into the **prompt-answer form**
removes this disclosure completely rather than mitigating it. The
playbook does that: `copy sftp://user@host/...` with the password
supplied as an answer to the device's `Password:` prompt. It is worth being precise
about what this costs — `no_log` stays, because the answer list still
carries the secret and Ansible offers no finer granularity, so a failed
transfer is still an opaque one.

**Whatever answered the connection, and this one is not fixable.**
`hosts[].ansible_host` is submitter-supplied, so an operator naming a
machine they control has that machine handed the credential. Withholding
connection vars (§8.1) does not close it: the address is enough on its
own, and any Flask-side compromise gets the same primitive by forging a
queued row. The credential is going to be disclosed, so the useful lever
is not confidentiality but **what it is worth when it is.**

Two things make it worth very little, and they only became available when
§3.3 settled that NetHub *is* the distribution host.

- **Lifetime.** NetHub owns the box, so it can issue the credential
  rather than hold a copy of a standing one. A minted password is valid
  for one phase execution, expires when that execution reaches a terminal
  state (§7.3) including on timeout and on the startup sweep, and is
  injected in memory rather than rendered into the `private_data_dir`
  (§9.1). What leaks into an attacker's listener is a string that no
  longer opens anything.
- **Scope.** The account it opens is purpose-built and constrained by
  construction: no shell, no interactive login, read-only, and confined
  to the published image subtree. What an attacker gains inside the
  window is the ability to download IOS-XE binaries — vendor files whose
  digests NetHub publishes anyway, to an operator who could already fetch
  them from Cisco. That is close to nothing, and it is only close to
  nothing because NetHub controls the account model. Against an existing
  remote host NetHub did not administer, none of these constraints could
  be assumed, which is exactly why §3.3 stopped treating the distribution
  target as modular.

The hardening in that second bullet is load-bearing rather than
hygienic. A shell on that account turns a near-worthless credential into
SSH access to the host running the only unauthenticated route in the
system (§9). It is stated here, not left to deployment.

**And it is why the transfer is SFTP rather than SCP.** The two protocols
are interchangeable for what this system does — NetHub renders an exact
path and never lists a directory (§3.4), and neither gives IOS-XE's
`copy` a resume — so the choice is decided entirely on the server side,
where they are not alike at all. `ForceCommand internal-sftp` with a
`ChrootDirectory` is a complete answer to the paragraph above: no shell,
no binaries in the chroot, nothing to execute. Legacy SCP is the `scp`
binary invoked *through a shell*, so serving it requires putting both
inside the chroot, and `ForceCommand internal-sftp` breaks it outright.
The hardening that carries the whole "what it is worth" argument is
nearly free one way and awkward the other. OpenSSH has also deprecated
the legacy SCP protocol, so the awkward option is the one with a
maintenance horizon.

The cost is one device-side prerequisite: Cisco documents `ip ssh
source-interface` as required for SFTP client functionality, which
`copy scp://` does not need. That is a real addition to §4.3's
device-configuration requirements rather than a free swap, and the
playbook asserts it at pre-check for the same reason it asserts privilege
15 — so a missing global command fails once and clearly instead of
part-way through a wave.

Two consequences worth stating plainly:

- **Minting is now a settled mechanism rather than an open question.**
  Earlier revisions listed rotated passwords, short-lived certificates
  and per-phase `authorized_keys` entries as candidates and could not
  choose between them, because the choice depended on a distribution
  host whose OS and account model §3.3 deliberately left unspecified.
  With the host NetHub's own, the credential is simply a value NetHub
  sets on an account it owns and clears when the phase ends. The
  authorization gate §4.2 describes is preserved intact: an
  unauthenticated puller still cannot fetch an image, because minting
  happens on NetHub's side of a dispatch that already required an
  authenticated submitter and an approved gate.
- **Only the phase that copies bytes gets one.** Under §8.1's split,
  stage is the phase that runs `copy sftp://…`; pre-check, activate,
  verify and cleanup never touch the distribution host. A run parked at
  the reload gate from Monday to Saturday holds no credential, which is
  the same property §8.1 claims for the EE process itself. It also leaves
  the rendered inventory with no long-lived secret in it at all, which is
  what lets Ansible Vault be removed rather than managed (§10).

**Why not push instead.** Inverting the transfer — NetHub enabling the
device's own SCP server and pushing the image to it — removes the second
credential entirely and was seriously considered. It was rejected on
cost, not on principle. It requires NetHub to modify the running
configuration of production hardware and put it back, and the restore is
skipped whenever a phase is killed, abandoned or cancelled mid-host,
leaving devices with a file-transfer service enabled that NetHub cannot
enumerate afterwards without building the per-device current-state view
§2 and §7.4 refuse. It moves every image's encryption from the device
into NetHub's own process, on the host that must not stall (§3.2). And it
depends on Ansible's `net_put`, whose SCP path is reported broken against
IOS-XE under `libssh` and whose `paramiko` alternative is deprecated with
a removal date — so the transfer would rest on a pinned image for
functional reasons or on shelling out to `scp`. Switching that transfer
to SFTP does not rescue it: **IOS-XE has no SFTP server.** Cisco
documents the SFTP client as always enabled and the server as
unsupported, consistently across trains. So under push, SCP is the only
wire protocol available and its Ansible path is the broken one. Pull has
the opposite shape — the device is a *client* in both protocols, so the
choice is free and falls to whichever hardens better. Pull keeps the transfer
inside `ios_command`, leaves device configuration untouched, and costs
one credential that the two levers above make nearly worthless. The
trade only looks like this because NetHub owns the distribution host; if
that ever changes, this section changes with it.


### 4.4 Local accounts, for teams without an IdP

OIDC is the default because it means NetHub never sees a password for
the admin session itself, but that shouldn't be a requirement to use
NetHub at all — a team with no IdP needs a way in too. Local
username/password is a second, self-hosted auth backend for the same
`users` row: same `role`, same `device_username` mapping, same session
shape downstream. `users.auth_backend` (`oidc` | `local`) picks which
one authenticates a given row, and a deployment isn't limited to one or
the other — an org with an IdP for most staff and a couple of contractor
accounts it doesn't want in the IdP can run both.

**Role is sourced from whichever backend authenticates the row, and only
one direction is ever locally editable.** For `local` rows, an admin
picks `role` at creation and can change it later from the same settings
page that manages the account. For `oidc` rows, `role` is computed at
every login from a configured claim — `oidc_group_claim` (which claim to
read, default `groups`) and `oidc_admin_group` (which value maps to
`admin`; any other membership maps to `operator`, and a *missing claim*
is treated separately — see below) are deployment-level settings
alongside the OIDC client id/secret/issuer
— and NetHub's settings page shows it read-only, not as an editable
field. An admin who wants to promote or demote an OIDC-backed user does
that in the IdP, by moving them into or out of the configured group, not
in NetHub; NetHub re-derives the role from the claim on the person's next
login rather than caching a stale grant. That's the same authority split
§4.1's allowlist and §4.3's `sub` claim already use, extended to role:
when an IdP is in the picture, it decides *who counts as what* for
anything it asserts, and NetHub's local table only decides what it's
authoritative for on its own — which, for an OIDC row, is no longer
role.

**An absent claim is not a demotion, because treating it as one fails in
a way that cannot be undone from inside NetHub.** The tempting rule is
that anything which isn't the admin group — including a claim that never
arrived — maps to `operator`. Group claims go missing for entirely
non-adversarial reasons:
a scope isn't requested after a client is re-registered, the IdP moves
groups to the userinfo endpoint, the group is renamed, or Entra emits an
overage indicator instead of `groups` past roughly two hundred
memberships. Any of those demotes *every* admin at once, and because
role is deliberately not editable for OIDC rows, the remedy lives on a
settings page that now requires an admin nobody has. None of what
follows softens the §4.4 asymmetry — the IdP still owns the decision.
It makes the asymmetry survivable:

- **An absent membership is not an absent claim.** Claim present and the
  admin group not in it is a demotion, and is applied. The claim
  entirely missing from the token is a *misconfiguration*:
  NetHub preserves the row's previous role for that session, refuses to
  write the demotion, and raises it as a deployment fault. Silently
  treating "the IdP told us nothing" as "the IdP told us no" is how one
  claim-scope change locks out an organization.
- **The deployment never reaches zero admins.** A role computation that
  would leave no active admin row is refused and alerted rather than
  applied. This is a floor, not a promotion path.
- **There is a documented break-glass.** A `nethub-admin` CLI, runnable
  only as the service user on the host — which is to say, by someone who
  already has the SQLite file — can create or reactivate a `local` admin
  row and issue an enrollment token. It is not a second authentication
  path into the web UI; it grants nothing that host access did not
  already imply, and it exists so that recovery is a documented
  procedure rather than an improvised `sqlite3` session.
- **Installation is the same problem in its first instance.** On a fresh
  database there is no active row, no admin, and no way to write either
  through a UI that refuses every login without one. First run seeds a
  single `local` admin with `must_reset_password` and prints a one-shot
  enrollment token to the service log — the §4.4 pattern above, applied
  to installation rather than invented for it.

**Mixed backends can be used to route around the IdP, so the deployment
gets to close that door.** The authority claim — the IdP decides who
counts as what — is defeated by the feature standing next to it. An OIDC
admin who knows they are about to lose their group membership creates a
`local` row with `role: admin` and an enrollment token; the IdP-side
demotion then changes nothing. This is an ordinary confused deputy, and
it is not a reason to drop local accounts, which exist for teams with no
IdP at all. It is a reason for `local_accounts_enabled` to be a
deployment-level setting that an org running OIDC can turn off,
following shared account mode's explicit-opt-in shape rather than being
implied. And it is why §5 grows an audit table for user administration:
in a system whose stated purpose is an audit trail, "who created this
account, who changed this role, who issued this token" should not be the
one question with no record behind it.

Enrollment for a local account reuses the allowlist's own TTL/one-shot
shape (§4.1) rather than inventing a second one: an admin creates the
user row and a single-use, time-bounded enrollment token — never a
password the admin picks on someone's behalf, which would just be the
shared-credential problem moved one level up. That token is the user's
first credential, valid for exactly one login, where it's spent setting
a real password; unused, it expires like an unused allowlist entry. The
row can't authenticate anything until that first reset completes.

Per-user device credentials assume a per-human identity — local or
OIDC, either satisfies it — but they still assume each engineer
authenticates to devices under their own name. A team on one shared
device account can't produce that, and forcing them to fake per-user
device logins just to use NetHub is worse than admitting the gap
outright. **Shared account mode** is an explicit, deployment-level
setting (not a per-user fallback, and not implied by choosing the local
auth backend) that fixes `ansible_user` to one admin-configured value
for every upgrade run in that deployment, instead of reading
`users.device_username`. It costs exactly what was just priced above
for the distribution account: every device-side change attributed to one
name,
answerable only from NetHub's own audit trail rather than corroborated
by the device's own AAA/syslog. That's a decision an admin makes once
and visibly, not a default a missing IdP quietly falls back to.

### 4.5 The session, which is where both backends' decisions expire

§4.3 and §4.4 both end at a successful login. What happens to that
decision afterwards was left unstated, and two of the properties above
depend entirely on the answer.

**`is_active` does not revoke anything on its own.** §5 chose
deactivation over deletion for good audit reasons and §4.3 refuses a
login without an active row — but neither touches a session that already
exists. Flask's default session is a client-side signed cookie, so with
no server-side store an offboarded admin keeps working until the cookie
expires on its own schedule. The same gap swallows §4.4's role
re-derivation: "re-derives on the person's next login rather than
caching a stale grant" sounds like freshness, and a login *is* the
caching event. If sessions are long and §4.3's workflows are
deliberately long-lived, an IdP-side demotion takes effect whenever the
user happens to log in again, which may be never.

So sessions are **server-side rows in SQLite**, and the decision is
re-checked rather than remembered:

- Every authenticated request re-reads `is_active` and `role` from the
  `users` row. Deactivation takes effect on the next request;
  a role change takes effect on the next request. The cookie carries a
  session id and nothing authoritative.
- Absolute and idle timeouts, both bounded. The absolute one exists
  because §4.3's activation waves outlive any sensible idle window and
  should not extend the session by being watched.
- A password reset, a role change, or deactivation invalidates that
  user's other sessions. An admin recovering an account should not be
  racing whoever else holds a cookie for it.
- Cookies are `Secure`, `HttpOnly`, and `SameSite=Lax` generally,
  `SameSite=Strict` on the approval routes. `SECRET_KEY` rotation
  invalidates sessions by design, which is acceptable once the store is
  server-side because the sessions themselves survive nothing else.

**CSRF is not a checkbox item here, because of what the buttons do.**
Every state-changing route takes a CSRF token, and the approval gates
take it most seriously: a cross-site POST that trips "approve: reload"
is a fleet outage triggered from a browser tab. §9.2 already requires
the credential form to be CSRF-protected for a different reason — that
form carries a password — and this is the same requirement arriving from
the availability side.

Two implementation notes that belong with the decision rather than in a
later ticket. The OIDC client is a maintained library rather than a
hand-rolled flow: `state` and nonce handling, redirect-URI allowlisting,
and ID-token signature/`iss`/`aud`/expiry validation are each a
well-known way to get authentication silently wrong, and none of them is
NetHub's problem to solve originally. And `config.py` currently sets
`DEBUG = True`, which is correct for the bootstrap the repo is in today
and is a release blocker for everything in this section as well as for
the credential path — §9.2 states why.

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
  `filename`, `sha512`, `file_size`, `remote_dir`, `storage_path`,
  `version`, `state`, `superseded_by_id`, `bytes_state`,
  `bytes_pruned_at`, `uploaded_by`, `uploaded_at`. Every byte NetHub
  serves, on either day, has exactly one row here.
  - `storage_path` is where the blob actually lives on disk, and
    `remote_dir` is how a *device* addresses it in a `copy sftp://…`
    path.
    They are not the same thing and both are needed: the first is what
    §7.3's retention purge collects by, the second is what the rendered
    registry entry carries into the playbook. Keeping them distinct is
    what stops the purge reasoning from a filename and a convention.
  - `bytes_state` / `bytes_pruned_at` split blob retention from row
    retention, which §7.4 otherwise conflates. "Retained while
    referenced, regardless of age" is the right rule for the *row* and
    the wrong one for half a gigabyte of superseded IOS-XE image pinned
    forever by one surviving job row. The row outlives the bytes and
    says so, so an audit query returns "published 2023-04, image pruned
    2026-04" rather than a path that silently no longer resolves.
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
- `image_registry.yml` (git-tracked), **a rendered projection of the
  `artifacts` table rather than an independent source of truth.** Its
  `image_bundle` entries are serialized artifact records field-for-field
  (`filename`, `sha512`, `version`, `remote_dir`, `file_size`), and the
  publish job is its sole writer. It remains the
  file the upgrade playbook reads from; it is simply no longer
  hand-maintained, so the two cannot drift. Because it is derived, it is
  also *re-derivable*: the whole file is rendered from the table on
  every publish rather than patched in place, which is what makes the
  recovery path in §7.2 possible.
- `registry_jobs` table: `id`, `artifact_id`, `supersedes_artifact_id`,
  `platform`, `bundle_key`, `version`, `filename`, `sha512`,
  `remote_dir`, `file_size`, `submitted_by`, `status`, `failure_stage`,
  `error_summary`, `render_state`, `created_at`, `started_at`,
  `heartbeat_at`, `deadline_at`, `finished_at`, `runner_instance_id`,
  `job_log_path`, `registry_commit_sha`.
  - `artifact_id` is the foreign key to the row being published. The
    `version`/`filename`/`sha512`/`remote_dir`/`file_size` columns
    sitting alongside it duplicate it on purpose: they are an immutable
    snapshot of what this job published *at the time it ran*, which has
    to survive the artifact later being superseded. The FK answers
    "which artifact"; the snapshot answers "what did we publish that
    day". The snapshot has to carry every field the rendered registry
    entry carries (§3.4), or the row that exists to answer that question
    cannot reproduce the entry it wrote.
  - `supersedes_artifact_id` records the row the admin was actually
    shown at confirmation time. §6's overwrite check happens at submit
    and the supersede happens when the job runs, minutes later and
    possibly behind another queued publish of the same key, so the admin
    can confirm replacing X and have the job supersede Y. The promote
    step fails if this is no longer the published row for the key. §5's
    partial unique index catches only the end state — two published rows
    — and not the wrong-predecessor case.
  - `status`, `failure_stage`, `error_summary`, and `render_state` are
    specified in §7.3 and §7.2.
  - `created_at`, `deadline_at` and `runner_instance_id` are the columns
    §7.3's machinery needs and §9 assumes. `created_at` is what the sibling orders the queue by —
    `started_at` is null until dispatch, so ordering on it means
    ordering on `NULL` for exactly the rows the sibling reads.
    `deadline_at` makes §8's wall-clock timeout survive a sibling
    restart, since a bound held only in the runner's memory is not a
    bound. `runner_instance_id` is a UUID minted at sibling startup and
    also written to settings; it is how the sweep identifies a job from
    a dead runner, where a bare PID is reused across container restarts
    and is meaningless across PID namespaces. There is no
    `private_data_dir` here: with NetHub owning the store (§3.3),
    publishing is a local operation rather than an EE dispatch (§8), so
    the log column is named `job_log_path` rather than
    `playbook_log_path`.
  - Indexed on `(status, created_at)`, which is what the dashboard's
    default view and the sibling's queue read both query.
  - `UNIQUE(platform, bundle_key) WHERE status IN ('queued','running')`.
    §7.1's `flock` serializes writers to the registry *file*; this
    serializes claimants on a *bundle key*, which is a different
    collision and the one that produces the wrong-predecessor case
    above.
- `upgrade_runs` table, the parent record for one dispatch of
  `upgrade_iosxe.yml` (§8.1): `id`, `platform`, `submitted_by`,
  `device_username_used`, `shared_account_mode`, `request_document`,
  `request_sha512`, `state`, `awaiting_phase`, `gate_expires_at`,
  `cancel_requested_at`, `cancel_requested_by`, `created_at`,
  `finished_at`.
  - `state` is the parent-level state the phase model needs, enumerated
    in §7.3 alongside the other two machines. Phase executions carry
    §7.3's status vocabulary; the run carries where it sits between
    them.
  - `awaiting_phase` says *which* gate a run at `awaiting_approval` is
    sitting at. Without it the answer has to be inferred from the
    highest phase row present, which re-encodes §8.1's phase order in
    application code — and because cleanup is optional, "awaiting
    cleanup approval" and "finished, cleanup declined" become
    indistinguishable. `gate_expires_at` bounds the wait, so a run
    nobody returns to reaches a terminal state instead of parking
    forever and being purged mid-flight by §7.4. Declining the optional
    cleanup gate is an explicit action that closes the run.
  - `cancel_requested_at` / `cancel_requested_by` are how a human stops
    something. §9 is right that the job row is the only control channel,
    which means a stop needs a column the sibling polls, and there was
    none: an activate phase reloading fifty switches with the wrong
    bundle could only be halted by killing the container, leaving a
    stale `running` row and an unknown fleet state. The sibling checks
    these between hosts. What cancel *means* is per phase and is stated
    in §8.1 — cancelling a stage is safe, cancelling an activation
    mid-wave is not.
  - `device_username_used` snapshots `users.device_username` at
    dispatch, for the reason `registry_jobs` snapshots
    `version`/`filename`/`sha512`: an audit row that re-reads its own
    answer from a mutable table stops being an audit row the first time
    somebody's mapping is corrected. `shared_account_mode` snapshots
    *how* that name was chosen, and it is not optional decoration.
    Under §4.4's shared account mode every run records the same
    configured name, so an auditor reading this table a year later
    cannot tell "jsmith ran this" from "everyone runs as jsmith" — which
    erases precisely the property §4.3 spends its length building. §4.4
    calls the flip "a decision an admin makes once and visibly"; that is
    a data requirement, not a UI one.
  - `request_document` stores the submitted request itself, and
    `request_sha512` its digest. NetHub hashes what it ingests (§3.4),
    and a document deciding which images land on which devices is not
    the exception to that — but a digest whose preimage is stored
    nowhere is unverifiable, which makes it decoration. The document is
    a few KB and expires with the run. It does not become an `artifacts`
    row: that would need a fourth `kind` for something that is a request
    rather than a served byte.
- `upgrade_run_hosts` table: `run_id`, `hostname`, `ansible_host`,
  `artifact_id`, `bundle_key`, `filename`, `sha512`, `version`,
  `remote_dir`, `file_size`, `flash_dir`, `reported_version_pre`,
  `reported_version_post`, `state`, `last_phase`, `error_summary`, with
  `PRIMARY KEY (run_id, hostname)`. One row per targeted device, using
  the same foreign-key-plus-snapshot arrangement as `registry_jobs`.
  Per-host state living here rather than in the play is what retires the
  `rescue`/`upgrade_stage_failed` bookkeeping §8.1 describes moving into
  the backend.
  - The primary key is load-bearing rather than tidy. A request document
    naming the same host twice is trivially producible in hand-written
    YAML, and without the key it yields two inventory entries and two
    reloads.
  - `remote_dir` and `file_size` complete the snapshot for the same
    reason they do on `registry_jobs`, and here the consequence is
    operational rather than archival. The playbook needs `remote_dir` for
    the `copy sftp://…` path and `file_size` for both the disk-space
    assertion and the `wait_for` byte-count condition — and §8 retires
    the remote-size discovery cascade specifically on the promise that
    NetHub always populates `file_size`. Without both columns the stage
    phase either cannot render its inventory from this table, or has to
    re-read `artifacts` at dispatch, which makes the snapshot decorative
    and lets a mid-run supersede silently re-target the run. With them,
    each phase renders from the run's own rows and reads `artifacts` not
    at all — which also settles a question the document otherwise leaves
    open, namely whether dispatch consumes the committed
    `image_registry.yml` or re-renders from the table. A self-contained
    run makes the question moot.
  - `version` is the snapshotted *target*, so §7.4's own sentence — that
    these rows record "the version a device reported during that run" —
    needed columns that did not exist. `reported_version_pre` and
    `reported_version_post` are those. They are not the forbidden
    roll-up: they are run-scoped, purged with the run, and never keyed
    by hostname across runs (§7.4).
  - `state` and `last_phase` are a cursor, not a record: activation
    overwrites what staging left, so "staged clean on Monday, failed
    activation on Saturday" — exactly the question the phase split
    exists to answer — becomes unanswerable from this table alone.
- `upgrade_host_phase_results` table: `run_id`, `hostname`, `phase`,
  `attempt`, `status`, `failure_stage`, `error_summary`, `started_at`,
  `finished_at`, keyed `(run_id, hostname, phase, attempt)`. One row per
  host per phase execution, which is what makes the per-host history
  above legible. `upgrade_run_hosts`' cursor columns stay as a derived
  convenience for the dashboard's default view rather than as the record
  of what happened.
- `device_host_keys` table: `ansible_host`, `key_type`,
  `fingerprint_sha256`, `first_seen_at`, `confirmed_by`,
  `confirmed_at`. One row per address NetHub has connected to,
  supporting §4.3's fail-closed host-key check. It is keyed on the
  address rather than on a device identity on purpose: NetHub is not
  tracking devices (§2), it is remembering what answered at an address
  so that a change in the answer is visible. `confirmed_by` records the
  human who accepted the key on first contact, which is what makes a
  later mismatch attributable rather than merely surprising.
- `upgrade_phase_jobs` table: `id`, `run_id`, `phase`, `attempt`,
  `approved_by`, `approved_at`, `status`, `failure_stage`,
  `error_summary`, `created_at`, `started_at`, `heartbeat_at`,
  `deadline_at`, `finished_at`, `runner_instance_id`,
  `private_data_dir`, `playbook_log_path`. One row per EE execution,
  reusing `registry_jobs`' status vocabulary and startup sweep (§7.3)
  unchanged — including the four columns that vocabulary turned out to
  need, which apply here identically. `approved_by` and `approved_at`
  are what §8.1 means by an approval being a row rather than a
  keystroke; without them the gate is a UI affordance instead of a
  record.
  - `UNIQUE(run_id, phase, attempt)`, and this is the mutex the gate
    actually needs. §8.1's serialization guarantee is scoped to
    *execution*: it stops two EE processes overlapping, not two rows
    being created. Two admins on the approval screen both clicking
    "approve: reload" write two rows, and the serial queue then runs
    them one after the other — the fleet reloads twice. §8.1 says an
    approval is a row rather than a keystroke, so the row is where the
    collision has to be refused. `attempt` exists because §7.3 grants an
    `abandoned` phase a fresh, separately-approved retry; without it the
    constraint would forbid the retry along with the double-click.
- `allowlist_entries` table, the day-0 side's central record: `id`,
  `serial`, `mac`, `platform`, `config_artifact_id`,
  `image_artifact_id`, `script_artifact_id`, `state`, `expires_at`,
  `consumed_at`, `window_expires_at`, `rearmed_from_id`, `created_by`,
  `created_at`. Carried forward from the existing ZTP design in spirit,
  but spelled out here because §3.4 and §4.1 pile requirements onto it
  that a "TTL and one-shot state" sentence cannot hold.
  - **The serial-to-artifact mapping is the central day-0 workflow and
    had no column anywhere.** §3.4 requires that day-0 map an allowlisted
    serial to specific artifact rows and §4.1 that "everything that
    differs per device lives in the artifact the allowlist maps to";
    neither is representable without these. It is deliberately plural
    and role-keyed rather than a single `artifact_id`: §1 and §6 say
    config *and* image, §3.3 adds the script, and one column cannot
    record a config plus an image.
  - `mac` is required by §4.1's Kea gate, which keys on MAC while
    phone-home keys on serial — the whole point being that an attacker
    has to get both right. It belongs on the row that drives the
    reservation.
  - `state` is `armed` → `consumed` (or `expired`), claimed by the
    conditional update in §4.1. Uniqueness is `UNIQUE(serial) WHERE
    state = 'armed'`, scoped to armed entries so an RMA'd chassis can be
    re-enrolled while the old consumed row is still in the log.
    `window_expires_at` bounds §4.1's provisioning window, which is what
    the minted paths in §3.3 resolve against. `rearmed_from_id` records
    an admin's deliberate re-arm as a new row pointing at the old one,
    rather than resetting `state` in place and erasing the fact that a
    first attempt happened.
- `provisioning_log` table: `id`, `occurred_at`, `serial_claimed`,
  `mac_seen`, `source_ip`, `outcome`, `allowlist_entry_id`,
  `fetched_at`. §4.2 makes this the evidentiary record for the system's
  only unauthenticated route, so it needs more than a boolean.
  - `outcome` is an enum, because "unknown serial", "expired entry",
    "already consumed", "rate-limited", and "resolved and offered" are
    five different facts an investigator needs to tell apart — and
    §4.2's highest-signal alert is precisely the pair *consumed* then
    *denied* for one serial inside one TTL window.
  - `serial_claimed` and `mac_seen` are denormalized onto the row rather
    than reached through the FK. Entries expire on a per-entry TTL while
    this log lives 90 days (§7.4), so a foreign key would either block
    the allowlist's own cleanup or dangle. `allowlist_entry_id` is kept
    as a nullable convenience for the window when both exist.
  - `fetched_at` records whether the minted paths were actually
    collected, which §3.3 is careful to distinguish from the device
    having booted the file.
- `provisioning_log_artifacts` junction: `(log_id, role, artifact_id)`.
  §3.4 says the log records "the `artifacts.id` it served", singular,
  and the flow serves several. The junction is what makes "which bytes
  was this device offered" answerable for a config *and* an image
  without a column per role on the log row.
- The provisioning log and `registry_jobs` share a row shape and
  retention helper per §3.4, distinguished by kind rather than merged.
- `settings` table: `key`, `value`, `updated_by`, `updated_at`, plus an
  append-only, never-purged `settings_audit` recording every change with
  its old and new value. §4.4 names five OIDC settings, shared account
  mode and its fixed `ansible_user`, and `local_accounts_enabled`; §8.1
  adds the target CIDR and the stage-phase concurrency cap; §4.3 makes
  managing all of it an admin's job — and none of it had anywhere to
  live.
  - The audit table is not symmetry for its own sake. §4.4's claim that
    flipping shared account mode is "a decision an admin makes once and
    visibly" is only true if the flip leaves a record, and the settings
    here decide who is an admin and which account every device sees.
  - Secrets stay out of this table. The OIDC client secret and the
    shared-account password are supplied as systemd credentials or
    environment, not rows, or the settings page becomes a plaintext
    secret store readable by any admin.
- `user_admin_audit` table: `id`, `occurred_at`, `actor_user_id`,
  `target_user_id`, `action`, `detail`. Who created an account, who
  changed a role, who issued or revoked an enrollment token, who
  deactivated whom. §4.4's mixed-backend confused deputy is exactly the
  kind of event that should not be reconstructable only from inference,
  and a system whose stated purpose is an audit trail should not have
  user administration as its one unrecorded operation. Never purged; it
  is a few rows per person per career.
- `users`, the accounts backing the authenticated session §3.2
  requires: `id`, `auth_backend`, `username`, `oidc_issuer`,
  `oidc_subject`, `password_hash`, `must_reset_password`,
  `display_name`, `device_username`, `role`, `is_active`, `created_at`.
  `uploaded_by` and `submitted_by` reference it. Without it they are
  free text that decays as people join and leave, which is a poor
  foundation for something whose stated purpose is an audit trail.
  - `auth_backend` (`oidc` | `local`, §4.4) picks which of the
    backend-specific fields is meaningful for this row; the other stays
    null. That pairing is stated as prose above and enforced as a
    `CHECK` here, because a rule that lives only in the paragraph
    describing it is a rule the first migration breaks.
  - `username` is what a `local` user types at a login form, and §4.4
    introduced a full second auth backend without it. Nothing else on
    the row can stand in: `oidc_subject` is OIDC-only by construction,
    `display_name` carries no uniqueness, and `device_username` is
    emphatically not this — §4.3 makes that the name a person
    authenticates to *devices* under, and conflating them would
    re-couple NetHub login to device AAA, which §4.3 spends its length
    separating. `UNIQUE(username) WHERE auth_backend = 'local'`.
  - `oidc_subject` is the IdP's `sub` claim. Email is not, because it
    changes. The unique key is `(oidc_issuer, oidc_subject)` rather than
    `sub` alone: `sub` is unique only *within* an issuer, and §4.4 makes
    the issuer a deployment setting an admin can change.
  - `password_hash` backs `local` rows only. It's never set directly by
    an admin — enrollment issues a one-shot token instead (§4.4), and
    the row is created with `must_reset_password` true, cleared only
    when the user spends that token on a real password.
  - `role` is `admin` or `operator` (§4.3): admin manages users and
    deployment-level settings, operator runs day-to-day
    provisioning/upgrade work.
  - `device_username` is the name this person authenticates to devices
    under (§4.3). An administrator sets it; its owner does not, and no
    submitted request is ever read for it. It is unique across active
    rows: §4.3's attribution property is a claim that one AAA identity
    corresponds to one human, and two NetHub rows mapping to one device
    identity breaks it silently, at exactly the moment an auditor is
    relying on it.
  - `is_active` rather than deletion. A row an audit trail references
    cannot be removed without rewriting history, which is the same
    reasoning that makes an artifact `superseded` instead of updated.
- `sessions` table: `id`, `user_id`, `created_at`, `last_seen_at`,
  `absolute_expires_at`, `revoked_at`. §4.5 explains why the session had
  to become a row: a client-side signed cookie makes `is_active` and
  §4.4's role re-derivation advisory, because neither has anything to
  act on until the cookie expires on its own. The cookie carries this
  id; authority is re-read from `users` on every request. Purged on
  expiry, and not an audit record — `user_admin_audit` above is where
  the durable statements live.
- Local-account enrollment tokens carry the TTL/one-shot shape §4.1
  already defines for the allowlist: a token tied to one `users.id`,
  consumed exactly once, expiring unused. Not a new pattern, so not
  broken out as a separate concept here beyond noting the reuse.

## 6. Workflow

**Day-0**: device boots → DHCP option 67 points it at the generic script
on the distribution host → script POSTs its serial to the phone-home
endpoint → allowlist claim (§4.1) → per-device artifacts resolved and
one-shot fetch paths minted (§3.3) → device fetches config/image over
plain HTTP → hash verification on the device → attempt, outcome and
resolved artifacts logged.

**Day-2, publish** (behind admin auth): admin submits bundle
key/version/file/checksum → backend verifies SHA-512 against staged
bytes → artifact row written as `staged` → bundle key checked against
the currently published row (overwrite requires confirmation, and
supersedes rather than overwrites, §5) → job row written and the request
returns → the sibling promotes the bytes into the published subtree,
promotes the artifact row to `published`, re-renders the registry from
the table under lock and commits it → job result and log recorded and
surfaced in the dashboard.

Publishing stopped being an EE dispatch when NetHub became the
distribution host (§3.3). It was one because the store might be a remote
machine and Ansible was the remote-operation tool already to hand; with
the store local, publish is a rename within one filesystem plus a git
commit. Two things this does *not* change, because the obvious
misreading is that dropping the EE drops the machinery with it. It does
not move into Flask — §3.2's argument applies unchanged, so publish
stays in the sibling, keeps its `registry_jobs` row, its place in the
serial queue, `render_state`, the `flock`, the startup sweep, and every
state in §7.3. And it does not merge with installing: publishing touches
no device, installing is now the only EE dispatch in the system, and
that makes the two harder to confuse rather than easier.

**Day-2, upgrade** (behind admin auth): admin uploads an upgrade request
→ request validated (supported platform, every bundle key resolving to a
`published` artifact row, `ansible_host` inside the target CIDR, no
other connection variables and no template expressions) → run and
per-host rows written, request document and digest recorded → device
credential collected at the submit gate and validated once against the
AAA server before dispatch → pre-check phase dispatched against a
NetHub-rendered inventory, asserting privilege 15 among its checks (§4.3)
→ per-host results recorded and the run parks at `awaiting_approval` →
admin approves staging, supplying the credential for that execution →
distribution password minted for that execution (§4.3.1) → each device
pulls its image over SFTP and verifies it against its SHA-512 → minted
password expired → admin approves activation → devices reloaded in serial
waves → verification runs without a gate →
optionally, admin approves cleanup, or declines it and closes the run →
per-host outcomes and phase logs surfaced in the dashboard.

A credential is collected at each gate rather than once at submit, and
§9.1 explains why that is the price of "no device credential is ever at
rest". An operator can cancel a run at any gate, and between hosts
during a phase; §8.1 says what that costs at each one.

All three flows above are the success path. What happens when an
individual step fails is §7. Because the publish flow spans a database,
a working tree, and a git repository — and the upgrade flow spans a
database and a fleet of devices that reboot — "what if it fails here"
has a different answer at almost every arrow.

### 6.1 The API surface

The workflows above are the thing being built; the routes are how the
frontend reaches them, and leaving them unstated means the dashboard and
the backend are designed against different assumptions about what an
error looks like. This is not a full specification — request and
response bodies belong with the implementation — but the shape is a
design decision and belongs here.

**One unauthenticated route, named exactly.** `POST /provision` takes
the claimed serial and whatever the device reports about itself, and
answers `200` with the minted fetch URLs (§3.3) whether or not the
serial was allowlisted, because §4.1 requires known and unknown serials
to receive the same response shape. It never returns `403`, never
returns a different body length for a known serial, and never varies
measurably in time. `429` is the one other status it emits, on the rate
limit. Nothing else in the system is reachable without a session.

**Everything else is session-authenticated, CSRF-protected on writes
(§4.5), and falls into four groups.** Allowlist management (`GET`/`POST`
`/allowlist`, `POST /allowlist/<id>/rearm`, `DELETE /allowlist/<id>`).
Artifacts and publishing (`POST /artifacts` for the streaming upload,
`POST /publish` returning `202` with a job id, `GET /registry`). Upgrade
runs (`POST /runs`, `GET /runs/<id>`, `POST /runs/<id>/approve` carrying
the phase and the device credential, `POST /runs/<id>/decline`, `POST
/runs/<id>/cancel`). Administration (users, settings, the audit views).
Role gates the last group to `admin`; the rest accept `operator`.

**Long operations return `202` and a job id, never a held connection.**
That is §3.2's rule as an API contract. The dashboard polls `GET
/jobs/<id>` and `GET /runs/<id>`, which return the row's `status`,
`render_state` or `awaiting_phase`, `heartbeat_at`, and a stalled flag
computed per §7.3. Polling rather than streaming is the same decision
§8.1 made against a PTY, for the same reason.

**One error envelope, speaking §7.3's vocabulary.** Failures carry the
`failure_stage` and `error_summary` already stored on the row rather
than a separately-invented set of UI strings. Where those two are enum
values, the API returns the enum and the frontend renders it, so the
dashboard and the log agree on words. §7.3's rule that the credential
path emits fixed enums and never lets an exception object across that
boundary applies to the response body exactly as it applies to the
column, and for the same reason.

Approval endpoints deserve one explicit note, because they are the
routes that carry a password and reload a fleet: `SameSite=Strict`,
CSRF-checked, `autocomplete="new-password"` on the credential field, and
no request-body logging at the proxy (§9.2). A `409` rather than a
second queued row is the correct answer to the second admin clicking
approve, per §5's uniqueness constraint.

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

That is the right response to *one* of the two things a dirty tree can
mean, and §7.2's reconcile prescribes the opposite response to the
other. Both fire on the same observable signal — the file no longer
matches what the table renders — and one says preserve, the other says
overwrite. `render_state` is the disambiguator, and using it is what
keeps the two sections from cancelling each other out:

- **A `registry_jobs` row sitting at `written` and not `committed`**
  means NetHub itself was interrupted mid-publish. The table wins, the
  reconcile re-renders, and the correction is *committed* so the repair
  lands in history rather than appearing as a file that silently changed.
- **No such row** means something outside NetHub edited the file — an
  operator during an incident, most plausibly, which is the case this
  check exists to protect. The tree is preserved untouched, publishes
  refuse, and NetHub surfaces it as a blocking condition rather than a
  failed job.

The second case then needs a way out, or the check is a trap: as
written, a dirty tree refuses every publish and the only cleaner
described anywhere is a startup reconcile, which quietly makes "restart
the sibling" the documented remedy. So an admin can resolve the
condition explicitly — review the diff in the UI, then either adopt the
outside edit (commit it as-is, attributed) or discard it (reset the tree
and re-render from the table). Either way it is an attributable action
with a record, which is the property the stop-and-say-so rule was
protecting in the first place.

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
  costs a hash comparison. Unequal means a publish was interrupted — as
  distinguished from an outside edit by §7.1's `render_state` test — and
  the table wins: it is the source of truth by definition (§5), and the
  file is a projection of it. The divergence is recorded before it is
  corrected, and the correcting commit carries a distinguishable
  message. "The table wins" is right; doing it silently is how a tamper
  disappears into a diff nobody reads.

**Tamper-evidence currently stops at the file, and it is worth saying
where the line falls.** The clean-tree check and the whole-file re-render
make a modified *file* detectable and correctable. The *table* is
covered by neither, and it is the source of truth for the digest devices
verify against (§3.4) — anyone with write access to the SQLite file,
including a Flask-side RCE, edits `artifacts.sha512` and every
downstream control agrees with them, because every downstream control
derives from that row. Two partial mitigations are worth their cost and
neither closes it: registry commits are signed with a key Flask does not
hold, so the audit copy cannot be rewritten from the web tier alone; and
the reconcile's divergence record gives the change a witness outside the
file. The honest statement is that NetHub detects drift between its
stores and does not detect a consistent lie told across all of them.

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
| `cancelled` | a human asked for it to stop, and it stopped |
| `expired` | sat `queued` past its `deadline_at` and was never claimed |

The last two are deliberately not folded into `abandoned`. One word for
"the machine crashed" and "a person pressed stop" would merge two events
with different follow-ups — a crash is investigated, a cancellation is
not — and it would do so in the column an operator scans first.
Splitting the vocabulary is what keeps `abandoned` diagnostic.

`failure_stage` records *where* it stopped, and `error_summary` carries a
short operator-facing reason. `status` is shared across job kinds —
same machine, same sweep — but `failure_stage` is **per job kind**, or
it collides with the phase names. A publish job stops at `promote`,
`render`, or `commit`; §8.1's phases include `stage` and `verify`, so a
shared vocabulary would produce an upgrade phase row reading
`phase='activate', failure_stage='stage'` that is ambiguous on its face.
Phase jobs use their own vocabulary (`credential`, `connect`, `hostkey`,
`privilege`, `precheck`, `transfer`, `checksum`, `install`, `reload`,
`postcheck`), which also carries more information than the publish set
could. `privilege` is worth calling out: it means the submitter's account
is not at level 15 (§4.3), which is a deployment fault rather than a
device fault, and it surfaces at pre-check rather than part-way through a
wave.

Both fields are written by the code paths that sit closest to the
secrets, and `error_summary` is free text retained for a year (§7.4). A
stray `str(exc)` from a socket handler or a runner exception is
therefore a durable credential leak with no other symptom. The rule is
that the credential path emits fixed enum strings into these two columns
and never lets an exception object cross that boundary; the detail
belongs in the scrubbed log, not in the row.
Without them, a failed job in the dashboard says only that something
went wrong, leaving the operator to go reading `playbook_log_path` by
hand at exactly the moment they need an answer quickly.

Several rules fall out of this:

- **A crashed job doesn't stay `running` forever.** `heartbeat_at` is
  updated during the run, and a sweep at startup moves any `running` job
  from a *different* `runner_instance_id` than the current sibling's to
  `abandoned` (§5). Keying on the instance UUID rather than on a PID is
  what makes that test correct: a PID is reused across container
  restarts and arrives as a meaningless number across PID namespaces. A
  job stuck at `running` is otherwise indistinguishable from a slow one,
  which means nobody investigates it.
- **A sibling that dies and stays dead is swept by nobody.** §9 puts the
  sweep in the sibling so it can never fire while a healthy run
  progresses under another process, which is the right reasoning and
  leaves a gap one level up: if the sibling never comes back, nothing
  ever runs the sweep, jobs sit at `running`, and the dashboard reports
  progress that is not happening. The warning in the bullet above
  reproduces itself exactly. `heartbeat_at` already exists for this and
  no component was specified to read it, so **Flask reads it**: a
  `running` row whose heartbeat is older than a small multiple of the
  heartbeat interval renders as *stalled* in the dashboard, with the
  sibling's own liveness shown beside it. Flask does not change the row
  — writing job state from the web tier would break §9's control-channel
  rule — it reports what the row already says. Sweeping stays the
  sibling's job; noticing does not have to be.
- **An `abandoned` device-touching phase is not silently re-dispatched.**
  Re-running it needs a fresh approval, because §8.1's approval is what
  supplies the credential (§9.1) and what names the human in
  `approved_by`. Auto-retrying would attribute a machine decision to
  whoever last clicked, which is the attribution property in reverse.
  The retry is a new row with an incremented `attempt` (§5), so the
  history shows both.
- **A cancel is a request in a column, not a signal.** §9 makes the job
  row the only control channel, so stopping something is
  `cancel_requested_at` being set by Flask and polled by the sibling
  (§5) rather than a message or a kill. The sibling checks it between
  hosts and at phase boundaries — never mid-device — and the run reaches
  `cancelled`. What that means differs by phase and is §8.1's subject:
  cancelling a stage is safe, cancelling an activation mid-wave leaves
  the fleet split across two versions, and the UI says so before it
  accepts the request. A `queued` job cancels cleanly by never starting.
- **`queued` is bounded too.** §8's wall-clock timeout starts at *run*,
  so a job that never starts has no bound at all. `deadline_at` (§5) is
  set at enqueue rather than at dispatch, and a job that passes it
  without being claimed goes to `expired`. This is the same bound
  §9.1 puts on a held credential, arriving from the queue's side.
- **A failed publish doesn't promote the artifact.** The artifact stays
  `staged`, the previously published row stays published, and the
  registry is never rendered, so a failed job leaves the fleet on the
  last known-good registry instead of in a partial state. Staged bytes
  already written into the staging tree are left in place and collected
  by retention (§7.4). Deleting them on failure would destroy evidence of
  what went wrong, and nothing references them in the meantime.

**All three machines, with the actor on every edge.** The table above
enumerates job status and the rest of the document named the other two
without listing them, which is how `upgrade_runs.state` came to have
exactly one documented value. Naming the actor matters as much as the
states: §9's whole architecture is a statement about which process may
write what, and a transition table that does not say who performs each
edge cannot be checked against it.

*Job status* — `registry_jobs` and `upgrade_phase_jobs` alike:

| from | to | actor |
| --- | --- | --- |
| — | `queued` | Flask, on submit or on approval |
| `queued` | `running` | sibling, conditional claim (§9.2) |
| `queued` | `cancelled` | sibling, seeing the cancel column |
| `queued` | `expired` | sibling, past `deadline_at` |
| `running` | `succeeded` / `failed` | sibling, on EE exit |
| `running` | `timed_out` | sibling, at `deadline_at` |
| `running` | `cancelled` | sibling, between hosts |
| `running` | `abandoned` | sibling's startup sweep, foreign `runner_instance_id` |

Flask writes only the first edge. Everything after dispatch is the
sibling's, which is §9's rule expressed as a table rather than as prose.

*Run state* — `upgrade_runs`:

| from | to | actor |
| --- | --- | --- |
| — | `pre_checking` | Flask, on submit (pre-check needs no gate) |
| `pre_checking` | `awaiting_approval` | sibling, on phase completion; sets `awaiting_phase` |
| `awaiting_approval` | `running` | Flask, on approval (writes the phase job row) |
| `running` | `awaiting_approval` | sibling, at the next gate |
| `running` | `failed` | sibling, on a phase reaching a failed terminal state |
| `awaiting_approval` | `expired` | sweep, past `gate_expires_at` |
| `awaiting_approval` | `cancelled` / `completed` | Flask: an operator cancels, or declines the optional cleanup gate |
| `running` | `cancelled` | sibling, honoring the cancel column |
| `running` | `completed` | sibling, after verify with no cleanup pending |

Declining cleanup is an edge rather than an absence, which is what stops
"awaiting cleanup approval" and "finished, cleanup declined" from being
the same row.

*Per-host state* — `upgrade_run_hosts.state`: `pending` → `precheck_ok`
→ `staged` → `activated` → `verified`, with `failed` reachable from any
of them and `skipped` for a host excluded by a pre-check assertion. The
sibling owns every edge; Flask never writes this table after the run's
host rows are created. The per-phase detail lives in
`upgrade_host_phase_results` (§5), because this column is a cursor.

### 7.4 Retention, and what staleness means here

§2 commits to bounded retention as part of not being an inventory
system, which requires numbers rather than an adjective:

- **Provisioning log**: 90 days. Long enough to answer questions about a
  deployment wave after the fact, short enough that it isn't a device
  history database.
- **Denied-attempt counters** (§4.2): never purged, over the bounded key
  space §4.2 defines. "Never purged" is only affordable because the keys
  are bounded; over an attacker-chosen key space it would be an
  unbounded-growth attack on the database every other subsystem shares.
- **`registry_jobs`**: 365 days, matching the operational question they
  answer ("what did we deploy last year, and when").
- **`upgrade_runs`** and their host and phase rows: 365 days, purged as
  a unit with the run. They answer the same class of question from the
  other end ("what did we install, where, and who approved it"), so
  splitting the horizon between the two would leave half of a publish-
  then-install story on disk and the other half collected.
- **Allowlist entries**: expire on their per-entry TTL (§4.1), not on a
  global schedule.
- **Artifact rows**: retained while referenced. A row that is
  `published`, or that any non-purged job row points at, is never
  collected regardless of age. That's a hard constraint rather than a
  policy knob. Collecting a published artifact would break the registry
  referencing it, and collecting a referenced one would leave the audit
  trail pointing at nothing.
- **Artifact bytes**: a separate axis, because the two were conflated
  and one of them is measured in hundreds of megabytes. The rule above
  is right for a row and wrong for a blob: a three-year-old superseded
  IOS-XE image pinned forever by one surviving job row is half a
  gigabyte of disk held to answer a question the row already answers.
  Bytes for a `superseded` artifact are prunable after their own
  horizon, `bytes_state`/`bytes_pruned_at` (§5) record that it happened,
  and the audit row honestly outlives the file it describes. Bytes for
  `published` and `staged` rows are never pruned; a `staged` row that no
  job references and that has passed its own TTL is collected outright,
  row and bytes together, which is what §7.3's "left in place and
  collected by retention" was promising and had no mechanism for.

**The purge runs in the sibling.** §7.1 lists it among the things that
can concurrently touch the registry tree and never says which process it
lives in, which leaves the `flock` argument naming a contender that
exists nowhere. It belongs with the sibling for the same reason the
startup sweep does: it takes the registry lock, it deletes files on the
store, and neither is work for the process holding the unauthenticated
route (§3.2). It runs on a timer rather than at startup,
so a long-lived deployment collects on schedule rather than on restart.

`device_host_keys` rows are not purged on a schedule. They are a
security control rather than a log, and expiring one silently converts a
fail-closed mismatch back into a first-contact prompt — which is the
exact moment an attacker would want. A row is removed when an operator
retires the address, deliberately and attributably.

Purging a `registry_jobs` or `upgrade_phase_jobs` row also removes its
`playbook_log_path` file in the same operation. A retention helper that
deletes rows and leaves logs behind produces an ever-growing directory
of orphans nothing can attribute — and the phase model multiplies those
files per run, so the coupling matters more here than it did with one
log per publish.

Staleness is a property of the fleet, not of NetHub. The registry
records what a device *should* run. Nothing in NetHub records what it
*does* run, and per §2 nothing should, because that is inventory. An
upgrade run is not the exception it looks like: `upgrade_run_hosts`
records the version a device reported *during that run*, which is a
property of the job and expires with it, and those rows are deliberately
never rolled up into a current-state view per device. That roll-up is
precisely the line between a job record and an inventory. The
consequence is that NetHub cannot tell an operator which devices are
behind, only what the current target is and which jobs ran against it.
Anything resembling fleet drift reporting has to come from the Ansible
side, where facts are gathered.

**Kea is a fourth store, and §7 was written as though there were
three.** The database, the rendered registry, and the git repository are
all handled carefully above; the DHCP reservations §4.1 relies on are
mentioned nowhere in this section. They are derived data with the same
drift problem: a reservation that outlives a consumed or expired
allowlist entry silently re-opens gate 1 — the gate §4.1 introduced
specifically so that phone-home would not be the only one — and it does
so invisibly, because nothing compares the two. The fix is §7.2's own
pattern applied one store over: the reservation set is *rendered whole*
from `allowlist_entries` rather than patched per entry, and reconciled
against Kea's configuration on startup and after every allowlist change.
The table wins there too. This does not make NetHub a DHCP manager any
more than the registry makes it a file server; it makes the reservation
a projection rather than a copy.

## 8. Ansible EE Integration

Reuse a pinned EE image, invoke via the `ansible-runner` Python API,
with a minimal per-job `private_data_dir` and a rendered inventory
rather than the full fleet inventory. **There is one EE dispatch in the
system and it is the upgrade** (§8.1). Publishing used to be a second
one, dispatched against a distribution host that might have been remote;
with NetHub owning the store (§3.3) it is local work the sibling does
directly (§6), so `publish_image.yml` and its per-platform selection no
longer exist. The two operations remain firmly separate — publishing
touches no device, installing is the only thing that does — but they are
now separate in kind rather than two instances of the same mechanism.

Every run carries a wall-clock timeout and is killed on expiry instead
of being allowed to hang indefinitely (`timed_out`, §7.3). An EE run
that never returns is the failure mode a serial queue handles worst,
since one stuck job blocks everything behind it, so the bound matters
more here than it would with a concurrent runner.

**A single job-level bound is the wrong shape for a phase that moves
gigabytes.** Sizing one number for the slowest host on the narrowest
link means every fast host inherits its slack, and a timeout that
generous stops being diagnostic. The stage phase therefore carries a
*per-host* bound derived from `upgrade_run_hosts.file_size` against a
floor transfer rate, so a host that stalls fails in minutes while a
genuinely slow one is allowed the time it needs. NetHub has the size
authoritatively at dispatch (below), so this costs nothing to compute.
The job-level bound remains as the outer backstop. What is retained for
diagnosis is the scrubbed `stdout` and `job_events`, written to
`playbook_log_path` and purged with the job row (§7.4). The
`private_data_dir` itself is *not* kept: it is tmpfs-backed, and its
`env/` and `inventory/` subtrees are destroyed when the execution ends
(§9.2). Retaining the directory wholesale — the obvious reading of "keep
it for diagnosis" — would park `env/extravars` on disk for the row's
full 365 days, which is the opposite of what §4.3 and §4.3.1 claim about
where credentials live.

NetHub always populates `file_size` in the rendered registry entry.
Because ingest measures the file in the same pass that hashes it (§3.4),
the size is authoritative before any device is ever contacted. This
retired `upgrade_iosxe.yml`'s remote-size discovery cascade: the per-run
localhost size cache, the remote stat fallback, and the
`files/remote_image_size.py` helper it shelled out to (referenced by the
playbook but never written). Those existed only to answer a question
NetHub already knows the answer to. The move to SFTP settled it
independently — the distribution account is chrooted under
`ForceCommand internal-sftp` with no shell (§4.3.1), so there is nothing
on the far end to run a remote stat with. `file_size` is now required
rather than discovered, and the playbook asserts it. That promise has a
schema precondition, which §5 now carries: `file_size` and `remote_dir`
are snapshotted onto `upgrade_run_hosts` as well as onto the artifact
row, so a phase renders its inventory from the run's own rows and the
size is present without a lookup that could return a superseded answer.

### 8.1 Upgrade dispatch and the phase split

Publishing an image and installing it are two different dispatches. §6's
day-2 flow ends at a published artifact and a committed registry;
`upgrade_iosxe.yml` is a second job kind, dispatched by the same sibling
against the same pinned EE, that installs a published image onto a set
of devices.

What a user submits is a request document, not an inventory: hosts, one
bundle key per host, and a small closed set of typed knobs. This is
§3.5's rule applied to the upgrade dispatch. NetHub
validates it and compiles the real inventory around it. The two things
it will not take are the reason for that indirection. A user-supplied
playbook is arbitrary code inside the EE with the distribution
credential in reach, which would put an authenticated RCE primitive
beside the unauthenticated route §4 spends its length reasoning about.
Per-run minting (§4.3.1) bounds what that credential is worth afterwards;
it does nothing about what a playbook could do with it while the phase is
still running, so the rule stands unchanged. A
user-supplied inventory carries `image_registry`, which would let a
request name any filename against any SHA-512 and bypass the `artifacts`
table entirely, breaking §3.4's "hashed once at ingest, consumed three
times" at the third consumption. Connection vars are withheld for a
third reason: `ansible_user` is the submitter's own device identity, and
an identity the submitter can type is not evidence of anything.

**`ansible_host` is the one connection var a request does supply, and
saying "no connection vars" without that carve-out is wrong in a way
that matters.** A request has to name the devices it targets; an address
is not an identity claim and withholding it would leave nothing to
submit. But the field is the whole attack in §4.3's credential model —
naming a machine you control is enough to be handed someone's device
password — so it is accepted under two constraints rather than trusted.
It is validated against a deployment-level target CIDR, which is a
network boundary rather than an inventory and so does not reopen §2's
non-goal. And it is authenticated on connection against
`device_host_keys` (§4.3), which is what turns "an address NetHub will
dial" into "a device NetHub has met before". The distinction to hold on
to is between vars that assert *who someone is* — `ansible_user`,
credentials, `become` settings — which a submitter never supplies, and
the address of the thing being acted on, which they must.

**The playbook's `pause` prompts assume a terminal that no longer
exists.** Run by hand, `upgrade_iosxe.yml` stops four times to ask
permission. Dispatched out-of-band by the sibling, there is no stdin to
answer on. Streaming a PTY to the browser would buy the interactivity
back at the cost of a general-purpose IPC channel between Flask and the
sibling, which §9 rules out. §9.1 does open a second channel, but a
deliberately narrow one carrying secrets in one direction and no control
at all; a PTY stream fits through neither it nor the job row. So the
interactivity is removed rather than transported. The run is split into
phases, each dispatched as its own EE execution, and the confirmations
become approval gates in the UI between them:

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
  `serial` is protecting, and it makes staging separable in time — images
  copied on Monday, activated in Saturday's window.

  It is also strictly faster *for the devices*, which is not the same as
  free. With NetHub the only distribution host (§3.3), an uncapped stage
  phase means every targeted device fetching a gigabyte from one box at
  once, over whatever link separates them from it. Stage therefore takes
  a concurrency cap — a deployment setting (§5), applied as `throttle` on
  the copy task. Narrowing `serial` to activation is still the right
  call; the cap is what keeps the resulting parallelism from moving the
  bottleneck onto the store.
- **Almost no state has to cross a phase boundary.** Facts flow through
  one play today via `set_fact`, which separate executions break. But
  filename, `sha512`, `remote_dir` and `version` come from the rendered
  inventory, and `file_size` from the registry NetHub always populates
  (above). What is left is device state — current version, free space —
  which is re-gathered per phase and *should* be: a pre-check from three
  days ago must not authorize today's reload. The split is cheap
  precisely because the registry already carries the expensive facts.
  The distribution credential deliberately does not cross either: it is
  minted for the stage execution and expired with it (§4.3.1), so the
  Monday-to-Saturday gap holds no live secret.
- **Activation re-checks what staging established.** Between the two
  phases a device may have been upgraded by hand or had its flash
  cleaned, so activation reopens with the "not already on target version"
  assertion and a confirmation that the image is still present and still
  verifies. This is §7.4's staleness question in its concrete form: the
  gap between phases is exactly the window in which a prior phase's
  findings expire.

  "Still verifies" means **re-running `verify /sha512`**, not stat-ing
  the file. A file of the right name and size is not the file staging
  checked, and the gap is measured in days. This is also the third
  consumption §3.4 promises for the ingest digest, so a phase split that
  quietly downgraded it to a presence test would break that invariant at
  the one point it is supposed to bind.

  **A host that fails either check does not proceed to `install add`.**
  That has to be said explicitly, because the committed playbook does not
  do it: `upgrade_iosxe.yml` wraps the copy, the `verify /sha512` and the
  presence assertion in one `block:` whose `rescue:` prints a message and
  sets `upgrade_stage_failed`, a fact read only by the summary play at
  the end. Nothing between the rescue and `install add … activate commit
  prompt-level none` consults it, and the install's only condition is a
  version comparison — so a host whose digest did not match is installed
  anyway, with the failure recorded as a colour in a summary. Under the
  phase model, per-host outcome is a row (§5) and the next phase reads
  it, which is what makes the gate real rather than advisory. Until that
  model exists, the playbook's rescue must end the host outright rather
  than set a flag. The same rescue also swallows a benign case that the
  phase split makes *normal*: at activation time the image is already in
  flash, so the presence assertion fails into rescue and a staged host
  skips verification on its way to installing.
- **Two approvals are not two runs, and the row is where that is
  refused.** The serialization guarantee below is scoped to *execution*:
  it stops two EE processes overlapping, not two rows being created. Two
  admins on the approval screen both clicking "approve: reload" write two
  phase jobs, and the serial queue then runs them one after the other —
  the fleet reloads twice. `UNIQUE(run_id, phase, attempt)` (§5) is what
  makes the second click a refusal instead of a queue entry. An approval
  being a row rather than a keystroke is the reason this works: a
  keystroke has nothing to collide with.
- **Cancelling means different things at different phases, and the UI
  says which.** `cancel_requested_at` (§5) is polled by the sibling
  between hosts. Cancelling a `queued` phase stops it before it starts.
  Cancelling pre-check, stage, or verify is safe: the first two are
  non-disruptive by construction and the third is read-only, so the run
  parks with some hosts staged and some not, which the next stage
  approval reconciles. Cancelling an activation mid-wave is *not* safe
  and is not presented as though it were — the devices already reloaded
  are on the new version and the rest are not, and the operator is
  choosing a split fleet over finishing the wave. That is sometimes the
  right call, which is why the button exists; the confirmation names the
  consequence rather than asking twice.
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

- Podman Quadlet units for the Flask app, the sibling job runner, and the
  distribution container, which serves the day-2 image subtree over
  SSH/SFTP and is dual-purposed to serve the day-0 subtree over plain
  HTTP, with no separate bootstrap container. The store is local to this
  host and is NetHub's own (§3.3); the two daemons are pointed at
  different subtrees of it. The HTTP daemon decides nothing: it serves one
  fixed path holding the generic script, plus a `mint/` subtree of
  one-shot symlinks Flask writes and reaps, and authorization for day-0
  lives in the phone-home handler. The SFTP side answers only to the
  service account §4.3.1 defines — `ForceCommand internal-sftp`,
  `ChrootDirectory` over the image subtree, no shell, read-only — whose
  password the sibling sets per phase execution and clears when it ends. Everything
  runs on one host, in separate units, under one rootless Podman user.
- **DHCP integration is the deliberate exception**: it runs natively as
  its own service rather than as a unit, because it binds a privileged
  broadcast-facing port on the provisioning VLAN and is the one
  component whose job is to be reachable by a device that has no
  configuration yet. Containerizing it would buy uniformity and cost the
  straightforward host networking that role depends on. It touches
  neither the job queue nor the credential path in §9.1, so it is
  outside that section's trust argument entirely.
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
  Flask app communicate through the database, so the job row *is* the
  control channel; §9.1 covers the single narrow exception, which
  carries secrets and no control. The sibling writes `status`,
  `heartbeat_at`, `failure_stage`, and `error_summary` (§7.3); Flask
  only reads them. A sibling that dies mid-run leaves a stale `running`
  row, which is exactly the case §7.3's startup sweep exists to resolve.
  That sweep runs in the sibling rather than in Flask so it can never
  fire while a healthy run is in progress under another process.

### 9.1 The control channel and the secret channel

§9 settles that the sibling exists. It leaves unsaid how the two halves
actually exchange anything, and the document has been relying on two
statements that cannot both be true: §4.3 says the device credential is
"never persisted", and §9 says the job row is the only channel between
Flask and the sibling. The credential arrives at a Flask request handler
and is needed by an `ansible-runner` call inside the sibling. Either it
travels through the database — which is persistence — or there is a
second channel. This section picks the second and bounds it; §9.2
covers how the channel is built and what keeps it narrow.

**First, this is a same-host question.** Every part of NetHub runs on
one host as separate Quadlet units. That is a deployment constraint
rather than a conclusion argued here, and §7 would independently force
it in any case: the job store is SQLite, whose locking is documented as
unreliable on network filesystems, and *both* processes write it — Flask
owns job rows, approvals, `users` and the provisioning log, not just the
sibling. Distributing the components would mean §7 rebuilt around a
different database, for no benefit at this scale.

Taking co-location as given is what makes the rest of this section
cheap. A same-host channel has the kernel available as a trust anchor,
so the question is which local mechanism to use rather than what to
authenticate a remote peer with. *Separate* units matter as much as the
shared host: distinct PID namespaces are what prevent same-uid `ptrace`
and `/proc/<pid>/mem` access between Flask and the sibling, so the two
must not be placed in a shared Quadlet `Pod=` — that would remove the
isolation this section depends on without changing anything visible.

**The split is by durability, not by convenience.** The job row is
durable, queryable, survives both processes restarting, and *is* the
audit record — §8.1's "an approval is a row rather than a keystroke"
depends on exactly those properties. They are the right properties for
state and precisely the wrong ones for a secret, which wants no
durability, no queryability, and no record. So the two kinds of
information get two channels, and the boundary is stated as a rule
rather than left to judgment:

- **Control plane — the job row.** Status, `heartbeat_at`,
  `failure_stage`, `error_summary`, phase transitions, approvals, and
  every fact anyone might later need to ask a question about. Unchanged
  from §7.3 and §9.
- **Secret plane — a Unix domain socket.** Per-execution credentials
  only: the submitter's device password, and nothing else that is not a
  credential.

Nothing that crosses the socket is ever written to the database, the
`private_data_dir`, or a log. Nothing that belongs in the row is allowed
onto the socket because the socket is more convenient. A new piece of
information that is not a secret goes in the row.

**The sibling initiates, and pulls on demand.** The sibling, having
claimed a `queued` row from the database, connects to Flask and asks for
the credential belonging to that job. Flask answers or refuses; it never
connects, never enqueues, never triggers a dispatch.

It is tempting to justify this as "a Flask-to-sibling socket would be
the Podman socket in miniature," and that is too quick — a deposit-only
socket carrying one message type could not make the sibling *act*
either. The sharper reasons are narrower and both hold. Depositing means
the sibling holds secrets for executions it has not started, which is
precisely the window this design exists to shrink. And a deposit
endpoint is something a compromised Flask can flood at will, while a
pull endpoint is only ever consulted at a moment the sibling chose.

That settles the shape. What the channel is made of, what it refuses to
carry, and what has to be true of the two processes either side of it
are §9.2's subject.

**What follows for the operator, stated plainly.** Because the
credential now lives no longer than one phase execution (§4.3), it is
collected at each approval rather than once at submit: submitting
collects for pre-check, approving the copy collects for stage, approving
the reload collects for activate and the verify that follows it, and
approving cleanup collects for cleanup. That is up to four password
entries across a full run instead of one. The compensation is that "no
device credential is ever at rest, anywhere, at any point" becomes a
true statement rather than an aspiration, and the human is already at
the browser to click the gate. Buying it back with an encrypted column
would mean a key readable by Flask, which is the PSK problem again.

The ergonomic cost is not the only cost, and the honest accounting says
so: training operators to type an enable-capable AAA password into a web
form four times a run makes the approval gate a high-value phishing
target. That is not a reason to reverse the decision — the alternative
is a durable secret, which is worse — but it argues for an approval
screen that is hard to clone convincingly, and for §4.3's pre-dispatch
credential validation being visible to the operator, so that a form
which silently accepts anything is recognisably not the real one.

**"For one phase execution" is bounded below by the queue, so the hold
has a TTL.** The window the credential actually sits in Flask's memory
runs from approval to the sibling picking the row up, and §3.2 and §7.1
run one EE execution at a time — a phase approved while another run's
fifty-host activation wave grinds along can sit `queued` for hours. That
is the same objection this section raised against the per-run lifetime,
smaller but not different in kind. So a held credential expires on a
bounded TTL measured in minutes; on expiry the phase fails
`credential` and the operator re-approves. Queue depth is shown at the
gate, so an approval made into a long queue is an informed one. It is
worth saying plainly that the longest phase — the serial activation wave
— can itself run for hours, and the credential is live for its
duration; "one phase execution" is an honest bound, not a short one.

**The distribution password never crosses at all.** §4.3.1's minted
credential is generated by the sibling, because the sibling is the
component that talks to the distribution host and the one that knows when
the execution has reached a terminal state and the credential should be
expired. Flask never sees it, so it is one fewer thing on either channel.
The socket carries the submitter's device credential and nothing else.

### 9.2 The socket, and what keeps it narrow

§9.1 settles that a second channel exists, that the sibling initiates
it, and that it carries credentials and nothing else. This section is
the construction: who is allowed to open it, what it refuses to carry,
and what has to be true of the processes at either end. Most of it
exists because the obvious implementation of each point is subtly wrong
in a way that still appears to work.

Four constraints keep the channel narrow:

- **The mount is the authenticator; `SO_PEERCRED` is a sanity check.**
  This is worth stating precisely, because the obvious formulation is
  wrong. `SO_PEERCRED` yields a kernel-attested uid/gid/pid that no
  caller can forge — but forgery was never the threat. *Discrimination*
  is, and since every unit runs under one rootless user, a uid check
  tells Flask only "the peer shares my uid", which anything a Flask
  compromise spawns also satisfies. What actually decides who can open
  the socket is the filesystem: the socket volume is mounted into
  exactly two units and no others, mode 0600. That is a real control and
  it is the same reasoning §4 uses about VLAN isolation — but it is a
  *mount* control, and the document should not credit it to a syscall.
  A deployment that can carry two rootless users gets the stronger
  version, where the socket is owned by Flask's uid with a shared group
  and `peercred.uid == SIBLING_UID` genuinely discriminates; §10.
  Authorizing on the *pid* field is wrong in any case: it is a snapshot,
  so any later `/proc/<pid>/…` lookup is a pid-reuse race, and across
  separate PID namespaces it arrives as `0`.
- **There is no TLS and no pre-shared key, because both would be
  redundant rather than because a key would be an at-rest secret.** The
  weaker argument is tempting and does not survive contact: Flask
  already reads a durable `SECRET_KEY` from its environment, so "Flask
  holds nothing at rest" was never true. The real reason is that the
  kernel already supplies confidentiality, integrity, and an unspoofable
  peer identity on an `AF_UNIX` socket, and the filesystem already gates
  who may open it. A PSK would authenticate nothing the mount does not.
  Getting this right matters, because the weak version of the argument
  is what lets the previous bullet slide by unexamined.
- **A credential is released once, for a job already `running` — an
  interlock, not an authorization check.** The sibling sets `running`
  and then asks Flask to verify `running`, so the precondition is
  controlled by the requester and constrains a compromised sibling not
  at all. That is acceptable, since a sibling holding Podman already
  owns the host. What the check does buy is real: a stray or duplicated
  request cannot drain credentials for jobs nobody started, and one-shot
  release makes replay worthless. Two details carry weight. The
  in-memory store is keyed by `upgrade_phase_jobs.id`, never by
  `run_id`, and Flask cross-checks the phase job's `approved_by` against
  the identity that supplied the credential — keying by run would
  eventually hand one person's password to another person's approved
  execution against an address that person chose. And the queue claim is
  a conditional update (`SET status='running' WHERE id=? AND
  status='queued'`, requiring one changed row), because nothing enforces
  that only one sibling is running and a read-then-write double-claims
  under WAL.
- **A few fixed message types, in both directions.** The request surface
  is small by construction, and the *response* surface needs saying too,
  because the sibling now parses bytes that a compromised Flask chose,
  on a path Flask cannot be prevented from answering. Fixed framing,
  hard byte caps, read deadlines, no `pickle` and no `yaml.load`, and
  the returned credential treated as opaque bytes validated against a
  character allowlist before it goes anywhere near a variable or a
  command string. Without that, "a Flask RCE gains nothing beyond
  forging a queued row" is not earned — it would gain a guaranteed-
  delivery path into the privileged process's parser. There is no job
  control on this channel, no status, no log streaming, and no PTY;
  §8.1 removed the interactivity rather than transporting it, and this
  does not reopen it.

**systemd owns the socket, not either container.** The usual
`unlink(path); bind(path)` idiom is a squatting primitive for anything
that can write the directory, and under one shared uid that is anything
that can see the volume — a squatter would receive the sibling's request
and could feed it arbitrary bytes. So the listening socket is a
`.socket` unit with `ListenStream=`, and systemd passes the fd to Flask.
Neither container calls `bind()`, the path survives a Flask restart
without a stale inode, the sibling cannot connect before the socket
exists, and path substitution has nothing to substitute. Three rootless-
Podman details belong with it, because each one surfaces as an
intermittent `failure_stage: credential` rather than as an obvious
error: both units need identical, explicit userns mappings, or the peer
uid arrives as the overflow value; a volume shared between two
containers needs `:z` and not `:Z`; and an abstract-namespace socket is
the wrong choice specifically because it carries no permission bits at
all.

**Flask is one worker, and this channel depends on that.** §3.2 argues
that nothing may stall the one Flask process, because the
unauthenticated phone-home route has to stay responsive for a device
that cannot wait. Adding a listening socket to that process is exactly
the kind of thing §3.2 legislated against, so it comes with conditions:
the socket is served on its own thread with a small backlog, hard read
and write deadlines at both ends, and a cap on concurrent connections,
and the sibling treats a connect or read timeout as
`failure_stage: credential` rather than retrying indefinitely. The
constraint that is easiest to violate by accident is `gunicorn`'s worker
count. **This design requires exactly one worker.** With two, a
credential submitted to worker A is invisible to worker B and the
listener lands in whichever process systemd handed the fd to. It fails
closed, which is the right direction, but it fails closed
intermittently and for a reason nothing in the error message suggests.

**What "in memory" does and does not protect.** §9.1 trades on memory
being a safer place than disk, which is true and is not the same as
safe. The exposures that follow, and what each one requires:

- **A core dump writes the heap to disk at exactly the wrong moment.** A
  Flask crash with `systemd-coredump` active deposits the plaintext in
  `/var/lib/systemd/coredump`. The unit sets `LimitCORE=0`, and the
  process sets `PR_SET_DUMPABLE` to 0 — which also denies same-uid
  `ptrace` and `/proc/<pid>/mem`, the attack the shared uid would
  otherwise leave open.
- **Debug mode turns an exception into a credential disclosure.**
  Werkzeug's interactive debugger renders frame locals into an HTTP
  response, and the reloader runs two processes. `DEBUG` must be off
  before the credential path exists. `config.py` currently sets it
  `True`, which is correct for the bootstrap the repo is in today and is
  a release blocker for this feature.
- **A Python `str` cannot be erased.** "Flask drops its copy on handoff"
  is achievable as dereference, not as erasure: the bytes persist in the
  heap until reused, and the form parser has already made copies. Hold
  it in a `bytearray` that can be zeroed, and state the residual rather
  than claiming the stronger property.
- **The heap is swappable**, so either swap is disabled on the host or
  the exposure is accepted explicitly.
- **The approval form puts the password in a POST body**, so the reverse
  proxy must not log request bodies, and the form needs CSRF protection
  and `autocomplete="new-password"`.

**Failure behaviour is fail-closed.** If the socket is unavailable, or
Flask has no credential for the job, the phase fails with
`failure_stage: credential` and the run parks. It never falls back to
reading a credential from disk, from the row, or from a previous phase.
A sibling that restarts mid-run loses the in-flight credential along
with the EE process, and §7.3's sweep moves the execution to
`abandoned` — which is the correct outcome, since re-approving is how
the next attempt gets a credential.

## 10. Open Questions

- **How the distribution account's password is set and cleared
  (§4.3.1).** Choosing SFTP settled the account's *shape*: a `Match`
  block with `ForceCommand internal-sftp`, `ChrootDirectory` over the
  published image subtree, no shell, read-only. What remains is the
  credential lifecycle — rewriting the system account's password per
  phase execution, versus a purpose-built SSH service the sibling runs
  and holds the credential inside. The second is tighter (it could scope
  a credential to one file for one execution) and more code. Worth
  deciding before the container is built; a migration later is not
  cheap.
- **Where NetHub's rendered `known_hosts` actually takes effect (§4.3,
  §9.2).** §4.3 requires the fail-closed host-key check on the
  `network_cli` session, and the connection plugins do not all read the
  file from the same place — some hardcode `~/.ssh/known_hosts` rather
  than honouring a path inside the `private_data_dir`. Which file gates
  the session has to be established by testing rather than assumed, or
  the pin §4.3 leans on may not be the one in force.
- **The two SFTP strings the playbook guesses.** The copy task carries
  over `Password:` as the prompt and "N bytes copied" as the completion
  condition from its SCP form. Neither has been confirmed against a real
  device on `copy sftp://`, and the `wait_for` is what turns a truncated
  transfer into a failure rather than a silent success. Verify both
  before the transfer is trusted.
- **Whether SFTP is slower than SCP on the links that matter.** SFTP is a
  windowed request/response protocol, so on a high-latency path it can
  underperform SCP unless the window is large; `ip ssh bulk-mode`
  (default from 17.10) raises the SSH transport window for both. This
  interacts with §8's per-host timeout and §8.1's stage concurrency cap,
  so it is a measurement those two settings depend on rather than a
  curiosity.
- **Whether the device verifies the distribution host's key at all.**
  The device-to-store leg is authenticated in one direction: the device
  proves nothing about who it is talking to unless its SSH client is
  configured with the server's key. §4.3.1 prices this correctly — what
  an impostor collects is a near-worthless credential, and a substituted
  image is caught by `verify /sha512` against a digest that arrived over
  the pinned session — so nothing depends on closing it. It is recorded
  because the reasoning should be revisited if either of those two
  properties ever changes.
- **When distributed distribution comes back (§2, §3.3).** NetHub is the
  only distribution host and multi-site mirrors are out of scope. The
  constraint that bites first is a branch site on a narrow link, where
  every device fetches its own copy across the WAN. §3.3 is the seam, and
  reopening it means reopening §4.3.1: a mirror NetHub does not
  administer is one whose account model it cannot constrain, which is
  what made the credential cheap to price in the first place.
- **Whether Flask and the sibling should run as two rootless users
  rather than one (§9.2).** Under a single user, the socket's mount
  scope is the only thing distinguishing callers and `SO_PEERCRED`
  cannot discriminate. Two users make the peer check mean something, at
  the cost of a second rootless Podman stack and more awkward sharing of
  the volumes both need. Worth deciding before the socket is built,
  since it is cheap now and a migration later.
- **Ansible Vault has nothing left to hold, and should be removed
  outright.** It existed for the distribution password, which §4.3.1
  deleted; the device credential is injected in memory (§9.1). The
  vaulted file and the vault password beside it are a secret-management
  surface with no remaining secret, and carrying them would leave a key
  disposal problem this document never solved for no benefit. Listed
  here rather than in §5 because the removal touches
  `example_inventory/` and the EE's expectations, not the schema.
- **Whether the tamper gap on the `artifacts` table is worth closing
  (§7.2).** NetHub detects drift between its stores and does not detect
  a consistent lie told across all of them, because the digest devices
  verify against comes from a row anyone with the SQLite file can edit.
  Signed registry commits and the divergence record narrow it; an
  append-only hash chain over `artifacts` writes, or a digest
  countersigned by the sibling at publish, would narrow it further at a
  cost this scale may not justify. Worth deciding deliberately rather
  than by omission.
- **The two day-0 numbers in §3.2 are asserted, not measured.** A
  two-second p99 and a fifteen-minute device backoff are what the
  architecture is calibrated against, and neither has been checked
  against a real IOS-XE ZTP client on a loaded upload path. They are
  written down so that the first measurement contradicts something.
- New repository, built clean for the backend, reusing applicable
  existing pieces (e.g. Kea configuration) rather than forking the repo
  wholesale. The frontend is the exception: it's carried over from
  Drawbridge as-is and extended in place (see §3.1), not rebuilt.
