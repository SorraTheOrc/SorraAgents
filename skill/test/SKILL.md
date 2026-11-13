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
the pre-`in_review` suite pass MUST therefore go through this skill. This is the
[Test execution policy](../../AGENTS_GLOBAL.md#test-execution-policy) made
enforceable: agents must never invoke test commands directly, and the rule is
guarded by the deterministic conformance test
`tests/test_test_skill_directory.py`. An ad-hoc
`npx vitest run`/`pytest` at the same commit does not populate the cache, so the
audit cannot auto-verify those ACs (it auto-executes the suite itself on a cache
miss — F3, SA-0MSTN5KRF0097TVP — or the operator attests with `--green-run HEAD`;
it never hard-blocks — F4, SA-0MSTN8CWM003AAU9).

Inputs
------

- Optional: work-item id for triaged `test-failure` children (`parent_work_item_id`); `--rerun-failures` checks flakiness before triage.
- Optional: `--type full|unit|smoke|<local-type>` (default `full`) selects the command profile; `--scope full|changed` (default `full`) selects full-vs-changed within it.

Outputs
-------

- Structured per-failure records (JSON).
- Critical `test-failure` items created/linked via the triage skill's `check_or_create.py` (no duplicates).
- A green full test suite, or a report of items requiring explicit permission.

References
----------

- Runner: `$(skill_path test)/scripts/run_tests.py` · Usefulness: `$(skill_path test)/scripts/evaluate_usefulness.py` · Triage: `$(skill_path triage)/scripts/check_or_create.py`
- Canonicalization: `../test_runner.py` · Anti-patterns: [Test Writing Guidelines](../shared/test-writing-guidelines.md)

Workflow
--------

### 1. Run the full suite in quiet mode

```bash
python3 $(skill_path test)/scripts/run_tests.py --json
```

**Project-root resolution:** the runner targets the invoking project (see [docs/dev/test-skill-reference.md](../../docs/dev/test-skill-reference.md)).

**Suite-command resolution order (F2, SA-0MSTMYE79006NA61):** the full suite is `full_suite_commands(project_root)`, resolved in this order:

1. **`.pi/test-config.json` extension file** (repo root) — overrides convention detection entirely: `{"suiteCommands": ["pytest ...", "node --test ..."], "timeoutPerCommand": 600}`. A pinned `suiteCommands` list is returned **verbatim** — the resolver never appends the hidden skill-test command to it, and records a non-fatal notice when the pin leaves hidden skill test directories uncovered (D3). Use it for bespoke suites (e.g. a monorepo package command) that conventions would miss.
2. **pytest** — `pytest -q -r a --disable-warnings` (canonicalised via `canonicalize_quiet_test_command` from `../test_runner.py`) ONLY when the repo declares a pytest suite (`pytest.ini` / `[tool.pytest.ini_options]` / pytest-style `tests/**/test_*.py` files).
3. **Project-local hidden skill tests** — exactly **one** extra quiet pytest command scoped to every `.pi/skills/<name>/tests` (or `test`) candidate that exists and holds a `*.py`, e.g. `pytest -q -r a --disable-warnings .pi/skills/proxy-usage-analysis/tests`. pytest's default `norecursedirs` includes `.*`, so the canonical recursive run never traverses `.pi/`; naming the directories **explicitly** collects them while the default pass prunes them. Full scope only — see *Hidden project-local skill tests* below.
4. **Node suite dirs** — `node --test "tests/node/**/*.mjs" "tests/cli/**/*.mjs" "tests/unit/**/*.mjs"` (or `npm --silent test` per suite dir). Glob patterns required — node v22.22.1 rejects a bare dir (SA-0MSF8KNE3003JDVD). Only suite dirs that **exist** under the target repo are included — missing dirs are skipped (SA-0MSJELL44009XYIL), so a repo without `tests/node` never gets a guaranteed-failing phantom command.
5. **npm-test convention** — `npm --silent test` for repos whose `package.json` declares a `test` script (TCE-like layouts), emitted ONLY when nothing above resolved (no pinned list, no pytest suite, no hidden skill-test candidates, no node suite dirs).

An empty resolved set (no extension file, no pytest suite, no hidden skill tests, no node dirs, no npm test script) is NOT an error — the runner reports zero commands and the audit skill treats the repo as execution-impossible (fail-open partial, never blocks — F4 AC2).

**Hidden project-local skill tests (SA-0MVQJSGLY0098DVB).** Pytest's default `norecursedirs` includes `.*`, so a recursive `pytest` run from the repository root never traverses `.pi/` — the tests a project ships with its *project-local* skills (`<root>/.pi/skills/<name>/tests` or `.../test`) are silently absent from the "full" suite (measured on the llm repo: 5 264 collected recursively vs 529 in the skill's own test directory — SA-0MVOBJGB4001TQ8X). pytest's `pytest_ignore_collect` hook only affects *explicit* initial paths, so naming a directory on the command line collects it even though the default recursive run prunes it. The resolver scans exactly that hidden root — a candidate is a directory that exists and contains at least one `*.py` (recursive); candidates are sorted, de-duplicated and `shlex`-quoted for deterministic command strings; no other hidden root (`.claude/`, `.git/`, `.worklog/`) is ever scanned. When at least one candidate exists the resolver emits **exactly one** additional quiet pytest command listing every candidate, immediately after the canonical pytest command and before any node command. One command (not one per directory) keeps a single cache entry, a single timeout budget and the existing semaphore accounting. Emission is independent of `repo_has_pytest_suite()` — a project shipping only skill tests still gets the command; no candidates → the command is not emitted and resolution is byte-identical to today.

**D3 — project-local configuration stays authoritative** (the framework contract this must not weaken — SA-0MVO0EB1U007L7DS, SA-0MV5SQZ0T004R8UG):

| Project-local input | Required behaviour |
|---|---|
| `.pi/test-config.json` `suiteCommands` (pinned list) | Returned **verbatim** — no appended command. The resolver records a machine-readable, non-fatal **notice** naming the hidden skill test dirs that the pinned list does not cover, so the gap is visible rather than silent. |
| `.pi/test-config.json` `timeoutPerCommand`, absent `suiteCommands` | Unchanged; convention detection (incl. the new rule) applies. |
| `.pi/test-config.json` `groups` | Group `commands` are project-owned and unchanged; the `full` group resolves through `full_suite_commands()` and therefore gains the extra command unless `suiteCommands` pins the list. `paths` matching unchanged. |
| `.pi/skills_extensions/test/extension.json` `types` | Type→command maps unchanged; the default/full type inherits the extra command through `full_suite_commands()`. |
| Project pytest configuration (`pytest.ini`, `pyproject.toml`, `setup.cfg`, `tox.ini`) already listing a candidate in `testpaths` or `addopts` | That candidate is **skipped** (already collected by the canonical command) — no double execution. |

**Machine-readable notice.** When `suiteCommands` is pinned and does not cover every candidate, the resolver reports a non-fatal notice with the stable code `hidden_skill_tests_uncovered` (`NOTICE_HIDDEN_SKILL_TESTS_UNCOVERED`), `source` (`.pi/test-config.json`), the uncovered `directories` and a human-readable `message` — surfaced as `run_tests: notice [hidden_skill_tests_uncovered] …` on stderr. A notice **never** changes the resolved command set or the exit status: the pinned list stays authoritative and the gap is merely surfaced. `full_suite_notices()` is the single place consumers read resolution notices from.

**No double execution.** A candidate that the project's *own* pytest configuration already collects — via `testpaths` or `addopts` in `pytest.ini`/`setup.cfg`/`tox.ini` or `[tool.pytest.ini_options]` in `pyproject.toml` — is dropped from the extra command, so its tests run exactly once. A token at or above the repo root (`.`, `tests`) does NOT count as coverage: the default `norecursedirs` (`.*`) still prunes `.pi/` from those roots. Unreadable or malformed pytest configuration is treated as absent (never an error) — the rule may only ever *add* the command.

**Full scope only (changed scope unaffected).** `changed_scope_commands()` is untouched by this rule: it never emits the extra command, so hidden skill tests are enforced at **full scope only** — the dev-push pre-push gate, the ship release gate and the audit's full-suite verification. A changed `*.py` under `.pi/skills/<name>/tests` is still selected individually by changed scope like any other test file, for fast feedback. Deep implementation detail: [docs/dev/test-skill-reference.md](../../docs/dev/test-skill-reference.md#1-suite-command-resolution-order-f2-sa-0mstmye79006na61).

Output: JSON with per-suite results and a flat `failures` array (`test_name`, `stdout_excerpt`, `stack_trace`).

**Background test runs.** When a suite is launched in the background (rather
than run in the foreground by `run_tests.py`), wait for it completion-aware
via `../shared/wait_for_job.py` per the shared [background job
etiquette](../shared/background-job-etiquette.md) — never a fixed `sleep N`
poll. The runner's own foreground subprocess calls already honour their
per-command timeout.

### 1a. Typed runs (`--type`) — fast feedback vs release evidence

`--type <T>` selects the command **profile**; `--scope full|changed` selects
full-vs-changed *within* that profile (the two axes are orthogonal):

| Type | Meaning | Populates full-suite cache? |
|------|---------|-----------------------------|
| `full` (default) | The complete project suite. Bare `/skill:test` = `--type full`. | **Yes** — the only audit-accepted evidence. |
| `unit` | The convention unit subset (`tests/unit`) or the local extension's `unit` commands. | No — independent cache key. |
| `smoke` | The convention smoke subset (`tests/smoke`) or the local extension's `smoke` commands. | No — independent cache key. |

- **Use `unit`/`smoke` during implementation** for fast feedback; use `full`
  (or bare `/skill:test`) at the pre-`in_review` gate and before release.
- A project may define **additional types** (`dev`, `e2e`, …) in its local
  extension; the global skill dispatches any locally-defined type.
- Only `--type full` writes the audit-accepted full-suite cache entry —
  non-`full` runs use independent cache keys and record `type` in the result
  JSON and `--summary` output, so a partial run can never satisfy a "full test
  suite passes" AC.
- An unknown type exits non-zero, listing the allowed values (the minimum set
  plus any locally-defined types).

**Local extension contract.** Type→command maps are project-local and
machine-readable: `<project_root>/.pi/skills_extensions/test/extension.json`:

```json
{ "types": { "unit": ["npx vitest run --project unit"], "e2e": ["npx playwright test"] } }
```

Values are a command string or a non-empty list of command strings. When the
extension exists but omits a requested minimum type, the runner fails naming
the type and the file — it never silently runs the full suite. A `full` type
that the extension omits still falls back to the suite commands, so a bare
invocation is never broken. Convention fallback applies only when no local type
map exists. Full contract:
[skill-extensions.md](../../docs/dev/skill-extensions.md).

```bash
python3 $(skill_path test)/scripts/run_tests.py --type unit --json   # fast feedback
python3 $(skill_path test)/scripts/run_tests.py --type full --json   # release evidence
```

### 0. Cached execution (default)

`run_tests.py` caches each suite run per-repo — re-running the same command at
the same git state within the **2-hour TTL** is served from cache. Details:
[docs/dev/test-skill-reference.md](../../docs/dev/test-skill-reference.md).

### 0.1 Concurrency lanes (SA-0MVWZQ269009SJXV)

Actual executions are bounded host-wide in **two independent semaphore lanes**
so a short partial run never queues behind a long full suite:

| Lane | Used by | Namespace | Ceiling (default) |
|---|---|---|---|
| `full` | whole-suite runs (`--scope full`, `--type full`) | `test` | `TEST_MAX_CONCURRENCY` (`6`) |
| `fast` | partial runs (`--scope changed`, `--type unit`/`smoke`/`group:*`, per-test reruns) | `test-fast` | `TEST_MAX_CONCURRENCY_FAST` (`4`) |

Cache hits acquire nothing. Both lanes use `TEST_LOCK_TIMEOUT` (default
`600` s) as their bounded wait. Every result reports `slot_lane` plus
`slot_wait_seconds` (queue time) **separately** from `elapsed_seconds`
(execution time), so a queue-delayed run can never be mistaken for a slow
suite; the aggregate adds `slot_lanes`. The ceilings are calibrated, not
guessed: [docs/dev/test-ceiling-validation.json](../../docs/dev/test-ceiling-validation.json).

Query a cached run without executing:

```bash
python3 $(skill_path test)/scripts/run_tests.py --summary --suite all                         # summary lines
python3 $(skill_path test)/scripts/run_tests.py --summary --summary-grep "Test Files|failed"  # read-only grep
python3 $(skill_path test)/scripts/run_tests.py --force                                       # fresh run
```

The **audit skill** consumes the cache read-only via `query_cached()`: a green
full-suite run at the audited git state within the TTL auto-verifies
execution-dependent ACs (SA-0MSIU5HFI0024D7W). Failed (non-zero-exit) runs use
a short 5-minute TTL so transient infra failures are not re-served as current
results (SA-0MSJELL44009XYIL).

**Evidence runs must bypass the cache (`--no-cache`).** A cache hit is a
*reused* verdict: nothing executed for the tree in front of you. Any run whose
result is consumed as evidence — proving a RED witness fails before the fix and
passes after it, or supplying the green full-suite run an audit, the implement
finish gate or the pre-push gate relies on — must execute fresh:

```bash
python3 $(skill_path test)/scripts/run_tests.py --scope changed --no-cache --json
python3 $(skill_path test)/scripts/run_tests.py --scope full --no-cache --json
```

The cache is defensive: it never serves a verdict for a git state it could not
fingerprint (a failing/locked index, a leaked `GIT_DIR`, an unreadable dirty
path) and it keys each entry on the state the run *left behind*, so a completed
run is always queryable afterwards. It cannot know whether **you** need fresh
evidence, though — that is what `--no-cache` is for. Cache-served runs are
marked `[cached]` in non-JSON output and carry `cached: true` per suite in JSON
(SA-0MSGN5OJ4002OZKY).

**Scope-aware execution (SA-0MT6BYQHB008DOGC):** `run_tests.py --scope full|changed`
— the FULL suite by default, or only the tests affected by changes since
`--target-branch` (default `origin/dev`) via named test groups, convention
mapping (pytest, node and vitest/TypeScript `*.test.ts`/`*.spec.ts`) and AST
import-graph expansion. `full` is the only scope accepted as full-suite
evidence: changed-scope runs get independent cache keys, record `scope:
changed` in their metadata, and NEVER populate the full-suite cache entry —
the audit's read-only full-suite query rejects `changed` entries, so a
partial run can never satisfy a "full test suite passes" AC. `suiteCommands`
describes the **full** suite only and no longer disables changed-scope
selection (SA-0MVL7YOMJ006UMDO); a declared `groups` entry whose `paths` match
the change is preferred, then file-level mapping. When no subset can be
selected the fallback names the **specific** reason (no changed files, no
changed source/test files, no matching group, no convention-matched tests, or
no runnable tooling for the selection) instead of a generic list.
**Deleted/non-existent paths are never selected (LP-0MTZYRTNF0092JKW):**
`map_changed_to_tests()` excludes any changed path absent from the worktree
(a deleted or renamed-away test file still appears in `git diff`), and
`changed_scope_commands()` filters the selection to on-disk paths and returns
`None` when nothing selectable remains — so a deletion-only change falls back
to the full suite instead of emitting a pytest command that exits 4 ("file or
directory not found") and is mistaken for a test failure. The implement skill
applies the same rule defensively: a changed-scope pytest exit 4 is treated
as "selection unavailable" (full-scope fallback), while a genuine failure
(exit 1) still blocks.
Result JSON and `--summary` output carry `scope` per suite. Pre-push
enforcement (`.githooks/pre-push`): pushes to `refs/heads/dev`/`main` run the
full suite (`--scope full`), feature-branch pushes skip tests
(`TEST_SCOPE_SKIP=1` bypasses); the implement skill validates the worktree
with **changed scope only** during implementation — the full suite first runs
at the dev-push integration gate (pre-push hook), and the ship release gate
re-runs it on the merged dev→main tree (SA-0MVECQUGF0064U81).

**The gate's subject is the pushed commit (SA-0MVTRBT50005DUEH).** The hook
forwards git's `<local_sha>` as `run_tests.py --gate-commit <sha>`, and the
runner resolves a checkout whose `HEAD` is that commit with no dirty paths
(the push cwd when it qualifies, else a clean `git worktree list` checkout),
refusing the push (exit 2) when none exists. The suite — and its cache key —
therefore describe the pushed commit, so a green `scope: full` entry recorded
for any other state (the main checkout's `HEAD` plus its dirty `.worklog/
churn, another commit, another tree) is a **miss** and cannot satisfy the
gate. The runner prints `run_tests: gate-subject` / `gate-root` /
`gate-verdict` / `gate-refused` lines on stderr (echoed by the hook); without
`--gate-commit` every other caller behaves exactly as before.
Details: [docs/dev/test-skill-reference.md](../../docs/dev/test-skill-reference.md).

### 1b. Live-repo mutation guard (detect-only)

`run_tests.py` snapshots the resolved checkout **before and after** every run
and fails the run (non-zero exit + an actionable diff) when the suite changed
it. This stops a test that mutates the live checkout from being reported as green
— and, because the `.githooks/pre-push` gate relies only on the exit code, from
being pushed to `dev`/`main` (SA-0MUG0WFP8008WN63). The guard is **detect-only**:
it does not prevent the mutation, it makes the run fail. It is armed on
cache-served runs too (nothing executes, but the delta is still verified) and for
every scope/type.

**What it snapshots** (read-only plumbing only — `for-each-ref`,
`config --local --list`, `worktree list --porcelain`, `status --porcelain=v1 -uall`):
refs, local `.git/config` keys and the working tree (tracked diff + untracked
non-ignored paths). Non-git project roots are a no-op.

**Exclusions** (avoiding false positives):

- refs checked out in registered agent worktrees under
  `.worklog/worktrees/` (derived from `git worktree list --porcelain`, never a
  `wl-*` name pattern) — a fixture branch with **no** registered worktree still
  fails the run;
- the `refs/worklog/` data namespace (churned by `wl` during a run).

**Working-tree attribution** (SA-0MUIJAHXS003M7BG): a path that was **already
dirty at run start** and is clean at run end was *resolved* during the run — a
sibling session committing work it had itself dirtied in the shared checkout —
so it is churn, not a mutation. It never fails the run; it is reported as an
informational note instead (`excluded: concurrently resolved working-tree
change in <checkout>: <path> [<code>]`), carried under `live_repo_excluded` in
the JSON result and printed as `note:` lines in text output. Every other
working-tree difference is still a mutation: a path that was clean at run start
and is modified, deleted or newly created, and a path that stays dirty with a
different status code. Rules and pinned blind spots:
[docs/dev/test-isolation.md](../../docs/dev/test-isolation.md) §8.5.

**Reading a failure:** the diff lists every added/deleted/moved ref, added/
removed/changed config key and added/removed/changed path, e.g.

```text
live-repo mutation detected: the test suite changed the checkout at <root>.
  added ref      refs/heads/feature-x -> abc123
  changed config user.email: real@example.com -> t@t.com
  added path     [??] root-file-repo/
```

JSON runs carry the same text under `live_repo_mutation` and set
`success: false`; `live_repo_excluded` (when present) lists the excluded churn.

**Direct `pytest` runs** are covered by a second, reusable guard:
[`live_repo_guard.py`](../shared/live_repo_guard.py), armed by the
repo-root [`conftest.py`](../../conftest.py). It compares a cheap refs+config
fingerprint at each test's teardown and **names the offending test**
(`pytest.fail`), then compares the full surface at session end. Excluded
working-tree churn from a concurrent session is printed there as a `note:`
line, never as a failure. It is a no-op
under `--collect-only`, under xdist workers and for non-git roots. A
**recursion marker** (`LIVE_REPO_GUARD_ACTIVE`) is exported when a guard arms:
subprocesses inherit it, so a nested `pytest`/`run_tests.py` invocation stands
down instead of double-reporting — the outermost guard owns the checkout. Tests
that deliberately arm the guard in a `tmp_path` repo clear the marker first.

Details: [docs/dev/test-isolation.md](../../docs/dev/test-isolation.md),
[`git_sandbox.py`](../shared/git_sandbox.py) and
[`tests/test_live_repo_mutation_guard.py`](../../tests/test_live_repo_mutation_guard.py).

### 2. Triage every failure

For each failure record, invoke the triage helper to create or link a critical
`test-failure` work item (no duplicates):

```bash
python3 $(skill_path triage)/scripts/check_or_create.py '{"test_name":"<test_name>", "stdout_excerpt":"...", "stack_trace":"...", "parent_work_item_id":"<current-id>"}'
```

Items are tagged `test-failure`, priority `critical`, child of the invoking
item when a parent id is provided. No items after triage + green suite → loop ends.

### 3. Evaluate usefulness of each failing test (code-path analysis)

Do **NOT** rely on code comments when deciding whether a test is useful —
analyze what the test actually asserts and exercises using the evaluator:

```bash
python3 $(skill_path test)/scripts/evaluate_usefulness.py <test-file> --json
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

- `$(skill_path test)/scripts/run_tests.py` — runs pytest / Node suites in quiet mode, parses failures into triage-compatible records.
- `$(skill_path test)/scripts/evaluate_usefulness.py` — code-path usefulness analysis (`keep` / `remove` / `report-to-user`).
- `$(skill_path triage)/scripts/check_or_create.py` — reused unchanged to create/link critical `test-failure` items.


## Final step: standardized end-of-session report

Render the canonical end-of-session report (helper: [`../report/SKILL.md`](../report/SKILL.md)) as the **last step**, replacing any ad-hoc end-of-session summary:

```bash
python3 $(skill_path report)/scripts/render_report.py <work-item-id> \
  --skill-name test \
  --headline "<1-3 sentence headline summary>" \
  --ac "Full test suite executed | run_tests.py --scope full | met" \
  --ac "Failures parsed into triage records | parsed results | met" \
  --ac "Critical failures triaged into work items | wl search/create | met" \
  --ac "Report rendered | final response | met" \
  [--producer-actions "<producer actions needed to complete the CURRENT stage; blocking only — omit for 'None needed'>"] \
  [--notes "<freeform context/caveats/assumptions>"] \
  [--next-action <review|plan|implement|...>]
```

> **Note:** ACs record the *skill session's* own deliverables — not the work item's feature
> acceptance criteria (surfaced in the Use Cases section). Passing feature ACs would render
> the session as "Not ready to ship" even though it completed successfully.

The script prints the rendered report to stdout — **paste it verbatim into
your final response**, so the operator sees the report itself (not just the
tool call), then close with: `<work-item-id>: <one-line summary>`. Do NOT
re-summarize the report in a different format — the report is the summary. When the session ends in a terminal state with no open questions for the operator, end your final response with `</end_session>` on its own line as the very last line after the summary; if the session ends with questions for the operator, do not emit the marker.
