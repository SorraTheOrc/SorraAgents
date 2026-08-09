---
name: test
description: "Run the full test suite in quiet mode; triage failures into critical work items. Use when: 'run all tests'."
---

Purpose
-------

Provide a deterministic, repeatable loop for verifying "the full project test
suite passes" before marking any work item `in_review`. Every failing test is
either fixed, removed (if useless), or escalated with a clear justification.

Inputs
------

- Optional: a work-item id to attach triaged `test-failure` children to
  (`parent_work_item_id`).
- Optional: `--rerun-failures` to verify flakiness before triage.

Outputs
-------

- Structured per-failure records (JSON) from the runner.
- Critical `test-failure` work items created or linked via the triage skill's
  `check_or_create.py` (no duplicates for the same test).
- A green full test suite, or a report to the operator of items requiring
  explicit permission / escalation.

References
----------

- Runner: `./scripts/run_tests.py`
- Usefulness evaluator: `./scripts/evaluate_usefulness.py`
- Triage helper (reused): `../triage/scripts/check_or_create.py`
- Quiet test-command canonicalization: `../test_runner.py`
- Test anti-patterns for usefulness evaluation:
  [Test Writing Guidelines](../shared/test-writing-guidelines.md)

Workflow
--------

### 1. Run the full suite in quiet mode

```bash
python3 ./scripts/run_tests.py --json
```

The runner executes, in quiet mode:

- **pytest**: `pytest -q -r a --disable-warnings` (canonicalized via
  `canonicalize_quiet_test_command` from `../test_runner.py`)
- **Node**: `node --test "tests/node/**/*.mjs" "tests/cli/**/*.mjs" "tests/unit/**/*.mjs"`
  (or `npm --silent test` per suite directory when an npm test script exists).
  Glob patterns are required — on node v22.22.1 `node --test <dir>` treats a
  bare directory as a module entry point and fails with `MODULE_NOT_FOUND`
  (see SA-0MSF8KNE3003JDVD).

Output is a JSON document with per-suite results and a flat `failures` array;
each failure record carries `test_name`, `stdout_excerpt` and `stack_trace`.

### 0. Cached execution (default)

`run_tests.py` caches each suite run per-repo by default (see
[`test_cache.py`](../test_cache.py)): re-running the same command at the
same git state within the **2-hour TTL** is served from cache **without
re-executing the suite**. This prevents agents from burning 1.5–7 minutes
re-running an identical suite just to extract summary lines.

- **Storage**: `<repo>/.worklog/cache/` (fallback `<repo>/.git/test-cache/`,
  resolved worktree-aware). Gitignored; never committed.
- **Invalidation**: git-state fingerprint (HEAD sha + working-tree changes) +
  2-hour TTL. A changed tree, an expired TTL, or a corrupt entry triggers a
  fresh run that replaces the stale entry. `--force` bypasses lookup (still
  stores); `--no-cache` bypasses lookup and storage entirely.
- **Pipeline normalization**: output-filtering pipelines (e.g.
  `npm test 2>&1 | grep -E "Test Files|failed"`, `| tail -30`, `| head`,
  `| tee`) normalize to the underlying run and share one cache entry.
- **Visibility**: non-JSON output marks cache hits with `[cached]`.

Query a cached run without executing anything:

```bash
# Print summary lines (Test Files / Tests / failed / passed) from the cache
python3 ./scripts/run_tests.py --summary --suite all

# Custom grep against cached output (read-only; never executes the suite)
python3 ./scripts/run_tests.py --summary --summary-grep "Test Files|failed"

# Force a fresh full run, refreshing the cache
python3 ./scripts/run_tests.py --force
```

Agents that only need summary information (e.g. release verification,
read-only audits) should use `--summary` instead of re-running the suite.

The **audit skill** consumes the cache directly (read-only, via
`query_cached()`): when the full suite was run green at the audited git state
within the TTL, the audit runner automatically verifies execution-dependent
acceptance criteria (e.g. "full project test suite passes") without any
operator attestation or re-execution (SA-0MSIU5HFI0024D7W).

### 2. Triage every failure

For each failure record, invoke the triage helper to create or link a critical
`test-failure` work item (no duplicates):

```bash
python3 ../triage/scripts/check_or_create.py '{"test_name":"<test_name>", "stdout_excerpt":"...", "stack_trace":"...", "parent_work_item_id":"<current-id>"}'
```

- If no `test-failure` items exist after triage and the suite is green, the
  loop ends.
- Each item is tagged `test-failure`, priority `critical`, created as a child
  of the invoking work item when a parent id is provided.

### 3. Evaluate usefulness of each failing test (code-path analysis)

Do **NOT** rely on code comments when deciding whether a test is useful.
Perform code-path analysis — what the test actually asserts and exercises —
using the evaluator:

```bash
python3 ./scripts/evaluate_usefulness.py <test-file> --json
```

The evaluator detects the anti-patterns from the
[Test Writing Guidelines](../shared/test-writing-guidelines.md):

1. **Source-code-grep tests** — reads source file text and asserts a string on it
2. **Placeholder tests** — `expect(true).toBe(true)`, zero assertions, TODO-only bodies
3. **Self-referential simulations** — re-implements logic with no production imports
4. **Duplicate coverage** — same behaviour already covered via a public API (heuristic)

Verdicts are **conservative**:

- `remove` — only when a hard anti-pattern (grep / placeholder / zero-assertion
  / self-referential) is detected by code-path analysis
- `report-to-user` — uncertainty, weak assertions, or duplicate-coverage
  heuristics. Never auto-remove on uncertainty.
- `keep` — no anti-pattern detected; the test appears useful.

### 4. Fix or remove, respecting change authorization

- **Remove** a test only when the evaluator returns `remove` with a hard
  anti-pattern reason, and only after recording the justification in the
  `test-failure` work item comment.
- **Test-code changes** are permitted only in response to a change in the main
  codebase and must be justifiable from test comments + code understanding.
  If justifiable, make the change; otherwise report to the user and leave a
  comment. Questionable changes require explicit user approval.
- **Project-code changes** are never made without explicit user permission.
  Explain why the change is needed, record the justification in comments, and
  obtain approval before editing non-test code.
- Ensure code comments in any retained test clearly indicate why the test is
  useful.

### 5. Loop until green

After fixing or removing failures, start again from step 1 (run all tests).
Repeat until the full suite passes.

### 6. Follow-up wiring item (do not duplicate)

When the loop completes (full suite green), reference the pre-created
top-level follow-up work item — wiring this `test` skill into all
code-touching skills and the master AGENTS.md:

- **SA-0MSAC1IAS007I3K8** — depends on this work item; the skill references
  it at loop completion instead of creating a duplicate.

Change-authorization policy
---------------------------

| Change type | Allowed when | Requires |
|-------------|--------------|----------|
| Remove useless test | Evaluator verdict `remove` (hard anti-pattern) | Justification comment on the `test-failure` item |
| Test-code change | Response to a main-codebase change; justifiable from comments + code | Comment recording rationale |
| Questionable test change | — | Explicit user approval |
| Project-code change | — | Explicit user permission + justification comment |

Escalation
----------

If the loop cannot reach green without a project-code change or a questionable
test-side change, stop and report to the operator with:

- the failing test(s),
- the evaluator verdict and reasoning,
- the exact change that would be needed and why,
- the work-item ids of the triaged `test-failure` items.

Scripts
-------

- `./scripts/run_tests.py` — runs pytest / Node suites in quiet mode
  and parses failures into triage-compatible records.
- `./scripts/evaluate_usefulness.py` — code-path usefulness analysis returning
  conservative verdicts (`keep` / `remove` / `report-to-user`).
- `../triage/scripts/check_or_create.py` — reused unchanged to create/link
  critical `test-failure` items.
