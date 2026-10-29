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

## Why drive reconciles terminal children after a session timeout (SA-0MVHXWLPG007OQVN)

A driven child session is bounded by `--child-timeout` (default 3600 s). That
bound is **not a verdict**: a child can commit, push to `dev` and reach a
terminal work-item state moments before the session is killed. Until
SA-0MVHXWLPG007OQVN `phase_drive` treated every `subprocess.TimeoutExpired`
from `_default_session_spawner` as a hard child failure, so `drive` reported
`success: false` / `failed_child` for children that were already done.

### Recorded production evidence

In the ContextHub epic `WL-0MVGEB3IP002V8IB` (three children) each child
session was killed at its per-child timeout (3600 s / 7200 s / 10800 s)
**after** the child had committed and pushed to `dev`, yet `drive` reported:

```
"result": "failed",
"reason": "Child session for WL-0MVGU4S58003IRXV timed out after 3600s."
```

Two children had verifiably reached a terminal state; the third had pushed
its work but was killed between the push and the final `StatusLifecycle`
update (left stranded `in-progress`, finalised manually afterwards).

### Classification rules

After a **failed** spawn (timeout **or** non-zero exit) `phase_drive` calls
`_reconcile_spawn_failure`, which re-fetches the child (read-only `wl show`)
and classifies it:

| Condition | Outcome |
|---|---|
| status `completed` / `done` / `in_review`, stage `in_review`, or soft-deleted | **terminal** — entry `result: "completed-late"`, the chain continues |
| status `open` / `blocked` / `in_progress`, or the fetch fails | **not terminal** — the pre-existing hard failure (`result: "failed"`, `failed_child`, chain stops) |

Reconciliation runs **only on the failure path** — a successful spawn never
pays the extra `wl show` (risk R1 in the intake). An unreadable child state
fails closed: no crash, no silent success.

### Report fields

Spawn-failure entries carry, alongside the existing `id` / `attempt` /
`result` / `reason`:

- `timed_out` (bool) and `timeout_seconds` (int | null)
- `terminal` (bool), `terminal_status`, `terminal_stage`
- `stranded` (bool) and `commit_evidence` when the child's work is already on
  the parent branch

`report["completed_late"]` counts completed-late children; the summary
`message` states how many completed late whenever the parent advanced after a
timeout. The human rendering uses a distinct `🕓` icon for `completed-late`
(never the `✅` success icon), `🛠` for `finalised`, and prints a
`timed out after Ns; terminal (status=…, stage=…)` evidence line. All
additions are additive: the existing keys (`success`, `parent_advanced`,
`sessions_spawned`, `children_driven`, `message`) keep their meaning and types.

### Stranded children (detection and opt-in finalisation)

A **stranded** child is the `WL-0MVGUA9EY003XFAD` shape: the session pushed to
`dev` but was killed before the final terminal update, leaving the item
`in-progress` and blocking `wl next`.

- **Detection (default, read-only).** `_detect_stranded_child` looks for a
  commit referencing the child id on `origin/dev` (then local `dev`) via a
  bounded `git log --grep` (30 s, no network fetch). Matches are reported in
  `report["stranded_children"]` (`id`, `status`, `stage`, `commit`,
  `evidence`, `reason`), the entry is marked `stranded`, and exactly **one**
  worklog comment per drive invocation names the items and how to finalise
  them. No lifecycle mutation and no worktree mutation happens.
- **Graceful degradation.** A missing/unreadable worktree, a git error, or a
  repository without the remote-tracking ref degrades to “unknown”
  (`stranded: false`) without crashing and without false positives.
- **Opt-in finalisation (`--finalise-stranded`).** When the pushed commit is
  verified, the item is still `in-progress`, no other agent holds it
  (non-empty `assignee`), and the child worktree holds **no uncommitted
  work**, `_finalise_stranded_child` sets `completed`/`in_review` through
  `StatusLifecycle.update_status` and comments the evidence on the child. The
  entry becomes `result: "finalised"`, the child is listed in
  `report["finalised_children"]`, and the chain continues. Any gate failing
  falls back to the read-only diagnostic.

### Tests

```bash
pytest -q tests/test_implement_drive.py
```

`TestTimeoutReconciliation`, `TestSpawnFailureReconciliation`,
`TestTimeoutOutcomeEvidence`, `TestStrandedChildren` and
`TestFinaliseStrandedChildren` cover the reconciliation, evidence, stranded
and finalisation paths hermetically (injected spawner, per-child `wl_show`
fake, injected commit-evidence provider, recording mutation fake).

### Scope limits

- The per-child timeout mechanism itself is unchanged (the defect was the
  consequence handling, not the bound).
- Session-per-child isolation and per-child worktree semantics are unchanged.
- No fetch/network in the evidence path; a stale `origin/dev` ref is accepted
  evidence on the failure path.

## Drive failure recovery: bounded finish attempts, reconciliation and reset

`implement.py drive` must terminate reliably and advance an epic even when a
**child's `finish`** keeps failing — a different failure path from a failed
child *session* (see the timeout section above), and the one that livelocked
epic `AH-0MV9DB68T002IROX` (SA-0MVLIZ33M009C5QY).

### The livelock this prevents

When a driven child session dies mid-work (timeout, host load), its worktree
is left dirty and its item non-terminal. `phase_parent` classifies the dirty
worktree as "has changes" and runs `implement.py finish <child>`; `finish`
fails on the abandoned WIP (build/tests red) and resets the item status to
`open` **but leaves the worktree dirty**. The next drive invocation repeats
the identical failing `finish`. Recorded evidence (2026-10-19): 29+ drive
runs, each exiting 1 after 600–890 s with `Failed to finish child
AH-0MVDV4PNZ004ZWOZ`, `sessions_spawned: 0`, `parent_advanced: false`.

### The three recovery tiers

Once a `finishing_failed` parent report is seen, `phase_drive` applies the
following tiers in order. Every tier is bounded and records its outcome in
`children_driven`:

1. **Bounded retry — `--finish-attempt-limit N`** (default: the
   `--max-child-sessions` value, so the default is one attempt and behaviour
   is unchanged from before this feature). Failing finish executions are
   counted **per child** in `finish_attempts` (a counter distinct from the
   session-spawn attempts). While the count is below the bound the loop
   re-runs `parent`, which retries the `finish`; the same child's failing
   `finish` is never executed beyond the bound within one drive invocation.
2. **Terminal reconciliation — `terminal-reconciled`.** When the bound is
   exhausted the child is re-fetched with a read-only `wl show` and
   classified with `_is_terminal_child` (status `completed`/`done`/
   `in_review`/`deleted`, or stage `in_review`). A child that reached a
   terminal state despite the failed finish (e.g. the push succeeded and a
   later gate step failed) is recorded as done (`result:
   "terminal-reconciled"`, never `failed`), an evidence comment is posted,
   and the loop continues to the next actionable child. It is **never**
   finished, reset or re-spawned. An unreadable child state fails closed
   (hard failure, no crash, no silent success).
3. **Reset for retry — `reset-for-retry`.** A genuinely non-terminal child is
   reset to a clean startable state so the livelock cannot repeat:
   - the child's **own** worktree is reset to the parent-branch HEAD with
     `git reset --hard <parent_branch>` followed by `git clean -fd`
     (`-fd` respects `.gitignore`, so the `node_modules` symlink survives);
   - the item status is set back to `open` through the shared
     `StatusLifecycle` path (`assignee=""`);
   - an audit comment on the child records the failure reason, the failed
     finish-attempt count, the child's non-terminal state and the reset
     performed — abandoned WIP is never discarded silently;
   - the loop then continues, so the next iteration starts a **fresh
     session** for that child (session-attempt accounting is unchanged).

   The reset runs **at most once per child per drive invocation**: a second
   exhaustion after a successful reset stops the chain (bounded and
   truthful) rather than resetting forever.

### Isolation gates for the reset

`_reset_child_worktree` only ever touches the child's own worktree:

- the path must exist and be a **linked git worktree** (`.git` is a file) —
  a main checkout (`.git` directory) is refused outright;
- `git rev-parse --show-toplevel` inside the path must resolve to the path
  itself (a nested/shared path is refused);
- only `git reset --hard <parent_branch>` and `git clean -fd` run, and only
  inside that path — `git clean` is never run on a shared or main path;
- a refused/failed reset leaves the hard failure in place and names the
  reason in `report["message"]`.

### Per-child outcomes (`children_driven`)

The `result` field distinguishes the three tiers (plus the pre-existing
values):

| `result` | Meaning |
|---|---|
| `finished` | The child's session exited successfully. |
| `terminal-reconciled` | The child's `finish` failed but the child was already terminal; recorded as done, the chain continues. Carries `status`, `stage`, `evidence` and `finish_attempts`. |
| `reset-for-retry` | The child was non-terminal after the finish bound; its worktree was reset to the parent-branch HEAD and its status set back to `open` for a fresh session. Carries `reset: true`, `status`, `stage`, `evidence` and `finish_attempts`. |
| `completed-late` | Session timeout/exit after the child reached a terminal state (see the timeout section). |
| `finalised` | A stranded child was finalised via `--finalise-stranded`. |
| `failed` | The child is not terminal; the chain stops and `failed_child` is set. |

Additional report fields:

- `finish_attempts` — map of child id → failing `finish` executions observed;
- `finish_attempt_limit` — the bound applied to this invocation;
- `terminal_reconciled` — count of children recorded as done after a failed
  finish;
- `reset_for_retry` — count of children reset for a fresh session.

The human rendering uses a distinct `🕓` icon for `terminal-reconciled` and
`♻` for `reset-for-retry`, and prints the `Terminal-reconciled: N` / `Reset
for retry: N` counts. All additions are additive: the existing keys keep
their meaning and types.

### Invariants

- **Terminal children are never touched.** The existing `skip-terminal`
  precedence in `_classify_child` runs before any worktree or finish
  handling, and neither the reconciliation nor the reset path can reach a
  terminal child (regression-covered).
- **`parent_advanced: false` always means real work remains** — a
  non-terminal or blocked child. The parent advances only when every child
  is terminal.
- **Session-per-child isolation, per-child worktree semantics and
  dependency-order driving are unchanged.**

### Tests

```bash
pytest -q tests/test_implement_drive_recovery.py tests/test_implement_drive.py
```

`tests/test_implement_drive_recovery.py` covers the finish-failure harness
(`DriveHarness`, `finishing_failed_report`), the terminal-child regression,
the bounded finish attempts, terminal reconciliation and the reset-for-retry
path (including a real in-repo git worktree fixture proving only the child's
worktree is reset).

### Reinstall after landing

The canonical source is this repository's `skill/` tree. Once the change is
on `dev`, run `scripts/install_pi.sh` so the installed
`~/.pi/agent/skills/implement/` copy matches it — never edit the installed
tree directly (repository-boundary rule).

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
  triggered by `implement.py` (changed-scope by the finish workflow;
  full-scope remains supported by the library for fallbacks and direct
  callers) acquires the shared `"test"` semaphore from the test skill via
  `paced_runner`, before spawning. Cache hits never invoke the runner and so
  never consume a slot (parity with `run_tests.py`). The finish workflow
  itself runs changed-scope only — the full suite is deferred to the
  dev-push integration gate (SA-0MVECQUGF0064U81). The ceiling and bounded
  wait come from `TEST_MAX_CONCURRENCY` / `TEST_LOCK_TIMEOUT`; on saturation
  the gate returns a clear failed result (logged with an actionable retry
  hint) and stores no cache entry, so a saturated host cannot be
  oversubscribed by concurrent finish gates. A test *inside* the spawned
  suite that invokes `run_tests.py` again is a nested run: its fixture sets
  `PI_SEMAPHORE_DIR` so it never queues behind the finish gate's own slot
  (SA-0MUY6U05F008RG86 — see
  [test-skill-reference.md](test-skill-reference.md#06-nested-run-isolation-sa-0muy6u05f008rg86)).

**Gate-failure diagnostics (WL-0MVGUT787000LNDG):** a failed changed-scope
run is never reported as an empty `Failures:` block. `_adapt_guarded_result`
carries the guarded run's `notices`/`notice_codes` (slot contention,
command-not-found, suite timeout) and bounded `stdout`/`stderr` tails into
`phase_finish`'s message builder, which renders:

- **No free test slot** — classified on the stable `notice_code`
  (`slot_busy`), with a narrow legacy fallback on the slot-busy notice
  prefix only when there are no structured failures. The message says the
  gate *could not start* (an infrastructure condition, not a code failure)
  and suggests raising `TEST_LOCK_TIMEOUT` to wait longer.
- **Unparsed failures** (e.g. vitest output that no parser matches) — the
  command's output tail is included so the operator sees the actual error.
- **Structured failures** — unchanged: the first 20 failures are listed.

Genuine structured failures always win the classification, so a real test
failure is never misreported as (or retried as) an infrastructure
condition. The classification uses the stable notice codes exported by
`run_tests.py` (`NOTICE_SLOT_BUSY`, `NOTICE_COMMAND_NOT_FOUND`,
`NOTICE_SUITE_TIMEOUT`), never string matching on the human-readable notice
text.

The returned dict includes `skipped` (bool) and `tooling` (str | None) in
addition to `success`/`stdout`/`stderr`/`exit_code`/`failures` — and, for a
guarded (test-skill) run, `notices`/`notice_codes` so callers can explain a
failure without any structured failures.

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

## Terminal-status read-back (no-op-push guarantee) (OSL-0MVME1PI5005WU58)

`implement.py finish` must never report success while the main-checkout store
holds a non-terminal state for the item. `phase_finish` writes the terminal
transition from `StatusLifecycle.__exit__`, which runs on **both** finish
paths — the push-with-diff path and the no-op-push path (a worktree branch
with no diff from `origin/dev`, e.g. a verification-only item whose evidence
is a worklog comment). The reproduction on OSL-0MVME1PI5005WU58 confirmed
that no step discards or overwrites that write, so the guarantee is enforced
rather than assumed.

After the `with` block (Step 8b), `phase_finish` calls
`_assert_terminal_status(work_item_id)`, which:

- reads the item back through `StatusLifecycle.show()` (the same
  prefix-to-sibling resolution as the write, so a cross-repo store — e.g. an
  `OSL-*` item from a SorraAgents worktree — is read from its own project
  store), and
- returns `{"success": True, "status": ..., "stage": ...}`, recorded as
  `report["steps"]["status_readback"]`, **or**
- raises `RuntimeError` when the store does not hold `status=completed` /
  `stage=in_review`.

The raised error flows through `phase_finish`'s normal `RuntimeError`
handler: the item is released to `open` and the run reports `success: false`
with the read-back message. That is the intended behaviour — a silently lost
or overwritten lifecycle write (the symptom recorded on OSL-0MVMB44C9008IXZX,
where finish reported success while the item stayed `open`) becomes a loud
failure instead of a green report.

Ordering guarantees:

- The read-back runs **only after** Step 6's confirmed successful push (it
  sits after the `with` block that contains the push), so a failed push, push
  timeout, or non-fast-forward failure still leaves the item `open` and never
  produces an `in_review` report.
- The read-back **reports**, it does not repair: it does not re-write the
  terminal status. Re-run `finish` (or complete the transition by hand) after
  investigating the store.
- Push-with-diff behaviour is unchanged — the assertion is additive and
  applies to both paths identically.

Regression pin: `skill/implement/tests/test_finish_no_diff_status.py` covers
the no-diff path (empty staged diff, no-op push success), the push-with-diff
companion, and the fail-loud path when the store is non-terminal.

## Live-repo guard ownership and main-checkout coverage (WL-0MVPE2BY9001CR9O)

Two guards stand between a test run and a corrupted checkout, and they have
**one owner each** — no third implementation, and no divergence in what they
inspect:

| Guard | Owner | Entry point |
|---|---|---|
| **Outer** (release/implement path) | `guarded_run_all()` in `skill/test/scripts/run_tests.py` | the `run_tests.py` CLI and `implement.py`'s finish-gate test step both delegate to it |
| **Inner** (direct `pytest`) | `LiveRepoMutationGuard` registered by `skill/shared/live_repo_guard.py:register` | the repo-root `conftest.py` (and any fixture that arms it for a `tmp_path` repo) |

`guarded_run_all` also owns the `LIVE_REPO_GUARD_ACTIVE` recursion marker: when
it is set by an outer guard the call stands down, and while it holds the marker
the inner plugin stands down. Exactly one guard owns a run — never both, never
neither. Both implementations take their snapshot/diff surface from the shared
`git_sandbox` helpers (`snapshot_guarded_roots`, `diff_guarded_roots`,
`describe_guarded_diff`), which is what keeps the two nets identical.

### What each guard watches

The surface is refs + local config + working tree of **every checkout the run
can reach**:

- the guarded root (the checkout the run was started in), and
- **its main checkout**, whenever the guarded root is a linked worktree
  (`<main>/.worklog/worktrees/<wl-…>`), resolved from git plumbing
  (`git rev-parse --git-common-dir`) — never from a directory-name pattern.

Both checkouts are needed because a worktree-scoped run shares one object store
*and* a symlinked `node_modules` chain
(`<worktree>/node_modules -> <main>/node_modules`, with
`node_modules/@worklog/shared -> ../../packages/shared`) with the main checkout.
A root-only snapshot pair therefore reported the 2026-10-26 `packages/shared`
wipe — a worktree-scoped `finish` that unlinked 25 tracked files in the main
checkout — as a fully clean run (see
[rca-main-checkout-wipe.md](rca-main-checkout-wipe.md)).

The exclusions that keep the guard free of false positives are applied **per
checkout**, so the main checkout's own churn is never a failure:

- refs checked out in a registered worktree other than *that* checkout's root,
  plus the `branch.<name>.*` config those worktrees own (ownership-based,
  SA-0MV8311B3005DNG8) — so `implement.py finish`'s own Step-5 fast-forward in
  the main checkout, and worktrees registered/removed mid-run, are churn;
- `refs/heads/dev` and the whole `refs/remotes/origin/*` namespace
  (WL-0MV5MYBKJ0033P8U);
- `refs/worklog/*` (worklog tooling refs).

Working-tree churn is attributed rather than excluded wholesale
(SA-0MUIJAHXS003M7BG): a path that was **already dirty at run start** and is
clean at run end was resolved during the run (a sibling session committing work
it had itself dirtied in the shared checkout), so it does not fail the run. It
is never silent — the run result carries the note
(`excluded: concurrently resolved working-tree change in <checkout>: <path>
[<code>]`) and `implement.py finish`'s test gate logs it
(`Live-repo guard note: …`) plus records it in the JSON report under
`live_repo_excluded`. A path that becomes dirty from a clean start is still a
mutation (delete/modify/create), and so is a path that stays dirty with a
different status code. Full rule and pinned blind spots: §8.5 of
[test-isolation.md](test-isolation.md).

A **failure names the mutated checkout and every mutated path in it**, e.g.:

```
live-repo mutation detected: the test suite changed a guarded checkout for
…/.worklog/worktrees/wl-… . The run is failed so a corrupted checkout cannot be pushed as green.
  in main checkout /home/…/ContextHub:
  added path     [ D] packages/shared/src/database.ts
```

### Cost

The extra surface is **git plumbing only**: one extra read-only snapshot per
run (`git for-each-ref`, `git config --local --list`,
`git worktree list --porcelain`, and one `git status`) plus the same again at
the end. Nothing is added per test: the per-test guard still compares the cheap
refs+config fingerprint first and only pays for the full working-tree snapshot
when that fingerprint actually moved (F5 AC4). The fingerprint is a *trigger*,
never a verdict — the authoritative diff decides, so excluded-set churn can
never fail a clean run (SA-0MUINEW6X0034C65).

Regression pins: `skill/shared/tests/test_git_sandbox.py` (`TestGuardedRoots`,
`TestWorkingTreeAttribution`, `TestWorkingTreeAttributionIncidents`,
`TestWorkingTreeAttributionBoundaries`),
`skill/test/tests/test_guarded_run_all.py`
(`test_guarded_run_from_worktree_fails_on_main_checkout_deletion`,
`test_guarded_run_fast_forward_of_main_checkout_dev_is_expected_churn`,
`test_guarded_run_reports_excluded_churn_without_failing`,
`test_guarded_run_still_fails_on_a_newly_dirty_tracked_path`) and
`tests/test_live_repo_mutation_guard.py`
(`TestConftestGuardCoversMainCheckout`, `TestWorkingTreeAttributionNested`).

## Finish main-checkout guard (WL-0MVPADZ1Z000FOSG AC3)

The test guards above protect a **test run**. But `implement.py finish`
itself operates on the main checkout — Step-5 fast-forward, Step-6 push,
Step-7 worktree removal — so a foreign actor mutating the main checkout
mid-finish (the 2026-10-26 `packages/shared` wipe) could be silently
carried into the push or committed by a subsequent `git commit -a`. The
finish main-checkout guard closes that window:

- **Snapshot at run start** (`_snapshot_main_checkout`): the main
  checkout's working-tree status surface (`git status --porcelain=v1`,
  `{path: code}`, `.worklog/` filtered out) is captured right after the
  placement gate — before build/test/commit.
- **Verify before the push** (Step 5b, `pre_push`): after the fast-forward,
  `own_churn` (the fast-forward's own file list, `git diff --name-only` of
  the pre-fast-forward HEAD → the merged commit) is attributed to finish and
  excluded; everything else that changed aborts.
- **Verify before worktree removal** (Step 6c, `pre_worktree_removal`): a
  mutation that appeared during the push (e.g. the pre-push hook's full
  suite) must still abort loudly.

On failure `phase_finish` raises inside the `StatusLifecycle` context; the
work item is left `open` (never `completed`/`in_review`), the abort message
names **every** offending path, the concurrent-actor evidence available at
runtime (other worktree `.implement_state.json` entries, live processes with
cwd in the main checkout, or `unknown`) and the recovery command
(`git checkout -- <path>`). Pre-existing dirty files present in the baseline
snapshot and project-declared expected-dirty paths are never a failure;
a deletion (`D`) is never allow-listed, even when the path appears in
`own_churn` — a fast-forward cannot delete a file relative to the tree, so
`D` entries are foreign by construction.

The guard's work is observable in the finish report under
`report["steps"]["main_checkout_guard"]` (snapshot time, verify times and
verdicts per stage, own-churn list, final verdict). Regression pins:
`skill/implement/tests/test_finish_main_checkout_guard.py` and the existing
`skill/implement/tests/test_finish_no_diff_status.py` (must stay green — a
clean finish is unaffected).

## Single-owner main-checkout lock (WL-0MVPADZ1Z000FOSG AC5)

`implement.py finish` serialises its main-checkout critical section (commit →
fast-forward → push → worktree removal) with a **per-checkout exclusive
advisory lock**: `<main-checkout>/.worklog/locks/main-checkout.lock`
(`acquire_main_checkout_lock`). Concurrency contract:

- **One finish per main checkout.** Acquisition is a single non-blocking
  `flock(LOCK_EX | LOCK_NB)` — no measurable latency when free, and no wait
  when held: a second finish exits **non-zero** with a message naming the
  holder (work item id, PID, ISO start time, repo root, host — recorded in the
  lock file after acquisition) and **without modifying the checkout or the
  work item** (no status write, no comment). Re-run once the holder
  completes.
- **No stale locks are possible.** The lock is the kernel's flock on the lock
  file's open file description, so a killed holder (SIGKILL included) releases
  it automatically — there is no PID bookkeeping to trust or clean up. The
  lock is also released on every in-process exit path (success, `RuntimeError`
  abort, escaping exception) via the `try/finally` around the critical
  section.
- **Per-checkout scope.** Finishes in different repositories on the same host
  are unaffected (the lock file lives inside each checkout).
- **Observability.** `report["steps"]["main_checkout_lock"]` records the lock
  path, whether it was acquired, when, and the holder description when refused.
- **Hygiene reporting.** `scripts/hygiene_check.sh` reports a held lock
  (`check_main_checkout_lock`) and any **tracked-file deletions** in the main
  checkout (`check_tracked_deletions`) in both human and `--json` output,
  reusing its existing git-status dirty-checkout detection.

The flock pattern mirrors `skill/shared/process_semaphore.py` (kernel-released
locks, holder metadata for messages only). Regression pins:
`skill/implement/tests/test_finish_main_checkout_lock.py` (contention, holder
metadata, SIGKILL crash release, cross-checkout independence, `phase_finish`
refusal/success, hygiene script JSON + human output).

### The main-checkout invariant these finish behaviours enforce

The normative statement — which main-checkout mutations are legitimate, which
are refused, and which code enforces each one — lives in
[worktree-isolation.md](worktree-isolation.md#the-main-checkout-is-a-guarded-surface),
together with the RCA and the recovery procedure. The three finish-side report
keys added for it are:

| Report key | Meaning |
|---|---|
| `report["steps"]["main_checkout_guard"]` | Snapshot time, per-stage verify times/verdicts (`pre_push`, `pre_worktree_removal`), the own-fast-forward churn allow-list and the final verdict |
| `report["steps"]["main_checkout_lock"]` | Lock path, whether it was acquired, when, and the holder description when the finish was refused |
| `report["steps"]["status_readback"]` | Confirms the terminal `completed`/`in_review` transition actually stuck in the store |

Sibling behaviour outside this repository: the `wl` CLI (ContextHub) prints a
single actionable diagnostic instead of an `ERR_MODULE_NOT_FOUND` stack trace
when the shared build output (`packages/shared/dist`) is missing — see the
[ContextHub dev note](https://github.com/SorraTheOrc/ContextHub/blob/dev/docs/dev/wl-shared-dist-diagnostic.md).
