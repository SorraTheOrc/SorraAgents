---
name: test
description: "Run the full test suite in quiet mode; triage failures into critical work items. Use when: 'run all tests'."
---

Purpose
-------

Deterministic loop for verifying "the full project test suite passes" before
marking any work item `in_review`. Every failing test is fixed, removed (if
useless), or escalated with a clear justification.

**Part of the contract:** every run is cached per-repo (git state + 2h TTL) and
consumed read-only by the audit skill to auto-verify execution-dependent ACs —
the pre-`in_review` suite pass MUST therefore go through this skill. An ad-hoc
`npx vitest run`/`pytest` at the same commit does not populate the cache, so the
audit cannot auto-verify those ACs (it auto-executes the suite itself on a cache
miss — F3, SA-0MSTN5KRF0097TVP — or the operator attests with `--green-run HEAD`;
it never hard-blocks — F4, SA-0MSTN8CWM003AAU9).

Inputs
------

- Optional: work-item id for triaged `test-failure` children (`parent_work_item_id`); `--rerun-failures` checks flakiness before triage.

Outputs
-------

- Structured per-failure records (JSON).
- Critical `test-failure` items created/linked via the triage skill's `check_or_create.py` (no duplicates).
- A green full test suite, or a report of items requiring explicit permission.

References
----------

- Runner: `./scripts/run_tests.py` · Usefulness: `./scripts/evaluate_usefulness.py` · Triage: `../triage/scripts/check_or_create.py`
- Canonicalization: `../test_runner.py` · Anti-patterns: [Test Writing Guidelines](../shared/test-writing-guidelines.md)

Workflow
--------

### 1. Run the full suite in quiet mode

```bash
python3 ./scripts/run_tests.py --json
```

**Project-root resolution:** the runner targets the invoking project (see [docs/dev/test-skill-reference.md](../../docs/dev/test-skill-reference.md)).

**Suite-command resolution order (F2, SA-0MSTMYE79006NA61):** the full suite is `full_suite_commands(project_root)`, resolved in this order:

1. **`.pi/test-config.json` extension file** (repo root) — overrides convention detection entirely: `{"suiteCommands": ["pytest ...", "node --test ..."], "timeoutPerCommand": 600}`. Use it for bespoke suites (e.g. a monorepo package command) that conventions would miss.
2. **npm-test convention** — `npm --silent test` for repos whose `package.json` declares a `test` script (TCE-like layouts).
3. **pytest** — `pytest -q -r a --disable-warnings` (canonicalized via `canonicalize_quiet_test_command` from `../test_runner.py`) ONLY when the repo declares a pytest suite (`pytest.ini` / `[tool.pytest.ini_options]` / pytest-style `tests/**/test_*.py` files).
4. **Node suite dirs** — `node --test "tests/node/**/*.mjs" "tests/cli/**/*.mjs" "tests/unit/**/*.mjs"` (or `npm --silent test` per suite dir). Glob patterns required — node v22.22.1 rejects a bare dir (SA-0MSF8KNE3003JDVD). Only suite dirs that **exist** under the target repo are included — missing dirs are skipped (SA-0MSJELL44009XYIL), so a repo without `tests/node` never gets a guaranteed-failing phantom command.

An empty resolved set (no extension file, no npm test script, no pytest suite, no node dirs) is NOT an error — the runner reports zero commands and the audit skill treats the repo as execution-impossible (fail-open partial, never blocks — F4 AC2).

Output: JSON with per-suite results and a flat `failures` array (`test_name`, `stdout_excerpt`, `stack_trace`).

### 0. Cached execution (default)

`run_tests.py` caches each suite run per-repo — re-running the same command at
the same git state within the **2-hour TTL** is served from cache. Details:
[docs/dev/test-skill-reference.md](../../docs/dev/test-skill-reference.md).

Query a cached run without executing:

```bash
python3 ./scripts/run_tests.py --summary --suite all                         # summary lines
python3 ./scripts/run_tests.py --summary --summary-grep "Test Files|failed"  # read-only grep
python3 ./scripts/run_tests.py --force                                       # fresh run
```

The **audit skill** consumes the cache read-only via `query_cached()`: a green
full-suite run at the audited git state within the TTL auto-verifies
execution-dependent ACs (SA-0MSIU5HFI0024D7W). Failed (non-zero-exit) runs use
a short 5-minute TTL so transient infra failures are not re-served as current
results (SA-0MSJELL44009XYIL).

### 2. Triage every failure

For each failure record, invoke the triage helper to create or link a critical
`test-failure` work item (no duplicates):

```bash
python3 ../triage/scripts/check_or_create.py '{"test_name":"<test_name>", "stdout_excerpt":"...", "stack_trace":"...", "parent_work_item_id":"<current-id>"}'
```

Items are tagged `test-failure`, priority `critical`, child of the invoking
item when a parent id is provided. No items after triage + green suite → loop ends.

### 3. Evaluate usefulness of each failing test (code-path analysis)

Do **NOT** rely on code comments when deciding whether a test is useful —
analyze what the test actually asserts and exercises using the evaluator:

```bash
python3 ./scripts/evaluate_usefulness.py <test-file> --json
```

The evaluator detects the anti-patterns from the
[Test Writing Guidelines](../shared/test-writing-guidelines.md):
(1) source-code-grep tests, (2) placeholders (`expect(true).toBe(true)`,
zero assertions, TODO bodies), (3) self-referential simulations, (4) duplicate
coverage (already covered via a public API).

Verdicts are **conservative**: `remove` only on a hard anti-pattern;
`report-to-user` on uncertainty or duplicate-coverage heuristics (never
auto-remove on uncertainty); `keep` when no anti-pattern is detected.

### 4. Fix or remove, respecting change authorization

- **Remove** a test only on evaluator `remove` (hard anti-pattern), recording justification in the `test-failure` item.
- **Test-code changes** only in response to a main-codebase change, justifiable from comments + code; otherwise report to the user. Questionable changes require explicit approval.
- **Project-code changes** never without explicit user permission — explain, record justification, obtain approval first.
- Retained tests must carry comments indicating why they are useful.

### 5. Loop until green

After fixing or removing failures, repeat from step 1 until the full suite passes.

### 6. Follow-up wiring item (do not duplicate)

When the loop completes, reference the pre-created follow-up work item wiring
this skill into all code-touching skills and the master AGENTS.md:
**SA-0MSAC1IAS007I3K8** (referenced instead of duplicated).

Change-authorization policy
---------------------------

| Change type | Allowed when | Requires |
|-------------|--------------|----------|
| Remove useless test | Evaluator `remove` (hard anti-pattern) | Justification comment on the `test-failure` item |
| Test-code change | Response to a main-codebase change; justifiable from comments + code | Comment recording rationale |
| Questionable test change | — | Explicit user approval |
| Project-code change | — | Explicit user permission + justification comment |

Escalation
----------

If the loop cannot reach green without a project-code change or a questionable
test-side change, stop and report: the failing test(s), evaluator verdict and
reasoning, the exact change needed and why, and the triaged `test-failure` ids.

Scripts
-------

- `./scripts/run_tests.py` — runs pytest / Node suites in quiet mode, parses failures into triage-compatible records.
- `./scripts/evaluate_usefulness.py` — code-path usefulness analysis (`keep` / `remove` / `report-to-user`).
- `../triage/scripts/check_or_create.py` — reused unchanged to create/link critical `test-failure` items.
