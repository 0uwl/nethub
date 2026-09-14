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
  crosses whatever link separates the NetHub host from the device —
  which direction it crosses in is configurable (§4.3.1), which end
  originates it is not. This is a deliberate narrowing rather than an
  omission: owning the store outright is what makes the transport an
  operational choice instead of a provenance question, since both
  adapters read a subtree NetHub administers. It is also a real
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
transport assumes the device can run a temporary file-transfer server
that NetHub can enable, push to, and disable again from the same
authenticated session it already holds; its pull transport assumes a
file-transfer client that can be driven from the CLI and prompted for a
password on that same session; and §4.3 assumes a level-15-equivalent
authorization exists at login. None of the three is universal, and a
platform that supports only one of the two transports is a supported
outcome — `image_transport` is deployment-level, but a platform that
cannot honour the configured value must fail at pre-check rather than
silently doing the other thing.

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
the backend dispatches to rather than in the Flask process itself. The
one-process constraint is not a scale judgement and cannot be relaxed
later as one: §9.2 shows two workers silently breaking the credential
path. Threads within that process are how the long operations below are
kept from blocking each other.

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
- **Day-2**: images move between the store and an already-enrolled
  device in whichever direction the deployment's `image_transport`
  setting selects (§4.3.1). By default NetHub **pushes** over SCP —
  Netmiko's `CiscoIosFileTransfer`, Paramiko underneath — under the same
  device credential the run already holds — no second credential is
  minted or held. A deployment whose devices may open outbound
  connections can instead have the device **pull** over SFTP from a
  distribution daemon, which costs a listening service and a
  distribution credential and is priced in §4.3.1.

**NetHub is the sole source of the bytes, and that is not modular.**
Earlier revisions let the image *source* be either a local bundled
container or an existing remote host, chosen by configuration. That
flexibility looked cheaper than it was: a source NetHub does not
administer is one it cannot guarantee holds the artifact NetHub actually
published. Owning the store outright turns that into ordinary
implementation, and it is what makes the transport a free choice rather
than a trust question — both adapters read the same published subtree on
the same host NetHub administers, so selecting pull changes who dials
whom and changes nothing about provenance.

Which is worth separating explicitly, because the two were conflated
before: **the source is fixed, the direction is configurable.** Under
push the sibling reads the published subtree read-only (§3.5) and pushes
straight from it over the connection the run already holds, and
"distribution host" names only the box the bytes are read from. Under
pull the same subtree is additionally exposed by an SFTP daemon and
"distribution host" names a service the device dials — a daemon and a
credential that push does not need, both specified in §4.3.1. Neither
mode admits a second store.

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

The pull transport does not weaken that. It exposes the day-2 subtree
over **SFTP**, to authenticated clients only, chrooted to that subtree
(§4.3.1) — which is a different daemon, a different protocol and a
credentialed one. What must not happen is the shortcut of serving day-2
images from the day-0 HTTP docroot to save running a second service:
that would put the image store behind a guessable filename on an
unauthenticated read path, which is precisely the arrangement this
paragraph exists to forbid. If serving images over HTTP is ever
revisited it needs the `mint/` capability-path treatment day-0 configs
get, not a shared docroot (§10).

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
   └─ egress B (day-2): admin-initiated, dispatched to the sibling, credentialed, one of
        ├─ push (default): NetHub → device, SCP
        └─ pull:           device → NetHub, SFTP
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
succeeds. Under the pull transport the device does not verify the
distribution host at all (§4.3.1), and day-0 names payload hash
verification as one of three compensations for its deliberate use of
plain HTTP (§4). Large binaries with unused space are good collision
carriers. The reopening condition is unchanged by any of this: a second
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
secret a phase execution needs is passed in memory — a Python attribute
on `phases.PhaseContext`, not a file — rather than written anywhere a
phase's inputs are.

The image bytes themselves never enter that snapshotted state, and that
falls out of the same rule rather than being an exception to it: what
NetHub snapshots onto `upgrade_run_hosts` is the *reference* — filename,
digest, size — not the file. Under push (§4.3.1) the sibling does handle
the bytes: `stage_image()` reads the file directly off the published
subtree and streams it to the device over the second SSH session Netmiko
opens for the transfer, rather than the file being copied anywhere else
first. Under pull the sibling never touches them — the distribution
daemon serves them and the device receives them — so that mode is the one
this paragraph was originally written for. Either way the invariant this
section actually needs holds: nothing the credential socket or the
snapshotted row set carries is ever the multi-hundred-megabyte image
itself, only a reference to where it already sits on the one mount both
transports read. What changed is only that "the phase never touches the
bytes" is now true of one transport and not the other, rather than being
a property of the design.

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

Day-2 image delivery deliberately stays credentialed, unlike day-0, and
stays credentialed in **both** transport directions (§4.3.1). Under push
the image rides the Netmiko session already authenticated with the
submitter's own device credential; under pull the device authenticates
to the distribution daemon. Neither is the plain HTTP used for the day-0
script fetch (§3.3). The two days are in different trust situations, so
this is not an inconsistency. A day-0 device has no credentials to offer
regardless of transport, so plain HTTP costs nothing extra there. A
day-2 device is already enrolled, and a credential is what gates who may
write an image to it or read one out of the store. Dropping that for
protocol uniformity would remove real authorization that hash
verification does not replace: hash verification confirms the bytes
weren't tampered with, and says nothing about who was allowed to move
them in the first place.

Whether a *second*, distribution-specific credential needs pricing
separately is now a per-deployment answer rather than a settled no. Push
collapses it into the one credential §4.3 and §4.3.1 already cover, and
its residual cost lives elsewhere — in the device configuration change
that makes the push possible. Pull reintroduces a distribution
credential, bounded to two possible sources and kept out of the
`settings` table; §4.3.1 prices it.

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
the submitter's device credential and holds it in memory as a plain
Python attribute — `phases.PhaseContext`'s credential field — for the life
of one phase execution. It is never written to disk, never kept in the
session, and never persisted.

It belongs to a *phase execution* rather than to the session or to the
run. The session is the wrong owner because a serial activation wave
outlives any reasonable session lifetime. The run is the wrong owner
because §8.1 lets a run park at an approval gate for days, and "held for
the life of the run" would mean a plaintext password resident in some
process's memory across a weekend. So it is collected with each approval
and dropped when the execution that approval released reaches a terminal
state. §4.3.1 covers why push needs no second credential with a lifetime
of its own; §9.1–§9.2 cover how this one crosses from the browser to the
sibling without touching disk, and what that costs the operator.

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
  to a design whose whole point is minimising exactly that. The probe's
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
  network can take it. §4.3.1 concludes that the old *distribution*
  password's exposure was structural and mitigated it with lifetime;
  this one is not structural and does not get that excuse. NetHub pins
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
  services on the device. It is relevant again for a deployment that
  selects the pull transport, where the device is an SSH client once
  more, but as a device-side prerequisite an operator satisfies rather
  than as an assertion NetHub makes (§4.3.1). NetHub does not otherwise write configuration
  outside the upgrade itself, and asserts privilege 15 at pre-check
  rather than discovering it mid-wave. One exception is deliberate and
  bracketed rather than standing, and it belongs to the push transport
  alone: the stage phase enables the device's own SCP server for the
  duration of the push and restores whatever it found — enabled or not —
  in a `finally:` block, confirmed by re-reading the running-config
  rather than trusted from the adapter's exit status (§4.3.1). That
  confirmation is why "does not write configuration outside the upgrade
  itself" still holds: the toggle is scoped to one phase execution on one
  host, not a standing device change. Under pull the exception does not
  arise at all.
- **Whether a distribution account exists is a deployment choice, and
  the default is that it does not.** Under push there is no second
  account to keep shared and no second password to rotate: the transfer
  rides the same Netmiko session already authenticated with the
  submitter's own device credential. Under pull there is a distribution
  daemon and therefore a distribution identity, which is either the
  submitter's own credential again or one dedicated read-only account
  supplied as a systemd credential — never a shared *device* login, and
  never a row in `settings`. §4.3.1 prices both; the point here is that
  this bullet's old absolute form was a property of one transport rather
  than of the security model.

#### 4.3.1 How the image reaches the device, and what each direction costs

The image can travel in either direction, and which one a deployment
uses is a setting (`image_transport`, §5) rather than a property of the
design. **Push** — NetHub writes the image to the device over SCP — is
the default. **Pull** — the device fetches the image from a distribution
host over SFTP — is the alternative. Both adapters read from the same
store NetHub owns (§3.3), both end in the same `verify /sha512` against
the digest computed once at ingest (§3.4), and `nethub/devices/
transfer.py`'s `stage_image()` selects between them by dispatching to a
push or a pull adapter function based on the deployment's
`image_transport` setting. Everything on either side of the transfer is
shared, which is what keeps this a choice of adapter rather than two
upgrade paths to maintain.

Making it a setting is not indecision. The two directions fail in
different places, and which failure a deployment can absorb is a fact
about that deployment's network and its change policy — not something
this document can settle once for everyone. What follows prices each
direction, because the choice is now an operator's to make and an
operator cannot make it from a section that argues for one.

**The history matters, because the default was reversed once already.**
An earlier revision of this design had the device fetch its own image
over SFTP, authenticating to a distribution account NetHub minted a
password for on every phase execution. **That design assumed the device could
open an outbound connection to NetHub, and a nontrivial fraction of real
deployments block exactly that.** Outbound SSH from a network device is
a common perimeter rule — it is the same jump-host concern that motivates
restricting SSH egress from servers generally, applied to infrastructure
that is even more attractive to pivot through. Where that rule is in
force, pull does not degrade, it does not work: there is no fallback and
no partial credit, only a device that can never reach the distribution
host regardless of anything NetHub does on its own side. That is why
push is the default, and why selecting pull is something a deployment
should do only after confirming its devices can actually reach the
distribution host. It is not a reason to withhold pull, which is what
the previous revision of this section concluded: a deployment that
permits the egress is not helped by NetHub pretending it does not exist,
and the costs below are costs, not disqualifications.

**Push inverts the connection, and where it is available that inversion
pays for itself immediately.** Every device in a run already has an
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

Under push, nothing new is minted regardless: there is no distribution
account, no distribution password, no per-phase credential lifecycle
beyond the one §4.3 and §9.1 already define for the device credential
itself. Everything
§4.3.1 used to specify about the old model — a minted password's
lifetime, its purpose-built low-privilege account, the SSH/SFTP daemon
serving it, the `ForceCommand internal-sftp` chroot hardening it needed —
is simply absent when push is the configured transport, because there is
no second credential to specify any of it for. Under pull those
requirements return, in the reduced form the pull half of this section
specifies; the point is that a deployment on push pays none of them, not
that they were wrong.

**What push costs instead is a device configuration change, and
`transfer.py`'s push adapter is explicit about the price.** Pulling left
the device untouched; pushing requires the device's own SCP server to be
listening, which means NetHub has to enable it, use it, and turn it back
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
  digest, unchanged by which direction the bytes travelled.
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
wire protocol available regardless of which library carries it;
switching to SFTP the way pull could, or falling back to the device
fetching its own image, is not an option in this direction either.

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
  landing in `/proc/<pid>/cmdline`, which is exactly what §9.2 forbids for
  the credential socket's payload and for the identical reason. This is
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
Flask, either direction. Only *which side of the wire* performs the SSH
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
  the SCP server; pre-check, activate, verify and cleanup never do. Under
  pull no phase toggles anything, which is that property's stronger
  form. A run
  parked at the reload gate from Monday to Saturday holds no device
  credential in memory (§9.1) and has made no standing change to any
  device — the same property §8.1 claims for the sibling's phase
  execution itself.

**Pull, and what it buys.** Under `image_transport = pull_sftp` the
stage phase issues one `copy sftp://…` on the device's own CLI over the
existing Netmiko session, answers the two prompts it raises on that same
channel, and then runs the same `verify /sha512` push runs. The gains are
the exact mirror of push's costs. **This adapter's prompt sequence
remains unverified against a real device** (§10) — it is the one item
from the pre-Netmiko design that outlived the rewrite unresolved, and
naming only the filesystem as the destination is deliberate: it forces
the `Destination filename` prompt to appear so the pair of prompts arrives
in a known order. A wrong prompt list does not fail fast; it hangs until
the read timeout:

- **Nothing on the device is reconfigured.** The enable/restore bracket
  above does not exist under pull, and neither does the residual
  kill/abandon gap that bracket cannot close. `scp_restore_confirmed` is
  null on a pull-transport stage row because there was never anything to
  restore — the column keeps its meaning, and "null" keeps meaning "no
  confirmed restore", which is why the distinction has to be read
  together with the row's transport rather than alone. For an
  organisation whose change policy treats any running-config write as a
  change requiring its own approval, this bullet is the entire argument.
- **It depends on neither Netmiko's file-transfer class nor Paramiko.**
  The transfer is performed by the device's own SFTP client and driven by
  one CLI command, so the Paramiko deprecation tracked in §10 and the
  `libssh` failure that forced Paramiko in the first place do not reach
  it. A deployment that finds push unreliable now has somewhere to go
  that is not "wait for a purpose-built transfer script".
- **It uses whatever outbound management path the device already
  has**, including a source-interface or VRF it is already configured
  for. The `ip ssh source-interface` check the pre-check dropped when
  push became the default was a pull-era requirement, and it is relevant
  again here — not as an assertion `phase_precheck` makes (there is no
  pre-check assertion infrastructure to hang it on today), but as a
  prerequisite an operator selecting pull has to satisfy on the device
  side.

**What pull costs, priced the same way.**

- **A daemon the device dials comes back.** §3.3's "day-2 no longer has
  a daemon a device connects to" holds only under push. Pull requires an
  SFTP service on the distribution host, listening where every targeted
  device can reach it, serving the published subtree, with the
  `ForceCommand internal-sftp` chroot hardening the original pull design
  specified and for the same reason. That is inbound network surface on
  the NetHub host which push does not have, and it is the cost that
  should weigh heaviest on the decision.
- **A distribution credential exists again.** This is the part of the
  change that had to be argued rather than waved through, and it is
  bounded below.
- **The device does not verify the distribution host's key.** This is
  the sharpest asymmetry between the two directions and it has no
  mitigation, only a compensating control. Under push, NetHub validates
  the device against a pinned `device_host_keys` row and fails closed
  (§4.3). Under pull the connection runs the other way, and the
  verifying party would have to be the IOS-XE SSH client, which does not
  perform host-key verification of the server it connects to. An
  attacker who can redirect that session to a host they control is
  therefore not detected by the transport. What detects them is the
  digest: `verify /sha512` compares against the SHA-512 rendered from
  the `artifacts` table (§3.4), which the attacker cannot influence, so
  substituted bytes fail verification and the host fails staging.
  Confidentiality of the transferred bytes is not protected, but an
  IOS-XE image is a vendor binary rather than a secret — unlike a day-0
  config, which is why §3.3 reasons about the two differently.
- **That same redirection collects the distribution credential.** The
  device answers a password prompt to whatever it actually connected to.
  This is the argument for the credential being either the submitter's
  own — already presented to every device in the run under §4.3's model,
  so no new secret is placed at risk — or a dedicated account whose only
  privilege is read-only access to one directory. It is also, and more
  sharply than any other line in this section, the argument for a
  submitter never choosing the transport (§3.5, §2's non-goals): a
  request that could select pull could select whose credential gets
  spent.

**Where the pull credential comes from, and why it is not a settings
row.** `distribution_credential_source` (§5) takes two values:

- `same_as_device` — the submitter's own device credential, the one
  §9.1 already collects at the approval gate and holds in memory for the
  life of the phase execution. Nothing new is stored and nothing new is
  minted, and §4.3's two-sided attribution property survives intact:
  the same human authenticates to the device and to the distribution
  host. The precondition is that the distribution host authenticates
  against the same directory the devices do, which a deployment with
  centralized AAA can generally arrange and one without generally
  cannot — the same precondition §4.3 already states for the
  attribution claim itself.
- `dedicated` — one deployment-wide account on the distribution host,
  read-only over the published subtree. Its password is **a systemd
  credential injected into the sibling's unit, never a row in
  `settings`.** §5 already rules that secrets stay out of that table,
  and supplies the OIDC client secret and the shared-account password
  exactly this way; the reason transfers without modification. A
  settings page that holds passwords is a plaintext secret store
  readable by every admin, and this password would be the most useful
  one in it.

Either way the credential reaches the phase execution the way the device
credential already does: held as a plain Python attribute on
`PhaseContext` (`transfer.py`'s `PullTarget` carries it, `field(repr=False)`
so it never renders into a traceback or a stray `log.debug`), destroyed
when the phase ends, never written to the database and never rendered
into anything retained. Under `same_as_device` it crosses §9.1's socket,
because it is a per-execution secret Flask collected at a gate. Under
`dedicated` it does not cross the socket at all — the sibling reads its
own unit's credential directly and Flask never holds it. The second case
is a smaller surface than the socket, not a wider one, and neither
widens what the socket is permitted to carry: it is still per-execution
credentials and nothing else.

**The password is answered at a prompt, never embedded in the URL.** A
`copy sftp://user:pass@host/…` form would place the credential in the
device's command history and in its AAA command accounting — the same
record §4.3 leans on for attribution, which would then contain a
password, turning the audit property into a disclosure. So the pull
adapter issues the command with no credential in it and answers the
device's own `Password:` prompt on the channel. It also refuses to put
exception text into its own error messages, because a channel read can
quote back whatever was written to that channel — the same reasoning
§7.3 states for `error_summary` generally, arriving here through a
different mechanism. Netmiko's `session_log` must not be enabled on a
pull-transport phase for the identical reason: it would capture the raw
channel bytes, prompt and password both, to a file. That is a constraint
on how the sibling drives this one adapter, recorded here because nothing
else in this document would record it.

**Ansible Vault was considered while this design still used Ansible, and
the reasoning against it carries over unchanged now that it doesn't.**
Vault encrypts a secret at rest in a file a runner reads. The credential
this section is about was never in a file to begin with — it is a Python
attribute held for the life of one phase execution — so vault would have
protected a copy that was already the least exposed one, with a vault
password that has to reach the same process by the same means, which is
two secrets where one would do and restores exactly the key-disposal
problem removed when the old minted distribution password went away.
The secret that genuinely needs protecting at rest is the `dedicated`
account's password, and that one was never a candidate for vaulting: it
is a systemd credential, the mechanism this deployment already uses for
the shared-account password (§4.4, §5). Adding a secret store to hold a
copy of it would be strictly worse than using the one it already has.

**One thing pull does not change, stated because it would be easy to
assume it does.** The authorization gate is identical: an
unauthenticated party still cannot cause an image to move anywhere, in
either direction. That requires an authenticated submitter, an approved
gate, and a valid credential, exactly as every other phase does. Pull
moves where the bytes are read from and who dials whom; it does not move
who is allowed to ask.


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
setting (`SHARED_ACCOUNT_MODE`; not a per-user fallback, and not implied
by choosing the local auth backend) that fixes the device username to one
admin-configured value for every upgrade run in that deployment, instead
of reading `users.device_username`. It costs exactly what §4.3's
two-sided attribution argument warned against for a shared service
account: every device-side change attributed to one name,
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
`journal_mode=WAL`, `busy_timeout` (a few seconds, so a concurrent
reader waits instead of raising), and `foreign_keys=ON`, which is off by
default and would otherwise silently turn every reference below into a
suggestion.

- `artifacts` table, the single ingest record behind both days (§3.4):
  `id`, `kind` (script/config/image), `platform`, `bundle_key`,
  `filename`, `sha512`, `file_size`, `storage_path`,
  `version`, `state`, `superseded_by_id`, `bytes_state`,
  `bytes_pruned_at`, `uploaded_by`, `uploaded_at`. Every byte NetHub
  serves, on either day, has exactly one row here.
  - `storage_path` is where the blob actually lives on disk, and is what
    §7.3's retention purge collects by. There is no `remote_dir`, and
    the return of a pull transport (§4.3.1) does not bring it back: that
    column existed because the distribution host could once have been a
    separate remote machine whose layout NetHub did not control (§3.3),
    which is no longer true in either direction. Push reads the file
    directly off the published subtree by filename, in the sibling's own
    process; pull has the device fetch by filename under the one
    directory the SFTP daemon exposes, which is that same subtree and is
    named once for the deployment rather than once per artifact. So there
    is still no per-artifact directory to record, and the constraint
    below is what both modes rely on instead. `UNIQUE(filename) WHERE
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
  `device_username_used`, `shared_account_mode`, `image_transport_used`,
  `distribution_host_used`, `request_document`,
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
    dispatch, for the same reason `upgrade_run_hosts` (below) snapshots
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
  - `image_transport_used` snapshots the transport the same way, and for
    a reason that is operational before it is evidentiary. A run parks at
    approval gates for days (§8.1), so a settings change between two
    phases of one run is ordinary rather than exotic — and a run that
    stages by push and then reads a changed setting at activate has no
    coherent answer for `scp_restore_confirmed`, which is null both when
    a pull-transport host had nothing to restore and when a
    push-transport host's restore was never confirmed. Snapshotting the
    transport at dispatch is what keeps that column readable, and it is
    the same "a run is self-contained" rule this table already applies to
    `filename`/`sha512`/`file_size`. Under `pull_sftp` the run also
    snapshots `distribution_host_used`, so the row records which host the
    fleet was told to authenticate to that day.
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
  `PRIMARY KEY (run_id, hostname)`. One row per targeted device, using
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
  - `scp_restore_confirmed` is nullable — set only on the stage row of a
    **push-transport** run, the only phase of the only transport that
    touches the device's SCP server (§4.3.1). It is null for every
    pull-transport stage row, and that null means "nothing was ever
    changed" rather than "a change may be outstanding", which is the
    opposite reading. Nothing in this row distinguishes the two, so
    anything querying for devices that may owe a restore must join
    `upgrade_runs.image_transport_used` rather than reading this column
    alone — including whatever eventually acts on §10's open question
    about it. It is otherwise
    the persisted counterpart of the fact the push adapter already
    computes locally in its own `finally:` block. It is how a future reconciliation
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
  `deadline_at`, `finished_at`, `runner_instance_id`, `log_path`. One row
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
  its old and new value. §4.4 names five OIDC settings, shared account
  mode and its fixed device username, and `local_accounts_enabled`; §8.1
  adds the target CIDR and the stage-phase concurrency cap; §4.3.1 adds
  the four transport keys below; §4.3 makes managing all of it an
  admin's job — and none of it had anywhere to live.
  - The transport keys are `image_transport` (`push_scp` | `pull_sftp`,
    default `push_scp`), and — read only under `pull_sftp` —
    `distribution_host`, `distribution_user`, and
    `distribution_credential_source` (`same_as_device` | `dedicated`).
    They are deployment-level and rendered into the job's inventory
    (§3.5); an upgrade request may not set any of them, for the reason
    §4.3.1 gives: selecting the transport selects whose credential is
    spent.
  - `image_transport` and `distribution_host` are security-relevant in
    §7.2's sense and belong in its signed set. Repointing
    `distribution_host` at a host the attacker controls is the single
    highest-yield settings write in the system — it directs every device
    in every subsequent run to authenticate to that host. The
    `verify /sha512` after the copy is what stops substituted *bytes*
    from being installed (§4.3.1), and it does nothing about the
    credential the device has already offered, which is why this key
    needs the signed-projection treatment rather than relying on the
    digest check downstream of it.
  - The audit table is not symmetry for its own sake. §4.4's claim that
    flipping shared account mode is "a decision an admin makes once and
    visibly" is only true if the flip leaves a record, and the settings
    here decide who is an admin and which account every device sees.
  - Secrets stay out of this table. The OIDC client secret, the
    shared-account password, and the `dedicated` distribution account's
    password are supplied as systemd credentials or environment, not
    rows, or the settings page becomes a plaintext secret store readable
    by any admin. The distribution password is the one most likely to be
    argued back in, because its settings page is the natural place to
    type it and it has no human to prompt at a gate — and it is
    precisely the one an admin-readable table should not hold. Note what
    this costs and accept it: changing that password is a unit
    credential change and a restart, not a form submission.
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
confirmation date shown on the approval screen → the image is
transferred by whichever adapter `image_transport` selects (§4.3.1):
under push, the device's SCP server is enabled if not already (prior
state captured first), the image goes over Netmiko's
`CiscoIosFileTransfer` under the same credential on a second SSH session
opened through NetHub's own pinned connection path, and the SCP server
is restored to its prior state with the restore confirmed or the host
fails; under pull, the device fetches the image from the distribution
daemon and nothing on it is reconfigured → either way the image is
verified against its SHA-512 on the device → admin approves activation →
devices reloaded in serial
waves → verification runs without a gate →
optionally, admin approves cleanup, or declines it and closes the run →
per-host outcomes and phase logs surfaced in the dashboard.

A credential is collected at each gate rather than once at submit, and
§9.1 explains why that is the price of "no device credential is ever at
rest". An operator can cancel a run at any gate, and between hosts
during a phase; §8.1 says what that costs at each one.

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
/artifacts/<id>/delete`, `POST /artifacts/check` for the on-demand drift
check §5's `check_store()` describes). Upgrade runs (`GET /upgrades`,
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
at this scale buys nothing there either, since upgrade dispatch is
occasional and admin-initiated, and it costs the entire class of
interleaved-device-work bugs. That queue is serial by construction
rather than by locking discipline, the same reasoning the old registry
lock used, applied to the one thing left in the system that still
needs it.

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
CIDR, shared-account mode, the OIDC admin group, and the transport keys
§5 adds — `image_transport` and `distribution_host`). A Flask-side RCE's
real yield is not "forge a queued row" (§9.2's stated baseline) or even
"substitute an image digest" — it is silently repointing the host-key
pin an approver's browser will show them at the next approval screen,
widening the target CIDR, or flipping the deployment to pull and naming
a distribution host it controls, ahead of the exact moment §9.1's socket
releases that approver's own AAA password. That last one is the newest
and among the most direct: it does not need to forge anything the
approver looks at, it just changes where the fleet is told to
authenticate. That is a materially larger
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
a confirmed pin, and there is no settings table yet for the transport-repointing
scenario below to apply to at all. An append-only hash chain over
`artifacts`, or a signed/countersigned projection of the security-relevant
rows once `settings` exists, is tracked as open in §10 rather than
described here as built. The honest statement is unchanged from before,
only weaker in degree: NetHub detects drift between the row and the bytes
on disk (§7.2's own mechanism) and detects nothing at all about a
consistent lie told from the row itself outward — a Flask-side RCE's real
yield is not "forge a queued row" (§9.2's stated baseline), it is
silently repointing a confirmed host-key pin, widening the target CIDR
once one exists, or (once `settings` and pull-transport deployments both
exist) naming a distribution host the attacker controls, ahead of the
exact moment §9.1's socket releases an approver's own AAA password. That
last one needs neither table to exist today to be worth recording as the
highest-yield future write in the system once they do.

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
`install`, `reload`, `postcheck`. That it no longer has to avoid
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
stray `str(exc)` from a socket handler or a runner exception is
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
  monitoring loop, independent of any per-host event boundary** — that
  precision matters because §8 gives the stage phase a per-host bound
  derived from `file_size` specifically because a single job-level bound
  is the wrong shape for a phase moving gigabytes, and the same reasoning
  applies to heartbeat freshness. A single transfer of a
  multi-hundred-megabyte image is one long-running call in either
  transport — a Netmiko SCP push, or a `copy sftp://…` the device runs
  while the session waits; driving `heartbeat_at` off per-host
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
  history shows both.
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
| `queued` | `running` | sibling, conditional claim (§9.2) |
| `queued` | `cancelled` | sibling, seeing the cancel column |
| `queued` | `expired` | sibling, past `deadline_at` |
| `running` | `succeeded` / `failed` | sibling, on phase completion |
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
| `pre_checking` | `failed` | sibling, on the pre-check phase job reaching a failed terminal state |
| `pre_checking` | `cancelled` | Flask, honoring a cancel requested before any gate exists |
| `awaiting_approval` | `running` | Flask, on approval (writes the phase job row) |
| `running` | `awaiting_approval` | sibling, at the next gate |
| `running` | `failed` | sibling, on a phase reaching a failed terminal state |
| `awaiting_approval` | `expired` | sweep, past `gate_expires_at` |
| `awaiting_approval` | `cancelled` / `completed` | Flask: an operator cancels, or declines the optional cleanup gate |
| `running` | `cancelled` | sibling, honoring the cancel column |
| `running` | `completed` | sibling, after verify with no cleanup pending |

**`pre_checking` needed its own exit edges, not just its own entry.** It
was given a literal distinct from `running` specifically because
pre-check needs no gate — but that split meant `running`'s
failure/cancel edges didn't automatically apply to it, and nothing else
did either. A pre-check phase job that times out, crashes to
`abandoned`, or is cancelled before any gate exists left the *run* row
with no documented transition out of `pre_checking` at all: not
`running`, so `running → failed` doesn't fire; not `awaiting_approval`,
so the sweep's TTL doesn't fire either. The two edges above close it —
a run can no longer get stuck at `pre_checking` permanently, which also
means it no longer escapes §7.4's purge story for a state that never
resolves.

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
  one would leave the audit trail pointing at nothing.
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
authentication mechanics from §4.3), `transfer.py` (`stage_image()` plus
the push/pull adapters from §4.3.1), `install.py` (activate/reload/
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
not a process-level kill of the sibling** — the distinction matters most
under the push transport, where the stage phase brackets the transfer
with a device-config change it must restore (§4.3.1), and a bound
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
the first place (§3.5, §9.2), so there is no `env/extravars` to
accidentally park on disk for the row's full 365 days — the credential
lives only as the `PhaseContext` attribute §4.3 and §4.3.1 describe.

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
anywhere in the schema (§5) — under push the sibling reads the file off
its own local mount, and under pull the SFTP daemon exports that exact
same path, so "the image is on the distribution host" names the address
the *device* dials, not a second filesystem NetHub would need to reach
across. A design that assumed the store could be a genuinely separate
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
input closest to a secret — a credential "validated against a character
allowlist before it goes anywhere near a variable or a command string" —
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
Flask and the sibling, which §9 rules out. §9.1 does open a second
channel, but a deliberately narrow one carrying secrets in one direction
and no control at all; a PTY stream fits through neither it nor the job
row. So the interactivity is a UI concept instead: the run is split into
phases, each dispatched as its own phase execution, and the confirmations
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

- **The plan phase dissolves into the UI.** There is no `plan` value in
  the `PHASES` vocabulary at all (§5) — the request is validated and
  compiled at submit, `upgrades.submit()` writes the run and its host
  rows in the same transaction, and the plan a human reviews is simply
  the submit form and the confirmation screen rendered from those rows.
  The same reasoning means there is no separate summary step either:
  per-host results are rows (`upgrade_host_phase_results`, §5), so there
  is nothing left for a final summary pass to compute that the dashboard
  can't already read.
- **Every phase runs its hosts strictly one at a time today, not just
  activation.** `phases.execute_phase()` loops over a run's hosts in a
  plain `for` loop, checking cancel and the deadline only *between*
  hosts (§7.3) — there is no concurrency anywhere in the phase model yet,
  stage included. That is stricter than the target design below, not a
  bug: correctness first, parallelism second. **What's still target
  rather than built** is letting pre-check and stage run across many
  hosts at once, since copying to flash drops no traffic, while keeping
  only activation serialized — copying is where a wave actually spends
  its wall-clock time (§8's measured push throughput, ~1.4 MB/s per
  device, makes a serial stage phase the dominant cost of a large wave).
  That parallelism is not free once built: with NetHub the sole source of
  the bytes (§3.3), an uncapped stage phase would mean NetHub pushing a
  gigabyte to every targeted device at once, over whatever link separates
  it from them. Concurrent devices cost NetHub's own link relatively
  little at the measured rate — twenty devices at once is roughly
  28 MB/s — but a deployment setting to cap it is still worth building
  before removing the serial gate, not after.
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
  (§9.1), same as every other phase, and a pull-transport run's
  distribution credential is either that same credential or a systemd
  credential the sibling reads at dispatch (§4.3.1) — neither is held
  while the run is parked, so the Monday-to-Saturday gap holds no live
  secret of any kind.
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
  a keystroke has nothing to collide with.
- **Cancelling means different things at different phases, and the UI
  says which.** `cancel_requested_at` (§5) is polled by the sibling
  between hosts. Cancelling a `queued` phase stops it before it starts.
  Cancelling pre-check or verify is safe outright: both are read-only.
  Stage under the push transport is not quite the same shape as those
  two: it's non-disruptive to the fleet's traffic, but push is the one
  thing in the system that mutates device configuration (§4.3.1's
  SCP-server toggle). Since every phase runs its hosts strictly one at a
  time today (above), a cancel during stage affects at most the single
  host currently mid-transfer when it lands — the sibling lets that host
  reach its own `try`/`finally` and confirm its restore before honoring
  the cancel, rather than killing the sibling process outright. Once
  concurrent staging exists, several hosts could be mid-transfer at once
  when a cancel lands, and the same reasoning would need to wait for all
  of them, not just one. Under the pull transport stage is read-only with
  respect to device configuration and cancels like pre-check does, which
  is one more line in pull's column. Cancelling an activation mid-wave is
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
doesn't cover. What remains untested beyond timing is the pull adapter's
prompt sequence (§4.3.1, §10) and a stage/activate/verify/cleanup
sequence driven in one sitting starting from a pre-check kicked off
through the web app rather than through `upgrade_cli.py` or a direct
call into `nethub/devices/`.

## 9. Deployment

- Podman Quadlet units for the Flask app, the sibling job runner, and the
  distribution container, which serves the day-0 subtree over plain
  HTTP, with no separate bootstrap container. Under the default push
  transport there is no day-2 daemon at all: the store is local to this
  host and is NetHub's own (§3.3), and images reach a device by the
  sibling reading the published subtree directly and pushing from it
  (§3.5, §4.3.1). A deployment that selects the pull transport adds one more
  unit — an SFTP daemon chrooted to that same subtree, serving the
  distribution account (§4.3.1) — which is the only unit in this list
  that exists to be dialled by a device rather than by an operator, and
  the only one whose absence is the safer default. The HTTP daemon decides nothing:
  it serves one fixed path holding the generic script, plus a `mint/`
  subtree of one-shot symlinks Flask writes and reaps, and authorization
  for day-0 lives in the phone-home handler. Everything runs on one host,
  in separate units, under one rootless Podman user.
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

### 9.1 The control channel and the secret channel

§9 settles that the sibling exists. It leaves unsaid how the two halves
actually exchange anything, and the document has been relying on two
statements that cannot both be true: §4.3 says the device credential is
"never persisted", and §9 says the job row is the only channel between
Flask and the sibling. The credential arrives at a Flask request handler
and is needed by a Netmiko connection opened inside the sibling. Either
it travels through the database — which is persistence — or there is a
second channel. This section picks the second and bounds it; §9.2
covers how the channel is built and what keeps it narrow. This is built
today as `nethub/credential_socket.py`, exercised by tests rather than
only argued here.

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

Nothing that crosses the socket is ever written to the database, to
disk, or to a log. Nothing that belongs in the row is allowed onto the
socket because the socket is more convenient. A new piece of information
that is not a secret goes in the row.

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
run one phase execution at a time — a phase approved while another run's
fifty-host activation wave grinds along can sit `queued` for hours. That
is the same objection this section raised against the per-run lifetime,
smaller but not different in kind. So a held credential expires on a
bounded TTL measured in minutes; on expiry the phase fails
`credential` and the operator re-approves. Queue depth is shown at the
gate, so an approval made into a long queue is an informed one. It is
worth saying plainly that the longest phase — the serial activation wave
— can itself run for hours, and the credential is live for its
duration; "one phase execution" is an honest bound, not a short one.

**The socket still carries one credential, and the pull transport does
not change that.** §4.3.1's original design had the sibling mint a
distribution password per execution and carry it here; that mechanism is
gone, and it is not what came back. Under
`distribution_credential_source = same_as_device` the distribution
credential *is* the submitter's device credential — the same secret
already crossing the socket, spent at a second destination, not a second
message type. Under `dedicated` it does not cross the socket at all: it
is a systemd credential in the sibling's own unit, which Flask never
holds and therefore never has to hand over. Either way the socket's
contract is unchanged — per-execution credentials, one kind, nothing
else — and neither arrangement leaves the sibling holding a standing
secret for an execution it has not started, which is the property §9.1
exists to preserve.

**The TTL above needed wiring that a correct-looking implementation can
still be missing.** `purge_expired()` and `discard()` can exist as
functions with no caller anywhere in the codebase, which is exactly what
happened here: the TTL was enforced only inside `release()`, meaning only
if the sibling eventually asked for that exact job. A held credential
whose job never actually ran — because the sibling was down, the run got
cancelled, or the job was abandoned — sat in memory for as long as the
worker process did, which with one worker and a long-lived unit (§3.2) is
weeks. `hold()` and `release()` now sweep expired entries on every call,
and cancelling a run discards the credentials of any of its still-`queued`
jobs, since a `running` job has already fetched its credential and keying
the discard by run rather than job would be exactly the mistake this
section forbids elsewhere. The sweep inside `release()` has to run
*after* the requested credential is popped, or an expired credential
reports "no credential held" — the same message a never-approved job
gets — instead of "expired; re-approve," a worse diagnosis for the
operator to work from. Two more details close the same class of gap:
`type(job_id) is not int` is checked explicitly rather than
`isinstance()`, because `bool` is a subclass of `int` in Python and
`hash(True) == hash(1)`, so a message carrying `{"job_id": true}` would
release the credential held under key `1`; and every field that ever
holds the credential (the store's internal record, `PhaseContext`,
`transfer.PullTarget`) is declared `field(repr=False)`, a structural
guard against the day some future `log.debug("ctx=%r", ctx)` writes it to
journald.

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
  at all. That is acceptable, since a compromised sibling already has
  direct access to every device credential that reaches it — it is the
  process that opens the Netmiko session the credential authenticates.
  What the check does buy is real: a stray or duplicated
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

`nethub/credential_socket.py`'s `systemd_socket()` is exactly this
adoption call, and it is built to fail toward "don't serve" rather than
toward "bind something wrong": it returns `None` when the process was
not socket-activated, and the caller skips serving entirely rather than
falling back to creating a path with the wrong ownership. Every dev run
and every test takes that branch — tests construct their own socket
directly — which is worth knowing when reading the test suite: none of
it exercises the systemd-activation path itself, only the protocol
spoken once a connection exists.

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
  `/var/lib/systemd/coredump`. The reference Quadlet unit's `[Service]`
  block now sets `LimitCORE=0`, which covers the deployed path; the
  process setting `PR_SET_DUMPABLE` to 0 — which would also deny same-uid
  `ptrace` and `/proc/<pid>/mem`, the attack the shared uid would
  otherwise leave open — is not yet done, and neither protection reaches
  a bare `flask run` or `upgrade_cli.py` run outside the unit.
- **Debug mode turns an exception into a credential disclosure.**
  Werkzeug's interactive debugger renders frame locals into an HTTP
  response, and the reloader runs two processes. `DEBUG` must be off
  before the credential path exists, and `config.py` already reads it
  from an environment variable and defaults it to off — precisely
  because the local username/password login this alpha added made the
  same class of disclosure reachable through a submitted password even
  before the device-credential path exists. It remains a release blocker
  for the full approval flow's device credentials, same reasoning, wider
  blast radius: don't flip the default back to `True` or make it easier
  to turn on than the current env-var opt-in.
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
with the phase execution, and §7.3's sweep moves the execution to
`abandoned` — which is the correct outcome, since re-approving is how
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
  against the two places with no backstop behind the digest (§4.3.1's
  unverified distribution host, §4's plain-HTTP day-0). Full reasoning
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
- **Whether the pull transport's prompt sequence is what IOS-XE actually
  emits.** `nethub/devices/transfer.py`'s `_pull_sftp` answers a
  `Destination filename` prompt and then a `Password:` prompt, in that
  order, and names only the filesystem as the destination specifically
  to force the first prompt to appear so the pair is deterministic. That
  is what the platform is expected to do, not what has been observed —
  the same class of unknown the transfer library question above was, and
  it should be settled the same way, by running it against a real device
  and recording the answer here. This is the one item from the pre-
  Netmiko design that outlived the rewrite unresolved. A wrong prompt
  list does not fail fast: it hangs until the read timeout. Capture the
  session with Netmiko's `session_log` **off** (hard rule, §4.3.1's
  reasoning on why the pull adapter must never enable it) and replay it
  offline rather than logging the live channel.
- **Whether `_pull_sftp`'s unanchored `read_until_pattern(r"[>#]")` can
  terminate mid-copy.** The pattern matches the first `>` or `#` anywhere
  in the stream, and `copy sftp://…` is exactly the kind of long-running
  device command that can emit a progress indicator before it actually
  finishes. If IOS-XE's `copy` emits anything containing either character
  while the transfer is still in progress, the adapter would return
  early and `verify_sha512` would then hash a partial file rather than
  failing loudly. This is answerable from the same real-device run the
  prompt-sequence question above needs, not a separate one.
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
- **Whether the credential socket needs a wall-clock bound in addition to
  its per-operation ones (§9.2).** `settimeout` on the listening side is
  per-`recv`/`send` call, not cumulative, so a peer that trickles one byte
  every few seconds never trips any single read's timeout while
  occupying the sibling's only dispatch loop for as long as it keeps
  doing that — on the order of a full day before anything notices.
  Bounding total elapsed time per connection (not just per operation)
  closes it; worth doing before this channel is exposed to anything less
  trusted than a same-host, same-uid peer.
- **Whether to build a purpose-built transfer script instead of relying
  on deprecated Paramiko indefinitely.** The options tried so far are
  both unsatisfying: `libssh` doesn't work under either library binding,
  and Paramiko works but is deprecated upstream. This is now less urgent
  than it was, and worth restating why: the pull transport (§4.3.1)
  reaches the same outcome without Netmiko's file-transfer class or
  Paramiko at all, so a deployment blocked by the deprecation has a
  supported way out that is a settings change rather than a code change.
  That is a mitigation for the deadline, not an answer to the question —
  push remains the default and the default should not be the one riding
  a removed library. A small, single-purpose Python script that does
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
  that owe a restore — with the §5 caveat that null also means "pull
  transport, nothing to restore", so any process acting on it must join
  `image_transport_used` rather than reading the column alone. A
  deployment running pull does not have this gap at all, which is a
  mitigation available today but not an answer: push is the default and
  the default is where the gap lives. What's still open is whether to
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
  is answered by construction too, for both directions the file could
  travel: there is one connection-opening code path in the tree, and
  every session — primary or transfer — goes through it.
- **Whether day-2 images should ever be pullable over HTTP as well as
  SFTP.** Pull currently requires an SFTP daemon and a credential, and
  the credential is the expensive part of it (§4.3.1): it is the thing a
  redirected session can collect, and the thing that forced a
  `distribution_credential_source` setting into §5. An HTTP pull would
  have none of that — no credential to phish, no account to hold, no
  second daemon since day-0 already runs one — and `verify /sha512`
  already provides the integrity property that would be doing the real
  work either way. What stops it from being obviously right is §3.3: the
  day-0 docroot is deliberately not a read path over the image store,
  and an image reachable by guessing a filename is exactly the
  arrangement that section refuses. Doing it properly means minting
  capability paths for images the way §3.3 mints them for day-0
  configs — reuse rather than a new mechanism, but not free, and it
  makes the day-0 daemon part of the day-2 path for the first time.
  Worth deciding deliberately; it would simplify §4.3.1's pull half more
  than anything else on this list.
- **Whether the transport should ever be per-host rather than
  per-deployment.** It is one setting for the whole deployment today,
  and the argument for keeping it that way is §4.3.1's: the transport
  selects whose credential is spent, so it is not a knob a request may
  touch. But a deployment is not necessarily uniform — a fleet with one
  branch site behind a perimeter that blocks device egress and a core
  site that permits it has a real reason to want both, and the current
  design makes it choose the mode that works everywhere. A
  NetHub-managed per-host or per-group override (admin-set, never
  request-set) would fit inside the existing rule; whether real fleets
  are mixed enough to need one is a question about deployments rather
  than about the mechanism.
- **When distributed distribution comes back (§2, §3.3).** NetHub is the
  sole source of the bytes and multi-site mirrors are out of scope. The
  constraint that bites first is a branch site on a narrow link, where
  every byte crosses the WAN from NetHub in whichever direction the
  transport runs. §3.3 is the seam. Reopening it is now two different
  questions rather than one, and the transports differ sharply in
  difficulty: a push mirror needs a process near the devices that
  performs the pushes and authenticates to them, which is most of a
  second NetHub; a pull mirror needs only an SFTP daemon holding a
  verified copy of the subtree, because the device does the work and the
  digest check already catches a mirror serving the wrong bytes. If
  multi-site support is ever built, pull is the cheaper path to it, and
  that is worth knowing before the decision rather than after.
- **Whether Flask and the sibling should run as two rootless users
  rather than one (§9.2).** Under a single user, the socket's mount
  scope is the only thing distinguishing callers and `SO_PEERCRED`
  cannot discriminate. Two users make the peer check mean something, at
  the cost of a second rootless Podman stack and more awkward sharing of
  the volumes both need. Worth deciding before the socket is built,
  since it is cheap now and a migration later.
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
  (`device_host_keys` exists today; the transport-repointing scenario
  needs `settings`) don't both exist yet — but the `artifacts` half is
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
