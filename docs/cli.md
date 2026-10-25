# CLI
Code: `nethub/cli.py` (`artifacts`, `hostkeys`), `nethub/auth.py` (`authenticate`), `nethub/upgrades.py` (`queue_scan`, `confirm_scan`) · Tests: `tests/test_cli.py`

---

## AI READING INSTRUCTION

Read `[SPEC]` and `[BUG]` blocks for authoritative facts.
Read `[NOTE]` only if additional context is needed.
`[?]` blocks are unverified; treat them with lower confidence.
§1 holds the rules, each H3 heading being a rule's slug. §2 lists the
commands, §3 decisions, §4 known gaps.

This is the CLI for when NetHub is **up**. `nhtui`, for when there is no
NetHub server, is in tui.md.

---

## 1. Rules

### 1.1 [cli-is-a-flask-front-end]
**[SPEC]**
`flask --app nethub artifacts|hostkeys ...` calls the same service
functions as the routes (`artifacts.ingest/publish/withdraw/delete`,
`upgrades.queue_scan/confirm_scan/delete_pin`), so it writes what a route
writes and does no device I/O. It imports nothing from `nethub.devices`;
a scan is a `queued` row for the sibling.
Pinned by: `tests/test_cli.py::TestFrontEnd::test_the_cli_imports_nothing_from_the_device_layer`, `tests/test_cli.py::TestFrontEnd::test_a_scan_is_queued_and_opens_no_session`, `tests/test_cli.py::TestFrontEnd::test_a_target_outside_the_rules_is_refused`, `tests/test_cli.py::TestArtifacts::test_under_the_rule_the_uploader_cannot_publish_but_someone_else_can`, `tests/test_cli.py::TestHostkeys::test_the_two_person_rule_applies`

**[NOTE]**
Logic shared with a route lives in the service module, never in either
front end, so a check added to one cannot be missed by the other.

### 1.2 [cli-acts-as-an-authenticated-user]
**[SPEC]**
Every writing command takes `--as <username>` and prompts for that user's
NetHub password, checked by `auth.authenticate` exactly as `/login`
checks it, with one failure message for every case. Read commands
(`list`, `history`, `show-scan`) take no login.
Pinned by: `tests/test_cli.py::TestActsAsAnAuthenticatedUser::test_a_bad_login_gets_the_login_message_and_writes_nothing`, `tests/test_cli.py::TestActsAsAnAuthenticatedUser::test_a_disabled_account_gets_the_same_message`, `tests/test_cli.py::TestActsAsAnAuthenticatedUser::test_read_commands_need_no_login`

**[NOTE]**
This is attribution, not a security boundary. A shell that can write
the SQLite file can forge rows anyway. The password keeps
`uploaded_by`, `confirmed_by` and the two-person rules meaning what they
mean in the web app.

### 1.3 [cli-confirm-restates-fingerprint]
**[SPEC]**
`hostkeys confirm <scan_id>` requires `--fingerprint`, which must match
the scan row's digest (and key type, if given). A mismatch pins nothing
and leaves the scan unconsumed. The pin itself still comes from the row
(host-keys.md [confirm-reads-the-scan-row]).
Pinned by: `tests/test_cli.py::TestHostkeys::test_confirm_with_the_restated_fingerprint_pins`, `tests/test_cli.py::TestHostkeys::test_a_fingerprint_that_differs_from_the_scan_is_refused`

**[NOTE]**
The web page shows the fingerprint beside the button. A terminal user
could confirm by scan id without ever looking at the key, so restating
it is what makes the confirmation a comparison against the device.

### 1.4 [cli-destructive-commands-ask]
**[SPEC]**
`artifacts delete` and `hostkeys delete` prompt with the consequence
text of the matching web checkbox, and declining changes nothing.
`--yes` skips the prompt.
Pinned by: `tests/test_cli.py::TestArtifacts::test_delete_asks_first_and_declining_changes_nothing`

---

## 2. Commands

**[SPEC]**
| command | does |
|---|---|
| `artifacts list [--state published\|staged]` | list artifacts |
| `artifacts add <path> --bundle K --version V --sha512 H --as U` | upload, as `/artifacts/new` |
| `artifacts publish <id> --as U` | publish a staged upload |
| `artifacts delete <id> --as U [--yes]` | withdraw a staged upload, or delete (or ask to delete) a published one, as the web buttons do |
| `hostkeys list` | list pins |
| `hostkeys scan <ip> --as U [--wait SECONDS]` | queue a scan, and optionally poll for its result |
| `hostkeys show-scan <id>` | a scan's result |
| `hostkeys confirm <scan_id> --fingerprint "<type> SHA256:..." --as U` | pin a scan's key |
| `hostkeys delete <ip> --as U [--yes]` | remove a pin, or ask for its removal |
| `hostkeys history <ip>` | the pin's audit trail |

Run them where the web process runs (in production, `podman exec` into
the web container). They need the web environment, and `create_app`
migrates at startup as it does for `create-admin`.

---

## 3. Decisions

**[NOTE]**
- **A Flask command group, not a growth of `nhtui`.** A writing `nhtui`
  would be the third writer that device-layer.md §7 rejects. Reopen it if the CLI ever needs to work with the web process
  down.
- **No `/settings` commands.** A shell user must not be able to turn off
  a two-person rule and then act alone.
- **No `--json`.** A JSON API is future.md material. Add it when a script
  needs one.

---

## 4. Known gaps

**[SPEC]**
- No `runs` or `users` groups yet. Submit, approve, retry, cancel and user
  administration are browser-only.
- `hostkeys delete` has no test of its own. The `delete_pin` it calls is
  pinned in host-keys.md §3.
