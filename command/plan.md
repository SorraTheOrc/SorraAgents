---
description: Decompose an epic into features and tasks
tags:
  - workflow
  - plan
  - decomposition
  - effort-risk
  - threshold-check
agent: build
subtask: true
---

You are helping the team decompose a Worklog epic (or other Worklog work item) into **features** and **implementation tasks**.

## Inputs

- The supplied <work-item-id> is $1.
  - If no valid <work-item-id> is provided (ids are formatted as '<prefix>-<hash>'), ask the user to provide one.
    -- Optional additional freeform arguments may be provided to guide your work. Freeform arguments are found in the arguments string "$ARGUMENTS" after the <work-item-id> ($1).

## Results and Outputs

- The parent work item ($1) or each of the epics below it are decomposed into child feature work items.
- Idempotence: The command reuses existing child work items and updates or augments previously generated feature work-items instead of creating duplicates.

## Hard requirements

- Terminology policy: use **Acceptance Criteria** as the canonical term; **Success Criteria** is an accepted synonym when referencing legacy wording.

- Do not create a work item for the planning process itself; the output of this command is the generated feature work items and updates to the parent work item.
- Provide guidance on how each feature can be delivered as a minimal, end-to-end slice (code, tests, docs, infra, observability).
- Where possible identify existing implementations details that are related to the feature.
- Where possible identify existing features or tasks that can be reused instead of creating duplicates.
- Use an interview style: concise, high-signal questions grouped to a soft-maximum of three per iteration.
- Do not invent requirements, commitments (dates), or owners - propose options and ask the user to confirm.
- Respect ignore boundaries: do not include or quote content from files excluded by `.gitignore` or the agent framework's ignore rules.
- Prefer short multiple-choice suggestions where possible, but always allow freeform responses.
- If the user indicates uncertainty, add clarifying questions rather than guessing.
- **Test-first ordering**: When creating child work items, test/verification work items must always be created before implementation work items. This ensures a test-driven development approach is followed - tests are defined first and implementation follows. The feature plan must list test features before implementation features, and the `wl create` commands for test items must be issued before those for implementation items.

- **Vertical slice phasing**: When phasing large work items into multiple integration phases, use the **tracer bullet** approach. Each phase should be a thin vertical slice that cuts through ALL integration layers end-to-end (code, tests, docs, infra, observability), NOT a horizontal slice of a single layer. This ensures each phase delivers end-to-end user value and can be integrated independently. Between each phase, review the next work items and update their descriptions if they depend on completed work or come next in the schedule.

- Whenever you are recommending next steps you MUST make the first one a progression to the next step in the process defined below, with a summary of what that step involves.

## Note

- This Hard requirements section is populated with the mandatory progression rule above; review the rest of the hard requirements for task-specific constraints.

## Status lifecycle (first action)

- **Before any other step**, claim the work item by running:
  `wl update <work-item-id> --status in_progress --json`
  This must be the very first action — before any pre-checks, context gathering, or
  other preflight steps. The status signals to other agents that this item is
  being processed and prevents concurrent claims.

## Seed context

- Read `docs/` (excluding `docs/dev`), `README.md`, and other high-level files for context.
- Fetch and read the work item details using Worklog CLI: `wl show <work-item-id> --json` and treat the work item description and any referenced artifacts as authoritative seed intent.
- Pay particular attention to any PRD referenced in this work item or any of its parent work items.
- If `wl` is unavailable or the work item cannot be found, fail fast and ask the user to provide a valid <work-item-id> or paste the work item content.
- Prepend a short "Seed Context" block to the interview that includes the fetched work item title, type, current tags, and one-line description.

## Pre-check: Effort/Risk Threshold (must do before Process step 1)

Before starting the planning process, check whether the work item is small enough
that planning can be skipped. This uses the shared decision logic from
``command/plan_helpers.py`` (the same logic used by Ralph's autoplan).

1. Run the effort/risk check:

   ```bash
   python3 command/plan_helpers.py plan-if-needed <work-item-id>
   ```

2. Parse the JSON result. Expected keys:

   - ``target_id`` — the work item id
   - ``decision`` — ``"skip"`` (effort and risk below threshold, planning not needed)
                   or ``"plan"`` (effort or risk above threshold, planning required)

3. Act on the decision:

   - **If ``decision == "skip"``**: The work item is small enough to implement
     directly without decomposition.  Mark it as ``plan_complete`` and record
     a brief comment:

     ```bash
     wl update <work-item-id> --stage plan_complete --status open --json
     wl comment add <work-item-id> --author "plan" --comment "Auto-plan skipped: effort and risk below threshold (checked via command/plan_helpers.py plan-if-needed). Proceeding directly to implementation." --json
     ```

     Then **exit** the planning command without proceeding to the Process steps.

   - **If ``decision == "plan"``**: Proceed to the Process steps below.  The
     work item is large or risky enough to warrant full decomposition.

   - **If the CLI fails** (non-zero exit, invalid JSON, or unexpected output):
     Default to full planning as a safety measure.  Proceed to the Process
     steps below and log a warning.

4. **Idempotence**: If the work item's stage is already ``plan_complete`` or
   later, the pre-check will return ``decision: "skip"`` and the command will
   exit with the existing stage preserved (a warning comment is added).

> **Note for PlanAll**: This pre-check is built into the ``/plan`` command
> prompt. PlanAll shells out to ``/plan <id>`` which runs this pre-check
> automatically — no changes to PlanAll are needed.

## Process (must follow)

1. Evaluate whether planning is required (agent responsibility)

- Before starting the full planning interview and decomposition, run a quick assessment to determine whether the work item already has a sufficient plan or is too small to require decomposition.
- Suggested checks (conservative, idempotent heuristics):
  - If the work item's `stage` is `plan_complete` or later, planning is already complete - skip with a no-op (step 2 handles this case with a comment).
  - If the work item is not an `epic` (for example `task` or `bug`) and the description already contains measurable acceptance criteria and a minimal implementation sketch, consider it "ready" and mark planning complete.
  - If the work item already has child features/tasks that cover the intended scope (use `wl list --parent <work-item-id> --json` and compare), and those children are adequate and idempotent, skip full planning and mark `plan_complete`.
  - If a concise plan block already exists in the work item (for example a labeled "Plan:" or a short numbered feature list with acceptance criteria), treat that as sufficient evidence to skip the full interview.
- If the checks indicate planning is not needed, update the work item to record the decision and advance the stage:
  - `wl update <work-item-id> --stage plan_complete --status open --json`
  - Add a comment documenting the reason: `wl comment add <work-item-id> "Plan auto-complete: work item appears sufficiently sized/defined for direct implementation." --actor Map --json`
- If evidence is borderline or key uncertainties remain, err on the side of progress and auto-complete (update stage and record a comment). Only fall back to the normal planning process (asking clarifying questions) when there is clear evidence that the item genuinely needs decomposition and the heuristics cannot make a determination.

1. Fetch & summarise (agent responsibility)

- Run `wl show <work-item-id> --json` and summarise the work item in one paragraph: title, type (epic/feature/task), headline, and any existing child tasks and plan info.
- Validate that the work item is ready for planning by inspecting its `stage` field:
- Run `wl show <work-item-id> --json` and summarise the work item in one paragraph: title, type (epic/feature/task), headline, and any existing child tasks and plan info.
- Validate readiness by examining the work item's `stage` value:
  - `intake_complete` indicates it is ready for planning.
  - `plan_complete` or later stages indicate planning has already been completed; skip planning entirely and record a no-op comment:
    - `wl comment add <work-item-id> "Plan not needed: stage is already plan_complete or later." --actor Map --json`
    - Then proceed to the finishing steps.
  - Any other `stage` value suggests the work item is not currently at the intake_complete stage. Run the planning heuristics (see step 1) to determine whether the item is sufficiently small/well-defined to auto-complete. If so, auto-complete via step 1. If the heuristics genuinely cannot determine, ask the user how to proceed (indicating that if the work item is small enough it is OK to proceed).
- Read any PRD linked in the work item or any of its parents to extract key details for later reference.
- Derive 3-6 keywords from the work item title/description to search the repo and work items for related work. Present any likely duplicates or parent/child relationships.

1. Interview

In interview iterations (≤ 3 questions each), gather the minimum information needed to produce an actionable feature plan in which each feature is large enough to be meaningful but small enough to be delivered as an end-to-end slice. For each feature capture:

- Target outcome: what user-visible capability must exist when this epic is "done"?
- Definition of done: what are the pass/fail acceptance checks (a short manual checklist and automated tests if possible)?
- Constraints: performance, compatibility, rollout/feature-flag expectations, or timeline constraints.
- Risky assumptions: identify where a prototype/experiment is needed (fake API, mock UI, spike) and what "success" means.

Keep asking questions until the breakdown into features is clear.

- Review existing Appendix entries first: the agent MUST NOT ask any question that already appears in the Appendix of the parent work item or the current work item unless further clarification is required. If further clarification is required, reference the existing Appendix entry in the question and explain what additional detail is needed before re-asking.

1. Propose feature plan (agent responsibility + user confirmation)

- Produce a draft plan (soft guide: 3-12 features) where each feature includes:
  - **Short Title** (canonical, stable, ≤ 7 words)
  - **Summary** (one sentence)
  - **Acceptance Criteria** (2-6 concise bullets; measurable/testable)
  - **Minimal Implementation** (2-6 bullets; smallest end-to-end slice)
  - **Prototype / Experiment** (optional; include success thresholds)
  - **Dependencies** (other features or explicit external factors)
  - **Deliverables** (artifacts: docs, tests, demo script, telemetry)

- Each of the features should clearly identify how the player experience will be changed by the feature and what acceptance critera are required to validate it.
- Each of the features should clearly identify how the user experience will be changed by the feature and what acceptance criteria are required to validate it.

- **Test-first ordering**: Test and verification features must be listed before implementation features in the proposed plan. This ensures that when work items are created from the plan, test tasks are created first and establish the validation criteria that implementation tasks must satisfy.

- Present the draft as a numbered list and ask the user to: accept, edit titles/scopes, reorder, or split/merge features.
- If the user requests changes, iterate until the feature list is approved.

1. Verify vertical slice phasing (agent responsibility)

- Review the proposed feature plan to ensure each phase represents a vertical slice that cuts through ALL integration layers end-to-end.
- If any phase appears to be a horizontal slice (focused on a single layer), ask the user to refactor it into vertical slices.
- Verify that between-phase guidance is included: implementation should review next work items and update descriptions as needed.
- If the work item is small enough to not require phasing, document this decision and proceed to the automated review stages.

1. Automated review stages (must follow; no human intervention required)

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
  1. Sequencing & dependencies review
  - Purpose: Ensure dependencies are coherent and actionable.
  - Actions: Detect cycles, missing prerequisites, or vague dependencies; propose minimal fixes that do not change intent; record uncertainty as Open Questions. Verify that test/verification features appear before implementation features - if they do not, reorder them so test features come first.
  1. Scope sizing review
  - Purpose: Ensure features are sized as deliverable increments.
  - Actions: Flag features that are too broad/vague or duplicate scope; suggest split/merge candidates as Open Questions.
  1. Acceptance & testability review
  - Purpose: Ensure acceptance criteria are pass/fail and testable.
  - Actions: Tighten criteria wording; add missing negative cases only when clearly implied.
  1. Polish & handoff review
  - Purpose: Make the plan copy-pasteable and easy to execute.
  - Actions: Standardize bullets, tense, and structure; keep titles canonical.

1. Update work items (agent)

- **Test-first creation**: When creating child work items, always create test/verification work items before implementation work items. This ensures that test tasks are available first, enabling a test-driven development workflow where tests define the validation criteria before implementation begins.
- Create child work items for each feature with a parent link to the original work item:
- `wl create --title "<Short Title>" --description "<Full feature description>" --parent <work-item-id> --priority P2 --stage intake_complete --json`
- Create dependency edges between feature work items where the plan specifies dependencies:
  - `wl dep add <DependentFeatureId> <PrereqFeatureId>`
  - Specifically: when creating implementation/code work items, add a dependency from the implementation work item to its corresponding test authoring work item so the implementation depends on the test. Example:
    - `wl dep add <implementation-work-item-id> <test-work-item-id>`
  - Operators may also link existing items using the same command pattern, for example: `wl dep add <work-item0id> <test-item-id>`. This ensures `wl next` and other scheduling tools favour test authoring tasks before implementation.

- When creating child work items, ensure idempotence:
- If a child work item with the same canonical name already exists, reuse it instead of creating a duplicate.
- Use `wl list --parent <work-item-id> --json` for features.

- Add a comment to the planned work item:
  - `wl comments add $1 "Planning Complete. <Summary of the approved feature list, any open questions that remain>" --actor <your-agent-name> --json`
    -- Update the planned work item's stage to indicate planning is complete:
  - `wl update $1 --stage plan_complete --status open --json`

1. Calculate Effort and Risk (agent responsibility; must follow)

- Call the `effort_and_risk` skill with the new or updated work item to produce an effort and risk estimate.

## Traceability & idempotence

- Re-running `/plan <work-item-id>` should not create duplicate child work items or duplicate generated plan blocks in the parent work item.
- If the command makes changes, include a changelog block in the parent work item (labelled "Plan: changelog") summarising actions and timestamps.

## Editing rules & safety

- Preserve author intent; where the agent is uncertain, create an Open Question entry rather than making assumptions.
- Keep changes minimal and conservative.
- Respect `.gitignore` and other ignore rules when scanning files for context.
- **Worklog validation**: when creating `feature` or `task` work items ensure the description includes a `## Acceptance Criteria` section.
- **JSON parsing**: `wl ... --json` output may be either an object or an array; when extracting ids with `jq`, handle both shapes (e.g., `if type=="array" then .[0].id elif type=="object" then .id end`).
- If any automated step fails or is ambiguous, surface an explicit Open Question and pause for human guidance.

## 8. Finishing (must do as the final step only)

- On the parent work item set the work item's stage to `plan_complete` and status to `open`:
  `wl update <work-item-id> --stage plan_complete --status open --json`
- Run `wl sync` to sync work item changes.
- Run `wl show <work-item-id>` (not --json) to show the entire work item.
- End with: "This completes the Plan process for <work-item-id>".

## Examples

- `/plan wl-456`
  - Starts an interview to break epic `wl-456` into feature and task work items.
- `/plan wl-456 MVP first`
  - Same as above, but seeds the interview with the phrase "MVP first".

## Appendix: Clarifying questions & answers (must include)

- Purpose: Every interview-driven planning session must produce an auditable Appendix that lists all clarifying questions the agent asked during the planning interview and the answers provided by the user or stakeholders. This Appendix must be appended to any plan content written into the parent work item or temporary draft files and must also be included in the Worklog work item description or a comment when the plan is finalized.

- Required contents for each entry:
  - The question text exactly as asked.
  - The answer provided and the answering party (user, stakeholder, or agent inference) and any supporting evidence or references (work item id, file path, PR link).
  - If the answer changed during the process, record prior answers and mark the final accepted answer.
  - If the question resulted in a discussion and/or research, include a concise summary (1-6 sentences) describing the discussion, research performed, findings, and any links to supporting artifacts (files, PRs, issues). Summaries should focus on impact to scope, dependencies, or implementation choices.

- Example format:

  - Q: "Should feature X be behind a feature flag? (yes/no/ask)" - Answer (product@acme): "Yes, gradual rollout behind flag". Source: interactive reply. Final: yes.
  - Q: "Can we reuse library Y?" - Answer (eng@acme): "Partially. Research: reviewed `libs/y` and PR #88; requires adapter wrapper. Follow-up: created wl-789 to implement adapter." (Research summary: adapter required; library lacks needed API surface.)

- Behavior and placement rules:
  - The agent MUST append the complete Appendix to any temporary draft file used for the plan and include it in the parent work item's description or as a `wl comment` when calling `wl update` or `wl comment add`.
  - Preserve idempotence: re-running `/plan` MUST NOT create duplicate Appendix entries. If a Q/A pair exists, either skip re-recording it or append a short revision note rather than duplicating the original entry.
  - Open questions that remain unanswered must be included and clearly labelled as "OPEN QUESTION" with brief context (who it was directed to and why it matters).
  - Do not include or quote content from files excluded by `.gitignore` or other the agent framework's ignore rules as part of the Appendix.

- Privacy & scope:
  - Only record information provided by authorized participants. Redact any inadvertent secrets and note the redaction.

- Traceability:
  - Each Appendix entry should be linkable from the work item (either embedded in the work item body or referenced in a comment) and include related item references for discovery (e.g., `related-to:<work-item-id>`).
