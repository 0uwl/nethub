# NetHub — Design Document

*A new, standalone project — not a [Drawbridge](https://github.com/0uwl/drawbridge) fork/rebrand. It takes
architectural lessons and components from both the existing Drawbridge
ZTP system and the earlier standalone "Network Software Depot" concept
and combines them into one unified device lifecycle dashboard.
Drawbridge will be deprecated in favor of this project once its
provisioning module reaches parity.*

## 1. Overview

NetHub covers two halves of a device's lifecycle in one
system:

- **Provisioning** (day-0) — a new device phones home, is checked
  against a serial allowlist, and receives its initial config/image.
- **Software Lifecycle** (day-2) — onboarding new IOS-XE software
  images into the `image_registry` group_vars structure, staging them
  to a distribution server, and triggering fleet upgrades via Ansible —
  all through the same admin UI and backend.

One Flask backend, one database, one admin frontend, one deployment
unit. The device-facing provisioning endpoint is the only
unauthenticated route in the system; the admin UI (including all
Software Lifecycle functionality) sits behind an authenticated session.

## 2. Goals / Non-Goals

**Goals**
- Single dashboard for provisioning new devices and maintaining
  software on devices already in the fleet.
- Image upload → checksum verification → staged publish → registry
  update → audit trail.
- A provisioning security model that's honestly documented rather than
  one that implies more protection than it delivers.

**Non-goals**
- Not a general job scheduler / orchestration platform (not an AWX
  replacement).
- Not an inventory management system — device/serial data is retained
  only as long as operationally needed (allowlist, bounded provisioning
  log, job audit trail), not as a persistent asset database.
- Does not perform the device upgrade's actual command sequence itself
  — that logic stays in the existing Ansible playbook.
- No image transformation/repackaging.
- Not multi-vendor on day one — see Vendor Scope.

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
Software Lifecycle module — image upload form, registry browser, and
the software-lifecycle side of the unified log viewer. Within that
viewer, provisioning history and software-lifecycle job history stay as
separate, distinctly labeled logs rather than being merged into one
timeline. Each entry is a single row describing one event (a
provisioning attempt, a registry job) — the system does not correlate
entries with each other or attach them to a persistent device record;
that would make it an inventory manager, which it explicitly isn't (see
Non-Goals).

### 3.2 Backend
One Flask service, organized as two logical modules:

- **Provisioning module** — phone-home endpoint, allowlist checks,
  DHCP integration, provisioning log.
- **Software Lifecycle module** — upload handling and SHA-512
  verification, bundle-key validation, per-job Ansible EE invocation via
  `ansible-runner`, registry file update under a lock, job audit
  logging.

Every Software Lifecycle route requires an authenticated admin session.
The phone-home route stays the system's sole unauthenticated entry
point, scoped narrowly to its existing contract.

Runs as a single process — no multi-worker/concurrency handling in the
Flask app itself. The traffic volume here (occasional device
phone-homes, occasional admin-triggered uploads) doesn't warrant that
complexity, and the actual heavy lifting for both modules happens
inside the Ansible EE containers the backend invokes, not in the Flask
process.

### 3.3 Distribution — day-0 vs. day-2
Both days serve bytes out of the **same staging tree on the same
distribution host** (see §3.4) — they differ only in the access method
bolted onto it, and in who is trusted to use it:

- **Day-0**: ZTP script and image/config delivered to a new device as
  part of the phone-home flow, over plain HTTP (see §4 for the transport
  decision). No separate dedicated bootstrap container.
- **Day-2**: images pulled by an already-enrolled device over SSH
  (`copy scp://…` in `upgrade_iosxe.yml`; the same sshd serves SFTP for
  staging), authenticated with credentials from the vaulted inventory.

The distribution target stays modular — a local bundled container or an
existing remote host, chosen via backend configuration. Whichever is
used, it exposes one directory tree through two daemons rather than
maintaining two separate stores.

### 3.4 The artifact pipeline — one ingest, two egress adapters
NetHub's two halves are one subsystem at the storage layer and diverge
only at the very last step. Everything is a single pipeline:

```
ingest (upload → hash → size → store → record)
   │
   ├─ egress A (day-0): device pulls, HTTP, allowlist-gated, device-initiated
   └─ egress B (day-2): device pulls, SCP,  credentialed,   admin-initiated via Ansible
```

**One artifact record.** A day-0 config, the ZTP script itself, and a
day-2 IOS-XE image are the same shape — blob, SHA-512, size, platform,
uploader, timestamp — differing only by a `kind` discriminator. One
upload path, one hash function, one table (§5). The protocol split is an
access-method detail in the egress adapters, not two subsystems.

**One hash, computed once, consumed three times.** Ingest computes the
SHA-512 once; it is then read by (a) day-0 payload verification, (b) the
rendered registry entry, and (c) the device's own `verify /sha512` step
during a day-2 upgrade. No component recomputes it.

**Shared log mechanism, deliberately unshared semantics.** Provisioning
history and software-lifecycle job history share a row shape, a
retention-purge helper, and a viewer component, with a discriminator
column keeping them as the separate, distinctly-labeled logs §3.1
requires. Shared plumbing; no merged timeline, no cross-entry
correlation.

**Explicitly not unified**: day-0 does not route through Ansible (it is
Kea + phone-home and needs no EE); the two logs are not merged into one
timeline; and the two egress protocols stay distinct, because the shared
store beneath them is what makes that split cheap.

## 4. Security Model

**Decision: the device-facing provisioning endpoint serves over plain
HTTP, not HTTPS.** (This applies specifically to the phone-home /
image-and-config delivery path to devices — the admin web dashboard is
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

The system's actual protection comes from three things, unchanged by
this decision:
- **Network isolation** — the provisioning VLAN, restricted to
  attached devices via 802.1X / port security / DHCP snooping.
- **Serial allowlisting** — unregistered devices get nothing beyond a
  normal DHCP lease and the generic script.
- **Payload hash verification** — SHA-256 (or SHA-512, matching the
  Software Lifecycle registry) checks on delivered images/configs.

This must be documented prominently for anyone deploying the system,
in the same spirit as Drawbridge's existing warning banner: **classic
ZTP is inherently less secure than Secure ZTP (RFC 8572)/MASA-based
provisioning**, and this system does not attempt to close that gap —
it only hardens classic ZTP with infrastructure the operator already
controls.

One residual nuance worth writing down rather than leaving implicit: if
the hash accompanies the payload over the same unauthenticated channel,
an active on-path attacker who can already tamper with the plaintext
response could in principle alter both the payload and its hash
together. Hash verification here is real protection against passive
corruption, a misconfigured server, or a compromised upstream file —
not an independent control against an attacker already on the
provisioning VLAN. That was equally true before this decision (the
prior TLS layer wasn't closing that gap either, per the rationale
above); it's just now stated directly rather than left to imply
otherwise.

**Day-2 image delivery deliberately stays credentialed (SCP), unlike
Day-0.** The `upgrade_iosxe.yml` playbook has the device pull its image
via `copy scp://...`, authenticated with credentials from the vaulted
inventory, rather than the plain HTTP used for the Day-0 script fetch
(§3.3). This is not an inconsistency — the two paths are in different
trust situations. A Day-0 device has no credentials to offer regardless
of transport, so plain HTTP costs nothing extra there. A Day-2 device is
already enrolled, and SCP's credentials are the actual access-control
gate on who can pull an image, not just transport encryption — dropping
them for protocol uniformity would remove real authorization that hash
verification does not replace (hash verification confirms the fetched
bytes weren't tampered with; it says nothing about who was allowed to
fetch them in the first place).

## 5. Data Model

- `artifacts` table — the single ingest record behind both days (§3.4):
  `id`, `kind` (script/config/image), `platform`, `filename`, `sha512`,
  `file_size`, `remote_dir`, `version`, `uploaded_by`, `uploaded_at`.
  Every byte NetHub serves, on either day, has exactly one row here.
- `image_registry.yml` (git-tracked, unchanged shape) — **a rendered
  projection of the `artifacts` table, not an independent source of
  truth.** Its `image_bundle` entries are serialized artifact records
  field-for-field (`filename`, `sha512`, `version`, `remote_dir`,
  `file_size`), and the publish job is its sole writer. It remains the
  file the upgrade playbook reads from; it is simply no longer
  hand-maintained, so the two cannot drift.
- `registry_jobs` table: `id`, `platform`, `bundle_key`, `version`,
  `filename`, `sha512`, `submitted_by`, `distribution_mode`
  (local/remote), `status`, `started_at`, `finished_at`,
  `playbook_log_path`, `registry_commit_sha`.
- Provisioning-side tables (allowlist, bounded provisioning log) carried
  forward from the existing ZTP design, retention-bounded as before. The
  provisioning log and `registry_jobs` share a row shape and retention
  helper per §3.4, distinguished by kind rather than merged.

## 6. Workflow

**Day-0**: device phones home over HTTP → allowlist check → DHCP hooks
→ image/config delivery → hash verification → provisioning logged.

**Day-2** (behind admin auth): admin submits bundle key/version/file/
checksum → backend verifies SHA-512 against staged bytes → bundle-key
validated against registry (overwrite requires confirmation) → per-job
inventory built for the active distribution target → `publish_image.yml`
run via `ansible-runner` against the pinned EE image → on success,
registry locked, updated, committed → job result and log recorded and
surfaced in the dashboard.

## 7. Ansible EE Integration

Reuse a pinned EE image, invoke via the `ansible-runner` Python API, a
minimal per-job `private_data_dir` and single-host inventory rather
than the full fleet inventory, and a dedicated `publish_image.yml`
selected by `platform`.

**NetHub always populates `file_size` in the rendered registry entry.**
Because ingest measures the file in the same pass that hashes it (§3.4),
the size is authoritative before any device is ever contacted. This
retires `upgrade_iosxe.yml`'s remote-size discovery cascade — the
per-run localhost size cache, the `scp_server` stat fallback, and the
`files/remote_image_size.py` helper it shells out to (referenced by the
playbook but never written). Those exist only to answer a question
NetHub already knows the answer to; with a NetHub-rendered registry they
are dead paths and can be deleted from the playbook.

## 8. Deployment

- Podman Quadlet units for the Flask app, plus DHCP integration
  running natively as its own service, plus — if local distribution
  mode is used — a Quadlet unit for the bundled SFTP container, dual-
  purposed to also serve the day-0 ZTP script over plain HTTP (no
  separate bootstrap container).
- The nested-container question remains open: `ansible-runner`
  invoking Podman from inside a rootless container needs either the
  host's rootless Podman API socket mounted in, or the EE-invocation
  step run as a sibling process outside the main container's
  containment boundary.

## 9. Open Questions

- New repository, built clean for the backend — reusing applicable
  existing pieces (e.g. Kea configuration) rather than forking the repo
  wholesale. The frontend is the exception: it's carried over from
  Drawbridge as-is and extended in place (see §3.1), not rebuilt.