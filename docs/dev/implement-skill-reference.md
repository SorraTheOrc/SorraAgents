# Implement skill — implementation reference

Deep implementation-reference detail relocated from `skill/implement/SKILL.md`
(relocation tracked by SA-0MSLK7SAE0032V9K). The SKILL.md is the agent-facing
operational brief; this document preserves the full implementation reference
for maintainers. Workflow semantics are unchanged — every command/flag
documented here is still valid.

## Build step for repos without a build script

The `implement.py finish` build step (`run_build()`) is tolerant of repos
whose root `package.json` has no `build` script (e.g. Python-only projects):

- No `scripts.build` entry (or no root `package.json` at all) → the build
  step is **skipped** and reported as a no-op (`success: True`), so finish
  proceeds to tests → commit → push instead of aborting on `npm run build`
  exit 1 (`Missing script: "build"`). Malformed `package.json` also counts
  as "no build script" (fail-open — never block finish on a broken
  manifest).
- `scripts.build` present → `npm run build` runs unchanged; a real build
  failure still blocks finish.

The returned dict includes a `skipped` flag (True when the step was
bypassed) in addition to `success`/`stdout`/`stderr`/`exit_code`.

## Test step for repos without test tooling

The `implement.py finish` test step (`run_tests()`) is tolerant of repos
with no test tooling (e.g. bash-only repos, Unity projects without a
configured runner):

- No pytest suite (no pytest config markers/test files, or pytest not
  importable via `python3`), no `scripts.test` in the root `package.json`,
  and no repo-local runner → the test step is **skipped** and reported as
  a no-op (`success: True`, `skipped: True`), so finish proceeds to commit
  → push instead of aborting on ENOENT or `Missing script: "test"`.
- Suite resolution order (single source of truth, F2 AC4): the test
  skill's `full_suite_commands()` — `.pi/test-config.json` `suiteCommands`
  first (when declared it is the *primary* list and convention detection is
  skipped), then convention detection (pytest → node suite dirs →
  npm-test). This is the same order `skill/test/scripts/run_tests.py` uses,
  so the finish gate exercises the repo's real full suite instead of a
  pytest-first heuristic (WL-0MUL6LCE00042MB0).
- Legacy convention detection (`IMPLEMENT_TEST_COMMAND` env override →
  pytest → npm `test` script → repo-local runner (`run_tests.sh` /
  `run_unity_tests.sh` / `run_unity_tests.bat`) → Unity project
  (Unity-specific skip message) → generic skip) remains as the fallback
  when the test skill is unavailable (partial install) or resolves no
  suite. It preserves implement.py's superset support for repo-local
  runners and Unity projects that the test skill does not model.
- An explicitly-empty `suiteCommands: []` is authoritative: the test step
  is skipped (no convention fallback), matching the test skill.
- Repos WITH tooling are unaffected: every resolved command runs (through
  the run cache) and a real failure in any of them still blocks finish.
  Multiple resolved commands (a declared `suiteCommands` list, or pytest
  plus node suite dirs) are all run and combined into one result. Commands
  are the canonical quiet forms (`pytest -q -r a --disable-warnings` /
  `npm --silent test`, via `canonicalize_quiet_test_command`) so cached runs
  share the test skill's cache keys and count as full-suite evidence
  (SA-0MSN6FBFS006Z5QP).
- `IMPLEMENT_TEST_COMMAND` overrides detection entirely (per-repo test
  command, e.g. a Unity test runner invoked via a repo-local script).
- **Paced execution (SA-0MUKHCO02009EQFG):** every real suite execution
  triggered by `implement.py` (changed- and full-scope, all tooling
  branches) acquires the shared `"test"` semaphore from the test skill via
  `paced_runner`, before spawning. Cache hits never invoke the runner and so
  never consume a slot (parity with `run_tests.py`). The ceiling and bounded
  wait come from `TEST_MAX_CONCURRENCY` / `TEST_LOCK_TIMEOUT`; on saturation
  the gate returns a clear failed result (logged with an actionable retry
  hint) and stores no cache entry, so a saturated host cannot be
  oversubscribed by concurrent finish gates.

The returned dict includes `skipped` (bool) and `tooling` (str | None) in
addition to `success`/`stdout`/`stderr`/`exit_code`/`failures`.

## Push timeout (SA-0MUH9R74O002MQDU)

`implement.py finish` pushes to `dev` via `git_push_to_dev()`.  The push
timeout is resolved from `.pi/test-config.json` (`timeoutPerCommand`) via
`_resolve_test_timeout()`, defaulting to 600 s when absent — the old
hard-coded 120 s is removed.

The push is spawned in its own process group (`start_new_session=True`) so that
a `TimeoutExpired` kills the entire tree (including the pre-push hook's
`run_tests.py` child) via `os.killpg()`.  This prevents orphaned hook
processes from surviving and deadlocking subsequent push attempts.

On timeout a `PushTimeoutError` (subclass of `RuntimeError`) is raised carrying:

- `commit_hash` — the local commit that was created
- `branch` — the feature branch name
- `timeout` — the resolved timeout value

`phase_finish` catches this error, posts a manual-push comment on the work
item, and re-raises as a `RuntimeError`.  The outer `RuntimeError` handler
resets status to `open` and reports the error; the commit and branch remain
intact for manual recovery.

Recovery from the manual-push comment:

```bash
git push origin <branch>:refs/heads/dev
```

### Process-group kill (AC2)

`_kill_process_group(pid)` calls `os.killpg(os.getpgid(pid), signal.SIGKILL)`;
`ProcessLookupError` and `PermissionError` are silently swallowed (the process
may already have exited).  This is best-effort — the goal is to kill surviving
children, not to guarantee it.

### Recoverable state (AC3)

A push timeout never silently loses work:

1. The commit is already created locally before the push.
2. `PushTimeoutError` carries `commit_hash` and `branch`.
3. `wl_add_comment` posts a full manual-push instruction including both.
4. Status is reset to `open` (not abandoned).
5. The agent or operator can push manually with the posted instruction.
