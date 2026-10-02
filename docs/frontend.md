# Frontend
Code: `nethub/templates/`, `nethub/static/css/`, `nethub/web.py` (headers, `confirmed`), `nethub/worker_status.py`, `nethub/upgrade_routes.py` (`show_run`) · Tests: `tests/test_templates.py`, `tests/test_frontend.py`, `tests/test_errors.py`

---

## AI READING INSTRUCTION

Read `[SPEC]` and `[BUG]` blocks for authoritative facts.
Read `[NOTE]` only if additional context is needed.
`[?]` blocks are unverified; treat them with lower confidence.
§1–§3 are rules: each H3 heading is a rule's slug. §4 explains how it
works, §5 decisions, §6 known gaps.

---

## 1. No JavaScript, and the headers that enforce it

### 1.1 [no-javascript]
**[SPEC]**
No page contains a `<script`, a `<style` block, a `style=` attribute or an
`on*=` event handler. Put styling in `static/css/nethub.css`.
Pinned by: `tests/test_templates.py::test_no_page_has_a_script_an_inline_style_or_an_event_handler`

### 1.2 [csp-on-every-response]
**[SPEC]**
`web.set_security_headers` (an `after_request` hook) *sets*, rather than
defaults, these headers on every response:
- `Content-Security-Policy: default-src 'self'; img-src 'self' data:;
  script-src 'none'; frame-ancestors 'none'; form-action 'self';
  base-uri 'none'`;
- `X-Content-Type-Options: nosniff`;
- `Referrer-Policy: same-origin`.

Pinned by: `tests/test_frontend.py::test_every_response_carries_the_security_headers`, `tests/test_frontend.py::test_the_policy_refuses_every_script_and_every_frame`

**[NOTE]**
`img-src 'self' data:` is the only widening, and it is needed: Pico draws
checkbox ticks, select chevrons and `<details>` markers as `data:` SVGs.
`style-src` falls back to `'self'`, which is why inline styles are dead
markup. The login and approve forms are where people type AAA passwords,
so no script may run there.

### 1.3 [pico-vendored-and-pinned]
**[SPEC]**
Pico CSS v2.1.1 is vendored as `static/css/pico.min.css`. `layouts/main.html`
records its SHA-256, and a test hashes the file against that record. To
upgrade Pico, replace the file and the recorded hash together. There is
no CDN.
Pinned by: `tests/test_templates.py::test_the_vendored_pico_matches_its_pinned_digest`, `tests/test_templates.py::test_every_stylesheet_is_served_by_nethub`, `tests/test_templates.py::test_nothing_is_left_under_static_but_the_stylesheets_and_icon`

---

## 2. Forms

### 2.1 [destructive-forms-need-the-box]
**[SPEC]**
These forms carry a required checkbox `confirm=yes`, and the route checks
it with `web.confirmed` before it uses a password or touches a row:
- approve, retry and cancel a run;
- delete an artifact;
- remove a pin;
- disable a user;
- change a user's role.

Browser `required` is a convenience, not the check.
Pinned by: `tests/test_templates.py::test_every_destructive_form_carries_a_confirmation_box`, `tests/test_frontend.py::test_an_unticked_approval_queues_nothing`, `tests/test_frontend.py::test_a_run_action_without_its_box_never_reaches_the_service`, `tests/test_frontend.py::test_an_artifact_delete_without_its_box_deletes_nothing`, `tests/test_frontend.py::test_a_pin_removal_without_its_box_removes_nothing`, `tests/test_frontend.py::test_a_disable_without_its_box_leaves_the_user_active`

**[NOTE]**
A JavaScript `confirm()` prompt is exactly what the CSP blocks.

### 2.2 [approve-label-counts-eligible-hosts]
**[SPEC]**
The approve checkbox label counts the hosts the phase will *run on*
(`STATE_BEFORE[awaiting_phase]`), not every host in the run. The retry
label likewise counts only the failed hosts it would run on.
Pinned by: `tests/test_templates.py::test_the_host_count_is_the_hosts_the_phase_runs_on`, `tests/test_templates.py::test_the_confirmation_names_the_host_count`

### 2.3 [reload-count-form-and-warnings]
**[SPEC]**
- Only the activate approval and retry forms ask for a reload count
  (`partials/reload_count.html`).
- The form names the canary (when there is more than one host) and
  carries four warnings. Keep them; they are part of the canary decision
  (dispatch.md §6).

Pinned by: `tests/test_templates.py::test_the_activate_form_asks_for_a_reload_count_and_names_the_canary`, `tests/test_templates.py::test_only_the_activate_form_asks_for_a_reload_count`, `tests/test_templates.py::test_a_retry_of_activate_warns_about_the_reload`

### 2.4 [row-actions-short-confirm]
**[SPEC]**
- The five table row actions are "Change role", "Disable user",
  "Withdraw upload", "Delete artifact" and "Remove pin". Each sits inside
  a `<details>` and is built by the `short_confirm` macro
  (`partials/confirm.html`).
- The artifact delete route branches on state: withdraw, delete, or a
  delete request. Under the two-person rule the label reads "Request
  delete" or "Confirm delete".
- The consequence is shown as a Pico `data-tooltip`, and also as a
  `<small class="explainer">` that the checkbox names in
  `aria-describedby`. On `@media (hover: none)` the explainer replaces
  the tooltip.

Pinned by: `tests/test_templates.py::test_the_row_action_boxes_are_short_and_explain_themselves_on_hover`

### 2.5 [no-device-username-no-password-field]
**[SPEC]**
When `current_user.device_username` is unset, the new-run page and the
approve and retry forms show a notice linking to `/profile` instead of a
password field. The server-side refusals still apply (credentials.md
[device-username-is-server-side]).
Pinned by: `tests/test_templates.py::test_with_no_device_username_the_new_run_page_says_so_instead_of_a_form`, `tests/test_templates.py::test_with_no_device_username_the_gate_says_so_instead_of_a_form`

**[NOTE]**
When the two-person rule for runs blocks the viewer, the gate shows
`partials/needs_other_approver.html` instead. That takes precedence over
the missing-username notice.

### 2.6 [flash-default-is-an-error]
**[SPEC]**
A flash with no category renders as `alert-error`. Successes pass
`'success'`, and neutral notices pass `'info'`. The remaining
uncategorised calls are refusals, so don't change the default.
Pinned by: `tests/test_templates.py::test_an_uncategorised_flash_still_reads_as_an_error`, `tests/test_templates.py::test_a_success_flash_is_not_styled_as_an_error`

### 2.7 [csrf-token-in-every-post-form]
**[SPEC]**
Every POST form carries a CSRF token, the logout form in the layout
included. `tests/test_templates.py` checks this with its own app that
has CSRF switched on, because the shared fixture switches it off.
Pinned by: `tests/test_templates.py::test_every_post_form_carries_a_csrf_token`, `tests/test_templates.py::test_the_logout_form_in_the_layout_has_a_token`

---

## 3. What the pages say about the sibling

### 3.1 [worker-status-is-read-only]
**[SPEC]**
`nethub/worker_status.py` reads rows and never writes one. If Flask
"fixed" a stalled job, it would be writing job state after dispatch,
which only the sibling may do.
Pinned by: none

### 3.2 [refresh-while-live]
**[SPEC]**
A run page reloads every 5 s (`<meta http-equiv="refresh">`) while any of
its jobs is `running` or queued-and-due (`worker_status.is_waiting`). A
job scheduled for later does not trigger a refresh; it gets a line
saying when it starts and who approved it. A stalled running job still
refreshes and shows its own warning. A scan page refreshes while its scan
is queued or running.
Pinned by: `tests/test_templates.py::test_the_run_page_refreshes_while_a_job_is_queued`, `tests/test_templates.py::test_the_run_page_stops_refreshing_at_a_gate`, `tests/test_templates.py::test_a_scheduled_job_is_shown_with_its_window_and_does_not_refresh`, `tests/test_templates.py::test_a_finished_scan_does_not_refresh`

### 3.3 [stalled-after-three-beats]
**[SPEC]**
A `running` job is shown as stalled when its `heartbeat_at` (or
`started_at`, if there is none) is older than `STALLED_AFTER`, which is
3 × `HEARTBEAT_INTERVAL` = 90 s. A running row with neither timestamp
counts as stalled.
Pinned by: `tests/test_frontend.py::TestStalled::test_the_threshold_is_three_heartbeat_intervals`, `tests/test_frontend.py::TestStalled::test_a_running_row_with_no_heartbeat_falls_back_to_its_start`, `tests/test_templates.py::test_a_job_with_a_stale_heartbeat_shows_as_stalled`

### 3.4 [no-worker-is-inferred]
**[SPEC]**
"No worker has picked this up. Is nethub-sibling running?" appears when
something has been queued for over `NO_WORKER_AFTER` (1 min) and no live
work is visible. Queued time is measured from `due_at()`, so a
scheduled job is not "stuck". Live work means a running job with a fresh
heartbeat, or a scan started within `STALLED_AFTER`. A stalled job does
not count as live. A queued scan is timed from its `created_at`. On a run
page the notice also requires one of that run's own jobs to be queued.
Pinned by: `tests/test_frontend.py::TestNoWorker::test_a_job_queued_over_a_minute_with_nothing_running_is_not`, `tests/test_frontend.py::TestNoWorker::test_a_stalled_phase_does_not`, `tests/test_templates.py::test_a_job_whose_window_has_just_opened_is_not_a_missing_worker`, `tests/test_frontend.py::TestNoWorker::test_a_queued_scan_counts_too`

**[NOTE]**
The sibling has no heartbeat of its own outside a phase, so its health
is inferred from the queue instead of adding a column. This is the
maintainer's chosen trade-off.

### 3.5 [queue-depth-on-approve]
**[SPEC]**
The approve form says how many jobs are queued ahead and running, across
every run (`worker_status.queue_depth`). Jobs scheduled for later are not
counted.
Pinned by: `tests/test_frontend.py::test_queue_depth_counts_every_run`, `tests/test_templates.py::test_the_approve_form_says_what_is_queued_ahead`

---

## 4. How it works

**[SPEC]**
- Pages are server-rendered Jinja. `layouts/main.html` is the shell,
  `pages/*` hold the pages, `partials/*` the reusable pieces, and
  `errors/{403,404,413,500}.html` the error pages.
- `nethub.css` holds what Pico lacks:
  - the flash colours (`alert-error`, `alert-success`, `alert-info`);
  - `.warning`, table `.actions` and `dl.facts`, which wraps digests with
    `overflow-wrap: anywhere`;
  - the explainer rules.
- Every table sits in a `<div class="overflow-auto">`, so a wide table
  scrolls inside itself rather than widening the page. Pages have been
  checked at 360 px.
- Times are UTC throughout, including the `datetime-local` start-time
  field.

Pinned by: `tests/test_templates.py::test_every_page_renders`, `tests/test_templates.py::test_every_table_can_scroll_on_its_own`, `tests/test_templates.py::test_the_approve_form_offers_a_start_time_in_utc`

---

## 5. Decisions

**[NOTE]**
- **No JavaScript at all.** Why: it lets the CSP be `script-src 'none'`
  on the pages where people type AAA passwords. Two consequences follow:
  confirmations are checkboxes that the route checks, and waiting pages
  use a meta refresh.
- **Pico CSS, vendored.** A Tailwind or DaisyUI build was set aside: it
  needs a Node toolchain in the image for no gain on forms and tables.
- **No JSON API.** Routes redirect after a POST. A `202` + job-id API is
  future.md material.

---

## 6. Known gaps

**[SPEC]**
- Rule 3.1 has no test; the module is read-only by inspection.
- Weak pins:
  - 1.1 and 2.7 render only the `PAGES` list plus a few extras. The run
    page's forms (approve, retry, cancel) and the 403/413/500 pages are
    not in the CSRF or no-script sweep.
  - 2.1: the role change has no unticked-box route test.
  - 3.5: excluding scheduled jobs from the count is untested.
  - §4: the table-wrapper test skips the run and history pages.
- `worker_status` imports `phases._aware` and `upgrades._utcnow`, which
  are private helpers from other modules.
