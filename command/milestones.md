---
description: Define project milestones and create milestone beads
tags:
  - workflow
  - milestones
agent: build
---

You are helping the team define a clear, actionable milestone plan for work tracked by a Beads issue.

## Quick inputs
  - The beads issue id is $1.
    - If no bead id is provided, ask the user to provide one.
  - Optional additional freeform arguments will be used to guide the milestone planning. Freeform arguments are found in the entire arguments string: "$ARGUMENTS".

## Hard requirements

- Be environment-agnostic: do not assume calendar systems, CI schedules, or release cadence unless the user specifies them.
- Use an interview style: concise, high-signal questions grouped to a soft-maximum of three per iteration.
- Do not invent commitments (dates, owners) — propose ranges and ask the user to confirm assignments and due dates.
- Respect ignore boundaries: do not include or quote content from files excluded by `.gitignore` or any OpenCode ignore rules.
- Prefer short multiple-choice suggestions where possible, but always allow freeform responses.
- If the user indicates uncertainty, add clarifying questions rather than guessing.

## Seed context

- Read `docs/` (excluding `docs/dev`), `README.md`, and other high-level files for context to help estimate scope.
- Fetch and read the bead details using beads CLI: `bd show $1 --json` and treat the bead description and any referenced artifacts as authoritative seed intent.
- Read any documents or beads referenced in the bead description, comments or external references.
- If `bd` is unavailable or the issue cannot be found, fail fast and ask the user to provide a valid bead id or paste the bead content.
- Prepend a short “Seed Context” block to the interview that includes the fetched bead title, type, current labels, and one-line description.

## Process (must follow)

1) Fetch & summarise (agent responsibility)

- Run `bd show $1 --json` and summarise the bead in one paragraph: title, type (epic/feature/task), headline, and any existing milestone/roadmap info.
- Derive 3–6 keywords from the bead title/description to search the repo and beads for related work. Present any likely duplicates or parent/child relationships.

2) Interview

- In interview iterations (≤ 3 questions each) build a full understanding of the work, and likely milestone breakdown.
- If anything is ambiguous, ask for clarification rather than guessing.
- Keep asking the user questions until you feel the breakdown into milestones is clear.

3) Propose milestone structure (agent responsibility + user confirmation)

- Produce a draft milestone plan (soft guide of 3–8 milestones recommended) with for each milestone:
  - Description full description of the milestone in the following format:
    - Short summary line (one sentence)
    - Scope
      - 1–2 lines summarizing scope
    - Success Criteria
      - 2–4 concise bullets (measurable / testable)
    - Dependencies
      - List other milestones or external factors
    - Deliverables
      - Short list of artifacts expected (e.g., example scene, API spec, Ink fragment, replay report)

- Present the draft as a numbered list and ask the user to: accept, edit titles/scopes/dates/owners, reorder, or split/merge milestones.
- If the user requests changes, iterate until the milestone list is approved.

4) Automated review stages (must follow; no human intervention required)

After the user approves the milestone list, run five review iterations. Each review MUST provide a new draft if any changes are recommended and then print a clear finish message as follows:
  - Then output exactly: "Finished <Stage Name> review: <brief notes of improvements>"
    - If no improvements were made: "Finished <Stage Name> review: no changes needed"

- General requirements for the automated reviews:
  - Run without human intervention.
  - Each stage runs sequentially in the order listed below.
  - Improvements should be conservative and scoped to the stage.
  - If an automated improvement could change intent (e.g., re-scoping milestones, changing dependency ordering, changing any stated dates/owners), do NOT apply it automatically; instead record an Open Question and continue.

- Review stages and expected behavior:

  1) Completeness review
    - Purpose: Ensure every milestone has all required fields.
    - Actions: Verify each milestone includes Short title, Scope summary, Success criteria, Dependencies, and a full Description. Add missing placeholders only when obvious; otherwise add Open Questions.

  2) Sequencing & dependencies review
    - Purpose: Ensure milestone dependencies are coherent and actionable.
    - Actions: Check that dependencies reference other milestones or explicit external factors; detect cycles or missing prerequisite milestones; propose minimal dependency edits that do not change intent. Record uncertainty as Open Questions.

  3) Scope sizing review
    - Purpose: Ensure milestones are sized and framed as deliverable increments.
    - Actions: Flag milestones that are too broad/vague or duplicate scope. Suggest split/merge candidates as Open Questions (do not apply automatically unless the user already approved that restructuring).

  4) Traceability & idempotence review
    - Purpose: Ensure the plan supports safe re-runs of the command.
    - Actions: Confirm milestone titles are canonical (stable, ≤ 7 words) and suitable for child bead creation; ensure the plan does not imply duplicate bead creation on reruns.

  5) Polish & handoff review
    - Purpose: Make the milestone plan copy-pasteable and easy to execute.
    - Actions: Tighten wording for clarity, standardize bullets.

5) Create beads (agent)

- Create child beads (type: epic) for each milestone with a parent link to the original bead:
  - `bd create "<Short Title>" --description "Description>" --parent $1 -t epic --json --labels "milestone" --priority P1 --assignee Build --validate`

  Note: milestone epics are assigned to Build by default per repository conventions. If a different owner is requested, update the bead after creation with `bd update`.
- Creating blocking relationship between each milestone as appropriate, this will be a "chain" of dependencies (e.g. M3 blocked by M2 blocked by M1).
  - `bd dep add <Bead ID> <Previous Milestone Bead ID>`
- Update the parent bead description to add or update a "Milestones" section with the agreed list (minimal, non-destructive). Identify the Milestone epic ID.
- When creating child beads, ensure idempotence: if a child bead with the same canonical name, or a child bead previously created by this command exists, reuse it instead of creating a duplicate. Use `bd list --parent $1 --json` or equivalent to detect existing children.
- When updating the parent bead, append or replace only a well-marked "Milestones" block; if a previous generated block exists, replace it rather than appending.

## Traceability & idempotence

- Re-running `/milestones <bd-id>` should not create duplicate child beads or duplicate generated milestone blocks in the parent bead.
- If the command makes changes, include a changelog block in the parent bead (labelled "Milestones: changelog") summarising actions and timestamps.

## Editing rules & safety

- Preserve author intent; where the agent is uncertain about scope or dates, create an Open Question entry rather than making assumptions.
- Keep changes minimal and conservative. If a proposed change could alter intent (e.g., shifting a milestone date earlier than stated constraints), ask for human confirmation.
- Respect `.gitignore` and other repo ignore rules when scanning files for context.
- If any automated step fails or is ambiguous, surface an explicit Open Question and pause for human guidance.

## Finishing steps (must do)

- On the parent bead remove the label: "Status: PRD Completed" ` bd update <bead-id> --remove-label "Status: PRD Completed" --json`
- On the parent bead add a Label: "Status: Milestones Defined" ` bd update <bead-id> --add-label "Status: Milestones Defined" --json`
- If child beads were created, print their ids and add a short changelog entry to the parent bead.
- Run `bs sync` to sync bead changes.
- Run `bd show <parentBeadId>` (not --json) to show the entire bead.
- End with: "This completes the Milestones process for <bd-id>".

## Examples

- `/milestones bd-123`
  - Starts an interview using bead bd-123 as seed context.
- `/milestones bd-123 Q3 release` 
  - Starts the same interview but seeds the conversation with a target horizon of "Q3 release".
