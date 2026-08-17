---
name: triage
disable-model-invocation: true
description: "Triage test failures: search or create critical test-failure work items. Use when triaging failing tests."
---

Purpose
-------

Deterministic helper for agents that detect failing tests they do not own. Searches Worklog for matching incomplete `test-failure` issues or creates one using the repository template.

When to use
-----------

When an agent observes a failing test outside its current change set.

Inputs
------

JSON payload (flat or under `failure_signature`):

- `test_name` (required) — failing test name
- `stdout_excerpt`, `stack_trace`, `commit_hash`, `ci_url` — optional context
- `parent_work_item_id` — optional; creates the test-failure item as a child that blocks this work item

Outputs
-------

`{ issueId, created: bool, matchedId?: id, reason: string }`

References
----------

- Template: `./resources/test-failure-template.md`
- Runbook: `./resources/runbook-test-failure.md`
- Test-writing anti-patterns to avoid when creating test-failure work items:
  [Test Writing Guidelines](../shared/test-writing-guidelines.md)

Script
------

`./scripts/check_or_create.py` — implementation using `wl` CLI.

Matching Heuristics (in order)
------------------------------

1. **Exact test name** in title/body of incomplete `test-failure` issue
2. **Token overlap + stacktrace** — title tokens match AND stacktrace top-frame in issue body
3. **Commit hash or CI URL** present in issue

If multiple candidates, prefer most recently updated.

Behavior
--------

- Conservative matching: return existing issue id if any heuristic matches
- No match: create new `critical` issue from template with assignee `Build`
- Prefer quiet test commands (`pytest -q` / `npm --silent test`) for local reproduction
- Enhance existing issues by adding comment with new evidence (don't overwrite fields)

Telemetry: emits `triage.issue.created` / `triage.issue.enhanced` to stderr.

Examples
--------

```bash
cat <<'JSON' > payload.json
{
  "test_name": "tests/test_example.py::test_failure",
  "stdout_excerpt": "AssertionError: expected 1 but got 0",
  "stack_trace": "...",
  "commit_hash": "abc123"
}
JSON
python3 ./scripts/check_or_create.py payload.json
```

Output (new issue): `{"issueId": "SA-NEW", "created": true, "reason": "No matching incomplete test-failure issue found; created new."}`

Output (matched): `{"issueId": "SA-EXISTING", "created": false, "matchedId": "SA-EXISTING", "reason": "Matched existing test-failure issue by test name."}`


## Final step: standardized end-of-session report

Render the canonical end-of-session report (helper: [`../report/SKILL.md`](../report/SKILL.md)) as the **last step**, replacing any ad-hoc end-of-session summary:

```bash
python3 ../report/scripts/render_report.py <work-item-id> \
  --skill-name <skill_name> \
  --headline "<1-3 sentence headline summary>" \
  --ac "<AC# description>|<verification metric>|met" \
  --ac "<...>|<...>|unmet" \
  [--producer-actions "<actions for the producer, or omit for 'None needed'>"] \
  [--notes "<freeform context/caveats/assumptions>"] \
  [--next-action <review|plan|implement|...>]
```

Then close with: `<work-item-id>: <one-line summary>`. Do NOT re-summarize the report in a different format — the report is the summary.
