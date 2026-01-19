---
description: Create or improve design notes for a beads issue
tags:
  - design
agent: build
---

You are creating or improving the **design notes** for a single Beads issue.

## Argument parsing

- Pattern: If the raw input begins with a slash-command token (a leading token that starts with `/`, e.g., `/design`), strip that token first.
- The first meaningful token after any leading slash-command is available as `$1` (the first argument). `$ARGUMENTS` contains the full arguments string (everything after the leading command token, if present).
- This command expects a single beads id as the first argument. Validate that `$1` is present and that `$2` is empty; if not, ask the user to re-run with a single bead id argument.

Beads context (must do):

- Fetch the issue details using beads CLI: `bd show $1 --json`.
- Use at minimum: `title`, `description`, `acceptance` (if present), and `design` (if present).
- If `bd` is unavailable or the issue cannot be found:
  - Fail fast and ask the user to provide a valid issue id or paste the issue content.

Temp file requirement (must do):

- During the interview/drafting loop, maintain a temporary draft file under `docs/dev/tmp/`.
- Suggested file name: `docs/dev/tmp/design_$1_$(date +%Y%m%d%H%M%S).md`.
- Update this file as the design evolves so the user can inspect the current draft at any time.
- After successfully adding the final design to the issue via `bd update --design`, delete the temporary file.

Process (must follow):

1. Seed context

   - Start by presenting a short “Seed Context” block derived from `bd show $1 --json`:
     - Title
     - Description
     - Acceptance criteria (if present)
     - Existing design notes (if present)

2. Decide: create vs improve

   - If `design` is empty/missing: CREATE a design.
   - If `design` is present: IMPROVE the existing design.
     - Treat the existing design as authoritative baseline.
     - Do not discard it; refine, correct, and fill gaps.

3. Interview loop

   - Ask concise, high-signal questions in iterations.
   - Soft-maximum of **three questions per iteration**.
   - Prefer multiple-choice options when it reduces ambiguity.
   - If the issue is underspecified, keep asking until the design is actionable.
   - When improving an existing design, prioritize questions that:
     - Resolve open questions / missing edge cases
     - Clarify data and state transitions
     - Identify integration points, failure modes, and rollback strategy
     - Make acceptance criteria testable

4. Draft design (write to temp file continuously)

   - Maintain a single evolving design document in the temp file.
   - Keep it practical and implementation-oriented.
   - Use the outline below (headings exactly) unless the existing design already uses a different structure.

Design outline (use headings exactly when creating a new design):

# Design

## Summary

## Goals

## Non-goals

## Architecture

## Data Model

## API / CLI Changes

## UX / TUI Changes

## Error Handling

## Observability

## Security & Privacy

## Rollout / Migration

## Testing Plan

## Open Questions

5. Write back to the issue

   - When the design is ready, update the Beads issue:
     - `bd update $1 --design "$(cat \"$TMP_FILE\")" --json`
   - If the design already existed, the new design should be a clear improvement (more precise, fewer unknowns).

6. Cleanup

   - Delete the temp file after the issue update succeeds:
     - `rm -f "$TMP_FILE"`

Notes:

- Do not invent repo-specific constraints beyond what the issue and user provide.
- Keep the design aligned with the issue’s acceptance criteria.
- If you discover missing requirements or scope creep, capture them in **Open Questions** (and ask the user).
