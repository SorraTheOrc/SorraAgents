---
name: plan
description: Decompose a Worklog work item into features and implementation tasks. Includes automated effort/risk pre-check to skip planning for small items. Use when a work item needs to be broken down into actionable sub-tasks.
---

# Plan Skill

Decompose a Worklog epic (or other Worklog work item) into **features** and
**implementation tasks**. Includes a built-in effort/risk pre-check that
automatically skips planning for small items (performing the same check used
by the autoplan decision logic).

## Inputs

- The supplied `<work-item-id>` is the work item to plan.
  - If no valid `<work-item-id>` is provided (ids are formatted as
    `<prefix>-<hash>`), ask the user to provide one.
- Optional additional freeform arguments may be provided to guide the
  planning process. Freeform arguments are found after the `<work-item-id>`.

## Results and Outputs

- The parent work item or each of the epics below it are decomposed into
  child feature work items.
- Idempotence: The command reuses existing child work items and updates or
  augments previously generated feature work-items instead of creating
  duplicates.

## Hard requirements

- Use **Acceptance Criteria** as the canonical term; **Success Criteria** is an accepted synonym for legacy references.
- Do not create a work item for the planning process itself; the output is feature work items and parent updates.
- Each feature must be deliverable as a minimal end-to-end slice (code, tests, docs, infra, observability).
- Identify existing implementations or features that can be reused.
- Use concise interview style: ≤3 high-signal questions per iteration. Prefer multiple-choice but allow freeform.
- Do not invent requirements, dates, or owners — propose options and ask for confirmation.
- Respect `.gitignore` and agent ignore rules.
- If the user is uncertain, add clarifying questions rather than guessing.
- **Test-first ordering**: create test/verification work items before implementation work items.
- **Vertical slice phasing**: use tracer-bullet approach — each phase cuts through ALL layers end-to-end, not a single horizontal layer. Between phases, review and update next items' descriptions.
- When recommending next steps, the first must progress to the next process step with a summary.

## Status lifecycle (first action)

See [AGENTS.md](../../AGENTS.md#workflow-for-ai-agents) for the standard claim-first pattern.

Claim the item with `StatusLifecycle.update_status(<work-item-id>, "in_progress")`
before starting, and always leave the item in a **valid terminal status**
(`open`, `blocked`, or `completed`) when the skill ends — including on error:

- **On completion:** `StatusLifecycle.update_status(<work-item-id>, "open", stage="plan_complete")`
- **When pausing for producer input:** reset to
  `StatusLifecycle.update_status(<work-item-id>, "open", needs_producer_review=True)`
  (optionally preserving the current stage) so the item is released back to
  `open` and flagged for producer triage instead of being left `in_progress`.
- **On error/abort:** `StatusLifecycle.update_status(<work-item-id>, "open")`
  and log a comment describing the failure.

Never leave an item in `in_progress` status when control returns to the
operator — an orphaned `in_progress` item is invisible to `wl next` and
blocks all downstream work until a human intervenes.

## Seed context

- Read `docs/` (excluding `docs/dev`), `README.md`, and other high-level files for context.
- Run `wl show <work-item-id> --json` — treat description and referenced artifacts as authoritative seed intent.
- Pay attention to any PRD referenced in this work item or parent items.
- If `wl` is unavailable or the work item cannot be found, fail fast and ask for a valid id.
- Prepend a short "Seed Context" block to the interview with the fetched title, type, tags, and description.

## Pre-check: Effort/Risk Threshold (must do before Process step 1)

Before starting the planning process, check whether the work item is small
enough that planning can be skipped. This uses the shared decision logic
from the bundled [plan_helpers.py](plan_helpers.py) (the same logic used by
the autoplan decision logic).

1. Run the effort/risk check from the skill directory:

   ```bash
   python3 $(dirname $(readlink -f $0))/plan_helpers.py plan-if-needed <work-item-id>
   ```

   Or use the canonical bundled script directly:

   ```bash
   python3 ./plan_helpers.py plan-if-needed <work-item-id>
   ```

2. Parse the JSON result. Expected keys:

   - `target_id` — the work item id
   - `decision` — `"skip"` (effort and risk below threshold, planning not
     needed), `"plan"` (effort or risk above threshold, planning required),
     or `"error"` (the work item could not be fetched — the helper makes
     NO writes to the work item in this case)
   - `effort` — the work item's effort t-shirt size (e.g. `"Small"`) when
     determinable, otherwise empty
   - `risk` — the work item's risk level (e.g. `"Low"`) when determinable,
     otherwise empty
   - `error` — present only when `decision` is `"error"`; a human-readable
     explanation (no changes were made to the work item)

3. Act on the decision:

   - **If `decision == "skip"`**: The work item is small enough to implement
     directly without decomposition. However, before marking it as
     `plan_complete`, run the six automated review stages (see
     **Automated review on existing content** below) against the existing
     work item content (description and any existing child work items).
     The review stages will identify and address any gaps before the work
     item reaches `plan_complete`.

     After the review stages complete and any identified issues have been
     addressed (conservatively — only fixing clearly needed and unambiguous
     gaps), output a summary to the console listing what each review stage
     checked and what (if anything) was found or changed. Then mark the
     work item as `plan_complete` and record a summary comment:

     ```python
     StatusLifecycle.update_status(work_item_id, "open", stage="plan_complete")
     ```
     ```bash
     wl comment add <work-item-id> --author "plan" --comment "Auto-plan completed with review: effort and risk below threshold. Review summary: [summarise what each stage checked and any changes made]" --json
     ```

   - **If `decision == "error"`**: The work item could not be fetched
     (invalid id, wrong cwd, or worklog unavailable). The helper made **no
     writes** — the item's effort/risk fields and comments are untouched;
     it never invokes the effort-and-risk orchestration with placeholder
     values when it could not read the item first. Default to full planning
     as a safety measure: proceed to the Process steps below and log a
     warning.

   - **If `decision == "plan"`**: Proceed to the Process steps below. The
     work item is large or risky enough to warrant full decomposition.

   - **If the CLI fails** (non-zero exit, invalid JSON, or unexpected
     output): Default to full planning as a safety measure. Proceed to the
     Process steps below and log a warning.

4. **Idempotence**: If the work item's stage is already `plan_complete` or
   later, the pre-check will return `decision: "skip"` and the command will
   exit with the existing stage preserved (a warning comment is added).

## Plan-approval gate (Process step 4)

Before asking the user to approve a proposed feature plan (Process step 4),
run the approval-gate check using the bundled `plan_helpers.py`:

```bash
python3 ./plan_helpers.py plan-approval-gate <work-item-id>
```

Parse the JSON result. Expected keys:

- `request_approval` — `true` (ask the user to approve the plan) or `false`
  (proceed without an approval pause)
- `reason` — human-readable explanation of why approval is requested
  (empty when approval is not needed)

The gate requests approval when the work item's effort t-shirt size is
Medium/Large/Extra Large (scale) **OR** its risk level is Medium/High, and
**skips** approval when effort is Extra Small/Small **AND** risk is Low.
When effort/risk values are absent from the work item, the gate defaults
conservatively to requesting approval (mirroring `resolve_complexity_tier`'s
Medium default), so a human checkpoint is never silently skipped.

- **If `request_approval == true`**: present the plan and ask the user to
  accept, edit, reorder, or split/merge — iterating until approved. When
  asking, state the reason explicitly so the user understands why the
  checkpoint is required, e.g. "This plan requires your confirmation because
  its effort is Large scale and its risk is Medium."
- **If `request_approval == false`**: do NOT ask the user to approve the
  plan; proceed directly to Process step 5 (vertical slice verification)
  and step 6 (automated review stages).

## Automated review on existing content (auto-complete path)

When the pre-check returns `decision: "skip"`, the skill runs the six
review stages against whatever content exists in the work item
description and any existing child work items. Unlike Process step 6
(which reviews a freshly generated feature plan), this auto-complete
path reviews the existing content as-is.

Each review stage MUST:

- Run sequentially in the order listed below.
- Operate on the existing work item content (description, child items).
- Be conservative: only fix gaps that are clearly needed and unambiguous.
- If an automated improvement could change intent, do NOT apply it
  automatically; instead record an Open Question and continue.
- After each stage, output exactly:
  "Finished <Stage Name> review: <brief notes of improvements>"

Review stages (adapted for existing content):

1. **Requirements & AC alignment review** — Verify the acceptance criteria
   are a faithful match to the work item's requirements and use cases.
   For each stated requirement or use case in the description (and any
   referenced PRD), confirm there is at least one corresponding acceptance
   criterion that would verify it, and that every acceptance criterion
   traces back to a stated requirement or use case. Flag ACs that are
   missing, contradictory, or cover requirements that were never stated;
   add missing ACs only where the intent is clear and unambiguous,
   otherwise record an Open Question and continue.
2. **Completeness review** — Ensure the work item has all required fields
   (description, acceptance criteria) and that any existing child items
   are complete. Add missing fields if clearly definable from context.

   Additionally, if the work item contains a ``**Key Files:**`` section
   (predicted during intake), validate the listed file paths:

   - **Syntactic validity**: every path should contain at least one ``/``
     (directory separator) and have a file extension. The helper
     ``validate_key_files_format()`` can be used to check this programmatically.
   - **Completeness**: flag any obviously missing files relative to the
     work item scope (e.g. if the description mentions modifying a module
     but no file in that module is listed).
   - **Accuracy**: flag any obviously irrelevant or incorrect files in the
     list.

   Any corrections (additions, removals, or corrections) to the ``**Key
   Files:**`` list identified during this review should be reflected in the
   work item description before the plan process completes.
3. **Sequencing & dependencies review** — Verify any existing child item
   dependencies are coherent. Check that test/verification items appear
   before implementation items if both exist. Ensure test features come first
   when ordering child items.
4. **Scope sizing review** — Ensure any existing features are sized as
   deliverable increments. If no child items exist, this stage is a no-op.
5. **Acceptance & testability review** — Verify acceptance criteria are
   pass/fail and testable. Improve vague or untestable criteria where
   the intent is clear and unambiguous.
6. **Polish & handoff review** — Ensure the work item description is
   clear, well-formatted, and actionable.

After all six stages complete, output a summary to the console listing
what each review stage checked and what (if anything) was found or changed.
Then proceed to mark the work item as `plan_complete` (see skip path
instructions above).

## Process (must follow)

1. Evaluate whether planning is required (agent responsibility)

   Before starting the full interview, assess if the item already has a sufficient plan:
   - If `stage` is `plan_complete` or later → no-op skip.
   - If not an `epic` and description has measurable ACs and a minimal implementation sketch → mark complete.
   - If existing child items already cover the scope (`wl list --parent <id> --json`) → skip.
   - If a concise plan block exists → treat as sufficient.

   If planning is not needed:
   - `StatusLifecycle.update_status(<work-item-id>, "open", stage="plan_complete")`
   - Add comment: `wl comment add <work-item-id> "Plan auto-complete: sufficiently sized/defined for direct implementation." --actor Map --json`

   When borderline, err toward auto-complete. Only fall back to clarifying questions when decomposition is clearly needed.

2. Fetch & summarise (agent responsibility)

   - Run `wl show <work-item-id> --json` and summarise the work item: title, type, headline, existing children and plan info.
   - Validate readiness by `stage`:
     - `intake_complete` → ready for planning
     - `plan_complete` or later → skip, record no-op comment
     - Other → check Step 1 auto-complete heuristics before asking the operator. If heuristics indicate planning is not needed, skip to recording a no-op comment. Only ask the operator when heuristics genuinely cannot determine.
   - Read any linked PRD for key details.
   - Derive 3-6 keywords from title/description to search for related work; present likely duplicates or relationships.

3. Interview

   In iterations (≤3 questions each), gather the minimum information for an actionable feature plan. For each feature capture:
   - **Target outcome**: what user-visible capability must exist?
   - **Definition of done**: pass/fail acceptance checks (checklist + automated tests where possible).
   - **Constraints**: performance, compatibility, rollout/feature-flag, timeline.
   - **Risky assumptions**: where is a prototype/experiment needed (mock API, spike) and what does "success" mean?

   Keep iterating until feature breakdown is clear.

   - Review existing Appendix entries first: do NOT re-ask questions already answered unless further clarification is needed (reference the existing entry).

4. Propose feature plan (agent responsibility + user confirmation)

   Produce a draft plan (guide: 3-12 features) where each feature includes:
   - **Short Title** (≤7 words) | **Summary** (one sentence) | **Acceptance Criteria** (2-6 measurable bullets)
   - **Minimal Implementation** (2-6 bullets, smallest end-to-end slice)
   - **Prototype/Experiment** (optional; success thresholds)
   - **Dependencies** | **Deliverables** (artifacts)

   Each feature must describe how the user experience changes and what ACs validate it.

   - **Test-first ordering**: test/verification features before implementation features.
   - **Approval gate**: before asking for approval, run the approval-gate check
     (see **Plan-approval gate** below). Approval is requested only when the
     work item's effort t-shirt size is Medium/Large/Extra Large (scale) OR its
     risk level is Medium/High, or when effort/risk are absent (conservative
     default). When approval IS requested, state the reason explicitly
     (e.g. "This plan requires your confirmation because its effort is Large
     scale and its risk is Medium."). Present as numbered list and ask user to
     accept, edit, reorder, or split/merge. Iterate until approved.
   - When the gate says approval is NOT warranted (effort Extra Small or Small
     AND risk Low), do NOT ask the user to approve the plan — proceed directly
     to step 5 (vertical slice verification) and step 6 (automated review
     stages) without an approval pause.

5. Verify vertical slice phasing (agent responsibility)

   Ensure each phase is a vertical slice through ALL layers (code, tests, docs, infra, observability).
   - If a phase is a horizontal slice, ask the user to refactor.
   - Include between-phase guidance: review next items, update descriptions.
   - If the item is small enough to not require phasing, document the decision and proceed to review stages.

6. Automated review stages (must follow; no human intervention required)

   After the user approves the feature list, run six review iterations.
   Each review MUST provide a new draft if any changes are recommended
   and then output exactly: "Finished <Stage Name> review: <brief notes
   of improvements>"

   - General requirements for the automated reviews:
     - Run without human intervention.
     - Each stage runs sequentially in the order listed below.
     - Improvements should be conservative and scoped to the stage.
     - If an automated improvement could change intent, do NOT apply it
       automatically; instead record an Open Question and continue.

   Review stages and expected behavior:
   1. Requirements & AC alignment review — Verify each feature's acceptance
      criteria faithfully match its stated requirements and use cases.
      For every requirement/use case there must be at least one AC that
      verifies it, and every AC must trace back to a stated requirement.
      Flag missing, contradictory, or invented ACs; add missing ACs only
      where intent is clear and unambiguous, otherwise record an Open
      Question and continue.
   2. Completeness review — Ensure every feature has all required fields.

      Additionally, if the work item contains a ``**Key Files:**`` section
      (predicted during intake), validate the listed file paths:

      - **Syntactic validity**: every path should contain at least one ``/``
        (directory separator) and have a file extension. The helper
        ``validate_key_files_format()`` can be used to check this programmatically.
      - **Completeness**: flag any obviously missing files relative to the
        work item scope (e.g. if the description mentions modifying a module
        but no file in that module is listed).
      - **Accuracy**: flag any obviously irrelevant or incorrect files in the
        list.

      Any corrections (additions, removals, or corrections) to the ``**Key
      Files:**`` list identified during this review should be reflected in the
      work item description before the plan process completes.
   3. Sequencing & dependencies review — Ensure dependencies are coherent
      and actionable. Verify that test/verification features appear before
      implementation features.
   4. Scope sizing review — Ensure features are sized as deliverable
      increments.
   5. Acceptance & testability review — Ensure acceptance criteria are
      pass/fail and testable.
   6. Polish & handoff review — Make the plan copy-pasteable and easy to
      execute.

7. Update work items (agent)

   - **Test-first creation**: create test/verification items before implementation items.
   - Create child work items for each feature:
     `wl create --title "<Short Title>" --description "<Full description>" --parent <work-item-id> --priority P2 --stage intake_complete --json`
   - Add dependency edges: `wl dep add <DependentId> <PrereqId>`
   - Ensure idempotence: if a child with the same canonical name exists, reuse it.
   - Add completion comment: `wl comments add $1 "Planning Complete. <Summary>" --actor <agent> --json`
   - Update stage: `StatusLifecycle.update_status(<work-item-id>, "open", stage="plan_complete")`

8. Calculate Effort and Risk (agent responsibility; must follow)

   - Call the `effort_and_risk` skill with the new or updated work item to
     produce an effort and risk estimate.

## Traceability & idempotence

- Re-running this skill must not create duplicate child work items or duplicate plan blocks.
- If changes are made, include a "Plan: changelog" block in the parent work item summarising actions and timestamps.

  > **Note:** This changelog block is for **work-item-level traceability** — it is **not** the repository
  > `CHANGELOG.md`, which is managed automatically by the ship skill's release pipeline.

## Editing rules & safety

- Preserve author intent; when uncertain, create an Open Question entry rather than assuming.
- Keep changes minimal and conservative. Respect `.gitignore`.
- **Worklog validation**: `feature` or `task` work items must include a `## Acceptance Criteria` section.
- **JSON parsing**: `wl ... --json` output may be an object or an array; handle both shapes when parsing.
- If any automated step fails or is ambiguous, surface an Open Question and pause for guidance.

## 8. Finishing (must do as the final step only)

- Set stage to `plan_complete` and status to `open`:
  `StatusLifecycle.update_status(<work-item-id>, "open", stage="plan_complete")`
- Run `wl sync` to sync changes.
- Run `wl show <work-item-id>` (not --json) to display the work item.
- End with: "This completes the Plan process for <work-item-id>".

## Bundled Resources

- `plan_helpers.py` — Shared autoplan decision module. Provides the CLI
  entry points `plan-if-needed` and `check-effort-risk` used in the pre-check
  above, plus `plan-approval-gate` for the step-4 approval gate. Can also be
  imported as a Python module by other tools.

  Usage:

  ```bash
  python3 ./plan_helpers.py plan-if-needed <work-item-id>
  python3 ./plan_helpers.py check-effort-risk <work-item-id>
  python3 ./plan_helpers.py plan-approval-gate <work-item-id>
  ```

  Import:

  ```python
  from skill.plan.plan_helpers import (
      make_autoplan_decision,
      resolve_complexity_tier,
      should_request_plan_approval,
      is_effort_risk_computed,
      run_effort_and_risk,
      append_autoplan_decision_comment,
      validate_key_files_format,
      validate_key_files_in_description,
      plan_if_needed,
      check_effort_risk,
      plan_approval_gate,
      DEFAULT_AUTOPLAN_EFFORT_SKIP,
      DEFAULT_AUTOPLAN_RISK_SKIP,
      PLAN_APPROVAL_EFFORT,
      PLAN_APPROVAL_RISK,
  )
  ```

## Appendix: Clarifying questions & answers (must include)

Every planning session must produce an auditable Appendix of questions asked and answers received, appended to the plan content in the parent work item (description or comment).

Required per entry:

- Question text exactly as asked.
- Answer provided, the answering party, and supporting evidence (work-item id, file path, PR link).
- If the answer changed, record prior answers and mark the final accepted answer.
- If the question led to discussion/research, include a concise summary (1-6 sentences) with links to artifacts.

Behavior:

- Append the complete Appendix to any temporary draft file and include it in the parent work item.
- Idempotence: re-running must not create duplicate entries — append revision notes instead.
- Open questions must be labelled "OPEN QUESTION" with context (directed to whom and why it matters).
- Privacy: only record authorized participants' information; redact inadvertent secrets.
- Traceability: each entry should be linkable from the work item.

**Example format:**

- Q: "Should feature X be behind a feature flag?" Answer (product): "Yes, gradual rollout". Final: yes.
- Q: "Can we reuse library Y?" Answer (eng): "Partially; requires adapter." Research: reviewed `libs/y` and PR #88.
