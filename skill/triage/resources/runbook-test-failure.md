# Runbook: Test Failure Triage

Purpose
-------
Guidance for triaging critical `test-failure` work items created by agents.

Owner inference
---------------
1. Check `.opencode/triage/owner-map.yaml` for overrides.
2. If not present, prefer CODEOWNERS if available.
3. Otherwise run `git blame <file>` and use recent commit authorship.
4. If confidence is low, assign to `Build` and request human triage.

Triage steps
------------
1. Re-run the failing test locally or in CI to verify reproducibility.
2. If flaky, tag with `flaky` and add to flaky-test triage queue.
3. If reproducible, add detailed reproduction steps and set assignee.
4. If the issue blocks active work, PM/triage should coordinate assignment per PM periodic check.

PR unblock guidance
-------------------
- Agents that discover a NEW failing test should block creating PRs for their current work item until the created critical issue is addressed or the PR references and closes the issue.
- Pre-existing critical issues do not block PR creation for unrelated agent work.

Unblocking by humans
--------------------
- If a human determines the match is a false positive, they should add a comment explaining why and close or retag the issue. Agents follow the issue state and may resume PR creation once the issue is closed or retagged.

Notes
-----
- Keep the issue body concise and include exact commands to reproduce the failure.
- Preserve original agent-submitted evidence; append new information as comments rather than overwriting.
