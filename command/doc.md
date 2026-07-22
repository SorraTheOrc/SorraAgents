---
description: Create or improve developer + user documentation for a Worklog work item
tags:
  - workflow
  - docs
  - documentation
agent: scribbler
subtask: true
---

Create or improve **documentation** for a single feature. Runs **before** tests or implementation code, so docs must be high-detail — a best-effort specification that may evolve.

Two doc sets:

- **User docs**: `docs/` (excluding `docs/dev/`)
- **Developer docs**: `docs/dev/`

Prefer updating existing docs over creating new ones. Create new pages only when no suitable home exists.

Worklog context (must do):

- Mark the **Docs:** task in_progress: `wl update <docs-id> --status in_progress`
- Fetch work item details: `wl show <parent-id> --json`
- Treat `title`, `description`, `acceptance` (if present), and linked artifacts as authoritative seed intent.

Hard requirements

- Docs authored **before** tests/implementation with detailed behavior, flows, edge cases, and examples.
- Call out assumptions and unknowns as **Open Questions**.
- Separate audiences:
  - **User docs**: how the feature fits into the product, how to use it. Omit if no user-visible component.
  - **Developer docs**: how it works, how to extend, how to troubleshoot.
- Prefer editing existing docs over creating duplicates. Avoid doc sprawl.
- Interview style when needed: concise, high-signal questions, max **three per iteration**.
- Do not invent commitments (dates) or owners.
- Respect `.gitignore` and agent framework ignore rules.

Temp draft requirement (must do)

- Maintain an evolving draft under `docs/dev/tmp/doc_$1_$(date +%Y%m%d%H%M%S).md` containing:
  - Seed context, proposed doc changes (file list), draft content blocks, Open Questions
- Do not delete the temp file automatically — it is useful during implementation when docs are revised.

## Results and Outputs

- A set of created/updated documentation files and a short headline summary
- Idempotence: existing pages are edited in-place on reruns; avoids creating duplicate content

## Hard requirements

- Next-step recommendations MUST always progress to the next step in the process below, with a summary of what that step involves.

Seed context

- Read `docs/` (including `docs/dev`), `README.md`, and other high-level files for product context.
- Fetch and read the work item: `wl show $1 --json`
- If the work item is a **Docs:** task, locate its parent (feature) and its PRD/design/plan as additional authoritative context.
- Present a short "Seed Context" block with: work item title, type, one-line description, acceptance criteria summary (if present), and linked references discovered.

Process (must follow)

1. **Inventory the doc set** (agent responsibility)
   - Identify **target readers** and required doc types:
     - **User docs**: install/setup (if applicable), purpose within the product, usage, examples, limitations, common errors
     - **Developer docs**: architecture overview, data model/state machine, extension points, configuration, debugging/troubleshooting, test strategy expectations
   - Propose minimal edits — prefer updating existing pages; create new only when no suitable home exists.
   - Propose exact file list split into `docs/…` (user) and `docs/dev/…` (developer).

2. **Interview** (only as needed)
   Ask questions only if the work item/PRD does not fully specify documentation needs. Focus on: primary user journeys, inputs/outputs, state changes, error messages, feature flags, compatibility constraints, non-functional expectations (latency, reliability). Keep asking until actionable.

3. **Draft the documentation** (agent responsibility)
   - Write content in the temp draft file first.
   - Include: step-by-step flows, clear definitions and terminology, examples (commands, config snippets, sample I/O), failure modes and recovery, troubleshooting guidance, **Open Questions** for anything unknown.

## User docs outline (recommended)

| Section | Description |
|---------|-------------|
| `# <Feature / Product Name>` | Title |
| `## Overview` | What and why |
| `## Who This Is For` | Target audience |
| `## Quick Start` | Fast setup/usage |
| `## How It Works (User View)` | Conceptual model |
| `## Usage Examples` | Common scenarios |
| `## Error Messages & Fixes` | Symptom → cause → fix |
| `## Limitations` | Known gaps |
| `## FAQ` | Frequent questions |
| `## Open Questions` | Unknowns |

## Developer docs outline (recommended)

| Section | Description |
|---------|-------------|
| `# <Component Name> (Developer)` | Title |
| `## Summary` | One-paragraph overview |
| `## Architecture` | High-level structure |
| `## Data Model / State` | Schema, state machine |
| `## Key Algorithms / Rules` | Core logic |
| `## Configuration` | Env vars, flags |
| `## Integration Points` | APIs, services |
| `## Failure Handling` | Errors, recovery |
| `## Observability` | Logging, metrics |
| `## Testing Notes` | Test strategy |
| `## Rollout / Migration` | Deploy steps |
| `## Open Questions` | Unknowns |

1. **User review** (must do)
   - Present proposed file list and draft content blocks to the user.
   - For each file: file path, summary of changes, draft content block (markdown).
   - Iterate until the user approves all changes.

2. **Automated review stages** (must follow; no human intervention)
   Run four reviews, outputting: `"Finished <Stage> review: <notes>"` or `"... no changes needed"`.

   i) **Completeness** — Ensure required sections exist; user docs answer what/how/errors/limits; dev docs answer how it works/extension/troubleshooting/testing.
   ii) **Consistency** — Terminology matches the work item/PRD; user and dev docs don't contradict; assumptions labeled.
   iii) **Actionability** — Steps runnable, examples specific, troubleshooting maps symptom → cause → fix.
   iv) **Markdown/style** — Fix headings, formatting, code fences; keep consistent with existing docs.

3. **Apply docs changes** (agent responsibility)
   - Create or update docs. Cross-link appropriately between user and dev docs.
   - Link from existing index pages (`docs/`, `docs/dev/`) when present.

4. **Write back to the work item** (agent responsibility)
   - Update the docs task description with a "Documentation" block listing updated/created files, contents, Open Questions, and a small changelog with timestamps.

5. **Finishing steps** (must do)
   - `wl update $1 --stage docs_drafted --json`
   - Optionally set parent feature's stage; prefer `--stage` for machine-readable state transitions.
   - `wl sync`
   - `wl show $1` (not --json)
   - End with: "This completes the Documentation process for $1".

## Notes

- Do not introduce new features in the docs that are not in the work item/PRD.
