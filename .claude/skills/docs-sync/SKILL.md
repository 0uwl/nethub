---
name: docs-sync
description: Keep docs/ and CLAUDE.md true to the code. Use at the end of any change that alters behaviour a docs/ rule describes, adds or removes a feature, command, env var, table, route or dependency, or changes a rule about what must not be built. Also use when asked to update, audit or check the docs, or when something in docs/ or CLAUDE.md turns out to be wrong.
---

# Keeping docs/ true

`docs/` describes NetHub as it is now, one file per area of the code.
`CLAUDE.md` is a short router into it. Nothing but `tests/test_docs.py`
enforces the link between docs and code, and that test checks only
citations, not meaning. A stale rule is worse than a missing one, because
the next session will act on it confidently.

## When

In the same commit as the change, never as a follow-up. Triggers:

- behaviour a `docs/` rule states changes, or a rule's pinning test is
  renamed, moved or deleted;
- a feature, route, table, column, command, env var or dependency is added
  or removed;
- something listed in `docs/future.md` gets built (move it out);
- a known gap is closed, or a new one is found;
- a hard rule in `CLAUDE.md` changes.

## How

1. **Load the `documentation-standards:hads` skill** before editing any
   `docs/` file. `docs/index.md` §2 is the binding format: HADS without a
   version line or changelog, one H3 per rule headed by its slug,
   `[SPEC]` at most two sentences of prose, and a `Pinned by:` line.
2. **Find the right file** with the table in `docs/index.md` §1. Edit the
   rule in place. Never append a correction beside a stale rule.
3. **Check every claim against the code**, not against memory or the old
   text. If you cannot confirm something, mark it `[?]` or leave it out.
4. **Pin new rules.** Name the pytest node ids that prove the rule, or
   write `Pinned by: none` and list it under that file's Known gaps.
5. **Delete, don't mark obsolete.** A removed feature loses its rules.
   No history, no "used to", no dates: git keeps that.
6. **Update `CLAUDE.md`** only if a hard rule, a command, the status or
   the routing table changed. Keep it a router.
7. **Code comments** cite rules as `docs/<file>.md [slug]`. When you rename
   a slug, grep for it (`grep -rn '\[old-slug\]'`) and update every cite.

## Verify

- Run `pytest tests/test_docs.py`. It fails on a cited test, file or rule
  that does not exist, a rule without `Pinned by:`, a duplicate slug, or
  a `WS-`/design-doc reference in the code.
- For a large change, have an agent that has not seen the old text check
  the edited file against the code and report CONFIRMED, WRONG, IMPRECISE
  or UNVERIFIABLE per rule. Have it report only, then confirm its main
  findings yourself before editing.
- Report anything you left inconsistent rather than leaving it silently.
