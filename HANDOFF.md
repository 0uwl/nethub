# HANDOFF — remediation plan for the branch review

**Status:** WS-1.1 done. Everything else in this file is not started.
**Branch:** `claude/repo-branch-init-5n0qhr`
**Source:** a four-lane review (device layer, web tier, frontend, security) of
`origin/main..HEAD` — the whole Ansible→Netmiko migration, 103 files, +10,405/−1,684.
Every finding below was traced in code, and the ones marked *reproduced* were
demonstrated by running something.

This file exists so a session with **no prior context** can pick a task and start
working in about five minutes. Read §1 and §2 before touching anything. §2 is the
part that will save you from writing a patch that gets rejected.

---

## 1. Orientation

### What NetHub is

A Flask app that upgrades Cisco IOS-XE switches. It holds an admin's *device*
credential in memory transiently and pushes ~1 GB firmware images to network gear.
Two properties matter more than anything else in the codebase:

1. **The device credential never reaches disk, a log, a row, or a process argument.**
2. **The SSH host-key pin is never bypassable.**

A compromise here is fleet-wide network control. Treat both as load-bearing.

`CLAUDE.md` is the real specification — ~400 lines of settled decisions *with their
reasoning*. Read it before you write code. `design-document.md` is the target
architecture (much of it unimplemented); `alpha.md` records where this slice
deliberately deviates.

### Layout

| Path | What it is |
|---|---|
| `nethub/devices/` | The device layer — `connection.py`, `transfer.py`, `install.py`, `facts.py`, `phases.py`. Ordinary Python over Netmiko. |
| `nethub/sibling.py` | The out-of-band dispatcher. Runs as its own unit, never inside Flask. |
| `nethub/credential_socket.py` | The §9.1 credential channel. Flask serves, the sibling connects. |
| `nethub/upgrade_routes.py`, `upgrades.py` | Thin routes over a service module. Submit, approve, host-key confirm. |
| `nethub/artifacts.py`, `artifact_routes.py` | The image store. Upload → SHA-512 verify → record. |
| `nethub/auth.py`, `bootstrap.py` | Local username/password login; first-boot admin. |
| `nethub/models.py` | Schema, plus §7.3's vocabularies as module constants. |
| `quadlet/nethub.container` | Reference Podman Quadlet unit. **Currently stale — see WS-1.** |
| `tests/captures/` | **Evidence, not fixtures.** Verbatim real-hardware output. Never edit a capture to make a test pass. |

### Environment setup — three traps that will cost you time

```bash
# 1. Distro PyYAML has no RECORD file and breaks a plain install.
pip install --ignore-installed PyYAML -r requirements.txt

# 2. The `pytest` on PATH is a uv-isolated tool that CANNOT see project deps.
#    It fails with a misleading `ModuleNotFoundError: No module named 'flask'`
#    raised from conftest. That is an environment artifact, not a bug. Always:
python -m pytest -q

# 3. Local Python here is 3.11; CI and the Containerfile pin 3.12.
#    Re-check anything version-sensitive against 3.12.
```

### Baseline you must not regress

```
python -m pytest -q     →  277 passed
ruff check .            →  All checks passed!
python -m yamllint .    →  clean
```

Run all three before and after every change. If your change adds tests, the count
goes up; it must never go down.

### Commit conventions

Conventional-commit prefixes (`fix:`, `feat:`, `refactor:`, `test:`, `docs:`).
The existing history is a good guide — commit messages here explain *why*, at
length, and that style is expected. One task per commit where practical.

---

## 2. NON-NEGOTIABLES — read before writing any patch

`CLAUDE.md` has a "Hard rules — do not implement these" section. These are settled
decisions with written reasoning, not oversights. **A patch that does any of the
following will be rejected regardless of how well it is written.** They are listed
here because every one of them looks like a reasonable improvement on a first pass.

- **Do not swap SHA-512 for MD5**, or add a second hash algorithm / `hash_algo`
  column. The swap was considered on 2026-09-09 and rejected on chosen-prefix
  collisions. The reasoning is in `CLAUDE.md`; do not re-litigate it.
- **Do not use `netmiko.file_transfer()`** or remove `hash_supported=False` from
  `_scp_put`. That flag suppresses a pointless ~1.2 GB MD5 pass.
- **Do not restore `load_system_host_keys()`** in `connection.py`. Loading no keys
  is *the mechanism* that guarantees our policy always runs.
- **Do not add a TOFU / first-contact-accept branch** to `connect()`. First contact
  is deliberately a separate admin action.
- **Do not add an enable secret, a `secret=` kwarg, or a call to `.enable()`.**
  Privilege 15 at login is the design.
- **Do not add `SO_PEERCRED`** to the credential socket. Under one rootless uid it
  discriminates nothing; the bind mount is the authenticator.
- **Do not run more than one gunicorn worker.** (More *threads* is required — see
  WS-1.3. Workers and threads are different halves of one rule.)
- **Do not make `pull_sftp` the default** or propose it as a fix for a push problem.
- **Do not let a request supply** a device username, credential, filename, SHA-512,
  or the image transport.
- **Do not resolve a submitted hostname to an IP.** Hostnames are refused by design.
- **Do not make an empty `DEVICE_TARGET_CIDRS` mean "allow all."**
- **Do not add a role dropdown for OIDC-backed users**, or a `must_reset_password`
  column without the reset flow that reads it.

If you believe one of these is genuinely wrong for a task you are doing, **stop and
raise it with the maintainer**. Do not work around it.

Two more process rules:

- **Never edit anything under `tests/captures/`.** It is the only evidence that
  ntc-templates parses what this fleet actually runs.
- **Never skip, disable, or `xfail` a test to get green.**

---

## 3. How the work is grouped

Six workstreams. **WS-1 through WS-5 are independent of each other** and can be done
in any order or in parallel. WS-6 needs a decision from the maintainer first. WS-7
needs hardware nobody in a container has.

Recommended order if you are working alone: **WS-1 first.** It is the highest
leverage per line changed, it is self-contained, and several of its items are what
make other findings exploitable.

| WS | Theme | Items | Risk |
|---|---|---|---|
| WS-1 | Deployment & configuration | 5 | Low — config only, no logic |
| WS-2 | Credential lifecycle | 4 | Medium — touches the crown jewel |
| WS-3 | Job state machine & liveness | 4 | Medium — concurrency |
| WS-4 | Failure classification & error hygiene | 4 | Low–medium |
| WS-5 | Web tier & frontend | 7 | Low |
| WS-6 | Needs a maintainer decision | 3 | — blocked |
| WS-7 | Needs hardware | 3 | — blocked |

---

## WS-1 — Deployment & configuration

The reference deployment cannot currently run the day-2 path. These are mostly
config-file edits with no application logic, which is why they are first.

### WS-1.1 — Remove the working default `SECRET_KEY` · HIGH — **DONE**

> Landed: the unit ships `Environment=SECRET_KEY=` commented out, and `config.py`
> now refuses a placeholder or a key under 32 characters as well as an absent one.
> `tests/test_config.py` covers it. `conftest.py`, `dev.sh` and `README.md` needed
> longer keys to satisfy the new rule — check that first if a fresh clone fails to start.

**Where:** `quadlet/nethub.container:63`, `nethub/config.py`

**Background.** The unit ships `Environment=SECRET_KEY=CHANGE_ME_use_openssl_rand_hex_32`
uncommented and live. `config.py` refuses an *absent* key but has no check for a
known-weak one, so a unit copied per its own install instructions starts normally
with a signing key published in a public GitHub repo. Flask's signed session cookie
is the only thing authenticating a user — there is no server-side `sessions` row in
this alpha (a known §4.5 deviation, see `alpha.md`) — so anyone who can read the repo
forges a cookie for `_user_id = "1"` and is the bootstrap admin, with no password and
no login event.

**Do:**
1. Comment out the `Environment=SECRET_KEY=` line in the unit; leave the
   `LoadCredential=` path as the documented mechanism and make the comment above it
   say plainly that the unit will not start until one of the two is supplied.
2. In `config.py`, refuse a `SECRET_KEY` that is on a known-bad list (at minimum the
   literal string above) or shorter than 32 characters, raising the same way the
   absent case already does. `config.py` already models this "fail closed on an unset
   security setting" pattern for `DEVICE_TARGET_CIDRS` — match it.

**Verify:** a test that `create_app()` raises for the known-bad literal and for a
short key, and still raises for `None`. Existing config tests show the pattern.

**Don't:** don't generate a key at runtime as a fallback. Failing to start is correct.

---

### WS-1.2 — Fix the stale Quadlet unit · HIGH

**Where:** `quadlet/nethub.container`

**Background.** The unit predates build step 7 and describes a subsystem that no
longer exists. Enumerated against current code:

- Sets `Environment=REGISTRIES_ROOT=/app/registries` and mounts a volume for it.
  `REGISTRIES_ROOT` was **deleted at build step 7**; nothing reads it. The whole
  "--- Optional ---" block about adopting `group_vars/os_iosxe.yml` is dead.
- **Never sets `ARTIFACT_STORE`**, so it defaults to `<repo root>/instance/artifacts`
  = `/app/instance/artifacts` — inside the image, under `ReadOnly=true`, with no
  volume and no tmpfs. `artifacts.store_dir()` (`artifacts.py:52`) calls
  `os.makedirs(..., exist_ok=True)` with no error handling, so the artifacts page
  raises `OSError` → 500 on every load. **The day-2 store is unusable as shipped.**
- Never sets `DEVICE_TARGET_CIDRS` (unset = every submit refused — correctly
  fail-closed, but undocumented in the unit) or `IMAGE_TRANSPORT`.
- `[Service]` has no `LimitCORE=0`, `NoNewPrivileges=`, or `ProtectProc=`.

**Do:**
1. Delete the `REGISTRIES_ROOT` env var, its volume mount, and the optional block.
2. Add `Environment=ARTIFACT_STORE=/app/artifacts` with a matching `Volume=` line,
   following the `/app/data` pattern already in the file.
3. Add commented `DEVICE_TARGET_CIDRS=` and `IMAGE_TRANSPORT=` lines with a note that
   an unset CIDR list refuses every submit.
4. Add `LimitCORE=0` to `[Service]`. `CLAUDE.md` already names this as an open item —
   a crash otherwise writes the heap, including a live device credential, to
   `/var/lib/systemd/coredump`.

**Verify:** you cannot run Podman here. Re-read the file against `config.py`'s actual
env var names and confirm every one the app reads is either set or deliberately
commented. Note in the commit message that this was **not** verified against a real
`podman run` — `CLAUDE.md` claims the unit was verified end-to-end, and that claim now
predates step 7, so do not restate it.

**Don't:** don't move `LoadCredential=`/`SetCredential=` out of `[Service]` — they are
systemd directives and are correct where they are. Don't add a `Pod=`; separate PID
namespaces are what prevent same-uid `ptrace` between Flask and the sibling.

---

### WS-1.3 — Set `threads` in the gunicorn config · HIGH

**Where:** `nethub/gunicorn.conf.py`

**Background.** The file sets `workers = 1` but never sets `threads` or
`worker_class`, so gunicorn 26.2 defaults to `sync` with **one thread**. `CLAUDE.md`
is explicit that this is half a rule: *"Flask runs exactly one worker — and more than
one thread… Threads are the other half of the same rule and are required, not
optional."* The reasoning: a 1.5 GB upload plus its SHA-512 pass is the longest
operation in the system after device work, and single-threaded it holds the entire
server for that window — the exact failure §3.2 legislates against. §3.2's stated
budget is **2 s p99 on phone-home**.

Compounding: `timeout = 120` with the `sync` worker is enforced per request, so a
1.2 GB upload under ~10 MB/s is killed mid-ingest and the worker restarted — which
also destroys the in-memory `CredentialStore` and fails every pending approval with
`failure_stage='credential'`.

**Do:**
1. Set `worker_class = 'gthread'` and `threads` to a small fixed number (4 is a
   reasonable start), with a comment explaining that this is the second half of the
   one-worker rule, not a tuning knob.
2. Reconsider `timeout` for long uploads. With `gthread` the timeout applies to
   worker liveness rather than a single request, which is part of why the thread
   change matters — say so in the comment.

**Verify:** `python -c "import nethub.gunicorn.conf"` won't work (dotted name); load
it with `runpy` or just assert the values by reading. A test is not really available
here — state in the commit message that this is config verified by inspection against
the installed gunicorn's defaults.

**Don't:** don't add a `WORKERS` env var. Don't raise `workers` above 1 — a credential
submitted to worker A is invisible to worker B and it fails closed but intermittently.

---

### WS-1.4 — Pin dependencies · MEDIUM

**Where:** `requirements.txt`

**Background.** Nine dependencies, zero version constraints, no lockfile, no hashes —
while CI's `publish` job resolves them fresh on every push to `main` and pushes the
result to `ghcr.io/<repo>:latest` for two architectures. This undercuts claims
`CLAUDE.md` actively relies on: *"the pinned gunicorn version (26.x) parses its app
argument as a Python expression"* — nothing is pinned, and gunicorn has already
changed relevant behaviour once at 25.1 (`control_socket_disable`). A poisoned
upstream release ships to `:latest` with no diff for anyone to review.

Also: `PyYAML` is still listed but imported nowhere in `nethub/`, `tests/`, or
`scripts/` — a leftover from the YAML registry deleted at build step 7.

**Do:**
1. Pin every dependency to the version currently resolved (`pip freeze` for the nine
   direct ones — do **not** paste the full transitive freeze into `requirements.txt`).
2. Remove `PyYAML`. Confirm first with `grep -rn "import yaml\|yaml\." nethub/ tests/ scripts/`.
   Note that `yamllint` is a CI-installed tool, not a runtime dep — removing PyYAML
   from `requirements.txt` does not affect it.
3. Mention in the commit message that a hash-pinned lockfile is the stronger fix and
   was not done here.

**Verify:** `pip install --ignore-installed PyYAML -r requirements.txt` in a clean
env, then the full baseline. Watch for anything that was silently relying on PyYAML.

---

### WS-1.5 — Session cookie flags · LOW

**Where:** `nethub/config.py` (these keys are currently absent)

**Background.** Observed from a real login: `Set-Cookie: session=...; HttpOnly; Path=/`.
`SESSION_COOKIE_SECURE` is False, `SESSION_COOKIE_SAMESITE` is unset, and
`session.permanent` is never set so the cookie carries no expiry of its own. Design
doc §4.5 asks specifically for `SameSite=Strict` on approvals — *"a cross-site
'approve: reload' is a fleet outage."* CSRF is genuinely complete (verified), so this
is defence in depth rather than an open hole — but the Quadlet publishes plain HTTP
on 8080, so the cookie travels in cleartext without a TLS terminator in front.

**Do:** set `SESSION_COOKIE_SAMESITE = 'Strict'`, `SESSION_COOKIE_HTTPONLY = True`
(explicit, even though it is Flask's default), and `PERMANENT_SESSION_LIFETIME` to a
sane absolute bound. Make `SESSION_COOKIE_SECURE` default **on**, with an env var to
turn it off for local HTTP dev — and document that a deployment without TLS in front
must set it.

**Don't:** don't build the §4.5 server-side `sessions` table here. That is a real
feature with its own design, and `alpha.md` records its absence as deliberate.

---

## WS-2 — Credential lifecycle

The crown jewel. Be careful and add a test for everything.

### WS-2.1 — Nothing ever purges held credentials · HIGH

**Where:** `nethub/credential_socket.py:129` (`discard`), `:133` (`purge_expired`),
`nethub/upgrade_routes.py:47-50` (`_hold`)

**Background.** `CredentialStore.purge_expired()` and `discard()` have **no callers
anywhere in `nethub/`** — the only call site is `tests/test_credential_socket.py:106`.
The TTL is enforced *only* inside `release()`, i.e. only if the sibling eventually
asks for that exact job. So the module's own docstring claim that the store is
"bounded by a TTL and emptied by use" is half true: emptied by use, bounded by
nothing.

Concretely: an admin approves `activate` and types their device password. The sibling
is down. An operator clicks Cancel; the job ends `cancelled` — and nothing calls
`store.discard(job.id)`. A named human's plaintext AAA password now sits in a dict in
the gunicorn worker that serves the only unauthenticated route, past its 30-minute
TTL, until the worker restarts. With `workers = 1` and a long-lived Quadlet unit that
is measured in weeks. Same outcome for `expired`, `abandoned`, or a superseded
approval. This is what gives the acknowledged `LimitCORE=0` gap something worth
dumping.

**Do:**
1. Call `store.discard(job.id)` wherever Flask learns a job will never run — the
   cancel path in `upgrade_routes.py` is the clear one.
2. Call `purge_expired()` on a cheap schedule. **Do not add a background thread for
   this**; the simplest correct option is to call it at the top of
   `CredentialStore.hold()` and `release()`, so the store is swept on every use. Say
   in the comment why a timer was not used.
3. Consider whether `release()` should zero the password string after popping. Python
   makes this unreliable (`str` is immutable), so if you skip it, say so rather than
   implementing something that looks like a wipe and is not.

**Verify:** tests that (a) a cancelled job's credential is gone from the store, and
(b) `hold()` on a store containing an expired entry drops it. Test 4 in the TEST GAPS
list below is exactly this.

**Don't:** don't widen the socket protocol to carry a "forget this" message from the
sibling. §9.1 keeps that channel to one purpose.

---

### WS-2.2 — Device password in three dataclass reprs · LOW (but cheap)

**Where:** `nethub/devices/phases.py:78` (`PhaseContext`),
`nethub/credential_socket.py:65` (`_Held`), `nethub/devices/transfer.py:97` (`PullTarget`)

**Background.** All three hold the credential in a field with no `repr=False`, so the
auto-generated repr prints it in cleartext. Reproduced:
`PhaseContext(device_username='jsmith', device_password='SUPERSECRET', ...)`.

No path renders any of them today — Python tracebacks carry no frame locals, `DEBUG`
is off, and neither netmiko nor paramiko defines a repr that would pull one in. This
is latent, not live. It is worth fixing anyway because `PhaseContext` is the object
`CLAUDE.md` names as the credential's *entire* lifetime container, and one
`log.debug("ctx=%r", ctx)` added while debugging writes it to journald, `podman logs`,
or a CI log retained far longer than the phase.

**Do:** `field(repr=False)` on the three password fields. Add a test that
`repr(ctx)` does not contain the password.

**Don't:** don't write a custom `__repr__` that prints `***` unless you also handle
`__str__` — `field(repr=False)` is the whole fix and costs nothing.

---

### WS-2.3 — Device password via environment variable · LOW

**Where:** `nethub/upgrade_cli.py:166`, `scripts/check_device_facts.py:59`

**Background.** Both tools read the password from `NETHUB_DEVICE_PASSWORD`.
`CLAUDE.md`'s hard rule is *"The device credential never reaches disk … no
`podman run -e` showing up in `/proc/<pid>/cmdline`"* — an env var is the same class
of exposure. `/proc/<pid>/environ` holds it for the whole run (up to ~20 minutes for
`--phases all`), every child inherits it, and an inline invocation lands in shell
history.

**Do:** keep the `getpass` fallback as the primary path. Either drop the env var from
`upgrade_cli.py` (the tool is interactive by design — it prompts at every mutating
phase anyway), or print a clear warning when it is used. Document the choice.

**Note:** this interacts with WS-6.1 — `check_device_facts.py` may be getting deleted
or rewritten, so coordinate.

---

### WS-2.4 — `{"job_id": true}` releases job 1's credential · LOW

**Where:** `nethub/credential_socket.py:164`

**Background.** `isinstance(job_id, int)` is satisfied by `bool`, and
`hash(True) == hash(1)`, so the dict lookup succeeds and the store hands back the
credential held under key 1. Reproduced. Impact is bounded — the mount is the
authenticator and the sibling never sends a bool — but this function's entire purpose
is validating a message before it selects a secret.

**Do:** `if type(job_id) is not int:` or
`if not isinstance(job_id, int) or isinstance(job_id, bool):`. One line, plus a test.

---

## WS-3 — Job state machine & liveness

### WS-3.1 — An abandoned phase can never be re-approved · HIGH

**Where:** `nethub/sibling.py:76-96` (`sweep`), `:182` (`_fail_run`),
`nethub/upgrades.py:222-258` (`approve`)

**Background.** This one was found independently by two review lanes, from opposite
ends of the codebase.

`sweep()` marks a stale row `abandoned` and then calls `_fail_run(job.run)`, which
sets `run.state = 'failed'` and clears `awaiting_phase` — identical treatment to a
real failure. `approve()` opens with `if run.state != 'awaiting_approval': raise
RequestError(...)`, so the retry is refused forever.

The retry was **designed for**: `models.py:282` comments that `attempt` exists
specifically "to keep that constraint from also forbidding the fresh retry §7.3 grants
an abandoned phase," and `upgrades.py:241` computes the next attempt by counting
abandoned jobs. That expression has never returned anything but 1. `CLAUDE.md` states
the intent plainly: *"An `abandoned` device-touching phase needs a fresh approval, not
an auto-retry — the approval is what supplies the credential and names the human. The
retry is a new row with an incremented `attempt`."*

Practical cost: the sibling is OOM-killed during `stage` on a 30-device wave. On
restart the run is `failed`; the devices hold a verified staged image, the pin and
artifact are unchanged, and the only way forward is a **new** run — re-collecting a
credential, re-running pre-check, and re-snapshotting `device_username_used`.

**Do:**
1. In `sweep()`, distinguish `abandoned` from a genuine failure: instead of
   `_fail_run`, return the run to `awaiting_approval` with `awaiting_phase` set to the
   abandoned phase, so `approve()` can create the next `attempt`.
2. Check what `_advance_run` and the terminal-status DDL trigger allow — `models.py`
   attaches a real trigger on `after_create` that raises `IntegrityError` (not
   `OperationalError`) on an illegal terminal transition. Your change must not fight it.
3. Confirm `attempt = 1 + count(abandoned)` then computes 2 and the
   `UNIQUE(run_id, phase, attempt)` constraint permits the new row.

**Verify:** the missing test is the whole point — walk `sweep()` → `approve()` in one
test and assert a second job row is created with `attempt == 2`. Note that
`tests/test_sibling.py::test_a_foreign_running_row_is_abandoned` currently **pins the
broken behaviour as correct** without ever attempting the retry; that test will need
updating, and its comment should say why.

**Don't:** don't auto-retry. The approval is what supplies the credential and names
the human — that is the entire reason this is a gate and not a retry loop.

---

### WS-3.2 — A socket `OSError` strands the claimed job forever · HIGH

**Where:** `nethub/sibling.py:146-154`, `nethub/credential_socket.py:227` / `:280`

**Background.** `run_once` wraps `fetch_credential` in `except CredentialError` only.
But the connection itself is made by `connect_to(path)._open()` → `socket.connect(path)`,
which raises `FileNotFoundError` / `ConnectionRefusedError` — both `OSError`, neither a
`CredentialError` — and `fetch_credential` deliberately does not wrap
`connect_socket()`. The exception escapes to `main()`'s `except Exception`, which logs
and continues.

By then `claim()` has already committed `status='running'` with **this** instance's
`runner_instance_id`. `next_queued()` will never return the row again, and `sweep()`
only matches rows where `runner_instance_id != self.runner_instance_id` — so the
instance that stranded the row is *structurally incapable* of sweeping it. The run
sits `running` forever and (with WS-2.1) the credential stays held.

Realistic trigger: the Flask unit restarting at the moment the sibling picks up a job
— exactly what a deploy or an OOM kill produces. The rare failure is handled cleanly;
the common one is not.

**Do:** catch `OSError` alongside `CredentialError` in `run_once` and take the same
path — `failure_stage='credential'`, `_fail_run`, commit. Keep the two messages
distinguishable in `error_summary` (a socket that is not there is a different
operational problem from a refused credential).

**Verify:** a test where `connect_socket` raises `ConnectionRefusedError` and the job
ends `failed`, not `running`. This is TEST GAP 3 below.

---

### WS-3.3 — `deadline_at` is never written · MEDIUM

**Where:** `nethub/models.py:309` (declared), `sibling.py:139` and
`devices/phases.py:355` (read), **written nowhere**

**Background.** No code path sets `UpgradePhaseJob.deadline_at` — not
`upgrades.submit`, not `upgrades.approve`, not `sibling._advance_run`. So
`phases.execute_phase`'s between-hosts deadline check and `sibling.run_once`'s
`expired` branch never fire, and two of §7.3's terminal states (`timed_out`,
`expired`) cannot be reached. Both existing tests construct the row with `deadline_at`
by hand, so the suite is green over inert machinery.

Cost: a stage phase on a 40-host wave hits a device that answers SSH but never
finishes the SCP put. `TRANSFER_READ_TIMEOUT` is 7200 s, so that one host burns two
hours; with no `deadline_at` the wave has no wall-clock bound of any kind, the
sibling's single FIFO queue is blocked behind it, and only Cancel helps — which is
honoured *between* hosts.

`UpgradeRun.gate_expires_at` has the mirror problem: `_advance_run` sets it and
nothing reads or enforces it, so a run parks at a gate indefinitely.

**Do:** set `deadline_at` when the `queued` row is created, in both `submit()` and
`approve()`. Derive it from a per-phase budget — `CLAUDE.md`'s measured timings are
the input: stage ~370 s for 471 MB at ~1.4 MB/s (so scale by image size and host
count), `install add … activate commit` 605–622 s, reload 228–238 s against a 900 s
default, cleanup ~5 s. Leave real headroom; a deadline that fires on a healthy run is
worse than none.

**Careful:** `phases._aware()` and `sibling._aware()` exist because **SQLite returns
naive datetimes for values written aware**, and comparing `now()` against a stored
`deadline_at` raises `TypeError` for any row read back from the database — always in
production, never in a test that skips the round trip. Route every comparison through
one of them.

**Verify:** a test asserting `submit()` and `approve()` produce a non-null
`deadline_at` — TEST GAP 1, and the one that fails today.

---

### WS-3.4 — `sweep()` misses a NULL `runner_instance_id` · LOW

**Where:** `nethub/sibling.py:87`

**Background.** `UpgradePhaseJob.runner_instance_id != self.runner_instance_id`
evaluates to NULL — not true — for a NULL column, so such a row is invisible to every
sweep forever. **Latent, not live:** `claim()` sets `status` and `runner_instance_id`
in one atomic UPDATE, so no current path produces this row. It matters because the
sweep is the only mechanism that un-sticks a crashed execution, and the correct
predicate is strictly safer.

**Do:** `or_(UpgradePhaseJob.runner_instance_id.is_(None), UpgradePhaseJob.runner_instance_id != self.runner_instance_id)`.
Add a test with a hand-built NULL row.

---

## WS-4 — Failure classification & error hygiene

### WS-4.1 — The reload loop swallows host-key and auth failures · HIGH

**Where:** `nethub/devices/install.py:233`

**Background.** The reconnect loop catches bare `Exception` as "not back yet" — the
comment says so explicitly. Two of the three exceptions `connection.py` is documented
to raise are never transient:

- **`HostKeyError`** — a device presenting a *different* host key after reload is the
  precise signal the pin exists to produce. It is reported as
  `ReloadTimeout(status='reload_timeout')` → `failure_stage='reload'`. The dashboard,
  §6.1's error envelope, and any alerting keyed on `failure_stage` all read "the
  device did not come back" for a key change. No credential is sent (the policy fires
  before auth), so this is diagnostic loss, not disclosure.
- **`AuthenticationError`** — with `DEFAULT_RELOAD_WAIT` (delay 60 s, interval 30 s,
  timeout 900 s) the loop performs **~28 full logins** with the same credential. If a
  device comes back with its AAA server unreachable and falls back to a local database
  the submitter is not in, NetHub presents `jsmith`'s real credential 28 times in 15
  minutes. Any TACACS+/RADIUS deployment with a failed-attempt lockout (3–5 is a
  common default) locks `jsmith` out of the **whole fleet**, and the run reports
  `failure_stage='reload'` rather than `credential`.

This is also the one place `connection.py`'s property — *"the three exceptions are the
three `failure_stage` values, so mapping them in `phases.py` is a lookup rather than a
judgement"* — is overridden, inside `install.py` where `phases.py` cannot see it.

**Do:** re-raise `HostKeyError` and `AuthenticationError` out of the loop immediately
rather than treating them as transient, so `phases.failure_stage_for` classifies them
as `hostkey` and `credential`.

**Read WS-7.3 first.** There is an open hardware question — whether an IOS-XE upgrade
can legitimately regenerate the device's SSH host key. If it can, re-raising
`HostKeyError` here turns a *successful* upgrade into a hard failure, and the right
fix is different. `AuthenticationError` has no such doubt; if you want to split the
work, do that half now and leave `HostKeyError` behind a note.

**Careful:** `wait_for_device` takes a connection *factory*, not a connection, and
`upgrade_cli.py` also depends on its contract. Check both callers.

---

### WS-4.2 — `_summarise`'s guard is type-level only · MEDIUM

**Where:** `nethub/devices/phases.py:153-158`, and the wrappers listed below

**Background.** `error_summary` is retained for a year (§7.4), and §7.3 warns that a
stray `str(exc)` there is a durable credential leak with no other symptom. `_summarise`
implements that as an allowlist of exception **types** whose messages are trusted. The
review found that is the wrong axis: several of *our own* exceptions interpolate a
**foreign** exception's text into their message, so the foreign string passes through
verbatim.

The wrappers, all confirmed:
- `transfer.py:277` — `TransferError(f"SCP push of {image} failed: {exc}")`
- `transfer.py:195` — `TransferError(f"cannot read {source}: {exc}")`
- `connection.py:117,124,126,174` — `DeviceConnectionError(f"… {exc}")`
- `facts.py:194` — `FactsError(f"could not parse {command!r}: {exc}")`
- `install.py:241-245` — `ReloadTimeout(f"… last attempt: {last}")`

Reproduced: a netmiko `ReadException` (which embeds `output={repr(output)}` — raw
device channel content) put a config line containing an SNMP community string into
`error_summary`.

**Honest scope, do not overstate it in a commit message:** the reviewer could **not**
demonstrate the *device password* reaching this column, and read netmiko 4.7 and
paramiko 4.0 for credential-bearing exception messages and found none. What is
confirmed is that up to 500 characters of arbitrary device output and library-internal
text reach a year-retained column, and that the stated invariant is not actually
enforced. `capture_running_config` — the one command whose output genuinely carries
AAA secrets — happens to sit outside these wrappers today by luck, not design.

**Do:** the maintainer-suggested shape (see WS-6.2 — this may need a decision) is to
give project exceptions an explicit `summary` attribute set only from
format-controlled text, and have `_summarise` read *that* rather than `str(exc)`.
Then wrapping a foreign exception is structurally unable to widen the summary, and the
`{exc}` interpolations can stay for the `__cause__` chain where they help local
debugging. If you implement the minimal version instead, at least stop forwarding the
library string in the four wrappers above — state the fault, don't quote the library.

**Verify:** the existing `test_the_credential_reaches_no_row` raises a foreign
`RuntimeError` directly, which is the case `_summarise` already handles. Add the
*wrapped* case: raise a foreign exception containing a secret, wrap it in one of ours,
assert the secret reaches no column.

---

### WS-4.3 — A caught host-key mismatch is filed as a transfer error · MEDIUM

**Where:** `nethub/devices/transfer.py:274-277`

**Background.** `_push_scp`'s `except TransferError: raise` / `except Exception as exc:`
pair re-wraps a `connection.HostKeyError` — which is a `DeviceConnectionError`, not a
`TransferError` — into a plain `TransferError`, so `phases.failure_stage_for`
classifies it `'transfer'` instead of `'hostkey'`.

Reachability confirmed: `_scp_put` → `CiscoIosFileTransfer.__enter__` →
`SCPConn.establish_scp_conn`, which calls `self.ssh_ctl_chan._build_ssh_client()` (our
override, so **the pin is correctly enforced on the second session**) and then
`.connect(**params)` with no intervening try/except.

The security control works and no credential is sent. The loss is detection: §7.3 made
`hostkey` its own vocabulary word precisely so this signal is not buried among genuine
flaky-SCP failures. The one event meaning "the pin just caught something" is filed as
routine.

**Do:** re-raise `DeviceConnectionError` (and its subclasses) before the generic
`except Exception` in `_push_scp`, the same way `TransferError` is already re-raised.

**Verify:** a test asserting a `HostKeyError` raised from the SCP path yields
`failure_stage='hostkey'`.

---

### WS-4.4 — Digest normalisation asymmetry · MEDIUM

**Where:** `nethub/devices/install.py:97` (`assert_ready_to_activate`) vs
`nethub/devices/transfer.py:138`

**Background.** `stage_image` runs the caller's `sha512` through `_normalise_digest`
(strip + lower, with a `[0-9a-f]{128}` fullmatch) before comparing.
`assert_ready_to_activate` passes `sha512` straight to `verify_sha512`, which
lower-cases only the value parsed *from the device* and compares against the raw
expected string. An uppercase or whitespace-padded digest therefore **stages
successfully and is then refused at activate**, with an error whose two halves differ
only in case.

Reachable today via `python -m nethub.upgrade_cli --phases all --sha512 <UPPERCASE>`
— the CLI does not normalise either. The web path is safe only because
`artifacts.ingest()` happens to record `hexdigest()`; nothing in
`upgrade_run_hosts.sha512` constrains the case.

It fails *closed*, before `write memory`, which is why this is medium and not high.

**Do:** normalise in `assert_ready_to_activate` (and in the CLI's `--sha512` parsing)
through the same `_normalise_digest`. Better: normalise once at the boundary so both
paths share it.

**Verify:** a test that `stage_image` and `assert_ready_to_activate` agree on an
uppercase digest — TEST GAP 5.

**Don't:** this is about normalising the *single* algorithm. It is not an opening to
revisit which algorithm, or to add a second. See §2.

---

## WS-5 — Web tier & frontend

### WS-5.1 — No UI to set `device_username`, so nobody can submit · HIGH (functional)

**Where:** `nethub/upgrade_routes.py:252` (`set_device_username`), `nethub/templates/`

**Background.** `upgrades.py:171` refuses any submit from a user with no
`device_username`, with a message telling them to "set it on your profile first." The
route that sets it exists — `POST /profile/device-username` — but it is **POST-only
and no template references it**. There is no profile page, no form, and no nav link
(the navbar has only Artifacts / Upgrades / Host keys / Users). On a fresh deployment
**nobody can submit an upgrade run through the web UI at all** until someone edits the
`users` row out of band.

This is one of three independent reasons the app-driven run `CLAUDE.md` lists as the
last open item would not have worked. The other two are WS-1.2 and WS-6.3.

**Do:** add a minimal profile page with a form posting to the existing route, and a
nav entry. Follow the existing template conventions exactly — every form in this app
carries `<input type="hidden" name="csrf_token" value="{{ csrf_token() }}"/>`, and
yours must too.

**Note:** `device_username` is read server-side and a request may never assert
*someone else's*. Setting your **own** through an authenticated form is the intended
mechanism (the route already uses `current_user`), so this does not conflict with §2's
rule — but do not add any path that lets one user set another's.

---

### WS-5.2 — `approve()` check-then-insert races to a 500 · MEDIUM

**Where:** `nethub/upgrades.py:244-250`, `nethub/upgrade_routes.py:208-220`

**Background.** Found from two directions (route layer and template layer). `approve()`
checks for an existing job then inserts; its own docstring claims the check "turns that
into a message rather than an `IntegrityError`", which is true single-threaded and
false under the threads the design requires (and which WS-1.3 is about to actually
enable). The route catches only `upgrades.RequestError`.

`UNIQUE(run_id, phase, attempt)` holds, so **there is no double reload** — the losing
admin just gets a 500 that looks like a crash.

**Do:** catch `IntegrityError` around the insert and convert it to the same
`RequestError('That phase has already been approved.')` the check produces. Roll the
session back before re-raising.

**Careful:** the same TOCTOU pattern exists in `artifacts.ingest()` (WS-5.3) and in a
couple of lower-severity spots — fix them as the same shape, not ad hoc.

---

### WS-5.3 — Concurrent uploads corrupt a published artifact's bytes · HIGH

**Where:** `nethub/artifacts.py:106-137`

**Background.** The filename is checked three ways — a DB query at `:106`, an
`os.lexists` at `:114` — then the upload streams and hashes for minutes, then
**`os.replace(temp_path, final_path)` runs unconditionally** at `:137` with no
`O_EXCL` and no re-check, and only then does the UNIQUE constraint fire at commit.

Two concurrent uploads sharing a filename: the loser overwrites the winner's
already-committed bytes before its own `IntegrityError`. The `except` block only
removes `temp_path`, which no longer exists after a successful replace, so nothing
rolls back. Reproduced: the artifact row kept content A's recorded SHA-512 while the
bytes on disk hashed to content B's.

**Impact is narrower than it first looks, and the commit message should say so:** the
device-side `verify /sha512` compares against the *row's* digest, so this fails
upgrades rather than installing wrong bytes, and `check_store()` would flag it. The
chain of custody holds. It is still a silent corruption of the store.

**Do:** make the final move fail rather than overwrite — `os.link(temp, final)` +
`os.unlink(temp)`, or `open(final, 'x')` as a reservation before streaming. Convert
the resulting `FileExistsError` into the same `ArtifactError` the pre-check produces.
Keep the pre-checks for the good error message; the atomic operation is the backstop.

**Don't:** don't take a lock in the Flask process to serialise uploads — that
reintroduces the long-request problem WS-1.3 exists to avoid.

---

### WS-5.4 — Host-key scanning does blocking device I/O in Flask · MEDIUM

**Where:** `nethub/upgrade_routes.py:66` (`scan_hostkey`)

**Background.** `scan_host_key(address)` is called synchronously in a request handler.
The address is validated only as an *IP literal* — `DEVICE_TARGET_CIDRS` is **not**
consulted here, unlike `upgrades.check_target` — so loopback, link-local, RFC1918 and
cloud metadata addresses are all accepted. On failure `flash(str(exc))` returns the
underlying `OSError` text, which distinguishes open from closed from filtered.

So: any authenticated user can walk the internal address space through
`POST /hostkeys/scan` and fingerprint any SSH server the NetHub host can reach. With
`CONNECT_TIMEOUT = 30.0` and WS-1.3's single thread, the same endpoint is also a
one-request DoS.

**Do (safe, uncontroversial part):** apply the same `DEVICE_TARGET_CIDRS` check
`upgrades.check_target` uses, and replace the flashed `str(exc)` with a fixed message
per failure class rather than the raw OSError text.

**Do not do the structural part without asking** — see WS-6.2. Whether `scan_host_key`
belongs in Flask at all is a design question, and moving it to the sibling is awkward
(the sibling has no request/response channel back, and the job row is deliberately the
only control channel).

---

### WS-5.5 — Login has no rate limit, no lockout, and a timing oracle · HIGH

**Where:** `nethub/auth.py:19-20`, `:51`

**Background.** Reproduced against a live app: **1.40 ms** median for a nonexistent
user vs **104.37 ms** for a real one — a 74× gap, because `User.query.filter_by(...)`
short-circuits before the scrypt pass for a missing user. Usernames are enumerable
through that gap with no subtlety required, and `GET /login` hands out the CSRF token
unauthenticated. 25 consecutive failures were followed by a clean login: no lockout,
no throttle, no failed-login audit row anywhere.

Every login user is an admin in this alpha, so one success yields the artifact store,
every host-key pin, and a seat at an approval gate.

Related, same file: `new_user` (`auth.py:51`) enforces **no password policy at all** —
a one-character password is accepted.

**Do:**
1. Close the timing gap: always run a password hash. The standard shape is to compare
   against a fixed dummy hash when the user is absent, so both branches cost the same.
2. Add a failed-login counter with a lockout or backoff. Keep it in the database — an
   in-memory counter dies with the worker, and WS-1.3 is about to make worker restarts
   matter more.
3. Add a minimum password length to `new_user`.
4. Log failed logins. There is currently no record that an attempt happened.

**Careful:** §4.2 of the design doc has a long argument about counters on an
unauthenticated route and unbounded key spaces. That argument is about the *day-0
phone-home* route (unimplemented). `/login` is different — the username space is
bounded by the `users` table — but read §4.2 before designing the counter so you do not
contradict it.

---

### WS-5.6 — Missing 413 handler and no upload feedback · MEDIUM

**Where:** `nethub/__init__.py:50,54` (only 500 and 404 are registered)

**Background.** `MAX_CONTENT_LENGTH` defaults to ~1.5 GB. An oversize upload gets
Werkzeug's bare default error page. There is also no progress feedback on what the
design doc calls the longest non-device operation in the system.

**Do:** register `@app.errorhandler(413)` rendering a real page that states the limit.
Progress feedback is a bigger piece of work — note it and leave it unless asked.

---

### WS-5.7 — Frontend affordances · MEDIUM / LOW

**Where:** `nethub/templates/`

Three small, independent items:

1. **The `activate` approval has no confirmation.** `upgrade_detail.html:27` posts the
   approval form with no `onsubmit="return confirm(...)"`, while artifact delete,
   host-key delete and cancel all have one. This is the action that **reboots
   production network hardware**. Add a confirm with specific copy naming the phase and
   the host count.
2. **All flash messages render as `alert-error`.** `layouts/main.html:71` hardcodes the
   class and no `flash()` call anywhere passes a category, so "Published…" and "Invalid
   username or password" look identical. Pass categories at the ~26 call sites and
   render them.
3. **`delete_hostkey` leaves no audit row** (`upgrade_routes.py:121`), so the
   "deliberate friction" before re-accepting a *changed* key is one extra POST leaving
   no evidence. Needs a decision on where such an audit row lives — see WS-6.

**Note:** the review checked the vendored jQuery 1.11.1 / Bootstrap 3.1.1 CVEs for
*reachability* and found none reachable — the app's JS does nothing with jQuery and the
only Bootstrap component used has a hardcoded target. **Do not spend a cycle upgrading
them** on CVE-scanner output alone; if you upgrade, do it for maintenance reasons and
say so.

---

## WS-6 — Needs a maintainer decision (do not implement unasked)

These are not fixes. Each is a design decision with real trade-offs, and a patch that
picks one unilaterally is likely to be wrong.

### WS-6.1 — Is `check_device_facts.py` still a supported command? · HIGH

`scripts/check_device_facts.py:63-71` connects with stock `ConnectHandler`, whose
default `ssh_strict=False` installs `paramiko.AutoAddPolicy()` — auto-accepting any
host key — and then sends the operator's privilege-15 password. It takes a
**hostname**, not an IP, so DNS decides where that credential goes. It also passes
`secret=password`, contradicting the no-enable-secret rule (inert today; nothing calls
`.enable()`).

The script's own docstring admits it is a "throwaway validation tool". `CLAUDE.md`
lists it under **Commands** as a first-class tool with no warning. Those cannot both
be true for something that sends a fleet-wide credential with `AutoAddPolicy`.

**Two coherent options.** (a) Give it `connection.connect()` and a `--fingerprint`
flag — it already depends on `nethub.devices`, so this is small, and it matches what
`upgrade_cli.py` does. (b) Delete the capture half and keep only `--replay`, which
needs no device and is what `tests/captures/` actually consumes.

**Ask before doing either.** Option (b) removes the ability to capture new releases,
which is how a new IOS-XE version gets validated.

### WS-6.2 — Two structural questions raised by the review

- **Should `_summarise` invert its trust direction?** (WS-4.2.) The `summary`-attribute
  design is cleaner but touches every project exception.
- **Does `scan_host_key` belong in Flask at all?** (WS-5.4.) It is device work in a
  request handler, which is either a hard-rule violation or an unstated exception. It
  should be one or the other on purpose.

### WS-6.3 — The host-key pin is self-asserted, and roles are the real fix · HIGH

`confirm_hostkey` (`upgrade_routes.py:91`) writes `key_type` and `fingerprint_sha256`
**straight from POST form fields**. Nothing corroborates that NetHub ever scanned that
key, nothing requires a scan to have happened, there is no separation of duty between
whoever confirms a pin and whoever submits a run against it, and there is no format
validation on either field. It also performs no `ipaddress.ip_address()` check on
`address`, unlike `scan_hostkey`.

Design doc §4.3 rests the whole "a submitter cannot be handed a colleague's AAA
credential" property on this being *"a separate, explicit admin action"*. In alpha,
everyone who can log in is an admin (`alpha.md`), so the confirmer and the submitter
are the same principal. A web-only user with no shell access can pin a box they
control inside `DEVICE_TARGET_CIDRS`, submit a run naming it, and collect the next
approver's privilege-15 password.

**The CLI break-glass argument does not transfer here.** `CLAUDE.md` justifies
`--fingerprint` on the grounds that someone with host access could edit
`device_host_keys` anyway. This attacker needs only a web login.

**Two honest options**, both decisions rather than patches: (a) state plainly in
`alpha.md` that §4.3's attribution property is **not in force** until roles land, or
(b) require `confirm_hostkey` to match a fingerprint NetHub itself scanned in the same
session, which at least binds a confirmation to an observed key. (b) is implementable
now and is the smaller change; it does not fully close the gap, because the same
person can still scan and confirm.

**Do not implement either without asking.** Note that this finding combines with
WS-1.1 into a full chain from "read the public repo" to "hold fleet credentials".

---

## WS-7 — Blocked on hardware

Nobody in a container can settle these. Do not guess; do not write code that assumes
an answer.

1. **The `pull_sftp` prompt sequence** is still unverified against a real device —
   `CLAUDE.md` already records this as the one item outliving the deleted playbooks.
   Two questions a single run answers: does IOS-XE prompt `Destination filename`
   before `Password:`, and does anything the device writes after the password remain
   in netmiko's `_read_buffer` when `_pull_sftp` returns? Capture the session with
   `session_log` **off** (hard rule) and replay it offline.
2. **Can `read_until_pattern(r"[>#]")` terminate mid-copy?** The pattern is unanchored
   and matches the first `>` or `#` anywhere in the stream. If `copy sftp://…` emits a
   progress hash mark, the adapter returns early and `verify_sha512` hashes a partial
   file.
3. **Can an IOS-XE upgrade legitimately regenerate the device's SSH host key?**
   This decides whether WS-4.1's `HostKeyError` fix is right. If it can, re-raising
   turns a successful upgrade into a hard failure and the design needs a sentence
   about it.

Also still untested per `CLAUDE.md`: a stack or a slower chassis against the reload
deadline, and a push across a constrained WAN link.

---

## 8. Test gaps, ranked

These are worth adding independently of the fixes, in roughly this order. Several are
the test that would have caught the corresponding finding.

1. **A phase job is created with a non-null `deadline_at`** (WS-3.3). Fails today.
2. **The abandoned → re-approve path** (WS-3.1), walking `sweep()` → `approve()`.
   `test_sibling.py::test_a_foreign_running_row_is_abandoned` currently pins the broken
   behaviour.
3. **`run_once` when the socket cannot be connected to at all** (WS-3.2). The
   `CredentialError` branch is covered; the `OSError` branch is not.
4. **A held credential is dropped on cancel/expiry, and something calls
   `purge_expired`** (WS-2.1).
5. **`stage_image` and `assert_ready_to_activate` agree on digest normalisation**
   (WS-4.4). Each is currently tested against a lowercase digest only.
6. **`wait_for_device` distinguishes a non-transient failure** (WS-4.1).
7. **Our exception messages carry no library text** (WS-4.2) — the *wrapped* case.
8. **Template rendering.** There are no template tests at all; only two route tests
   check status codes and none inspect bodies. **CSRF is disabled in the `app`
   fixture**, so nothing currently guards against a token silently disappearing from a
   form. A test with CSRF enabled that asserts every POST form renders a token would be
   high value.
9. **Concurrency tests** for the three TOCTOU sites (WS-5.2, WS-5.3).
10. **`serve()` on a transient accept error**, and **`_read_line` with a slow peer**
    (both `credential_socket.py` — the elapsed-time bound is missing; `settimeout` is
    per-operation, so a one-byte-per-9s peer holds the sibling's only dispatch loop for
    ~22 hours).

---

## 9. When you finish a task

1. Run the full baseline: `python -m pytest -q`, `ruff check .`, `python -m yamllint .`
   Test count must not go down.
2. Commit with a message that explains **why**, matching the existing history's style.
3. **Update `CLAUDE.md` in the same pass** if your change alters what a future session
   needs to know — a design decision, a command, a dependency, a rule about what must
   not be built. There is a `design-doc-sync` skill in `.claude/skills/` covering what
   to check. Several items here will need it: WS-1.2 invalidates the claim that the
   Quadlet unit was verified end-to-end, WS-1.3 changes a documented hard-rule detail,
   and WS-1.4 changes the dependency story.
4. Update the status line at the top of this file, and strike through what you did.

## 10. Provenance

Findings came from four parallel read-only review agents (device layer, web tier,
frontend, cross-cutting security), each carrying an anti-rubric of §2's settled
decisions so none spent effort re-proposing them — none did. Every finding marked
*confirmed* was re-traced independently afterwards; findings marked *reproduced* were
demonstrated by running code. Two findings (WS-3.1 and WS-5.2) were reached by two
lanes separately through different code paths.

Properties the review checked and found **genuinely holding** — do not "fix" these:
the host-key pin as a required positional with no TOFU branch; `HostKeyError` not
subclassing `paramiko.SSHException`; no host keys loading from disk; the policy firing
before `_auth`; the pin covering the second SCP session (which answers design doc
§10's open question — it is covered by construction); `upgrade_cli.py` not being a pin
bypass; the SCP enable/restore bracket; all four stated credential-socket properties;
complete CSRF coverage; every route `@login_required` bar `/login` and `/`; no XSS,
SQL injection, path traversal or mass assignment; and no real secret in git history.
