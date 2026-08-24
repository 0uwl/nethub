---
name: design-doc-sync
description: Keep CLAUDE.md in sync with the actual state of this project. Use at the end of any change that alters what a future session would need to know - design decisions or sections in design-document.md, commands, dependencies, directory layout, schema, or a rule about what must not be built. Also use when asked to update, refresh, audit, or check CLAUDE.md, or when something in CLAUDE.md turns out to be stale, wrong, or contradicted by the code.
---

# Keeping CLAUDE.md in sync

CLAUDE.md is the only project context a fresh session gets for free. It is
derived from two sources that drift apart on their own:

1. `design-document.md` — describing the target architecture.
2. The repo — which implements very little of it so far.

Nothing enforces the derivation, so it goes stale silently, and a stale
CLAUDE.md is worse than a thin one: a future session will act on it
confidently and be wrong.

## When to sync

Sync in the same pass as the change, not as a follow-up task. Triggers:

- A design document section is added, rewritten, or has a decision reversed.
- A settled decision changes — especially one phrased as "never do X".
- A command, env var, or dependency changes.
- A new top-level file or directory appears whose purpose isn't obvious.
- Something in CLAUDE.md is discovered to be wrong while doing other work.
- A component described as "not yet implemented" becomes implemented.

That last one is the easiest to miss and the most damaging, because the
"Project status" section is what stops a session from assuming the design
document describes real code.

## What belongs in it

Context a session cannot cheaply derive by reading the repo:

- **What exists vs. what is only designed.** The single most valuable
  thing in this file. Name the specific files and roles that are
  referenced but absent.
- **Invariants and their reasoning.** Not just "the table wins" but why,
  so a future change can tell whether it's violating the rule or
  legitimately revisiting it.
- **Hard rules** — decisions that look like missing features. Anything a
  reasonable person would otherwise "fix", with the argument attached.
- **Traps.** Places where two correct descriptions coexist (committed code
  vs. target design), or where two similar things must not be conflated.
- **Commands** that aren't discoverable from a standard file.

## What does not belong

- Restating design document prose at length. Summarize and cite the section.
- Anything derivable from a quick read — file structure, function names,
  what a library does.
- Git history, past bugs, or narrative about how a decision was reached.
- Aspirational content. This file describes what is true now, including
  the truth that most of the design is unbuilt.

## How to do it

1. **Diff the sources.** Read what changed in `design-document.md` and check each
   claim CLAUDE.md makes against it. Section numbers move — verify every
   cross-reference still points at the section it names.
2. **Check claims against the repo, not against memory.** Before writing
   that something exists, confirm it. Before leaving a "not present in
   this repo" note in place, confirm it is still absent.
3. **Prefer editing to appending.** A stale paragraph left beside a
   correct one is a contradiction, not a history. Rewrite it.
4. **Keep the register.** Match the file's existing voice; it is written
   in the same style as the design document.
5. **Report what you left inconsistent.** If a change implies updates
   beyond the current scope, say so explicitly rather than silently
   leaving a half-synced file.

## Verifying

There is no test for this. Read the result start to finish and ask
whether a session with no other context could act on it without being
misled. Specifically check that every "not yet implemented" note is still
accurate and every design document section reference resolves.
