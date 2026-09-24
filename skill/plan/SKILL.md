---
name: plan
description: "Decompose a work item into features and implementation tasks; auto-skip small items. Use when planning work."
---

# Plan Skill

Decompose a Worklog epic (or other Worklog work item) into **features** and
**implementation tasks**. Includes a built-in effort/risk pre-check that
automatically skips planning for small items (performing the same check used
by the autoplan decision logic).

## Inputs

- The supplied `<work-item-id>` is the item to plan; if invalid (ids are
  `<prefix>-<hash>`), ask the user. Optional freeform args may guide it.

## Results and Outputs

- The parent (or each epic below it) is decomposed into child feature items.
- Idempotence: reuses existing children, updating/augmenting previously
  generated features instead of duplicating.

## Hard requirements

- Use **Acceptance Criteria** as the canonical term; **Success Criteria** is an accepted synonym for legacy references.
- Do not create a work item for the planning process itself.
- Each feature must be deliverable as a minimal end-to-end slice (code, tests, docs, infra, observability).
- Identify existing implementations or features that can be reused.
- Use concise interview style: ≤3 high-signal questions per iteration; prefer multiple-choice but allow freeform.
- Do not invent requirements, dates, or owners — propose options and ask for confirmation.
- Respect `.gitignore` and agent ignore rules.
- If the user is uncertain, add clarifying questions rather than guessing.
- **Test-first ordering**: create test/verification work items before implementation work items.
- **Vertical slice phasing**: tracer-bullet approach — each phase cuts through ALL layers end-to-end, not a single horizontal layer. Between phases, review and update next items' descriptions.
- When recommending next steps, the first must progress to the next process step with a summary.

## Status lifecycle (first action)

See [AGENTS_GLOBAL](../../AGENTS_GLOBAL.md#workflow-for-ai-agents) for the claim-first pattern.

**Invariant (SA-0MTFTFUIH000UWM9): actively worked => `in_progress`; release to `open` only at true handoff.** No agent process may perform work-item mutations (create children, update description, wire deps, add completion comments) while `status: open`. Use `StatusLifecycle.require_claimed(<id>)` as a guard before any mutation, and `StatusLifecycle.ensure_claimed(<id>)` to re-claim on in-session resume.

Claim with `StatusLifecycle.update_status(<work-item-id>, "in_progress")`
before starting, and always leave the item in a **valid terminal status**
(`open`, `blocked`, or `completed`) when the skill ends — including on error:

- **On completion:** `StatusLifecycle.update_status(<work-item-id>, "open", stage="plan_complete")`
- **Pausing for producer input (handoff-only):** Release to `open` AND mark needs-producer-review **only when the session genuinely hands control back** for an open-ended wait with no in-session resume. Do **not** release when an in-session resume is expected (e.g. interactive approval pane — see Resume re-claim below). A downtime-dispatched run with no interactive approver that hits the approval gate must **hold `in_progress` or abort cleanly — never release to `open` mid-run** (the `open` window caused duplicate dispatch of AH-0MTFPDKDU006QUDC on 2026-08-30):
  ```bash
  wl reviewed <work-item-id> true
  ```
  (Alternatively: `StatusLifecycle.update_status(<work-item-id>, "open", needs_producer_review=True)`)
- **On error/abort:** `StatusLifecycle.update_status(<work-item-id>, "open")` + a comment describing the failure.

**Resume re-claim (in-session approval):** When a paused plan run resumes after producer approval **in the same session** (interactive pane), immediately re-claim before any work-bearing step (child creation, dep wiring, description/comment updates):
  ```python
  from shared.status_lifecycle import StatusLifecycle
  StatusLifecycle.ensure_claimed(work_item_id)   # idempotent re-claim
  StatusLifecycle.require_claimed(work_item_id)  # guard — fails closed if still open
  ```
  The item must be `in_progress` continuously from approval through completion — never `open` in that window.

Never leave an item in `in_progress` when control returns to the operator — an orphaned `in_progress` item is invisible to `wl next` and blocks downstream work.

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

2. Parse the JSON result. Expected keys: `target_id` (the id), `decision`
   (`"skip"` — below threshold, planning not needed; `"plan"` — above
   threshold, planning required; `"error"` — could not fetch; NO writes made
   in this case), `effort` (t-shirt size when determinable), `risk` (level
   when determinable), `error` (only when `decision == "error"`).

3. Act on the decision:

   - **If `decision == "skip"`**: the item is small enough to implement
     directly. Before marking `plan_complete`, run the six automated review
     stages (see **Automated review on existing content** below) against the
     existing content (description and any existing children). After the
     review stages complete and any clearly-needed gaps are addressed
     (conservatively — only unambiguous fixes), output a summary of what
     each stage checked/found, mark `plan_complete`, and record a summary
     comment:

     ```python
     StatusLifecycle.update_status(work_item_id, "open", stage="plan_complete")
     ```
     ```bash
     wl comment add <work-item-id> --author "plan" --comment "Auto-plan completed with review: effort and risk below threshold. Review summary: [summarise what each stage checked and any changes made]" --json
     ```

   - **If `decision == "error"`** (could not fetch; NO writes made) or **the
     CLI fails** (non-zero exit, invalid JSON): default to full planning as a
     safety measure — proceed to the Process steps and log a warning.
   - **If `decision == "plan"`**: proceed to the Process steps below.

4. **Idempotence**: stage already `plan_complete` or later → pre-check returns
   `decision: "skip"` and exits with the existing stage preserved (warning
   comment added).

## Plan-approval gate (Process step 4)

Before asking the user to approve a proposed feature plan (Process step 4),
run the approval-gate check:

```bash
python3 ./plan_helpers.py plan-approval-gate <work-item-id>
```

Expected keys: `request_approval` (`true`/`false`) and `reason`. The gate
requests approval when effort is Medium/Large/Extra Large **OR** risk is
Medium/High; it skips when effort is Extra Small/Small **AND** risk is Low.
Absent effort/risk → default conservatively to requesting approval (a human
checkpoint is never silently skipped).

- **`request_approval == true`**: present the plan and ask the user to
  accept, edit, reorder, or split/merge — iterating until approved. State
  the reason explicitly (e.g. "This plan requires your confirmation because
  its effort is Large scale and its risk is Medium.").
- **`request_approval == false`**: do NOT ask; proceed directly to step 5
  (vertical slice verification) and step 6 (automated review stages).

## Automated review on existing content (auto-complete path)

When the pre-check returns `decision: "skip"`, the skill runs the six review
stages against the existing work item content (description + any children).
Unlike Process step 6 (which reviews a freshly generated plan), this path
reviews existing content as-is. Each stage MUST: run sequentially in order;
operate on existing content; be conservative (only fix gaps that are clearly
needed and unambiguous); if an automated improvement could change intent, do
NOT apply it — record an Open Question and continue. After each stage, output
exactly: "Finished <Stage Name> review: <brief notes of improvements>".

Review stages:

1. **Requirements & AC alignment review** — verify ACs faithfully match the
   requirements/use cases (each requirement has an AC; each AC traces back).
   Flag missing/contradictory/invented ACs; add only where intent is clear,
   otherwise record an Open Question.
2. **Completeness review** — ensure required fields (description, ACs) and
   existing children are complete. If a ``**Key Files:**`` section exists,
   validate syntactic validity (each path has ≥1 ``/`` and a file extension;
   ``validate_key_files_format()``), completeness (missing files vs scope),
   and accuracy (irrelevant files); reflect corrections before plan completes.
3. **Sequencing & dependencies review** — dependencies coherent; ensure
   test tasks appear before implementation tasks.
4. **Scope sizing review** — features sized as deliverable increments
   (no-op if no children).
5. **Acceptance & testability review** — ACs pass/fail and testable; improve
   vague criteria where intent is clear.
6. **Polish & handoff review** — description clear, well formatted, actionable.

After all six stages, run the AC coverage review:

```python
from skill.shared.tree_coverage import run_coverage_review
review = run_coverage_review(<work-item-id>)
```

- If ``recommendation == "proceed"`` → mark `plan_complete`.
- If ``recommendation == "auto_close"`` → close gaps, record comment, mark `plan_complete`.
- If ``recommendation == "stop"`` → **do NOT mark `plan_complete`**; leave the item `open` with a comment describing conflicts.

Then output a summary of what each stage checked/found.

## Process (must follow)

0. Tree iteration (when children exist)

   If the work item already has children, iterate the entire subtree **before** any other processing:

   - Run `wl show <work-item-id> --children --json` to fetch existing children.
   - Order children by dependency edges using `wl dep list <id> --json` (topological order, ties broken by listed order).
   - For each child (in dependency order), recurse: if that child has its own children, process them first.
   - Use the shared tree-coverage helper from `../shared/tree_coverage.py`:
     ```bash
     python3 ./tree_coverage.py run-coverage-review <work-item-id>
     ```
     Or import and call `run_coverage_review(<work-item-id>)` directly.
   - If the coverage review returns `recommendation: "stop"` with unresolvable conflicts,
     record the conflicts as a comment and **do not advance the stage** — leave the item `open`.
   - If the coverage review returns `recommendation: "auto_close"`, apply the auto-closed gaps
     and note them in a comment.
   - If the coverage review returns `recommendation: "proceed"`, continue to Step 1.
   - Idempotence: re-running must not create duplicate children or duplicate comments.

   This step ensures every node in the tree has been planned/processed and that parent ACs
   are collectively covered by their children.

1. Evaluate whether planning is required (agent responsibility)

   Before the full interview, assess if the item already has a sufficient plan:
   - If `stage` is `plan_complete` or later → no-op skip.
   - If not an `epic` and description has measurable ACs and a minimal implementation sketch → mark complete.
   - If existing child items already cover the scope (`wl list --parent <id> --number 1 --json` existence check) → skip.
   - If a concise plan block exists → treat as sufficient.

   If planning is not needed:
   - `StatusLifecycle.update_status(<work-item-id>, "open", stage="plan_complete")`
   - Add comment: `wl comment add <work-item-id> "Plan auto-complete: sufficiently sized/defined for direct implementation." --actor Map --json`

   When borderline, err toward auto-complete. Only fall back to clarifying questions when decomposition is clearly needed.

2. Fetch & summarise (agent responsibility)

   - Run `wl show <work-item-id> --json` and summarise: title, type, headline, existing children, plan info.
   - Validate readiness by `stage`: `intake_complete` → ready for planning; `plan_complete` or later → skip, record a no-op comment; other → check Step 1 heuristics before asking. Only ask the operator when heuristics genuinely cannot determine.
   - Read any linked PRD for key details.
   - Derive 3-6 keywords from title/description to search for related work; present likely duplicates or relationships.

3. Interview

   In iterations (≤3 questions each), gather the minimum information for an actionable plan. Per feature capture: **Target outcome**, **Definition of done** (pass/fail checks + automated tests), **Constraints** (performance, compatibility, rollout, timeline), **Risky assumptions** (where a prototype is needed and what "success" means). Iterate until the breakdown is clear. Review existing Appendix entries first — don't re-ask answered questions.

   **Producer review:** When the agent cannot proceed without producer input (clarifying questions unanswered, critical information missing), mark the work item as needing producer review. **Handoff-only:** only release to `open` when the session genuinely ends and waits for an open-ended producer reply; if the run will resume in-session (e.g. approval pane), **hold `in_progress`** or re-claim via `StatusLifecycle.ensure_claimed` immediately on resume before any mutation (see Status lifecycle). A downtime run with no interactive approver must hold or abort — never release to `open` mid-run:

   ```bash
   wl reviewed <work-item-id> true
   ```

   This flags the item so the producer knows attention is required. The agent should STOP and wait for the producer's response. Once answers are received, re-claim (`StatusLifecycle.ensure_claimed`) then continue the interview or proceed.

4. Propose feature plan (agent responsibility + user confirmation)

   Produce a draft plan (guide: 3-12 features) where each feature includes: **Short Title** (≤7 words) | **Summary** (one sentence) | **Acceptance Criteria** (2-6 measurable bullets) | **Minimal Implementation** (2-6 bullets, smallest end-to-end slice) | **Prototype/Experiment** (optional; success thresholds) | **Dependencies** | **Deliverables**.

   - **Test-first ordering**: test/verification features before implementation features.
   - **Approval gate**: run the approval-gate check first (see **Plan-approval gate**). When approval IS requested, state the reason explicitly and iterate until approved. When NOT warranted (effort Extra Small/Small AND risk Low), proceed directly to steps 5-6 without an approval pause. **On any approval resume, re-claim first:** `StatusLifecycle.ensure_claimed(<id>)` + `StatusLifecycle.require_claimed(<id>)` before steps 5-7; downtime dispatch with no approver holds `in_progress` or aborts — never releases to `open`.

5. Verify vertical slice phasing (agent responsibility)

   Ensure each phase is a vertical slice through ALL layers (code, tests, docs, infra, observability). If a phase is horizontal, ask the user to refactor. Include between-phase guidance: review next items, update descriptions. If the item is small enough to not require phasing, document the decision and proceed.

6. Automated review stages (must follow; no human intervention required)

   After the user approves the feature list, run six review iterations. Each review MUST provide a new draft if changes are recommended, then output exactly: "Finished <Stage Name> review: <brief notes of improvements>".

   - Run without human intervention, sequentially; improvements conservative and scoped; if a change could alter intent, do NOT apply it — record an Open Question and continue.

   1. **Requirements & AC alignment review** — verify each feature's ACs match its requirements/use cases; every AC traces back; flag missing/contradictory/invented ACs; add only where intent is clear.
   2. **Completeness review** — every feature has all required fields; validate ``**Key Files:**`` (syntactic validity via ``validate_key_files_format()``, completeness, accuracy); corrections reflected in descriptions.
   3. **Sequencing & dependencies review** — dependencies coherent and actionable; ensure test tasks appear before implementation tasks.
   4. **Scope sizing review** — features sized as deliverable increments.
   5. **Acceptance & testability review** — ACs pass/fail and testable.
   6. **Polish & handoff review** — plan copy-pasteable and easy to execute.

   **AC coverage review (final step):**

   After the six review stages, run the AC coverage review using the shared helper:

   ```python
   from skill.shared.tree_coverage import run_coverage_review
   review = run_coverage_review(<work-item-id>)
   ```

   - If ``recommendation == "proceed"`` → all parent ACs are covered; continue to Step 7.
   - If ``recommendation == "auto_close"`` → unambiguous gaps are identified; close them by adding the missing child ACs (where intent is clear), record a comment noting what was auto-closed.
   - If ``recommendation == "stop"`` → unresolvable conflicts exist; **do NOT advance the stage**. Leave the item `open` with a comment describing the conflicts and which parent ACs are uncovered.

7. Update work items (agent)

   - **Guard (SA-0MTFTFUIH000UWM9):** before any mutation in this step, assert `StatusLifecycle.require_claimed(<work-item-id>)` — fails closed if the item is `open` (the 2026-08-30 gap left AH-0MTFPDKDU006QUDC `open` while creating 3 children). On in-session approval resume, `StatusLifecycle.ensure_claimed(<work-item-id>)` first.
   - **Test-first creation**: create test/verification items before implementation items.
   - Create child work items: `wl create --title "<Short Title>" --description "<Full description>" --parent <work-item-id> --priority P2 --stage intake_complete --json`
   - Add dependency edges: `wl dep add <DependentId> <PrereqId>`
   - Ensure idempotence: if a child with the same canonical name exists, reuse it.
   - Add completion comment: `wl comments add $1 "Planning Complete. <Summary>" --actor <agent> --json`
   - Update stage: `StatusLifecycle.update_status(<work-item-id>, "open", stage="plan_complete")`

8. Calculate Effort and Risk (agent responsibility; must follow)

   - Call the `effort_and_risk` skill with the new or updated work item to produce an effort and risk estimate.

## Traceability & idempotence

- Re-running this skill must not create duplicate child work items or duplicate plan blocks.
- If changes are made, include a "Plan: changelog" block in the parent work item summarising actions and timestamps.

  > **Note:** This changelog block is for **work-item-level traceability** — it is **not** the repository `CHANGELOG.md`, which is managed automatically by the ship skill's release pipeline.

## Editing rules & safety

- Preserve author intent; when uncertain, create an Open Question entry rather than assuming.
- Keep changes minimal and conservative. Respect `.gitignore`.
- **Worklog validation**: `feature` or `task` work items must include a `## Acceptance Criteria` section.
- **JSON parsing**: `wl ... --json` output may be an object or an array; handle both shapes.
- If any automated step fails or is ambiguous, surface an Open Question and pause for guidance.

## 8. Finishing (must do as the final step only)

- Set stage to `plan_complete` and status to `open`: `StatusLifecycle.update_status(<work-item-id>, "open", stage="plan_complete")`
- Run `wl sync` to sync changes.
- Run `wl show <work-item-id>` (not --json) to display the work item.
- End with: "This completes the Plan process for <work-item-id>".

## Bundled Resources

- `plan_helpers.py` — Shared autoplan decision module. CLI entry points: `plan-if-needed`, `check-effort-risk`, `plan-approval-gate` (see usage above); importable as a Python module. Full import list: [docs/dev/plan-skill-reference.md](../../docs/dev/plan-skill-reference.md).

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


## Final step: standardized end-of-session report

Render the canonical end-of-session report (helper: [`../report/SKILL.md`](../report/SKILL.md)) as the **last step**, replacing any ad-hoc end-of-session summary:

```bash
python3 $(skill_path report)/scripts/render_report.py <work-item-id> \
  --skill-name <skill_name> \
  --headline "<1-3 sentence headline summary>" \
  --ac "<AC# description>|<verification metric>|met" \
  --ac "<...>|<...>|unmet" \
  [--producer-actions "<actions for the producer, or omit for 'None needed'>"] \
  [--notes "<freeform context/caveats/assumptions>"] \
  [--next-action <review|plan|implement|...>]
```

The script prints the rendered report to stdout — **paste it verbatim into
your final response**, so the operator sees the report itself (not just the
tool call), then close with: `<work-item-id>: <one-line summary>`. Do NOT
re-summarize the report in a different format — the report is the summary. When the session ends in a terminal state with no open questions for the operator, end your final response with `</end_session>` on its own line as the very last line after the summary; if the session ends with questions for the operator, do not emit the marker.
