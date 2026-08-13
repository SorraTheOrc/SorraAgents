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
- Detection order: `IMPLEMENT_TEST_COMMAND` env override → pytest → npm
  `test` script → repo-local runner (`run_tests.sh` /
  `run_unity_tests.sh` / `run_unity_tests.bat`) → Unity project
  (Unity-specific skip message) → generic skip.
- Repos WITH tooling are unaffected: the detected command runs (through
  the run cache) and a real failure still blocks finish. When pytest is
  detected, `npm test` remains the fallback if the repo also defines a
  `scripts.test` entry. Commands are the canonical quiet forms
  (`pytest -q -r a --disable-warnings` / `npm --silent test`, via
  `canonicalize_quiet_test_command`) so cached runs share the test skill's
  cache keys and count as full-suite evidence (SA-0MSN6FBFS006Z5QP).
- `IMPLEMENT_TEST_COMMAND` overrides detection entirely (per-repo test
  command, e.g. a Unity test runner invoked via a repo-local script).

The returned dict includes `skipped` (bool) and `tooling` (str | None) in
addition to `success`/`stdout`/`stderr`/`exit_code`/`failures`.
