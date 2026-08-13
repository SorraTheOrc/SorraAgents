# Plan skill — implementation reference

Deep implementation-reference detail relocated from `skill/plan/SKILL.md`
(relocation tracked by SA-0MSLK7SAE0032V9K). The SKILL.md is the agent-facing
operational brief; this document preserves the full implementation reference
for maintainers. Workflow semantics are unchanged.

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
