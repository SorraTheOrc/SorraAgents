# Test skill — implementation reference

Deep implementation-reference detail relocated from `skill/test/SKILL.md`
(relocation tracked by SA-0MSLK7SAE0032V9K). The SKILL.md is the agent-facing
operational brief; this document preserves the full implementation reference
for maintainers. Workflow semantics are unchanged — every command documented
here is still valid.

## Test execution policy and conformance gate (SA-0MTYIMWQP0020ZUC)

`AGENTS_GLOBAL.md` carries an explicit **Test execution policy** (`## Test
execution policy`): agents must **never** invoke test commands directly — no
ad-hoc `pytest`, `npm test`, `vitest`, `node --test`, or equivalent — and
**every agent-initiated test run goes through the test skill** (`/skill:test`,
`run_tests.py`; full and changed scope, unit and smoke types), with no
exceptions. Automated gates that already route through this skill's runner
(the pre-push hook's `--scope full` runs, `implement.py finish` via
`guarded_run_all`, the audit skill's F3 auto-execution) are pipeline
infrastructure, not agent-invoked ad-hoc runs, and are out of scope.

The rule exists because only the test-skill runner populates the per-repo test
cache (§0 Cached execution below; git state + 2h TTL) that the audit skill
reads read-only via `query_cached()` to auto-verify execution-dependent ACs.
An ad-hoc run at the same git state is invisible to the cache and degrades
audit verification to F3 auto-execution or an operator `--green-run HEAD`
attestation.

**Conformance gate:** the policy is enforced machine-readably by
`tests/test_test_skill_directory.py` (AC3 of SA-0MTYIMWQP0020ZUC). It scans
every agent-facing doc (`AGENTS_GLOBAL.md`, `skill/*/SKILL.md`, `command/*.md`)
for command-shaped direct test-command patterns and fails when a *directive*
is introduced that is not an explicitly commented whitelist entry. The nested
exceptions are the legitimate infrastructure references: this skill's own
runner docs, git hooks/CI (`pre-push hook`, `--scope full`), the audit F2
suite-command resolution prose (`skill/audit/SKILL.md`, SA-0MSTMYE79006NA61),
and the single known resolve-pr-comments snippet tracked by SA-0MSN6FBFF009VCOB.
`<skill> ## Tests` sections are reworded to route through the test skill
(AC2), never deleted.

Note: doc-conformance tests scan **git-tracked** skill dirs only
(`tests/skill_inventory.py`), so an untracked `skill/<name>/` directory left by
concurrent WIP is excluded (SA-0MUU206QT001M66F) — its docs are not part of
the policy surface until committed.

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
- **Invalidation**: git-state fingerprint (HEAD sha + the **content** of
  every dirty tracked/untracked path, not just the `git status --porcelain`
  path list) + 2-hour TTL. Any content change — including a second edit to a
  file that is already dirty — an expired TTL, or a corrupt entry triggers a
  fresh run that replaces the stale entry (AH-0MVFGIV6700329SZ).
  `--force` bypasses lookup (still stores); `--no-cache` bypasses lookup and
  storage entirely.
- **Failed-run TTL (SA-0MSJELL44009XYIL)**: runs with a non-zero exit get a
  short 5-minute TTL instead of the full 2 hours, so a transient infra
  failure (e.g. `/tmp` disk quota) is never re-served as a current result for
  the full TTL — it expires quickly and the next query/run re-executes fresh.
  Green runs keep the full 2-hour TTL.
- **Pipeline normalization**: output-filtering pipelines (e.g.
  `npm test 2>&1 | grep -E "Test Files|failed"`, `| tail -30`, `| head`,
  `| tee`) normalize to the underlying run and share one cache entry.
- **Visibility**: non-JSON output marks cache hits with `[cached]` and names
  the pytest interpreter used (`[pytest interpreter: <path>]`). The concrete
  interpreter/executable is also stored in `metadata.json` (`interpreter`)
  — see *pytest interpreter resolution* below (SA-0MVK154IE007OS3D).
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

### 0.5. Concurrency bounding (SA-0MTG5U75A001F1RG)

Concurrent test-suite executions are bounded host-wide via the shared flock
semaphore in `skill/shared/process_semaphore.py`.

**Separate namespace:** test runs use the **"test"** semaphore name, which is
completely independent of the **"audit"** semaphore. Holding the audit slot
never blocks a test run and vice-versa. Each workload has its own ceiling.

**Configuration via environment variables:**

| Variable | Default | Purpose |
|---|---|---|
| `TEST_MAX_CONCURRENCY` | `2` | Maximum concurrent test-suite executions |
| `TEST_LOCK_TIMEOUT` | `600` | Max seconds to wait for a free slot |
| `TEST_LOCK_JITTER` | `0.1` | Per-waiter stagger fraction of the bounded wait (`0` = off) — see *Staggered-wait contract* below |
| `VITEST_MAX_WORKERS` | `4` | Maximum workers in a single vitest suite (per-suite fan-out bound) — resolved value is **exported** to every suite command; see *Per-suite vitest worker bound* below |

The numeric env vars are validated at runtime: invalid values produce a stderr
warning and fall back to the documented default (never breaking the test run).
Values below 1 are clamped to 1.

**Per-suite vitest worker bound (SA-0MVE98T1M006A17T):** the host-wide
`TEST_MAX_CONCURRENCY` ceiling bounds how many *suites* run at once, but a
single vitest suite normally sizes its tinypool to the CPU count (15+ forks
were observed from one suite, SA-0MSAEKOQE009TEB4). Every suite command the
runner executes therefore inherits `VITEST_MAX_WORKERS` in its **child
environment** — vitest's config resolution applies it as
`resolved.maxWorkers`, and `npm test` propagates it to the vitest process. The
default (`4`) matches the measured per-suite cap already applied to the
consumer repos (ContextHub / Tableau-Card-Engine, SA-0MSAK2ZH6009Z3TW).

- **Precedence:** an ambient `VITEST_MAX_WORKERS` (operator- or consumer-set)
  wins; the default applies only when it is unset. Values below 1 clamp to 1
  (serial); a non-numeric value warns and falls back to the default.
- **Env-only:** command strings are never rewritten, so the per-repo test
  cache key is unchanged and read-only consumers (the audit skill) still reuse
  entries. `NODE_OPTIONS` and every other consumer-set variable are preserved.
- **Escape hatch:** set `VITEST_MAX_WORKERS=1` for a suite that must run
  serially, or raise it for a repo whose tests are I/O-bound (env-var mechanism
  only; no config file).

**Bounded wait semantics:** unlike the audit runner's fail-fast slot wait, a
test run under saturation WAITS (bounded) for a free slot. If no slot frees
within `TEST_LOCK_TIMEOUT`, the run reports a clear failure notice (e.g.
`"test concurrency slot busy: no free slot within 600s"`) instead of failing
immediately or crashing. This means multiple agents running tests concurrently
will serialize gracefully — each waits its turn and succeeds once a slot
becomes free.

**Failure-notice contract (WL-0MVGUT787000LNDG):** a suite that fails
WITHOUT structured test failures reports a stable machine-readable
`notice_code` alongside the human-readable `notice` — `slot_busy`
(`NOTICE_SLOT_BUSY`), `command_not_found` (`NOTICE_COMMAND_NOT_FOUND`) or
`suite_timeout` (`NOTICE_SUITE_TIMEOUT`). `run_suite` also carries bounded
`stdout`/`stderr` tails (`_output_tail`, 4000 chars) and `run_all`
aggregates them into top-level `notice_codes`/`stdout`/`stderr`. Consumers
(e.g. `implement.py`'s finish gate) classify failures on the codes — never
by string-matching the notice text — so rewording a notice can never
silently reclassify a genuine test failure as infrastructure contention.
The slot-busy notice text itself is anchored to the exported
`SLOT_BUSY_NOTICE_PREFIX` constant.

**Staggered-wait contract (SA-0MVEDOF7A004TSWD):** under a saturated pool,
\*concurrent* waiters all enqueue with the same `TEST_LOCK_TIMEOUT` bound and,
without measures, would poll the slot pool at a fixed cadence and expire at one
shared wall-clock instant — then all report failure and re-run the suite in
lockstep, re-storming the pool (a thundering-herd wave). To break this, the
bounded wait is staggered per waiter:

- **Jittered deadline:** each waiter's deadline is reduced by a uniform random
  fraction of `TEST_LOCK_TIMEOUT` (default `TEST_LOCK_JITTER=0.1`, i.e.
  deadlines land in the final 10% of the bound). The configured bound is
  never extended — only staggered earlier — so no waiter ever waits longer
  than `TEST_LOCK_TIMEOUT`, and a herd that enqueues together expires across
  a spread of instants instead of one.
- **Jittered polling:** each retry sleeps `[0.5, 1.5] ×` the base 50ms cadence,
  so waiters do not hammer the flock in lockstep.
- **Retry guidance (AC3):** the saturation notice and `implement.py`'s pacer
  log explicitly instruct callers to wait a staggered delay (e.g. 30–120s)
  before retrying, rather than re-running immediately.

`TEST_LOCK_JITTER` accepts `[0, 1)`; `0` restores exact historical timing
(no stagger) for callers that prefer it. The jitter only affects the `"test"`
semaphore namespace — audit/`audit-host` semaphores keep `jitter=0.0`
byte-identical behaviour unless a caller opts in explicitly.

**Cache hits never block:** the semaphore is acquired ONLY when an actual
execution is about to happen (cache miss path). A cached result is served
without acquiring a slot, so read-only `--summary` queries and fast cached
hits are completely unaffected by concurrency saturation.

**Interaction with `--force`:** `--force` bypasses the cache lookup but still
acquires the concurrency slot before executing. Under saturation, a forced run
waits bounded rather than failing fast.

**Reusable pacer for other callers (`paced_runner`, SA-0MUKHCO02009EQFG):** the
test skill exposes `paced_runner(runner)` as the single entry point for other
code paths that execute suites through `run_cached`. It wraps a cache *runner*
so each real execution holds a `"test"` slot, while cache hits — which never
invoke the runner — consume no slot. `implement.py`'s finish gate (changed- and
full-scope, all tooling branches: pytest / npm / override / repo-script) now
passes its runners through `paced_runner`, so implement-gate executions are
bounded by the same host-wide ceiling as `run_tests.py` and can no longer
oversubscribe the host alongside audit-triggered or browser runs. On
saturation the gate returns a clear failed result (logged: `Implement
run_tests: could not acquire a test-run slot …`) and stores no cache entry —
raise `TEST_LOCK_TIMEOUT` to wait longer. `paced_runner(runner, on_wait=…)`
also reports `(queued_at, wait_seconds)` immediately after the slot is
acquired; implement-gate runs use this to emit a
`test slot acquired — queued_at=… wait_seconds=…` line, so contention is
observable rather than silent (SA-0MUA8BSAG000YZA2 AC3).

**Browser-suite contention (SA-0MUA8BSAG000YZA2):** with every execution path
paced by the shared `"test"` semaphore, cross-suite oversubscription is bounded
by `TEST_MAX_CONCURRENCY` — the historically observed 30+ concurrent
`vitest`/Chromium processes came from **unpaced** `run_cached` call paths
(implement.py finish gates and the audit F3 path, both now paced), not from a
raised cap. Repos whose suites spin up a 4-worker Chromium/browser stage and
that must never overlap another suite's browser stage should set
`TEST_MAX_CONCURRENCY=1` in the environment (env-var mechanism only; no config
file). Slot-file pruning beyond the ceiling is tracked separately
(ContextHub WL-0MUL1DSEF004IJU9).

### 0.6. Nested-run isolation (SA-0MUY6U05F008RG86)

A full-suite run holds a host-wide `"test"` semaphore slot for its whole
duration. Any test running inside it that invokes `run_tests.py` again —
in-process (`rt.main(...)`) or as a subprocess — is a **nested run**. If the
nested run acquires the same host-wide namespace, it queues behind the very
slot its own outer run holds; with two concurrent full suites and
`TEST_MAX_CONCURRENCY=2`, no slot can ever free and the nested acquire waits
out the whole `TEST_LOCK_TIMEOUT` (observed 2700s hangs in `implement.py
finish` and the pre-push gate). The observed offenders were
`tests/test_live_repo_mutation_guard.py::TestRunTestsGuardFires::*` and
`tests/test_pytest_path_resolution.py::test_run_tests_spawns_runner_interpreter_when_pytest_missing`.

**Contract:** a fixture that spawns (or calls) a nested runner sets
`PI_SEMAPHORE_DIR` to a per-test isolated directory for that invocation. The
nested run then bounds itself in its own namespace and never contends for the
host-wide pool. Top-level runs never do this, so the host-wide ceiling
(`TEST_MAX_CONCURRENCY`) and the bounded-wait semantics are unchanged for
genuine parallel suites — **only nested child runs are isolated.**

Use `nested_semaphore_dir()` from `skill/shared/process_semaphore.py` to name
the directory (re-exported by `run_tests.py`):

```python
from test.scripts.run_tests import nested_semaphore_dir

@pytest.fixture(autouse=True)
def _isolated_nested_semaphore(tmp_path, monkeypatch):
    monkeypatch.setenv("PI_SEMAPHORE_DIR", str(nested_semaphore_dir(tmp_path)))
```

For a **subprocess** invocation, pass the overridden value in the child
environment (e.g. `_child_env()` in `tests/test_live_repo_mutation_guard.py`):
`env["PI_SEMAPHORE_DIR"] = os.environ["PI_SEMAPHORE_DIR"]`. For an
**in-process** call (`rt.main(...)` with the runner stubbed), set it on the
test process with `monkeypatch.setenv` before the call
(`tests/test_run_tests_cache.py` and `tests/test_pytest_path_resolution.py`).

Keep the isolated directory **outside** every fixture repository: slot files
newly created under the guarded checkout would otherwise be reported as a
live-repo mutation. `nested_semaphore_dir(tmp_path)` places it beside the
repo, not inside it.

**Suite-wide safety net.** The repo-root `conftest.py` additionally installs an
autouse fixture that points `PI_SEMAPHORE_DIR` at a fresh per-test directory for
**every** test in the session. That covers in-process runners whose file has no
bespoke override (e.g. `tests/test_run_tests_cache.py::test_strict_git_env_*`,
which was observed waiting ~287s for a host slot), so no test can leak a nested
acquire into the host-wide pool. The explicit fixture injections above stay as
the documented contract at the call sites; tests that exercise the semaphore
directly (`test_test_skill_semaphore.py`, the audit concurrency suites) override
`ENV_LOCK_DIR` themselves and are unaffected.

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

**Project-specific note (SorraAgents):** this repo declares
`.pi/test-config.json` with `{"timeoutPerCommand": 1500}` (added under
SA-0MTYMFLZD004O2VV / commit 715806bb). The default 600s cap kills the full
suite (~412s measured wall-clock) before it can finish, so the timeout was
raised to 1500s (~3.6x margin) to allow both the test skill and the audit
runner's auto-execute path to complete. No `suiteCommands` override is set
— command detection continues to use the convention-based resolution above.

### 1b. pytest interpreter resolution (SA-0MVK154IE007OS3D)

The canonical pytest **command** (the cache key) is always
`pytest -q -r a --disable-warnings`. The **executable prefix** actually run is
resolved at execution time by `resolve_pytest_command()` in
`skill/test_runner.py`, replacing the historical bare-`pytest` rule
(SA-0MSQ012QG005N22S). Every candidate is **probed** (a cheap
`<candidate> --version`, run once per process via the memoised
`_PYTEST_COMMAND`) and skipped when it cannot run pytest — a failing or
foreign-interpreter candidate is never selected while a working one exists:

1. **`PYTEST_BIN`** — explicit pytest binary/command override, used verbatim
   when it runs.
2. **`TEST_PYTHON`** — explicit interpreter override (`<TEST_PYTHON> -m
   pytest`).
3. **`sys.executable -m pytest`** — the interpreter that started the runner,
   i.e. the project's interpreter. This is the fix for hosts where a PATH
   `pytest` script carries a system-Python shebang that cannot import the
   project's dependencies (collection then fails with exit 2 — e.g.
   `ModuleNotFoundError: No module named 'numpy'` — even though the repo's
   canonical `python3 -m pytest` run is green). Because `sys.executable`
   precedes the PATH `pytest`, the two orders can never disagree while the
   runner's interpreter can run pytest.
4. **`pytest` on PATH** (`shutil.which`) — only when earlier forms fail the
   probe.
5. **`~/.local/bin/pytest`** — retained user-install fallback
   (SA-0MSQ012QG005N22S; user-site bin dirs are absent from non-login
   shells). It sits between the PATH `pytest` and the final `python3`
   candidate: a concrete, probed, user-site install that carries the user's
   dependency set is preferred over the possibly-foreign PATH `python3`
   interpreter, while still never winning over the runner's interpreter.
6. **`python3 -m pytest`** — last resort when the runner's interpreter cannot
   run pytest but PATH `python3` can.

When every candidate fails the probe the resolved command falls back to bare
`pytest` and the spawn surfaces the error (never a silently narrowed suite).
Overrides are read on the first resolution in a process (the result is
memoised for the whole process), so export `PYTEST_BIN` / `TEST_PYTHON`
before invoking `run_tests.py` rather than mid-run.

**Auditability (AC2):** the concrete interpreter/executable selected is
recorded in the cache entry (`metadata.json` → `interpreter` field) and in
`run_tests.py` output (`[pytest interpreter: /usr/bin/python3]` on the
non-JSON suite line; `interpreter` in the per-suite JSON result and in the
`--summary` JSON `interpreters` map). The cache key stays git-state + command
+ type — the interpreter is informational metadata, so an entry is never
silently invalidated for having run under a different interpreter; the choice
remains visible and auditable. Direct `run_cached()` consumers — the audit
F3 auto-run and the implement finish gate — get the same behaviour through
`test_cache._default_runner`, which rewrites the executable prefix via
`executable_test_command()` exactly like `_cached_runner` does.

#### Field verification (AC4, SA-0MVK154IE007OS3D)

**Observed failure (host `RossGameDev`, repo ContentCreator, 2026-10-22, from
the origin report):** with the old bare-`pytest`-first resolution, a PATH
`~/.local/bin/pytest` carrying a system-Python shebang (`#!/usr/bin/python3`;
PEP 668 externally managed, no project dependencies) shadowed the runner's
interpreter. `python3 <skill>/test/scripts/run_tests.py --type full` then
exited 2 with 13 collection errors — the first
`ModuleNotFoundError: No module named 'numpy'` came from
`.pi/skills/wiki-tts-generate/scripts/tts_generate.py`, plus
`tests/test_podcast_parts_layout.py` and `tests/test_rss_process_notes.py` —
while the same tree was green under the repository's canonical runner
`./scripts/run_tests.sh` (2588 passed, 16 skipped). The red result was cached
by git state + scope, so one such run poisoned the audit-accepted full-suite
entry for that commit until a `--force` run replaced it.

**Post-fix behaviour:** the same command now resolves `sys.executable -m
pytest` (the interpreter carrying the project's dependencies, which precedes
a PATH `pytest` that fails its probe), executes the same tests the canonical
runner would, prints `[pytest interpreter: <path>]`, and records the choice
in the cache entry's `metadata.json` — so the green result matches
`./scripts/run_tests.sh` and is auditable. The resolution is executed by the
interpreter the agent started the runner with, which is the repository's
canonical interpreter (`python3 …/run_tests.py`).

**Measured parity (real ContentCreator checkout, 2026-10-29).** Verified on
host `rgardler-AI-Laptop` against ContentCreator `main` (`bf46a75`, the
`packages/ContextHub` submodule initialised, and the vendored
`.pi/packages/pi-llm-wiki-manual` dependencies installed with `npm ci` —
without them the repository's own runner fails three
`tests/test_rebuild_wiki_meta.py` cases on a fresh clone):

| Runner | Command | Result |
|--------|---------|--------|
| Canonical | `./scripts/run_tests.sh` | exit 0 — **3286 passed, 2 skipped** across the 7 suites |
| Test skill | `run_tests.py --type full --force --json` | success — **3286 passed, 2 skipped**, interpreter `/usr/bin/python3` |

Both runners therefore report the same green result, and the skill run names
the interpreter it used. In this repository the executed pytest command comes
from the repo-level pin (`.pi/test-config.json` → `suiteCommands` =
`python3 -m pytest -q -r a --disable-warnings`, tracked separately in
OSL-0MVJOKJXK009J8ZA), which agrees with the framework resolution. Per
producer decision 2026-10-29 (AC4), host-independent parity of this kind
satisfies the field verification; the literal `RossGameDev` host is not
required.

**Failure condition reproduced end to end (2026-10-29).** With a `pytest`
first on `PATH` whose `--version` succeeds but whose interpreter cannot
import the project's dependencies (the `RossGameDev` shape), the pre-fix
selection — a bare `pytest -q -r a --disable-warnings` — reproduces the field
failure exactly (`ModuleNotFoundError: No module named 'numpy'`, exit 2),
while the fixed resolver, run in the same checkout with the same poisoned
`PATH`, selects the runner's interpreter:

```text
which(pytest)               = <foreign-pytest-dir>/pytest
resolve_pytest_command()    = /usr/bin/python3 -m pytest
executable_test_command("pytest -q -r a --disable-warnings")
                            = /usr/bin/python3 -m pytest -q -r a --disable-warnings
```

**Repeatable verification (any host — no `RossGameDev` checkout required):**

```bash
# The green full-suite run names the interpreter actually used:
python3 <skill>/test/scripts/run_tests.py --type full --force --json \
  | jq -r '.suites | to_entries | map(.value.interpreter) | unique[]'
```

The configuration-independent regression
`tests/test_pytest_path_resolution.py::test_foreign_pytest_on_path_is_skipped_end_to_end`
reproduces the shadowing end to end (a temp repo whose PATH holds a
`pytest` bound to an interpreter that cannot run pytest), asserting that the
suite executes green under `sys.executable -m pytest`, that the JSON result
and output name that interpreter, and that the foreign shim never executes
the suite.

### 1a. Test types (`--type`) and the local extension contract (SA-0MTJQB2MA008HMO6)

`--type <TYPE>` selects the **command profile** (default `full`). It is
orthogonal to `--scope`: the type chooses *which* commands form the profile,
the scope chooses *full vs changed* within it.

**Minimum types** (accepted by every project):

| Type | Without a local extension |
|------|---------------------------|
| `full` (default) | `full_suite_commands(project_root)` (section 1) — unchanged. |
| `unit` | Convention subset: `pytest tests/unit` when the repo declares pytest and the dir holds `.py`; `node --test "tests/unit/**/*.mjs"` when it holds `.mjs`. |
| `smoke` | Convention subset: `tests/smoke` (same pytest/node rules). |

When no convention subset exists (e.g. `--type smoke` in a repo without
`tests/smoke`), the runner exits non-zero with a clear diagnostic — it never
silently runs the full suite.

**Local extension (machine-readable).** A project declares its own type→command
map at `<project_root>/.pi/skills_extensions/test/extension.json` (the
SA-0MSQ7MQEJ0064ZB0 convention, loaded via
`shared.skill_extensions.load_extension`):

```json
{ "types": { "unit": ["npx vitest run --project unit"], "e2e": ["npx playwright test"] } }
```

- Values are a command string or a **non-empty** list of command strings.
- A locally-defined type wins over the convention/minimum rules, so projects may
  add extra types (`dev`, `e2e`, `quick`, …).
- If the extension exists but omits a requested **minimum** type
  (`unit`/`smoke`), the runner fails with a diagnostic naming the type and the
  extension file — never a silent full-suite substitution. `full` that the
  extension omits still falls back to `full_suite_commands` so a bare invocation
  is never broken.
- A malformed `extension.json` (bad JSON, non-object top level, non-string
  commands) raises a clear error naming the file.
- Convention fallback applies only when no local `types` map exists (a
  prose-only `SKILL_PREFIX.md`/`SKILL_POSTFIX.md` extension does not define
  types).

**Unknown types** exit non-zero listing the allowed values — the minimum set
plus all locally-defined types.

**Cache and evidence.** Only `--type full` populates the audit-accepted
full-suite cache entry. The cache key namespaces non-`full` types
(`test_cache.cache_key(..., test_type=...)`), each entry's metadata records
`test_type`, and `query_cached`/`run_cached` match on it, so a typed run can
never be served as (or mistaken for) full-suite evidence. `run_suite`/`run_all`
results carry `type`; `--summary` reports the recorded type per suite
(`types` in JSON, `type=<T>` in text). The audit skill queries with the
default `test_type="full"` and is therefore unaffected.

```bash
python3 ./scripts/run_tests.py --type unit --json    # fast implementation feedback
python3 ./scripts/run_tests.py --type full --json    # release/audit evidence
```

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

**Deleted / non-existent paths (LP-0MTZYRTNF0092JKW).** A deleted or
renamed-away test file still appears in `git diff` even though it is absent
from the worktree. `map_changed_to_tests()` adds a changed test path only
when `(root / path).exists()`, and applies a final existence filter over the
whole selection (covering convention-mapped and import-graph entries too).
`changed_scope_commands()` repeats the filter defensively; if nothing
selectable remains (a deletion-only change) it returns `None`, forcing the
full-scope fallback. This prevents emitting `pytest <deleted-file>`, which
exits 4 ("file or directory not found") and would otherwise be reported as
a phantom test failure. The implement skill's changed-scope gate mirrors
this: a scoped pytest exit 4 is treated as "selection unavailable" →
full-scope fallback, while a genuine failure (exit 1) still blocks.

**Fallback to full scope** (with a logged warning) happens when no subset
can be selected: no diff base / no changed files, all changed files are
non-test/unmapped, the selection references only deleted/non-existent paths,
the repo declares custom `suiteCommands` in `.pi/test-config.json` (not
introspectable), or the repo has no subsettable tooling. A scoped run never
silently skips testing.

**The fallback honours pinned suite commands
(OSL-0MVNWYRQB0095WXQ).** When the fallback fires, the full-scope command set
is resolved through `full_suite_commands(project_root)` — the single source
of truth (section 1) — filtered by the requested suite name via
`filter_commands_for_suite()` (`pytest` → commands containing `pytest`,
`node` → commands containing `node`, `all` → every command).

- A repo that pins `suiteCommands` (`{"suiteCommands": ["python3 -m pytest -q"]}`)
therefore runs the **pinned** command for `--suite pytest --scope changed`,
exactly as `--suite all` does. The legacy bare `pytest` token is never
substituted on the fallback path, so a scoped run can no longer silently
re-resolve `pytest` to a different interpreter and report false collection
errors. Selection honours the pin *verbatim* (that is what
`run_suite`'s `command` result field reports); execution-time
`executable_test_command()` resolution (SA-0MVK154IE007OS3D) still normalises
only the pytest interpreter prefix, so `--scope changed` and `--scope full`
spawn byte-identical commands for the same suite.
- A **multi-command pin** (mixed pytest + node entries) maps each command to
its own suite name; `--suite pytest` runs the pytest entries only.
- When the filtered set is **empty** (e.g. a node-only pin queried as
`--suite pytest`), `run_suite` raises the unknown-suite-style
`ValueError` (`unknown suite: pytest — the repo's suite commands define no
matching command`) instead of silently running nothing and reporting success.
- The reporting contract is unchanged: a fallback still records
`scope: "full"`.
- Regression coverage: `skill/test/tests/test_run_tests_scope.py` →
`TestExplicitSuiteFallbackHonoursPins`. Because the execution-time
interpreter rewrite makes the legacy token and a `python3 -m pytest` pin look
alike in the spawned argv, the tests assert the selected command source
(`result["command"]`), which is the pin verbatim on the fixed path.

Direct full-scope runs (`--scope full`) keep the legacy name-based resolution
for `pytest`/`node` (section 1), and tool-less repos keep their legacy
fallback, so existing behaviour is untouched.

**Read-only summaries resolve the same way
(OSL-0MVNWYRQB0095WXQ).** `--summary` (`run_summary`) also resolves each suite
through `full_suite_commands(project_root)` filtered by suite name, so a
summary of `--suite pytest` in a pinned repo looks up the **pinned** cache
entry rather than missing it against a bare-token key. When the repo declares
suite commands but none match the requested suite (e.g. a node-only pin
queried as `--suite pytest`), the suite is reported as a miss (`missing`),
never as an empty success. A tool-less repo (empty full-suite command set)
keeps the legacy name-based lookup, unchanged.

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

