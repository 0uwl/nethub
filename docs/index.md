# NetHub docs: index
NetHub · documentation entry point · format: HADS without version or changelog (see §2)

---

## AI READING INSTRUCTION

Read `[SPEC]` and `[BUG]` blocks for authoritative facts.
Read `[NOTE]` only if additional context is needed.
`[?]` blocks are unverified; treat them with lower confidence.
Start with §1 to find the file for your task. In that file, scan the
headings first, then read the `[SPEC]` and `[BUG]` blocks of the sections
that apply.

---

## 1. Where to look

**[SPEC]**
| You are touching… | Read |
|---|---|
| How the pieces fit, which process does what, the module map | [architecture.md](architecture.md) |
| The sibling, job/run/host state, phases, retry, canary, cancel, scheduling | [dispatch.md](dispatch.md) |
| The device password: sealing, opening, `error_summary`, privilege 15 | [credentials.md](credentials.md) |
| `nethub/devices/`: Netmiko, SCP push, install/reload, facts, `upgrade_cli` | [device-layer.md](device-layer.md) |
| Host-key pins, scans, confirm/delete, target address checks | [host-keys.md](host-keys.md) |
| Upload/ingest, the artifact store, `check-store`, SHA-512 | [artifacts.md](artifacts.md) |
| Login, sessions, users, roles, two-person rules, `/settings` | [auth-and-roles.md](auth-and-roles.md) |
| Models, migrations, SQLite pragmas, CHECKs and triggers | [schema.md](schema.md) |
| Containers, Quadlet units, env vars, CI, releases | [deployment.md](deployment.md) |
| Templates, CSP, confirmation boxes, worker status | [frontend.md](frontend.md) |
| Fixtures, captures, the end-to-end test | [testing.md](testing.md) |
| Anything not built (day-0, OIDC, retention), open questions, rejected ideas | [future.md](future.md) |

---

## 2. How these files are written

**[SPEC]**
- Every file follows HADS: an H1 title, a metadata line, an
  `AI READING INSTRUCTION` section, numbered H2 sections, and the block
  tags `[SPEC]`, `[NOTE]`, `[BUG]` and `[?]`.
- Two deviations from HADS, on purpose: there is no `**Version**` line
  and no changelog. Git holds the history.
- The metadata line names the code and the tests the file covers.
- Section order in a topic file:
  1. Rules, grouped into numbered H2s.
  2. How it works.
  3. Decisions.
  4. Pitfalls.
  5. Known gaps.
- Each rule is an H3 whose heading is its slug, e.g.
  `### 1.2 [claim-is-conditional]`.
  - Its `[SPEC]` is the rule in at most two sentences, followed by a
    `Pinned by:` line giving pytest node ids, or `none`.
  - A clause saying why breaking the rule matters stays in the `[SPEC]`.
    Longer reasoning goes in a `[NOTE]` right after it.
- Code comments cite a rule as `docs/<file>.md [slug]`, so
  `grep -rn '\[slug\]'` finds the rule and everything that depends on it.
- Decisions are `[NOTE]` blocks, each giving the decision, the reason,
  and what would reopen it. Whatever a decision forbids is also a rule.
- Pitfalls are titled `[BUG]` blocks with Symptom, Cause and Fix.
- Known gaps are `[SPEC]`: true today, and not intended.
- `tests/test_docs.py` fails when a cited test or slug does not exist.

**[NOTE]**
Change a rule in the same commit as the behaviour it describes. Adding a
rule means adding the test that pins it. When a feature is removed,
delete its rules instead of marking them obsolete.

---

## 3. Glossary

**[SPEC]**
| Word | Meaning |
|---|---|
| run | One upgrade request: a set of hosts and one bundle key (`upgrade_runs`). |
| phase | One of `precheck`, `stage`, `activate`, `verify`, `cleanup`, in that order. |
| job | One execution of one phase for one run (`upgrade_phase_jobs`). A run has many. |
| attempt | A job's number for its phase: 1 + the highest so far. Re-approvals and retries make new attempts. |
| gate | A pause before `stage`, `activate` or `cleanup` until someone approves. The run sits at `awaiting_approval` with `awaiting_phase` set. |
| approval | A user approving a gate. It writes a queued job carrying that user's sealed device credential. |
| retry | An approval that re-runs an earlier phase on just the hosts that failed it (`is_retry`). |
| cursor | `upgrade_run_hosts.state`: how far a host has got. The history is in `upgrade_host_phase_results`. |
| wave | The hosts one job runs on. |
| canary | In a multi-host activate, the first eligible host in request order. It is activated and checked alone before the rest. |
| stop | A job ending before all its hosts ran (`PhaseResult.stopped_by`). |
| kept | A host a stop left at its cursor, so the next approval of that gate runs it. |
| supplier | Whose credential a job carries: the approver, or the submitter for pre-check. |
| sibling | `python -m nethub.sibling`: the process that runs all device work. |
| pin | A confirmed `device_host_keys` row: the SSH host key NetHub accepts for an address. |
| scan | A `host_key_scans` row: fetch an address's host key so a person can confirm it. |
| bundle key | The name a run uses to select a published image (`artifacts.bundle_key`). |
