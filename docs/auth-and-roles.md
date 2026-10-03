# Auth and roles
Code: `nethub/auth.py`, `nethub/web.py` (`admin_required`, `confirmed`), `nethub/settings.py`, `nethub/bootstrap.py`, `nethub/config.py` (`SECRET_KEY`, cookies), `nethub/models.py` (`User`, `load_user`, `UserAdminAudit`, `Setting`, `SettingsAudit`) · Tests: `tests/test_auth.py`, `tests/test_user_management.py`, `tests/test_roles.py`, `tests/test_bootstrap.py`, `tests/test_config.py`

---

## AI READING INSTRUCTION

Read `[SPEC]` and `[BUG]` blocks for authoritative facts.
Read `[NOTE]` only if additional context is needed.
`[?]` blocks are unverified; treat them with lower confidence.
§1–§4 are rules: each H3 heading is a rule's slug. §5 explains how it
works, §6 decisions, §7 known gaps.

This covers logging in to NetHub itself. The device credential is in
credentials.md. How each two-person rule applies to artifacts, host keys
and runs is in artifacts.md, host-keys.md and dispatch.md.

---

## 1. Login

### 1.1 [login-always-pays-for-a-hash]
**[SPEC]**
`login()` verifies a password on every attempt. An unknown username is
checked against `_ABSENT_USER_HASH`, made with the same
`generate_password_hash`, so it has the same KDF cost. Don't add an early
return for a missing user, and don't swap in a cheaper hash.
Pinned by: `tests/test_auth.py::test_an_absent_username_still_pays_for_a_password_hash`, `tests/test_auth.py::test_the_absent_user_hash_has_the_same_kdf_as_a_real_one`

**[NOTE]**
An early return answered in ~1.4 ms for an unknown user and ~104 ms for a
real one. That 74× gap let anyone enumerate usernames, and `GET /login`
hands out the CSRF token without a session.

### 1.2 [one-failure-message]
**[SPEC]**
A wrong password, a locked account and a disabled account all get the
same message, "Invalid username or password.", and all pay for the hash
check. Saying "locked" or "disabled" would confirm the username exists.
Pinned by: `tests/test_auth.py::test_login_with_wrong_password_fails`, `tests/test_user_management.py::TestDisable::test_a_disabled_user_cannot_log_in_and_gets_the_usual_message`, `tests/test_user_management.py::TestDisable::test_a_disabled_login_still_pays_for_the_hash`

### 1.3 [lockout-keyed-on-the-row]
**[SPEC]**
- `users.failed_logins` and `users.locked_until` count failures per
  existing user row: 10 failures lock the account for 15 minutes.
- Wrong passwords keep counting while the account is locked, so the lock
  can be renewed.
- Failed attempts against usernames that do not exist are not counted.
- A successful login, an admin reset or an unlock clears the count.
  Re-enabling a user does not.

Pinned by: `tests/test_auth.py::test_repeated_failures_lock_the_account`, `tests/test_auth.py::test_an_absent_username_does_not_create_a_counter_row`, `tests/test_auth.py::test_a_lock_expires`, `tests/test_auth.py::test_a_successful_login_clears_the_counter`

**[NOTE]**
A counter keyed on a username the attacker chooses would let them grow
the shared SQLite file without bound. Not counting unknown usernames is
safe only because rule 1.1 stops anyone learning which usernames exist.

### 1.4 [failed-logins-logged-without-password]
**[SPEC]**
Every failed or refused login is logged with the username and source
address; the password never is.
Pinned by: `tests/test_auth.py::test_a_failed_login_is_logged`, `tests/test_auth.py::test_the_submitted_password_is_never_logged`

---

## 2. Sessions

### 2.1 [epoch-in-the-cookie]
**[SPEC]**
`User.get_id()` returns `id:session_epoch`, and Flask-Login signs it into
the session cookie. `models.load_user` refuses a cookie that has no
epoch, has an epoch other than the row's current one, or belongs to an
inactive user.
Pinned by: `tests/test_user_management.py::TestSessions::test_the_session_id_carries_the_epoch`, `tests/test_user_management.py::TestSessions::test_a_cookie_with_a_stale_epoch_is_refused`, `tests/test_user_management.py::TestSessions::test_a_cookie_without_an_epoch_is_refused`, `tests/test_user_management.py::TestSessions::test_an_inactive_row_ends_the_session_even_with_the_right_epoch`

### 2.2 [what-revokes-sessions]
**[SPEC]**
- Disabling a user, an admin password reset, and changing your own
  password all bump the epoch, so the user's existing cookies stop
  working on their next request.
- After changing your own password, you are logged in again under the
  new epoch, so only your *other* sessions end.
- Re-enabling a user does not revive old sessions.

Pinned by: `tests/test_user_management.py::TestDisable::test_a_disabled_users_existing_session_ends_on_its_next_request`, `tests/test_user_management.py::TestAdminReset::test_a_reset_sets_the_password_and_ends_their_sessions`, `tests/test_user_management.py::TestOwnPassword::test_changing_it_ends_your_other_sessions_but_not_this_one`, `tests/test_user_management.py::TestDisable::test_enabling_does_not_revive_the_old_session`

### 2.3 [role-read-every-request]
**[SPEC]**
`load_user` reads the row on every request, so a role change or a
disable takes effect on that user's next click. Nothing caches the role.
A role change does not bump the epoch: the session continues under the
new role.
Pinned by: `tests/test_roles.py::TestRoles::test_a_role_change_is_audited_and_applies_on_the_next_request`

### 2.4 [cookie-flags]
**[SPEC]**
- The session cookie is `SameSite=Strict`, `HttpOnly`, and `Secure` unless
  `SESSION_COOKIE_INSECURE=1`.
- `PERMANENT_SESSION_LIFETIME` (12 h) applies only because `login()` and
  `change_password()` set `session.permanent = True`.
- Flask's default `SESSION_REFRESH_EACH_REQUEST` re-issues the cookie on
  every request, so the 12 h is an *idle* timeout, with no absolute cap.
Pinned by: `tests/test_auth.py::test_login_marks_the_session_permanent`

### 2.5 [secret-key-refused-if-weak]
**[SPEC]**
`config.py` refuses to load when `SECRET_KEY` is any of these:
- absent;
- a known placeholder;
- shorter than 32 characters.

A systemd credential named `secret_key` takes priority over the
environment variable.
Pinned by: `tests/test_config.py::test_absent_key_is_refused`, `tests/test_config.py::test_the_quadlet_placeholder_is_refused`, `tests/test_config.py::test_a_short_key_is_refused`

**[NOTE]**
There is no server-side session row, so the cookie signature is the only
thing proving who a user is. A published key lets anyone forge an admin
session.

**[NOTE]**
A disabled user is refused in three places: the login route, `load_user`,
and Flask-Login itself (`UserMixin.is_authenticated` is `is_active`).

### 2.6 [csrf-on-every-form]
**[SPEC]**
Flask-WTF's `CSRFProtect` is initialised in `create_app()`, so every POST
needs a token. The shared test `app` fixture turns CSRF off.
`tests/test_templates.py` builds its own app with it on.
Pinned by: `tests/test_templates.py::test_every_post_form_carries_a_csrf_token`, `tests/test_user_management.py::TestUsersPage::test_every_action_form_carries_a_csrf_token`

---

## 3. User management

### 3.1 [admin-required-is-the-check]
**[SPEC]**
`web.admin_required` guards every `/users*` route and `/settings`, and
answers an operator with a 403; templates only hide the links. Add any
new admin route to `ADMIN_ONLY` in `tests/test_roles.py`.
Pinned by: `tests/test_roles.py::TestRoles::test_an_operator_is_refused_every_admin_only_route`

### 3.2 [never-yourself]
**[SPEC]**
- Nobody can disable themselves or change their own role.
- The admin reset route refuses your own account. Your own password is
  changed on `/profile`, which asks for the current one.

Pinned by: `tests/test_user_management.py::TestDisable::test_you_cannot_disable_yourself`, `tests/test_roles.py::TestRoles::test_you_cannot_change_your_own_role`, `tests/test_user_management.py::TestAdminReset::test_your_own_password_is_changed_on_your_profile`

### 3.3 [last-active-admin]
**[SPEC]**
The last active admin can be neither disabled nor demoted. The check sits
inside the `UPDATE` as a subquery: `_another_active_admin`, which counts
*other* active admins on an alias.
Pinned by: `tests/test_user_management.py::TestDisable::test_the_last_active_admin_is_never_disabled`, `tests/test_roles.py::TestRoles::test_the_last_active_admin_cannot_be_demoted`, `tests/test_roles.py::TestRoles::test_the_only_admin_can_still_disable_an_operator`

**[NOTE]**
Two admins disabling each other at the same time would both pass a check
made beforehand. SQLite runs one writer at a time, so the second
statement finds no other admin left and changes nothing.

### 3.4 [own-password-change-counts-failures]
**[SPEC]**
A wrong current password on `/profile/password` counts as a failed login,
and a locked account cannot change its password. An unattended session
must not offer unlimited guesses at the password behind it.
Pinned by: `tests/test_user_management.py::TestOwnPassword::test_a_wrong_current_password_is_refused_and_counted`, `tests/test_user_management.py::TestOwnPassword::test_guessing_the_current_password_locks_the_account`

### 3.5 [user-audit-append-only]
**[SPEC]**
Each of these writes a `user_admin_audit` row in the same transaction as
the change it records (`models.record_user_action`):
- create, password change or reset;
- disable, enable or unlock;
- role change, with `detail` "operator to admin" or the reverse.

Triggers make the table append-only. `actor_user_id` is null for
`create-admin` and first-boot bootstrap, and `detail` is always NetHub's
own fixed text.
Pinned by: `tests/test_user_management.py::TestAudit::test_the_table_is_append_only`, `tests/test_user_management.py::TestAudit::test_the_command_line_is_recorded_as_no_user`, `tests/test_user_management.py::TestAudit::test_no_password_reaches_the_audit_table`

### 3.6 [role-change-needs-the-box]
**[SPEC]**
A role change needs the `confirm=yes` box (`web.confirmed`), as disabling
a user does. Promotion exempts the user from every two-person rule.
Pinned by: `tests/test_roles.py::TestRoles::test_an_unticked_role_change_changes_nothing`

### 3.7 [least-privilege-defaults]
**[SPEC]**
- The model default role and the Users form default are both `operator`.
- `create-admin` and first-boot bootstrap create admins.
- conftest's `make_user` creates an admin unless told otherwise; a test
  that builds `User(...)` by hand gets an operator.
- Minimum password length is 12 for the form, `create-admin` and resets.

Pinned by: `tests/test_roles.py::TestRoles::test_a_user_created_in_the_ui_is_an_operator_unless_picked`, `tests/test_auth.py::test_new_user_rejects_a_short_password`, `tests/test_auth.py::test_create_admin_cli_rejects_a_short_password`

### 3.8 [bootstrap-first-admin]
**[SPEC]**
- On an empty `users` table, `bootstrap_admin` creates an admin named
  `ADMIN_USERNAME`. There is no default name: if it is unset, NetHub
  starts with no users and prints how to run `create-admin`.
- The password comes from the first of: the `admin_password` systemd
  credential, `ADMIN_PASSWORD`, or a random 12 characters printed once.

Pinned by: `tests/test_bootstrap.py::test_there_is_no_default_admin_username`, `tests/test_bootstrap.py::test_credential_beats_env_and_generated`, `tests/test_bootstrap.py::test_env_beats_generated_when_no_credential`, `tests/test_bootstrap.py::test_bootstrap_admin_skips_when_users_exist`

**[NOTE]**
A well-known `admin` username combined with the lockout would let anyone
on the network keep the first account locked out.

---

## 4. Roles and the two-person rules

### 4.1 [two-roles]
**[SPEC]**
`users.role` is `admin` or `operator`, enforced by a CHECK.
- Operators upload, publish and delete artifacts; scan, confirm and
  remove host-key pins; and submit, approve, retry and cancel runs.
- Admins can do all of that, and also manage users and `/settings`.

Pinned by: `tests/test_roles.py::TestRoles::test_an_operator_is_refused_every_admin_only_route`

### 4.2 [rules-are-rows-read-every-time]
**[SPEC]**
- The rules `two_person_artifacts`, `two_person_hostkeys` and
  `two_person_runs` are `settings` rows set to `on`/`off`. A missing row
  means off.
- `settings.applies(key, user)` is true when the rule is on *and* the
  user is not an admin.
- The row is read on every call, so a rule is checked at the moment of
  the second action, and turning it off releases whatever was waiting.

Pinned by: `tests/test_roles.py::TestRunRule::test_turning_the_rule_off_releases_a_waiting_run`, `tests/test_roles.py::TestArtifactRule::test_turning_the_rule_off_releases_a_waiting_upload`, `tests/test_roles.py::TestHostKeyRule::test_turning_the_rule_off_releases_a_waiting_scan`

### 4.3 [settings-changes-audited]
**[SPEC]**
`settings.change` writes `settings_audit` (append-only, by trigger) in the
same transaction as the change. The form submits `shown_<key>` for each
rule, and if any value moved since the page loaded, nothing changes.
Pinned by: `tests/test_roles.py::TestSettings::test_each_change_writes_an_audit_row`, `tests/test_roles.py::TestSettings::test_the_audit_table_is_append_only`, `tests/test_roles.py::TestSettings::test_a_form_from_before_a_change_changes_nothing`

**[NOTE]**
An unticked checkbox is simply absent from the POST, so a tab loaded
before someone turned a rule on would otherwise turn it off again.

### 4.4 [second-person-delete-is-a-claim]
**[SPEC]**
`settings.second_person_delete` is the request-then-confirm step shared
by artifacts and pins.
- It records a request with `UPDATE … WHERE delete_requested_by IS NULL`.
- A user who loses that race is refused with `RequestRaced`; they are
  not turned into the confirmer of a request they never saw.
- The same user deleting again returns `WAITING`.

Pinned by: `tests/test_roles.py::TestArtifactRule::test_two_people_requesting_one_delete_make_one_request`

### 4.5 [admins-exempt-and-visible]
**[SPEC]**
Admins are never bound by a rule, and their role at the time is recorded:
`upgrade_phase_jobs.approved_by_role`, `artifact_audit.actor_role` and
`device_host_key_audit.actor_role`.
Pinned by: `tests/test_roles.py::TestRunRule::test_an_admin_approves_their_own_run_and_the_job_says_admin`, `tests/test_roles.py::TestArtifactRule::test_an_admin_acts_alone_and_the_record_says_so`, `tests/test_roles.py::TestHostKeyRule::test_an_admin_confirms_their_own_scan_and_removes_alone`

---

## 5. How it works

**[SPEC]**
| route | who | does |
|---|---|---|
| `/login` | anyone | GET form; POST log in |
| `/logout` | logged in | POST |
| `/profile` | logged in | own password and device username (`upgrade_routes.py`, blueprint `upgrades`) |
| `/profile/password` | logged in | POST, change own password (`auth.py`) |
| `/users`, `/users/new` | admin | list, create |
| `/users/<id>/reset-password` | admin | set another user's password |
| `/users/<id>/disable` and `/role` (both need the confirm box), `/enable`, `/unlock` (no box) | admin | POST |
| `/users/<id>/history` | admin | that user's audit rows |
| `/settings` | admin | the three two-person rules plus their history |

```
flask --app nethub create-admin <username>    # prompts for the password; records 'create-admin command'
```

---

## 6. Decisions

**[NOTE]**
- **Only local username/password logins exist; there is no
  self-registration.** OIDC, enrollment tokens and the `nethub-admin`
  break-glass CLI are future.md material. `create-admin` is what exists.
- **Revocation uses an epoch in the cookie, not a `sessions` table.** It
  gives "disable takes effect on the next request" without a server-side
  row. Per-session idle and absolute timeouts are therefore not built.
- **There is no forced password reset on first login.** The `User` model
  has no field for it and no flow to send someone to. Don't add a
  `must_reset_password` column without building the flow.
- **Two-person rules bind operators only, and default off.**
  - Why: a small team may have only one admin.
  - Cancelling a run and declining cleanup never need a second person,
    because stopping is the conservative action.

---

## 7. Known gaps

**[SPEC]**
- No absolute session lifetime: a session used at least every 12 h never
  expires. There is no list of a user's sessions.
- Every session ends at once only by rotating `SECRET_KEY` or by bumping
  every `session_epoch` in the database.
- `bootstrap_admin` does not apply the 12-character minimum to
  `ADMIN_PASSWORD` or to the credential.
- The lockout budget is per account. A well-known username can be kept
  locked by anyone on the network; `/users/<id>/unlock` is the remedy.
- No rate limit applies to `/login` beyond the per-account lockout.
- Weak pins:
  - 1.2: `test_login_with_wrong_password_fails` covers only the plain
    wrong-password message.
  - 1.4: only one of the four log branches is tested.
  - 2.4: `change_password()` setting `permanent` is untested.
  - 2.6: the users-page test only counts tokens.
    `tests/test_templates.py::test_every_post_form_carries_a_csrf_token`
    covers every page, and nothing tests that a POST without a token is
    rejected.
  - 3.5: per-action rows and the same-transaction property are untested.
  - 3.8: nothing tests that bootstrap creates an admin from
    `ADMIN_USERNAME`.
