# Plan skill — implementation reference

Deep implementation-reference detail relocated from `skill/plan/SKILL.md`
(relocation tracked by SA-0MSLK7SAE0032V9K). The SKILL.md is the agent-facing
operational brief; this document preserves the full implementation reference
for maintainers. Workflow semantics are unchanged.

## Interview conduct

Interview conduct is defined once in the canonical shared interview skill
(`$(skill_path interview)/SKILL.md`); see
[interview-skill-reference.md](interview-skill-reference.md) for the
maintainer summary. `skill/plan/SKILL.md` § Interview delegates to it and
retains only plan-specific interview inputs (target outcome, definition of
done, constraints, risky assumptions). The shared rules are not restated here.

## plan_helpers.py — full import list

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

## Original Bundled Resources section (full)

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

## Plan-approval gate semantics

`should_request_plan_approval(work_item)` implements an **AND** gate: the plan
skill asks the producer to approve a proposed feature plan only when the work
item is both the largest and the highest-risk — effort t-shirt **Extra Large**
(`PLAN_APPROVAL_EFFORT`) **AND** risk **High** or **Severe**
(`PLAN_APPROVAL_RISK`). Every other effort/risk combination proceeds directly
to the automated review stages without an approval pause. Absent effort and/or
risk values, and present-but-unrecognised values (outside the canonical `wl`
effort/risk vocabulary), default conservatively to requesting approval, so a
human checkpoint is never silently skipped. The returned `reason` names both
the effort scale and the risk level that triggered the gate
(SA-0MUYG0HE6001YTEM, SA-0MUIUTJJS008W06Y).

## Autoplan effort/risk derivation and unknown fallback

The autoplan pre-check (`run_effort_and_risk` / `make_autoplan_decision`)
never posts the historical hard-coded placeholder payload
(`o=m=p=0`, certainty 100). That payload aggregated into
`Extra Small`/`Low` (0.00h) and made `plan-if-needed` return `skip` for
**every** item — silently skipping planning for epics (SA-0MW0LPXXY0002CQ0).

Instead:

- **Derive from children.** `run_effort_and_risk` fetches the item and its
  children (`wl show <id> --children --json`). Each child with a stored
  `effort` t-shirt label contributes a non-zero WBS row, with hours taken
  from the effort-and-risk thresholds (`Extra Small` 2h, `Small` 14h,
  `Medium` 52h, `Large` 160h, `Extra Large` 240h). Child and item `risk`
  labels are mapped to conservative probability/impact pairs for the risk
  aggregation. When no risk label exists, risk defaults to `Medium` — never a
  confident `Low` — and the assumption is recorded in the estimate output.
- **Unknown when there are no real inputs.** If no child carries an effort
  label (or the item cannot be read), `run_effort_and_risk` returns `None`
  **without invoking the orchestrator**. `make_autoplan_decision` then returns
  `do_plan=True` / stage `plan_complete`, leaves the item's `effort`/`risk`
  untouched, and posts **no** autoplan-decision comment. No
  `autoplan-decision-hash` marker is written, so a later run can still compute
  a genuine estimate. The observable decision vocabulary stays
  `skip` / `plan` / `error`; `unknown` is expressed as `plan`.
- **Defence in depth.** `orchestrate_estimate.py` refuses an all-zero
  `o=m=p` payload (exit code 6, explicit `error`) before any `wl update`, so a
  zero-derived `Extra Small`/`Low` can never be persisted by any caller.

## AC coverage review: triage-generated children are excluded

The AC coverage review (`run_coverage_review` / `apply_coverage_review` in
`skill/shared/tree_coverage.py`) compares a parent's acceptance criteria (ACs)
against the ACs of its direct children. Direct children that were auto-created
by the **triage** skill are excluded from that comparison.

**Marker.** The marker is the `test-failure` **tag** set by
`skill/triage/scripts/check_or_create.py` (`--tags test-failure`). The
`[test-failure]` title prefix is not the marker, and neither is the child's
status.

**Behaviour.**

- A triage-generated child is not passed to `compute_coverage`; coverage is
  computed from the remaining (evaluated) children only.
- Excluded children stay visible for the audit trail: they are listed in
  `child_summary` with `excluded: true` (and `ac_count: 0`) and in the
  top-level `excluded_children` list (`id` / `title` / `reason`).
- When **every** direct child is excluded there is no decomposition left to
  check, so coverage is vacuously complete and the review recommends
  `proceed` — the same outcome as a parent with no children. This is what
  lets a parent whose only children are reparented triage items reach
  `stage: plan_complete`.
- Tag-less children keep today's behaviour exactly: the same Jaccard
  thresholds (0.4 coverage, 0.2 auto-close floor) and the same `stop`
  semantics, so genuine decompositions stay gated.
- **Closed children.** A child is exempt only when it also carries the
  `test-failure` tag. A closed tag-less feature child still takes part in the
  comparison, so closing an uncovered feature child cannot weaken the gate.

**Rationale.** Triage children are incident fixes re-parented onto the item
that owns the flake (the documented triage practice). Their ACs come from a
boilerplate `[test-failure]` template ("the test passes on latest `dev`",
"all related documentation is updated", "full project test suite passes") and
are not a decomposition of the parent's ACs. Comparing them against the
parent made the coverage review return `stop`, so the item could never reach
`plan_complete` (observed on AH-0MVWCFS7K000RIWI). Treating the
`test-failure` tag as a non-decomposition marker fixes the false block while
leaving the gate intact for real feature decompositions
(AH-0MVWNR4XF007FX5T).

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
