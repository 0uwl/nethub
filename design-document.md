# NetHub — Design Document

> NOTE: This document is the target architecture, not a description of
> current code. The Software Lifecycle module's device-facing half (day-2:
> onboarding images, publishing, and driving upgrades) is implemented —
> `nethub/devices/` drives real hardware over Netmiko, replacing the
> Ansible/EE design this document originally specified for that half, and
> `nethub/sibling.py` replaces `ansible-runner` dispatch with the same
> out-of-process/never-in-Flask discipline this document argues for. The
> alpha's actual deviations from what follows — local username/password
> auth instead of OIDC, no `sessions` table, no `settings`/`settings_audit`,
> no roles — are tracked in `CLAUDE.md`, which is the authoritative
> account of what exists today. Provisioning (day-0) remains entirely
> unimplemented. Sections below describing unbuilt pieces (day-0, OIDC,
> server-side sessions, the `settings` table, role-based access, a
> git-committed registry) are kept as forward-looking design, argued at
> the same level of detail as the parts that now exist — verify against
> `CLAUDE.md` before assuming any specific claim below is already true.

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
  images into NetHub's own artifact store, publishing them, and
  triggering fleet upgrades by driving each device directly over
  Netmiko, all through the same admin UI and backend.

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
  replacement). NetHub dispatches a closed set of code it ships in
  `nethub/devices/` against a request it validates and compiles itself.
  It does not run user-supplied playbooks or scripts and does not accept
  user-supplied inventories, so the set of things it can do to a device
  is fixed at build time rather than at submit time (§8.1).
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
- No image transformation/repackaging.
- **Not a distributed content-delivery system.** NetHub is the sole
  source of day-2 image bytes (§3.3, §3.4); there is no support for
  image mirrors sited near remote fleets, so every byte of an upgrade
  crosses whatever link separates the NetHub host from the device.
  This is a deliberate narrowing rather than an omission: owning the
  store outright means the push (§4.3.1) reads a subtree NetHub
  administers, so provenance is never a question. It is also a real
  limitation for multi-site deployments, and §10 records where it would
  come back.
- Not multi-vendor on day one. See Vendor Scope.

## 2.1 Vendor Scope

Only Cisco IOS-XE is supported initially. The Software Lifecycle module
carries an explicit `platform` field through its schema and job model
(even with one valid value today), so a second platform is an addition to
`nethub/devices/` — new facts-parsing, transfer, and install modules
selected by that field — rather than a rework of the schema or the phase
model around it. Publishing does not vary by platform at all — with
NetHub owning the store (§3.3) it is a local file operation, and a file is
a file.

Things a second platform inherits, worth checking early: §4.3.1's push
assumes the device can run a temporary file-transfer server that NetHub
can enable, push to, and disable again from the same authenticated
session it already holds; and §4.3 assumes a level-15-equivalent
authorization exists at login. Neither is universal. A platform that can
only fetch its own image would need the device-side pull transport §10
records as possible future work.

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
  verification, bundle-key validation, per-host-phase dispatch to
  `nethub/devices/` (facts, connection, transfer, install — driven
  directly over Netmiko, with no intermediate runner or container),
  registry/publish update under a lock, job audit logging.

Every Software Lifecycle route requires an authenticated admin session.
The phone-home route stays the system's sole unauthenticated entry
point, scoped narrowly to its existing contract.

Runs as a single *worker* — one OS process, threaded within it — with no
multi-process/shared-nothing concurrency handling in the Flask app
itself. The traffic volume here (occasional device phone-homes,
occasional admin-triggered uploads) doesn't warrant that complexity, and
the heavy lifting for both modules happens in a separate sibling process
the backend dispatches to rather than in the Flask process itself.
One worker used to be a correctness requirement, because the credential
was held in that worker's memory; since PLAN.md WS-7 moved it into the
job row as ciphertext (§9.1), it is a tuning choice that suits one SQLite
file (§9.2). Threads within that process are how the long operations
below are kept from blocking each other.

That justification only holds if device work never blocks a request
handler. A single-device stage phase alone runs for minutes at measured
push throughput (~1.4 MB/s, see §8), and an `install add … activate
commit` runs 600+ seconds before the device even reloads. A synchronous
Netmiko session opened inside a route would stall the entire process for
its duration, including the day-0 phone-home endpoint, which has to stay
responsive because a device booting on the provisioning VLAN can't wait
or retry indefinitely. Requests that would trigger device work therefore
return as soon as the job row is written: the phase execution is
dispatched out-of-band by the sibling process that owns the queue (§9),
and the dashboard reports progress by polling the job row's `status`
instead of holding a request open. Execution is serialized, one phase
execution at a time. That is sufficient at this scale, and it is what
makes the registry write path in §7 tractable.

The same argument applies to ingest, and it is easy to miss because
ingest never dispatches to the sibling at all — it runs synchronously in
the request handler that received the upload. A 1.2 GB image upload
occupies that handler for the length of the transfer plus a full SHA-512
pass, which is longer than most publish jobs spend in the sibling. On a
single-threaded process the phone-home route would return nothing for
that whole window — the exact failure the paragraph above legislates
against, arriving through the other half of the same design. So the
process is single-*worker* and multi-*threaded*: `gunicorn` with
`--workers 1` and a threaded worker class. The worker count is a tuning
choice (§9.2); the thread count is what keeps a long upload from being a
global stall, and that part is not optional. The digest is computed incrementally over the chunks as they are
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
horizontally (§9.1: everything runs on one host).

### 3.3 Distribution — day-0 vs. day-2
Both days draw bytes from the **same store on the NetHub host** (see
§3.4). They differ in how those bytes reach a device, and in who is
trusted to ask:

- **Day-0**: ZTP script and image/config delivered to a new device as
  part of the phone-home flow, over plain HTTP (see §4 for the transport
  decision). No separate dedicated bootstrap container as it was in
  Drawbridge. What the HTTP daemon serves and what NetHub decides are
  two different things — see "the day-0 fetch sequence" below.
- **Day-2**: NetHub **pushes** images from the store to an
  already-enrolled device over SCP (§4.3.1) — Netmiko's
  `CiscoIosFileTransfer`, Paramiko underneath — under the same device
  credential the run already holds. No second credential is minted or
  held. Push is the only day-2 transport; a device-side pull is recorded
  in §10 as possible future work.

**NetHub is the sole source of the bytes, and that is not modular.**
Earlier revisions let the image *source* be either a local bundled
container or an existing remote host, chosen by configuration. That
flexibility looked cheaper than it was: a source NetHub does not
administer is one it cannot guarantee holds the artifact NetHub actually
published. Owning the store outright turns that into ordinary
implementation, and it means the transfer is never a trust question:
the sibling reads the published subtree read-only (§3.5) and pushes
straight from it over the connection the run already holds. "Distribution
host" names only the box the bytes are read from. There is no second
store and no day-2 daemon a device dials.

The cost is stated rather than hidden. A deployment with branch sites
cannot put a copy of the image near its devices; every push crosses
whatever link separates NetHub from the device. That is a real limitation
for multi-site fleets, it is out of scope for now consistent with §1's
stated scale, and §10 records where it would come back.

The HTTP daemon does not expose the whole store. "One store" means one
ingest path and one `artifacts` table, which is not the same as one
docroot: the daemon serves only the day-0 subtree (artifacts of `kind`
script/config, and only in the narrow arrangement the next section
specifies), with directory indexing disabled. Day-2 images are never
served by the **HTTP** daemon — they live under a subtree the HTTP
docroot never includes, read directly by the sibling process itself
(§3.5), and reachable by no other component. Without that split the
HTTP adapter would be an unauthenticated read path over the image store,
bypassable by anyone who can guess a filename. The point of the shared
store is to make the day-0/day-2 split cheap; a shared docroot would
make that split meaningless.

What must not happen, if a device-side pull is ever built (§10), is the
shortcut of serving day-2 images from the day-0 HTTP docroot to save
running a second service: that would put the image store behind a
guessable filename on an unauthenticated read path, which is precisely
the arrangement this paragraph exists to forbid. If serving images over
HTTP is ever revisited it needs the `mint/` capability-path treatment
day-0 configs get, not a shared docroot.

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
   paths minted for that provisioning attempt and recorded on the
   allowlist row. Minting writes a symlink into a `mint/` subtree of the
   day-0 docroot pointing at the artifact blob, which is §3.5's
   render-don't-accept rule applied to a filesystem — the subtree is a
   projection of live allowlist rows, reaped when the provisioning
   window closes (§4.1), on TTL expiry, and on the startup reconcile.
   **Not on first fetch.** §4.1 is explicit that the *window*, not the
   individual `GET`, is the one-shot unit — a real ZTP flow is several
   fetches, and reaping after the first would turn a retried or partial
   fetch into a failure. A minted path is therefore a bearer capability
   for the life of its window, not a single-use token; that trade is
   made deliberately, not left ambiguous between this section and §4.1.
4. The device fetches those paths from the same HTTP daemon, which is
   now serving an unguessable path it was handed rather than deciding
   who may read what.

The daemon stays a daemon; the authorization stays in NetHub. The minted
path is a capability with the same TTL/one-shot-window shape §4.1
already defines for the allowlist entry, so this is a reuse rather than
a fourth pattern. Known and unknown serials receive the same response
shape at step 3 (§4.1): an unknown serial is answered with syntactically
identical URLs that were never minted and resolve to nothing.

**That equivalence has to survive the next hop, or it's decorative.** An
attacker who POSTs a candidate serial and then `GET`s the URL step 3
returned learns nothing from step 3 alone — but the daemon's own
response to that `GET` is a second distinguisher: a minted path resolves
(`200`, the blob), an unminted one does not (`404`). That is the same
enumeration oracle §4.1 rules out at the phone-home route, reappearing
one hop later on a component that — by §3.3's own design — consults
nothing, counts nothing, and writes no `provisioning_log` row. Closing
step 3 without closing this makes the daemon the place an attacker
actually probes. The `mint/` location therefore answers *any*
well-formed request under it with the same shape: a resolved path
serves the blob, an unresolved one serves a fixed-size decoy body at
`200` rather than a `404` — a static rewrite rule at the daemon
(`error_page 404 =200 /mint/.decoy;` or equivalent), not application
logic, so the "the daemon decides nothing" property (§3.3, §9) still
holds. The daemon's access log is routed into §4.2's per-source counters
for the same reason: it is part of the authorization-relevant surface
even though it makes no authorization decision itself.

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
   └─ egress B (day-2): admin-initiated, dispatched to the sibling, credentialed,
                        NetHub pushes → device over SCP
```

There is one artifact record. A day-0 config, the ZTP script itself, and
a day-2 IOS-XE image are the same shape (blob, SHA-512, size, platform,
uploader, timestamp), differing only by a `kind` discriminator. One
upload path, one hash function, one table (§5). The protocol split is an
access-method detail in the egress adapters rather than a division into
two subsystems.

The hash is computed once and consumed three times. Ingest computes the
SHA-512; it is then read by (a) day-0 payload verification, (b) the
snapshot a run takes onto `upgrade_run_hosts` at submit, and (c) the
device's own `verify /sha512` step
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

That trade was tested rather than assumed. Swapping the system's single
algorithm to MD5 was evaluated on 2026-09-09 — motivated by Netmiko
shipping MD5 verification helpers, and by MD5 being measurably cheaper
on a switch CPU — and rejected on three findings. The first disposes of
the motivation: Netmiko's `compare_md5` compares the file on NetHub's
own mount against the device, whereas the check this design needs
compares the digest recorded at ingest against the device. §7.2's rule
that the table wins over the file names exactly the case that
comparison would miss, so the compare stays NetHub's own code whichever
algorithm is chosen and the swap saves nothing. The second bounds the
cost of keeping SHA-512: measured on a Catalyst 9200CX, a 408 MB
package hashes in 18.5s under `verify /md5` and 33.9s under
`verify /sha512`, so a 1.2 GB image costs roughly 45 extra seconds per
verification and about a minute and a half per device across an
upgrade's two verifications — against an activate-and-reload measured
in minutes.

The third finding is the one that decides it, and it is narrower than
the argument usually made. Substituting bytes to match a digest already
recorded is a preimage attack, and MD5's preimage resistance is not
practically broken, so "MD5 is broken" is not on its own a reason here.
Chosen-prefix collisions are, and they have been practical since 2019:
they give an attacker who supplies an image to an operator a working
path — hand over a benign build prepared to collide with a malicious
one, let NetHub ingest and record the benign one, substitute later. Two
places in this design have nothing standing behind the digest if that
succeeds. Day-0 names payload hash verification as one of three
compensations for its deliberate use of plain HTTP (§4), and a
device-side pull transport, if one is ever built (§10), would leave the
digest as the only control against a redirected session, because the
device does not verify the host it fetches from. Large binaries with
unused space are good collision carriers. The reopening condition is unchanged by any of this: a second
platform that only supports MD5, which would be an *added* algorithm
and the column this section refuses, argued on its own terms.

A hash binds bytes, and it binds nothing about identity or freshness. A
digest confirms that the file a device received is the file NetHub
stored. It says nothing about whether that was the *right* file for that
device, or a current one: an artifact that was valid a year ago still
verifies today, which for an image store means a superseded IOS-XE build
carrying a correct NetHub hash. Both egress adapters therefore resolve
what to serve server-side rather than honoring a client-supplied path.
Day-0 maps an allowlisted serial to a set of artifact rows — a config,
optionally an image, each in a distinct *role* — and day-2 snapshots a
specific artifact row's `filename`/`sha512`/`version`/`file_size` onto
`upgrade_run_hosts` at submit (§5), so a run reads its own rows rather
than the `artifacts` table again at dispatch. The `artifacts.id` values
resolved are recorded on the log entry (§5), so
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
the device layer at all, since it is Kea plus phone-home and touches no
device. The two logs are not merged into one timeline. The two egress
protocols stay distinct, because the shared store beneath them is what
makes that split cheap.

### 3.5 Everything a phase acts on is read from rows, not supplied

§3.4's registry file is the first instance of a pattern the rest of the
system follows: **NetHub renders or snapshots the inputs a phase acts on
rather than accepting them.** The registry file is a projection of the
`artifacts` table (§7.2). So is the per-run inventory a phase actually
reads: hosts, bundle reference, connection variables are snapshotted onto
`upgrade_run_hosts` at submit rather than re-resolved from `artifacts` at
dispatch (§5), so a mid-run supersede cannot silently re-target a run
already in flight. There is no template and no playbook selected per
platform any more — the code that acts on a host is `nethub/devices/`
itself, chosen by `platform` in the sense that a second platform is a new
set of modules there, not a new file rendered per job.

What a user submits is a request: which devices, and which published
bundle each should end up on. NetHub validates it and compiles the rest.
The reasoning is in §8.1, and the rule is the one §7.2 already states
for the registry — the table is the source of truth, the rendered or
snapshotted form is derived from it, and there is no second place for the
two to disagree.

This is also why nothing a phase reads carries credentials. Connection
variables come from the submitting admin's identity (§4.3), and the one
secret a phase execution needs travels sealed to the sibling's key in its
own job row (§9.1) and is opened into a Python attribute on
`phases.PhaseContext`, not a file — never written in the clear anywhere
a phase's inputs are.

The image bytes themselves never enter that snapshotted state, and that
falls out of the same rule rather than being an exception to it: what
NetHub snapshots onto `upgrade_run_hosts` is the *reference* — filename,
digest, size — not the file. The sibling does handle the bytes during
the push (§4.3.1): `stage_image()` reads the file directly off the
published subtree and streams it to the device over the second SSH
session Netmiko opens for the transfer, rather than the file being
copied anywhere else first. The invariant this section actually needs
still holds: nothing the job rows or the snapshotted row set
carries is ever the multi-hundred-megabyte image itself, only a
reference to where it already sits on the one mount the push reads.

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

**The authentication argument and the confidentiality argument are not
the same argument, and only the first one is actually settled here.** No
trust anchor exists to authenticate a certificate on first contact — that
reasoning is sound and it is specific to authentication. It does not
follow that nothing can be encrypted: unauthenticated encryption needs no
trust anchor at all, and §3.3's own sequence supplies a candidate one —
the generic script NetHub ships is what performs every fetch after it, so
a certificate pinned *inside that script* would let the sensitive
second-hop payloads (config, image) cross the VLAN encrypted against a
key the first hop established, without requiring the device to validate
anything against an external CA. That downgrades the passive-listener
exposure above to "an active on-path attacker must substitute the first
script," a materially higher bar. Whether that's worth building is left
open (§10) rather than decided here, because it is not free: it adds a
certificate NetHub must generate, rotate, and re-embed in the script it
ships, and a rotation that isn't itself pinned to something reopens the
same first-contact problem one level up. The point of stating it here is
narrower — the "adds complexity without adding real protection" cost/
benefit line in the paragraph above is true for authentication and is
not yet shown to be true for confidentiality, so it should not be read as
having settled both.

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
  on different attributes (MAC and serial). That is defense in depth
  against a mistyped reservation or a stale entry, not two independent
  barriers against an adversary — §4's own honesty paragraph about the
  three transport-level controls applies here too: both MAC and serial
  are readable from the position an attacker must already occupy to
  reach either gate (MAC via an ordinary DHCP `DISCOVER` on the VLAN,
  serial via the chassis label, packing slip, or CDP/LLDP from an
  adjacent port), so getting both right is not two separate secrets, it
  is one position with two readable attributes. Worth noting for the
  same reason: "802.1X" as the VLAN-isolation mechanism (§4) is written
  for a population of devices with no configuration and no credentials
  yet, which in practice means MAC Authentication Bypass rather than a
  certificate or credential exchange — the same spoofable MAC this
  bullet already relies on, not an independent control layered on top
  of it.
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

**Buffering the counters and not the rest of the write was an
incomplete fix.** Every phone-home — including every denial — still
writes a `provisioning_log` row synchronously (plus junction rows on a
successful claim), and a claim additionally opens `BEGIN IMMEDIATE`.
Those are the larger writes on the same hostile path the paragraph above
was written to protect, against a WAL database the sibling concurrently
writes during publish and retention purges. `provisioning_log` rows are
therefore buffered and flushed the same way the counters are — sampled
under sustained load rather than written one-for-one — while the
`BEGIN IMMEDIATE` claim transaction stays synchronous, since it is the
one write that has to be durable to mean anything (§4.1's rowcount-as-
authorization-result). §5's "a few seconds" `busy_timeout` is, on its
own, larger than §3.2's 2-second p99 budget, so a single contended write
already misses that budget by construction; the phone-home route sets
its own `busy_timeout` below the p99 target with an explicit fast-fail,
rather than inheriting the connection-wide default.

**A successful spoof looks like nothing, and that is the event worth
alerting on.** An attacker who wins the claim in §4.1 is answered
normally: the entry is consumed, a log row is written, no counter moves.
The genuine device then arrives minutes later and is denied — one
ordinary "unknown or already-consumed serial" row among however many the
flood produced. But *a denial for a serial that was consumed inside its
own TTL window* is the highest-signal event this endpoint can produce,
it costs one indexed query on the denial path — `provisioning_log` is
indexed on `(serial_claimed, occurred_at)` specifically so this query
and the per-serial counter lookup above it share an index rather than
scanning — and nothing else in the system will ever notice it. NetHub
raises it as its own alert, distinct from the volume-based ones. This is
the same shape as the silence case below: the attack that succeeds is
the one that generates no failure.

Silence is also a signal. A rogue DHCP relay or a redirected option 67
shows up as a device that simply never arrives: no failed provisioning
attempt, no log entry, nothing to look at. Because §4.1 makes allowlist
entries time-bounded, NetHub already knows which enrollments it is
expecting, so an allowlisted serial that doesn't phone home before its
window closes raises a notice. Without that, the entry just expires and
nobody learns anything. This is the only detection NetHub has for an
attack that succeeds by keeping the device away from it.

**This section's apparatus is not phone-home-specific, and §6.1 used to
claim it was the only unauthenticated route.** It isn't: the local login
form, the OIDC redirect/callback, and one-shot enrollment-token
redemption (§4.4) are all reachable before a session exists, and in a
deployment with `local_accounts_enabled` the login form is the
highest-value pre-session target in the system — a seeded admin account
sits behind it. None of that traffic is phone-home, so none of it gets
this section's rate budgets, counters, or alerting by default. The same
machinery applies with a different key: per-username-plus-source-prefix
counters instead of per-serial-plus-source, the same buffered-write
discipline, and an equivalent high-signal alert — a successful login
from a source that just exhausted a failure budget, or an enrollment
token redeemed from a source other than the one it was issued to. §6.1
is corrected to say so explicitly, since it is the sentence an
implementer reads to decide which routes need this treatment.

Day-2 image delivery deliberately stays credentialed, unlike day-0
(§4.3.1): the push rides an SSH session authenticated with the
submitter's own device credential, never the plain HTTP used for the
day-0 script fetch (§3.3). The two days are in different trust situations, so
this is not an inconsistency. A day-0 device has no credentials to offer
regardless of transport, so plain HTTP costs nothing extra there. A
day-2 device is already enrolled, and a credential is what gates who may
write an image to it or read one out of the store. Dropping that for
protocol uniformity would remove real authorization that hash
verification does not replace: hash verification confirms the bytes
weren't tampered with, and says nothing about who was allowed to move
them in the first place.

There is no second, distribution-specific credential to price. Push
collapses it into the one credential §4.3 and §4.3.1 already cover, and
its residual cost lives elsewhere — in the device configuration change
that makes the push possible. A device-side pull (§10) would bring a
distribution credential back.

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
value is that NetHub never sees a password; a Netmiko session needs a
password to send. Both are true, so the upgrade dispatch (§8.1) collects
the submitter's device credential, seals it to the sibling's public key
in the job row it was collected for, and the sibling opens it into a
plain Python attribute — `phases.PhaseContext`'s credential field — for
the life of one phase execution. It never reaches disk in the clear, is
never kept in the session, and its ciphertext is cleared when the job is
claimed or ends (§9.1).

It belongs to a *phase execution* rather than to the session or to the
run. The session is the wrong owner because a serial activation wave
outlives any reasonable session lifetime. The run is the wrong owner
because §8.1 lets a run park at an approval gate for days, and "held for
the life of the run" would mean a password stored, sealed or not, across
a weekend. So it is collected with each approval
and dropped when the execution that approval released reaches a terminal
state. The one execution with no approval of its own is `verify`, which
§8.1 runs on completion of `activate`: the sibling runs it straight after
`activate` on the credential already in hand and drops it when `verify`
ends, so approving the reload releases both (`sibling.run_once`). Nothing
is ever held for `verify` separately, so if the sibling dies between the
two, the restarted one fails `verify` with `failure_stage='credential'`
rather than finding a password waiting. §4.3.1 covers why push needs no second credential with a lifetime
of its own; §9.1–§9.2 cover how this one crosses from the browser to the
sibling, what sealing it protects, and what that costs the operator.

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

**Two-sided attribution.** Under a shared service account, every change
on every device is attributed to that one account, and who actually made
it is answerable only from NetHub's own records — that is, from the one
system that would also be wrong if it were compromised.
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
being true. The arrangement still beats a shared service account, but it
is corroboration by a second copy rather than by a second authority.
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

- **A wrong credential must not become a lockout, and "validated once
  against the AAA server" has to name an actual mechanism.** A stale
  password against a fifty-host activation wave is fifty failed
  authentications at the AAA server, which is how an engineer loses
  access to the entire estate. What NetHub does about this is a
  single-connection credential probe: before dispatch, the sibling opens
  one Netmiko session — to the first host in the run's target set — with
  the submitted credential and confirms it authenticates. A
  full AAA-client integration (NetHub itself speaking to the TACACS+/
  RADIUS server) was considered and rejected as out of proportion: it
  would add a standing trust relationship and a shared secret of its own
  to a design whose whole point is minimising exactly that. As built, the
  probe is each phase's first login rather than a separate session:
  `phases.LoginGate` holds every other host's login until one host has
  answered, and a refusal stops the phase (§8.1). The probe's
  known limitation is stated rather than implied: Cisco AAA commonly
  authorizes by device group or VTY access class, so a credential
  confirmed against host 1 is evidence the *password* is correct, not
  proof it is *authorized* on host 30 in the same wave — a
  group-scoped denial can still surface mid-wave. Closing that gap
  further (e.g. probing one host per distinct AAA policy group named in
  the request) is left open (§10); the one-host probe is the floor, not
  a claim of completeness.
- **The device proves who it is before the credential is sent — and
  that proof must be confirmed by someone other than whoever is about to
  benefit from skipping it.** Netmiko authenticates by sending the
  password after key exchange, and `hosts[].ansible_host` is
  submitter-supplied (§8.1) and must be an IPv4/IPv6 literal, checked
  against the target CIDR (§8.1) by numeric comparison rather than
  after a DNS lookup — a hostname would let the CIDR check and the
  eventual connection resolve to different addresses at different times,
  and would key `device_host_keys` on a string whose meaning can change
  after the fact. Even with that closed, without host-key verification
  an operator can name a machine they control and be handed a
  colleague's AAA credential, or an on-path attacker on the management
  network can take it. An earlier pull design treated its minted
  *distribution* password's exposure as structural and mitigated it with
  lifetime; this one is not structural and does not get that excuse. NetHub pins
  the fingerprint from `device_host_keys` (§5) as a `paramiko` host-key
  policy object built fresh for each connection — there is no rendered
  `known_hosts` file for a connection plugin to read from a different
  path than the one NetHub intended, because the client that opens the
  connection is NetHub's own (`nethub/devices/connection.py`) — and fails
  closed on mismatch.

  **First contact is where the excuse runs out, and the design used to
  get this wrong.** TOFU pinning fails closed only on a *changed* key —
  every instance of "an operator names a machine they control" is a
  *first* contact, the one case the pin doesn't cover, because pinning
  a first-seen key and using it are the same event with nobody in a
  position to say no. The earlier text here — "shown at the submit gate
  and recorded against the approver" — assumed first contact happens at
  an approval gate, but §8.1's own phase table runs pre-check
  immediately on submit, with no gate, using the submitter's own
  credential; by the time anyone *else* is asked to approve a later
  phase and supply a different credential, the key for that address is
  already pinned, silently, by the same person who chose the address.
  So: **an address with no already-confirmed `device_host_keys` row
  cannot be named as `ansible_host` by any run at all.** Confirming a
  new address is a separate, explicit admin action — connect (no device
  credential needed; the host key is exchanged before authentication),
  show the fingerprint, record `confirmed_by` — decoupled from any
  particular run's submit or approval flow. This doesn't make the pin
  out-of-band verification; an attacker can still confirm their own box
  under their own name. What it removes is the *silent* version of the
  attack: every approval screen shows each target host's confirmed-by
  identity and confirmation date, so an approver asked to release their
  own credential against an address confirmed minutes ago by someone
  else, for a run someone else submitted, has the information in front
  of them to notice it. What this costs a first-time fleet onboarding is
  left open (§10).
- **NetHub requires privilege level 15, and that is a policy rather than
  a platform quirk.** Every command an upgrade runs — `write memory`,
  `copy` to flash, `install add … activate commit` — needs level 15 on
  IOS-XE. NetHub asks for it *at login* rather than reaching it through
  `enable`, so `connection.connect()` passes no `secret` and nothing ever
  calls Netmiko's `.enable()` — there is no escalation step and no second
  secret to collect. That removes a second secret from a
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
  should stay short.** Privilege 15 at login is the one thing NetHub asks
  a device to already have configured. The pre-check's earlier
  `ip ssh source-interface` assertion is gone: it was a pull-era
  requirement for the device's SFTP *client*, and push has confirmed it
  does not gate the device's SCP *server* — the two are unrelated
  services on the device. NetHub does not otherwise write configuration
  outside the upgrade itself, and asserts privilege 15 at pre-check
  rather than discovering it mid-wave. One exception is deliberate and
  bracketed rather than standing: the stage phase enables the device's
  own SCP server for the
  duration of the push and restores whatever it found — enabled or not —
  in a `finally:` block, confirmed by re-reading the running-config
  rather than trusted from the adapter's exit status (§4.3.1). That
  confirmation is why "does not write configuration outside the upgrade
  itself" still holds: the toggle is scoped to one phase execution on one
  host, not a standing device change.
- **There is no distribution account.** There is no second account to
  keep shared and no second password to rotate: the transfer runs under
  the submitter's own device credential, over a second session to the
  same pinned address (§4.3.1).

#### 4.3.1 How the image reaches the device

NetHub pushes the image to the device over SCP. That is the only day-2
transport: `nethub/devices/transfer.py`'s `stage_image()` reads the file
from the store NetHub owns (§3.3), pushes it, and ends in a `verify
/sha512` against the digest computed once at ingest (§3.4).

**The history matters, because the direction was reversed once already.**
An earlier revision of this design had the device fetch its own image
over SFTP, authenticating to a distribution account NetHub minted a
password for on every phase execution. **That design assumed the device
could open an outbound connection to NetHub, and a nontrivial fraction of
real deployments block exactly that.** Outbound SSH from a network device
is a common perimeter rule — the same jump-host concern that motivates
restricting SSH egress from servers generally, applied to infrastructure
that is even more attractive to pivot through. Where that rule is in
force, pull does not degrade, it does not work. A later revision kept
pull as a deployment-selectable alternative beside push, but nothing ever
configured its distribution host or credential and it was never run
against hardware, so it was deleted rather than carried. §10 records
what bringing it back would need.

**Push inverts the connection, and that inversion pays for itself
immediately.** Every device in a run already has an
inbound, admin-initiated Netmiko session open for pre-check, activate,
verify and cleanup, authenticated with the submitter's own device
credential (§4.3). The stage phase pushes under **that same identity**,
over a **second** SSH session rather than the persistent one already in
use for command execution — this is now settled by construction rather
than probable. Netmiko's `CiscoIosFileTransfer` opens its own SCP
transport for the put, and `SCPConn.establish_scp_conn` builds that
second session through `connection.py`'s own `_build_ssh_client()`
override, so the pinned host-key policy and the keyboard-interactive
authentication both apply to it exactly as they do to the primary
session — verified end-to-end against a real Catalyst 9200CX, not
inferred from the library's documentation. The credential claim below is
unaffected either way — nothing new is minted — and the host-key
question this section used to leave open for a second connection is
answered by construction under Netmiko: there is one connection-opening
path in the whole codebase (`connection.py`), and both sessions go
through it. What real hardware has not yet exercised is the
session-*count* consequence: a second SSH/AAA authentication event gives
command-authorization policy a second chance to diverge between an exec
session and a transfer session, and some IOS-XE deployments cap
concurrent sessions per user, which a second simultaneous connection
under the same identity could trip. That risk is unchanged by which
library carries the second session, and is tracked in §10.

Nothing new is minted: there is no distribution account, no distribution
password, no per-phase credential lifecycle beyond the one §4.3 and §9.1
already define for the device credential itself. Everything this section
used to specify about the old pull model — a minted password's lifetime,
its purpose-built low-privilege account, the SSH/SFTP daemon serving it,
the `ForceCommand internal-sftp` chroot hardening it needed — is absent,
because there is no second credential to specify any of it for.

**What push costs instead is a device configuration change, and
`transfer.py`'s push adapter is explicit about the price.** Pushing
requires the device's own SCP server to be listening, which means NetHub has to enable it, use it, and turn it back
off. The push adapter does this narrowly and defensively, and this
sequence is validated end-to-end against a real Catalyst 9200CX, not
just written to spec:

- It reads the device's current `ip scp server enable` state *before*
  changing anything, so a device that legitimately runs its own SCP
  server is left alone rather than having NetHub's restore turn it off
  underneath whatever else depends on it.
- It enables the server only if it was not already enabled, pushes the
  image via Netmiko's `CiscoIosFileTransfer` with `hash_supported=False`
  (Netmiko's transfer class MD5s the source file in its constructor
  whenever that flag is left on — including under
  `file_transfer(disable_md5=True)`, which only skips the *comparison* —
  and this design has exactly one hash algorithm, §3.4), and re-verifies
  the pushed bytes on the device by asking it for its own `verify
  /sha512` digest and comparing in Python rather than substring-testing
  the device's echoed output — §3.4's third consumption of the ingest
  digest.
- It restores the prior state in a `finally:` block that runs whether the
  push succeeded, failed, or raised partway, and it does not trust the
  restore call's return value: it reads the running-config back and
  compares it explicitly. **This is the committed behavior, not just the
  target**: an unconfirmed restore raises from the `finally:` block
  itself, superseding an in-flight push failure and keeping it as
  `__context__`, and fails the host outright rather than logging a
  warning — a device left changed is the more urgent of the two facts.
  `_restore_scp_server` never raises on its own: a restore that could not
  be attempted *is* an unconfirmed restore. The gap that would let a
  stray `ip scp server enable` ride into startup-config on a later
  `write memory` is closed for every path that reaches `finally:` at
  all — which is still not every path, per the next paragraph.

**What that mechanism does not cover is the honest gap, and it is worth
stating rather than implying it away.** The confirm-and-fail-host logic
only runs if the transfer's `try`/`finally` is reached at all. A killed
process, an abandoned run, or a crashed sibling mid-transfer never gets
there, and can leave a device with its SCP server enabled and nothing in
NetHub recording it — exactly the "cannot enumerate afterward" problem
the design once used to reject push outright. Closing it fully would mean
either a device-side timeout on the enabled state (IOS-XE does not offer
one for this knob) or a periodic reconciliation pass that connects to
every device that was ever mid-stage and checks, which is the per-device
current-state view §2 and §7.4 refuse to build for anything else in the
system. This is recorded as an accepted residual risk, not a solved one.

**That gap is reachable through two paths this design controls, not only
through a crash, and both are closeable without the reconciliation pass
above.** §8 gives the stage phase a per-host wall-clock bound derived
from `file_size`, described as the run being "killed on expiry" — if
that kill is a process-level `SIGKILL` against the sibling itself, a slow
link (exactly the condition a multi-site deployment hits, §3.3) routinely
ends the transfer without ever reaching `finally:`, making the
"exceptional" case the *frequent* one for the deployments that need push
most. Fixed by scoping the bound to the transfer call itself — Netmiko's
own read timeout on the SCP session — so expiry raises inside the
adapter's own `try`/`finally` rather than killing the sibling process
outright; the job-level wall clock (§8) remains as an outer backstop for
everything else, not the mechanism for this one bound. Separately, §8.1
calls cancelling a stage "safe, non-disruptive by construction" — true
for the fleet's traffic, not for a device mid-SCP-toggle: stage is the
only phase that mutates device config, and the stage concurrency cap
(§8.1) means several hosts can be mid-push when a cancel lands. That
sentence is corrected to **"cancelling a stage is safe for the fleet;
an in-flight host may still need its SCP server restored,"** and the
cancel handling waits for in-flight transfers to reach their own
`try`/`finally` before honoring the cancel, rather than killing the
sibling process outright.

Both of those keep the mechanism above intact — they stop it from being
bypassed by two things this design chooses to do on purpose — so the
tracking this needs is a state to distinguish "restore confirmed" from
"restore owed," not the per-device inventory §2 refuses to build. A
nullable `scp_restore_confirmed` boolean on the stage row in
`upgrade_host_phase_results` (§5) — the persisted counterpart of the
same-named fact the adapter already computes locally — is that state:
null or `false` means the run ended (by any means, including a hard
kill outside this design's control) without a confirmed restore for
that host, which is exactly the row a future reconciliation mechanism
(§10) would need to act on if one is ever built — and, short of that,
is a queryable answer to "which devices might still be exposed" that
today's design has no column for at all.

**The transfer library was the one part of this design not settled on
paper — it's now settled, pragmatically rather than cleanly.** Under
Ansible, `net_put`'s SCP path with the `libssh` connection type was
tested against a real IOS-XE device at image size, not just with the
small text file a different playbook had moved successfully, and found
unusable: the SSH connection broke repeatedly for reasons the debug log
did not surface, with nothing actionable to fix. That result carried
over the switch to Netmiko rather than being re-litigated: Netmiko is
Paramiko-based, and Paramiko is the library the working case above used,
so push works here and is validated at 471 MB against real hardware.
Paramiko is deprecated upstream with a scheduled removal, but it's the
only thing that has actually worked at image size, so it's what's
committed (`nethub/devices/transfer.py`; if it is ever swapped, §10
records that the swap needs re-testing at image size rather than
assuming a smaller transfer generalises). IOS-XE has no SFTP server —
Cisco documents the SFTP client as always enabled and the server as
unsupported, consistently across trains — so under push, SCP is the only
wire protocol available regardless of which library carries it.

**Shelling out to OpenSSH `scp` as a subprocess was considered as the
documented fallback and rejected, not merely deferred.** The reason is
the second bullet below: unlike Netmiko's file-transfer class, a bare
`scp` subprocess needs the device password on an interface that has no
secure way to receive it. The credential already lives as a plain Python
attribute for the life of one phase execution (§3.5, §9.1) rather than
being handed to a container or a runner, so the sibling *could* pass it
to a child process — but only over a pipe on `stdin` via a small wrapper,
never as an argument or an environment variable, and nothing here builds
that wrapper. Until it is, the deprecated-but-working Paramiko path is
the safer of the two, not because deprecation is harmless but because
the alternative's credential handling isn't solved yet. This contract is
kept here, unbuilt, as the reference for whoever builds it or the custom
transfer script that might replace both (§10):

- It would be a **second SSH connection**, independent of the primary
  Netmiko session, with its own host-key answer to get right. It must be
  given `-o StrictHostKeyChecking=yes -o UserKnownHostsFile=<a file
  rendered from the same `device_host_keys` fingerprint §4.3 already
  pins>` explicitly — `scp`'s default behavior on an unrecognized host
  does not fail closed on its own, and inheriting that default would
  quietly reopen the first-contact problem §4.3 just closed.
- It needs the device password on an interface Netmiko's own transfer
  class doesn't need one for. The obvious wrappers — `sshpass`, an expect
  script, a naive `SSH_ASKPASS` — put the credential on a command line,
  landing in `/proc/<pid>/cmdline`, readable by anything sharing the uid
  for the life of the transfer — the plaintext exposure §9.2 keeps to a
  Python attribute inside the sibling. This is
  the unsolved part: the credential would need to reach the `scp`
  subprocess over a pipe on `stdin` via a small wrapper, and nothing here
  builds that wrapper yet.
- Its per-host timeout is a killed child process either way, which is
  the same failure mode the restore-gap fix above addresses for the
  Netmiko path — the read-timeout / `try`/`finally` structure around it
  wouldn't change with the swap.

**The concern that image encryption "moves onto the process that must
not stall" does not survive contact with where the code actually runs.**
The earlier rejection worried that push would relocate every image's
crypto into NetHub's own process on the host §3.2 protects from
stalling. The transfer runs inside the sibling's own process, exactly
where every other device-touching phase already runs (§8, §9) — never in
Flask. Only *which side of the wire* performs the SSH
work changed; where that work happens did not.

Two consequences carry over from the credential model unchanged, because
push did not touch them:

- **The authorization gate §4.2 describes is preserved intact.** An
  unauthenticated party still cannot cause an image to be pushed anywhere
  — that requires an authenticated submitter, an approved gate, and the
  submitter's own valid device credential, exactly as every other phase
  does.
- **Only the phase that moves bytes touches the device's transfer
  service.** Under §8.1's split, stage is the phase that toggles and uses
  the SCP server; pre-check, activate, verify and cleanup never do. A run
  parked at the reload gate from Monday to Saturday holds no device
  credential anywhere, sealed or otherwise, because that gate has not
  collected one yet (§9.1), and has made no standing change to any
  device — the same property §8.1 claims for the sibling's phase
  execution itself.

**Ansible Vault was considered while this design still used Ansible, and
the reasoning against it carries over unchanged now that it doesn't.**
Vault encrypts a secret at rest in a file a runner reads. The credential
this section is about was never in a file to begin with — it is a Python
attribute held for the life of one phase execution — so vault would have
protected a copy that was already the least exposed one, with a vault
password that has to reach the same process by the same means, which is
two secrets where one would do and restores exactly the key-disposal
problem removed when the old minted distribution password went away.


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
explicitly rather than implied by anything else. And it is why §5 grows an audit table for user administration:
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
device account can't produce that. **Every run uses the submitter's own
`users.device_username`**, and NetHub has no mode that substitutes one
shared name. An explicit, deployment-level shared account mode was
designed and partly stubbed (a hardcoded-off flag and a column
snapshotting it on every run), but it never had a username setting or
anything that read it, so it was deleted; §10 records it as possible
future work. If it returns, its cost is the one §4.3's two-sided
attribution argument warns against for a shared service account: every
device-side change attributed to one name, answerable only from NetHub's
own audit trail rather than corroborated by the device's own AAA/syslog.
That must be a decision an admin makes once and visibly, never a default
a missing IdP quietly falls back to.

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

- `sessions.id` is stored as `sha256(token)`, not the token itself — the
  cookie carries the token, the table never does. The table is
  read/write from every route including the phone-home-adjacent surface
  discussed below, so a database read (a backup, a copied file, an
  operator's `sqlite3` session) must not be equivalent to holding live
  sessions for every logged-in admin, which a plaintext `sessions.id`
  would make it.
- A fresh session id is minted at login, not reused from any pre-auth
  state (a pending OIDC `state`/nonce row, a pre-login placeholder).
  Carrying a pre-auth identifier across the authentication boundary is
  how session fixation happens; minting fresh is the one-line fix.
- Every authenticated request re-reads `is_active` and `role` from the
  `users` row. Deactivation takes effect on the next request. **A role
  change takes effect on the next request only for `local` rows** — for
  `oidc` rows, §4.4 computes `role` *at login* and writes it onto the
  row, so re-reading the row on every request re-reads a value that only
  changes when the person next authenticates. The real bound on an
  IdP-side demotion is therefore the *absolute session timeout* below,
  not "the next request" — restated at that strength rather than left to
  imply the stronger one. A deployment should size its absolute timeout
  against how quickly an IdP-side group change needs to take effect
  (§10), and the cookie itself carries a session id and nothing
  authoritative either way.
- Absolute and idle timeouts, both bounded. The absolute one exists
  because §4.3's activation waves outlive any sensible idle window and
  should not extend the session by being watched — and, per the point
  above, is now also the OIDC role-revocation SLA.
- A password reset, a role change, or deactivation invalidates that
  user's other sessions. An admin recovering an account should not be
  racing whoever else holds a cookie for it. Whether a user may hold more
  than one concurrent session at all — this bullet presumes they can —
  is a deployment policy left open (§10) rather than decided here.
- Cookies are `Secure`, `HttpOnly`, `SameSite=Lax`. **"`SameSite=Strict`
  on the approval routes" was never expressible with one cookie** —
  `SameSite` is a property of a cookie, not a route, so a single session
  cookie is one or the other for the whole origin. The fix is a second,
  narrower cookie: approval routes additionally require a `Strict`,
  `HttpOnly` companion cookie set only by same-site navigation, checked
  alongside the session cookie and the CSRF token below. The OIDC
  redirect-back is the one flow this affects, and it's closed with a
  same-site landing page between the IdP redirect and the dashboard
  rather than by weakening the approval routes' cookie. `SECRET_KEY`
  rotation invalidates sessions by design, which is acceptable once the
  store is server-side because the sessions themselves survive nothing
  else.

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
`journal_mode=WAL`, `busy_timeout` (5 s, so a second writer waits for
the lock instead of raising), and `foreign_keys=ON`, which is off by
default and would otherwise silently turn every reference below into a
suggestion. All three are set in `extensions._configure_sqlite`, with a
test asserting each.

The schema changes only through migrations (Alembic, via Flask-Migrate;
`nethub/migrations/`), and they run by themselves: the web process brings
the database to the latest migration at startup, before it serves anything
(`nethub/schema.py`), so upgrading a deployment is a new image and a restart
(§9). Only the web process migrates; the sibling waits until the database is
at the revision its own code expects, because two processes altering one
SQLite file at once is how a schema ends up half-changed. Each upgrade runs in
one transaction with foreign keys suspended for SQLite's table rebuilds and
checked before commit, so a migration that fails leaves the database as it
was. NetHub refuses to start on a database a newer version has migrated,
rather than write to a schema it does not understand. A database created
before migrations existed is adopted only by the repairs `schema.py` lists
for drift NetHub itself caused (columns added to an existing table, or
removed, when `create_all()` could do neither); anything else is refused
with the differences named. The models and the migrations are held equal by
a test that builds a database each way and compares them, CHECK
constraints, partial indexes and the terminal-status trigger included.

- `artifacts` table, the single ingest record behind both days (§3.4):
  `id`, `kind` (script/config/image), `platform`, `bundle_key`,
  `filename`, `sha512`, `file_size`, `storage_path`,
  `version`, `state`, `superseded_by_id`, `bytes_state`,
  `bytes_pruned_at`, `uploaded_by`, `uploaded_at`. Every byte NetHub
  serves, on either day, has exactly one row here.
  - `storage_path` is where the blob actually lives on disk, and is what
    §7.3's retention purge collects by. There is no `remote_dir`: that
    column existed because the distribution host could once have been a
    separate remote machine whose layout NetHub did not control (§3.3),
    which is no longer true. The push reads the file directly off the
    published subtree by filename, in the sibling's own process, so there
    is no per-artifact directory to record, and the constraint below is
    what the push relies on instead. `UNIQUE(filename) WHERE
    state IN ('staged', 'published')` is the constraint that makes
    "push reads by filename" safe: without it, two artifacts uploaded
    under the same original filename can promote to the same on-disk
    path and silently overwrite one artifact's bytes with another's, and
    every downstream hash check still passes — each one compares a row's
    own `sha512` against whatever currently sits at that path, not
    confirming the row and the disk agree on *which* artifact this is.
    That would quietly break the "hashed once, consumed three times"
    chain of custody §3.4 is built on, so the constraint is load-bearing
    rather than tidy.
  - `bytes_state` / `bytes_pruned_at` split blob retention from row
    retention, which §7.4 otherwise conflates. "Retained while
    referenced, regardless of age" is the right rule for the *row* and
    the wrong one for half a gigabyte of superseded IOS-XE image pinned
    forever by one surviving job row. The row outlives the bytes and
    says so, so an audit query returns "published 2023-04, image pruned
    2026-04" rather than a path that silently no longer resolves. In
    practice only `published` and `present` are ever written today —
    there is no promotion step that reaches `staged` through and no
    supersede flow yet, the same no-supersede stance an earlier iteration
    of this store had — so `superseded_by_id` handling arrives with the
    flow that reads it, not before.
  - `bundle_key` is what a day-2 request names to resolve an image
    (`hosts[].bundle`, §8.1): `UNIQUE(platform, bundle_key)` over rows
    where `kind = 'image'` and `state = 'published'` gives one published
    image per bundle key per platform, enforced by the database rather
    than by whatever writes the row remembering to check.
  - `state` runs `staged` → `published` → `superseded`, with
    `superseded_by_id` pointing at the row that replaced it, though in
    practice only `published` is ever written today (`bytes_state`
    likewise only ever reaches `present`, above); delete is still a hard
    removal of row and bytes rather than a supersede.
  - Indexed on `(kind, platform)` for the browse views and on `sha512`
    for duplicate detection at ingest.

**There is no separate rendered registry file, no publish job, and no
git commit, and that is a deliberate simplification rather than a gap
this document forgot to update.** An earlier revision of this design had
publish write a `software_registry.yml` the device-side automation read,
committed to git under an advisory `flock`, with its own `registry_jobs`
row, `render_state` machine, and startup reconcile (§7.1, §7.2 as they
used to read) — because that file was the only thing the automation could
consume, and NetHub did not yet own the store it described. Both
preconditions are gone: NetHub owns the store outright (§3.3), and the
phase model reads a run's own snapshotted `upgrade_run_hosts` columns
directly (§3.5) rather than a rendered file, so nothing left in the
system reads `software_registry.yml`. The maintainer confirmed nothing
*outside* NetHub read it either — it existed only for the old
automation layer — so removing it was a deletion rather than an export
path that needed replacing. What replaces the whole publish-job
machinery is `artifacts.ingest()` itself: uploading an artifact *is*
publishing it, synchronously, in the request that received the upload,
with the two constraints above as the only concurrency control (§7.1
restates why that is enough and §7.2 restates what atomicity story
replaces the render/commit/reconcile one). There is consequently no
`registry_jobs` table, no `job_log_path`, and nothing for §7.3's sweep to
apply to on the publish side — the sweep and the terminal-status trigger
described there are `upgrade_phase_jobs`-only now.
- `upgrade_runs` table, the parent record for one upgrade dispatch —
  staging and installing a software bundle across a set of devices
  (§8.1): `id`, `platform`, `submitted_by`,
  `device_username_used`, `request_document`,
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
    these before starting each host. What cancel *means* is per phase and is stated
    in §8.1 — cancelling a stage is safe, cancelling an activation
    mid-wave is not.
  - `device_username_used` snapshots `users.device_username` at
    dispatch, for the same reason `upgrade_run_hosts` (below) snapshots
    `version`/`filename`/`sha512`: an audit row that re-reads its own
    answer from a mutable table stops being an audit row the first time
    somebody's mapping is corrected. It is always the submitter's own
    name (§4.4). If shared account mode ever returns (§10), this table
    needs a column recording *how* the name was chosen, or an auditor
    cannot tell "jsmith ran this" from "everyone runs as jsmith". The
    same goes for a second transport: `scp_restore_confirmed` (below) is
    readable alone only because push is the only transport, so a
    returning pull transport needs a snapshotted transport column beside
    it.
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
  `file_size`, `flash_dir`, `config_backup_path`, `reported_version_pre`,
  `reported_version_post`, `state`, `last_phase`, `error_summary`, with
  `PRIMARY KEY (run_id, hostname)`. `artifact_id` is a foreign key with
  `ON DELETE SET NULL`: a finished run keeps its snapshot columns and
  loses only the link if the artifact is later deleted, and
  `artifacts.delete()` refuses while a run in `pre_checking`,
  `awaiting_approval` or `running` references it. One row per targeted device, using
  the same foreign-key-plus-snapshot arrangement `upgrade_runs` uses for
  its own request-level snapshot. Per-host state living here rather than
  scattered across ad hoc bookkeeping is what makes the phase model's
  per-host history legible at all.
  - `config_backup_path` is a home for a pre-reload running-config
    capture that the phase model accounts for at the point that actually
    matters: `phases.phase_activate` calls
    `install.capture_running_config()` immediately before issuing the
    reload, not at pre-check — a capture taken at submit time would be
    stale by the time activation happens days later (§8.1), and this is
    the same reasoning that puts `write memory` last during install
    (below). The column exists so a backup taken minutes before an
    upgrade is retrievable afterward rather than living only in a job
    log; wiring the capture through to a written file at this path is
    tracked as outstanding rather than claimed done here. It gets the
    same retention horizon as the run it belongs to (365 days, §7.4),
    purged as a unit with it, since a config backup with no expiry would
    be exactly the persistent per-device record §2 refuses to keep, and
    one that outlives its own run's audit trail is a backup nobody can
    date. Whatever eventually reads it back has to treat a device's
    running-config as the same class of secret §4 spends a page on (AAA
    keys, SNMP communities, enable hashes), which bounds who may retrieve
    it the same way `error_summary` bounds what may be written to a row.
  - The primary key is load-bearing rather than tidy. A request document
    naming the same host twice is trivially producible by hand, and
    without the key it yields two host rows and two reloads.
  - `file_size` completes the snapshot for the same reason `sha512` and
    `version` do, and here the consequence is operational rather than
    archival: §8's per-host stage bound is derived from it *at dispatch*,
    before any connection opens, so NetHub needs the value in this row
    regardless of anything measured device-side. Without it the stage
    phase either cannot render its host list from this table, or has to
    re-read `artifacts` at dispatch, which makes the snapshot decorative
    and lets a mid-run supersede silently re-target the run. With it,
    each phase reads only the run's own rows and reads `artifacts` not
    at all.
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
  `attempt`, `status`, `failure_stage`, `error_summary`,
  `scp_restore_confirmed`, `started_at`, `finished_at`, keyed `(run_id,
  hostname, phase, attempt)`, with `FOREIGN KEY (run_id, phase, attempt)
  REFERENCES upgrade_phase_jobs (run_id, phase, attempt)` and `FOREIGN
  KEY (run_id, hostname) REFERENCES upgrade_run_hosts (run_id,
  hostname)`. One row per host per phase execution, which is what makes
  the per-host history above legible. `upgrade_run_hosts`' cursor
  columns stay as a derived convenience for the dashboard's default view
  rather than as the record of what happened.
  - The two foreign keys were missing even though both parents were
    already built to be referenced this way — `upgrade_phase_jobs`
    carries `UNIQUE(run_id, phase, attempt)` and `upgrade_run_hosts`
    carries `PRIMARY KEY(run_id, hostname)` for exactly this. Without
    them, nothing stops a per-host result row from being written for a
    phase execution that was never approved or dispatched, or for a
    hostname never in the run's target list, and "purged as a unit with
    the run" (§7.4) has no declared mechanism keeping this table in
    lockstep with its parents beyond a shared `run_id`.
  - `scp_restore_confirmed` is nullable — set only on a stage row, the
    only phase that touches the device's SCP server (§4.3.1). Null means
    no bracket ran: the image was already staged, or the failure came
    before the bracket. It never means a confirmed restore. With push the
    only transport this column is readable alone; an earlier revision
    also left it null for a pull-transport host, which had nothing to
    restore, and needed a join against the run's transport to tell the
    two apart. It is the persisted counterpart of the fact the push
    adapter already computes locally in its own `finally:` block. It is how a future reconciliation
    pass, or an operator running an ad hoc query today, finds "which
    devices might still have their SCP server enabled" without NetHub
    needing to keep a persistent per-device inventory to answer it
    (§4.3.1, §10).
- `device_host_keys` table: `ansible_host`, `key_type`,
  `fingerprint_sha256`, `first_seen_at`, `confirmed_by`,
  `confirmed_at`, `UNIQUE(ansible_host)`. One row per address NetHub has
  connected to, supporting §4.3's fail-closed host-key check. It is
  keyed on the address rather than on a device identity on purpose:
  NetHub is not tracking devices (§2), it is remembering what answered
  at an address so that a change in the answer is visible.
  - Uniqueness is on `ansible_host` alone, with one pinned `key_type`
    per address, not `(ansible_host, key_type)`. The two read
    differently against the fail-closed goal: keyed on the pair, an
    address offering a different — but still valid — algorithm on a
    later connection (an ordinary SSH negotiation-order change, not an
    attack) reads as a mismatch and false-positives a run; keyed on the
    address alone, an attacker who wants a fresh TOFU prompt can't get
    one just by offering an unpinned algorithm. A device whose preferred
    algorithm genuinely changes needs an explicit admin re-accept, the
    same action first contact requires — not a silent second pin.
  - `confirmed_by` is no longer something a run's own first connection
    can set implicitly. §4.3.1 requires it be written by a deliberate,
    separate admin action — connecting once with no device credential
    (the host key is exchanged before authentication) to fetch and show
    the fingerprint — *before* any run may name that address as
    `ansible_host` at all, which is what makes a later mismatch
    attributable to a change rather than to whoever happened to submit
    first. `first_seen_at` still records the raw first contact,
    separately from `confirmed_at`, for the audit trail to distinguish
    "we saw this key" from "a human accepted it."
- `host_key_scans` table: `id`, `ansible_host`, `requested_by`, `status`
  (its own smaller vocabulary — `queued`/`running`/`succeeded`/`failed`/
  `abandoned`, deliberately without `cancelled`/`expired`/`timed_out`,
  since a scan has no approval gate and nothing to time out against
  beyond the connect timeout), `key_type`, `fingerprint_sha256`,
  `error_summary`, `created_at`, `started_at`, `finished_at`,
  `runner_instance_id`, `consumed_at`. This is what makes the confirming
  admin action above dispatched work rather than a Flask-side connection:
  scanning is device I/O like everything else in `nethub/devices/`, so it
  runs in the sibling and is claimed and swept exactly like a phase job
  (same conditional-claim shape, same FIFO queue, same NULL-safe sweep
  predicate) even though it is not a phase job in §7.3's sense — it has no
  `failure_stage` vocabulary of its own and no approval gate. `consumed_at`
  is what makes a succeeded scan confirmable at most once: the confirm
  action reads the address, key type and fingerprint off this row rather
  than from request-body fields a submitter could otherwise supply, and
  refuses a scan already spent, from a different requester, or older than
  a short freshness window past `finished_at`. It does not close §4.3's
  separation-of-duty gap by itself — the same person can still scan and
  then confirm, since there is no role model yet (§4.4) — it only proves a
  confirmation corresponds to a key NetHub itself observed at some
  specific prior moment rather than to whatever a form claims.
- `device_host_key_audit` table: `id`, `ansible_host`, `action`
  (`confirmed` | `deleted`), `key_type`, `fingerprint_sha256`, `actor_id`,
  `at`. An append-only log of who confirmed or deleted a pin and what the
  fingerprint was at that moment — captured as the **pre-image**: the
  fingerprint being removed, for a delete, or the one newly confirmed, for
  a confirm. It is keyed on the address string rather than on a foreign
  key to `device_host_keys.id`, deliberately, so the record of a deletion
  outlives the row it describes; a foreign key here would either block
  the delete this table exists to log or dangle the moment it succeeds.
  There is no "changed" action, because confirming already refuses to
  overwrite a confirmed row in place (above) — the only way to change a
  pinned key is delete, which this table records, followed by a fresh
  confirm, which it also records.
- `upgrade_phase_jobs` table: `id`, `run_id`, `phase`, `attempt`,
  `approved_by`, `approved_at`, `status`, `failure_stage`,
  `error_summary`, `created_at`, `started_at`, `heartbeat_at`,
  `deadline_at`, `finished_at`, `runner_instance_id`, `log_path`,
  `sealed_credential` (the approval's device credential, sealed to the
  sibling's key and non-null only while `queued`, by CHECK; §9.1),
  `is_retry` (the job re-runs its phase on the hosts that failed it;
  §8.1). One row
  per phase execution, reusing the same status vocabulary and startup
  sweep (§7.3) a publish job would need if one still existed as a
  dispatched job kind — it no longer does (above), so this table is the
  sole consumer of that machinery today. There is no `private_data_dir`
  here at all: nothing renders a directory for an execution to read, so
  there is no `env/extravars` and no per-execution filesystem tree to
  name. `log_path` records NetHub's own transcript of the execution
  rather than a playbook's stdout. `approved_by` and `approved_at` are
  what §8.1 means by an approval being a row rather than a keystroke;
  without them the gate is a UI affordance instead of a record.
  - `UNIQUE(run_id, phase, attempt)`, and this is the mutex the gate
    actually needs. §8.1's serialization guarantee is scoped to
    *execution*: it stops two phase executions overlapping, not two rows
    being created. Two admins on the approval screen both clicking
    "approve: reload" write two rows, and the serial queue then runs
    them one after the other — the fleet reloads twice. §8.1 says an
    approval is a row rather than a keystroke, so the row is where the
    collision has to be refused. `attempt` exists because §7.3 grants an
    `abandoned` phase a fresh, separately-approved retry, and §8.1 lets
    the hosts that failed a phase be retried; without it the constraint
    would forbid both along with the double-click. A new attempt is
    always one past the highest so far for that phase.
  - `status` carries §7.3's vocabulary plus `partial`: `succeeded` means
    every host the phase ran on passed, `failed` none of them, `partial`
    the rest.
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
  - **A plain foreign key only proves the artifact row exists, not that
    it's the right kind or in a usable state.** Nothing as written stops
    `image_artifact_id` from pointing at a `kind='config'` row, or at a
    `staged`, unpublished one that a day-0 device would then be handed —
    a live provisioning bug, not a data-quality nit, given §4.1's whole
    argument is that "everything that differs per device lives in the
    artifact the allowlist maps to." A `BEFORE INSERT`/`BEFORE UPDATE`
    trigger validates that each of the three FKs points at
    `artifacts.kind` matching its own role. State is looser by design
    rather than unspecified: day-0 config/script artifacts don't
    necessarily go through the `staged → published` promotion gate
    §3.4/§5 define for the image-registry publish flow — an
    admin-uploaded per-device config is not "published" to a fleet the
    way an IOS-XE image is — so the trigger's bar is only `state !=
    'superseded'` and `bytes_state` not yet pruned, i.e. "still the
    current, still-retrievable bytes for this row," not "went through
    publish." Whether day-0 artifacts should get their own
    review/promotion gate instead of relying on upload-time correctness
    alone is left open (§10).
  - `script_artifact_id` is audit-only, and worth being explicit about
    because §3.3 and this table read as though they disagree otherwise.
    §3.3 is clear the generic script is identical for every device and
    served at one fixed path before any per-serial decision is made — it
    is not part of the minted, per-attempt fetch set the way config and
    image are. This column does not change that: it records which
    script version was current when the entry was armed, for symmetry
    with `provisioning_log_artifacts` logging what was offered, and is
    never consulted by the day-0 fetch sequence to decide what to serve.
- `provisioning_log` table: `id`, `occurred_at`, `serial_claimed`,
  `mac_seen`, `source_ip`, `outcome`, `allowlist_entry_id`,
  `fetched_at`, indexed on `(serial_claimed, occurred_at)`. §4.2 makes
  this the evidentiary record for the system's only unauthenticated
  route, so it needs more than a boolean.
  - `outcome` is an enum, because "unknown serial", "expired entry",
    "already consumed", "rate-limited", and "resolved and offered" are
    five different facts an investigator needs to tell apart — and
    §4.2's highest-signal alert is precisely the pair *consumed* then
    *denied* for one serial inside one TTL window, which is what the
    index above exists to serve without a scan.
  - `serial_claimed` and `mac_seen` are denormalized onto the row rather
    than reached through the FK. Entries expire on a per-entry TTL while
    this log lives 90 days (§7.4), so a foreign key would either block
    the allowlist's own cleanup or dangle. `allowlist_entry_id` is kept
    as a nullable convenience for the window when both exist.
  - `fetched_at` records whether the minted paths were actually
    collected, which §3.3 is careful to distinguish from the device
    having booted the file.
  - **A 90-day horizon bounds age, not volume, and both keys on this
    route are attacker-chosen (§4.2).** The same unbounded-key-space
    concern §4.2 already prices for the denial *counters* applies to
    this *log*: a flood spread across many distinct serials or sources,
    each individually under its own rate budget, still writes one row
    per attempt for up to 90 days. §4.2's buffered-write fix (this
    section) addresses the write-cost half; the row-volume half is
    addressed the same way the counters are — high-volume unknown-serial
    floods collapse into a coarser aggregate row rather than one row per
    attempt, so a flood shows up as a large count on one row instead of
    as rows enough to matter.
- `provisioning_log_artifacts` junction: `(log_id, role, artifact_id)`,
  with `log_id REFERENCES provisioning_log(id) ON DELETE CASCADE`.
  §3.4 says the log records "the `artifacts.id` it served", singular,
  and the flow serves several. The junction is what makes "which bytes
  was this device offered" answerable for a config *and* an image
  without a column per role on the log row.
  - The cascade is not optional under §5's own `foreign_keys=ON`
    pragma: without `ON DELETE CASCADE` on the `log_id` side, purging a
    `provisioning_log` row that still has junction children raises a
    constraint violation instead of succeeding, which is nearly every
    "resolved and offered" row §7.4's 90-day purge is supposed to
    collect. The `artifact_id` side stays `RESTRICT` (the default) —
    artifact rows are retained-while-referenced (§7.4), and a dangling
    junction row pointing at a purged artifact would be the opposite
    bug, silently answering "which bytes" with nothing.
- The provisioning log and the software-lifecycle job tables share a row
  shape, a retention-purge helper, and a viewer component per §3.4,
  distinguished by kind rather than merged into one timeline.
- `settings` table: `key`, `value`, `updated_by`, `updated_at`, plus an
  append-only, never-purged `settings_audit` recording every change with
  its old and new value. §4.4 names five OIDC settings and
  `local_accounts_enabled`; §8.1 adds the target CIDR and the
  stage-phase concurrency cap; §4.3 makes managing all of it an admin's
  job — and none of it had anywhere to live. There are no transport
  keys: push is the only transport (§4.3.1). A returning pull transport
  (§10) would add a distribution host, user and credential source here,
  and repointing that host would become the highest-yield settings write
  in the system.
  - The audit table is not symmetry for its own sake. The settings here
    decide who is an admin and which addresses a run may target, and a
    change to either that leaves no record is exactly what an audit
    trail exists to prevent.
  - Secrets stay out of this table. The OIDC client secret is supplied
    as a systemd credential or environment, not a row, or the settings
    page becomes a plaintext secret store readable by any admin. Note
    what this costs and accept it: changing it is a unit credential
    change and a restart, not a form submission.
  - **"Append-only" needed to be a constraint, not a description.**
    `users.auth_backend`'s field pairing is enforced with a real `CHECK`
    specifically because, in this document's own words, "a rule that
    lives only in the paragraph describing it is a rule the first
    migration breaks" — `settings_audit` was held to a lower bar than
    that until now. `BEFORE UPDATE` and `BEFORE DELETE` triggers that
    raise unconditionally back the append-only claim at the database
    level, closing the gap between a Flask-side compromise being able to
    forge a queued row (§9.2's stated baseline) and being able to
    silently rewrite the record an admin would check *after* an
    incident to find out what happened — which is a materially larger
    yield than the baseline currently prices (§7.2 extends the same
    reasoning to `device_host_keys` and this table's own security-
    relevant rows).
- `user_admin_audit` table: `id`, `occurred_at`, `actor_user_id`,
  `target_user_id`, `action`, `detail`, guarded by the same
  unconditional `BEFORE UPDATE`/`BEFORE DELETE` triggers as
  `settings_audit`, for the identical reason. Who created an account, who
  changed a role, who issued or revoked an enrollment token, who
  deactivated whom. §4.4's mixed-backend confused deputy is exactly the
  kind of event that should not be reconstructable only from inference,
  and a system whose stated purpose is an audit trail should not have
  user administration as its one unrecorded operation. Never purged; it
  is a few rows per person per career. The `nethub-admin` break-glass CLI
  (§4.4) writes here too — it grants nothing host access didn't already
  imply, but it is still the one path that creates or reactivates an
  admin outside the UI, and that is precisely the kind of action this
  table exists so that it is never the unrecorded one.
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
  act on until the cookie expires on its own. Purged on expiry, and not
  an audit record — `user_admin_audit` above is where the durable
  statements live. `id` is `sha256(token)`, not the token (§4.5): the
  cookie carries the token and this table never does, so a read-only
  exposure of the database is not equivalent to holding every admin's
  live session.
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
key/version/file/checksum → bytes stream to a temp file in the store,
hashed incrementally as they're written → digest compared against the
submitted claim → the two uniqueness constraints checked (§5) → the file
is linked into place with `os.link` and the artifact row committed as
`published` in the same request that received the upload → the result is
surfaced immediately, with no further step and nothing left to poll.

Publishing is synchronous, in-request work rather than a dispatched job,
and that is a real simplification over an earlier revision of this
design rather than an oversight. That revision needed a job because the
store might be a remote machine and Ansible was the remote-operation
tool already to hand to reach it; with the store local and owned by
NetHub outright (§3.3), publishing is a local filesystem operation with
nothing left to dispatch to. There is consequently no publish job row,
no rendered registry file, no lock, and no git commit (§5, §7.1, §7.2
say what replaced each of those). What this does *not* change is the
separation from installing: publishing still touches no device, and
installing is the only phase-execution dispatch in the system, which
makes the two harder to confuse rather than easier.

**Day-2, upgrade** (behind admin auth): admin uploads an upgrade request
→ request validated (supported platform, every bundle key resolving to a
`published` artifact row, every request field within its character
allowlist, `ansible_host` an IP literal inside the target CIDR *with an
already-confirmed `device_host_keys` row* — an unconfirmed address is
rejected at this step rather than accepted and TOFU'd later, no other
connection variables and no template expressions) → run and per-host
rows written, request document and digest recorded → device credential
collected at the submit gate and probed against one host in the target
set before dispatch, refusing to start on failure rather than
discovering a bad password one device at a time → pre-check phase
dispatched against a NetHub-rendered inventory, asserting privilege 15
among its checks (§4.3) → per-host results recorded and the run parks at
`awaiting_approval` → admin approves staging, supplying the credential
for that execution, with each target host's confirmed-by identity and
confirmation date shown on the approval screen → the image is pushed
(§4.3.1): the device's SCP server is enabled if not already (prior
state captured first), the image goes over Netmiko's
`CiscoIosFileTransfer` under the same credential on a second SSH session
opened through NetHub's own pinned connection path, and the SCP server
is restored to its prior state with the restore confirmed or the host
fails → the image is verified against its SHA-512 on the device → admin approves activation →
devices reloaded in serial
waves → verification runs without a gate →
optionally, admin approves cleanup, or declines it and closes the run →
per-host outcomes and phase logs surfaced in the dashboard.

A credential is collected at each gate rather than once at submit, and
§9.1 explains why that is the price of "no device credential is ever at
rest". An operator can cancel a run at any gate, and during a phase,
where it stops further hosts from starting; §8.1 says what that costs at
each one.

All three flows above are the success path. What happens when an
individual step fails is §7. Because the upgrade flow spans a database
and a fleet of devices that reboot, "what if it fails here" has a
different answer at almost every arrow; publish, now synchronous and
scoped to one filesystem plus one database row, has a much shorter list
of ways to fail partway (§7.1, §7.2).

### 6.1 The API surface

The workflows above are the thing being built; the routes are how the
frontend reaches them, and leaving them unstated means the dashboard and
the backend are designed against different assumptions about what an
error looks like. This is not a full specification — request and
response bodies belong with the implementation — but the shape is a
design decision and belongs here.

**`POST /provision` is the only route with no session and no login
form.** It takes the claimed serial and whatever the device reports
about itself, and answers `200` with the minted fetch URLs (§3.3)
whether or not the serial was allowlisted, because §4.1 requires known
and unknown serials to receive the same response shape. It never returns
`403`, never returns a different body length for a known serial, and
never varies measurably in time. `429` is the one other status it emits,
on the rate limit.

**It is not, however, the only route reachable without an authenticated
session — the earlier claim that it was overstated the case.**
`POST /login`, the OIDC `/auth/callback`, and enrollment-token
redemption (§4.4) are pre-session by construction, since their entire
purpose is to establish one. §4.2's rate-limit/counter/alert apparatus
is scoped to phone-home and does not cover them by default, and in a
deployment with `local_accounts_enabled` the login form is the
highest-value pre-session target in the system. §4.2 is corrected to
extend that machinery — buffered, bounded-key-space counters and a
high-signal alert — to these routes as well, keyed on username plus
source prefix rather than on serial plus source.

**Everything else is session-authenticated, CSRF-protected on writes
(§4.5), and falls into four groups.** Allowlist management (`GET`/`POST`
`/allowlist`, `POST /allowlist/<id>/rearm`, `DELETE /allowlist/<id>`, none
of it built yet, day-0 being entirely unimplemented), joined by
`GET /hostkeys`, `GET`/`POST /hostkeys/scan`, `GET /hostkeys/scan/<id>`,
`POST /hostkeys/confirm`, `POST /hostkeys/<id>/delete`, and
`GET /hostkeys/history/<address>` for the device-host-key scan/confirm
flow §4.3.1 requires before an address can be targeted by any run — a
deliberately separate action from anything a run's own submit or
approval does. Artifacts and publishing (`GET /artifacts`,
`GET`/`POST /artifacts/new` for the streaming upload — which is
publishing itself, synchronously, per §6 above — `POST
/artifacts/<id>/delete`, refused while a live run needs the artifact).
The drift check §5's `check_store()` describes is a CLI command,
`flask --app nethub check-store`, not a route: it hashes every image,
and long work does not belong in a request handler. Upgrade runs (`GET /upgrades`,
`GET`/`POST /upgrades/new`, `GET /upgrades/<id>`,
`POST /upgrades/<id>/approve` carrying the phase and the device
credential, `POST /upgrades/<id>/decline-cleanup`,
`POST /upgrades/<id>/cancel`). Administration (`GET /users`,
`GET`/`POST /users/new`, plus `GET /profile` and
`POST /profile/device-username` for a user's own device-username
mapping). Role gates the administration group to `admin`; the rest
accept `operator` once roles exist (§4.3) — today, with no role model
built, every authenticated user can reach everything an operator could.

**Long operations return `202` and a job id, never a held connection**
is the target contract; today's routes are server-rendered pages that
redirect after a `POST` and ask the operator to reload rather than
returning a JSON envelope a frontend polls. The underlying rule §3.2
states is honored either way — a route that would trigger device work
returns as soon as the job row is written, never after device I/O
completes — but the API shape described here (a `202` plus a job id,
`GET /jobs/<id>` and `GET /runs/<id>` returning `status`,
`awaiting_phase`, `heartbeat_at`, and a stalled flag per §7.3) is target
design for a richer frontend, not what a request against today's routes
receives back. §3.1's frontend section is the place that decision
belongs; this section states the contract it would need to honor.

**One error envelope, speaking §7.3's vocabulary, is the same target/
actual split.** The row itself always carries `failure_stage` and
`error_summary` rather than a separately-invented set of internal
strings — that part is built, and §7.3's rule that the credential path
emits fixed enums and never lets an exception object cross into either
column is enforced today. Whether those values reach the browser as a
structured API response for a frontend to render, or as text interpolated
into a server-rendered page, is the same target-vs-actual distinction as
the paragraph above.

Approval endpoints deserve one explicit note, because they are the
routes that carry a password and reload a fleet: `SameSite=Strict`,
CSRF-checked, `autocomplete="new-password"` on the credential field, and
no request-body logging at the proxy (§9.2). A `409` rather than a
second queued row is the correct answer to the second admin clicking
approve, per §5's uniqueness constraint.

## 7. Failure, Concurrency, and Staleness

§6 describes what happens when everything works. This section describes
what happens when it doesn't, which for a system writing to a database
and a filesystem with no shared transaction, and separately driving a
fleet of devices that reboot, is most of the design.

### 7.1 One writer per byte, and why a lock stopped being the mechanism

An earlier revision of this design had publish render a git-committed
registry file, serialized by a single advisory `flock` taken for the
whole render-commit sequence, because the things that could concurrently
touch that tree were not all inside one process — a retention purge, an
operator's shell, a second NetHub started by accident mid-deploy, and
the automation container itself were all outside it. That file, and the
lock protecting it, are both gone (§5): with no rendered projection to
diverge from, there is nothing left for a `flock` to serialize writers
to. What replaced it is narrower and lives at the point bytes actually
land on disk, in `artifacts.ingest()` — see §7.2.

The publish half of the "one run at a time" rule in §3.2 is gone with
it, because publish is no longer a dispatched run at all; it is ordinary
request-handling work, and ordinary concurrent HTTP requests are exactly
what §7.2's constraints are built to survive. The rule that *is* still
true, and still named explicitly rather than left implicit, is that the
sibling executes one phase execution at a time (§3.2, §8.1). Concurrency
*between executions* buys nothing at this scale, since upgrade dispatch
is occasional and admin-initiated, and it costs the entire class of
interleaved-device-work bugs: two runs never touch devices at once. That
queue is serial by construction rather than by locking discipline, the
same reasoning the old registry lock used, applied to the one thing left
in the system that still needs it. *Within* one execution, hosts are a
different matter, and since PLAN.md WS-9 every phase but activation runs
several of them at once (§8.1). That parallelism is confined to device
I/O in worker threads; the rows are still written by the one thread
that holds the database session, so the job row keeps a single writer.

### 7.2 Ingest isn't atomic across two stores, so the ordering is chosen deliberately

The day-2 publish touches two stores with no transaction spanning them:
the bytes on disk and the artifact row in the database. A crash between
the two steps leaves a visible inconsistency — bytes on disk with no row
naming them, or (if the ordering were reversed) a row naming bytes that
were never actually written. The design does not try to make the
sequence atomic. It chooses an ordering and a linking primitive that
make the failure modes each step can leave behind either harmless or
self-correcting:

- **Every check at ingest's top runs before the upload streams, and
  proves nothing by the time the bytes are moved.** The uniqueness
  checks (§5: `UNIQUE(filename)`, `UNIQUE(platform, bundle_key)`) are
  read at the start of the request, but a 1.2 GB upload takes long
  enough that a second concurrent upload sharing a filename can pass the
  same check before either one finishes streaming. This is not a
  hypothetical: it happened, and it corrupted a store before the fix
  below. Two uploads sharing a filename both reached the final move, and
  the loser overwrote the winner's already-committed bytes with its own
  — before hitting its own `IntegrityError` — leaving the winner's row
  recording one artifact's SHA-512 against the *other* artifact's
  content, silently breaking the "hashed once, consumed three times"
  chain of custody §3.4 depends on.
- **The final move is `os.link`, never `os.replace`.** `os.link` raises
  `FileExistsError` instead of silently overwriting the destination.
  Both the temp path and the final path are on the same store by
  construction, so a hard link is always available, and the race that
  used to corrupt a store now fails loudly on whichever request loses it
  instead of destroying the winner's bytes.
- **The database commit is wrapped too, and ordered after the link
  rather than before it.** The schema's uniqueness constraints fire only
  *after* the bytes are already in place, so a file on disk with no row
  accounting for it would otherwise block that filename for every later
  upload forever. Losing the race removes its own bytes as part of the
  same wrapped operation, so the filename is free again for the next
  attempt.

Bounded rather than eliminated: the corruption this closes was real, and
its blast radius was bounded by a control one layer downstream rather
than by this fix alone — the device's own `verify /sha512` compares
against the row's digest at day-2 install time, so a corrupted store
failed upgrades outright rather than installing the wrong bytes onto a
device. That is the control this section's fix removes the need for, not
one this section duplicates.

**Tamper-evidence used to stop at a rendered file, and now there is no
rendered file for it to stop at — which is a real regression worth
naming rather than a gap this document forgot to update.** The earlier
revision's clean-tree check and whole-file re-render
make a modified *file* detectable and correctable. The *table* is
covered by neither. Framed only around `artifacts.sha512` (the digest
devices verify against, §3.4), this understates the exposure: the same
Flask process that could edit that row also writes `device_host_keys`
(§4.3, §5) and the security-relevant rows in `settings` (the target
CIDR and the OIDC admin group). A Flask-side RCE's real yield is not
"forge a queued row" (§9.2's stated baseline) or even "substitute an
image digest" — it is silently repointing the host-key pin an
approver's browser will show them at the next approval screen, or
widening the target CIDR, ahead of the exact moment that approver types
their own AAA password into the approval form (§9.2). That is a materially larger
yield than the baseline currently prices, and it is priced correctly
only once the tamper gap is understood to cover those tables and not
only the digest.

**There is currently no mitigation at all, and that is a real regression
from the git-commit-signing design this section used to describe, not a
detail this rewrite chose to drop.** The earlier revision signed registry
commits with a key Flask did not hold, so an audit copy could not be
rewritten from the web tier alone, and proposed extending that
signed-projection pattern to `device_host_keys` and the security-relevant
`settings` keys once they existed. With the registry file gone (§5, §7.2)
there is nothing left to sign a commit of, and nothing built has replaced
it: `device_host_keys`, `host_key_scans`, and `device_host_key_audit`
(§5) all guard against a *submitter* naming an unconfirmed or wrong
address, none of them against a *Flask-side compromise* silently rewriting
a confirmed pin, and there is no settings table yet to protect. An append-only hash chain over
`artifacts`, or a signed/countersigned projection of the security-relevant
rows once `settings` exists, is tracked as open in §10 rather than
described here as built. The honest statement is unchanged from before,
only weaker in degree: NetHub detects drift between the row and the bytes
on disk (§7.2's own mechanism) and detects nothing at all about a
consistent lie told from the row itself outward — a Flask-side RCE's real
yield is not "forge a queued row" (§9.2's stated baseline), it is
silently repointing a confirmed host-key pin, or widening the target
CIDR once one exists, ahead of the exact moment an approver types their
own AAA password into the approval form. A returning pull transport (§10) would
add a worse one: naming a distribution host the attacker controls, so
the fleet authenticates to it.

### 7.3 Job lifecycle and failure semantics

`upgrade_phase_jobs.status` is a state machine with explicit terminal
states rather than a success flag. `host_key_scans` reuses the shape with
a deliberately smaller vocabulary of its own (below); there is no longer
a publish-side job status to share it with, since publish is synchronous
(§7.1, §7.2):

| status | meaning |
| --- | --- |
| `queued` | job row written, phase execution not yet started |
| `running` | phase execution in progress; `heartbeat_at` is being updated |
| `succeeded` | phase completed on every targeted host |
| `failed` | phase ran and a host reported failure |
| `timed_out` | exceeded the per-job wall-clock limit and was killed |
| `abandoned` | was `running` when the process died; assigned by the startup sweep |
| `cancelled` | a human asked for it to stop, and it stopped |
| `expired` | sat `queued` past its `deadline_at` and was never claimed |

The last two are deliberately not folded into `abandoned`. One word for
"the machine crashed" and "a person pressed stop" would merge two events
with different follow-ups — a crash is investigated, a cancellation is
not — and it would do so in the column an operator scans first.
Splitting the vocabulary is what keeps `abandoned` diagnostic.
`host_key_scans` carries only `queued`/`running`/`succeeded`/`failed`/
`abandoned` — no `cancelled`/`expired`/`timed_out` — because a scan has
no approval gate to expire at and nothing to time out against beyond the
connect timeout itself (§5).

**A row in a terminal `status` doesn't get written to again, and that
needs a trigger behind it, not just a habit.** `upgrade_phase_jobs` gets
a `BEFORE UPDATE` trigger that raises if the row being replaced already
had a terminal `status`
(`succeeded`/`failed`/`timed_out`/`abandoned`/`cancelled`/`expired`).
Without it, nothing distinguishes "this row has always accurately
described what happened" from "this row was mutated after the fact,"
which is exactly the property the audit-table triggers elsewhere in §5
exist to guarantee and this table shares the same requirement for.

`failure_stage` records *where* it stopped, and `error_summary` carries a
short operator-facing reason. With no publish job status left to share a
vocabulary with, `failure_stage` for a phase job is free to be exactly
the phase-specific vocabulary this design always needed: `credential`,
`connect`, `hostkey`, `privilege`, `precheck`, `transfer`, `checksum`,
`install`, `reload`, `postcheck`, and `internal` for an error in NetHub's
own code rather than anything the device or the network did (recorded when
the sibling recovers from an exception nothing else handled; before it had a
word of its own this read as `connect`). That it no longer has to avoid
colliding with a publish-side `promote`/`render`/`commit` set is a side
effect of that set no longer existing, not the original reason for
keeping the vocabularies separate — the original reason survives anyway,
since §8.1's phases are themselves named `stage` and `verify`, and a
value called `failure_stage` reading `phase='activate',
failure_stage='stage'` would still be ambiguous on its face if the two
vocabularies were ever merged. `privilege` is worth calling out: it means
the submitter's account is not at level 15 (§4.3), which is a deployment
fault rather than a device fault, and it surfaces at pre-check rather
than part-way through a wave.

Both fields are written by the code paths that sit closest to the
secrets, and `error_summary` is free text retained for a year (§7.4). A
stray `str(exc)` from the credential path or a runner exception is
therefore a durable credential leak with no other symptom. The rule is
that the credential path emits fixed enum strings into these two columns
and never lets an exception object cross that boundary; the detail
belongs in the scrubbed log, not in the row.
Without them, a failed job in the dashboard says only that something
went wrong, leaving the operator to go reading `log_path` by hand at
exactly the moment they need an answer quickly.

Several rules fall out of this:

- **A crashed job doesn't stay `running` forever.** `heartbeat_at` is
  updated during the run **on its own timer inside the sibling's
  monitoring loop, independent of any per-host event boundary** (built in
  PLAN.md WS-9: the thread driving a phase writes it every
  `phases.HEARTBEAT_INTERVAL`, 30 s, while hosts run in worker threads;
  before that it moved only between hosts) — that
  precision matters because §8 gives the stage phase a per-host bound
  derived from `file_size` specifically because a single job-level bound
  is the wrong shape for a phase moving gigabytes, and the same reasoning
  applies to heartbeat freshness. A single transfer of a
  multi-hundred-megabyte image is one long-running call — a Netmiko SCP
  push; driving `heartbeat_at` off per-host
  start/end events rather than a wall-clock timer would render a
  slow-but-healthy transfer as *stalled* for its whole duration,
  undermining the distinction the next paragraph is built on. A sweep at
  startup moves any `running` job from a *different* `runner_instance_id`
  than the current sibling's to `abandoned` (§5). Keying on the instance
  UUID rather than on a PID is what makes that test correct: a PID is
  reused across container restarts and arrives as a meaningless number
  across PID namespaces. A job stuck at `running` is otherwise
  indistinguishable from a slow one, which means nobody investigates it.
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
  history shows both. The same holds for retrying the hosts that failed a
  phase (§8.1): an approval, a new attempt, never automatic.
- **A cancel is a request in a column, not a signal.** §9 makes the job
  row the only control channel, so stopping something is
  `cancel_requested_at` being set by Flask and polled by the sibling
  (§5) rather than a message or a kill. The sibling checks it between
  hosts and at phase boundaries — never mid-device — and the run reaches
  `cancelled`. What that means differs by phase and is §8.1's subject:
  cancelling a stage is safe *for the fleet* — an in-flight host may
  still need its SCP server restored, so the sibling lets a host already
  mid-push reach its own `try`/`finally` before honoring the cancel,
  rather than killing the sibling process outright (§4.3.1) — cancelling
  an activation mid-wave leaves the fleet split across two versions, and
  the UI says so before it accepts the request. A `queued` job cancels
  cleanly by never starting.
- **`queued` is bounded too.** §8's wall-clock timeout starts at *run*,
  so a job that never starts has no bound at all. `deadline_at` (§5) is
  set at enqueue rather than at dispatch, and a job that passes it
  without being claimed goes to `expired`. This is the same bound §9.1
  puts on a held credential, arriving from the queue's side. The
  sibling's dispatch loop checks for a queued `host_key_scans` row ahead
  of a queued `upgrade_phase_jobs` row on every iteration, rather than
  merging the two into one ordered queue: a scan takes seconds and an
  admin is watching a result page for it, so it should not queue behind
  a phase job that might be a 15-minute stage already in flight. A
  consequence worth stating plainly: `expired` is a real state in
  `upgrade_phase_jobs`' vocabulary but effectively unreachable in
  practice, since the credential TTL (§9.1,
  measured in minutes) will almost always expire and fail the phase
  `failure_stage: credential` before any sane `deadline_at` would — not
  a bug, just a reason not to spend effort tuning `deadline_at`
  precision for phase jobs specifically.
- **A failed ingest doesn't leave a partial artifact.** §7.2 already
  covers this at the mechanism level: the row commits only after the
  bytes are linked into place, so a failure at any point before that
  leaves no `published` row and no orphaned filename claim, and a
  previously published row for the same bundle key is untouched. There is
  no separate "failed publish" state to reason about, because there is no
  longer a window between "bytes moved" and "row committed" wide enough
  to observe as one.

**All three machines, with the actor on every edge.** The table above
enumerates job status and the rest of the document named the other two
without listing them, which is how `upgrade_runs.state` came to have
exactly one documented value. Naming the actor matters as much as the
states: §9's whole architecture is a statement about which process may
write what, and a transition table that does not say who performs each
edge cannot be checked against it.

*Job status* — `upgrade_phase_jobs`, and `host_key_scans` sharing the
same shape minus the three states it has no use for:

| from | to | actor |
| --- | --- | --- |
| — | `queued` | Flask, on submit or on approval |
| `queued` | `running` | sibling, conditional claim that also clears the sealed credential (§9.1) |
| `queued` | `cancelled` | sibling, seeing the cancel column |
| `queued` | `expired` | sibling, past `deadline_at` |
| `running` | `succeeded` / `partial` / `failed` | sibling, on phase completion (every host passed / some did / none did); `failed` also when the credential cannot be opened or its own code raises |
| `running` | `timed_out` | sibling, at `deadline_at` |
| `running` | `cancelled` | sibling, seeing the cancel column before starting a further host; hosts already running finish first |
| `running` | `abandoned` | sibling's startup sweep, foreign `runner_instance_id` |

Flask writes only the first edge. Everything after dispatch is the
sibling's, which is §9's rule expressed as a table rather than as prose.

*Run state* — `upgrade_runs`:

| from | to | actor |
| --- | --- | --- |
| — | `pre_checking` | Flask, on submit (pre-check needs no gate) |
| `pre_checking` | `awaiting_approval` | sibling, on pre-check succeeding; sets `awaiting_phase` and `gate_expires_at` |
| `pre_checking` / `running` | `failed` | sibling: a phase leaving no host able to carry on, a credential it could not open, or an unexpected error in its own code |
| `pre_checking` / `running` | `failed` | sibling's startup sweep, abandoning a phase nobody approves (`precheck`, `verify`) |
| `pre_checking` / `running` | `cancelled` | sibling, seeing the cancel column before claiming the job or before starting a further host |
| `pre_checking` / `running` | `expired` | sibling, finding the queued job past its `deadline_at` |
| `awaiting_approval` | `running` | Flask, on approval or on a retry of failed hosts (writes the phase job row; leaving the gate is a conditional update, so only one request can) |
| `awaiting_approval` | `expired` | sibling, past `gate_expires_at` (checked every loop) |
| `awaiting_approval` | `cancelled` / `completed` | Flask: an operator cancels, or declines the optional cleanup gate |
| `running` | `awaiting_approval` | sibling, at the next gate, including after a `partial` phase or a retry that failed again while other hosts carry on; or its startup sweep, parking an abandoned `stage`/`activate`/`cleanup` for re-approval, or an abandoned retry of `precheck`/`verify` back at the gate it was made from |
| `running` | `completed` | sibling, after cleanup succeeds |

`activate` succeeding queues `verify` directly, so the run stays `running`
across that boundary. Flask refuses an approval past `gate_expires_at` even
if the sibling has not yet run its check, so the TTL holds while the sibling
is down; it does not write the `expired` edge itself.

**The job row is created with its credential already in it.** Flask
flushes the new `queued` row to get its id, seals the credential into it
(§9.1), and commits both at once. The sibling can only see committed rows,
so there is no moment at which it can claim a job whose credential is not
there yet, and a failed commit takes the ciphertext with it.

**`pre_checking` has the same exits as `running`.** It is a distinct
literal only because pre-check needs no gate. The table lists the two
states together wherever the sibling treats them alike, so a pre-check
that fails, is abandoned, cancelled or expires has a documented way out,
and a run cannot get stuck at `pre_checking` and escape §7.4's purge.

Declining cleanup is an edge rather than an absence, which is what stops
"awaiting cleanup approval" and "finished, cleanup declined" from being
the same row.

*Per-host state* — `upgrade_run_hosts.state`: `pending` → `precheck_ok`
→ `staged` → `activated` → `verified`, with `failed` reachable from any
of them and `skipped` for a host excluded by a pre-check assertion. The
sibling owns every edge; Flask never writes this table after the run's
host rows are created. The per-phase detail lives in
`upgrade_host_phase_results` (§5), because this column is a cursor.

A phase runs on the hosts whose cursor sits just before it (`pending` for
pre-check, `precheck_ok` for stage, and so on; cleanup runs on and leaves
hosts at `verified`), not on "every host that has not failed". So a phase
re-approved after it was abandoned skips the hosts it had already
finished, rather than issuing a second `install add` to a switch that has
just reloaded. A retry adds one edge, `failed` → the cursor before the
retried phase, which the sibling writes for the hosts that failed that
phase when it starts the retry job (`is_retry`, §5).

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
- **`upgrade_runs`** and their host and phase rows: 365 days, purged as
  a unit with the run, matching the operational question they answer
  ("what did we install, where, and who approved it, and when").
  `host_key_scans` is not part of this horizon — it is a dispatch record
  rather than a publish or install audit trail, and is cheap enough to
  retain on its own shorter schedule or purge on use via `consumed_at`.
- **Allowlist entries**: expire on their per-entry TTL (§4.1), not on a
  global schedule.
- **Artifact rows**: retained while referenced. A row that is
  `published`, or that any non-purged run's `upgrade_run_hosts` snapshot
  points at, is never collected regardless of age. That's a hard
  constraint rather than a policy knob. Collecting a published artifact
  would break day-2 dispatch resolving it, and collecting a referenced
  one would leave the audit trail pointing at nothing. This rule is for
  the automated purge (not built). An admin's manual delete is allowed
  once no live run references the row; finished runs keep their own
  snapshot of filename, digest, version and size, and their
  `artifact_id` becomes null.
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

**The purge runs in the sibling.** It deletes artifact bytes and rows,
run/phase/host rows, and provisioning-log rows on the store and in the
database, and neither is work for the process holding the unauthenticated
route (§3.2) — the same reasoning that keeps every other device-touching
or filesystem-mutating operation out of Flask (§9). It runs on a timer
rather than at startup, so a long-lived deployment collects on schedule
rather than on restart.

`device_host_keys` rows are not purged on a schedule. They are a
security control rather than a log, and expiring one silently converts a
fail-closed mismatch back into a first-contact prompt — which is the
exact moment an attacker would want. A row is removed when an operator
retires the address, deliberately and attributably — logged to
`device_host_key_audit` (§5), which is itself never purged for the same
reason `user_admin_audit` isn't.

Purging an `upgrade_phase_jobs` row also removes its `log_path` file in
the same operation. A retention helper that deletes rows and leaves logs
behind produces an ever-growing directory of orphans nothing can
attribute — and the phase model produces one such file per phase
execution per run, so the coupling matters more here than it would with
a single log per operation.

Staleness is a property of the fleet, not of NetHub. NetHub's own
records describe what was published and what a run targeted; nothing in
NetHub records what a device *currently* runs, and per §2 nothing
should, because that is inventory. An upgrade run is not the exception it
looks like: `upgrade_run_hosts` records the version a device reported
*during that run*, which is a property of the job and expires with it,
and those rows are deliberately never rolled up into a current-state view
per device. That roll-up is precisely the line between a job record and
an inventory. The consequence is that NetHub cannot tell an operator
which devices are behind, only what the current target is and which jobs
ran against it. Anything resembling fleet drift reporting has to come
from a dedicated fact-gathering pass — `scripts/check_device_facts.py`
today is a manual, per-device version of that, run and replayed by hand
rather than a scheduled sweep — not from a persistent view this design
refuses to build.

**Kea is a third store, and §7 was written as though there were only
two.** The database and the artifact store on disk are handled carefully
above (there is no longer a git repository in the mix, §7.1/§7.2); the
DHCP reservations §4.1 relies on are mentioned nowhere in this section.
They are derived data with the same drift problem: a reservation that
outlives a consumed or expired allowlist entry silently re-opens gate 1 —
the gate §4.1 introduced specifically so that phone-home would not be the
only one — and it does so invisibly, because nothing compares the two.
The fix is §7.2's own before-and-after ordering discipline applied one
store over: the reservation set is *rendered whole* from
`allowlist_entries` rather than patched per entry, and reconciled against
Kea's configuration on startup and after every allowlist change. The
table wins there too. This does not make NetHub a DHCP manager any more
than owning the artifact store makes it a general file server; it makes
the reservation a projection rather than a copy.

**The sibling renders it, not Flask, and that follows from a rule §9
already states for a different store.** "After every allowlist change"
names a Flask write as the trigger, and Kea runs natively on the host,
outside the container units and outside §9.1's trust argument entirely
(§9). The natural reading — the process behind the only unauthenticated
route writes host-level DHCP configuration and reloads a native service
— is the same escalation §9 explicitly rejects for the Podman socket,
with the reasoning ("neither is work for the process holding the
unauthenticated route") sitting one section away and not yet applied
here. The sibling owns Kea rendering instead: Flask writes the
allowlist row exactly as before, and the sibling reconciles on a timer
and on a polled change signal, the same division of labor it already
has for the registry.

## 8. Device Layer and Dispatch

Ordinary Python driving Netmiko, in five modules under
`nethub/devices/`: `facts.py` (`show version`/`dir`/`show privilege`,
parsed with ntc-templates), `connection.py` (the only way any NetHub
process opens a device session, carrying the host-key pin and the
authentication mechanics from §4.3), `transfer.py` (`stage_image()` and
the SCP push from §4.3.1), `install.py` (activate/reload/
verify/cleanup), and `phases.py` (the per-host loop, the
exception-to-`failure_stage` mapping, and the rows). `nethub/sibling.py`
dispatches them and `nethub/upgrade_routes.py`/`nethub/upgrades.py`
create the rows they work from. **There is one phase-execution dispatch
in the system and it is the upgrade** (§8.1). Publishing used to be a
second one, dispatched against a distribution host that might have been
remote; with NetHub owning the store (§3.3) it is synchronous, in-request
work (§6, §7.1, §7.2), so there is nothing left for it to dispatch to.
The two operations remain firmly separate — publishing touches no
device, installing is the only thing that does — and they are now
separate in kind rather than two instances of the same mechanism: one is
a request handler, the other is a dispatched execution the sibling
claims from a queue.

Every phase execution carries a wall-clock timeout and is killed on
expiry instead of being allowed to hang indefinitely (`timed_out`, §7.3).
An execution that never returns is the failure mode a serial queue
handles worst, since one stuck job blocks everything behind it, so the
bound matters more here than it would with a concurrent runner.

**A single job-level bound is the wrong shape for a phase that moves
gigabytes.** Sizing one number for the slowest host on the narrowest
link means every fast host inherits its slack, and a timeout that
generous stops being diagnostic. The stage phase therefore carries a
*per-host* bound derived from `upgrade_run_hosts.file_size` against a
floor transfer rate, so a host that stalls fails in minutes while a
genuinely slow one is allowed the time it needs. NetHub has the size
authoritatively at dispatch (below), so this costs nothing to compute.
**This bound is Netmiko's own read timeout on the transfer call itself,
not a process-level kill of the sibling** — the distinction matters
because the stage phase brackets the transfer with a device-config change it must restore (§4.3.1), and a bound
implemented as a `SIGKILL` against the sibling process would routinely
bypass that restore on exactly the slow links this bound exists to
catch, turning the "accepted residual risk" §4.3.1 describes for a hard
kill into the common case rather than the rare one. Expiring the
read-timeout bound raises inside the adapter's own `try`/`finally` like
any other transfer failure. The job-level bound remains as the outer
backstop for everything else — a genuinely hung phase execution, not
this specific transfer. What is retained for diagnosis is written to
`log_path` and purged with the job row (§7.4). There is no directory
retained wholesale the way an earlier, EE-based revision of this design
worried about: nothing renders a directory for an execution to read in
the first place (§3.5), so there is no `env/extravars` to
accidentally park on disk for the row's full 365 days. The credential's
only on-disk form is the sealed column, cleared at claim (§9.1); in the
clear it lives only as the `PhaseContext` attribute §4.3 and §4.3.1
describe.

NetHub always populates `file_size` on the artifact row at ingest, and
snapshots it onto `upgrade_run_hosts` at submit (§5). Because ingest
measures the file in the same pass that hashes it (§3.4), the size is
authoritative before any device is ever contacted, which is what lets
§8's per-host stage bound be computed *at dispatch* — before any
connection opens and therefore before anything device-side could measure
anything.

**What the transfer layer trusts is the mount, not the snapshotted
column, and the snapshot is a cross-check rather than an input.**
`transfer.resolve_source()` measures the image itself with one `stat`
against NetHub's own copy of the published tree, and treats the
snapshotted `file_size` as something to *agree with* rather than depend
on: if the two disagree, the run stops before any transfer, raising
`TransferError` rather than proceeding, because the bytes on the mount
are then not the bytes the row describes and the device-side
`verify /sha512` would only discover that after moving several hundred
megabytes. This is a small, direct function rather than a discovery
cascade with a remote tier: there is no second machine to shell out to
and measure, for the same reason there is no `remote_dir` column
anywhere in the schema (§5) — the sibling reads the file off its own
local mount, so "the image is on the distribution host" names no second
filesystem NetHub would need to reach across. A design that assumed the store could be a genuinely separate
machine would need that remote tier back; owning the store outright
(§3.3) is what removes it.

### 8.1 Upgrade dispatch and the phase split

Publishing an image and installing it are two different dispatches. §6's
day-2 publish flow ends at a published artifact row, full stop; installing
is the one dispatch left in the system, run by the sibling against
`nethub/devices/phases.py`, that installs a published image onto a set
of devices. This is built and validated against real hardware, not a
hand-run stand-in — see "Device layer" below for the timings.

What a user submits is a request document, not an inventory: hosts, one
bundle key for the whole run, and a small closed set of typed knobs
(`nethub/upgrades.py`'s own docstring is the contract itself, having
moved there from `ansible/inventory/README.md` when build step 6 deleted
that layer). NetHub validates it and compiles the rest. The two things it
will not take are the reason for that indirection. User-supplied device
code is arbitrary code with the live device credential in reach — an
authenticated RCE primitive beside the unauthenticated route §4 spends
its length reasoning about; there is nothing left in the tree that
accepts one, since device work is `nethub/devices/`, closed at build
time because it is code in this repo rather than anything a request
selects. There is no longer a separate distribution credential to price
this against either (§4.3.1 removed it); the device credential §9.1
injects for the phase execution is the one that matters, and per-phase
collection bounds what it is worth after the phase ends. It does nothing
about what code could do with it while the phase is still running, so the
rule stands unchanged regardless. A user-supplied inventory or registry
entry would let a request name any filename against any SHA-512 and
bypass the `artifacts` table entirely, breaking §3.4's "hashed once at
ingest, consumed three times" at the third consumption — so a request
names a bundle *key*, resolved server-side to a row, never a filename or
a digest. Connection vars are withheld for a third reason: the device
username is the submitter's own identity read server-side
(`users.device_username`), and an identity the submitter could type
instead would not be evidence of anything. Two narrowings in the actual
implementation are worth naming as simplifications rather than
disagreements with the contract above: today's `submit()` takes one
bundle key for the whole run rather than one per host, and `flash_dir` is
not submittable at all (it defaults on the column) — both are additive
to widen later, needing no rule revisited.

**Every field a request *does* supply should be validated against a
character allowlist before it reaches a device command string, and this
is only partly built today.** §9.2 already states this rule for the
input closest to a secret — the character allowlist is applied to a
credential "before the credential reaches any variable or command
string" —
and `artifacts.py` enforces it for the fields it owns: `_SHA512_RE`
requires exactly 128 hex characters, `_BUNDLE_KEY_RE` restricts the
bundle key to a boring charset, and the filename is passed through
`werkzeug.secure_filename` at ingest, closing the injection path before
any of those values reach `install add file …` / `verify /sha512 …`
command strings at day-2 dispatch. `hosts[].name` (the request's
hostname field) is **not** charset-restricted in `upgrades.parse_hosts()`
today — it is deduplicated and rejected if blank, nothing more. That
matters because the design intends it to become a literal path component
(the config-backup filename, §5), where an unvalidated `../../` would
escape the intended directory; the risk is currently latent rather than
live only because the config-backup write path itself isn't wired up yet
(§5), and closing the charset gap belongs in the same change that wires
it up, not after.

**The target address is the one connection-shaped field a request does
supply, and saying "no connection vars" without that carve-out is wrong
in a way that matters.** A request has to name the devices it targets; an
address is not an identity claim and withholding it would leave nothing
to submit. But the field is the whole attack in §4.3's credential model —
naming a machine you control is enough to be handed someone's device
password — so it is accepted under constraints rather than trusted, and
`upgrades.check_target()` and `confirmed_key()` are exactly this,
enforced in code today rather than only argued here. It must be an
IPv4/IPv6 **literal** — a hostname would let the CIDR check and the
eventual connection resolve to different addresses at different times,
and would key `device_host_keys` on a string whose meaning can silently
change later (§4.3). It is validated against `DEVICE_TARGET_CIDRS` by
numeric comparison on that literal, which is a network boundary rather
than an inventory and so does not reopen §2's non-goal. And — the
constraint that closes the gap §4.3.1 describes rather than merely
raising its cost — the address must already have a **confirmed** row in
`device_host_keys` (§4.3.1); an address nobody has deliberately confirmed
cannot be submitted at all, which is what turns "an address NetHub will
dial" into "a device a human has already met and attested to," not
merely "a device NetHub happened to meet first during this run." The
distinction to hold on to is between vars that assert *who someone is* —
the device username, credentials — which a submitter never supplies, and
the address of the thing being acted on, which they must and which is
now verified before it's acted on rather than only afterward.

**There is no terminal to prompt at dispatch time, and the design was
never built any other way once the sibling model existed.** An
interactive tool run by an operator can stop and ask before touching a
device — `upgrade_cli.py` (§ "Manual escape hatch") does exactly that,
prompting for confirmation before every phase in its own `MUTATING` set.
Dispatched out-of-band by the sibling on behalf of a web session, there is
no stdin to answer on. Streaming a PTY to the browser would buy the
interactivity back at the cost of a general-purpose IPC channel between
Flask and the sibling, which §9 rules out: the job row is the only
channel, credentials included (§9.1), and a PTY stream does not fit
through it. So the interactivity is a UI concept instead: the run is split into
phases, each dispatched as its own phase execution, and the confirmations
become approval gates in the UI between them:

| phase | device impact | gate before it |
| --- | --- | --- |
| plan | none | *is the submit form* |
| pre-check | read-only | none; runs on submit |
| stage | writes flash, non-disruptive | approve: copy image |
| activate | reload, traffic loss | approve: reload |
| verify | read-only | none; runs on completion, on activate's credential |
| cleanup | removes inactive packages | approve: cleanup |

An approval is a row rather than a keystroke, which is the point of
doing it this way rather than with a terminal. "Who authorized the
reload of this device, and when" is a question the phase model answers
by construction; a terminal transcript is not an audit record.

What follows from the split:

- **The plan phase dissolves into the UI.** There is no `plan` value in
  the `PHASES` vocabulary at all (§5) — the request is validated and
  compiled at submit, `upgrades.submit()` writes the run and its host
  rows in the same transaction, and the plan a human reviews is simply
  the submit form and the confirmation screen rendered from those rows.
  The same reasoning means there is no separate summary step either:
  per-host results are rows (`upgrade_host_phase_results`, §5), so there
  is nothing left for a final summary pass to compute that the dashboard
  can't already read.
- **Pre-check, stage, verify and cleanup run several hosts at once;
  activation runs one at a time** (PLAN.md WS-9). `phases.execute_phase()`
  hands hosts to a thread pool of `PHASE_CONCURRENCY` workers (a
  deployment setting on the sibling, default 4); activation goes through
  the same code with one worker, because it reloads switches and a fleet
  is not reloaded four at a time. Copying is where a wave spends its
  wall-clock time (§8's measured push throughput, ~1.4 MB/s per device,
  made a serial stage the dominant cost of a large wave: about two hours
  for twenty switches), and copying to flash drops no traffic. The cap
  exists because with NetHub the sole source of the bytes (§3.3), an
  uncapped stage would push a gigabyte to every targeted device at once
  over whatever link separates NetHub from them. At the measured rate
  four devices cost that link about 6 MB/s; a site behind a narrow link
  should lower it. Three rules keep the parallelism narrow:
  - **Workers do device I/O and nothing else.** The thread holding the
    database session copies each host into a plain value (address,
    filename, digest, size, flash directory and the pinned host key)
    before handing it over, and writes every row itself. The pin is
    looked up there too, because activation's reconnect after the reload
    needs it from inside its worker.
  - **A password is tried once before it is tried in parallel.** The
    first host to reach its login goes ahead and the others wait: if the
    device accepts it they all log in, if it refuses no other host tries
    and the rest are recorded `not_attempted`, and if the host could not
    be reached at all the next one tries instead. Without that, a
    mistyped password would be refused on every host logging in at the
    same moment, toward the AAA lockout the next bullets describe.
  - **Cancel and the deadline stop further hosts from starting.** Hosts
    already running finish and are recorded (below), and the job's
    deadline stays sized as if hosts ran one at a time, which leaves it
    loose by up to the concurrency (`upgrades.PHASE_BUDGET_SECONDS`).
- **Almost no state has to cross a phase boundary, and this is built as
  designed.** `filename`, `sha512`, `version` and `file_size` are
  snapshotted onto `upgrade_run_hosts` at submit (§5) rather than
  re-resolved per phase, so a phase reads its own run's rows and touches
  `artifacts` not at all. What is left is device state — current version,
  free space — which is re-gathered per phase and *should* be: a
  pre-check from three days ago must not authorize today's reload. The
  split is cheap precisely because the expensive facts are snapshotted
  once. No credential of any kind crosses that gap: the device credential
  the stage phase needs is collected fresh at its own approval gate
  (§9.1), same as every other phase, and is not held while the run is
  parked, so the Monday-to-Saturday gap holds no live secret of any
  kind.
- **Activation re-checks what staging established, and the check that
  matters is built correctly.** Between the two phases a device may have
  been upgraded by hand or had its flash cleaned, so
  `install.assert_ready_to_activate` reopens with the "not already on
  target version" assertion and a confirmation that the image is still
  present and still verifies. This is §7.4's staleness question in its
  concrete form: the gap between phases is exactly the window in which a
  prior phase's findings expire.

  "Still verifies" means **re-running `verify /sha512`**, and this is the
  actual pre-activate check, not a `dir` presence test — a file of the
  right name and size is not the file staging checked, and the gap is
  measured in days. This is also the third consumption §3.4 promises for
  the ingest digest, so a downgrade to a presence test would have broken
  that invariant at the one point it is supposed to bind. There is a test
  asserting no `install add` is issued when the pre-activate check fails.
  One normalisation detail was worth getting right and initially wasn't:
  `assert_ready_to_activate` runs the caller's `sha512` through the same
  `transfer._normalise_digest()` `stage_image()` already uses, rather
  than comparing it raw against the device's own lower-cased echo — an
  uppercase or whitespace-padded digest used to stage successfully and
  then be refused at activate, with an error whose two halves differed
  only in case.
- **Two approvals are not two runs, and the row is where that is
  refused.** The serialization guarantee below is scoped to *execution*:
  it stops two phase executions overlapping, not two rows being created.
  Two admins on the approval screen both clicking "approve: reload" write
  two phase jobs, and the serial queue then runs them one after the other
  — the fleet reloads twice. `UNIQUE(run_id, phase, attempt)` (§5) is
  what makes the second click a refusal instead of a queue entry. An
  approval being a row rather than a keystroke is the reason this works:
  a keystroke has nothing to collide with. With retries (below) two
  requests at one gate can name *different* phases, which the constraint
  cannot see, so leaving a gate is also a conditional update on the run
  (`... WHERE state='awaiting_approval' AND awaiting_phase=:gate`, one
  changed row or a refusal), and a gate action is refused while any job
  of the run is queued or running.
- **A host that fails a phase does not fail the run** (PLAN.md WS-8).
  The phase ends `partial`, the run moves on with the hosts that passed,
  and the failed ones stay at `failed` with their result rows. At the
  next gate an operator can **retry** the phases that ran since the
  previous one (pre-check at the stage gate, stage at the activate gate,
  activate or verify at the cleanup gate) on the hosts that failed them.
  A retry is an approval like any other: it collects the retrier's
  credential and records `approved_by`, under the next `attempt`. The run
  then carries on from that phase as it did the first time — a retried
  activate is verified again, on the hosts it activated — and lands back
  at the same gate. A retry that fails again changes nothing else; the
  hosts stay retryable. A run fails only when a phase leaves no host able
  to carry on. There is no retry of cleanup, since no gate follows it;
  a cleanup failure on one host completes the run with that host marked.
- **A refused credential stops the phase at once.** Every host in a
  phase gets the same password, so a refusal on one is a refusal on all,
  and each attempt counts toward the AAA server's lockout: a mistyped
  password across a 40-host wave could lock the account out fleet-wide.
  The phase stops at the first `credential` failure, and the hosts it did
  not reach are recorded as failed (`not_attempted`, `failure_stage:
  credential`) so a retry with the right password picks them up. With
  hosts running in parallel that needs the login gate above, or every
  host logging in at that moment would be refused too.
- **Cancelling means different things at different phases, and the UI
  says which.** `cancel_requested_at` (§5) is polled by the sibling
  before it starts each host. Cancelling a `queued` phase stops it before it starts.
  Cancelling pre-check or verify is safe outright: both are read-only.
  Stage is not quite the same shape as those
  two: it's non-disruptive to the fleet's traffic, but push is the one
  thing in the system that mutates device configuration (§4.3.1's
  SCP-server toggle). Up to `PHASE_CONCURRENCY` hosts can be mid-transfer
  when a cancel lands (above), and the sibling lets every one of them
  reach its own `try`/`finally` and confirm its restore before honoring
  the cancel, rather than killing the sibling process outright: the
  cancel only stops further hosts from starting. Cancelling an activation mid-wave is
  *not* safe and is not presented as though it were — the devices already
  reloaded are on the new version and the rest are not, and the operator
  is choosing a split fleet over finishing the wave. That is sometimes
  the right call, which is why the button exists; the confirmation names
  the consequence rather than asking twice.
- **The serialization lock is held per phase execution, not per run.** A
  run parked at a gate holds no phase execution running against it, so
  §3.2's one-execution-at-a-time rule applies to phases rather than to
  runs. Otherwise a run awaiting approval overnight would block every
  other upgrade. Phase jobs use the shared status vocabulary and startup
  sweep (§7.3); awaiting approval is a state of the parent run, which by
  definition has no process to sweep.

**This is built, hardware-validated, and the one thing not yet exercised
is a full run driven end to end from the web app.** `facts.py`,
`connection.py` and `transfer.py` are exercised against a real Catalyst
9200CX on IOS-XE 17.12.06, push included: enable, transfer, confirmed
restore, `verify /sha512`, and the skip-if-already-staged path.
`install.py` closed the remaining gap with a full round trip on the same
lab switch — 17.12.6 → 17.12.08 → 17.12.6, both directions through
`stage_image` → `activate` → `wait_for_device` → `verify_upgrade` →
`cleanup`. Measured timings, worth planning against: staging 471 MB over
SCP took ~370s (1.28 MB/s); `install add … activate commit` ran
605–622s and does **not** drop the session partway — it returns `SUCCESS`
with the session still up and only then reboots; the reload took
228–238s against a 900s default deadline; `install remove inactive` took
~5s. Version comparison had to be normalised (`facts.same_version`) once
a real device reported `17.12.8` for a target declared `17.12.08` — a
naive string compare would have failed a *successful* upgrade. The
reload deadline has ample headroom at 228–238s against a 900s default;
untested is whether a stack or a slower chassis eats into that margin,
and whether a push over a genuinely constrained WAN link (as opposed to
the lab's own link) moves the bottleneck somewhere this timing table
doesn't cover. What remains untested beyond timing is a
stage/activate/verify/cleanup sequence driven in one sitting starting from a pre-check kicked off
through the web app rather than through `upgrade_cli.py` or a direct
call into `nethub/devices/`.

## 9. Deployment

- Podman Quadlet units for the Flask app, the sibling job runner, and the
  distribution container, which serves the day-0 subtree over plain
  HTTP, with no separate bootstrap container. There is no day-2 daemon
  at all: the store is local to this host and is NetHub's own (§3.3),
  and images reach a device by the sibling reading the published subtree
  directly and pushing from it (§3.5, §4.3.1). The HTTP daemon decides nothing:
  it serves one fixed path holding the generic script, plus a `mint/`
  subtree of one-shot symlinks Flask writes and reaps, and authorization
  for day-0 lives in the phone-home handler. Everything runs on one host,
  in separate units, under one rootless Podman user.
- **The Flask and sibling units share two volumes and nothing secret.**
  Both mount the database directory and the artifact store, with the
  shared SELinux label (`:z`): the private label (`:Z`) gives each
  container its own, so on an enforcing host the second unit to start
  relabels the directory and locks the first out of the database. They
  share configuration only for those (`nethub/shared_config.py`). The
  session-signing `SECRET_KEY` lives in the web tier's `config.py`, which
  the sibling never imports, so the sibling unit holds no key that could
  forge an admin session.
- **Upgrading NetHub is a new image and a restart.** Back up the database
  first, change the image tag in both units, restart. The web unit applies
  any pending migrations at startup and the sibling waits for it (§5). There
  are no downgrades: going back to an older image means restoring the backup
  taken before the upgrade, and an older image refuses to start on a
  database a newer one has migrated rather than guess at it.
- **DHCP integration is the deliberate exception**: it runs natively as
  its own service rather than as a unit, because it binds a privileged
  broadcast-facing port on the provisioning VLAN and is the one
  component whose job is to be reachable by a device that has no
  configuration yet. Containerizing it would buy uniformity and cost the
  straightforward host networking that role depends on. It touches
  neither the job queue nor the credential path in §9.1, so it is
  outside that section's trust argument entirely.
- **The nested-container question is settled in favor of the sibling
  process, and half of the original reasoning for it no longer applies —
  what survives is enough on its own.** Mounting the host's rootless
  Podman API socket into the Flask container would have given the
  process behind the only unauthenticated route in the system (§3.2) the
  ability to start arbitrary containers on the host, which was the
  original argument for a separate sibling. There is no such socket to
  mount any more: device work is plain Python calling Netmiko, not a
  container NetHub invokes, so "a Flask RCE becomes host-level container
  control" no longer describes anything real. What survives, and is
  enough by itself, is §3.2's plainer reason: Flask holds the only
  unauthenticated route, and a handler that opened a device session would
  hold it for minutes. So Flask writes a `queued` row and nothing else;
  `nethub/sibling.py` runs as its own Quadlet unit, watches for `queued`
  rows, and is the only component that opens a device connection. The
  Flask app never touches a device directly.
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

### 9.1 The control channel, and how a credential crosses it

§9 settles that the sibling exists. It leaves open how the two halves
exchange anything, and one exchange is hard: the device credential
arrives at a Flask request handler and is needed by a Netmiko session
opened inside the sibling. An earlier version of this section put it on
a second channel, a sibling-initiated Unix socket to an in-memory store in
Flask, on the grounds that the job row is durable and a secret should not
be. PLAN.md WS-7 replaced that with a **sealed credential in the job
row**, for reasons the socket itself supplied: a restart of the web
process lost every held credential and failed every approved phase that
had not started, and because phases run one at a time, approving a phase
queued behind a long one outran the store's time-to-live. The socket also
cost a `.socket` unit, a shim to stop gunicorn from serving HTTP on it,
and the rule that gunicorn run exactly one worker.

The earlier text dismissed an encrypted column because "the key would be
readable by Flask". That is true of a shared key and false of public-key
encryption. Flask holds only the sibling's **public** key and can seal but
never open; only the sibling holds the private key.

**First, this is a same-host question.** Every part of NetHub runs on one
host as separate Quadlet units. That is a deployment constraint rather than
a conclusion argued here, and §7 would force it anyway: the job store is
SQLite, whose locking is unreliable on network filesystems, and *both*
processes write it. *Separate* units matter as much as the shared host:
distinct PID namespaces are what prevent same-uid `ptrace` and
`/proc/<pid>/mem` access between Flask and the sibling, so the two must
never share a Quadlet `Pod=`.

**The job row is the only channel.** Status, `heartbeat_at`,
`failure_stage`, `error_summary`, phase transitions, approvals — and now the
credential, as ciphertext. There is no socket, no RPC, and no status or
control anywhere else.

**How it works** (`nethub/sealed_credentials.py`):

- On submit (for pre-check) and on each approval, Flask seals
  `{job_id, approved_by, username, password, expires_at}` with libsodium's
  sealed box (PyNaCl `SealedBox`) to the sibling's public key, and writes it
  to `upgrade_phase_jobs.sealed_credential` **in the same transaction that
  creates the `queued` row**. No committed queued job ever lacks its
  credential, which closes by construction the race where the sibling
  claimed a row before its credential was held.
- `approved_by` is the identity that supplied the credential: the approver
  for a gated phase, the submitter for pre-check, which has no gate (§8.1).
  `expires_at` is the job's `deadline_at`, so a credential waits exactly as
  long as its job may, and there is no separate time-to-live to run out.
- The sibling's claim reads the ciphertext and clears the column in the
  conditional update that claims the job (`... WHERE status='queued' AND
  sealed_credential = <what was read>`, one changed row or nothing). It then
  opens it and checks the job id and the supplying identity against the row,
  and the expiry. Any failure — nothing sealed, tampered or foreign
  ciphertext, another job's credential, another identity's, expired — fails
  the phase with `failure_stage: credential` and a fixed message. A job
  that reaches its deadline unclaimed ends `expired` with `failure_stage:
  credential` ("its credential was discarded unused").
- A CHECK constraint, `status = 'queued' OR sealed_credential IS NULL`,
  holds every writer to "ciphertext only while queued". Cancel clears it at
  once on the run's queued jobs; every path that ends a job before a claim
  clears it in the same update.
- `verify` has no approval, so nothing is sealed for it: it runs straight
  after `activate`, in the same sibling pass, on the credential activate's
  approval supplied (§8.1: "runs on completion").
- Both processes refuse to start on bad keys: Flask without a valid public
  key (before it migrates anything), the sibling when its private key does
  not match the public one.

**What follows for the operator, stated plainly.** The credential still
lives no longer than one phase execution (§4.3), so it is collected at each
approval rather than once at submit: submitting collects for pre-check,
approving the copy for stage, approving the reload for activate and the
verify that follows it, and approving cleanup for cleanup — up to four
password entries across a full run. What changed is that an approval now
survives a web restart and a long queue: the credential waits in the row,
sealed, until its job runs or its deadline passes.

The ergonomic cost is not the only cost: training operators to type an
enable-capable AAA password into a web form four times a run makes the
approval gate a high-value phishing target. That argues for an approval
screen that is hard to clone convincingly, and for §4.3's pre-dispatch
credential validation being visible to the operator.

### 9.2 What sealing does and does not protect

The threat model, stated so it can be checked:

- **At rest: ciphertext only, and only while the job is queued.** The
  device credential reaches disk as a sealed box in one column of one row,
  from the approval until the sibling claims the job or the job ends. It
  never reaches disk in the clear, and the column is null on every row that
  is not queued (the CHECK constraint above).
- **Who can decrypt: the sibling's private key, and nothing else.** Flask
  cannot open what it sealed. The private key lives only in the sibling
  unit, as a systemd credential or a read-only file mounted into that
  container alone, and never in an environment variable.
- **A copy of the database alone reveals nothing** — a backup, a stolen
  file, a WAL fragment. Recovering a password needs the database *and* the
  sibling's private key.
- **A compromised Flask still sees passwords as they are submitted**,
  exactly as before: they arrive in its request handlers. Sealing protects
  the stored copy, not the web tier. The baseline for a Flask-side
  compromise is therefore unchanged: it can forge a queued row, and it
  reads the next password an approver types. §7.2 prices what else it
  can reach.
- **A sealed box proves nothing about who sealed it.** Anyone holding the
  public key — which is public — and able to write the database can plant a
  credential. Binding the job id and supplying identity stops a sealed blob
  being copied onto another job's row; it does not stop a forgery. A forger
  must supply the password themselves, so learns nothing by doing it, and
  can only make the sibling log in with a credential the forger already
  had.
- **The sibling treats what it opens as untrusted**, because a compromised
  Flask chooses those bytes and the sibling is the privileged side: a size
  cap before decrypting, the exact field set, `type(job_id) is int` (a JSON
  `true` equals 1 in Python), the job and identity matched against the row,
  and the character allowlist applied again before the credential reaches
  any variable or command string.

**What "in memory" does and does not protect.** The plaintext still exists
in two places: in Flask for the request that seals it, and in the sibling
for the life of one phase execution (plus the `verify` chained onto
`activate`) — for a stage phase, ~15 minutes per host. The exposures, and
what each requires:

- **A core dump writes the heap to disk.** The reference Quadlet units set
  `LimitCORE=0`; the process setting `PR_SET_DUMPABLE` to 0 — which would
  also deny same-uid `ptrace` and `/proc/<pid>/mem` — is not yet done, and
  neither protection reaches a bare `flask run` or `upgrade_cli.py`.
- **Debug mode turns an exception into a credential disclosure.**
  Werkzeug's interactive debugger renders frame locals into an HTTP
  response. `DEBUG` is off by default and read from an environment
  variable; don't make it easier to turn on.
- **A Python `str` cannot be erased.** Dropping a reference is not
  erasure: the bytes persist in the heap until reused, and the form parser
  has already made copies. The residual is stated rather than a stronger
  property claimed. Every field that holds the plaintext is declared
  `field(repr=False)`, so an accidental `log.debug("%r", ...)` cannot write
  it out.
- **The heap is swappable**, so either swap is disabled on the host or the
  exposure is accepted explicitly.
- **The approval form puts the password in a POST body**, so the reverse
  proxy must not log request bodies, and the form needs CSRF protection
  and `autocomplete="new-password"`.

**gunicorn's single worker is now a tuning choice.** The socket design
required exactly one worker, because a credential held in worker A's memory
was invisible to worker B. With the credential in the row that is no longer
true; one worker stays for SQLite's sake (§5), not for correctness.

**Failure behaviour is fail-closed.** A job with no usable credential fails
with `failure_stage: credential` and the run fails; nothing falls back to a
credential from another row, another phase or a previous attempt. A sibling
that dies mid-phase loses the plaintext with the process, and §7.3's sweep
moves the execution to `abandoned`, which is correct: re-approving is how
the next attempt gets a credential.

## 10. Open Questions

- **Resolved: the single hash algorithm stays SHA-512; MD5 was evaluated
  and rejected.** Raised because Netmiko ships MD5 verification helpers
  and MD5 is measurably cheaper on a switch CPU (408 MB: 18.5s against
  33.9s on a Catalyst 9200CX). Rejected because those helpers compare
  NetHub's mount against the device rather than the ingest digest
  against the device — the comparison §7.2 exists to stop anything
  relying on — so the swap saves no code; and because chosen-prefix
  collisions give a supplier of images a practical substitution path
  against the places with no backstop behind the digest (§4's plain-HTTP
  day-0, and the unverified distribution host of any future pull
  transport). Full reasoning
  in §3.4. Worth carrying forward: the *generic* form of the security
  argument is wrong — matching an already-recorded digest is a preimage
  attack, which MD5 still resists — so anyone re-opening this on
  "MD5 is broken" is arguing from the weaker case.
- **Resolved: Netmiko's `CiscoIosFileTransfer` is usable against IOS-XE
  at image size, but only under Paramiko.** Tested against a real
  Catalyst 9200CX at image size, not just a small text file. This
  repeats a finding made once already under Ansible — `net_put`'s
  `libssh` connection type was unusable there for the same reason,
  breaking the SSH connection repeatedly with nothing actionable in the
  debug log — and the result carried over the library switch rather than
  needing to be re-litigated: Paramiko is the common thread, and push is
  validated at 471 MB with it. Paramiko is deprecated upstream with a
  scheduled removal, but it's the only thing that has actually worked at
  image size, so it's what's committed (§4.3.1). Shelling out to OpenSSH
  `scp` as a subprocess was considered and rejected rather than built,
  because it needs the device password on an interface with no secure
  credential-delivery path built for it yet (§4.3.1's contract for what
  that path would need).
- **Possible future transport: device-side pull.** An earlier revision
  let a deployment choose between the SCP push and the device fetching
  its own image over SFTP (`copy sftp://…`). The pull adapter was written
  but never wired up: nothing configured a distribution host or
  credential, so selecting it failed every stage, and it was never run
  against hardware. It was deleted rather than carried. Push stays right
  as the only transport because many deployments block device-initiated
  outbound SSH, and where they do pull does not work at all (§4.3.1).
  Pull's real attractions were that it reconfigures nothing on the
  device, avoids Paramiko entirely, and makes a branch-site mirror cheap
  (see "distributed distribution" below). Bringing it back needs:
  - **A distribution host and a credential source.** An SFTP daemon on
    the NetHub host, chrooted (`ForceCommand internal-sftp`) to the
    published subtree, and a credential that is either the submitter's
    own device credential or one dedicated read-only account supplied
    as a systemd credential in the sibling's unit — never a `settings`
    row. Repointing the distribution host becomes the highest-yield
    settings write in the system (§7.2), and IOS-XE's SSH client does
    not verify the host it dials, so a redirected session collects the
    credential; `verify /sha512` catches substituted bytes, never the
    substituted host. Transport must stay deployment-level, never
    request-level, because choosing it chooses whose credential is
    spent. Whether it should ever be per-host (a mixed fleet with one
    site blocking egress) is a question to answer then.
  - **A hardware test of the prompt sequence.** The deleted adapter
    answered `Destination filename` and then `Password:`, naming only the
    filesystem as the destination to force the first prompt, and waited
    for an unanchored `[>#]` to mark completion — which could match a
    progress indicator and return mid-copy. None of it was observed on a
    device. The password must be answered at the prompt, never embedded
    as `sftp://user:pass@host/` (it would land in command history and
    AAA accounting), error text must not quote the channel, and
    Netmiko's `session_log` must stay off on that phase.
  - **Schema.** A snapshotted transport column on `upgrade_runs`, so
    `scp_restore_confirmed` can be read correctly again (§5).
  - An HTTP variant would avoid the credential entirely but makes the
    day-0 daemon part of the day-2 path; it needs §3.3's `mint/`
    capability paths, never a shared docroot.
- **Possible future work: shared account mode.** A deployment-level
  opt-in that fixes the device username to one admin-configured value
  for every run, for teams with no per-human device logins (§4.4). It
  was stubbed — a hardcoded-off `SHARED_ACCOUNT_MODE` flag and an
  `upgrade_runs.shared_account_mode` column recorded on every run — but
  never had a username setting or anything that read it, so it was
  deleted. Building it means: the username as a deployment setting in
  the audited `settings` table, a snapshotted column on `upgrade_runs`
  recording that the name came from shared mode, and the run pages
  saying so. It must never be inferred from a missing IdP or anything
  else, and it gives up the device-side half of §4.3's attribution.
- **Whether an IOS-XE upgrade can legitimately regenerate a device's SSH
  host key.** This decides whether `wait_for_device()`'s reconnect loop
  is right to leave `connection.HostKeyError` untouched by the
  not-transient carve-out it already gives `AuthenticationError` (design
  doc "Device layer" above, and the module notes in `nethub/devices/
  install.py`). If a legitimate upgrade can rotate the host key, treating
  a mismatch as non-transient during the exact window a device might do
  that would turn a successful upgrade into a hard failure the operator
  has no way to distinguish from an actual on-path attack. If it cannot,
  the current caution is free to relax. Settled by asking the platform
  question directly or by observing it across enough real upgrades, not
  by guessing either way.
- **Resolved by removal: whether the credential socket needed a
  wall-clock bound (§9.1).** A peer trickling bytes could hold the
  sibling's only dispatch loop past every per-operation timeout. PLAN.md
  WS-7 replaced the socket with a sealed credential in the job row, so
  the sibling no longer reads from a peer at all; opening a sealed box is
  a bounded, local operation behind a size cap.
- **Whether to build a purpose-built transfer script instead of relying
  on deprecated Paramiko indefinitely.** The options tried so far are
  both unsatisfying: `libssh` doesn't work under either library binding,
  and Paramiko works but is deprecated upstream. Push is the only
  transport, so the only transport rides a deprecated library. A small,
  single-purpose Python script that does
  only the SCP push, using the socket layer directly instead of a
  paramiko-dependent transfer class, might outlive either, but the
  amount of work that would take (auth, host-key checking against the
  same pinned policy `connection.py` already builds, framing the
  transfer, error handling equivalent to what the current adapter's
  `try`/`finally` gives for free today) hasn't been scoped. Worth
  revisiting once Paramiko's removal has an actual deadline rather than
  before.
- **Closing what's left of the restore-on-kill/abandon gap (§4.3.1).**
  The ordinary failure paths are now covered two ways: the stage phase
  confirms its own restore and fails the host if it can't, and the
  per-host bound that used to risk bypassing that restore (§8) is now
  Netmiko's own read timeout on the transfer call, which falls into the
  same `try`/`finally` rather than a process kill, with cancel handling
  doing the same. What remains is narrower and genuinely residual: a
  killed process, an abandoned run, or a crashed sibling outside
  NetHub's own timeout/cancel logic still doesn't reach `finally:`. A
  general reconciliation pass over every device is not the fix for that
  residual either — connecting to every device that was ever mid-stage
  and checking is exactly the per-device current-state view §2 and §7.4
  refuse to build. The candidate that doesn't reopen that non-goal is
  job-scoped rather than device-scoped, and its tracking column already
  exists (§5): `scp_restore_confirmed` on the stage row in
  `upgrade_host_phase_results` is null or `false` for exactly the rows
  that owe a restore. What's still open is whether to
  build an active process against that column — the sibling's startup
  sweep, or a separate periodic pass, connecting to *those specific
  hosts* and finishing the restore — or to leave it as a queryable
  answer an operator checks by hand. The column exists either way, so
  this is a decision about automation, not about whether the state is
  tracked.
- **Resolved: NetHub's host-key pin takes effect at exactly one place,
  because there is exactly one place a connection can be opened from
  (§4.3, §9.2).** This used to be genuinely open under Ansible — its
  connection plugins didn't all read a rendered `known_hosts` from the
  same place, and some hardcoded `~/.ssh/known_hosts` regardless of what
  was rendered into a job's working directory. Under Netmiko the
  question dissolves rather than gets answered on its own terms: there
  is no rendered `known_hosts` file at all, and the paramiko client every
  connection uses is NetHub's own, built by `connection.py`'s
  `_build_ssh_client()` override with the pinned policy object attached
  directly. §4.3.1's second SSH session for the SCP put goes through the
  same override (`SCPConn.establish_scp_conn` is built from it), so the
  question of whether the pin covers a second connection independently
  is answered by construction too: there is one connection-opening code path in the tree, and
  every session — primary or transfer — goes through it.
- **When distributed distribution comes back (§2, §3.3).** NetHub is the
  sole source of the bytes and multi-site mirrors are out of scope. The
  constraint that bites first is a branch site on a narrow link, where
  every byte crosses the WAN from NetHub. §3.3 is the seam. A push mirror
  needs a process near the devices that performs the pushes and
  authenticates to them, which is most of a second NetHub; a pull mirror
  needs only an SFTP daemon holding a verified copy of the subtree,
  because the device does the work and the digest check already catches
  a mirror serving the wrong bytes. So if multi-site support is ever
  built, the device-side pull transport above is the cheaper path to it,
  and that is worth knowing before the decision rather than after.
- **Whether Flask and the sibling should run as two rootless users
  rather than one (§9.2).** The socket this entry was first written about
  is gone (WS-7), and the question moved to the sibling's private key.
  Under one user, what keeps the key from Flask is that only the sibling
  unit mounts or loads it — a Flask-side compromise confined to its
  container cannot read it, but anything that reaches the host as that
  user can, and with the key a copy of the database yields every sealed
  credential still queued. Two users would put the key behind a uid
  boundary, at the cost of a second rootless Podman stack and more
  awkward sharing of the volumes both need.
- **Whether to rebuild any tamper-evidence mechanism at all for
  `artifacts.sha512`, `device_host_keys`, and the security-relevant
  `settings` keys once they exist (§7.2).** The earlier git-commit-
  signing mechanism this design once had is gone along with the
  rendered registry file it protected (§5, §7.1, §7.2), and nothing has
  replaced it — this is a real regression, not a stale open question
  left over from before. The options are the same ones §7.2's old text
  considered: an append-only hash chain over `artifacts` writes, a
  digest or projection countersigned by the sibling (the one component
  that could plausibly hold a key Flask does not), or a lighter
  pre-dispatch re-check the sibling performs against whichever store
  ends up being authoritative. Deciding this needs `settings` to exist
  first, since two of the three rows this section is about
  (`device_host_keys` exists today; the security-relevant settings rows
  need `settings`) don't both exist yet — but the `artifacts` half is
  buildable now and shouldn't wait on the other two.
- **The two day-0 numbers in §3.2 are asserted, not measured.** A
  two-second p99 and a fifteen-minute device backoff are what the
  architecture is calibrated against, and neither has been checked
  against a real IOS-XE ZTP client on a loaded upload path. They are
  written down so that the first measurement contradicts something.
- **Whether to pin a certificate in the day-0 generic script (§4).**
  This would close the confidentiality gap the plain-HTTP decision
  leaves open — a passive listener currently reads the bootstrap
  payload's TACACS+/RADIUS secrets, SNMP communities, and enable hashes
  outright — by having the script NetHub ships pin a key for every fetch
  after it, downgrading the exposure to "an active attacker must
  substitute the first script." Not free: NetHub would need to generate,
  rotate, and re-embed that certificate in the script it serves, and a
  rotation mechanism that isn't itself anchored to something reopens the
  same first-contact problem one level up. Worth a deliberate yes/no
  rather than defaulting to "no" by never revisiting it.
- **How far to extend the single-connection AAA credential probe
  (§4.3).** The current design validates a submitted device credential
  against one host in the target set before dispatch, which catches a
  flatly wrong password but not one that's valid on some AAA policy
  groups and not others — a group-scoped denial can still surface
  mid-wave. Probing one host per distinct AAA group named in the
  request would close that at the cost of more pre-dispatch connections
  and more places for the probe itself to fail; whether that trade is
  worth it depends on how commonly real deployments actually segment
  AAA policy by device group, which is a question about the deployments
  NetHub will run against rather than about the mechanism itself.
- **What the mandatory host-key confirmation step costs a first-time
  fleet onboarding (§4.3.1).** Requiring every address to have a
  confirmed `device_host_keys` row before any run can target it closes
  the confused-deputy gap described there, but a bulk import of a new
  fleet now means confirming every address individually before the
  first run against any of them. Whether that needs a lightweight
  bulk-confirm affordance — and what would keep such an affordance from
  quietly reopening the gap it's meant to avoid friction around — is
  worth deciding with real onboarding volumes in mind rather than
  guessed at here.
- **How to size the OIDC absolute session timeout against the IdP's own
  revocation SLA (§4.5).** The timeout is now the honest bound on how
  fast an IdP-side role demotion or account disablement takes effect at
  NetHub, not "the next request." What that bound should actually be is
  a property of the deployment's IdP and its own group-membership
  propagation delay, not something this document can size once for
  everyone.
- **Whether a user may hold more than one concurrent session (§4.5).**
  The invalidate-other-sessions behavior on password reset / role
  change / deactivation presumes multiple sessions can exist per user
  without saying whether that's intended normal use (an admin logged in
  on a laptop and a phone) or an incidental side effect of the session
  model that should be capped at one.
- **Whether day-0 config/script artifacts need their own promotion gate
  (§5, `allowlist_entries`).** The role/kind-matching trigger there
  requires only that a referenced artifact isn't `superseded` and isn't
  byte-pruned — deliberately looser than the `staged → published`
  workflow §3.4/§5 define for images, since an admin-uploaded per-device
  config isn't "published" to a fleet the way an IOS-XE image is.
  Whether day-0 artifacts should get an explicit review/promotion step
  of their own instead of relying on upload-time correctness is a
  product question about how much day-0 content review NetHub's
  operators actually want, not a technical one.
- New repository, built clean for the backend, reusing applicable
  existing pieces (e.g. Kea configuration) rather than forking the repo
  wholesale. The frontend is the exception: it's carried over from
  Drawbridge as-is and extended in place (see §3.1), not rebuilt.
