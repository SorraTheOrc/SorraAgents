---
description: Create or edit a PRD through interview
agent: build
subtask: true
---

You are helping create or update a Product Requirements Document (PRD) for an arbitrary product, feature, or tool.

## Inputs

- The supplied <work-item-id> is $1.
  - If no valid <work-item-id> is provided (ids are formatted as '<prefix>-<hash>'), ask the user to provide one.
- Optional additional freeform arguments may be provided to guide your work. Freeform arguments are found in the arguments string "$ARGUMENTS" after the <work-item-id> ($1).

## Results and Outputs

- PRD written to the work item $1.
- Idempotence: Running this command again updates the PRD in the work item description, rather than creating duplicate work items or PRDs

## Hard requirements

- Whenever you are recommending next steps you MUST make the first one a progression to the next step in the process defined below, with a summary of what that step involves.


## Hard requirements

- Be environment-agnostic: do not assume tech stack, hosting, repo layout, release process, or tooling.
- Use an interview style: concise, high-signal questions grouped to a soft-maximum of three per iteration.
- Do not invent integrations or constraints; if unknown, ask.
- Respect ignore boundaries: do not include or quote content from files excluded by `.gitignore` or any OpenCode ignore rules.
- Prefer short multiple-choice suggestions where possible, but always allow freeform responses.
- If the user indicates uncertainty at any point, add clarifying questions rather than guessing.

- Whenever you are recommending next steps you MUST make the first one a progression to the next step in the process defined below, with a summary of what that step involves.

## Behavior

The command implements the procedural workflow below. Each numbered step is part of the canonical execution path; substeps describe concrete checks or commands that implementors or automation should run.

When a step is gated by user approval, do not proceed until the user approves the output of that step. Do not provide alternative paths unless explicitly requested by the user.

## Process (must follow)

0. Gather context (agent responsibility)

- Mark the work item as in progress using `wl update <work-item-id> --status in_progress --json`
- Read `docs/` (excluding `docs/dev`), `README.md`, and other high-level files for product context.
- Fetch and read the work item details using Worklog CLI: `wl show <work-item-id> --json`.
- Fetch and read any documents or work items referenced in the work item external references.
- If a PRD already exists at the path indicated in the work item then assume we are updating an existing PRD.
- If `wl` is unavailable or the work item cannot be found, fail fast and ask the user to provide a valid work item id or paste the work item content.
- Prepend a short “Seed Context” block to the interview that includes the fetched details and treat it as authoritative initial intent while still asking clarifying questions.

1. Interview

- In interview iterations (≤ 3 questions each) build a full understanding of the work, offering templates/examples informed by repo context where possible.
- If anything is ambiguous, ask for clarification rather than guessing.
- Keep asking the user questions until all core PRD information is captured and clarifications are made.
- Once you feel you are able to do so, write a draft PRD using the template below and including noting any areas that could benefit from further expansion.

3. Draft PRD

- Store the draft PRD at `docs/prd/PRD_<work_item_title>_(<work_item_id>).md`
- Present the draft to the user and ask the user to review it and provide feedback.
- The user may:
  - Respond with edits or clarifications, in which case you must incorporate them, and go back to the previous step of drafting the intake brief,
  - Ask you to continue asking questions, in which case you must continue the interview to gather more information, or
  - Approve the current draft, in which case you must proceed to the next step.

4. Automated review stages (must follow; no human intervention required)

After the user approves the draft PRD, run five review iterations. Each review MUST provide a new draft if any changes are recommended and then print a clear "finished" message as follows:

- "Finished <Stage Name> review: <brief notes of improvements>"
- If no improvements were made: "Finished <Stage Name> review: no changes needed"

- General requirements for the automated reviews:
  - Run without human intervention.
  - Each stage runs sequentially in the order listed below.
  - When the stage completes the command MUST output exactly: "Finished <Stage Name> review: <brief notes of improvements>"
    - If no improvements were made, the brief notes MUST state: "no changes needed".
  - Improvements should be conservative and clearly scoped to the stage. If an automated improvement could change intent, the reviewer should avoid making that change and instead record an Open Question in the PRD.

- Review stages and expected behavior:
  1. Structural review
     - Purpose: Validate the PRD follows the required outline and check for missing or mis-ordered sections.
     - Actions: Ensure headings appear exactly as specified in the PRD outline; detect missing sections; propose and apply minimal reordering or section insertion to satisfy the outline. If structural changes may alter intent, add an Open Question instead of applying them.
  2. Clarity & language review
     - Purpose: Improve readability, clarity, and grammar without changing meaning.
     - Actions: Apply non-destructive rewrites (shorten long sentences, fix grammar, clarify ambiguous phrasing). Do NOT change intent or add new functional requirements.
  3. Technical consistency review
     - Purpose: Check requirements and technical notes for internal consistency with gathered context.
     - Actions: Detect contradictions between Requirements, Users, and Release & Operations sections; where safe, adjust wording to remove contradictions (e.g., normalize terminology). Record unresolved inconsistencies as Open Questions.
  4. Security & compliance review
     - Purpose: Surface obvious security, privacy, and compliance concerns and ensure the PRD includes at least note-level mitigations where applicable.
     - Actions: Scan for missing security/privacy considerations in relevant sections and add short mitigation notes (labelled "Security note:" or "Privacy note:"). Do not invent security requirements beyond conservative, informational notes.
  5. Lint, style & polish
     - Purpose: Run automated formatting and linting (including markdown lint) and apply safe autofixes.
     - Actions: Run `remark` with autofix enabled, apply whitespace/formatting fixes, ensure consistent bulleting and code block formatting. Summarize what lint fixes were applied.

- Failure handling:
  - If any automated review encounters an error it cannot safely recover from, the command MUST stop and surface a clear error message indicating which stage failed and why. Do not attempt destructive fixes in that case; instead record an Open Question in the PRD and abort the remaining automated stages.

- Human handoff:
  - Although the reviews are automated, the output messages and changelog entries MUST be sufficient for a human reviewer to understand what changed and why.

5. PRD sign-off (human step)

- Present the final PRD draft to the user for approval.
- If this was an update to an existing PRD, highlight the changes made during this process.
- The user may:
  - Request further changes, in which case you must return to the interview step to gather more information and then re-run the automated reviews.
  - Approve the final draft, in which case you must proceed to the finishing steps.

6. Finalizing the PRD

- Save the final PRD markdown to the appropriate path:
- Record a the existince of the PRD in the Worklog work item by adding a comment: "The PRD has been created/updated at: <path>" and linking to the file using an external reference.
- run `wl sync` to sync work item changes.
- Raise a PR in the repo with the new/updated PRD file
- Ask the user to review the PR and merge it when ready.
- Monior the PR until it is merged, providing updates to the user as needed.

7. Next steps

- After the PR is merged, ask the user whether they would like to cleanup the repository and move back to main branch (recommended).
  - If the user agrees, run the cleanup command: `/cleanup`
- Once cleanup is complete tell the user: "The next step is to decompose the PRD into master epics using the command: `/milestones <work-item-id>`" and offer to run it for them.
  - If the user agrees, run the command with the same work item id used to create/update the PRD.

## Editing rules (when updating an existing PRD)

- Preserve the document structure and intent; only change what is necessary.
- If you are making significant structural changes, call them out and ask for confirmation.
- Update the Open Questions section based on what is newly resolved vs still unknown.
- Before signing-off run a markdown lint process using `remark` with autofix enabled.

## Finishing steps (must do)

-- Set the work item stage to indicate PRD completion:
  `wl update <work-item-id> --stage prd_complete --json`
- Run `wl sync` to sync work item changes.
- Run `wl show <work-item-id>` (not --json) to show the entire work item.
- End with: "This completes the PRD process for <work-item-id>".

## PRD outline (use headings exactly):

# Product Requirements Document

## Introduction

### One-liner

A concise one-sentence summary of the product or feature.

### Problem statement

Describe the problem the product or feature is intended to solve.

### Goals

List measurable goals the project should achieve.

### Non-goals

Explicitly state what is out of scope for this effort.

## Users

### Primary users

Describe the primary user personas and their needs.

### Secondary users (optional)

Describe any secondary users or stakeholders.

### Key user journeys

Outline major user journeys and success criteria for each.

## Requirements

### Functional requirements (MVP)

Bullet the minimum functional requirements needed for an initial release.

### Non-functional requirements

Specify performance, reliability, scalability, and accessibility targets.

### Integrations

List external systems, APIs, and data sources the product must integrate with.

### Security & privacy

Add security notes and privacy considerations (see Security note:, Privacy note: where applicable).

## Release & Operations

### Rollout plan

Describe rollout stages, canary strategy, and target audiences.

### Quality gates / definition of done

State acceptance criteria, tests, and release gates required to ship.

### Risks & mitigations

List key risks and corresponding mitigations or contingency plans.

## Open Questions

List remaining unknowns as clear, answerable questions to resolve during intake.
