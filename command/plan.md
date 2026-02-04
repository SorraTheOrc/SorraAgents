---
description: Decompose an epic into features and tasks
tags:
  - workflow
  - plan
  - decomposition
agent: build
subtask: true
---

You are helping the team decompose a Worklog epic (or other Worklog work item) into **features** and **implementation tasks**.

## Inputs

- The supplied <work-item-id> is $1.
  - If no valid <work-item-id> is provided (ids are formatted as '<prefix>-<hash>'), ask the user to provide one.
    -- Optional additional freeform arguments may be provided to guide your work. Freeform arguments are found in the arguments string "$ARGUMENTS" after the <work-item-id> ($1).

## Results and Outputs

- The parent work item ($1) or each of the milestone epics below it are decomposed into child feature work items.
- Idempotence: The command reuses existing child work items and updates or augments previously generated feature work-items instead of creating duplicates.

## Hard requirements

- Provide guidance on how each feature can be delivered as a minimal, end-to-end slice (code, tests, docs, infra, observability).
- Where possible identify existing implementations details that are related to the feature.
- Where possible identify existing features or tasks that can be reused instead of creating duplicates.
- Use an interview style: concise, high-signal questions grouped to a soft-maximum of three per iteration.
- Do not invent requirements, commitments (dates), or owners — propose options and ask the user to confirm.
- Respect ignore boundaries: do not include or quote content from files excluded by `.gitignore` or any OpenCode ignore rules.
- Prefer short multiple-choice suggestions where possible, but always allow freeform responses.
- If the user indicates uncertainty, add clarifying questions rather than guessing.

- Whenever you are recommending next steps you MUST make the first one a progression to the next step in the process defined below, with a summary of what that step involves.

## Note

- This Hard requirements section is populated with the mandatory progression rule above; review the rest of the hard requirements for task-specific constraints.

## Seed context

- Read `docs/` (excluding `docs/dev`), `README.md`, and other high-level files for context.
- Fetch and read the work item details using Worklog CLI: `wl show <work-item-id> --json` and treat the work item description and any referenced artifacts as authoritative seed intent.
- Pay particular attention to any PRD referenced in this work item or any of its parent work items.
- If `wl` is unavailable or the work item cannot be found, fail fast and ask the user to provide a valid <work-item-id> or paste the work item content.
- Prepend a short “Seed Context” block to the interview that includes the fetched work item title, type, current tags, and one-line description.

## Process (must follow)

1. Fetch & summarise (agent responsibility)

- Run `wl show <work-item-id> --json` and summarise the work item in one paragraph: title, type (epic/feature/task), headline, and any existing milestone/plan info.
- Validate that the work item is ready for planning by inspecting its `stage` field:
- Run `wl show <work-item-id> --json` and summarise the work item in one paragraph: title, type (epic/feature/task), headline, and any existing milestone/plan info.
- Validate readiness by examining the work item's `stage` value:
- `milestones_defined` indicates it is ready for planning.
- `plan_complete` indicates this is a request to review the existing plan; follow the steps below but consider previous planning work.
- any other `stage` value suggests the work item is not currently ready for planning — ask the user how to proceed indicating that if the work item is small enough it is OK to proceed.
- Read any PRD linked in the work item or any of its parents to extract key details for later reference.
- Derive 3–6 keywords from the work item title/description to search the repo and work items for related work. Present any likely duplicates or parent/child relationships.

2. Interview

In interview iterations (≤ 3 questions each), gather the minimum information needed to produce an actionable feature plan in which each feature is large enough to be meaningful but small enough to be delivered as an end-to-end slice. For each feature capture:

- Target outcome: what user-visible capability must exist when this epic is “done”?
- Definition of done: what are the pass/fail acceptance checks (a short manual checklist and automated tests if possible)?
- Constraints: performance, compatibility, rollout/feature-flag expectations, or timeline constraints.
- Risky assumptions: identify where a prototype/experiment is needed (fake API, mock UI, spike) and what “success” means.

Keep asking questions until the breakdown into features is clear.

3. Propose feature plan (agent responsibility + user confirmation)

- Produce a draft plan (soft guide: 3–12 features) where each feature includes:
  - **Short Title** (canonical, stable, ≤ 7 words)
  - **Summary** (one sentence)
  - **Acceptance Criteria** (2–6 concise bullets; measurable/testable)
  - **Minimal Implementation** (2–6 bullets; smallest end-to-end slice)
  - **Prototype / Experiment** (optional; include success thresholds)
  - **Dependencies** (other features or explicit external factors)
  - **Deliverables** (artifacts: docs, tests, demo script, telemetry)

- Each of the features should clearly identify how the player experience will be changed by the feature and what acceptance critera are required to validate it.
- Each of the features should clearly identify how the user experience will be changed by the feature and what acceptance criteria are required to validate it.

- Present the draft as a numbered list and ask the user to: accept, edit titles/scopes, reorder, or split/merge features.
- If the user requests changes, iterate until the feature list is approved.

4. Automated review stages (must follow; no human intervention required)

- After the user approves the feature list, run five review iterations. Each review MUST provide a new draft if any changes are recommended and then output exactly: "Finished <Stage Name> review: <brief notes of improvements>"

- General requirements for the automated reviews:
  - Run without human intervention.
  - Each stage runs sequentially in the order listed below.
  - Improvements should be conservative and scoped to the stage.
  - If an automated improvement could change intent (e.g., adding/removing scope, changing ordering that implies different priorities), do NOT apply it automatically; instead record an Open Question and continue.

- Review stages and expected behavior:
  1. Completeness review
  - Purpose: Ensure every feature has all required fields.
  - Actions: Add missing placeholders only when obvious; otherwise add Open Questions.
  2. Sequencing & dependencies review
  - Purpose: Ensure dependencies are coherent and actionable.
  - Actions: Detect cycles, missing prerequisites, or vague dependencies; propose minimal fixes that do not change intent; record uncertainty as Open Questions.
  3. Scope sizing review
  - Purpose: Ensure features are sized as deliverable increments.
  - Actions: Flag features that are too broad/vague or duplicate scope; suggest split/merge candidates as Open Questions.
  4. Acceptance & testability review
  - Purpose: Ensure acceptance criteria are pass/fail and testable.
  - Actions: Tighten criteria wording; add missing negative cases only when clearly implied.
  5. Polish & handoff review
  - Purpose: Make the plan copy-pasteable and easy to execute.
  - Actions: Standardize bullets, tense, and structure; keep titles canonical.

5. Update work items (agent)

- Create child work items for each feature with a parent link to the original work item:
- `wl create "<Short Title>" --description "<Full feature description>" --parent <work-item-id> -t feature --priority P2 --stage idea --validate --json`
- Create dependency edges between feature work items where the plan specifies dependencies:
  - `wl dep add <DependentFeatureId> <PrereqFeatureId>`

- When creating child work items, ensure idempotence:
- If a child work item with the same canonical name already exists, reuse it instead of creating a duplicate.
- Use `wl list --parent <work-item-id> --json` for features.

- Add a comment to the planned work item:
  - `wl comments add $1 "Planning Complete. <Summary of the approved feature list, any open questions that remain>" --actor @your-agent-name --json`
    -- Update the planned work item's stage to indicate planning is complete:
  - `wl update $1 --stage plan_complete --json`

## Traceability & idempotence

- Re-running `/plan <work-item-id>` should not create duplicate child work items or duplicate generated plan blocks in the parent work item.
- If the command makes changes, include a changelog block in the parent work item (labelled "Plan: changelog") summarising actions and timestamps.

## Editing rules & safety

- Preserve author intent; where the agent is uncertain, create an Open Question entry rather than making assumptions.
- Keep changes minimal and conservative.
- Respect `.gitignore` and other ignore rules when scanning files for context.
- **Worklog validation**: when creating `feature` or `task` work items with `--validate`, ensure the description includes a `## Acceptance Criteria` section (the validator rejects missing sections).
- **JSON parsing**: `wl ... --json` output may be either an object or an array; when extracting ids with `jq`, handle both shapes (e.g., `if type=="array" then .[0].id elif type=="object" then .id end`).
- If any automated step fails or is ambiguous, surface an explicit Open Question and pause for human guidance.

## Finishing steps (must do)

-- On the parent work item set the work item's stage to `plan_complete`:
`wl update <work-item-id> --stage plan_complete --json`

- Run `wl sync` to sync work item changes.
- Run `wl show <work-item-id>` (not --json) to show the entire work item.
- End with: "This completes the Plan process for <work-item-id>".
  - On the parent work item set the machine-readable `stage` field to `plan_complete`:
    `wl update <work-item-id> --stage plan_complete --json`
- Run `wl sync` to sync work item changes.
- Run `wl show <work-item-id>` (without `--json`) to display the entire work item.
- End with: "This completes the Plan process for <work-item-id>".

## Examples

- `/plan wl-456`
  - Starts an interview to break epic `wl-456` into feature and task work items.
- `/plan wl-456 MVP first`
  - Same as above, but seeds the interview with the phrase "MVP first".
