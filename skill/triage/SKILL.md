---
name: triage
description: Triage workflows and helpers for test-failure detection and critical issue creation. Provides a skill to search for or create critical `test-failure` work items and related resources.
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
- `repo_path` (default `.`), `file_path` — for owner inference

Outputs
-------

`{ issueId, created: bool, matchedId?: id, reason: string }`

References
----------

- Template: `./resources/test-failure-template.md`
- Runbook: `./resources/runbook-test-failure.md`
- Owner inference: `../owner-inference/SKILL.md`

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
- No match: create new `critical` issue from template, infer owner via owner-inference skill
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
  "commit_hash": "abc123",
  "file_path": "tests/test_example.py"
}
JSON
python3 ./scripts/check_or_create.py payload.json
```

Output (new issue): `{"issueId": "SA-NEW", "created": true, "reason": "No matching incomplete test-failure issue found; created new."}`

Output (matched): `{"issueId": "SA-EXISTING", "created": false, "matchedId": "SA-EXISTING", "reason": "Matched existing test-failure issue by test name."}`
