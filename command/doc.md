---
description: Create or improve developer + user documentation for a Worklog work item
tags:
  - workflow
  - docs
  - documentation
agent: scribbler
subtask: true
---

You are creating and/or improving **documentation** for a single feature detailed in a Worklog work item.
This command runs **before any tests or implementation code is written**, so the documentation must be
**high-detail** and written as a best-effort specification that may evolve later.

This command produces two doc sets:

- **User documentation**: stored under `docs/` (excluding `docs/dev/`).
- **Developer documentation**: stored under `docs/dev/`.

Where possible, update and improve existing documentation rather than creating new files. Create new docs only when needed.


Worklog context (must do):

- Mark the **Docs:** task as in-progress `wl update <docs-id> --status in_progress`
- Fetch the work item details using Worklog CLI: `wl show <parent-id> --json`.
- Treat `title`, `description`, `acceptance` (if present), and any linked artifacts as authoritative seed intent.

Hard requirements

- Documentation is authored **before** tests and implementation.
  - It must include detailed behavior, flows, edge cases, and examples.
  - It should explicitly call out assumptions and unknowns as **Open Questions**.
- Separate audiences:
  - **User docs** explains how the feature fits into the overal product and how to use it. If the feature has no user-visible component (e.g., backend service), user docs can be omitted.
  - **Developer docs** explain how it works, how to extend it, and how to troubleshoot it.
- Prefer updating existing docs:
  - If a doc already covers the area, edit it and add a new section rather than creating duplicates.
  - Avoid doc sprawl; do not create multiple overlapping pages for the same topic.
- Use an interview style when needed:
  - Ask concise, high-signal questions in iterations.
  - Soft-maximum of **three questions per iteration**.
- Do not invent commitments (dates) or owners.
- Respect ignore boundaries: do not include or quote content from files excluded by `.gitignore` or any OpenCode ignore rules.

Temp draft requirement (must do)

- Maintain an evolving draft under `docs/dev/tmp/` while authoring.
- Suggested filename: `docs/dev/tmp/doc_$1_$(date +%Y%m%d%H%M%S).md`.
- The temp file should contain:
  - Seed context
  - Proposed doc changes (file list)
  - Draft content blocks (ready to paste into final docs)
  - Open Questions
- Do not delete the temp file automatically. It is useful during implementation when docs are revised by later workflow commands.

## Results and Outputs

- A set of created/updated documentation files.
- A short headline summarising what was documented.
- Idempotence: Existing docs pages are edited in-place when possible; reruns reuse existing pages and update content instead of creating new, significantly duplicating content.

## Hard requirements

- Whenever you are recommending next steps you MUST make the first one a progression to the next step in the process defined below, with a summary of what that step involves.

Seed context

- Read `docs/` (including `docs/dev`), `README.md`, and other high-level files for product context.
- Fetch and read the work item details using `wl show $1 --json`.
- If the work item is a **Docs:** task, locate its parent (feature) and its PRD/design/plan and treat those as additional authoritative context.
- Start the command by presenting a short “Seed Context” block:
  - Work item title
  - Work item type
  - One-line description
  - Acceptance criteria summary (if present)
  - Linked references discovered (PRD/design/plan/doc files)

Process (must follow)

1. Inventory the doc set (agent responsibility)

- Identify the **target readers** and doc types required:
  - **User docs**: how to install/setup (if applicable), intended purpose within the product, how to use, examples, limitations, common errors.
  - **Developer docs**: architecture overview, data model/state machine, extension points, configuration, debugging/troubleshooting, test strategy expectations.
- Propose a minimal set of doc edits:
  - Prefer: update an existing page in `docs/` and/or `docs/dev/`.
  - Only create a new page when no suitable home exists.
- Propose the exact file list to update/create, split into:
  - `docs/…` (user)
  - `docs/dev/…` (developer)

2. Interview (only as needed)

Ask questions only if the work item/PRD does not fully specify what must be documented. Focus on:

- Primary user journeys (happy path) and the most important edge cases.
- Inputs/outputs, user-visible state changes, and error messages.
- Feature flags/rollout behavior (if any).
- Compatibility constraints (platforms, versions, data migration).
- Any non-functional expectations a user would perceive (latency, reliability).

Keep asking until the documentation is actionable.

3. Draft the documentation (agent responsibility)

- Write the new/updated documentation content in the temp draft file first.
- The docs must read as if they are the best specification available right now.
- Include:
  - Step-by-step flows
  - Clear definitions and terminology
  - Examples (commands, config snippets, sample inputs/outputs)
  - Failure modes and how the user can recover
  - Troubleshooting guidance
  - “Open Questions” for anything unknown

User docs outline (recommended)

Use this outline when creating a new user-facing doc page:

# <Feature / Product Name>

## Overview

## Who This Is For

## Quick Start

## How It Works (User View)

## Usage Examples

## Error Messages & Fixes

## Limitations

## FAQ

## Open Questions

Developer docs outline (recommended)

Use this outline when creating a new developer-facing doc page:

# <Feature / Component Name> (Developer)

## Summary

## Architecture

## Data Model / State

## Key Algorithms / Rules

## Configuration

## Integration Points

## Failure Handling

## Observability

## Testing Notes

## Rollout / Migration

## Open Questions

4. User review (must do)

- Present the proposed file list and draft content blocks to the user for review.
- For each file, show:
  - File path
  - Summary of changes (new page or updated sections)
  - Draft content block (markdown)
- Ask for feedback and requested changes.
- Iterate drafting and review until the user approves all changes.

5. Automated review stages (must follow; no human intervention required)

Once the user approves the draft, run four review iterations (see below). Each review MAY make changes to the draft and MUST output exactly:

- "Finished <Stage Name> review: <brief notes of improvements>"
  - If no improvements were made: "Finished <Stage Name> review: no changes needed"

Review stages:

    i) Completeness review
        - Ensure required sections exist for the relevant doc type.
        - Ensure user docs answer: what it is, how to use, examples, errors, limitations.
        - Ensure dev docs answer: how it works, extension points, troubleshooting, testing notes.

    ii) Consistency review
      - Ensure terminology matches the work item/PRD.
        - Ensure user and dev docs do not contradict each other.
        - Ensure any assumptions are clearly labeled.

    iii) Actionability review
        - Ensure steps are runnable and examples are specific.
        - Ensure troubleshooting guidance is concrete and maps symptom → cause → fix.

    iv) Markdown/style review
        - Fix headings, formatting, code fences.
        - Keep formatting consistent with existing docs.

5. Apply docs changes to the repo (agent responsibility)

- Create new docs/update existing docs.
- Ensure all docs in the repo cross-link appropriately (from user doc to user doc and from developer doc to user doc or developer doc).
- If there is an obvious existing index page in `docs/` and/or `docs/dev/`, add a link to the new page.

6. Write back to the work item (agent responsibility)

- Update the docs task work item (`$1`) description by adding or updating a well-marked block titled "Documentation" with:
  - The list of updated/created files
  - What each file contains
  - Open Questions
  - A small changelog with timestamps

7. Finishing steps (must do)

- Set the docs task work item's stage to indicate docs drafting is complete:
  `wl update $1 --stage docs_drafted --json` (leave other fields intact).
- Optionally set the parent feature's stage or add a human-readable label if desired; prefer using `--stage` for machine-readable state transitions.
- Run `wl sync` to sync work item changes.
- Run `wl show $1` (not --json) to show the entire work item.
- End with: "This completes the Documentation process for $1".

Notes

- Do not introduce new features in the docs that are not in the work item/PRD.
