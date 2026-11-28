# Runbook: Test Failure Triage

Purpose
-------

Guidance for triaging critical `test-failure` work items created by agents.

Owner assignment
----------------

New test-failure issues are deterministically assigned to `Build` (the previous owner
heuristics were retired). Route to humans via the normal PM/triage check when the
issue needs an owner decision.

Triage steps
------------

1. Re-run the failing test locally or in CI to verify reproducibility.
2. If flaky, tag with `flaky` and add to flaky-test triage queue.
3. If reproducible, add detailed reproduction steps and set assignee.
4. If the issue blocks active work, PM/triage should coordinate assignment per PM periodic check.

Environmental failure signatures
---------------------------------

Some failures are caused by the host, not the tested code. Recognise and treat them as environment issues rather than product regressions:

- **`Unknown system error -122` (errno EDQUOT) / `ENOSPC` on `writeFileSync` or `mkdtempSync`** — the OS temp dir is a tmpfs with a per-user quota (`/tmp` on this host). Leaked test harness sandboxes (see test-writing-guidelines anti-pattern 9) and other projects' artefacts accumulate until the quota is exhausted; a later suite run then fails with this opaque error on an arbitrary test that happens to write next.
  - Verify: `df -h /tmp` (check the `Use%`), and look for the failing stack pointing at a temp-dir `writeFileSync`/`mkdtempSync`
  - Remediation: free space under `/tmp` (remove stale test artefacts, e.g. `find /tmp -maxdepth 1 -type d -mtime +7 -exec rm -rf {} +` run by an operator) and **fix the leaking harness** so it removes its temp dirs (route via the shared `tests/helpers/temp-dir.mjs` helper). Not a code regression — do not treat `-122` as a product defect.
  - Reference incidents: SA-0MVTSV5SO008625I and SA-0MVTSV8GO005KZ5V (2026-10-29). A single quota exhaustion surfaced as **several** failing node tests in the same full-suite run, all in the `run-release` harness family (`run-release: --help is side-effect free`, `run-release: post-release gated on a newly created tag`), because whichever harness wrote next hit the quota. Treat a cluster of unrelated node failures in one run as one environmental incident, not several product regressions.

Gate/cache artefacts
--------------------

A gate/cache artefact is a **refusal that is not a test regression**. The most common one is the post-push verification's
`verdict=missing`: after a successful push, `implement.py finish` reads the full-suite cache for the pushed commit's
tree and finds **no valid entry** (absent, expired, unkeyable, or a bypassed pre-push gate), so it cannot prove a green
verdict and refuses the terminal transition (fail-closed by design). No test failed — the suite simply was not recorded
against that tree.

- **Signature:** `post-push full-suite verification: no valid full-suite cache entry` (reported as
  `Finish refused: post-push verification is missing.`).
- **Possible causes:** the pre-push gate was bypassed (`WORKLOG_SKIP_PRE_PUSH=1`), the cache key ignores worktree/
  environment identity (SA-0MVCTZRZD004CE3L), the pre-push gate verdict is not keyed on the pushed tree
  (SA-0MVXUCGPK003G2KV), or an entry expired.
- **Filed as:** a non-critical `[gate-artefact] …` item tagged `gate-artefact` — **never** a critical
  `[test-failure] … — failing test`. Its acceptance criteria are agent-satisfiable (populate the cache, re-run finish).
- **Remediation (agent-satisfiable):** run the full suite through the test skill, then re-run `finish`:

  ```
  python3 skill/test/scripts/run_tests.py --scope full
  python3 skill/implement/scripts/implement.py finish <work-item-id>
  ```

  The no-diff finish path converges: once a valid green entry exists for the pushed tree, the verification passes and the
  item advances. No source change is implied, so do not look for a failing test or a product defect.
- **Dedupe:** an identical signature is matched across **all priorities** and both triage tags, so a recurring cache miss
  adds a comment to the existing item instead of filing a new one (SA-0MWOTZ17K005RH8P).
- **Contrast with a real failure:** a `verdict=red` refusal names the failing test from the cached suite output and files
  a **critical** `[test-failure]` item — treat that as a genuine regression.

PR unblock guidance
-------------------

- Agents that discover a NEW failing test should block creating PRs for their current work item until the created critical issue is addressed or the PR references and closes the issue.
- Pre-existing critical issues do not block PR creation for unrelated agent work.

Unblocking by humans
--------------------

- If a human determines the match is a false positive, they should add a comment explaining why and close or retag the issue. Agents follow the issue state and may resume PR creation once the issue is closed or retagged.

Sample `wl create` invocation
-----------------------------

```bash
wl create \
  --title "[test-failure] test_scheduler_heartbeat — AttributeError in heartbeat loop" \
  --description "## Failure Signature

- Test name: test_scheduler_heartbeat
- Failing commit: abc1234 (if available)
- CI job: (not available)

## Evidence

- Short stderr/stdout excerpt (first 1k characters):

\`\`\`
AttributeError: 'NoneType' object has no attribute 'send_heartbeat'
\`\`\`

## Steps To Reproduce

1. Checkout the commit: git checkout abc1234
2. Run the failing test: pytest -q -r a --disable-warnings -k \"test_scheduler_heartbeat\"
3. Capture full logs and attach to the work item

## Impact

Blocks scheduler test suite; does not affect other test modules.

## Suggested Triage Steps

1. Verify flakiness: rerun CI/test locally once.
2. If reproducible, assign to `Build` and route for triage.
3. If flaky, tag flaky and route to flaky-test queue.

## Links

- Runbook: skill/triage/resources/runbook-test-failure.md" \
  --priority critical \
  --tags "test-failure" \
  --json
```

Notes
-----

- Keep the issue body concise and include exact commands to reproduce the failure.
- Preserve original agent-submitted evidence; append new information as comments rather than overwriting.
