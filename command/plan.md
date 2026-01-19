---
description: Decompose an epic into features and tasks
tags:
  - workflow
  - plan
  - decomposition
agent: build
---

You are helping the team decompose a Beads epic (or other Beads issue) into **features** and **implementation tasks**.

## Quick inputs

  - The beads issue id is $1.
    - If no bead id is provided, ask the user to provide one.
  - Optional additional freeform arguments will be used to guide the plan. Freeform arguments are found in the entire arguments string: "$ARGUMENTS".

## Argument parsing

- Pattern: If the raw input begins with a slash-command token (a leading token that starts with `/`, e.g., `/plan`), strip that token first.
- The first meaningful token after any leading slash-command is available as `$1` (the first argument). `$ARGUMENTS` contains the full arguments string (everything after the leading command token, if present).
- This command expects a single beads id as the first argument. Validate that `$1` is present and that `$2` is empty; if not, ask the user to re-run with a single bead id argument.

## Hard requirements

- Provide guidance on how each feature can be delivered as a minimal, end-to-end slice (code, tests, docs, infra, observability).
- Where possible identify existing implementations details that are related to the feature.
- Where possible identify existing features or tasks that can be reused instead of creating duplicates.
- Use an interview style: concise, high-signal questions grouped to a soft-maximum of three per iteration.
- Do not invent requirements, commitments (dates), or owners — propose options and ask the user to confirm.
- Respect ignore boundaries: do not include or quote content from files excluded by `.gitignore` or any OpenCode ignore rules.
- Prefer short multiple-choice suggestions where possible, but always allow freeform responses.
- If the user indicates uncertainty, add clarifying questions rather than guessing.

## Seed context

- Read `docs/` (excluding `docs/dev`), `README.md`, and other high-level files for context.
- Fetch and read the bead details using beads CLI: `bd show <beadId> --json` and treat the bead description and any referenced artifacts as authoritative seed intent.
- Pay particular attention to any PRD referenced in this bead or any of its parent beads.
- If `bd` is unavailable or the issue cannot be found, fail fast and ask the user to provide a valid bead id or paste the bead content.
- Prepend a short “Seed Context” block to the interview that includes the fetched bead title, type, current labels, and one-line description.

## Process (must follow)

1) Fetch & summarise (agent responsibility)

- Run `bd show <beadId> --json` and summarise the bead in one paragraph: title, type (epic/feature/task), headline, and any existing milestone/plan info.
- Read any PRD linked in the bead or any of its parents to extract key details for later reference.
- Derive 3–6 keywords from the bead title/description to search the repo and beads for related work. Present any likely duplicates or parent/child relationships.

2) Interview

In interview iterations (≤ 3 questions each), gather the minimum information needed to produce an actionable feature plan in which each feature is large enough to be meaningful but small enough to be delivered as an end-to-end slice. For each feature capture:

- Target outcome: what user-visible capability must exist when this epic is “done”?
- Definition of done: what are the pass/fail acceptance checks (a short manual checklist and automated tests if possible)?
- Constraints: performance, compatibility, rollout/feature-flag expectations, or timeline constraints.
- Risky assumptions: identify where a prototype/experiment is needed (fake API, mock UI, spike) and what “success” means.

Keep asking questions until the breakdown into features is clear.

3) Propose feature plan (agent responsibility + user confirmation)

- Produce a draft plan (soft guide: 3–12 features) where each feature includes:

  - **Short Title** (canonical, stable, ≤ 7 words)
  - **Summary** (one sentence)
  - **Acceptance Criteria** (2–6 concise bullets; measurable/testable)
  - **Minimal Implementation** (2–6 bullets; smallest end-to-end slice)
  - **Prototype / Experiment** (optional; include success thresholds)
  - **Dependencies** (other features or explicit external factors)
  - **Deliverables** (artifacts: docs, tests, demo script, telemetry)
  - **Tasks to create** (at least: implementation, tests, docs; infra/ops if applicable)

- Each of the features should clearly identify how the player experience will be changed by the feature and what acceptance critera are required to validate it.

- Present the draft as a numbered list and ask the user to: accept, edit titles/scopes, reorder, or split/merge features.
- If the user requests changes, iterate until the feature list is approved.

4) Automated review stages (must follow; no human intervention required)

- After the user approves the feature list, run five review iterations. Each review MUST provide a new draft if any changes are recommended and then output exactly: "Finished <Stage Name> review: <brief notes of improvements>"

- General requirements for the automated reviews:
  - Run without human intervention.
  - Each stage runs sequentially in the order listed below.
  - Improvements should be conservative and scoped to the stage.
  - If an automated improvement could change intent (e.g., adding/removing scope, changing ordering that implies different priorities), do NOT apply it automatically; instead record an Open Question and continue.

- Review stages and expected behavior:

  1) Completeness review
    - Purpose: Ensure every feature has all required fields.
    - Actions: Add missing placeholders only when obvious; otherwise add Open Questions.

  2) Sequencing & dependencies review
    - Purpose: Ensure dependencies are coherent and actionable.
    - Actions: Detect cycles, missing prerequisites, or vague dependencies; propose minimal fixes that do not change intent; record uncertainty as Open Questions.

  3) Scope sizing review
    - Purpose: Ensure features are sized as deliverable increments.
    - Actions: Flag features that are too broad/vague or duplicate scope; suggest split/merge candidates as Open Questions.

  4) Acceptance & testability review
    - Purpose: Ensure acceptance criteria are pass/fail and testable.
    - Actions: Tighten criteria wording; add missing negative cases only when clearly implied.

  5) Polish & handoff review
    - Purpose: Make the plan copy-pasteable and easy to execute.
    - Actions: Standardize bullets, tense, and structure; keep titles canonical.

5) Create beads (agent)

- Create child beads for each feature with a parent link to the original bead:
  - `bd create "<Short Title>" --description "<Full feature description>" --parent <beadId> -t feature --json --labels "feature" --priority P2 --validate`

- For each created/reused feature bead, create implementation tasks as children (minimum set):
  - `bd create "Docs: <Short Title>" --description "<Docs task notes>" --parent <featureBeadId> -t task --json --labels "docs" --priority P2 --assignee Scribbler --validate`
  - `bd create "Tests: <Short Title>" --description "<Test task notes>" --parent <featureBeadId> -t task --json --labels "test" --priority P1 --assignee Probe --validate`
  - `bd create "Implement: <Short Title>" --description "<Implementation task notes>" --parent <featureBeadId> -t task --json --labels "task" --priority P1 --assignee Patch --validate`

  Note: These child beads include default assignees following repository conventions (Docs → Scribbler, Tests → Probe, Implementation → Patch). If you need a different owner, update the bead after creation with `bd update`.
  - Add optional tasks (only if needed): Infra/Ops, UX, Security review.

- Create dependency edges between feature beads where the plan specifies dependencies:
  - `bd dep add <DependentFeatureId> <PrereqFeatureId>`

- When creating child beads, ensure idempotence:
  - If a child bead with the same canonical name already exists, reuse it instead of creating a duplicate.
  - Use `bd list --parent <beadId> --json` for features, and `bd list --parent <featureBeadId> --json` for tasks.

- Update the parent bead description to add or update a "Plan" section with:
  - The approved feature list
  - The created/reused feature bead ids
  - Any Open Questions

- When updating the parent bead, append or replace only a well-marked "Plan" block; if a previous generated block exists, replace it rather than appending.

## Traceability & idempotence

- Re-running `/plan <bd-id>` should not create duplicate child beads or duplicate generated plan blocks in the parent bead.
- If the command makes changes, include a changelog block in the parent bead (labelled "Plan: changelog") summarising actions and timestamps.

## Editing rules & safety

- Preserve author intent; where the agent is uncertain, create an Open Question entry rather than making assumptions.
- Keep changes minimal and conservative.
- Respect `.gitignore` and other ignore rules when scanning files for context.
- **Beads validation**: when creating `feature` or `task` beads with `--validate`, ensure the description includes a `## Acceptance Criteria` section (the validator rejects missing sections).
- **JSON parsing**: `bd ... --json` output may be either an object or an array; when extracting ids with `jq`, handle both shapes (e.g., `if type=="array" then .[0].id elif type=="object" then .id end`).
- If any automated step fails or is ambiguous, surface an explicit Open Question and pause for human guidance.

## Finishing steps (must do)

- On the parent bead remove the label: "Status: Milestones Defined" ` bd update <bead-id> --remove-label "Status: Milestones Defined" --json`
- On the parent bead add a Label: "Status: Plan Created" ` bd update <bead-id> --add-label "Status: Plan Created" --json`
- Run `bs sync` to sync bead changes.
- Run `bd show <beadId>` (not --json) to show the entire bead.
- End with: "This completes the Plan process for <bd-id>".

## Examples

- `/plan bd-456`
  - Starts an interview to break epic `bd-456` into feature and task beads.
- `/plan bd-456 MVP first`
  - Same as above, but seeds the interview with the phrase "MVP first".
