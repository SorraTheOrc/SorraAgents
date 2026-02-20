---
name: triage
description: Triage workflows and helpers for test-failure detection and critical issue creation. Provides a skill to search for or create critical `test-failure` work items and related resources.
---

Purpose
-------
Provide a deterministic helper for agents that detect failing tests they do not own. The skill's canonical function is `check_or_create_critical_issue` which searches Worklog for matching incomplete critical issues and creates a new one using the repository template when none exists.

When to use
-----------
- When an agent observes a failing test during implementation that appears to originate outside of the agent's current change set.

Inputs
------
- failure_signature: { test_name, stdout_excerpt, stack_trace?, commit_hash?, ci_url? }

Outputs
-------
- A JSON object: { issueId, created: true|false, matchedId?: id, reason: string }

References
----------
- Templates: `skill/triage/resources/test-failure-template.md`
- Runbook: `skill/triage/resources/runbook-test-failure.md`

Scripts
-------
- `scripts/check_or_create.py` — implementation using `wl` CLI via the local WL adapter.

Behavior
--------
- Prefer conservative matches: if any incomplete (open or in_progress) `test-failure` issue matches the test name (title or body), return the existing issue id.
- If no match is found, create a new `critical` work item using the template, attach evidence, and return the new id.
- When enhancing an existing issue, do not overwrite existing fields — add a comment with new evidence instead.

Telemetry
---------
- Emit events: `triage.issue.matched`, `triage.issue.created`, `triage.issue.enhanced`.

Examples
--------
Calling the script with a JSON payload should return the structured result and print JSON to stdout.
