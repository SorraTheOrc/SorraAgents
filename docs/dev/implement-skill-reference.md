# Implement skill — implementation reference

Deep implementation-reference detail relocated from `skill/implement/SKILL.md`
(relocation tracked by SA-0MSLK7SAE0032V9K). The SKILL.md is the agent-facing
operational brief; this document preserves the full implementation reference
for maintainers. Workflow semantics are unchanged — every command/flag
documented here is still valid.

## Project-local extensions and expected-dirty paths (SA-0MUYEV5AO006V0HK)

`implement.py` loads the project-local extension for the `implement` skill via
the shared `load_extension()` helper (`skill/shared/skill_extensions.py`,
contract SA-0MSQ7MQEJ0064ZB0). Discovery is repo-root scoped:
`<project_root>/.pi/skills_extensions/implement/`. An absent extension is a
no-op.

Two components are consumed:

- **Prose hooks.** `phase_start` surfaces `SKILL_PREFIX.md` after the gating
  steps and before the first actionable step; `phase_finish` surfaces
  `SKILL_POSTFIX.md` after the final step. Both are recorded in the JSON
  report (`extension.prefix_prose` / `extension.postfix_prose`) and printed as
a delimited block in human mode.

- **Machine-readable data.** `extension.json` may declare
  `ignoreDirtyPaths`, a list of repository-relative path prefixes (a single
  string is accepted) that are expected to be dirty:

  ```json
  {"ignoreDirtyPaths": [".llm-wiki/"]}
  ```

  - `git_has_dirty_files(status_output, expected_dirty=...)` skips a dirty
    path matching a declared prefix. It defaults to no exemption, so callers
    that do not opt in keep the original behaviour.
  - `phase_start` passes the project's declaration to the dirty-tree safety
    gate; `_worktree_placement_violation()` and `_child_main_checkout_violation()`
    pass it to the placement gates (via `_main_checkout_offending_paths()` and
    `_git_path_has_changes()`).
  - The declaration cannot relax the `.worklog/` skip, the stash hygiene
    gate, or the build → test → commit order. Any unexpected dirty path still
    stops the workflow.
  - A malformed `extension.json` or a malformed `ignoreDirtyPaths` value
    raises `SkillExtensionError` naming the file; `phase_start` reports the
    failure and resets the work item to `open` instead of silently changing
    the gate. `phase_finish` treats a malformed extension as best-effort for
    the postfix hook (already-successful pushes are never failed by prose).

## Key-Files-aware dirty-tree gate (SA-0MV0NSOGE000OSMR)

`phase_start` defers its dirty-tree safety gate until after it has fetched the
work item, so the relevance of a dirty **main checkout** can be judged against
the description's Key Files section.

- `classify_dirty_tree_relevance(status_output, description, expected_dirty=...)`
  parses the Key Files section (any of `## Key Files`, `## Key Files
  (predicted)` or `**Key Files:**`) and compares each dirty path against it.
- A dirty path is treated as **relevant** when it equals, is a directory-prefix
  ancestor/descendant of, or shares a basename with any Key Files entry
  (`dirty_file_matches_key_files`). Matching errs toward relevant.
- When the checkout is dirty and **no** dirty file is relevant, `phase_start`
  proceeds, creates the worktree from `HEAD`, leaves the dirty files untouched,
  records the decision and the exact paths in a work-item comment, and reports
  `dirty_worktree: false` plus `dirty_worktree_left_behind`.
- When any dirty file is relevant — or the description has **no** Key Files
  section, so relevance cannot be judged — the gate keeps the original
  abort-and-ask behaviour (`dirty_worktree: true`, exit code 2, status reset to
  `open`).
- The gate is **skipped entirely** when `phase_start` is invoked from inside a
  linked worktree (detected by `_current_checkout_is_linked_worktree()`), e.g.
  resuming a driven child: the dirty files there are the agent's own
  in-progress work.
- No stash, commit, or revert is ever performed on the dirty files.

## Why driven child sessions run with extensions disabled (SA-0MUY9PYBD009Y7J5)

The ``implement.py drive`` child spawner (``_default_session_spawner``) builds
its command as:

```
pi -p "/skill:implement <child-id>" --approve --no-extensions [--verbose]
```

``--no-extensions`` is **load-bearing**, not cosmetic. Without it, a driven
child's captured stderr can be flooded with repeated Pi-internal errors:

```
Extension error (<boundary>): turn_end could not resolve the persisted assistant entry ID
```

### Root cause (Pi core, not fixed from this repo)

``AgentSession._dispatchTurnEndBoundary`` (``@earendil-works/pi-coding-agent``,
``dist/core/agent-session.js``) resolves the finishing assistant message to a
persisted session entry via ``_findPersistedMessageEntryId``. When that returns
``undefined`` **and** ``ExtensionRunner.hasHandlers("turn_end")`` is true, it
emits the error and returns ``false`` (non-fatal). The identity mismatch is a
Pi-core bug; SorraAgents cannot patch it, and ``npm view
@earendil-works/pi-coding-agent version`` reports the installed ``1.0.4`` as
also the latest release, so no fixed version is available to upgrade to.

### Why guarding a single handler is not enough

``hasHandlers("turn_end")`` (``dist/core/extensions/runner.js:520``) is
**global across all loaded extensions**. Two installed extensions register a
``turn_end`` handler:

- SorraAgents ``pi-client/proxy-sse-signals/index.ts:48``
- ContextHub ``packages/tui/extensions/Worklog/lib/recovery/register-recovery.ts:94``

So removing or guarding the ``proxy-sse-signals`` handler alone cannot silence
the error. ``proxy-sse-signals`` is **intentionally left unchanged** — it
remains functional in interactive sessions (its SSE signal markers and
status-clearing behaviour must stay intact, SA-0MSHAKSEA001LQ6T / SA-0MUJASR2K0012DYS).
The only reliable SorraAgents-side lever is to stop loading extensions in the
headless driven child process entirely.

### Precedent

``skill/audit/scripts/audit_runner.py`` already runs every headless ``pi`` call
with ``--no-extensions`` for the same class of problem (stale ``ExtensionContext``
callbacks crashing the process after a session replacement, SA-0MUEIOGT2005IR7F;
see the Isolation note in ``skill/audit/SKILL.md``). This change mirrors that
accepted precedent for driven child sessions.

### Recorded reproduction

Recorded before the fix (``tce-main-street`` epic ``MS-0MUXW90YE009L4DQ``, child
``MS-0MUXWGT7G009THBO``): a single driven child emitted **191** identical
boundary-error lines and nothing else — ``/tmp/child1-session.log`` was exactly
191 lines, all boundary errors — so ``implement.py drive``'s captured
subprocess output was useless for progress/failure diagnosis. The same noise
was the only content in a timed-out child's failure reason in ``AI_Hell``
(``.worklog/logs/drive-AH-0MUX6S20F002GHPF.log``), and the ``tce-main-street``
epic had to abandon ``drive`` for a manual per-child workaround.

After the fix, ``drive`` constructs the child command with ``--no-extensions``,
so ``--no-extensions`` disables extension discovery entirely — no extension
loads, ``hasHandlers("turn_end")`` is ``false``, and the code path that emits
the boundary error is never entered. Because the underlying Pi-core identity
mismatch is intermittent (it depends on a message not resolving to a persisted
entry, e.g. after a session replacement), the regression guard is the
deterministic command-construction unit test rather than an e2e assertion:

```bash
pytest -q skill/implement/tests/test_drive_child_session.py
```

### Scope limits

- No Pi-core patching (``node_modules`` is out of scope).
- No change to the ContextHub ``Worklog`` extension (different repository).
- No upstream issue filed (producer decision, Q2-B); the root cause is
documented locally only.
- ``--no-extensions`` also excludes the ContextHub ``Worklog`` recovery
extension from driven children, removing its provider-error auto-retry/notify
path. This is accepted because driven children are headless, the same exclusion
is already accepted for audit calls, and the implement skill manages its own
loop.

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
