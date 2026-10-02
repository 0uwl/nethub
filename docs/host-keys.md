# Host keys
Code: `nethub/upgrade_routes.py` (`hostkeys_bp`, `may_confirm`, `SCAN_CONFIRM_WINDOW`), `nethub/upgrades.py` (`check_target`, `confirmed_key`, `delete_pin`), `nethub/settings.py` (`second_person_delete`), `nethub/models.py` (`DeviceHostKey`, `HostKeyScan`, `DeviceHostKeyAudit`), `nethub/sibling.py` (`run_scan_once`), `nethub/devices/phases.py` (`pinned_key`) · Tests: `tests/test_upgrade_routes.py`, `tests/test_roles.py`, `tests/test_upgrade_models.py`, `tests/test_sibling.py`, `tests/test_templates.py`

---

## AI READING INSTRUCTION

Read `[SPEC]` and `[BUG]` blocks for authoritative facts.
Read `[NOTE]` only if additional context is needed.
`[?]` blocks are unverified; treat them with lower confidence.
§1–§3 are rules: each H3 heading is a rule's slug. §4 explains how it
works, §5 decisions, §6 known gaps.

How a connection uses a pin (`connect()`, the paramiko policy) is in
device-layer.md §1.

---

## 1. Which addresses a run may name

### 1.1 [target-is-an-ip-in-a-cidr]
**[SPEC]**
`upgrades.check_target` accepts only an IP literal inside one of the
`DEVICE_TARGET_CIDRS`. A hostname is refused, never resolved, and an
empty CIDR list refuses every address. Submitting a run and requesting a
scan both go through it.
Pinned by: `tests/test_upgrade_routes.py::TestTargetValidation::test_a_hostname_is_refused_rather_than_resolved`, `tests/test_upgrade_routes.py::TestTargetValidation::test_no_configured_cidr_refuses_everything`, `tests/test_upgrade_routes.py::TestTargetValidation::test_an_address_outside_the_cidr_is_refused`, `tests/test_upgrade_routes.py::TestHostkeyScanDispatch::test_a_hostname_is_refused`

**[NOTE]**
A hostname could resolve differently at the CIDR check and at the
connection. Pins are keyed on the address string, and a name's meaning
can change later. An unset security setting is not "allow all".

### 1.2 [no-confirmed-pin-no-run]
**[SPEC]**
`submit` refuses an address that has no *confirmed* `device_host_keys`
row (`confirmed_key`). An unconfirmed row counts as absent. At dispatch,
`phases.pinned_key` checks again and fails that host as `hostkey`.
Pinned by: `tests/test_upgrade_routes.py::TestSubmit::test_an_unconfirmed_address_cannot_be_targeted`, `tests/test_upgrade_routes.py::TestSubmit::test_a_seen_but_unconfirmed_pin_is_still_refused`, `tests/test_phases.py::TestPinnedKey::test_a_seen_but_unconfirmed_key_is_refused`

**[NOTE]**
Pinning fails closed only on a *changed* key. "An operator names a
machine they control" is always a first contact, so first contact has to
be a separate, deliberate step. Pre-check runs at submit with no gate,
so confirmation cannot wait for an approval.

### 1.3 [one-pin-per-address]
**[SPEC]**
`device_host_keys.ansible_host` is unique, with one pinned `key_type`
per address. A device offering a different algorithm is a mismatch, and
no second pin is added.
Pinned by: `tests/test_upgrade_models.py::TestDeviceHostKeys::test_one_pin_per_address`, `tests/test_connection.py::test_policy_refuses_a_different_key_type`

**[NOTE]**
If uniqueness were keyed on `(address, key_type)`, an attacker could get
a fresh first-contact prompt just by offering an unpinned algorithm.

---

## 2. Scanning and confirming

### 2.1 [scan-is-dispatched]
**[SPEC]**
`POST /hostkeys/scan` checks the address (rule 1.1) and inserts a
`queued` `host_key_scans` row; Flask never opens the connection. The
sibling claims and runs the scan with the same conditional claim and
sweep as phase jobs.
Pinned by: `tests/test_upgrade_routes.py::TestHostkeyScanDispatch::test_a_valid_address_queues_a_scan_without_blocking`, `tests/test_sibling.py::TestHostKeyScanDispatch::test_only_one_sibling_wins_a_scan`, `tests/test_sibling.py::TestHostKeyScanDispatch::test_a_stale_running_scan_is_abandoned_by_sweep`

### 2.2 [confirm-reads-the-scan-row]
**[SPEC]**
`POST /hostkeys/confirm` takes only `scan_id`. The address, key type and
fingerprint come from that scan row, never from the request body.
Pinned by: `tests/test_upgrade_routes.py::TestConfirmHostkeyBinding::test_confirm_ignores_extra_fields_in_the_post_body`, `tests/test_upgrade_routes.py::TestConfirmHostkeyBinding::test_confirming_a_succeeded_scan_pins_the_address`

### 2.3 [confirm-refusals]
**[SPEC]**
A confirm is refused unless all of these hold:
- the scan `succeeded`;
- `may_confirm` allows this user;
- `consumed_at` is null, so a scan backs one confirmation at most;
- the scan finished within `SCAN_CONFIRM_WINDOW` (15 min).

A successful confirm sets `consumed_at`.
Pinned by: `tests/test_upgrade_routes.py::TestConfirmHostkeyBinding::test_a_failed_scan_cannot_confirm`, `tests/test_upgrade_routes.py::TestConfirmHostkeyBinding::test_a_queued_scan_cannot_confirm`, `tests/test_upgrade_routes.py::TestConfirmHostkeyBinding::test_an_already_consumed_scan_cannot_confirm_twice`, `tests/test_upgrade_routes.py::TestConfirmHostkeyBinding::test_a_stale_scan_outside_the_freshness_window_is_refused`

### 2.4 [who-may-confirm]
**[SPEC]**
`may_confirm(scan, user)` decides who may confirm a scan:
- With `two_person_hostkeys` off, only the scan's requester may.
- With it on, anyone except the requester may, and an admin may confirm
  any scan, their own included.

The rule is read at confirm time, so turning it off releases a scan that
was waiting for a second person. A requester blocked by the rule is told
why. Anyone else who may not confirm sees only "No matching scan", which
reveals nothing about the scan.
Pinned by: `tests/test_upgrade_routes.py::TestConfirmHostkeyBinding::test_a_scan_belonging_to_a_different_user_is_refused`, `tests/test_roles.py::TestHostKeyRule::test_with_it_on_someone_else_confirms`, `tests/test_roles.py::TestHostKeyRule::test_an_admin_confirms_their_own_scan_and_removes_alone`, `tests/test_roles.py::TestHostKeyRule::test_turning_the_rule_off_releases_a_waiting_scan`

### 2.5 [changed-key-not-reconfirmed]
**[SPEC]**
Confirming a scan for an address whose confirmed pin has a *different*
key is refused; the pin must be deleted first, which leaves an audit row.
A scan with the same key re-confirms the pin in place (a new
`confirmed_by` and `confirmed_at`), and an unconfirmed row is overwritten.
Pinned by: none

**[NOTE]**
Re-accepting a changed key looks exactly like the attack the pin exists
to catch. The extra step of deleting the pin first is deliberate.

---

## 3. Removing pins and the audit trail

### 3.1 [pin-delete-may-need-a-second-person]
**[SPEC]**
- `upgrades.delete_pin` goes through
  `settings.second_person_delete('two_person_hostkeys', ...)`.
- When the rule binds the user, the first delete is only a request: a
  conditional update `WHERE delete_requested_by IS NULL`. Someone else's
  delete then removes the pin.
- The pin stays in force while a request is pending.
- A request cannot be withdrawn and does not expire.

Pinned by: `tests/test_roles.py::TestHostKeyRule::test_with_it_on_removing_a_pin_needs_a_second_person`, `tests/test_frontend.py::test_a_pin_removal_without_its_box_removes_nothing`

### 3.2 [hostkey-audit-pre-image]
**[SPEC]**
Every confirm, delete request and delete writes a
`device_host_key_audit` row with the action, actor, `actor_role`,
`requested_by` and a key: the newly confirmed key for a confirm, or the
key being removed for a delete. The row is keyed on the address string,
not a foreign key, so it outlives the pin.
Pinned by: `tests/test_upgrade_routes.py::TestHostkeyAudit::test_confirming_leaves_an_audit_row_with_the_new_fingerprint`, `tests/test_upgrade_routes.py::TestHostkeyAudit::test_deleting_a_pin_leaves_an_audit_row_with_the_pre_delete_fingerprint`, `tests/test_upgrade_routes.py::TestHostkeyAudit::test_the_audit_row_survives_the_pins_deletion`

**[NOTE]**
There is no "changed" action. A key can only change by being deleted and
then confirmed again, and both steps are recorded.

### 3.3 [pins-never-expire]
**[SPEC]**
Pins and their audit rows are never purged or expired. They are removed
only by a deliberate delete.
Pinned by: none

**[NOTE]**
Expiring a pin would quietly turn a fail-closed mismatch back into a
first-contact prompt. Pins are the one per-address record NetHub keeps,
and they are a security control, not an inventory.

---

## 4. How it works

**[SPEC]**
| route | method | does |
|---|---|---|
| `/hostkeys` | GET | lists pins, plus every scan still confirmable (succeeded, unconsumed, inside the window) |
| `/hostkeys/scan` | GET, POST | form; POST queues a scan |
| `/hostkeys/scan/<id>` | GET | result page; meta-refreshes while queued or running; shows the confirm deadline |
| `/hostkeys/confirm` | POST | confirm from `scan_id` |
| `/hostkeys/<id>/delete` | POST | delete or request a delete; needs the `confirm=yes` box |
| `/hostkeys/history/<address>` | GET | audit trail for one address |

Scan status is `queued`/`running`/`succeeded`/`failed`/`abandoned`. A
scan spends no credential, because the host key is exchanged before
authentication. Every route requires login, and operators may use all of
them.

---

## 5. Decisions

**[NOTE]**
- **The pin is keyed on the address, not on a device identity.**
  - Why: NetHub is not an inventory. It remembers what answered at an
    address, so that a change is visible.
- **Confirming is separate from submitting and approving.**
  - Why: pre-check runs at submit with the submitter's own credential.
    Pinning there would let whoever chose the address also pin its key,
    silently.
- **The confirmation is bound to a scan NetHub itself performed.**
  - Why: a fingerprint typed into a form is not evidence.
  - The two-person rule is what puts a second person on it. With the rule
    off, the binding proves only that the key matches one NetHub saw.
- **There is no bulk confirm.**
  - Reopen if: onboarding a whole fleet one address at a time proves too
    slow (future.md).

---

## 6. Known gaps

**[SPEC]**
- `device_host_key_audit` has no append-only triggers, unlike
  `user_admin_audit`, `artifact_audit` and `settings_audit`
  (`models._append_only`).
- `/hostkeys/history/<address>` is linked only from addresses still in
  the pin list. The history of a fully deleted address is reachable only
  by typing the URL.
- None of this stops a Flask-side compromise from rewriting a confirmed
  pin in the database. There is no tamper-evidence (future.md).
- Weak or missing pins:
  - 2.5 and 3.3 have no tests.
  - 3.1: nothing tests that a request cannot be withdrawn or never
    expires.
  - 3.2: the audit tests do not assert `actor_role` or `requested_by`;
    `tests/test_roles.py` covers those indirectly.
