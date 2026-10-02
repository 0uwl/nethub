# Device layer
Code: `nethub/devices/{connection,transfer,install,facts}.py`, `nethub/upgrade_cli.py`, `scripts/check_device_facts.py` · Tests: `tests/test_connection.py`, `tests/test_transfer.py`, `tests/test_install.py`, `tests/test_facts.py`, `tests/test_upgrade_cli.py`, `tests/captures/`

---

## AI READING INSTRUCTION

Read `[SPEC]` and `[BUG]` blocks for authoritative facts.
Read `[NOTE]` only if additional context is needed.
`[?]` blocks are unverified; treat them with lower confidence.
§1–§5 are rules: each H3 heading is a rule's slug. §6 says how it works
(including hardware timings), §7 decisions, §8 pitfalls, §9 known gaps.

`nethub/devices/phases.py`, the per-host driver, is covered in dispatch.md.
The modules here hold no database and no policy, apart from
`upgrade_cli`'s optional pin lookup.

---

## 1. Connecting (`connection.py`)

### 1.1 [connect-requires-a-pin]
**[SPEC]**
`connection.connect()` is the only way any NetHub code opens a device
session. It takes the pinned `HostKey` as a required positional argument,
and there is no trust-on-first-use path anywhere.
Pinned by: `tests/test_connection.py::test_connect_cannot_be_called_without_a_pinned_key`, `tests/test_connection.py::test_policy_refuses_a_changed_fingerprint`, `tests/test_connection.py::test_policy_refuses_a_different_key_type`

### 1.2 [no-host-keys-loaded]
**[SPEC]**
`IosXeSSH._build_ssh_client` loads no host keys and attaches
`_PinnedHostKeyPolicy`, so every connection reaches the pin comparison.
Calling `load_system_host_keys()` would let a line in this host's
`~/.ssh/known_hosts` silently skip the check.
Pinned by: `tests/test_connection.py::test_client_is_built_with_our_policy_and_no_loaded_keys`

### 1.3 [hostkey-error-not-sshexception]
**[SPEC]**
`DeviceConnectionError`, and its subclasses `HostKeyError` and
`AuthenticationError`, must not subclass `paramiko.SSHException`.
Netmiko re-raises that type as a timeout, which would turn a key mismatch
into "increase conn_timeout".
Pinned by: `tests/test_connection.py::test_host_key_errors_are_not_paramiko_exceptions`

### 1.4 [password-auth-only]
**[SPEC]**
The device credential is a password; NetHub uses no SSH client key.
`connect()` accepts no `**kwargs`, so nothing can turn on `use_keys` or
`allow_agent`, and nothing in `~/.ssh` or an agent is offered.
Pinned by: `tests/test_connection.py::test_connect_exposes_no_way_to_turn_key_auth_on`, `tests/test_connection.py::test_no_ssh_key_material_is_ever_offered`

### 1.5 [keyboard-interactive-stated-not-detected]
**[SPEC]**
`DEFAULT_AUTH` is `keyboard-interactive`. `_SSHClientKeyboardInteractive`
answers every prompt with the password. The method is the caller's
choice; never add auto-detection.
Pinned by: `tests/test_connection.py::test_keyboard_interactive_is_the_default_client`, `tests/test_connection.py::test_keyboard_interactive_answers_every_prompt_with_the_password`

**[NOTE]**
IOS-XE with `aaa new-model` offers `publickey,keyboard-interactive`, not
`password`. The device drops the session after one failed attempt (even
a bare `auth_none` probe), so trying one method and falling back cannot
work. If password auth is refused, the error is a misleading
`transport shut down or saw EOF`.

### 1.6 [scan-writes-nothing]
**[SPEC]**
`scan_host_key()` does key exchange only, so it spends no credential,
and returns the key without storing it. It runs in the sibling;
host-keys.md covers what happens to the result.
Pinned by: `tests/test_connection.py::test_fingerprint_matches_what_ssh_keygen_prints`, `tests/test_connection.py::test_scan_host_key_reports_an_unreachable_address`

### 1.7 [three-connection-exceptions]
**[SPEC]**
A failed connection raises one of three exceptions, each mapping to a
`failure_stage`:
- `HostKeyError` → `hostkey`;
- `AuthenticationError` → `credential`;
- any other `DeviceConnectionError` → `connect`.

`connect()` re-raises `HostKeyError` unchanged, turns Netmiko's
authentication exception into `AuthenticationError`, and wraps anything
else as `DeviceConnectionError`. The key-type mismatch and the
fingerprint mismatch get different messages.
Pinned by: `tests/test_phases.py::TestFailureStageMapping::test_every_device_exception_has_a_stage`

---

## 2. Staging the image (`transfer.py`)

### 2.1 [scp-bracket-confirmed]
**[SPEC]**
- `_push_scp` reads the device's `ip scp server enable` state first, and
  enables the server only if it was off.
- In `finally:` it always re-sends the prior state, even if that was
  already enabled, and confirms it by re-reading the running-config.
- An unconfirmed restore raises `ScpRestoreError`, which fails the host,
  and never only logs a warning.

Pinned by: `tests/test_transfer.py::TestPushBracket::test_enables_transfers_and_restores_to_disabled`, `tests/test_transfer.py::TestPushBracket::test_an_already_enabled_server_is_left_enabled_and_not_re_enabled`, `tests/test_transfer.py::TestPushBracket::test_unconfirmed_restore_fails_the_host`

**[NOTE]**
Activate's `write memory` would otherwise save an un-restored
`ip scp server enable` into startup-config. This rule and rule 3.5 are
one mechanism seen from two phases.

### 2.2 [restore-error-wins]
**[SPEC]**
`ScpRestoreError` is raised from `finally:`, so it replaces an in-flight
push failure and keeps it as `__context__`. `_restore_scp_server` never
raises: a restore that could not be attempted counts as unconfirmed.
Pinned by: `tests/test_transfer.py::TestPushBracket::test_unconfirmed_restore_wins_over_a_push_failure`

### 2.3 [scp-state-whole-line]
**[SPEC]**
`_scp_server_enabled` matches whole lines. `no ip scp server enable`
contains the enabled form as a substring, and a substring test would
restore it backwards.
Pinned by: `tests/test_transfer.py::TestScpServerReading::test_negated_line_reads_as_disabled`

### 2.4 [second-session-pinned]
**[SPEC]**
The SCP put opens a second SSH session via `CiscoIosFileTransfer`, which
builds it through the same `_build_ssh_client`, so the pin and
keyboard-interactive auth cover it too. `_push_scp` re-raises
`DeviceConnectionError` before its generic handler, so a host-key failure
there is `hostkey`, not `transfer`.
Pinned by: `tests/test_transfer.py::TestPushBracket::test_a_hostkey_mismatch_on_the_second_session_is_not_filed_as_a_transfer_error`

### 2.5 [verify-asks-for-the-digest]
**[SPEC]**
`verify_sha512` runs `verify /sha512 <fs><image>`, parses the device's
own 128-hex digest, and compares in Python. Never hand the device the
expected digest: it echoes it back, so a substring test would pass on the
echo.
Pinned by: `tests/test_transfer.py::TestVerify::test_the_expected_digest_is_never_sent_to_the_device`, `tests/test_transfer.py::TestVerify::test_the_word_verified_alone_does_not_pass`, `tests/test_transfer.py::TestVerify::test_parses_the_real_device_format`

### 2.6 [skip-if-staged-by-digest]
**[SPEC]**
`stage_image` skips the push only if the file on the device has the
right size *and* `verify_sha512` matches. A file with the right name is
not proof. Otherwise `_check_space` refuses (`no_space`) unless free space
plus any existing file of that name exceeds the image size.
Pinned by: `tests/test_transfer.py::TestSkipIfStaged::test_matching_size_and_digest_skips_the_transfer_entirely`, `tests/test_transfer.py::TestSkipIfStaged::test_right_name_wrong_bytes_is_not_staged`

### 2.7 [no-md5-pass]
**[SPEC]**
`_scp_put` passes `hash_supported=False`, and `netmiko.file_transfer()` is
not used. With the flag on, Netmiko MD5s the whole source file in its
constructor, even under `disable_md5=True`.
Pinned by: none

### 2.8 [source-measured-before-push]
**[SPEC]**
`resolve_source` stats the image under `search_dir`. If the size differs
from the snapshotted `file_size`, it refuses before any transfer.
Pinned by: `tests/test_transfer.py::TestSource::test_declared_size_mismatch_stops_before_any_transfer`

### 2.9 [names-checked-as-text]
**[SPEC]**
- Anything interpolated into a CLI command is matched against a pattern
  first: image names (`[\w.+-]+`), file systems (`[\w:./-]+`, in both
  `transfer` and `facts.dir_command`), and digests (128 hex after
  `_normalise_digest`).
- No module layer sits between NetHub and the device CLI to escape these
  values.

Pinned by: `tests/test_transfer.py::TestGuards::test_image_names_that_reach_the_cli_are_checked`, `tests/test_facts.py::test_dir_command_rejects_injection`, `tests/test_transfer.py::TestGuards::test_a_non_sha512_digest_is_refused`

---

## 3. Installing (`install.py`)

### 3.1 [activate-guards-read-only]
**[SPEC]**
`assert_ready_to_activate` issues only reads and refuses, in this order:
- `already_current`;
- `wrong_boot_mode` (anything but INSTALL, including unknown);
- `privilege` below 15;
- `image_missing`;
- a failed `verify /sha512`.

A refusal is therefore safe to re-run.
Pinned by: `tests/test_install.py::TestActivate::test_guards_run_before_anything_is_written`, `tests/test_install.py::TestGuards::test_present_image_with_the_wrong_bytes_is_refused`, `tests/test_install.py::TestGuards::test_unknown_boot_mode_is_refused_rather_than_attempted`

### 3.2 [pre-activate-is-a-verify]
**[SPEC]**
The pre-activate image check is `verify /sha512` against the snapshotted
digest, never a `dir` presence check. The digest is normalised through
`transfer._normalise_digest`, as in `stage_image`.
Pinned by: `tests/test_install.py::TestGuards::test_present_image_with_the_wrong_bytes_is_refused`, `tests/test_install.py::TestGuards::test_an_uppercase_or_padded_digest_is_normalised_before_comparing`

**[NOTE]**
A host whose staged image failed verification still has it in flash under
the target filename. A `dir` check would install it anyway.

### 3.3 [install-needs-success]
**[SPEC]**
- *Any* exception from `install add … activate commit`, including a lost
  session or the 1800 s read timeout, is treated as the reload starting.
  The verify phase decides whether the upgrade worked.
- A command that returns *cleanly* without `SUCCESS`, or with
  `FAILED`/`% Error`/`ERROR`, is an `install_failed` error.

Pinned by: `tests/test_install.py::TestActivate::test_a_clean_return_without_success_is_a_failure`, `tests/test_install.py::TestActivate::test_silence_is_also_a_failure_not_a_reboot`, `tests/test_install.py::TestActivate::test_a_lost_session_is_the_expected_ending`

**[NOTE]**
IOS-XE reports some install failures in-band with the session still up.
Reading those as "rebooting" would waste the whole reload deadline before
reporting a failure that was visible immediately.

### 3.4 [reconnect-not-blind]
**[SPEC]**
`wait_for_device` takes a connection *factory* and waits `delay` before
the first try. Each attempt must run `show version` before its
connection counts as up. A connection that answers but cannot serve is
closed and retried.
Pinned by: `tests/test_install.py::TestWaitForDevice::test_a_device_answering_ssh_but_not_serving_cli_is_not_ready`, `tests/test_install.py::TestWaitForDevice::test_a_half_up_connection_is_closed_rather_than_leaked`, `tests/test_install.py::TestWaitForDevice::test_waits_the_delay_before_the_first_attempt`

### 3.5 [auth-error-not-retried]
**[SPEC]**
`wait_for_device` re-raises `AuthenticationError` immediately instead of
retrying. `HostKeyError` is deliberately still treated as "not back yet"
(see §9).
Pinned by: `tests/test_install.py::TestWaitForDevice::test_an_authentication_error_is_not_treated_as_transient`, `tests/test_install.py::TestWaitForDevice::test_a_hostkey_error_is_still_treated_as_transient_for_now`

**[NOTE]**
With the default wait, retrying would present the same password about 28
times in 15 minutes. A device that came back with AAA unreachable would
then lock the account out across the whole fleet.

### 3.6 [write-memory-first]
**[SPEC]**
`activate` runs `write memory` before `install add`. The install reboots
the device, and an unsaved running-config would be lost.
Pinned by: `tests/test_install.py::TestActivate::test_saves_configuration_before_installing`

### 3.7 [cleanup-rejection-before-success]
**[SPEC]**
`cleanup` checks for `User Rejected Deletion` *before* checking for
`SUCCESS: install_remove`, because a declined prompt still prints the
success marker. Keep that order.
Pinned by: `tests/test_install.py::TestCleanup::test_a_declined_deletion_is_not_a_success`

### 3.8 [versions-compared-normalised]
**[SPEC]**
Versions are compared with `facts.same_version`, which compares numeric
components (`17.12.8` equals `17.12.08`), never as strings. A string
compare would fail a successful upgrade with `wrong_version`.
Pinned by: `tests/test_install.py::TestVerifyUpgrade::test_accepts_the_registry_spelling_of_the_same_release`, `tests/test_facts.py::test_registry_and_device_version_formats_agree`

---

## 4. Parsing facts (`facts.py`)

### 4.1 [boot-mode-derived]
**[SPEC]**
`boot_mode` is derived from the running image: `.conf` means INSTALL,
`.bin` means BUNDLE, and anything else is `""`. `""` means refuse, never
BUNDLE.
Pinned by: `tests/test_facts.py::test_bundle_mode_is_distinguished_from_install`, `tests/test_facts.py::test_unknown_boot_mode_is_empty_not_bundle`

### 4.2 [dir-excludes-directories]
**[SPEC]**
`parse_dir` drops entries whose permissions start with `d`. `size_of`
answers "is the staged image here at the right length", and a directory
must never answer it.
Pinned by: `tests/test_facts.py::TestRealCapture::test_dir_omits_directories`

### 4.3 [no-default-on-parse-miss]
**[SPEC]**
A parse failure raises `FactsError` (`failure_stage='precheck'`), never a
zero or a guess. A wrong free-space number would fill a device's flash.
Pinned by: `tests/test_facts.py::test_unparseable_output_raises_rather_than_defaulting`, `tests/test_facts.py::test_unparseable_version_raises`

### 4.4 [facts-parses-phases-police]
**[SPEC]**
`facts.py` returns numbers and refuses nothing.
- The "privilege must be 15" refusal is in `phases.phase_precheck` and
  `install.assert_ready_to_activate`.
- The "enough free space" refusal is in `phases.phase_precheck` and
  `transfer._check_space`.

Pinned by: none

### 4.5 [captures-are-evidence]
**[SPEC]**
`tests/captures/<model>-<release>/` holds verbatim device output, plus that
device's public host key and its `ssh-keygen -lf` fingerprint. Never edit
a capture to make a test pass. Add a directory per release; put synthetic
samples in the test file and label them there.
Pinned by: `tests/test_facts.py::TestRealCapture::test_parse_version`, `tests/test_connection.py::test_fingerprint_matches_what_ssh_keygen_prints`

---

## 5. The manual escape hatch (`upgrade_cli.py`)

### 5.1 [cli-needs-nothing-and-writes-nothing]
**[SPEC]**
`python -m nethub.upgrade_cli` runs device phases with no Flask app, no
sibling, no sealed credential and no job rows. It writes nothing to the
database, and every phase run (not `--scan`) says so. Keep `nethub/devices/` free of
database dependencies, or this tool breaks.
Pinned by: `tests/test_upgrade_cli.py::TestItWritesNothing::test_the_cli_imports_no_job_or_run_models`, `tests/test_upgrade_cli.py::TestItWritesNothing::test_it_warns_on_every_invocation`

### 5.2 [cli-never-tofu]
**[SPEC]**
- `resolve_pin` uses `--fingerprint` if given, else a confirmed
  `device_host_keys` row if the database is reachable, else it refuses.
- An unreachable database is not an error by itself.
- `scripts/check_device_facts.py` imports the same `resolve_pin` and
  accepts only an IP literal.

Pinned by: `tests/test_upgrade_cli.py::TestPinResolution::test_no_pin_and_no_fingerprint_refuses_rather_than_trusting`, `tests/test_upgrade_cli.py::TestPinResolution::test_an_unreachable_database_is_not_fatal_by_itself`, `tests/test_upgrade_cli.py::TestPinResolution::test_a_confirmed_row_is_preferred_when_the_database_answers`

### 5.3 [cli-confirms-mutating-phases]
**[SPEC]**
`stage`, `activate` and `cleanup` (`MUTATING`) each ask for confirmation;
`--yes` skips the prompts. `--phases` defaults to `precheck`, so a bare
invocation changes nothing.
Pinned by: `tests/test_upgrade_cli.py::TestConfirmations::test_every_device_changing_phase_is_confirmed`, `tests/test_upgrade_cli.py::TestConfirmations::test_declining_stops_before_the_phase_runs`, `tests/test_upgrade_cli.py::TestConfirmations::test_yes_skips_the_prompt`

---

## 6. How it works

### 6.1 Commands sent to a device
**[SPEC]**
| step | command(s) | timeout |
|---|---|---|
| facts | `show version`, `dir <fs>`, `show privilege` | `dir`: `DIR_READ_TIMEOUT` 120 s |
| stage | `dir <fs>` (+ `verify` if already present); `dir <fs>` (space); `show running-config \| include ^ip scp server`; config `[no] ip scp server enable`; SCP put; `verify /sha512 <fs><image>` | SCP socket 60 s; verify 900 s |
| activate | `show running-config` (captured by `phase_activate`, before the guards), guards (§3.1), `write memory`, `install add file <fs><image> activate commit prompt-level none` | show 300 s; write 300 s; install 1800 s |
| reconnect | `show version` per attempt | `ReloadWait(delay=60, interval=30, timeout=900)` |
| cleanup | `install remove inactive`, answering `y` | 600 s |

Connect, banner and auth timeouts are 30 s each.

### 6.2 Measured on hardware
**[SPEC]**
Measured on a Catalyst 9200CX running IOS-XE 17.12.06 (INSTALL mode),
with a full round trip 17.12.6 → 17.12.08 → 17.12.6:

| step | duration |
|---|---|
| SCP push | ~1.4 MB/s (408,739,840 B in 319.9 s); 471 MB in ~370 s |
| `verify /sha512`, 408 MB | 33.9 s (`verify /md5`: 18.5 s) |
| `install add … activate commit` | 605–622 s, returns `SUCCESS` with the session up, *then* reboots |
| reload until the CLI serves again | 228–238 s (default deadline 900 s) |
| `install remove inactive` | ~5 s |

Plan with ~15 min per device to stage a 1.2 GB image. Push speed is
limited by the device, not the link: 20 devices at once is about
28 MB/s. Untested: stacks, slower chassis, and a constrained WAN link.

### 6.3 Re-parsing without a device
**[SPEC]**
```
python scripts/check_device_facts.py <ip> --user <name> [--fingerprint "<type> SHA256:..."]
python scripts/check_device_facts.py --replay <dir>
```
The first captures the three outputs to a directory and reports what
parsed. The second re-parses a capture offline. To validate a new IOS-XE
release, capture it, add the capture to `tests/captures/`, and replay it.

### 6.4 The escape hatch
**[SPEC]**
```
python -m nethub.upgrade_cli --scan <host>
python -m nethub.upgrade_cli --host <host> --user <name> --fingerprint "<type> SHA256:..." \
    --image <path> --sha512 <hex> --version <v> --phases all
```

---

## 7. Decisions

**[NOTE]**
- **Push over SCP is the only transport.**
  - Why: IOS-XE has no SFTP server, and many networks block outbound
    connections from devices, which a pull needs.
  - Reopen if: a deployment's devices can reach NetHub. See future.md
    (pull transport).
- **Netmiko, so Paramiko underneath.**
  - Why: under Ansible, `libssh` broke repeatedly at image size;
    Paramiko is the only library that has worked at 471 MB.
  - Paramiko is deprecated upstream. If it is replaced, re-test at image
    size.
- **No OpenSSH `scp` subprocess.** It needs the password on an interface
  with no safe way to pass it: command line, environment and askpass
  all leak.
- **SHA-512 everywhere, compared in NetHub's code.** Netmiko's
  `compare_md5` checks the file on NetHub's disk against the device, but
  the check that matters is the digest recorded at ingest against the
  device. See artifacts.md for why MD5 was rejected.
- **Privilege 15 at login, no enable.** See credentials.md
  [privilege-15-at-login].
- **NetHub changes no device configuration apart from the upgrade
  itself.** The SCP-server toggle is the one exception, and it is
  bracketed (rule 2.1). Pre-check asserts nothing about
  `ip ssh source-interface`, which has no bearing on the device's SCP
  server.
- **Parsing is separate from connecting**, so real output can be
  replayed offline (§6.3).
- **`upgrade_cli` writes no audit trail.** A third writer would put
  rows in the database that no approval accounts for, which is worse
  than an honest gap.

---

## 8. Pitfalls

**[BUG] Password auth rejected with "transport shut down or saw EOF"**
- Symptom: login fails with an EOF error that says nothing about
  authentication.
- Cause: the device offers `keyboard-interactive`, not `password`, under
  `aaa new-model`.
- Fix: use the default `auth="keyboard-interactive"`. Do not probe other
  methods first: the device drops the session after one failed attempt.

**[BUG] A host-key mismatch reported as a timeout**
- Symptom: "increase conn_timeout" when the pin actually fired.
- Cause: an exception class derived from `paramiko.SSHException`.
- Fix: rule 1.3.

**[BUG] A declined cleanup reported as success**
- Symptom: the cleanup succeeds, but no files were removed.
- Cause: checking `SUCCESS: install_remove` first.
- Fix: rule 3.7.

---

## 9. Known gaps

**[SPEC]**
- `stage_image` accepts `transfer_read_timeout` (`TRANSFER_READ_TIMEOUT`,
  7200 s) and passes it to `_push_scp`, but `_scp_put` never uses it. A
  transfer is bounded only by `SCP_SOCKET_TIMEOUT` (60 s of silence) and
  the job deadline.
- Some exceptions put device output into `message` without setting
  `summary`, so up to 500 chars of that output reach `error_summary`:
  - `VerificationError` when no digest is found;
  - `InstallError` `install_failed`;
  - cleanup `not_clean`;
  - `FactsError` for a missing version, `dir` totals or privilege line,
    or no rows.

  Others interpolate short device-reported values, such as the version
  in `already_current`, `wrong_boot_mode` and `PostCheckError`.
- A process kill, an abandoned run or a crashed sibling never reaches
  `_push_scp`'s `finally:`. Such a device can be left with its SCP server
  enabled. A killed process writes no result row at all, so nothing
  records it. `scp_restore_confirmed` is false only for
  `scp_not_restored`, and null means no bracket ran.
- It is unknown whether an IOS-XE upgrade can legitimately regenerate a
  device's host key. That decides whether `wait_for_device` should also
  re-raise `HostKeyError` (rule 3.5).
- The second SSH session per push is an extra AAA login. A per-user
  session limit, or different command authorisation for exec and
  transfer sessions, has not been tested.
- `upgrade_cli`'s `--fingerprint` with no key type assumes `ssh-rsa`.
- Weak or missing pins:
  - 2.7 and 4.4 have no tests.
  - 1.6: nothing tests that a scan stores nothing.
  - 2.9: the file-system pattern in `transfer` is untested.
  - 3.1: only single refusals are tested, not their order.
  - 5.1: the "imports no models" test is a source-text grep.
