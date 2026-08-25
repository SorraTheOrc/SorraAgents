# Test skill — implementation reference

Deep implementation-reference detail relocated from `skill/test/SKILL.md`
(relocation tracked by SA-0MSLK7SAE0032V9K). The SKILL.md is the agent-facing
operational brief; this document preserves the full implementation reference
for maintainers. Workflow semantics are unchanged — every command documented
here is still valid.

### 0. Cached execution (default)

`run_tests.py` caches each suite run per-repo by default (see
[`test_cache.py`](../../skill/test_cache.py)): re-running the same command at the
same git state within the **2-hour TTL** is served from cache **without
re-executing the suite**. This prevents agents from burning 1.5–7 minutes
re-running an identical suite just to extract summary lines.

**Project-root resolution (SA-0MSNQV9J20010LE7):** the CLI targets the
*invoking* project, not the framework's install location. `main()` resolves
the project root at CLI time via `git rev-parse --show-toplevel` from the
current working directory (mirroring the audit skill's
`TARGET_PROJECT_ROOT`), falling back to the framework `REPO_ROOT` when cwd
is not inside a git repo. `--project-root <path>` overrides detection
explicitly. This means a run from e.g. the llm repo writes/reads
`<llm>/.worklog/cache/` with the fingerprint at llm's HEAD — and the audit
skill's read-only `query_cached(..., cwd=TARGET_PROJECT_ROOT)` continues to
hit the same entries because both resolve the same project root.

- **Storage**: `<repo>/.worklog/cache/` (fallback `<repo>/.git/test-cache/`,
  resolved worktree-aware). Gitignored; never committed.
- **Invalidation**: git-state fingerprint (HEAD sha + working-tree changes) +
  2-hour TTL. A changed tree, an expired TTL, or a corrupt entry triggers a
  fresh run that replaces the stale entry. `--force` bypasses lookup (still
  stores); `--no-cache` bypasses lookup and storage entirely.
- **Failed-run TTL (SA-0MSJELL44009XYIL)**: runs with a non-zero exit get a
  short 5-minute TTL instead of the full 2 hours, so a transient infra
  failure (e.g. `/tmp` disk quota) is never re-served as a current result for
  the full TTL — it expires quickly and the next query/run re-executes fresh.
  Green runs keep the full 2-hour TTL.
- **Pipeline normalization**: output-filtering pipelines (e.g.
  `npm test 2>&1 | grep -E "Test Files|failed"`, `| tail -30`, `| head`,
  `| tee`) normalize to the underlying run and share one cache entry.
- **Visibility**: non-JSON output marks cache hits with `[cached]`.
- **PATH augmentation for user-installed executables (SA-0MSUZAJPC003BS66)**: the
  default runner (`_default_runner` in `test_cache.py`) prepends
  `~/.local/bin` to the subprocess `PATH` when it is not already present, so
  user-installed executables (e.g. `pytest` installed via `pip --user`) are
  found when a suite command is spawned in a restricted environment (e.g. an
  audit runner whose PATH lacks the user-local bin directory). The path stays
  clean for callers that already have the directory configured (no duplicate
  entry).

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
operator attestation or re-execution (SA-0MSIU5HFI0024D7W). Suite commands
only cover node suite dirs that actually exist in the target repo
(`tests/node`, `tests/cli`, `tests/unit` — missing dirs are skipped,
SA-0MSJELL44009XYIL), so a repo whose layout diverges from that set can still
auto-verify; when it cannot, the runner prints a clear diagnostic naming the
missing/failed command and the remedy (`/skill:test` or `--green-run HEAD`).
Since F3 (SA-0MSTN5KRF0097TVP) a cache miss triggers auto-execution via this
skill's machinery instead of blocking; since F4 (SA-0MSTN8CWM003AAU9) the
audit never hard-blocks on execution-impossible repos.

### 1. Suite-command resolution order (F2, SA-0MSTMYE79006NA61)

The full suite is `full_suite_commands(project_root)`, resolved in this
order (single source of truth shared by the test skill runner, the audit
runner's auto-execution path, and its read-only cache classification):

1. **`.pi/test-config.json` extension file** (repo root) — overrides
   convention detection entirely: `{"suiteCommands": ["..."], "timeoutPerCommand": 600}`.
   Use it for bespoke suites (e.g. a monorepo package command) that
   conventions would miss. `timeoutPerCommand` overrides the audit runner's
   default per-command timeout (600s, `AUDIT_TEST_SKILL_RUN_TIMEOUT` code
   constant — not an env var).
2. **npm-test convention** — `npm --silent test` for repos whose
   `package.json` declares a `test` script (TCE-like layouts).
3. **pytest** — `pytest -q -r a --disable-warnings` ONLY when the repo
   declares a pytest suite (`pytest.ini` / `[tool.pytest.ini_options]` /
   pytest-style `tests/**/test_*.py` files) — no phantom pytest for
   no-pytest repos (SA-0MSQ72BVV0011SRU AC3, `repo_has_pytest_suite`).
4. **Node suite dirs** — `node --test "tests/node/**/*.mjs"
   "tests/cli/**/*.mjs" "tests/unit/**/*.mjs"` (or `npm --silent test` per
   suite dir); only dirs that exist under the target repo are included.

An empty resolved set (no extension file, no npm test script, no pytest
suite, no node dirs) is NOT an error: `run_tests.py` reports zero commands
and the audit skill treats the repo as execution-impossible — fail-open
partial, never blocks (F4 AC2).

### 2. Scope-aware execution (SA-0MT6BYQHB008DOGC)

`run_tests.py` supports execution **scope** so full-suite evidence is only
generated at the gates that need it (push to `dev`/`main`, release, audit),
while cheap changed-file-scoped runs back iterative validation.

**`--scope full|changed`** (default `full`):

- **`full`** — the complete suite (`full_suite_commands`, section 1). This is
the ONLY scope that populates the *full-suite* cache entry and therefore the
only scope the audit skill accepts as evidence of a green full suite.
- **`changed`** — only the tests affected by changes since the diff base.
Used for fast validation during feature-branch iteration (implement skill's
worktree test loop, ad-hoc agent validation); NEVER used as full-suite
evidence.

**`--target-branch <ref>`** sets the diff base for changed-file detection
(default `origin/dev`, falling back to `dev`). Changed files are computed as
`git diff --name-only <base> HEAD` (merge-base resolved automatically).

**Changed-file → test selection** combines:

1. **Convention mapping** — a changed file maps to its own tests:
   `src/foo.py` → `tests/test_foo.py`, a changed `tests/test_x.py` → itself,
   etc.
2. **AST import-graph expansion** — the mapping adds test files that import
the changed module (deterministic, no heuristic coverage tools).

Selected test files are passed explicitly (`pytest tests/test_foo.py ...`),
so a scoped run is deterministic and cache-keyed distinctly from the full
suite.

**Fallback to full scope** (with a logged warning) happens when no subset
can be selected: no diff base / no changed files, all changed files are
non-test/unmapped, the repo declares custom `suiteCommands` in
`.pi/test-config.json` (not introspectable), or the repo has no subsettable
tooling. A scoped run never silently skips testing.

**Result JSON** carries `scope` (`full`/`changed`) at the run level
(`run_all` outputs `scope` + per-suite `resolved_scopes`); `run_suite`
results carry `scope` on every path (normal, `FileNotFoundError`,
timeout). `--summary` prints `{suite} summary ({scope} scope):` and the JSON
summary carries per-suite `scopes` — a partial summary can never be
mistaken for full-suite evidence.

**Cache interaction:** scoped runs use independent cache keys
distinct from the full suite (the command differs; the stored metadata
also records `scope`). A `changed`-scope run NEVER populates the
full-suite cache entry — the audit's read-only full-suite query
(`query_cached`) filters by scope and rejects `changed` entries, so
full-suite verification always requires a genuine `full` run at the same
git state.

