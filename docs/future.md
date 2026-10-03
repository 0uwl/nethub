# Future
Code: none (this file describes what is not built) · Tests: none

---

## AI READING INSTRUCTION

Read `[SPEC]` and `[BUG]` blocks for authoritative facts.
Read `[NOTE]` only if additional context is needed.
`[?]` blocks are unverified; treat them with lower confidence.
§1 lists what is **not built**: check the code before assuming any of it
exists. §2 holds the target design for each unbuilt piece, §3 rejected
alternatives, §4 open questions. Known gaps in built code live in each
topic file's last section, not here.

---

## 1. Not built

**[SPEC]**
None of these exist in the code:
- day-0 provisioning: phone-home, serial allowlist, Kea/DHCP, minted
  fetch paths, `provisioning_log`;
- OIDC login, enrollment tokens, a `nethub-admin` break-glass CLI,
  `local_accounts_enabled`;
- a server-side `sessions` table, with per-session idle and absolute
  timeouts (revocation works today through a per-user epoch in the
  cookie; auth-and-roles.md);
- any retention purge: runs, rows and image bytes all stay until someone
  deletes them;
- per-job log files: the `log_path` column exists, but nothing writes it;
- a JSON API (a `202` with a job id, polling, an error envelope);
- deployment settings in the `settings` table (`DEVICE_TARGET_CIDRS` and
  `ARTIFACT_STORE` are environment variables);
- a supersede flow for artifacts;
- tamper-evidence for artifact digests, pins or settings
  (`check-store` only catches bytes drifting from their row);
- a device-side pull transport, and shared device-account mode;
- a fully non-interactive (unattended) run.

---

## 2. Target design for unbuilt pieces

### 2.1 Day-0 provisioning
**[NOTE]**
Planned as a **separate service** beside NetHub, sharing its artifact
ingest and its single `artifacts` table, with `kind` set to `script` or
`config`. Those kinds are already allowed by the schema and by
`ingest(kind=...)`, but no route uploads them, and the listing and lookup
code only handles `image`.
- **Flow.** DHCP option 67 points a new device at one fixed generic
  script on a static HTTP server. The script POSTs its serial to Flask's
  phone-home route. Flask *claims* an allowlist entry and mints
  high-entropy, one-shot fetch paths (symlinks under `mint/`), and the
  device fetches those.
- **Authorisation lives in the phone-home handler only.** The static
  server decides nothing. An unminted path under `mint/` answers with a
  fixed decoy `200`, not a `404`, so the server is not an enumeration
  oracle.
- **The allowlist claim.**
  - It is a conditional `UPDATE … WHERE serial=:s AND state='armed' AND
    expires_at > :now` inside `BEGIN IMMEDIATE`, and the rowcount is the
    authorisation.
  - One-shot is scoped to a provisioning *window* of minutes, not to one
    request.
  - Uniqueness is `UNIQUE(serial) WHERE state='armed'`, so an RMA'd
    chassis can be enrolled again.
  - Re-arming is an explicit admin action that creates a new row
    (`rearmed_from_id`).
- **Same answer for every serial.** Known and unknown serials get the
  same response shape and timing.
- **Kea reservations** are rendered whole from the allowlist and
  reconciled by the sibling, never by Flask.
- **Transport is plain HTTP, on purpose.** A device with no trust anchor
  cannot authenticate TLS. The protection comes from VLAN isolation,
  serial allowlisting and payload hashes instead. The stated cost: a
  passive listener on the provisioning VLAN can read the bootstrap config
  (AAA secrets, SNMP communities). Classic ZTP is weaker than RFC 8572
  Secure ZTP, and the docs must say so.
- **Abuse visibility.**
  - Rate limits apply per source and per *existing* serial.
  - Counters for unknown serials aggregate per /32 or /64, with a top-N
    and an `__overflow__` bucket.
  - Counter writes are buffered off the request path.
  - Alert on a denial for a serial that was consumed inside its own TTL
    window, and on an allowlisted serial that never phones home.
- **Budget:** a 2 s p99 on phone-home, against a 15-minute device
  backoff. These numbers are asserted, not measured.
- **The log** records what was *offered*, never "served".
- **Never serve day-2 images from the day-0 docroot.**

### 2.2 OIDC and server-side sessions
**[NOTE]**
- **OIDC login.**
  - Authorization code with PKCE, using a maintained library.
  - Users are keyed on `(issuer, sub)`, not email.
  - The local `users` table still decides access. The role comes from a
    group claim (`oidc_group_claim`, `oidc_admin_group`) and is shown
    read-only.
- **A missing claim is not a demotion.** A claim present without the
  admin group demotes. A claim absent from the token keeps the previous
  role and raises a fault.
- **Admin safety.**
  - A role computation that would leave zero admins is refused.
  - A `nethub-admin` CLI provides break-glass access.
  - `local_accounts_enabled` closes the "make myself a local admin before
    the IdP demotes me" route.
- **Local accounts.** Enrollment uses a one-shot, TTL-bounded token,
  never a password an admin chooses for someone else.
- **Sessions as rows.** Stored as `sha256(token)`, with idle and absolute
  timeouts and a fresh id at login. `is_active` and `role` are re-read on
  every request. The absolute timeout is the real bound on how quickly an
  IdP-side demotion takes effect.
- **Rate limits** like day-0's would also apply to `/login` (which today
  has only the per-account lockout) and to the OIDC callback.

### 2.3 Retention
**[NOTE]**
- **Runs**, with their hosts, jobs and results: 365 days, purged as a
  unit.
- **Provisioning log**: 90 days.
- **Artifact rows** are kept while anything references them. **Artifact
  bytes** are a separate axis: superseded bytes are pruned
  (`bytes_state`, `bytes_pruned_at`) and the row stays.
- **Never purged:** pins and the append-only audit tables
  (`device_host_key_audit`, `user_admin_audit`, `artifact_audit`,
  `settings_audit`).
- The purge runs **in the sibling**, on a timer. It must delete
  `upgrade_host_phase_results` before runs (schema.md), and remove a
  job's log file with its row once logs exist.

### 2.4 Non-interactive runs
**[NOTE]**
A run that goes from submit to completion with nobody at the gates needs
four things:
- a rule for continuing after a `partial` phase without a person
  reviewing it;
- the sibling creating queued rows for gated phases (today it creates
  only the chained `verify`);
- a ciphertext rule for the whole run, replacing "only while queued";
- an audit column recording that nobody approved the gate.

Scheduled approvals (dispatch.md) were built instead.

### 2.5 Pull transport and shared account mode
**[NOTE]**
- **Pull transport** would need:
  - an SFTP server chrooted to the store;
  - a credential source (the submitter's own, or one read-only account
    given only to the sibling, never stored as a `settings` row);
  - a hardware test of the IOS-XE `copy` prompts;
  - a transport column on `upgrade_runs`.

  It can only be a deployment setting, never a per-request choice.
  IOS-XE's SSH client does not verify the server it dials.
- **Shared account mode** would need the username stored as an audited
  setting, a snapshot column on `upgrade_runs`, and the pages saying so.
  It gives up device-side attribution, and must never be inferred from a
  missing IdP.

---

## 3. Rejected alternatives

**[NOTE]**
- **MD5 instead of SHA-512.** See artifacts.md §5.
- **OpenSSH `scp` as a subprocess.** It has no safe way to receive the
  password. If it is ever built: give it a pipe on stdin, set
  `StrictHostKeyChecking=yes`, and render `known_hosts` from the pin.
- **`libssh` for transfers.** It broke repeatedly at image size.
- **Ansible Vault for the device credential.** The credential is never
  in a file.
- **An enable secret or `enable` escalation.** See credentials.md.
- **A credential socket between Flask and the sibling.** A web restart
  lost every held credential. Credentials are sealed in the row instead.
- **The Drawbridge frontend, or Tailwind/DaisyUI.** See frontend.md.
- **A rendered registry file, publish jobs, `flock`, or git commits for
  publishing.** The `artifacts` table plus two indexes replaced all of
  them.
- **AAA-server integration to validate credentials.** It would add a
  standing trust relationship and a shared secret. The one-host login
  gate (dispatch.md) does the job.

---

## 4. Open questions

**[?]**
- Can an IOS-XE upgrade legitimately regenerate the device's host key?
  That decides whether `wait_for_device` should stop retrying on
  `HostKeyError` (device-layer.md).
- When Paramiko's removal has a date, should a purpose-built SCP push
  replace Netmiko's transfer class?
- Should the sibling actively restore SCP servers left enabled, using
  `scp_restore_confirmed` null/false rows, or should operators check by
  hand?
- For multi-site fleets, would a pull mirror (an SFTP daemon near the
  devices) be cheaper than pushing over the WAN?
- Should Flask and the sibling run as two rootless users, so a uid
  boundary protects the private key?
- Tamper-evidence: an append-only hash chain over `artifacts`, a
  projection countersigned by the sibling, or a re-check before dispatch?
- Should the credential be tested on one host per AAA policy group
  instead of one host per phase?
- Should there be a bulk host-key confirm for onboarding a whole fleet,
  and what would stop it reopening first-contact trust?
- Do multiple SSH sessions per user (one for exec, one for SCP) trip
  per-user session caps or command-authorisation differences on real
  deployments?
- Day-0: pin a certificate inside the generic script, to encrypt
  second-hop fetches? Give day-0 artifacts their own promotion gate?
- Sessions: how should the absolute timeout be sized against the IdP's
  revocation delay? May a user hold more than one session?
