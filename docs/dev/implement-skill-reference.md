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

## Assignee release when a kill cannot run an exit hook (SA-0MVVF75ZA007XNKB)

A non-empty `assignee` is a **scheduling hold** (see the
[assignee-release invariant](../../AGENTS_GLOBAL.md#assignee-release-invariant)
— canonical wording, cross-referenced rather than restated): `wl next` skips
a held item, so an item left on a lingering agent claim is invisible to
dispatch. The shared `StatusLifecycle` releases the assignee on every exit
path it controls, but three outcomes never reach a normal exit and need an
explicit durable write:

**Direct terminal writes release too.** `StatusLifecycle.update_status` — the
shared choke point every skill status write uses — releases any claim whenever
the transition leaves the active-work status (`in-progress`). A prose-only
skill (`plan`), the AC-coverage stage advance
(`tree_coverage._advance_stage`) and a captured-status restore
(`effort-and-risk`) therefore cannot reintroduce the hold by omitting an
explicit `assignee=""`. Every non-empty assignee — including the producer
identity `Map` — is released; `needsProducerReview` is the sole persistent
hold and its flag is preserved. An explicit `assignee=` / `release_assignee=`
overrides the automatic decision. Full contract:
[assignee-release.md](assignee-release.md#direct-durable-writes-statuslifecycleupdate_status).

| Outcome | Who does not get to write | Who persists the release |
|---|---|---|
| SIGTERM / SIGINT | the interrupted phase | `_signal_handler` → `_handle_signal_cleanup` → `_safety_reset_if_in_progress` (status `open` **and** `assignee=""`) |
| Per-child session timeout (`drive --child-timeout`) | the killed child session — `subprocess.run(timeout=…)` is a `SIGKILL`, so no Python exit hook runs | the **surviving** driver: `_release_orphaned_child_claim` in `phase_drive`'s spawn-failure path |
| `finish` subprocess overruns its bound inside `phase_parent` | the killed `finish` — `_invoke_implement` now maps `subprocess.TimeoutExpired` to `None` instead of propagating | `phase_parent::_run_finish` failure path → `_release_orphaned_child_claim` |
| Unclassified exception in `phase_start` / `phase_finish` | the crashing phase (Python exits without a classified branch) | the new `except Exception` guard in each phase → `_safety_reset_if_in_progress` |

### `_release_orphaned_child_claim` semantics

The timeout counterpart of the signal-path safety reset. It releases **only an
`in-progress` child**, and it **keeps the status**: the existing recovery paths
depend on it — `phase_parent` re-finishes a child whose worktree still holds
the work, and `drive --finalise-stranded` requires `in-progress` plus an
assignee that is either empty or the driver's own. Clearing the assignee first
is what lets that finaliser run at all (it refuses a child "claimed by"
someone else).

Every other state is untouched, so a terminal item is never disturbed. The
helper is idempotent: a child whose own failure path already reset it holds no
assignee and produces no write. The release never
masks the original failure — the chain still stops with `failed_child` and the
report carries the release under `claim_release` / `orphaned_claim_release`.

### Residual window

A process killed by `SIGKILL` with **no surviving supervisor** cannot write
anything, so its claim survives until the reaper clears it. The
TTL-bounded stale-claim reaper (`skill/shared/claim_release.py::reap_stale_claims`,
default 24 h) now closes that window: it runs after a successful `wl sync`
through `skill/shared/worklog_sync.py`, releases claims on non-terminal items
older than the TTL, never clears the `needsProducerReview` flag and never
touches a `completed`/`deleted` item, and records every release as a comment
naming the previous assignee
(SA-0MW0F91KN004J06U; reference:
[assignee-release.md](assignee-release.md#3-stale-claim-reaper-reap_stale_claims)).

Every in-tree kill path also has a surviving writer — the signal handler, or
`drive` / `phase_parent` — and the session-end backstop
(`release_session_claim`, invoked by the end-of-session report, plus the
`signal_release` guard used by the read-only `refactor` / `find-related`
lifecycles) releases a session's own claim when it ends without reaching a
lifecycle exit. A `SIGKILL` with no supervisor is therefore bounded by the
reaper TTL rather than unbounded.

### Tests

The release is proved **across a real process boundary**, not by inspection:

```bash
pytest -q tests/test_implement_signal_safety.py
```

- `test_signal_kill_releases_the_assignee[SIGTERM|SIGINT]` — a real
  `implement.py start` subprocess is held open by a `git` shim on
  `git worktree add` (after the claim, before the worktree), sent a real
  signal, and the persisted `status`/`assignee` are read back from a real `wl`
  store: `open` / `""`, exit code 2.
- `test_timeout_killed_child_releases_the_assignee` — `phase_drive` with a
  fake `pi` that never exits and `child_timeout=1`: the real timeout kill
  happens, the child stays `in-progress`, and the persisted assignee is `""`.
- `test_phase_start_unexpected_exception_releases_the_claim` and
  `test_phase_finish_unexpected_exception_releases_the_assignee` — the
  unclassified-crash guards.
- `test_phase_parent_releases_a_child_claim_after_a_finish_timeout` and
  `test_invoke_implement_timeout_returns_none` — the `phase_parent` timeout
  path.

The matrix in `tests/test_assignee_release_all_skills.py` additionally
exercises `_release_orphaned_child_claim` as a release-only entry point, and
the discovery guard keeps any future assignee-mutating entry point classified.

## Finalisation ordering and the audit-dispatch hold (SA-0MVXUSK02003ZMDN)

Parent **SA-0MVWFPI31000HP3H** (Only mark as complete when fully complete).

### The race this ordering closes

The herdr downtime dispatcher selects an audit candidate from items that are
terminal (`completed` / `in_review`) and not on an assignee hold
(`selectAuditCandidate` → `isAssigneeHeld`, ContextHub
`packages/herdr/src/downtime-worker.selection.ts`). The implement skill's only
hold mechanism is that non-empty `assignee`; a terminal item with an empty
assignee is dispatchable.

`phase_finish` used to write the terminal `completed`/`in_review` transition
and release the assignee inside its `StatusLifecycle` block and then return,
while the completion comment, the worktree cleanup and the belt-and-suspenders
post-push full-suite verification still ran **after** the transition. In that
window the item was a valid audit candidate, so the dispatcher could dispatch
an audit that raced a still-running implement session, producing duplicate or
conflicting work and audit records.

### The contract

Design A (verify-then-transition, decided in SA-0MVXUSED6008J04R) makes the
terminal write **and** the assignee release the implement run's **last
lifecycle action**:

- Every step that can still change the work item — the read-only post-push
  verification, the completion comment and the (non-deferred) worktree
  removal — runs **inside** the held, non-terminal `StatusLifecycle` window.
  The item is `in_progress` with the assignee held throughout, so it is never
  an audit-dispatch candidate while the run is unfinished.
- `finish` therefore returns only when the run is **fully complete**: a
  returned `finish` means the store already holds `completed` / `in_review`
  with an empty assignee.
- The read-only terminal-status read-back (`_assert_terminal_status`,
  OSL-0MVME1PI5005WU58) runs after the write and before `finish` returns; a
  lost write still fails loudly.

The hold is the ordinary assignee claim — no new hold type and no dispatcher
change. It is released exactly once, by the `StatusLifecycle` exit that writes
the terminal status, so the
[assignee-release invariant](../../AGENTS_GLOBAL.md#assignee-release-invariant)
is preserved: a finished item is never left on a hold.

### Ordered finalisation steps

`phase_finish` performs, in order (all inside the `StatusLifecycle` window
until the terminal write):

1. build → changed-scope test gate → commit → dev push (pre-existing)

   The changed-scope gate's node/vitest selection order (group → convention
   mapping → full fallback) and its real-gate proof are documented in
   [node-vitest-selection-order.md](node-vitest-selection-order.md)
   (SA-0MW0LNT970057DM9).
2. local `dev` fast-forward and post-push ancestry assertion (pre-existing)
3. **read-only post-push verification** — resolve the full-suite verdict for
   the pushed tree from the test cache; never executes a suite. `green` /
   `not_applicable` permit the transition; `red` / `missing` **refuse** it —
   the item is released to `open`, the failure is routed to triage, and the
   pushed commit is retained, never reverted (SA-0MVXUSJ2P009T97H AC4)
4. worktree removal (deferred when the tree is a driven child's cwd or
   `--keep-worktree` was passed — ownership passes to the driver/caller, so
   the deferred cleanup is **not** a remaining lifecycle step)
5. post-push parent-branch sync (pre-existing)
6. **completion comment** (commit hash, branch, verdict provenance, retained
   tree) — before the terminal write, so no comment follows it and the
   audit-freshness ordering rule holds (SA-0MTHC710X003ORZM)
7. **terminal write** — `completed` / `in_review` + assignee release, issued
   by `StatusLifecycle.__exit__`: the run's last lifecycle action
8. terminal-status read-back (`_assert_terminal_status`)

The same ordering applies to the two other implement paths that advance an
item: `phase_parent` posts its per-child summary **before** the parent's
terminal write, and `_finalise_stranded_child` posts its finalisation comment
before the stranded child's write (design A §3).

### Interruption semantics

An interruption before step 7 leaves the item non-terminal — see
[Recoverable interruption and idempotent re-run](#recoverable-interruption-and-idempotent-re-run-sa-0mvxusjjy009hmq0).
An interruption **after** the terminal write cannot regress the item: no
lifecycle-relevant work remains, and the read-only read-back plus the
stale-claim reaper bound the residual window.

### The regression test that pins the invariant

`skill/implement/tests/test_finish_finalisation_ordering.py` drives the real
`phase_finish` state machine (only its external boundaries patched) and
asserts:

- the ordered timeline: the post-push verification, the completion comment
  and the cleanup all precede the terminal write; the read-back is the
  phase's last action;
- audit candidacy at each post-push checkpoint (the item is never a valid
  candidate there) and the single post-terminal candidate transition;
- the final store state — `completed` / `in_review` with an empty assignee;
- the post-push verification's cache-only semantics and its `red` / `missing`
  refusal (SA-0MVXUSJ2P009T97H AC4);
- the comment-before-write ordering for `phase_parent` and
  `_finalise_stranded_child`.

## Recoverable interruption and idempotent re-run (SA-0MVXUSJJY009HMQ0)

An implement run interrupted **after** a commit/push but **before** its final
lifecycle action (the terminal `completed`/`in_review` write, design A —
SA-0MVXUSED6008J04R) must never leave the item stuck in a terminal state for
work that did not finish. Every such exit releases the item and records what
survived, once:

| Interruption | Status after | Assignee | Record |
|---|---|---|---|
| `SIGTERM` / `SIGINT` during `finish` (after the commit) | `open` | cleared | `Run interrupted: SIGTERM during the finish phase.` comment naming the commit, branch, worktree fate and resume command |
| classified `RuntimeError` exit after the commit (push, ancestry, guard, verification refusal, sync, failed read-back) | `open` | cleared | one `Run interrupted: finish failed (…)` comment |
| unexpected exception after the commit | `open` | cleared | one `Run interrupted: unexpected error (…)` comment |
| `phase_abort` | `open` | cleared | `Implementation aborted.` (worktree removed, unchanged) |

Two stable markers make the records idempotent:
`COMPLETION_COMMENT_MARKER` (`Implementation complete.`) and
`INTERRUPTION_COMMENT_MARKER` (`Run interrupted`). Each is deduplicated on the
marker **plus the commit hash**, read from the item's comments via
`StatusLifecycle.show` (fail-open: an unreadable store posts the record rather
than dropping it), so retrying an interrupted `finish` never stacks a second
completion comment and a repeated signal never stacks a second interruption
record.

The retained-state record answers the three questions a recovery needs —
what state was retained (`open`, empty assignee, stage unchanged, hence not
an audit-dispatch candidate), whether work was published (the commit, marked
`pushed to dev` or `committed locally, not pushed`), and how to resume:

```bash
python3 skill/implement/scripts/implement.py finish <id>
# tree already removed (the plain signal path): recreate it first
python3 skill/implement/scripts/implement.py start <id>
```

Re-running `finish` converges through the **existing** paths rather than a new
state machine: a commit that already reached `dev` makes the branch diff-free,
so the run takes the ordinary no-op-push path (commit → push no-op → ancestry
→ read-only post-push verification → completion comment → terminal write).
Recreating the tree with `start` forks from `dev`, which now contains the
pushed commit, so the same no-diff path applies. The report carries
`steps.completion_comment` (`posted` / `deduplicated` / `commit`) and
`steps.interruption_comment` (`recorded` / `deduplicated` / `commit`) as
structured evidence of both decisions.

Scope limit: the interruption context
(`_finish_progress_global` — work item, worktree, branch, commit, pushed) is
recorded from the commit step onwards and cleared when `phase_finish` leaves
its critical section, so an interruption **before** the commit records no
retained-state comment (nothing was published; the existing start-phase and
abort records cover it). A `SIGKILL` with no surviving supervisor still cannot
write: that window is bounded by the stale-claim reaper (above).

## Worktree removal ownership (SA-0MVTE8XAJ002IUDR)

Exactly one owner removes a worktree, decided by context. `finish` used to
remove it unconditionally right after the dev push, which deleted the
**finishing session's own cwd** whenever that session was standing in the
tree: Pi's bash tool then refuses every command with `Working directory does
not exist`, while absolute-path `read`/`write` keep working. The session
degrades exactly when it must write its completion comment, render its report
and verify the pushed commit (evidence: driven child sessions
`RISE-0MVPZ31UJ000BC33` — 8 × `Working directory does not exist` — and
`RISE-0MVPZ32R1001LWWX`, which burned ~73 minutes that way).

| Flow | Who removes the worktree | When |
|---|---|---|
| Driven child session (`IMPLEMENT_DRIVE_ACTIVE=1`) | `implement.py drive` | After that child's session has exited, and only when the child is terminal and the tree is clean |
| Plain leaf session that opted in (`--keep-worktree` / `IMPLEMENT_KEEP_WORKTREE=1`) | The session (or an operator) | After the completion comment, the report and the post-push verification — as the session's **last** bash command. The comment and verification already ran inside `finish`, before the terminal write (SA-0MVXUSK02003ZMDN) |
| Plain leaf session (default) | `finish` | Immediately after a successful dev push (unchanged) |
| Parent/parent-phase flow | `phase_parent` (its own `finish` subprocess per child) | Each child is finished in its own subprocess and its worktree removed there — no session-level cleanup instruction applies |
| Aborted item | `abort` | Unconditionally — an aborted item is abandoned, not resumed |

### One predicate, two removal sites

`_should_defer_worktree_removal(keep_worktree)` is the single deferral
predicate. It reads only `IMPLEMENT_DRIVE_ACTIVE` (`"1"`), the explicit
`--keep-worktree` flag and `IMPLEMENT_KEEP_WORKTREE` (`"1"`) — the same `"1"`
convention as the drive recursion guard. It returns `(deferred, reason)` with
a **stable** reason token, in fixed precedence order:

| `reason` | Input |
|---|---|
| `driven-session` | `IMPLEMENT_DRIVE_ACTIVE=1` |
| `keep-worktree-env` | `IMPLEMENT_KEEP_WORKTREE=1` |
| `keep-worktree-flag` | `finish --keep-worktree` |

Combining them is idempotent (the tree is deferred once); `drive` refuses to
run inside a driven session, so the driver itself never inherits the
deferral.

Deferral is never silent — the `finish` report carries

```json
"steps": {
  "worktree_removal": {"deferred": true, "reason": "driven-session", "worktree_path": "…"},
  "worktree_cleanup": {"deferred": true, "path": "…", "reason": "driven-session", "command": "python3 skill/implement/scripts/implement.py cleanup <id>"},
  "worktree_removed": false
}
```

and the completion comment and the human (non-JSON) output name the retained
path with the exact remedy. `worktree_cleanup.command` is the reviewed
`cleanup` action (`_worktree_cleanup_command`) rather than a raw `git worktree
remove`, so the follow-up runs behind the same safety gates (terminal item,
clean tree, never the main checkout) and is an idempotent no-op when the tree
is already gone. When removal is **not** deferred the report carries
`deferred: false`, `command: ""` and `worktree_removed: true` on success
(`false` when the removal failed).

### What is skipped when removal is deferred

`finish` skips Step 6b (`cleanup_worktree_processes`) and Step 7
(`remove_state` + `_remove_worktree`) — the tree, its `wl-*` branch and
`.implement_state.json` all survive, so an interrupted run resumes onto the
same worktree instead of failing on `git worktree add`. Everything else is
unchanged: the main-checkout guard (Step 6c), the verified local-dev
fast-forward, the push, the ancestry assertion, the parent-branch sync, the
terminal-status write and its read-back all operate on the **main checkout**,
so deferring the removal cannot affect them.

The signal handler honours the same predicate: a `SIGTERM`/`SIGINT` during a
deferred finish still releases the item (`_safety_reset_if_in_progress`) but
leaves the tree in place. `phase_abort` is deliberately **not** deferral-aware
— abort removes the worktree unconditionally.

### Driver-side removal

`_remove_finished_worktree(work_item_id, worktree_path, repo_root)` is the one
removal path used by the driver and by the session-side `cleanup` action (see
below) — every deferred caller:

1. the path is known and exists — an absent tree is a reported no-op
   (`already_absent: true`, `removed: false`), so a repeat run is idempotent;
2. `_worktree_has_uncommitted_changes` must be false — uncommitted work is
   never discarded and the tree is retained (`retained_dirty: true`);
3. `cleanup_worktree_processes` runs first (self-exclusion contract
   SA-0MVJC5KNT0044L2B), then `_remove_worktree` with its own repo-root and
   `.git/`-directory safety gates; `remove_state` runs last.

`phase_drive` calls it **after** `spawner(...)` returns, on both terminal
paths: the spawn-success path and the `completed-late` branch (the exact path
the production incident took, where the session died after its push). The
child's `children_driven` entry records `worktree_removed` plus the
`worktree_cleanup` outcome, and every retained tree is named once in
`report["retained_worktrees"]` with a copy-pasteable remedy:

```bash
git worktree remove <path> && git worktree prune
```

A cleanup problem never fails the drive: a refused or failed removal is
recorded on the entry (and in `retained_worktrees`) and the chain continues.

### Start-of-run sweep

A drive interrupted between a session's exit and the driver-side removal
would strand that child's tree forever, so a drive sweeps leftover worktrees
of **terminal** children when it starts (`report["worktree_sweep"]`). The
sweep is bounded and best-effort — terminal children only, clean trees only,
never fatal. Note that `scripts/hygiene_check.sh` reports orphaned worktrees
only when their branch was deleted, so a deferred tree is otherwise invisible
to it.

### The session-side removal action: `implement.py cleanup <id>`

The opted-in plain leaf flow has no driver to remove its tree, and a raw
`git worktree remove` would bypass every gate above (dirty trees, process
cleanup, the repo-root refusal). `implement.py cleanup <work-item-id>
[--json] [--worktree-path <path>]` is the reviewed final step
(SA-0MVTHIZE0007EAKZ):

```bash
python3 skill/implement/scripts/implement.py cleanup <work-item-id> --json
```

**Ordering contract (stated in the function's docstring):** run it as the
session's **last** bash command. Removal deletes the tree the session stands
in, so nothing that needs the worktree cwd may follow — every later command
would fail with a deleted cwd. The comment, the report and any post-push
verification come first.

A session whose final `cleanup` reports `retained_dirty` or
`retained_non_terminal` records the retention in a work-item comment (with
the reported `reason`/`remedy`) and stops — it never forces the removal (the
action has no force switch). The retention is a reported outcome, and
re-running `cleanup` later is safe once the tree is clean and the item
terminal (`already_absent` is then a no-op success).

Gates, in order — a failure at any of them **retains** the tree and is
reported (never forced past, never silent), so a retained tree is a reported
outcome rather than a crash:

| Gate | Report field | Remedy printed |
|---|---|---|
| No worktree can be resolved | `retained_no_worktree` | Create/point at a tree (`--worktree-path`) |
| Status is not `completed`/`in_review` (`CLEANUP_REMOVABLE_STATUSES`) | `retained_non_terminal` | Re-run once the item is finished |
| Path resolves to the main checkout | `retained_main_checkout` | Inspect `.implement_state.json` (stale artifact) |
| Path exists but is not a linked worktree (`.git` is not a file) | `retained_no_worktree` | `ls -la <path>/.git` |
| State file belongs to a different item | `retained_other_item` | Run cleanup for that item |
| `_worktree_has_uncommitted_changes` is true | `retained_dirty` | `git -C <path> status`, then commit/discard |

Resolution order: a candidate (the `--worktree-path` override, then the
current directory) whose `.implement_state.json` records *this* item, then
the override as given, then `_discover_worktree(<id>)`. Removal itself is
`_remove_finished_worktree` — `cleanup_worktree_processes` →
`_remove_worktree(repo_root=…)` → `remove_state` — the same single path the
driver uses, so the action adds **no** deletion mechanism of its own.

**Idempotent.** An already-absent tree is a reported no-op success
(`already_absent: true`, `removed: false`, exit 0) that still calls
`remove_state`, so an interrupted workflow can simply be re-run and never
resumes a dead tree. The report carries `removed`, `path`, `already_absent`,
`retained_dirty`, `retained_non_terminal`, `retained_no_worktree`,
`retained_main_checkout`, `retained_other_item`, `removed_state` ("no state
file remains at the path"), `reason`, `remedy` and `message`.

### Tests

- `skill/implement/tests/test_finish_deferred_worktree_removal.py` — driven /
  flag / env deferral, default removal unchanged, signal-handler deferral,
  `abort` unchanged.
- `tests/test_implement_drive_worktree_cleanup.py` — driver removal after
  session exit (both terminal paths), dirty/non-terminal retention,
  already-absent no-op, failing removal, sweep.
- `skill/implement/tests/test_cleanup_worktree_action.py` — the `cleanup`
  action: deterministic removal through the shared boundaries, every
  retention gate, already-absent idempotence, refused removal reported,
  CLI surface and the ordering-contract docstring.

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

### The recovery tiers

Once a `finishing_failed` parent report is seen, `phase_drive` **first
classifies** the failure with `classify_finish_failure` (see the
classification vocabulary below) before any retry accounting, then applies
the following tiers in order. Every tier is bounded and records its outcome
in `children_driven`:

1. **Infrastructure retries — `--infra-retry-limit N`** (default `5`). A
   failure classified `infrastructure` — a transient refusal that
   self-clears: a concurrent session's local-`dev` fast-forward, a held
   main-checkout lock, a dropped push connection, a failed ancestry
   assertion — is retried on its **own** bounded budget, `infra_retries`
   (a counter distinct from `finish_attempts` below). Each retry waits the
   exponential backoff schedule `[2, 4, 8, 16, 30, 30, …]` seconds (2 s
   doubling, capped at 30 s, `_drive_infra_backoff_delay`) and re-runs
   `parent` **without spawning a session**; it records a `children_driven`
   entry with `result: "infra-retry"` (plus the classified `step`/`reason`)
   and a durable child comment naming the real failing step/reason. It
   **never** consumes the finish-attempt budget: a refusal that clears
   within the infra budget is absorbed there, and a spent budget falls
   through to the finish-attempt bound below with the failure evidence
   appended to the stopping message.
2. **Bounded retry — `--finish-attempt-limit N`** (default: the
   `--max-child-sessions` value, so the default is one attempt and behaviour
   is unchanged from before this feature). Genuine failing finish executions
   (classified `gate` or `unknown`) are counted **per child** in
   `finish_attempts` (a counter distinct from the session-spawn attempts and
   from `infra_retries`). While the count is below the bound the loop
   re-runs `parent`, which retries the `finish`; the same child's failing
   `finish` is never executed beyond the bound within one drive invocation.
3. **Terminal reconciliation — `terminal-reconciled`.** When the bound is
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
   - an audit comment on the child records the failure reason (with the
     classified failing `finish` step/reason when available), the failed
     finish-attempt count, the child's non-terminal state, the reset
     performed, and the **file list** the reset discarded (captured from the
     pre-reset `git status --porcelain` as `discarded_paths`) — abandoned WIP
     is never discarded silently;
   - the loop then continues, so the next iteration starts a **fresh
     session** for that child (session-attempt accounting is unchanged).

   **`reset-for-retry` is limited to uncommitted WIP** (parent AC4,
   LP-0MVWEP2VQ001NPRK). The preservation gate below refuses a worktree that
   carries commits, so the reset path is only ever reached for a worktree
   that had none; the guard `_reset_for_retry_violation` fails the chain
   closed if a reset result ever reports commits ahead (or a preserved tip)
   instead of recording a reset-for-retry entry. The comment therefore states
   explicitly that no commit was discarded and contains no commit-hash-shaped
   token (hash-like runs are redacted, the discarded path list stays
   verbatim), so a reader can never infer that a commit was thrown away. The
   per-child entry carries `discarded_paths` as observability.

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
- **a worktree that carries commits is refused** (see the preservation gate
  below), and a refused/failed reset leaves the hard failure in place and
  names the reason in `report["message"]`.

### Preservation gate: a commit-carrying worktree is never reset

Before any destructive command runs, `_reset_child_worktree` checks whether
its `HEAD` is ahead of the parent branch (`_worktree_ahead_of_parent`, a
tri-state result — an undecidable state is refused, never assumed safe). When
it is ahead, the tip is parked on `refs/wip/<child-id>` with `git update-ref`
and the reset is **refused**: no `git reset --hard` and no `git clean -fd`
run, so a committed slice can never be discarded by the recovery path. The
refusal is reported in `preserved_refs` (`{id, ref, commit}`), commented on
the child, and it stops the drive chain — spawning a session would race work
that was deliberately preserved. `refs/wip/<id>` is a fully qualified ref
outside `refs/heads/`, so it never appears in `git branch` or `git worktree
list` output. The ref is **not** cleaned up automatically: an operator who
has triaged the preserved slice removes it manually once its work has been
re-landed (or is known to be superseded):

```bash
git log --oneline -1 refs/wip/<child-id>   # inspect the preserved tip
git update-ref -d refs/wip/<child-id>      # delete the ref (manual cleanup)
```

Inspection is always read-only: `git rev-parse refs/wip/<child-id>` resolves
the parked commit and `git log -1 refs/wip/<child-id>` shows it. Deletion is
deliberately not performed by the recovery path — a preserved ref is the
only copy of a committed slice whose reset was refused, so automatic cleanup
could silently destroy work the operator has not yet recovered.

Marking the invariant complete: because the gate runs first, a successful
`_reset_child_worktree` result always carries `commits_ahead: 0`, so
`reset-for-retry` is only ever recorded for an uncommitted-only reset. The
drive path pins this with `_reset_for_retry_violation` and fails the chain
closed if a result ever reports commits ahead or a preserved tip.

### Per-child outcomes (`children_driven`)

The `result` field distinguishes the three tiers (plus the pre-existing
values):

| `result` | Meaning |
|---|---|
| `infra-retry` | The child's `finish` was refused for a transient **infrastructure** reason; the loop retried `parent` on the separate `--infra-retry-limit` budget with backoff and no session spawn. Carries `step`, `reason`, `attempt` and the finish-failure `message`. |
| `finished` | The child's session exited successfully. |
| `terminal-reconciled` | The child's `finish` failed but the child was already terminal; recorded as done, the chain continues. Carries `status`, `stage`, `evidence` and `finish_attempts`. |
| `reset-for-retry` | The child was non-terminal after the finish bound; its worktree was reset to the parent-branch HEAD and its status set back to `open` for a fresh session. Carries `reset: true`, `status`, `stage`, `evidence`, `finish_attempts` and `discarded_paths` (the uncommitted files the reset removed — never a commit). |
| `completed-late` | Session timeout/exit after the child reached a terminal state (see the timeout section). |
| `finalised` | A stranded child was finalised via `--finalise-stranded`. |
| `failed` | The child is not terminal; the chain stops and `failed_child` is set. |

Additional report fields:

- `finish_failure` — the classified failing-`finish` payload
  (`{kind, step, reason, message, steps}`) carried on the **failing parent
  report** (`finishing_failed`); drive reads it from there and never parses
  prose (LP-0MVWEP1MM0097RAT AC5);
- `finish_attempts` — map of child id → failing `finish` executions observed
  (the finish-attempt bound accounting);
- `finish_attempt_limit` — the bound applied to this invocation;
- `infra_retries` — map of child id → infrastructure-refusal retries taken on
  the separate `--infra-retry-limit` budget;
- `infra_retry_limit` — the infra-retry bound applied to this invocation;
- `preserved_refs` — list of `{id, ref, commit}` entries for commit-carrying
  worktrees parked on `refs/wip/<id>` (the reset was refused);
- `terminal_reconciled` — count of children recorded as done after a failed
  finish;
- `reset_for_retry` — count of children reset for a fresh session.

These additions are the full report-surface delta for the recovery work:
`finish_failure`, `infra_retries`, `infra_retry_limit` and `preserved_refs`
are new (the last three top-level on the drive report, the first on the
failing parent report), and every pre-existing key keeps its meaning and
type. There are no forward-looking keys: every documented field is emitted
by the shipped `phase_drive`, so a field the docs promise and the code does
not emit (or a result value that is not a shipped verdict) would fail the
doc↔code consistency test.

The human rendering uses a distinct `🕓` icon for `terminal-reconciled` and
`♻` for `reset-for-retry`, and prints the `Terminal-reconciled: N` / `Reset
for retry: N` counts. All additions are additive: the existing keys keep
their meaning and types.

### Finish-failure classification vocabulary

`phase_parent._run_finish` runs every `finish` subprocess with
`parse_on_failure=True` and annotates the parent report with
`finish_failure = classify_finish_failure(<parsed finish report>)` —
`{kind, step, reason, message, steps}` — before surfacing a failure. The `None`
sentinel is preserved: a failure (parsed report or not) still returns
`None` from `_run_finish`. The classifier resolves the failing
`(step, reason)` from the report's structured `steps` payload (precedence:
`main_checkout_lock` → `main_checkout_guard` → `local_dev_fast_forward` →
`push` → `build` → `build_churn` → `tests` → `ancestry_assertion` →
`worktree_placement` → `diff`), never from `report["message"]` prose — every
`finish` failure exit records a structured `steps` entry for this purpose.
`kind` is one of `infrastructure`, `gate`, `unknown`:

| `(step, reason)` | kind |
|---|---|
| `local_dev_fast_forward` + `local_dev_diverged` / `commit_not_based_on_remote_tip` / `fetch_failed` / `checkout_failed` / `no_remote_tracking_ref` | `infrastructure` |
| `main_checkout_lock` refusal (`lock_held`) | `infrastructure` |
| `main_checkout_guard` verdict `fail` | `infrastructure` |
| `push` + `timeout` / `connection_closed` / `transport` | `infrastructure` |
| `ancestry_assertion` + `ancestry_failed` | `infrastructure` |
| `tests` + `tests_failed` | `gate` |
| `build` + `build_failed` | `gate` |
| `build_churn` + any reason | `gate` |
| `worktree_placement` + any reason | `gate` |
| `diff` + `no_changes` | `gate` |
| anything else | `unknown` (never `infrastructure`) |

The vocabulary lives in `FINISH_INFRA_REFUSALS` / `FINISH_GATE_REFUSALS`
beside the emitters (`_fast_forward_local_dev_for_push`, `phase_finish`) in
`skill/implement/scripts/implement.py`; `classify_finish_failure` is the
single consumer. **Drift rule:** any new `_refuse` reason or failure exit the
shipped `finish` code emits must be added to the table in the same commit —
an unlisted pair still classifies as `unknown` (fail-safe, never
`infrastructure`), but it stays invisible to the operator unless listed. The
drive retry/`wip`-preservation semantics built on top of this vocabulary are
completed by sibling slices.

### Invariants

- **Terminal children are never touched.** The existing `skip-terminal`
  precedence in `_classify_child` runs before any worktree or finish
  handling, and neither the reconciliation nor the reset path can reach a
  terminal child (regression-covered).
- **Zero-work children are never finalised.** `_classify_child_worktree`
  refines `_has_worktree_changes` into the question `finish` actually needs:
  `work` (uncommitted non-state files or commits ahead referencing the item),
  `verification-only` (the state-file opt-in), `no-work` (a state-file-only
  dirty surface or non-attributable commits ahead), or `clean`. A `no-work`
  child is reported `action: "not-finalisable"` in `children_processed` plus a
  structured `not_finalisable_children` key, `finish` is never run for it,
  and the parent is not advanced (SA-0MW74IKJE008JXDQ). `finish`'s own no-op
  refusal remains the defence in depth for direct invocation.
- **`parent_advanced: false` always means real work remains** — a
  non-terminal, blocked, or not-finalisable (zero-work) child. The parent
  advances only when every child is terminal.
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

The canonical source is this repository's `skill/implement/` tree
(`~/projects/SorraAgents/skill/implement/`). Once the change is on `dev`,
run `scripts/install_pi.sh` from the canonical checkout so the installed
runtime copy `~/.pi/agent/skills/implement/` matches it — the installed tree
is a generated copy that must **never be edited directly** (repository-
boundary rule): edit only the canonical `~/projects/SorraAgents/skill/implement/`
and reinstall. The runtime copy is refreshed on the next
`scripts/install_pi.sh` run, which is why a docs/code change to the
implement skill always follows this landing → reinstall order.

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

#### Gate-failure diagnostics (WL-0MVGUT787000LNDG)

A failed changed-scope run is never reported as an empty `Failures:` block. `_adapt_guarded_result`
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
`NOTICE_SUITE_TIMEOUT`, `NOTICE_KILLED`, `NOTICE_UNPARSED_FAILURE`), never
string matching on the human-readable notice text.

**Timeout, kill and unparsed reasons (SA-0MVTET8KQ0031R6Z).** A red gate has
to answer one question before an operator (or an agent) acts: fix the code,
raise the timeout, treat the host as the problem, or read the output. The gate
therefore publishes one reason per condition, mapped from the runner's stable
codes:

| `reason` | Runner code | Meaning | Next action |
|----------|-------------|---------|-------------|
| `test_failure` | structured `failures` | Real failing tests. | Fix the code. Never treated as infrastructure. |
| `timeout` | `suite_timeout` | The suite exceeded `timeoutPerCommand` and was stopped. | Raise `timeoutPerCommand` in `.pi/test-config.json` with headroom over a green run's elapsed time, then re-run. |
| `killed` | `killed` (suite `returncode < 0`) | The child was signal-terminated (OOM kill, worker/supervisor crash) with no test result. | Treat as host/infrastructure: read the output tail, re-run; do not "fix" a test. |
| `unparsed` | `unparsed_failure` | The suite exited non-zero and no test failure could be parsed (bad flag, unloadable config, missing wrapper). Vitest's default reporter — browser mode included — is parsed, so a failing Vitest test is `test_failure`, not this. | Read the command and output tail; the command or its configuration is the problem. |
| `slot_contention` | `slot_busy` | No host test slot within the bounded wait. | Wait a staggered delay, or raise `TEST_LOCK_TIMEOUT`. The queue wait is reported separately from suite execution. |
| `infrastructure` | any other notice, or a live-repo mutation | Unattributable to a test, but explained by a notice. | Read the notices; the repo may have been mutated by a concurrent session. |
| `unknown` | — | Last resort: no structured failure, no notice and no executed command. | Re-run the suite with the test skill to obtain diagnostics. It is unreachable for a timed-out, killed or non-zero run. |

**Vitest frame fallback (SA-0MWDLPC3F005S9S2).** `test_failure` also wins when
the runner built **no** structured record but the gate's bounded output tail
(stderr first, then stdout — each already bounded by `_tail_text` at
`GATE_OUTPUT_TAIL_CHARS` = 1200 chars) carries a Vitest stack frame
(`❯ [<fn>] <file>:<line>:<col>`, matched by
`gate_reason.vitest_frame_location`): the 1200-char window can strip the `FAIL`
header and the `AssertionError:` line (for example a long README diff in the
assertion message), leaving the frame as the failure's only surviving signal —
such a non-zero run is classified `test_failure`, never `unparsed`
(RISE-0MW5JFQCU001YYQK). The fallback replaces **only** the `unparsed`
outcome: `timeout`, `killed`, `slot_contention` and `infrastructure` (including
a live-repo mutation) are decided first, so a frame that merely survived in the
tail can never reclassify them.

The deciding **fact fields** travel with the verdict — on `test_phase` in the
finish report and in the operator message under `Gate facts:` — so nothing has
to be re-derived from prose:

- `configured_timeout` — the `timeoutPerCommand` that was applied (seconds).
- `elapsed_seconds` — wall time of the command execution only; `null` (never
  `0`) when nothing ran (fully cached, or a slot wait expired before the
  spawn).
- `slot_wait_seconds` — the host test-slot queue wait, measured once by the
  pacer and **never** folded into `elapsed_seconds`; on a slot-busy result it
  is the elapsed bounded wait.
- `command` — the suite command that was run.
- `exit_code` / `signal` — the process exit code, with the terminating signal
  when the child was killed. An exit code is reported only when a suite
  actually reported one; the report-shape fallback is never presented as a
  fact.

Where a runner predates the `killed`/`unparsed_failure` codes the gate
classifies the *shape*: a negative per-suite `returncode` is `killed`, and a
positive one with no parsable failure is `unparsed`. The test skill's
failure-notice contract (fields, units and codes) is documented in
[test-skill-reference.md](test-skill-reference.md#notice-codes-stable-strings).

The returned dict includes `skipped` (bool) and `tooling` (str | None) in
addition to `success`/`stdout`/`stderr`/`exit_code`/`failures` — and, for a
guarded (test-skill) run, `notices`/`notice_codes` so callers can explain a
failure without any structured failures.

### Finish gate: testing the tree the commit produces (SA-0MVL2NH1G0094FA2)

The finish gate is `phase_finish`'s test step, and its contract is one
sentence: **a green gate must mean the suite passed on exactly the tree the
commit will produce**. Before F6/F7/F8 neither half of that sentence was true
— the suite ran against the loose working tree while `git_commit` staged
*after* the test step, and a top-level full-suite run could be served from a
cache entry recorded for a different state. The two properties below are only
meaningful together, and both are pinned by regression tests.

1. **The suite runs against the would-be-commit tree.** The pending change
   set is staged into a temporary copy of the index and exported as
   `GIT_INDEX_FILE`, so `git ls-files` guards (and any other tracked-file
   reader in the suite) see exactly what the commit will contain.
2. **The verdict is bound to that tree.** The cache key, the report's
   fingerprint and the completion comment all name the tree SHA of that
   staged index (`git write-tree`), never the loose working-tree state. A
   result can therefore not be attributed to a tree whose suite never ran.

#### Gate semantics: the tested tree is the would-be-commit tree (SA-0MVQHVGFR006W8LG)

`phase_finish` delegates its test step to `run_gate_suite()`, which wraps
`run_tests()` in `would_be_commit_index()`:

- **Temporary index.** The real index is copied to
  `<git-dir>/implement-gate-*.index` (a missing real index is seeded with
  `git read-tree HEAD`), the change set is staged into the copy with
  `git add -A` under `GIT_INDEX_FILE`, and its tree SHA is computed with
  `git write-tree`. The real index and the working tree are not modified.
- **`GIT_INDEX_FILE` is exported for the suite.** Every child `git` command a
  test spawns inherits it, so tracked-file guards see the staged file set
  instead of the loose working tree. `GIT_INDEX_FILE` intentionally stays out
  of the test skill's repository-override scrub for this reason — the
  pass-through is itself pinned by `tests/test_git_index_file_env.py`
  (SA-0MVQHV9Y3001RNNW).
- **The real index receives the change set only on green.** `stage_tested_tree()`
  runs the same `git add -A` `git_commit` would run and asserts the real
  index's `git write-tree` equals the tested tree. A mismatch (a build step
  or concurrent writer changed files after the gate ran) unstages again,
  creates no commit, and returns an actionable drift message.
- **The temp index cannot be silently mutated.** After the suite,
  `verify_unchanged()` recomputes the temporary index's tree; a difference
  means a test staged into the inherited index, so the verdict is forced red
  with a `gate evidence invalid` notice instead of being attributed to a tree
  that was never tested.
- **The gate's own git commands act on the real index.** `run_cmd()` drops an
  ambient `GIT_INDEX_FILE` by default; only suite runners opt back in with
  `keep_git_index=True`, and the plumbing helper `_run_git_plumbing()` always
  selects its index explicitly (`index_file=…` sets it, `None` removes it).
- **Report.** `report["steps"]["would_be_commit"]` records the tested tree,
  the temporary index path, the real index path, the staged paths and
  `index_unchanged`; `report["steps"]["would_be_commit_assertion"]` records
  the real-index verification (`success`, `tree`, `expected`, `verified`).

**Scope stays changed-only at finish.** The gate runs `scope="changed"` as the
development-time (pre-commit) gate; the **full** suite is deferred to the
dev-push integration gate — the pre-push hook's `run_tests.py --scope full`
and the ship release gate (SA-0MVECQUGF0064U81). The tree-binding contract
applies to whichever scope the gate runs.

**Repeat finish (no diff, already pushed) and the full-scope fallback
(SA-0MW0V9PEP004EJYV).** Re-running `finish` on a clean, fully-committed
worktree (the documented recovery path after a failed push) leaves the
changed-scope selector with nothing to select: with no changed files, group
selection and the changed-file → test-file selector both come back empty and
`run_tests()` falls back to the **full** suite — still tree-bound, still run
inside `would_be_commit_index()`. Three properties make that repeat finish
converge instead of failing the gate:

- **The full-scope fallback is served from the dev-push tree entry.** The
  pre-push hook's `run_tests.py --scope full --gate-commit <sha>` records its
  green verdict under `fingerprint_kind="tree"` keyed on the pushed commit's
  tree (SA-0MW7C6AYO0062GJB). A repeat finish fingerprints the same tree — on
  a clean worktree the would-be-commit tree *is* the pushed commit's tree — so
  the full-scope fallback's cache lookup hits that entry (same command, same
  tree namespace, within TTL) and the suite is **not** re-executed. No entry
  (or an expired one) genuinely re-runs the full suite and still converges;
  the reuse is an optimisation, never a prerequisite.
- **No test may stage into the inherited index.** The suite runs under the
  gate's exported `GIT_INDEX_FILE`, and a fixture that honours it would trip
  `verify_unchanged()` → `gate evidence invalid` (the observed failure on a
  fresh repeat run). The repo-root `conftest.py`'s autouse `IndexWatch` names
  the offending test; the fixture rule is `shared.git_sandbox.run_git` /
  `fixture_git_env` (see `docs/dev/test-isolation.md` §9.7).
- **The terminal read-back proves convergence.** The no-op push (branch
  already on `dev`) still runs the post-push verification and `finish`
  persists `completed`/`in_review` exactly like the push-with-diff path — see
  [Terminal-status read-back](#terminal-status-read-back-no-op-push-guarantee-osl-0mvme1pi5005wu58)
  below. Regression coverage:
  `skill/implement/tests/test_finish_repeat_no_diff_cache.py` (cache-hit
  repeat finish, fresh-run repeat finish, index unchanged).

#### Verdict provenance (SA-0MVQHVJEX0071R7G)

Every finish-gate verdict is self-describing. Each entry of
`report["steps"]["tests"]` (one per gate attempt) records:

| Key | Meaning |
| --- | --- |
| `attempt` | 1-based attempt number. |
| `success` | The attempt's verdict. |
| `failures` | Structured failures reported by the suite (empty on green). |
| `skipped` | `true` only when a human chose `skip` in the interactive fix loop. |
| `tooling` | Suite tooling that ran (`"suite"`, a legacy runner name, or `null`). |
| `scope` | `"changed"` for the finish gate (the full suite runs at the dev-push gate). |
| `tree` | Absolute path of the worktree the suite ran in (`.resolve()`d). |
| `git_fingerprint` | The **would-be-commit tree** SHA (`git write-tree` of the staged temporary index) the verdict is bound to — not the loose working-tree state. `null` outside a git work tree. |
| `cached` | `true` only when the whole verdict was served from the per-repo test cache. |
| `cache_entry` | Present only for a cache-served verdict: the served entry's `command`, `completed_at` (epoch float), `scope` and `fingerprint_kind`. |
| `composed_engine` | The composition the suite ran against: per submodule the resolved checkout commit, the gitlink the would-be-commit tree records (`source`), and whether they match (SA-0MVZE164P0048306 AC4). `null` outside a git work tree. |

A cached green is accepted only when its fingerprint is the would-be-commit
tree's fingerprint: `run_gate_suite()` passes the temporary index's tree SHA
into `run_tests(..., fingerprint=...)`, which keys the lookup with
`fingerprint_kind="tree"` (SA-0MVQHVHXI007PQAB, contract documented in
[test-skill-reference.md](test-skill-reference.md)). An entry recorded for a
loose worktree state, for another tree, or without the provenance metadata is
a miss, so the suite runs and the fresh verdict is recorded for the gate's
tree — no green verdict can exist for a state whose suite never ran.

`report["steps"]["would_be_commit"]` records the same tree (plus the temp
index path, the staged paths and `index_unchanged`), so the report can be read
against the cache independently of the attempt list.

**The completion comment carries the same provenance.** After a successful
push the comment posted on the work item includes a *Test gate verdict* block
— tree, fingerprint, cached flag and (for a cache-served verdict) the cache
entry's command, completion time, scope and fingerprint kind, plus the attempt
count when the interactive fix loop re-ran — so an operator reading only the
work item can attribute the accepted green.

#### Abort behaviour: a failed gate leaves no trace (SA-0MVQHVCRE0070YO6)

Every non-green exit — a failing suite, an abort in the interactive fix loop,
an exception escaping the gate, or a signal — leaves the repository exactly as
the gate found it:

- **No commit.** `HEAD` is unchanged: `git_commit` and the real-index staging
  (`stage_tested_tree()`) only run after a green verdict.
- **No residual staging.** The real index is byte-identical to its pre-gate
  state and `git diff --cached --quiet` holds; the change set lives only in
  the temporary index while the gate runs. A tree-drift mismatch reverses the
  staging it just performed with `git reset --quiet`.
- **The working tree is untouched.** No gate path writes a file.
- **The temporary index is removed on every exit path.** The staging context
  manager's `finally` and the SIGTERM/SIGINT handler share
  `_remove_gate_index()`, which deletes the index (and any `*.lock`) and also
  restores any previously exported `GIT_INDEX_FILE` value. The abort
  regression module (`skill/implement/tests/test_finish_gate_abort.py`) proves
  this from two independent detectors — the path the suite actually observed,
  and index-shaped artefacts in the git directory and an isolated `TMPDIR`.
- **A mutated temporary index is a red verdict, not an abort.** If a test
  stages into the inherited index, the gate reports `gate evidence invalid`
  and the temp index is still removed; the refusal message names the remedy
  (`shared.git_sandbox.fixture_git_env()` for fixture git commands).

The work item is reset to `open` on a failed gate (`StatusLifecycle` abort
semantics), so a red gate never leaves a stranded `in_progress` item.

#### Single-execution guarantee (SA-0MVL2NH1G0094FA2 AC5)

A clean change set costs exactly **one** suite execution per gate verdict:

- `phase_finish` calls `run_gate_suite()` once, then `stage_tested_tree()`, then
  commits. There is no second, duplicate run of the changed-scope suite, and no
  full-suite run at finish (deferred to the dev-push gate).
- A **cache-served** verdict is served, not produced: the suite does not run a
  second time, and the attempt is recorded with `cached: true` plus the served
  entry's metadata so the reuse is visible rather than implicit.
- A further execution happens only after a **failure**, and only in the
  interactive fix loop (`Enter` to re-run, up to the retry bound,
  `--max-retry`). Non-interactive (JSON) runs report the failing attempt and
  return without re-running. Each attempt appends its own provenance record.
- A `skip` chosen in the interactive fix loop marks that attempt
  `skipped: true`; it does not spawn another suite execution.

#### No default bypass (SA-0MVL2NH1G0094FA2 constraints)

The gate has no environment-variable or default bypass: there is no
`TEST_SCOPE_SKIP`-style switch, and `implement.py` exposes no “skip the test
step” setting. The only ways past a failing gate are (a) fixing the failures
and re-running (the fix loop), or (b) a **human** typing `skip` at the
interactive prompt, which is recorded in the report as
`report["steps"]["tests"][-1]["skipped"] = true`. Neither weakens the overall
contract: the **dev-push pre-push hook and the ship release gate run the full
suite independently**, so a skipped or stale finish gate can never land a red
tree on `dev` and still be reported as a verified release.

Guard ownership (`LIVE_REPO_GUARD_ACTIVE`) and the nested-run stand-down
contract are unchanged by tree binding: the suite still runs through
`guarded_run_all()` and consumes a host-wide test slot exactly as documented
above.

#### Fail-soft outside a git work tree (SA-0MVVKJGKP003QVNM)

Staging a would-be-commit tree needs a repository, so the gate first checks
`git rev-parse --is-inside-work-tree`. Outside a work tree there is no commit
tree to attribute a verdict to: the suite then runs plainly (no temp index,
no `GIT_INDEX_FILE`) and `report["steps"]["would_be_commit"]` records
`available: false` with the stable `reason: "not_a_git_worktree"` and a
`notice`. `phase_finish` records
`report["steps"]["would_be_commit_assertion"]["skipped"] = true` instead of
running `stage_tested_tree()` and logs a warning. Every real finish runs
inside a worktree, so the tree-verification guarantee above is unchanged in
production; the degradation exists so a stubbed finish harness (or any
non-repository caller) is not refused before the suite runs.

#### Harness contract for `phase_finish`

A harness that drives `phase_finish` from a bare temp directory should stub
**both** gate seams in addition to `run_tests`, so the outcome never depends on
the absence of a repository and the `would_be_commit` /
`would_be_commit_assertion` report fields keep describing a real staged tree:

- `implement.run_gate_suite` → `(tests_result, gate_summary)`, where
  `gate_summary` carries `tree` and `index_unchanged=True`;
- `implement.stage_tested_tree` →
  `{"success": True, "tree": <tested tree>, "expected": <same tree>}`.

Stubbing only `run_tests` leaves the real temp-index staging to run against a
non-git directory whenever the fail-soft path does not apply (or is later
tightened), which fails the harness for reasons unrelated to the behaviour
under test (SA-0MVVX85KW001ZHE7).

Harnesses that run **fixture** git commands must pass
`shared.git_sandbox.fixture_git_env()`: the gate exports its temporary index as
`GIT_INDEX_FILE`, so an ambient index would redirect the fixture's
`git add`/`commit` into the gate's index — invalidating the verdict with the
"gate evidence invalid" notice *and* corrupting the fixture's own state
(`tests/test_implement_finish_local_parent_branch.py`). The gate's staging
behaviour itself is covered by `skill/implement/tests/test_finish_gate_*.py`,
so the harness stubs do not duplicate core coverage.

### Build-originated composition churn (SA-0MVZE164P0048306)

A build may legitimately change the engine composition it is built against.
In a game repo `npm run build` starts with `npm run engine:sync`, which — when
the tracked engine branch tip has advanced past the worktree's pin — runs
`git submodule update --remote core`, rewrites `configs/engine-pin.json` and
stages the pin **plus** the `core` gitlink. Left alone, `git_commit`'s blanket
`git add -A` then commits that engine bump: an engine change the item never
originated, sitting in a commit whose subject describes unrelated work.

The finish gate therefore runs a **churn stage between the build and the
gate** (`_exclude_build_originated_churn`, reported under
`steps.build_churn`):

1. **Baseline before the build.** `_build_churn_baseline()` snapshots the
tracked dirty set (a per-path state token: the blob hash of a regular file,
the checked-out commit of a submodule) and `HEAD`'s gitlinks immediately
before `run_build`. Refactor edits are already in that snapshot, so they count
as the item's own change.
2. **Restore what the build produced.** After the build, a path that was
**clean before the build** and is dirty after it is build-originated. It is
restored to the pre-build `HEAD` state: files with
`git checkout HEAD -- <path>`, gitlinks with `git reset HEAD -- <path>` **and**
a submodule `checkout --detach <recorded gitlink>` — the second half is what
makes the *tested* composition equal the *committed* one rather than leaving
the suite on the advanced engine.
3. **The exclusion is conditional on origin, never an ignore list.** A
pin/gitlink that is already part of the item's change set before the build is
preserved untouched, and a path the item had already modified that the build
rewrote further is reported as `item_modified_during_build` and **never**
restored — rolling it back would discard the item's own work.
4. **Unrestorable churn fails closed.** A gitlink that cannot be restored (for
example a submodule whose worktree carries local modifications: the checkout
refuses instead of discarding them) leaves `restore_failed` non-empty, so
finish refuses with an actionable message, resets the item to `open` and
creates no commit. Churn is never force-discarded and never silently
committed.

Report shape (`report["steps"]["build_churn"]`):

| Key | Meaning |
| --- | --- |
| `baseline_dirty_paths` | The item's own change set, as snapshotted before the build. |
| `build_originated` | Paths clean before the build and dirty after it. |
| `item_modified_during_build` | Paths the item had already modified **and** the build touched further — always kept. |
| `restored` | `[{path, kind, restored, commit?}]` — the churn actually undone (`kind` is `file` or `submodule`). |
| `excluded_from_commit` | Paths restored, therefore absent from the item's commit. |
| `left_in_place` | Build-originated paths that have no `HEAD` state to restore (e.g. a gitlink the build added). |
| `restore_failed` | Paths that could not be restored — non-empty means finish refuses (`reason: "restore_failed"`, classified as a `gate` failure). |
| `composed_engine` | The resolved composition after the stage, compared against `HEAD` (`source: "HEAD"`). |

Two path families are outside this stage by design: untracked build output
(`git diff HEAD` sees tracked state only) and the finish run's own bookkeeping
surface (`.worklog/` — `wl` comments, status and cache writes are never
composition, so a concurrent bookkeeping write is never mistaken for build
churn and restored).

#### The composed engine SHA in every verdict (SA-0MVZE164P0048306 AC4)

`_composed_engine_state()` resolves, for every gitlink recorded by the tree
under test, the commit the submodule **checkout** is actually at — the
composed engine the suite ran against — and compares it with the gitlink that
tree records. Each gate attempt (`report["steps"]["tests"][*]`) therefore
carries `composed_engine`, alongside `tree` and `git_fingerprint`:

```json
{"composition": "submodules",
 "submodules": [{"submodule": "core", "commit": "c86808c6…",
                 "recorded_gitlink": "c86808c6…",
                 "source": "would-be-commit tree",
                 "matches_recorded": true}]}
```

`source` is `would-be-commit tree` for a gate attempt (the gitlinks of the
temporary index's `git write-tree`, so `matches_recorded` is exactly the
"tested composition equals committed composition" check) and `HEAD` for the
pre-gate churn stage. `report["steps"]["would_be_commit"]` carries the same
block, and the completion comment gains a
`- Composed engine: core@<sha> (recorded gitlink <sha> in <source>, match: <bool>)`
line (or `none` when the tree records no submodule composition), so the green
is attributable to a specific engine composition from the work item alone.

The cache key is **not** touched: `compute_git_state` still fingerprints the
pin/gitlink content, so a green entry recorded against a different engine is
never served (the rejected remedy B). `engine:sync` keeps running on dev
builds and the release path keeps using `engine:verify` (the rejected remedy
C); the pin/gitlink pair restored here is the pre-build pair, which was
coherent, so the pair stays coherent either way.

### Engine lag on a failed build (SA-0MW06FLFR009F0P4)

`npm run build` in a game repo runs `engine:sync` **first**, so a build can
advance `./core` to the tracked-branch tip and rewrite the pin — and *then*
fail, because the item's source predates a breaking engine API change (the
reproduced `hasChoices`/`isChoice` rename). The advance used to be invisible:
the failure comment posted only `build_result["stderr"][:500]`, while
`engine:sync` reports the advance (`engine:sync: core updated <from> → <to>`)
and the pin rewrite on **stdout**. The data was already there, unused on the
failure path: `_build_churn_baseline()` records the pre-build gitlink (the
item's pin) and the submodule checkout `HEAD` is the engine the build actually
compiled against.

`_engine_lag()` compares the two and reports the result **additively**:
nothing is restored, staged or committed, the churn exclusion above is
unchanged, and `compute_git_state()` is untouched (the cache key is not
relaxed). The field is present only on the build-failure path, in the
returned report and in the printed JSON
(`report["steps"]["build"]["engine_lag"]`):

| Key | Meaning |
| --- | --- |
| `submodules` | One entry per gitlink the pre-build `HEAD` recorded (empty for a repo with no submodule). |
| `submodules[].submodule` | Repository-relative submodule path (e.g. `core`). |
| `submodules[].pinned_sha` | The composition staged at build start — the item's own pin when it changed the composition itself, else the committed gitlink — so an item-originated composition change is never reported as a build advance. |
| `submodules[].composed_sha` | The submodule checkout `HEAD` the failed build compiled against (`null` when it cannot be resolved). |
| `submodules[].advanced` | `true` only when the checkout differs from the pin. |
| `advanced` | `true` when any submodule advanced. |

An advance adds one diagnosis line per advanced submodule plus the
`engine:sync` lines found in stdout (at most 20), and the comment reproduces
the tail of **both** streams:

```
Build failed during finish phase (exit code 2).

**Engine advanced past the item's pin during the build.** The compile errors
below may come from engine APIs this item predates:
- core: build ran against engine 2305f888…; the item pinned 21bedf7a… — the advance is not part of this item's commit

**engine:sync output**
engine:sync: core updated 21bedf7a8c → 2305f888d0

**stdout (tail)**
…

**stderr (tail)**
…
```

When nothing advanced — or the repo records no submodule — `advanced: false`
and the comment carries **no** engine section: finish behaves exactly as
before. A build with no `engine:sync` output at all (Python-only repo, skipped
build, a sync failing before it prints) is handled without error, because the
lag is derived from git state rather than parsed output.

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

### Push transport retry (SA-0MVSJX5HS002DJHI AC4, SA-0MW22SEMV00230O5 AC1)

A transient transport close — `Connection to <host> closed by remote host`,
`Connection reset by peer`, `remote end hung up unexpectedly` or
`kex_exchange_identification` — used to abort an otherwise-green push with no
retry. `git_push_to_dev()` now retries those stderr patterns up to
`PUSH_RETRY_MAX_ATTEMPTS = 3` attempts (the initial push plus two retries)
with jittered exponential backoff (`PUSH_RETRY_BASE_SECONDS = 2.0`, doubling,
capped at `PUSH_RETRY_CAP_SECONDS = 30.0`, +/-25% jitter).

**AC1 — auth/transport is infrastructure, never a bare push failure.** The
classifier (`classify_push_failure`) extends the retryable set to the
concurrent-run failures observed under multiple agent sessions:
`Permission denied (publickey)`, `Could not read from remote repository`,
`Connection reset` / `Connection timed out`, and a remote `Internal Server
Error`. All are treated as transient transport — a fresh key handshake per
connection (no `SSH_AUTH_SOCK`), a connect timeout/reset or a GitHub-side 5xx
— retried on the bounded budget with backoff, and reported on exhaustion as
`reason: transport` (infrastructure, on the drive classifier's infra
allow-list). Branch-policy rejections (`GH006`), hook declines and
non-fast-forward errors are deliberately **not** in the retryable set: those
are content conditions that retrying cannot fix (`classify_push_failure`
returns `content`, a hard stop).

- **Same cwd, same argv:** the retry re-runs the identical
  `git push origin <branch>:refs/heads/dev` from the same checkout, so the
  pre-push gate runs again against the same tree and serves its cached
  full-suite entry — N transport retries never mean N full suites (AC6).
  Nothing bypasses the gate.
- **Everything else fails as before:** branch-policy refusals and a
  non-fast-forward rejection are never retried. A non-fast-forward keeps its
  dedicated rebase-and-retry recovery, and a push timeout raises
  `PushTimeoutError` immediately.
- **Observable:** every attempt logs `attempt N/3`, the delay and the same cwd;
  the attempt bound and backoff are module constants, and the sleep is the
  `_push_retry_sleep()` seam used by the hermetic tests. The trail is also
  carried in the push helper's `evidence_out` (`attempts` plus `retry_trail`
  entries of `{attempt, delay_seconds, reason}`), which `phase_finish` exposes
  on `report["steps"]["push"]` for both a successful and a gate-refused push —
  so a `--json` finish report shows the attempt trail, not just a log line
  (SA-0MVXEQEI500641SJ AC3). Auth/connect/5xx retries record `reason:
  "transport"` in the trail; a connection close records `"transport_close"`.

### Bounded leaf push loop, already-landed short-circuit and push outcomes
(SA-0MW22SEMV00230O5 AC2/AC3/AC4/AC5/AC6/AC7)

A plain leaf `finish` now absorbs the failures that used to strand items
under concurrent runs (a driven child was already retried on the infra budget
by `phase_drive`; the leaf was not):

- **Non-fast-forward is rebase-and-retried within the bounded budget.**
  `phase_finish` runs the push in a loop capped at `DEFAULT_INFRA_RETRY_LIMIT`
  rounds; each round fetches `origin/dev`, rebases the feature branch onto
  it and re-pushes from the worktree. A *second* concurrent advance (the
  observed repeated-race case) is recovered instead of failing the item. A
  rebase conflict is a genuine stop: `_recover_non_fast_forward_push` raises
  with an actionable message and never forces or silently mutates the worktree.
  Exhaustion records `reason: non_fast_forward` with `retries`/`budget` on the
  push step and posts a comment with the manual recovery commands.
- **Already-landed short-circuit (AC3/AC5).** Before the push, `finish` asks
  `_commit_is_on_origin_dev` — a **remote-verified** ancestry query that
  fetches `origin` first, never trusting a stale remote-tracking ref. When
  the commit is already an ancestor of `origin/dev` the push is treated as
  satisfied and **skipped** (`outcome: already_pushed`): re-running `finish`
  over a commit that an earlier run published converges straight to
  `in_review`, and a transport flake can never regress a landed item. The
  remote-verified result is also used as Step 6a's ancestry assertion (the
  fetch is not repeated).
- **No open regression by a transport failure (AC5).** A transport/connection
  failure re-checks remote ancestry before failing: if the commit landed
  concurrently it converges (`outcome: already_pushed`), and the outer
  failure handler reclassifies an already-landed commit as
  `push_landed_but_unverified` — never the misleading "push it manually".
- **Push outcomes (AC4).** Every `finish` push step and its work-item comment
  name the outcome: `pushed` (this run landed it), `already_pushed` (it was
  already on `origin/dev`), `push_failed` (nothing landed — the specific
  `reason` names why: `transport`, `timeout`, `non_fast_forward`, a
  `gate_reason=…` code, …), or `push_landed_but_unverified` (landed, but the
  post-push full-suite verdict could not be proven from the read-only cache,
  or the ancestry assertion failed).
- **At most one full-suite gate per finish (AC6).** Transport/fast-forward
  retries only re-run the **push**; the tree-keyed gate verdict is served from
  the cache, and the changed-scope gate (Step 3) is never re-entered by the
  retry loop.
- **Load-flake gate failures are labelled, not misread as code failures
  (AC7).** When a gate refusal's stderr names suite *load* errors (`Cannot
  find package …`, `Cannot find module …`, `MODULE_NOT_FOUND` — a missing
  shared build, an import failure), `phase_finish` retries the identical tree
  exactly once. When the retry passes, `report[steps][push][load_flake]`
  records `{failed_on_attempt, reason: "load_flake"}` and the log says
  *suite failed to load on attempt N, passed unmodified on attempt N+1* — so
  an environment flake under concurrent load reads as infrastructure, never
  as a regression. A reproducible load failure (both attempts fail) still
  blocks the push: the gate is never weakened.

### SSH keepalives and the transport-lost failure (SA-0MVWNHLQQ002TFZR)

Retrying alone does not fix a **deterministic** idle close. `.githooks/pre-push`
runs the full suite *after* git has opened the SSH connection and produces no
SSH traffic for the whole run (measured 11 min warm, 35 min cold), so a remote
that closes idle connections (GitHub does) drops the connection before the ref
update. Every attempt then fails the same way, burning a full suite run each
time, and the output reads as success ("full suite green") while `origin/dev`
never moves.

- **Keepalives travel with the code (AC1/AC2).** `git_push_to_dev()` sets
  `GIT_SSH_COMMAND` in its **push child environment** to an ssh invocation
  carrying `-o ServerAliveInterval=30 -o ServerAliveCountMax=20 -o
  TCPKeepAlive=yes` (`SSH_KEEPALIVE_OPTIONS`). Because it is applied by the
  push itself, a fresh clone or worktree gets it with no operator
  `~/.ssh/config` edit. The operator's own `GIT_SSH_COMMAND` is preserved: an
  existing `ssh ...` invocation has the options spliced after the `ssh` token,
  a command that already carries `ServerAliveInterval` is left untouched, and
  an unrecognised wrapper is kept (with a warning) rather than broken.
  (`ServerAliveInterval` is the effective option — the OS-level `TCPKeepAlive`
  alone defaults to ~2 h.)
- **A lost ref update is a distinct, loud failure (AC3).** When the bounded
  retries are exhausted on a transport close, `git_push_to_dev()` raises
  `PushTransportLostError` instead of returning a generic `False`. It records
  `evidence_out = {success: False, reason: "connection_closed",
  transport_lost: True, attempts, retry_trail, stderr_tail}` and `phase_finish`
  reports `report["steps"]["push"]["reason"] = "connection_closed"` with a
  comment that says *transport lost — not a test failure*. This distinguishes
  it from a red suite (`gate_reason=test_failure` / `reason: test_failure`),
  and `connection_closed` is on the infrastructure allow-list for the drive
  retry classifier, so a driven child is retried on the infra budget. The
  pre-push hook's green line is worded as a *gate* verdict ("test gate GREEN
  … the ref update follows") so it can never be misread as a landed push.
- **Regression coverage (AC4).** `skill/implement/tests/
  test_implement_push_transport_keepalive.py` simulates the closed connection
  (scripted `Popen` stderr) and asserts the keepalive child env, the distinct
  `PushTransportLostError`, `reason: connection_closed` rather than
  `test_failure`, and the infrastructure classification.

### Dev-push gate reason reporting (SA-0MVSJX5HS002DJHI AC2)

`.githooks/pre-push` prints a stable `gate_reason=<code>` line on failure,
derived from the runner's machine-readable aggregate via the shared
`skill/test/scripts/gate_reason.py` classifier, resolved repo-local first and
then from the installed global skill (`$HOME/.pi/agent/skills/test/scripts/`,
SA-0MVXEQE01008JW5R AC4) with a conservative `infrastructure` fallback when
no tier holds an importable copy. When the push fails,
`git_push_to_dev()` parses that line into
`evidence_out = {success: False, gate_reason, stderr_tail}` and
`phase_finish` raises `PushGateFailureError` carrying it. The finish report
then records `report["steps"]["push"]["reason"] = <code>` (for example
`slot_contention` or `test_failure`) and posts a work-item comment naming the
reason, the remedy and the gate output tail — instead of the generic
`reason: push_failed` / "git push to dev failed" that told an agent nothing
about fix-versus-retry. The reason vocabulary is the same one the
changed-scope gate reports (see
[gate failure diagnostics](#gate-failure-diagnostics-wl-0mvgut787000lndg));
the hook only consumes the runner's published notice codes, never re-defines
them.

#### Pushed-commit guard refusal (SA-0MW3H07A8004Q54C AC3)

§3a of `.githooks/pre-push` refuses a dev/main push **before** the suite runs
when the tree it would measure is not the pushed commit (or is dirty at it),
so it prints no `gate_reason=` line. `git_push_to_dev()` recognises the guard's
stable diagnostic instead and reports the distinct reason
`refused_by_pushed_commit_guard`, carrying the values the guard named:
`evidence_out["pushed_commit_guard"] = {pushed_sha, gate_head, gate_tree}`.
`phase_finish` records `report["steps"]["push"]["reason"] =
"refused_by_pushed_commit_guard"` plus
`report["steps"]["push"]["refused_by_pushed_commit_guard"]` with those three
values, and its remedy names the worktree push (`git push origin
HEAD:refs/heads/dev` from the worktree that holds the pushed commit). This is
the recovery the guard itself documents; finish's normal path already pushes
from that worktree, so the refusal is a fail-closed safety net, not a routine
outcome.

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
rather than assumed. A **repeat finish** (re-running `finish` on an
already-pushed, clean worktree — the failed-push recovery path) converges
through this same no-op-push read-back; see the full-scope-fallback note
above]

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

### No-commit finalisation: refusal by default, `--verification-only` opt-in

`git_commit` used to return `True` both when it created a commit **and** when
the tree was clean, so `phase_finish` read the ambient `dev` HEAD (which can
be another item's published commit), pushed a no-op and wrote
`completed`/`in_review` for an item that produced no work. That is the silent
`in_review` lie this slice removes (SA-0MW74IJWI009NRZ1).

`git_commit` now returns a structured `CommitOutcome`
(`success`/`committed`/`hash`/`reason`); `bool(outcome)` keeps the historical
success/failure contract, so a caller that only tests truthiness is unchanged.
`phase_finish` branches on `committed` **and** on whether an existing commit is
attributable to the item (SA-0MW98UNVB0085LFA AC1):

| Commit outcome | Signal / attribution | Behaviour |
|---|---|---|
| a commit was created | — | Fast-forward, push, ancestry assertions, terminal write (unchanged) |
| clean tree, no new commit, an **attributable commit ahead of `origin/<parent_branch>`** | — | **Prior-run attribution**: the existing commit is named in the report, the completion comment and the retained-state interruption record, then fast-forwarded, pushed and finalised exactly like a fresh commit |
| clean tree, no commit | `--verification-only` **or** `verification_only` in the state file | Verification-only finalisation: no-op push, terminal write; report and completion comment say “no changes / no commit” and never name a hash |
| clean tree, no commit, no attributable commit | neither | **Refused**: `success: false`, `steps.commit.reason = "no_commit_no_signal"`, no push, no terminal write, no completion comment; the item is released to `open` and the retained worktree is named with the exact opt-in |

**Interrupted-push re-run (SA-0MW98UNVB0085LFA).** A finish interrupted during
its push leaves a clean worktree whose commit already exists on the branch and
on local `dev` but not on `origin/dev`. Re-running `finish` creates no new
commit, so — instead of refusing — it attributes the **prior run's commit**:
the newest commit ahead of `origin/<parent_branch>` whose message references
the work-item id (`_commit_attribution`). The base is `origin/<parent_branch>`,
not local `dev`, because after an interrupted push the commit is already on
local `dev` and a local base would report “0 ahead”. Attribution stays strict:
a commit that does not mention the item is never claimed, so ambient HEAD from
another item is not attributed and a genuinely work-less worktree is still
refused (`no_commit_no_signal`). The refusal wording states that no commit is
attributable to the item — it never falsely claims “nothing was committed or
pushed”.

`start <id> --verification-only` records the flag in
`.implement_state.json` (the single source of truth `drive` also reads);
`finish <id> --verification-only` supplies it directly. Either is sufficient,
and an item that genuinely needs no code change is still finalisable —
deliberately.

### Work-less classification (`drive` / `phase_parent`)

The refusal is also applied **before** `finish` runs. `_classify_child_worktree`
refines `_has_worktree_changes` into the question finish needs — `work`,
`verification-only`, `no-work` or `clean` — and a `no-work` child (state-file-only
dirty surface, or commits ahead not attributable to the item) is reported
`action: "not-finalisable"` in `children_processed` plus a structured
`not_finalisable_children` key. `finish` is never run for it, the child stays
open, and the parent is not advanced (SA-0MW74IKJE008JXDQ AC4). `finish`'s own
refusal is the defence in depth for a direct invocation.

Regression pin (same file): the default-refusal path (no terminal write, no
misattributed hash, item released to `open`) and the verification-only path
(terminal write persists, store read-back passes, report says no changes).

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
itself operates on the main checkout — Step-5 fast-forward, Step-6b worktree
removal and the post-push ancestry assertion (the Step-6 dev push now runs from
the worktree that holds the pushed commit, SA-0MVTFMB7K002Y1F3) — so a foreign
actor mutating the main checkout mid-finish (the 2026-10-26 `packages/shared`
wipe) could still be silently carried into the main checkout's bookkeeping or
a subsequent `git commit -a`. The finish main-checkout guard closes that
window on the success path **and** on every pre-push abort path
(SA-0MVXVBYXI000QMXM):

- **Snapshot at run start** (`_snapshot_main_checkout`): the main
  checkout's working-tree status surface (`git status --porcelain=v1`,
  `{path: code}`, `.worklog/` filtered out) is captured right after the
  placement gate — before build/test/commit.
- **Verify on every pre-push abort**: build failure, gate failure (JSON mode
  and retries exhausted) and the interactive user abort each record a verify
  before `finish` returns, so `verifies` is **never empty** on a failure
  report produced by those paths (the 2026-10-31 shape — `verifies: []` on a
  gate abort while 37 foreign deletions sat in the main checkout — can no
  longer happen). The refactor check records an **advisory** verify (see
  below).
- **Scope of the guarantee**: the guard's verify points are the stages in its
  table. Two refusals are raised *before* either verify point — the build-churn
  exclusion refusal (Step 2b) and the stale shared-package resolution refusal
  (Step 2c) — and return with their own loud messages without recording a
  verify. They are worktree-local refusals outside the guard's failure-path
  set; an operator reading a report from one of them sees the refusal message
  itself, never `verifies`.
- **Verify before the push** (Step 5b, `pre_push`): after the fast-forward,
  `own_churn` (the fast-forward's own file list, `git diff --name-only` of
  the pre-fast-forward HEAD → the merged commit) is attributed to finish and
  excluded; everything else that changed aborts.
- **Verify before worktree removal** (Step 6c, `pre_worktree_removal`): a
  mutation that appeared while the push ran (e.g. a concurrent actor) must
  still abort loudly.

### Verify stages

Every verify calls `_verify_main_checkout`, which appends
`{stage, at, verdict, offending?, actor_evidence?}` to `guard["verifies"]`
and updates `guard["verdict"]`. `VERIFY_STAGES` (in
`skill/implement/scripts/implement.py`) is the complete vocabulary; a
docs-consistency test (`skill/implement/tests/test_guard_docs.py`) fails when
this table and the constant drift apart.

| Stage | When it runs | Aborts the run? |
|---|---|---|
| `refactor` | the Step-1 refactor check reports issues | **no** — advisory: report entry plus a wl comment, then finish continues |
| `build_failure` | the Step-2 build fails | yes (the run is already failing) |
| `gate_failure` | the Step-3 gate fails in `--json` mode, or every fix-and-re-run retry is exhausted | yes (already failing) |
| `user_abort` | the interactive fix loop's `abort` choice (or EOF/`KeyboardInterrupt`) | yes (already failing) |
| `pre_push` | after the Step-5 fast-forward, immediately before the push | yes — `_abort_on_main_checkout_violation` |
| `pre_worktree_removal` | after the push, before the worktree is removed | yes — `_abort_on_main_checkout_violation` |

A verify that passes still records a `pass` entry, so a green main checkout is
observable on every path (e.g.
`test_finish_main_checkout_guard.py::test_clean_gate_abort_records_passing_failure_verify`).
The failure paths record their verify **after** the path's own failure action:
a build failure has already posted its build-failure comment, the
test-retries-exhausted path its gate-failure comment, the user abort its abort
comment; the
JSON-mode gate failure posts no failure comment (the report carries
`message`/`test_phase` instead) and the interactive `input()` prompt never runs
in that mode.

### Loud-comment contract (both comments, never one instead of the other)

On a violation the guard posts the same message via `wl_add_comment` on every
stage, aborting or advisory:

- **Two comments, in order, on the failure paths.** The path's pre-existing
  failure comment is posted first, then the guard's main-checkout comment — a
  guard comment never *replaces* the failure explanation (fail loud, never
  silent).
- **Advisory refactor comment.** The refactor verify is evaluated for its own
  sake — `run_refactor` failure is a non-gating warning today — so its comment
  is prefixed `Advisory only — this guard verify does not abort the run
  (refactor step):`. Treating it as an abort source would create a new
  false-positive class from a pre-existing advisory step
  (SA-0MUIJAHXS003M7BG).
- **Aborting stages raise after commenting.** `_abort_on_main_checkout_violation`
  (push-path stages) and `_record_main_checkout_verify(..., aborting=True)`
  (failure-path stages) post the comment and then abort; `phase_finish` raises
  inside the `StatusLifecycle` context, so the work item stays `open` (never
  `completed`/`in_review`) and the audit trail survives the status reset.
- **Message content.** The abort message names the stage, **every** offending
  path (`<path> [<code>]`, capped at 20 lines plus `… and N more`), the
  concurrent-actor evidence available at runtime (other worktree
  `.implement_state.json` entries, live processes whose cwd is the main
  checkout, or `unknown`) and the recovery command (`git checkout -- <path>`
  / `git restore <path>`), so an agent hitting the abort can act without
  spelunking.

Pre-existing dirty files present in the baseline snapshot and project-declared
expected-dirty paths are never a failure.

### Report fields

| Field | Meaning |
|---|---|
| `guard["repo_root"]` | The main checkout the guard snapshots and verifies (`_resolve_main_repo_root`, below) |
| `guard["snapshot_at"]` | ISO timestamp of the baseline snapshot |
| `guard["baseline_paths"]` | Number of dirty/untracked entries in the baseline snapshot |
| `guard["own_churn"]` | The attribution allow-list: checkout-relative paths from finish's own fast-forward that were observed dirty in this checkout (never a deletion) |
| `guard["verifies"]` | Ordered list of verify entries, one per verify point reached |
| `guard["verdict"]` | Verdict of the most recent verify (`pass` or `fail`) |
| `verifies[].stage` | The stage label from the table above (`VERIFY_STAGES`) |
| `verifies[].at` | ISO timestamp of that verify |
| `verifies[].verdict` | `pass` or `fail` |
| `verifies[].offending` | One `"<path> [<code>]"` entry per offending path — present only on `fail` |
| `verifies[].actor_evidence` | Concurrent-actor evidence string, or `unknown` — present only on `fail` |

### Attribution contract (`own_churn`)

`own_churn` is the only thing the guard allow-lists beyond `.worklog/` and
project expected-dirty paths, so it may name **nothing this run did not do in
this checkout** (SA-0MVXVC03O0034A7V). `_record_finish_own_churn` constrains it
three ways and `_main_checkout_guard_violations` enforces the rest:

- **The finished repo only.** `repo_root` must be the root of the finished
  working tree *and* its main checkout. `_repository_root_mismatch` refuses a
  subdirectory, a linked worktree or a sibling checkout loudly
  (`git rev-parse --show-toplevel` must be `repo_root` itself; the parent of
  `git rev-parse --git-common-dir` must be `repo_root`), and a refusal
  allow-lists nothing.
- **Observed churn only.** Every `git diff --name-only` path must be
  checkout-relative (absolute and `..`-escaping spellings are dropped), must
  resolve inside `repo_root`, and must have been **observed dirty in this
  checkout** after the advance (the post-fast-forward status map). Dropped
  paths are reported as a diagnostic in
  `report["steps"]["local_dev_fast_forward"]` (`not_observed`) and are never
  allow-listed — the 2026-10-31 cross-checkout attribution incident was a
  finish reporting seven paths that were merely *in the commit* local `dev`
  advanced to.
- **Deletions are never allow-listed.** A `D` entry is foreign by
  construction (a fast-forward cannot delete a file relative to the tracked
  tree), even when the path also appears in `own_churn`. A foreign spelling
  of a dirty path simply does not compare equal to the normalised
  checkout-relative status path, so the real path stays offending.

Proven by `skill/implement/tests/test_finish_cross_checkout_attribution.py`
(`test_own_churn_excludes_paths_not_observed_dirty`,
`test_own_churn_refuses_a_root_that_is_not_the_repository`,
`test_guard_never_allow_lists_a_foreign_spelling_of_a_dirty_path`,
`test_guard_still_never_allow_lists_a_deletion`,
`test_finish_never_acts_on_a_sibling_checkout`) and
`skill/implement/tests/test_finish_main_checkout_guard.py`
(`test_violations_own_churn_allow_list_never_hides_deletion`,
`test_violations_own_churn_list_only_matches_listed_paths`).

### Fail-closed `repo_root` resolution

`_resolve_main_repo_root` **derives** the main checkout instead of trusting
`state.repo_root`. The stored value is accepted only when it resolves to the
same main checkout `_get_repo_root` derives from the worktree; a stale value
(the worktree itself or a path inside it — MS-0MVM7NJF70037YU4) or a value
naming a sibling checkout is **rejected, logged and replaced by the derived
root**, because a trusted foreign root made `finish` snapshot, fast-forward
and push another repository. When derivation is impossible the documented
fallback (stated root, else discovery from the current directory) is logged
too, so a silently trusted value cannot exist. The removal safety gates stay
untouched — the fix supplies the correct root rather than relaxing a check.
Pinned by `skill/implement/tests/test_repo_root_resolution.py`
(`test_resolve_main_repo_root_rejects_stated_sibling_checkout`,
`test_finish_rejects_stale_state_repo_root_and_removes_worktree`,
`test_resolve_main_repo_root_falls_back_loudly_when_underivable`,
`test_remove_worktree_still_refuses_the_main_checkout`).

Regression pins overall: `skill/implement/tests/test_finish_main_checkout_guard.py`
(failure-path verifies, the loud-abort message, clean-run behaviour) and the
existing `skill/implement/tests/test_finish_no_diff_status.py` (must stay
green — a clean finish is unaffected).

## Write-surface confinement audit (SA-0MVXVC0KB004XDH0)

The guard and the cleanup path are safety mechanisms, so their *own* write
surfaces were audited: every write must land inside `repo_root`, inside the own
worktree, or in the worklog store — never in another checkout and never in an
unrelated directory. The table is the contract; every row is pinned by a test
that asserts observable behaviour (no source greps).

| Surface | Writes | Resolved root | Confinement | Evidence |
|---|---|---|---|---|
| `_snapshot_main_checkout` / `_main_checkout_status_map` | nothing — `git status --porcelain=v1` read only | `repo_root` | confined (read-only) | `test_finish_write_surface_confinement.py::test_guard_surfaces_leave_the_checkout_byte_identical` |
| `_verify_main_checkout` / `_main_checkout_guard_violations` | nothing — re-snapshot plus an in-memory verdict | `repo_root` | confined (read-only; never restores a foreign deletion) | `test_finish_write_surface_confinement.py::test_guard_verify_reports_a_foreign_deletion_without_touching_it` |
| `_main_checkout_actor_evidence` | nothing — `.implement_state.json` and `/proc/<pid>/cwd` reads | `repo_root` | confined (read-only) | `test_finish_write_surface_confinement.py::test_guard_surfaces_leave_the_checkout_byte_identical` |
| `_fast_forward_local_dev_for_push` | `git fetch` / `checkout` / `merge --ff-only` — refs and working tree | `repo_root` | confined — every git call uses `cwd=repo_root` | `test_finish_write_surface_confinement.py::test_main_checkout_git_operations_only_run_in_repo_root` |
| `_restore_local_dev_after_failed_push` | `git reset --merge` / `git update-ref` | `repo_root` | confined — same `cwd` rule | `test_finish_write_surface_confinement.py::test_main_checkout_git_operations_only_run_in_repo_root` |
| `_sync_parent_branch` | `git fetch` / `merge --ff-only` on the shared branch | `repo_root` | confined — same `cwd` rule | `test_finish_write_surface_confinement.py::test_main_checkout_git_operations_only_run_in_repo_root` |
| `git_push_to_dev` | remote refs plus the checkout's remote-tracking refs | the checkout holding the pushed commit (the worktree) | confined | `skill/implement/tests/test_implement_push_from_worktree.py` |
| `cleanup_worktree_processes` / `wl cleanup-worktree` | signals only PIDs the CLI registered under the exact path; no filesystem writes; spawned with `cwd` outside the target and in its own session | the named worktree | confined to the named worktree, invoker excluded | `tests/test_implement_cleanup_worktree_self_exclusion.py` |
| `write_state` / `remove_state` | `<worktree>/.implement_state.json` | the own worktree | confined | `test_finish_write_surface_confinement.py::test_state_file_surface_stays_inside_the_worktree` |
| `_remove_worktree` | `git worktree remove` plus a manual `shutil.rmtree` fallback | `repo_root` / the named worktree | **not confined — fixed here**: the fallback ran unconditionally and deleted any directory outside the repo that lacked `.git/`; a new gate refuses every target git does not register as a worktree of `repo_root` (and fails closed when the registry is unreadable) | `test_finish_write_surface_confinement.py::test_remove_worktree_refuses_a_directory_outside_the_repository`, `test_finish_write_surface_confinement.py::test_remove_worktree_refuses_when_ownership_cannot_be_proven`, `test_finish_write_surface_confinement.py::test_remove_worktree_removes_only_the_registered_worktree` |
| `acquire_main_checkout_lock` | `<repo_root>/.worklog/locks/main-checkout.lock` | `repo_root` | confined | `skill/implement/tests/test_finish_main_checkout_lock.py` |
| `would_be_commit_index` / `_remove_gate_index` | a temporary index inside the checkout's git directory | `repo_root/.git` | confined | `skill/implement/tests/test_finish_gate_temp_index.py` |
| `wl_add_comment` (the guard's fail-loud comment) | the worklog store, through the `wl` CLI | n/a (store, not a checkout) | confined — no checkout file is touched | `test_finish_write_surface_confinement.py::test_wl_add_comment_writes_the_worklog_store_not_the_checkout` |
| `StatusLifecycle` / `_safety_reset_if_in_progress` | the worklog store | n/a (store, not a checkout) | confined — no checkout file is touched | `tests/test_assignee_release_all_skills.py` |

Two consequences of the audit are worth naming:

- **A removal refusal never fails a finish or an abort.** A confinement refusal
  retains the tree and is reported (`report["steps"]["worktree_removal"]
  ["refused"]`, `report["worktree_refused"]` on abort) instead of turning an
  already-pushed finish into a failure.
- **Read-only means read-only.** The guard reports a foreign mutation and
  aborts; it never restores, reverts or "fixes" the offending path — the
  recovery command belongs to the operator.

Cross-repo *locking* is out of scope here (WL-0MVPE2DA7007WF84 /
WL-0MVWK20DK004TQZM); this section audits confinement only.

## Stale shared-package resolution gate (SA-0MVPI41SR008JYXL)

A worktree resolves a path-consumed workspace package (the reference case is
`@worklog/shared`) through its own `node_modules` link farm. When that farm is
missing or stale, the package resolves to the **main checkout's** build output
(`<main-checkout>/packages/shared/dist`), so the gate validates code the
worktree does not contain: a correct shared change false-reds
(WL-0MVK6S4HM005GBCO) and a broken one can false-green. The gate must never
report a verdict about the wrong code, so `finish` **fails closed** in one
narrow case instead of warning.

**One detector, two phases.** `_shared_resolution_check(worktree_path,
repo_root)` is the single source of truth for "does this worktree resolve the
shared package to its own `packages/shared`?". It returns
`{applicable, ok, package, expected_root, resolved_root, stale_dist}`;
`applicable` is `False` when the main checkout declares no such workspace
package (the degradation path) and a missing `node_modules` is *stale*, not
"not applicable". `start` reports it in the JSON report
(`report["steps"]["node_modules"]`, mirrored as `shared_resolution` /
`shared_resolution_ok`) and warns; `finish` uses the same helper to decide
whether to refuse.

**When `finish` refuses.** `_stale_shared_resolution(worktree_path, repo_root,
parent_branch)` fires only when **both** hold:

1. the worktree's change set touches `packages/shared/src/**` — uncommitted
   files *and* commits ahead of `parent_branch`'s merge-base
   (`_worktree_change_set_paths`), and
2. the resolution check is applicable and **not** `ok`.

Either condition alone is a no-op: an unchanged shared package never blocks a
worktree (a blanket hard-fail would block every item in a worktree created
before the fix landed), and a stale resolution is only a hazard for a change
the gate could not see. Repositories without the workspace shared package are
never refused.

The refusal runs **after the build and before the test gate**: `report[
"steps"]["shared_resolution"]` records the check plus `refused`, no gate run
happens, nothing is committed, the message names the stale
`<main-checkout>/packages/shared/dist` path and the remediation (re-run
`implement.py start` from the main checkout to refresh the idempotent link
farm, then rebuild and finish again), and the work item is released to `open`.

Regression pins: `skill/implement/tests/test_stale_shared_resolution.py`
(detector unit tests plus the `phase_finish` negative path, and the two
no-op companions — unchanged shared source and no workspace package) and
`skill/implement/tests/test_node_modules_link_farm.py` (the helper shared with
`start`). `docs/dev/probe_worktree_shared_resolution.py` is the repeatable
end-to-end probe: it seeds a throwaway workspace, applies a probe change to a
worktree's `packages/shared/src/**`, and runs the real
`run_tests.py --type full --scope full` in three places — the worktree with
the link farm (passes), the worktree with the pre-farm whole-directory
symlink (fails), and the unmodified main checkout (fails).

## Single-owner per-repository lock (WL-0MVPADZ1Z000FOSG AC5, SA-0MVPZG3IQ009IJMO)

`implement.py finish` serialises its main-checkout critical section (commit →
fast-forward → push → worktree removal → post-push parent-branch sync) with a
**per-repository exclusive advisory lock**:
`<main-checkout>/.worklog/locks/main-checkout.lock`. The mechanics live in the
shared helper `skill/shared/repo_lock.py` (`RepoLock`, beside
`process_semaphore.py` and the per-work-item `work_item_lock.py`), so the same
lock also guards the adjacent window in `start` that moves the shared ref.
Concurrency contract:

- **One finish per main checkout.** Acquisition is a single non-blocking
  `flock(LOCK_EX | LOCK_NB)` by default — no measurable latency when free, and
  no wait when held: a second finish exits **non-zero** with a message naming
  the holder (work item id, PID, ISO start time, repo root, host — recorded in
  the lock file after acquisition) and **without modifying the checkout or the
  work item** (no status write, no comment). Re-run once the holder
  completes.
- **Bounded wait, never a hang.** `IMPLEMENT_FINISH_LOCK_TIMEOUT` (seconds,
  default `0` = fail fast) opts into waiting for a concurrent finish. The wait
  is always bounded by that deadline; on expiry the refusal states the deadline
  and still names the lock path, the holder and the recovery step. An
  indefinite wait is rejected outright (`RepoLock.acquire(timeout=None)` raises
  `ValueError`), so no code path can block forever.
- **No stale locks are possible.** The lock is the kernel's flock on the lock
  file's open file description, so a killed holder (SIGKILL included) releases
  it automatically — there is no PID bookkeeping to trust or clean up. Holder
  metadata is written for refusal messages **only**; it never decides
  ownership, so a lock file left behind by a crashed run cannot block anyone.
  The lock is also released on every in-process exit path (success,
  `RuntimeError` abort, escaping exception) via the `try/finally` around the
  critical section.
- **The adjacent `start` window is locked too.** `phase_start` refreshes the
  parent branch before forking a worktree, and that refresh fast-forwards the
  same shared local parent ref a finish moves. It takes the same lock
  (`_sync_parent_branch_locked`), **fail-open**: it waits at most
  `IMPLEMENT_START_LOCK_TIMEOUT` (default 30 s) and then skips the refresh with
  a warning naming the lock (`reason: "lock_held"`) — a start is never blocked
  or failed by a concurrent finish, and the local branch remains a valid base.
  The abort-path `_restore_repo_state` is deliberately unlocked: it runs in a
  process that may already hold the lock, and a dying process releases the
  lock itself.
- **Per-checkout scope.** Finishes in different repositories on the same host
  are unaffected (the lock file lives inside each checkout).
- **Observability.** `report["steps"]["main_checkout_lock"]` records the lock
  path, whether it was acquired, when, the configured bounded wait and the
  holder description when refused; `phase_start`'s
  `report["steps"]["sync_parent_branch"]` carries the same under `lock`.
- **Hygiene reporting.** `scripts/hygiene_check.sh` reports a held lock
  (`check_main_checkout_lock`) and any **tracked-file deletions** in the main
  checkout (`check_tracked_deletions`) in both human and `--json` output,
  reusing its existing git-status dirty-checkout detection.

The flock pattern mirrors `skill/shared/process_semaphore.py` (kernel-released
locks, holder metadata for messages only). Regression pins:
`skill/shared/tests/test_repo_lock.py` (bounded wait, timeout message,
metadata-never-trusted, context-manager release),
`skill/implement/tests/test_finish_main_checkout_lock.py` (contention, holder
metadata, SIGKILL crash release, cross-checkout independence, `phase_finish`
refusal/success, hygiene script JSON + human output) and
`tests/test_implement_finish_concurrent_orphan.py` (dev-ref window scenarios,
including the bounded-wait timeout message and the `start` refresh window).

## Verified reversible local-dev sync and post-push ancestry assertion (SA-0MVPZG7A0000N66U)

**The dev-push gate's subject is the pushed commit, and the push runs from the
worktree that holds it (SA-0MVTFMB7K002Y1F3).** `.githooks/pre-push` forwards
git's `<local_sha>` as `run_tests.py --gate-commit <sha>`, so the gate resolves
a clean checkout of the pushed commit and measures that tree
(SA-0MVTRBT50005DUEH). `phase_finish` Step 6 therefore calls
`git_push_to_dev(worktree_path, …)` — never the main checkout — so the gate can
always materialise the pushed commit. A pre-push subject guard
(`_assert_push_subject`) refuses the push with `PushSubjectMismatchError` when
the worktree cannot hold the pushed commit (missing, not a git working tree,
its `HEAD` is a different commit, or it has tracked uncommitted changes that a
recovery rebase cannot carry); the message names the commit, branch, worktree
and actual `HEAD`, so a checkout holding a different commit can never silently
gate the wrong tree. The non-fast-forward recovery re-establishes the same
subject: it fetches and rebases inside the worktree, recomputes the rewritten
commit **there**, guards again and retries `git_push_to_dev(worktree_path, …)`
— so the rebased replacement hash, not the pre-rebase commit, is what the gate
measures.

On success `git_push_to_dev` logs the hook's `stderr` (so "the gate ran" is
visible) and one structured line:

```
dev-push gate evidence: root=<cwd> head=<sha> pushed=<sha> full-suite=<classification>
```

The classification is derived from the **read-only** test cache for the pushed
tree, never from wall-clock: a valid full-suite entry already present before the
push ⇒ `cached`; none before but one after the push ⇒ `executed`; still none ⇒
`no-evidence` (warned — never silently reported as green). The same dict is
exposed as `report["steps"]["push"]["gate_evidence"]` (`root`, `head`,
`pushed`, `full_suite`) for `--json` consumers. The evidence path executes no
test command and never turns a gate-accepted push into a failure.

**Cache compatibility.** Cache keys stay path-independent. A worktree's cache
directory is private and is discarded with the tree. The gate's verdict is
**tree-keyed** (command + the pushed commit's tree SHA with
`fingerprint_kind="tree"` — SA-0MW7C6BMZ00931JG), so it is the same entry a
tree-keyed finish gate records for that tree and either consumer reuses the
other's verdict. `.githooks/pre-push`'s `mirror_suite_cache` copies any entries
the fresh worktree cache holds (tree-kind included) that the main checkout's
`.worklog/cache` lacks (content-addressed, so an identical key is an identical
run). Without it, pushing from the worktree would lose the pushed commit's
green `scope: full` tree-kind entry as far as the main checkout's post-push
verification and the audit skill (F3) are concerned.

The pre-push fast-forward of the main checkout's local parent branch is
**main-checkout hygiene only** (it keeps the next worktree's base up to date),
never the gate subject, and may be skipped safely while the push and the
ancestry assertion remain authoritative. That advance is inherently
unverified — the commit is by definition not on `origin/dev` yet — so
`phase_finish` decomposes it into three cooperating guarantees, all inside
the per-repository lock above:

| Step | Helper | Report key | Guarantee |
|---|---|---|---|
| Pre-push advance | `_fast_forward_local_dev_for_push` | `report["steps"]["local_dev_fast_forward"]` | `git merge --ff-only` only (never a rewrite); records `pre_advance_tip`; refuses loudly when the advance would be unsafe |
| Push-failure restore | `_restore_local_dev_after_failed_push` | `report["steps"]["local_dev_restore"]` | Local `dev` returns to `refs/remotes/origin/dev`; uncommitted work survives (no `reset --hard`) |
| Post-push verification | `_assert_commit_on_origin_dev` | `report["steps"]["ancestry_assertion"]` | Finish succeeds only when the pushed commit is an ancestor of `refs/remotes/origin/dev` |

### The advance is verified and reversible

- **Fast-forward only, never a rewrite.** Local `dev` is moved with `git merge
  --ff-only` (first onto `refs/remotes/origin/dev`, then onto the finish
  commit); no local commit is discarded and no history is rewritten.
- **Refuse rather than silently skip.** The previous broad `except` could
  disable the whole fast-forward without a trace. Every unsafe precondition
  now returns `method="refused"`, a loud warning naming the refs and hashes,
  and a machine-readable `reason`: `fetch_failed`,
  `no_remote_tracking_ref`, `checkout_failed`, `local_dev_diverged` (local
  `dev` carries commits the remote does not have, or cannot fast-forward to
  the remote tip) and `commit_not_based_on_remote_tip` (a sibling landed
  first). A refusal never fails the finish: the push (with its rebase-and-retry
  recovery) and the post-push ancestry assertion remain authoritative.
- **Retried after the recovery rebase (SA-0MW3H07A8004Q54C AC1).** When Step 5
  refused (`local_dev_diverged` / `commit_not_based_on_remote_tip`) because a
  sibling moved `dev`, the non-fast-forward recovery rebases the feature branch
  onto `origin/dev` and then **retries** the fast-forward for the rebased
  commit, recorded as `report["steps"]["local_dev_fast_forward"]["retry"]`.
  The main checkout's `dev` (and its `HEAD` when `dev` is checked out)
  therefore tracks the pushed commit before the retry push. A second refusal is
  recorded and never fails the finish, and the retry's own file churn is added
  to the main-checkout guard's `own_churn` so it is never misreported as a
  foreign mutation.
- **Reversal on a failed push.** Every push-failure path (`PushTimeoutError`,
  `PushNonFastForwardError`, a plain push failure) calls
  `_restore_local_dev_after_failed_push`, so the shared local `dev` is never
  left pointing at an unpushed finish commit — the state that made the
  2026-10-25 orphan possible. The restore is deliberately conservative:
  - the target is `refs/remotes/origin/dev`, a commit known to be published;
  - only finish's **own** advance is undone — when the branch moved since
    (`branch_moved_since_advance`) another actor owns that move and nothing is
    touched;
  - checked out → `git reset --merge` (uncommitted work in the shared checkout
    survives; an overlapping local modification is reported as
    `reset_conflict` and never clobbered — `reset --hard` is never used);
  - not checked out → atomic compare-and-swap `git update-ref`.
- **Own churn is still attributed — narrowly.** The fast-forward's file churn
  is recorded as `own_churn` so the main-checkout guard never aborts on it
  (WL-0MVPADZ1Z000FOSG AC3/AC4), but only paths *observed dirty in this
  checkout* qualify: unobserved diff paths are reported as `not_observed`
  diagnostics, a non-main-checkout root allow-lists nothing, and a deletion is
  never allow-listed (see
  [the attribution contract](#attribution-contract-own_churn)).

### The post-push ancestry assertion fails closed

`_assert_commit_on_origin_dev` refreshes `refs/remotes/origin/dev` (best
effort — the push already updates it), resolves the commit to its **full**
40-character hash (short hashes are display-only and unsafe to assert with)
and requires `git merge-base --is-ancestor <commit> refs/remotes/origin/dev`
to succeed. Anything else raises `AncestryAssertionError` (a `RuntimeError`
subclass, so `phase_finish`'s existing handler resets the item to `open`):

- the commit or the remote-tracking ref cannot be resolved → publication
  cannot be verified;
- the commit is not on the remote branch → it did not land.

This closes the false-positive shape of the 2026-10-25 incident: the
non-fast-forward recovery rebases and rewrites the commit hash, so a success
signal from the push alone can describe a commit the remote never received.
The assertion is evaluated against the hash that was actually pushed (the
rebased one when recovery ran), and the refusal comment names the resolved
commit, the target ref and its tip, the reason, and the retained branch and
worktree.

### State left behind by a refusal, and the recovery path

| Refusal | State left behind | Recovery |
|---|---|---|
| Lock held by a live run | Checkout and work item byte-identical (no status write, no comment) | Read the holder from the message / `report["steps"]["main_checkout_lock"]` / the lock file; re-run `finish` when it completes. A dead holder's lock file does not block anything — the kernel released the flock. |
| `AncestryAssertionError` | Worktree + feature branch retained, item `open`, refusal comment posted | Rebase the branch onto `origin/dev`, push it, re-run `finish` on the retained worktree (idempotent: it re-verifies commit → ancestry → status) |
| Non-fast-forward recovery failed | Same state; the recovery comment carries the commit, branch, worktree and rebase/push commands | Resolve the rebase conflicts (or push manually), then re-run `finish` |
| `local_dev_diverged` on the advance | Local `dev` untouched by this finish; the commit is pushed from the feature branch and verified afterwards | Inspect `git -C <main> log --oneline origin/dev..dev`; publish those commits (or merge them into `origin/dev`) before re-running, or deliberately `git reset --merge origin/dev` if stale — never `--hard` |
| Push failed / timed out | Worktree retained, item `open`, local `dev` restored to `origin/dev` | Re-run `finish`, or push manually: `git push origin <branch>:refs/heads/dev` |

The retained worktree is intentional: the commit, its tests and the manual
recovery commands all live there. Remove it only after the commit is on
`origin/dev` and `finish` has marked the item `in_review`.

**Runtime copy.** `~/.pi/agent/skills/` is installed by
`scripts/install_pi.sh`. The fixed behaviour reaches subsequent sessions
**only after** that script has refreshed the runtime skill copy, so re-run it
after pulling a change to `skill/implement/` (the `implement.py` it runs and
the skill text it reads).

Regression pins: `tests/test_implement_finish_local_dev_verified_sync.py`
(advance/refuse/restore/ancestry contracts), and
`tests/test_implement_finish_concurrent_orphan.py` (the end-to-end orphan
scenario).

### The main-checkout invariant these finish behaviours enforce

The normative statement — which main-checkout mutations are legitimate, which
are refused, and which code enforces each one — lives in
[worktree-isolation.md](worktree-isolation.md#the-main-checkout-is-a-guarded-surface),
together with the RCA and the recovery procedure. The three finish-side report
keys added for it are:

| Report key | Meaning |
|---|---|
| `report["steps"]["main_checkout_guard"]` | The main checkout (`repo_root`), snapshot time and baseline size, one verify entry per stage reached (`refactor`, `build_failure`, `gate_failure`, `user_abort`, `pre_push`, `pre_worktree_removal`) with its time, verdict, offending paths and actor evidence, the own-fast-forward churn allow-list and the final verdict |
| `report["steps"]["main_checkout_lock"]` | Lock path, whether it was acquired, when, and the holder description when the finish was refused |
| `report["steps"]["local_dev_fast_forward"]` | Whether the pre-push advance ran (`advanced`), why it refused (`reason`), the pre-advance and remote tips, and the `own_churn` allow-list |
| `report["steps"]["local_dev_restore"]` | Whether a failed push restored local `dev` to `origin/dev`, and why not when it was skipped |
| `report["steps"]["push"]` | The dev push outcome, plus `gate_evidence` (`root`, `head`, `pushed`, `full_suite` classification) when the push succeeded (SA-0MVTFMB7K002Y1F3) |
| `report["steps"]["ancestry_assertion"]` | The resolved commit, target ref and remote tip verified after the push |
| `report["steps"]["status_readback"]` | Confirms the terminal `completed`/`in_review` transition actually stuck in the store |

Sibling behaviour outside this repository: the `wl` CLI (ContextHub) prints a
single actionable diagnostic instead of an `ERR_MODULE_NOT_FOUND` stack trace
when the shared build output (`packages/shared/dist`) is missing — see the
[ContextHub dev note](https://github.com/SorraTheOrc/ContextHub/blob/dev/docs/dev/wl-shared-dist-diagnostic.md).

## Per-child implementation lock (SA-0MVQ7VMRL00498YB)

`implement.py start` serialises the critical section of one work item — the
work-item claim plus worktree creation/resume — with a **per-work-item
exclusive lock**. It exists because two concurrent `implement.py drive`
invocations could previously spawn two child sessions for the same child in the
same worktree: both wrote the same files, untracked work was replaced, and one
session's `finish` removed the worktree under the other
(SA-0MVOABYW00004978). The primitive lives in
`skill/shared/work_item_lock.py`; `phase_start` is the single choke point, so
`drive`, `parent` and a manual `start` all share it.

### Lock path and owner metadata

- **Path:** `<worklog>/locks/child-<work-item-id>.lock` — under the worklog
  **store**, never inside a worktree (a worktree can be removed mid-flight,
  while its lock must outlive it).
- **Store override:** `IMPLEMENT_CHILD_LOCK_DIR` replaces the resolved
  worklog store. It exists for **test isolation**: `phase_start` takes the
  real `flock` before its test-mocked steps and synthetic test ids share the
  repository's own prefix, so an un-isolated suite wrote — and raced — real
  lock files in the developer's `.worklog/locks`. The suites set it per test
  to a throwaway directory; it is unset in every production invocation (see
  [test isolation §9.8](test-isolation.md#98-per-work-item-lock-store-isolation-sa-0mvwj6u3j003tlrz)).
- **Mechanism:** a non-blocking exclusive advisory `fcntl.flock`
  (`LOCK_EX | LOCK_NB`) held for the holder's lifetime. There is no polling,
  no wall-clock TTL that can expire under a long legitimate session, and no
  waiting: a contender fails immediately.
- **Owner metadata** (JSON, written through the locked descriptor) records
  `agent`, `pid`, `session_id`, `started_at` (ISO-8601 UTC) and `token`.

### Acquire, refuse and fail closed

`phase_start` acquires the lock **before** claiming the item and before any
worktree creation/resume. A contender that does not acquire it reports
`action: "skipped-held"` and performs **no** store write: no claim, no
status/assignee mutation, no comment, no worktree create/remove, and no lock
metadata rewrite (the classification probe is read-only). The JSON report
carries:

| Field | Meaning |
|---|---|
| `action` | `"skipped-held"` |
| `success` | `false` |
| `held_by` | Holder metadata (`agent`, `pid`, `session_id`, `started_at`, `token`), or `null` when unreadable |
| `lock_path` | The lock file that is held |
| `message` | `Work item <id> is held by <agent>@pid <pid> (session <session>, since <started_at>). …` |

Ambiguity fails closed: an unreadable or corrupt lock file, or a failure to
record ownership, returns *held* (`acquired: false`) instead of overwriting a
possible live holder. The warning names the file so an operator can inspect it
and remove it only when no live holder exists.

`parent` and `drive` surface the same refusal — `action: "skipped-held"`,
`skipped_held: true`, `held_by`, `held_children`, and
`children[].held_by`/`children_processed[].held_by` — and spawn **no second
session** for the held child. `phase_parent` stops cleanly (no worktree
create/remove, no status mutation, no comment) and the parent/drive command
can be re-run once the holder finishes.

### Release semantics

The kernel owns the lock, not the bookkeeping:

- **Normal exit, early return, exception** — `phase_start` releases it in a
  `finally` around the whole mutating section.
- **`SIGINT` / `SIGTERM`** — the signal handler resets the item status and
  releases the lock deterministically before `os._exit`. If the signal lands
  after the claim but before the worktree exists, it also posts an explanatory
  comment on the work item (`Start phase interrupted by <SIGNAL> before the
  worktree was created…`) so a released item is never silently unexplained.
- **`SIGKILL` / crash** — guaranteed by the kernel when the descriptor is
  closed; no stale flock can survive a killed holder.

### Ownership token (`IMPLEMENT_CHILD_LOCK_TOKEN`)

Ownership is propagated by one explicit token, never inferred from pid
equality:

- The holder's token is exported as `IMPLEMENT_CHILD_LOCK_TOKEN` to every
  subprocess it spawns (`_invoke_implement`) and to the driven child session
  (`_child_session_env`).
- An acquire whose token matches the token in the lock file is a
  **re-entrant no-op** (`acquired: true`, `reentrant: true`): the driver's own
  `parent` → `start` chain and the driven child session (which runs
  `start`/`finish` for the same child id) are therefore never falsely blocked.
- A process with a different or absent token is a different owner, even if it
  happens to share the pid.

### Driven sessions: the driver holds the child's lock (SA-0MW7LMUUQ00701KJ)

During a drive, the **long-lived `phase_drive` driver process is the lock
holder** — not the short-lived `start` subprocess inside `parent`. `parent`
runs `start` as a short-lived subprocess, so the lock that subprocess takes is
released before the driven session begins. Previously, while the session was
alive the lock file recorded a dead `start` pid and no kernel `flock` existed,
so the herdr hydrator (ContextHub, WL-0MVWDB3IG004NRIU) saw a pane-less item
whose claim holder looked dead and demoted it to `open` mid-implementation
(SA-0MVXWMVTH007MNJR).

- **Who holds it.** The driver itself. The lock file's recorded `pid` is the
  driver pid, and the kernel `flock` is held by the driver process.
- **For how long.** For the session's whole lifetime: from immediately before
  the session is spawned until the spawner returns (success, non-zero exit or
  timeout). Only the spawn is wrapped — the lock exists to cover the session
  itself.
- **How the token reaches the session.** The driver acquires the lock with
  `_new_child_lock(child_id)`, registers it as the process's held lock via
  `_set_child_lock`, and `_child_session_env()` then propagates
  `IMPLEMENT_CHILD_LOCK_TOKEN` to the spawned session (headless `pi -p` and
  the pane transport alike). The session's own `start`/`finish` re-enter the
  lock as a token-based re-entrant no-op; ownership stays token-based and is
  never inferred from pid equality.
- **Release paths.** The release sits in a `finally` around the spawn, so
  every spawner outcome releases it: success, non-zero exit and timeout all
  leave no stale `flock`, and the next `parent`/`start` acquires the lock
  cleanly (or reclaims a genuinely dead holder with the existing warning). An
  acquire that raises `OSError`/`RuntimeError` fails closed — the driver
  records `failed_child` and spawns nothing.
- **A different live holder is still refused.** If the lock is already held
  by a different live owner, the driver reports `action: "skipped-held"` with
  `held_by`, `held_children` and `lock_path`, and spawns no session for that
  child. A holder chain re-entering by this process's own token is reported
  as acquired (`reentrant`), never as contention. A reclaimed stale holder is
  surfaced in the drive report (`lock_reclaimed`, `reclaimed_from`,
  `warnings`).

**Residual window: driver death while the session lives.** If the driver is
killed while a spawned session is still working, the kernel releases the
driver's `flock` and the hydrator may demote the item mid-session — the
original symptom, narrowed from "every driven session" to "the driver died
before its session finished". This is an **accepted residual window**: the
session itself keeps running (its `start`/`finish` still re-enter by token and
its work completes). **Option B** — the spawned session itself holding the
lock for its own lifetime — would close the window, but it needs a dedicated
lock-holder process that outlives the driver's spawn call, so it is
deliberately out of scope here.

### Stale-holder reclamation

A recorded owner whose pid is no longer alive does not block forever. When the
kernel lock is free but the file still records a dead pid, `acquire()` reclaims
it and logs:

```
reclaimed stale work item lock <path>: previous holder <agent> (pid <pid>) is no longer alive; kernel lock was free.
```

The `LockResult.reclaimed` field carries the previous holder's metadata and
`phase_start` logs the reclamation **and surfaces it in the report**, naming
the previous holder (SA-0MVUX6Q4S006KII3 — the parent epic's AC3 requires the
reclamation to be visible to a `--json` consumer, not only in the log). A
`SIGKILL`ed holder needs no manual cleanup: killing the process releases the
flock, and the next acquire reclaims the stale metadata.

Report fields recorded when a stale lock is reclaimed:

| Field | Meaning |
|---|---|
| `lock_reclaimed` | `true` when this acquire reclaimed a dead holder's lock (absent or `false` otherwise — never a phantom `true`) |
| `reclaimed_from` | The reclaimed holder's `agent`, `pid`, `session_id` and `started_at` (the ownership `token` is deliberately **not** copied into a report) |
| `lock_path` | The lock file that was taken over, `<worklog>/locks/child-<id>.lock` |
| `warnings` | Human-readable line(s) naming the reclaimed holder, e.g. `Reclaimed a stale per-child lock for <id> from <agent>@pid <pid> (session <session>, since <started_at>); the recorded holder pid is dead, so the lock was taken over (the previous session crashed).` |

`parent` and `drive` forward a child's reclamation into their own reports, so a
drive-level consumer sees it without reading the child's log:
`lock_reclaimed`, `reclaimed_from`, `lock_path`, the child id in
`reclaimed_children`, the same `warnings` entry, and a `message` that names the
reclaimed child. A clean acquire never emits any of these fields.

### Diagnosing a held child

When `start`, `parent` or `drive` reports `skipped-held`:

1. **Read the lock file.** `cat <worklog>/locks/child-<id>.lock` gives the
   holder's `agent`, `pid`, `session_id`, `started_at` and `token`. The
   report's `held_by` (plus `lock_path` in a `start` report) exposes the same
   values without opening the store.
2. **Confirm the holder is alive.** `ps -p <pid> -o pid,lstart,cmd` (or
   `kill -0 <pid>`). A live pid means an in-flight session owns the child: let
   it finish and re-run the parent/drive command; never start a second writer.
3. **When reclamation happens automatically.** Only when the recorded pid is
   provably dead (`ProcessLookupError`) *and* the kernel lock is free — the next
   acquire reclaims and warns, and surfaces the reclaimed holder in the report
   (`lock_reclaimed`, `reclaimed_from`, `warnings`). A pid owned by another user
   (`PermissionError`) or an ambiguous `OSError` is treated as alive (fail
   closed), so it is never stolen.
4. **Corrupt metadata** is never auto-reclaimed: the warning names the file.
   Inspect it, confirm no live holder exists, and only then remove it.

### Scheduling hold versus live process lock

Keep the two holds distinct — they answer different questions and neither
substitutes for the other:

| Signal | Where it lives | Meaning |
|---|---|---|
| `child-<id>.lock` + `IMPLEMENT_CHILD_LOCK_TOKEN` | Worklog `locks/` directory, held by a live process | **Live process lock**: a session is executing the child's critical section right now; no second writer is admitted. |
| `--assignee <agent>` on the work item | Worklog item metadata | **Human-visible scheduling hold**: a producer/agent has reserved the item; it is a coordination convention, not proof of a running process, and a stale assignee does not block `phase_start`. |

A child can be held by a live process while its item shows no assignee (the
claim is status-only), and an assigned item can have no live lock (the holder
exited). Reports surface the lock's `held_by`; they never infer a live holder
from `assignee`.

### Scope limits and tests

- One lock per **child work item**; serialising whole `drive` invocations (a
  driver-level mutex) is explicitly out of scope.
- The lock does not nest into the host-wide `process_semaphore` slot pools.
- Regression pins: `skill/shared/tests/test_work_item_lock.py`
  (acquire/release, token re-entrancy, SIGTERM release, dead-pid reclamation
  with warning), `skill/implement/tests/test_phase_start_child_lock.py`
  (`phase_start` ordering/refusal/signal/token propagation, `skipped-held`
  reports) and `skill/implement/tests/test_drive_child_lock.py` (two concurrent
  subprocesses: exactly one acquires, the loser writes nothing and the winner's
  files are not diverged). Lock-store isolation is pinned by
  `skill/implement/tests/test_phase_start_claim.py::test_child_lock_lives_in_the_isolated_store_not_the_real_one`
  and `tests/test_implement_child_lock_isolation.py`. The driven-session
  holder documented above is pinned by
  `tests/test_implement_driven_lock_holder_doc.py`.

## Herdr session boundary helper and fake CLI (SA-0MVPHXNTJ006TNFB)

`skill/shared/herdr_session.py` is the single, injectable boundary the
pane-driven `drive` transport builds on. `herdr_pane.py`
(SA-0MTFLQEQQ0083EW1) owns *pane titles*; this helper owns the **session**
surface an epic child needs — split a sibling pane, start an interactive agent
in it, prompt it, wait for its lifecycle state, then close or rename the pane.
It reuses `herdr_pane`'s `HERDR_BIN_PATH` resolution and subprocess-env
handling (`resolve_herdr_bin`, `subprocess_env`) rather than duplicating them,
and follows the same fail-open style.

### Surface

One client, `HerdrSessionClient`, whose every method returns a parsed result
dict or a fail-open `{"success": False, "reason": ...}` — **it never raises**:

| Method | herdr call |
|---|---|
| `split_pane(cwd, direction="right", env=None, focus=False, current=True, pane_id=None)` | `pane split --current --direction <dir> --cwd <path> [--env K=V]… --no-focus` |
| `start_agent(name, pane_id, kind="pi", args=[...], timeout_ms=None, ready_budget_seconds=0.0)` | `agent start <name> --kind pi --pane <id> -- <args>` |
| `prompt_agent(target, text, wait=True, until=None, timeout_ms=None)` | `agent prompt <target> <text> --wait [--until <state>]… [--timeout <ms>]` |
| `wait_agent(target, until=None, timeout_ms=None)` | `agent wait <target> [--until <state>]… [--timeout <ms>]` |
| `close_pane(pane_id)` | `pane close <pane_id>` |
| `rename_pane(pane_id, label)` | `pane rename <pane_id> <label>` |

Success results carry `pane_id`, `agent`, `agent_name` and `agent_status`
parsed defensively from `.result.pane.pane_id` / `.result.agent`. An unexpected
payload is reported as a failure (`unexpected herdr response (expected …)`)
instead of being assumed — Herdr API drift is a recorded risk, so an unknown
shape must stop the chain cleanly, never crash it. Failures carry the herdr
error `code` (e.g. `agent_not_ready`, `agent_blocked`, `timeout`,
`pane_not_found`), the exact `command` and the `returncode`.

`pane split` never creates a tab or workspace: it splits the calling pane
(`--current`) so each child appears as a sibling pane in the caller's tab.

### Agent naming

herdr names must match `[a-z][a-z0-9_-]{0,31}` and be unique among live agents.
`agent_name_for_child(child_id, existing=…)` derives
`drive-<child-id-lowercased>`, collapses unsafe characters to `-`, truncates
deterministically to 32 characters, and appends `-2`, `-3`, … when the result
collides with a name already in use (staying inside the same length budget).
`start_agent` refuses an invalid name with a reported reason before invoking
herdr, so a malformed name can never reach the CLI.

### `agent_not_ready`

`herdr agent start` requires the pane to be at its interactive shell prompt.
When it is not, herdr returns `agent_not_ready`; `start_agent` retries within
`ready_budget_seconds` (polling every `poll_interval`, both injectable clocks)
and otherwise surfaces the code as a reported reason with `attempts`. A
non-readiness error is never retried.

### Fake CLI (test-only)

`skill/shared/tests/fake_herdr_cli.py` provides both flavours of the double:

- **`FakeHerdrRunner`** — an in-process `subprocess.run` replacement with
  ordered scripted responses, recording every argv.
- **the executable shim itself** — run the module as a script for a real
  subprocess boundary; it records each invocation as one JSON line
  (`{"argv": [...]}`) to `FAKE_HERDR_RECORD` and prints the next response from
  `FAKE_HERDR_SCRIPT` (`--script`/`--record` flags override the env vars).

Responses are consumed in order, so a script pins the call order while the
record file proves the exact arguments the caller built. Response builders
(`pane_info_response`, `agent_started_response`, `agent_prompted_response`,
`agent_info_response`, `ok_response`, `error_response`) emit the real CLI's
envelopes (verified against `herdr` 0.9.5), including `done`, `blocked`,
`timeout`, `agent_not_ready` and CLI-failure transitions — no live Herdr is
needed by any test.

### Tests

`skill/shared/tests/test_herdr_session.py` covers naming (valid, truncated,
collision), the exact argv of every call, the absence of tab/workspace
creation, the `done`/`blocked`/`timeout`/`unknown` transitions, the
`agent_not_ready` retry and exhaustion paths, every fail-open mode, and an
end-to-end shim run asserting a full split → start → prompt → wait → close
sequence.

## Pane-driven drive transport and opt-in (SA-0MVPHYIIE002OBC8)

`implement.py drive <parent>` can run each child's implementation session as an
**interactive Pi TUI agent in a sibling Herdr pane** of the calling tab instead
of a headless `pi -p` subprocess. The transport is chosen per invocation and
the headless path remains the default everywhere else.

### Selection (fail-open)

Pane driving is selected only when **both** halves agree:

| Condition | Source |
|---|---|
| Herdr is present | `HERDR_ENV=1` **and** a resolvable herdr binary (`HERDR_BIN_PATH`, else `herdr` on `PATH` — the same resolver `herdr_pane` uses for pane titles) |
| The operator opted in | `--pane-sessions`, or `IMPLEMENT_DRIVE_PANE_SESSIONS=1` |

`--no-pane-sessions` is an explicit opt-out and wins over an ambient
`IMPLEMENT_DRIVE_PANE_SESSIONS=1`. Any other combination — not inside Herdr, no
binary, no opt-in — returns the existing `_default_session_spawner` unchanged,
so the downtime worker, audit dispatch and CI keep the headless path even if a
stray flag is passed. `spawn_session` remains the test injection point;
`_select_session_spawner(pane_sessions, focus, env, default, client)` is the
production selector.

### `_pane_session_spawner`

`_pane_session_spawner(child_id, worktree_path, timeout, env, verbose=False,
*, focus=False, client=None)` mirrors the headless spawner's contract and adds
`pane_id` and `transport`:

1. `pane split --current --direction right --cwd <worktree> --env K=V … --no-focus`
   — a sibling pane in the caller's tab (never `tab create` / `workspace
   create`).
2. `agent start drive-<child-id> --kind pi --pane <id> -- --approve
   --no-extensions` — the same extensions-disabled child session shape as the
   headless path; `agent_not_ready` is retried within a 30 s readiness budget.
3. `agent prompt <name> "/skill:implement <child>" --wait --until done --until
   blocked --timeout <child budget>` — the interactive turn that runs the leaf
   workflow. Completion is still decided by the next `phase_parent` stage
   re-read; the prompt wait only occupies the session like the headless
   process would.

The result is `{"success", "returncode", "reason", "pane_id", "transport"}` — a
superset of `_default_session_spawner`'s shape, so `phase_drive`'s existing
success/failure handling, `completed-late` reconciliation and stranded-child
detection are reused unchanged. The one addition is the pane lifecycle: the
driver closes or retains the pane it created (see below). Every herdr call is
fail-open: a split, start or prompt failure is a reported `reason` (with the
herdr error code), never an exception, and the chain stops cleanly. A prompt
timeout is worded as `… timed out after Ns: …` so `_reconcile_spawn_failure`
still classifies it as a bound rather than a verdict.

### Pane lifecycle and report parity (SA-0MVPHZ1CI0045QR0)

The **agent's settled session state is a fast-path signal, never the verdict**.
Completion of a child is decided by the next `phase_parent` stage re-read
(`children_processed`), exactly as the deferred worktree removal is. The pane
is disposed of by that signal:

| Session outcome | Pane | Reported |
|---|---|---|
| `done` and the parent confirms a terminal stage | **closed** (`pane close <id>`) | entry `pane_closed: true`, `pane_cleanup` |
| `done` but the parent still sees the child non-terminal | **retained** | `retained_panes` entry, ⚠️ title |
| `blocked` (agent needs a human decision) | **left open** (deliberately, for inspection), chain stops | `blocked_child`, `blocked_pane`, `retained_panes`, 🚫 title |
| timeout | **closed** (after reconciliation), chain stops | entry `timed_out: true`, `pane_closed: true` |
| timeout whose child is already terminal | **closed** | `completed-late`, `pane_closed: true` |
| other failure, child not terminal | **retained** as evidence | `retained_panes`, ⚠️ title |
| drive ended before the parent reported the child | **retained** (reported, no title) | `retained_panes` |

`blocked` is a distinct verdict (`children_driven[].result == "blocked"`,
rendered 🚫): the drive does **not** re-spawn the child (it would immediately
block again) and does **not** close its pane. `blocked_child` / `blocked_pane`
name it, an actionable `herdr pane read <pane_id>` remedy is reported, and a
comment is posted on the child. A retained pane is titled through
`herdr_pane.update_pane_title(verdict, pane_id=…)` — `attention` (🚫) for a
blocked child, `note` (⚠️) for a failed one — so the pane grid shows the
verdict without opening the child's log. Titles are fail-open and never break
the chain, and no title is applied when the drive has no verdict yet.

**Report parity.** Both outputs keep their existing shape and gain additive
keys:

- every `children_driven` entry carries `transport` (`pane`/`headless`) and
  `pane_id` (`None` on the headless path);
- `pane_closed` / `pane_cleanup` record a close outcome;
- `retained_panes` lists `{id, pane_id, verdict, reason, remedy}` for every
  pane kept open;
- `blocked_child` / `blocked_pane` record the blocked stop.

The human rendering prints a `transport: pane, pane <id>` line per driven
child and a **"Panes retained for inspection"** list with the inspection
remedy, so an operator running `drive` in a terminal sees the same facts a
`--json` consumer does.

### Environment isolation

The pane child receives the environment built by `_child_session_env`:
`IMPLEMENT_DRIVE_ACTIVE=1`, `IMPLEMENT_WORKTREE_PATH` and — when present — the
per-child lock token. Only those markers are forwarded through `pane split
--env` (an allow-list: the whole inherited environment never enters the
recorded argv, so secrets cannot leak there). `PI_SESSION_ID` and
`PI_SESSION_FILE` are removed by `_child_session_env` and never re-injected.
Because `herdr agent start` has no unset-environment option, the same scrubbed
environment is handed to the herdr CLI process itself — the `env -u
PI_SESSION_ID -u PI_SESSION_FILE` equivalent — so the launched TUI cannot
inherit the driving session's identity from this process either.

### Focus

`--focus` / `--no-focus` control whether each new child pane takes focus. The
default is **`--no-focus`** so driving an epic never moves the operator's focus
away from the session they invoked it from; the exact flag passed to `pane
split` is asserted in the tests.

### Worked example: driving an epic in a Herdr tab

An operator in a Pi session running inside a Herdr pane drives a three-child
epic:

```bash
python3 $(skill_path implement)/scripts/implement.py drive SA-0MVLKHY27005UK40 --pane-sessions
```

**What the operator sees.** A sibling pane appears to the **right** of the
calling pane for each driven child — `pane split --current`, never a new tab or
workspace — running an interactive Pi TUI agent named `drive-<child-id>` with
`/skill:implement <child-id>` as its prompt. Focus stays in the invoking pane
(the default is `--no-focus`). The invoking pane prints, as the chain advances:

```text
  ✅ SA-…C1 (attempt 1): finished
     transport: pane, pane w1:p7
...
  Panes retained for inspection: 1
     SA-…C2: pane w1:p9 (attention) — the driven agent reported `blocked`; a human decision is needed
       inspect with: herdr pane read w1:p9
```

Each child's pane is closed once the next `phase_parent` re-read confirms that
child reached a terminal stage, so a green chain leaves no panes behind.

**A blocked child.** When a child agent settles `blocked` — it needs a person to
answer something — the drive stops rather than re-spawning a session that would
block again. `--json` reports `blocked_child` and `blocked_pane`; the human
output lists the pane under **Panes retained for inspection** with its remedy.
The pane is **deliberately left open** and titled 🚫, so the operator can see
which pane to look at without guessing. Read it with the printed remedy:

```bash
herdr pane read w1:p9
```

and a comment on the child work item records the same instruction.

**Resuming after intervening.** Answer the question in the blocked pane, or
resolve the blocker outside it (a `wl` update, a missing decision on the child
the agent asked about), then re-run the **same** command:

```bash
python3 $(skill_path implement)/scripts/implement.py drive SA-0MVLKHY27005UK40 --pane-sessions
```

`phase_parent` skips children that are already terminal and takes up the first
child that still has work. A pane session that is still alive holds that
child's per-child lock, so the re-run reports the child as `skipped-held`,
names the holder (`agent`, `pid`, `session_id`, `started_at`) and spawns
nothing — finish or close that pane session first instead of racing it. Panes
retained by an earlier `drive` invocation are not tracked by the new one, so
close any you no longer need (they are listed in the earlier report).

**Without Herdr, or from an automated caller.** The same command run outside
Herdr (no `HERDR_ENV=1`, no resolvable binary), without `--pane-sessions`, or
with `--no-pane-sessions` uses the headless `pi -p` spawner unchanged and
reports `transport: headless` with `pane_id: null`. The downtime worker, audit
dispatch and CI therefore keep the headless path even if a stray flag is
passed.

### Tests

`skill/implement/tests/test_drive_child_session.py` covers the spawner's exact
herdr argv (split → start → prompt, no tab/workspace creation), the
`--focus`/`--no-focus` flag, the env allow-list and the `env -u` equivalent,
fail-open split/start/prompt failures (including a prompt timeout), the
detection × opt-in selection matrix, and end-to-end `phase_drive` runs through
the pane transport with a fake herdr CLI — including the pane lifecycle
(closed on a terminal child, left open and reported on `blocked`).
`tests/test_implement_drive.py` (`TestDrivePaneLifecycle`) pins the
done/blocked/timeout/completed-late and failed-close outcomes, the close
happening only after the terminal stage is confirmed, and the human report's
retained-pane rendering. No live Herdr is needed.

---

## Appendix — relocated SKILL.md prose (SA-0MVYDCUWA001Z8K0)

`skill/implement/SKILL.md` was restored to its operational brief size class
(≤20,000 B) by relocating the deep reference prose below **verbatim** into
this appendix (AC2 of SA-0MVYDCUWA001Z8K0). Workflow semantics are
unchanged; the brief keeps the operational step sequence and links here for
rationale, tables, guard matrices, failure-mode taxonomies and SA-tagged
feature cards. Relative links are rewritten for this document's location.

### Relocated: StatusLifecycle Integration

## StatusLifecycle Integration

Status transitions are managed by the shared `StatusLifecycle` context manager
from `../shared/status_lifecycle.py`. The orchestration script (`implement.py`)
uses it automatically: `phase_start()` claims (`in_progress`); `phase_finish()`
wraps build/test/commit/push in `with StatusLifecycle(..., target_stage="in_review")`
(success → `completed`/`in_review`; error → original status restored);
`phase_abort()` resets to `open` (also `implement.py abort <WIP-id>`). Manual
use: `StatusLifecycle.update_status()` or the context-manager pattern in
`../shared/status_lifecycle.py`.

**Assignee release on every terminal outcome.** Each implement run releases
the assignee when it ends, regardless of how it ends, per the
[assignee-release invariant](../../AGENTS_GLOBAL.md#assignee-release-invariant)
(canonical wording; cross-referenced here rather than restated):

- successful `finish` clears the assignee together with the
  `completed`/`in_review` transition: the context manager is built with the
  agent's `assignee=`, and `StatusLifecycle` releases the claim it holds on
  every exit path by default (`preserve_assignee_on_exit=True` is the only
  documented opt-out — its legitimate uses and the inline-comment
  requirement are documented with the invariant)
- every failure/abort reset to `open` (`phase_start`, `phase_finish`,
  `phase_abort`, the signal/KeyboardInterrupt safety reset) also passes
  `assignee=""`
- epic/parent completion and the stranded-child `drive --finalise` path
  clear the assignee on the `completed`/`in_review` advance

A finished or released item is therefore never left on a scheduling hold.

**Invariant (SA-0MTFTFUIH000UWM9): actively worked => `in_progress`.** No
`wl` mutation (description/comment/child creation) while `status: open`. Guard
with `StatusLifecycle.require_claimed(<id>)` before any mutation; on any
in-session resume, re-claim first via `StatusLifecycle.ensure_claimed(<id>)`
(idempotent).

**Terminal status on both finish paths (OSL-0MVME1PI5005WU58).** `finish`
persists `completed`/`in_review` on the push-with-diff path **and** on the
no-op-push path (a branch with no diff from `origin/dev` — e.g. a
verification-only item): both paths reach the same
`with StatusLifecycle(..., target_stage="in_review")` exit, so the terminal
write is issued either way. After that write, `finish` reads the item back
from the main-checkout store (`_assert_terminal_status`) and **fails loudly**
— item released to `open`, non-zero result — when the store does not hold
`completed`/`in_review`, so a silently lost lifecycle write can never be
reported as success. The read-back runs only after a confirmed successful
push; a failed push, build, or gate still leaves the item `open`. Regression
pin: `tests/test_finish_no_diff_status.py`. Deliberate gap: the read-back
*reports* a lost write, it does not repair it — re-run `finish` after
investigating the store.

### Relocated: Project-local extensions

## Project-local extensions

The implement skill consumes the project-local extension convention
(`.pi/skills_extensions/implement/`, contract SA-0MSQ7MQEJ0064ZB0):

- **`SKILL_PREFIX.md`** is surfaced by `implement.py start` after the gating
  steps and before the first actionable step; `SKILL_POSTFIX.md` is surfaced
  by `implement.py finish` after the final step. Both are recorded in the JSON
  report (`extension.prefix_prose` / `extension.postfix_prose`).
- **`extension.json`** may declare `ignoreDirtyPaths` — a list (or single
  string) of repository-relative path prefixes that are *expected* to be dirty
  and therefore do **not** hard-fail the dirty-tree gate or the worktree
  placement gate, e.g. `{"ignoreDirtyPaths": [".llm-wiki/"]}`.

The declaration is project-scoped, explicit and additive: it can only relax
`git_has_dirty_files()` for the named prefixes, never for the rest of the
checkout, and never for the `.worklog/` or build → test → commit gates.
Absence is a no-op (byte-for-byte unchanged behaviour). A malformed extension
is a loud failure: `implement.py start` refuses with an actionable message and
releases the work item (status → `open`). Uncommitted expected-dirty paths are
never stashed, committed or reverted. See
[docs/dev/skill-extensions.md](../../docs/dev/skill-extensions.md).

### Relocated: Background Job Etiquette

## Background Job Etiquette

The canonical guidance for running and waiting on long-running background
processes lives in the shared doc
[`../shared/background-job-etiquette.md`](../../skill/shared/background-job-etiquette.md)
(SA-0MVCX570P003VLBK), which this section mirrors and must stay consistent
with (SA-0MVDP5JTK009HWOT AC4).

When a step needs to run a long-running background process (audit, build,
release, batch job), follow this pattern:

**launch → prepare other work → one completion-driven wait with progress → collect result**

1. **Launch** the job with a redirect to a tailable log, e.g.
   `nohup python3 ... > /tmp/job.log 2>&1 &`; record the PID and log path.
2. **Prepare other work** while it runs — read context, draft docs, update
   the work item. Do **not** idle-sleep.
3. **Wait once, completion-aware**, with the shared helper
   `../shared/wait_for_job.py` (SA-0MVCX570P003VLBK):

   ```bash
   python3 ../shared/wait_for_job.py --pid $PID \
     --log /tmp/job.log --pattern "Audit complete" \
     --timeout 3600 --interval 60 --message "audit SA-XXXX"
   ```

   It polls at the configured interval (default ≤ 60 s), emits a timestamped
   heartbeat at least once per poll interval (tailable in a herdr pane), and
   returns **as soon as** the job's completion marker appears (process exit /
   checkpoint ``--marker`` / ``--log``+``--pattern``), never after a fixed
   sleep. With ``HERDR_SOCKET_PATH``/``HERDR_PANE_ID`` set it reports
   ``working``/``blocked`` to the herdr sidebar (silently optional). A
   ``herdr_status`` startup line shows whether sidebar reporting is enabled
   (and for which pane), so an operator can see at a glance why a sidebar is
   (not) updating. On completion the final line is followed by a terminal
   bell; set ``WAIT_NOTIFY=1`` (with ``notify-send`` on PATH) for a desktop
   notification (fail-soft, disabled by default).
4. **Collect** the result from the log / exit code and continue.

**Feedback channels** (which interface shows what):

- **Live job output (on by default):** the helper tails the job's output and
  prints only the lines written since the previous poll (last 10,
  `--tail-lines N`). The source is an explicit `--log FILE`, otherwise for a
  `--pid` wait the file that process' stdout/stderr is redirected to
  (`/proc/<pid>/fd/1`). `--no-tail` disables it.
- **Process vitals:** every poll emits `progress: elapsed … state=… cpu=…
  rss=… threads=… children=…`, so a wait whose output is unreadable (pipe or
  socket) still shows liveness.
- **Interactive pi TUI:** the bash tool streams this partial output live into
  the tool box (live elapsed timer; Tab expands beyond the last 5 lines).
- **Herdr sidebar:** with ``HERDR_SOCKET_PATH`` + ``HERDR_PANE_ID`` exported
  into the wait process, the helper sends ``pane.report_agent``
  (``working``/``blocked`` + message) every poll interval; without them it
  silently degrades — check the ``herdr_status`` startup line to confirm.
- **Completion:** terminal bell in text mode; ``notify-send`` desktop
  notification when ``WAIT_NOTIFY=1`` and the binary is present.

> **Make the job's output visible.** Redirect job output to a file and run it
> unbuffered, otherwise the tail stays silent until the process flushes:
>
> ```bash
> nohup python3 -u my_job.py > /tmp/job.log 2>&1 &
> python3 ../shared/wait_for_job.py --pid $! --timeout 3600 --interval 60 \
>   --message "audit SA-XXXX"
> ```
>
> Any other program works too (`stdbuf -oL <cmd>`); when a tail stays silent the
> helper says so explicitly and suggests the fix.

> **Deprecated:** fixed-length ``sleep N`` polling of background processes
> (`sleep 900; grep <log>; ps aux | grep` loops) is deprecated. A 23.7 h
> session spent 88.6% of its time sleeping (95 ``sleep`` calls totalling
> 21.0 h); each wake-up costs a full model round-trip and nearly always
> returns "no change". Use the completion-driven wait above instead.

### Relocated: Test Timeout Configuration

## Test Timeout Configuration

The finish step's changed-scope validation runs through `implement.py finish` with a
per-command timeout (default **600 seconds**). Repos with slow test suites can
override this via `.pi/test-config.json`:

```json
{"timeoutPerCommand": 1500}
```

The `timeoutPerCommand` field is read from `.pi/test-config.json` in the project
root. When the file is absent, the field is missing, or the value is invalid,
the default of 600 seconds is used. This keeps other repos unaffected — only
projects that explicitly create the file get the override.

Set the value high enough to cover the full suite with headroom (e.g. TCE uses
1500 to cover its ~19-minute suite).

### Relocated: Push Timeout (SA-0MUH9R74O002MQDU)

### Push Timeout (SA-0MUH9R74O002MQDU)

The push step (git push to `dev`) also uses `_resolve_test_timeout()` to
resolve its timeout from `timeoutPerCommand` in `.pi/test-config.json`, defaulting
to 600 s when the config is absent.  The old hard-coded 120 s timeout is removed.

When the push times out (typically because the pre-push hook is still running the
full test suite):

- The entire process group is killed via `os.killpg()` so orphaned
  `run_tests.py` children cannot survive and deadlock subsequent pushes.
- A `PushTimeoutError` is raised carrying the commit hash, branch name, and
  timeout value.
- `phase_finish` catches this and posts a manual-push comment on the work item
  (commit hash + branch + `git push origin` instruction).
- The work item status is reset to `open` (not silently abandoned).

Recovery: push manually from the worktree that holds the pushed commit
(never the main checkout, or the gate could measure a different tree):

```bash
git push origin <branch>:refs/heads/dev
```

### Relocated: Dev-push gate reason and transport retry (SA-0MVSJX5HS002DJHI)

### Dev-push gate reason and transport retry (SA-0MVSJX5HS002DJHI)

The dev push used to report `git push to dev failed` for every non-zero exit,
so an agent could not tell *fix the code* from *retry later*.

- **The hook names its reason.** `.githooks/pre-push` consumes the runner's
  `--json` aggregate and prints a stable `gate_reason=<code>` line plus a
  distinct diagnostic: `slot_contention`, `timeout`, `killed`, `unparsed`,
  `test_failure`, `infrastructure` or `unknown`. `finish` parses the line and
  records `report["steps"]["push"]["reason"]` and posts the reason with its
  remedy on the work item, instead of the generic failure. The red-suite
  `test_failure` message is unchanged in substance.
- **The pushed-commit guard is distinguishable.** §3a of `.githooks/pre-push`
  refuses a dev/main push *before* the suite runs when the gate tree is not the
  pushed commit (or is dirty at it), so it prints no `gate_reason=` line.
  `finish` recognises the guard's stable diagnostic and reports the distinct
  reason `refused_by_pushed_commit_guard`, with the pushed sha, gate HEAD and
  gate tree in `report["steps"]["push"]["refused_by_pushed_commit_guard"]`;
  the remedy names the worktree push. The normal path already pushes from the
  worktree that holds the commit, so this is a fail-closed safety net.
- **The fast-forward is retried after the recovery rebase.** When Step 5's
  local-dev fast-forward was refused (`local_dev_diverged` /
  `commit_not_based_on_remote_tip`) because a sibling moved `dev`, the
  non-fast-forward recovery rebases onto `origin/dev` and then retries the
  fast-forward for the rebased commit
  (`report["steps"]["local_dev_fast_forward"]["retry"]`), so the main
  checkout's `dev` tracks the pushed commit. A second refusal is recorded and
  never fails the finish.
- **Transient transport closes and auth/connect failures retry.**
  `Connection … closed by remote host`, `Connection reset by peer`, `remote
  end hung up unexpectedly` and `kex_exchange_identification` are retried up
  to 3 attempts with jittered exponential backoff from the **same cwd**, so
  the gate re-runs and serves its cached full-suite entry. AC1
  (SA-0MW22SEMV00230O5) extends the retryable set to the concurrent-run
  auth/transport signatures (`Permission denied (publickey)`, `Could not
  read from remote repository`, connect timeouts/resets, a remote `Internal
  Server Error`), reported on exhaustion as `reason: transport`
  (infrastructure). Branch-policy and non-fast-forward errors are never
  retried as transport (non-fast-forward keeps its dedicated rebase
  recovery), and a timeout still raises `PushTimeoutError` immediately.
- **SSH keepalives keep the idle gate connection alive** (SA-0MVWNHLQQ002TFZR).
  The pre-push full suite runs *after* git opens the SSH connection and
  produces no SSH traffic, so a remote that closes idle connections (GitHub)
  drops it before the ref update and silently discards a green gate.
  `git_push_to_dev()` therefore sets `GIT_SSH_COMMAND` in the push child env to
  an ssh invocation carrying `-o ServerAliveInterval=30 -o
  ServerAliveCountMax=20 -o TCPKeepAlive=yes`, preserving any operator
  `GIT_SSH_COMMAND` (an existing `ssh …` command is merged; one that already
  sets `ServerAliveInterval` is left untouched). The fix travels with a fresh
  clone/worktree — no per-operator `~/.ssh/config` edit.
- **A lost ref update is a distinct, loud failure.** If the retries are
  exhausted on a transport close, the push raises `PushTransportLostError` and
  `finish` records `reason: connection_closed` (infrastructure) with a
  "transport lost — not a test failure" comment, never "full suite green"
  followed by silence. This is distinct from a red suite
  (`gate_reason=test_failure`).
- **Fairness, not capacity.** `process_semaphore`'s arrival-ordered turnstile
  grants a free slot to the lowest outstanding ticket, so a waiter is bounded
  by its queue position instead of being overtaken indefinitely;
  `TEST_MAX_CONCURRENCY` is unchanged. See
  [docs/dev/test-skill-reference.md](../../docs/dev/test-skill-reference.md).

### Relocated: Reading a gate failure — `reason` and its facts (SA-0MVTET8KQ0031R6Z)

### Reading a gate failure: `reason` and its facts (SA-0MVTET8KQ0031R6Z)

A red finish gate reports a machine-readable `reason` plus the facts that
decided it, on `test_phase` in the `--json` report and in the operator message.
Read `reason` **first** — it answers the only question that matters before you
act, and every reason never lands on `unknown` with an empty `failures` list.

| `reason` | What happened | What to do |
|----------|---------------|------------|
| `test_failure` | Real, parsable failing tests. | Fix the code. Structured failures always win: a genuine failure is never reported as infrastructure. |
| `timeout` | The suite exceeded `timeoutPerCommand` and was stopped. | Raise `timeoutPerCommand` in `.pi/test-config.json` with headroom over a green run's `elapsed_seconds`, then re-run. |
| `killed` | The child was signal-terminated (OOM kill, worker/supervisor crash) with no test result. | Treat as host/infrastructure: read the output tail, re-run. Do **not** "fix" a test. |
| `unparsed` | The suite exited non-zero and no test failure could be parsed (bad flag, unloadable config, missing wrapper). A failing **Vitest** test — browser mode included — parses into structured failures, so it is `test_failure`, never this. | Read `command` and the output tail: the command or its configuration is wrong, not a test. |
| `slot_contention` | No host test slot within the bounded wait (`slot_busy`). | Wait a staggered delay, or raise `TEST_LOCK_TIMEOUT`. The wait is reported separately from execution. |
| `infrastructure` | Explained by a runner notice but not attributable to a test (includes a live-repo mutation). | Read the notices; the checkout may have been mutated by a concurrent session. |
| `unknown` | Last resort: no structured failure, no notice and no executed command. | Re-run the suite with the test skill to obtain diagnostics. |

A non-zero Vitest run whose failed-test output was trimmed by the gate's
1200-character tail down to a bare `❯ <file>:<line>:<col>` frame (the `FAIL`
header and `AssertionError:` line lost, e.g. to a long README diff in the
assertion message) is still `test_failure`, never `unparsed`: the frame is the
failure's surviving signal and is reported as the failing location
(`gate_reason.vitest_frame_location`, SA-0MWDLPC3F005S9S2). The frame fallback
replaces only the `unparsed` outcome — timeout, killed, slot-contention and
infrastructure verdicts are decided first and are never reclassified by a
surviving frame.

The deciding **fact fields** accompany every classified failure:

| Field | Unit / meaning |
|-------|----------------|
| `configured_timeout` | Seconds — the `timeoutPerCommand` that was applied. |
| `elapsed_seconds` | Seconds — wall time of the command execution **only**. `null` (never `0`) when nothing executed (fully cached, or the slot wait expired before the spawn). |
| `slot_wait_seconds` | Seconds — host test-slot queue wait, measured once by the pacer and **never** folded into `elapsed_seconds`. On a slot-busy result it is the elapsed bounded wait. |
| `command` | The suite command that was run. |
| `exit_code` | The process exit code, reported only when a suite actually reported one (negative means signal-terminated). |
| `signal` | The terminating signal number when the child was killed. |

The same vocabulary is documented for the runner's notice codes in
[docs/dev/test-skill-reference.md](../../docs/dev/test-skill-reference.md#notice-codes-stable-strings)
and for the gate's classification rules in
[docs/dev/implement-skill-reference.md](../../docs/dev/implement-skill-reference.md#gate-failure-diagnostics-wl-0mvgut787000lndg);
a docs-vs-source consistency test (`$(skill_path implement)/tests/test_gate_classification_docs.py`)
fails if a documented reason, notice code or fact field no longer exists in the
code — or if a new one is left undocumented.

### Relocated: Status Safety & Abort Handling

## Status Safety & Abort Handling

### Critical Rule: Always Reset Status on Abort

When an implementation is aborted, interrupted, or fails before the final
commit/push step, the item can remain stuck at `in_progress`, blocking other
agents. **Every abort/failure path MUST reset status to `open`** to release
the lock.

### Mandatory Abort Pattern

1. **Reset to open:** `StatusLifecycle.update_status(<work-item-id>, "open")`
2. **Stop execution:** Return control to the operator with a clear explanation

> `in_progress` items are filtered by `wl next`; an orphaned one blocks work.

### Abort Scenarios

The implement skill covers **five** abort/failure scenarios: (1) dirty work
tree abort, (2) definition gate failure (unclear scope, untestable ACs), (3)
user-initiated abort, (4) error/exception during implementation, (5)
unexpected termination (covered by the Final cleanup step).

> **Status reset is conditional, not blind:** `_safety_reset_if_in_progress()`
> resets to `open` **only if** currently `in-progress`; SIGINT/SIGTERM during
> any phase releases the item via `os._exit`.

### Error/exception handling (abort on unexpected errors)

On unexpected error (API/network failure, exception): (1) reset to open
(`StatusLifecycle.update_status(<work-item-id>, "open")`); (2) log it
(`wl comment add <work-item-id> --comment "Error: <description>" --author "<AGENT>" --json`);
(3) return control with details; (4) operator may retry if transient.

### User-initiated abort

If the operator cancels after Step 2: reset to `open`
(`StatusLifecycle.update_status(<work-item-id>, "open")`), return control, and
document: `wl comment add <work-item-id> --comment "Aborted by operator" --author "<AGENT>" --json`.

### Recoverable interruption (interrupted after the push)

An `implement.py finish` interrupted **after** its commit/push but **before**
the final lifecycle write leaves the item `open` with the assignee cleared —
never `completed`/`in_review` for unfinished work — and records one comment
starting `Run interrupted:` that names the commit (marked `pushed to dev` or
`committed locally, not pushed`), the branch, whether the worktree survived,
and the command that resumes (SA-0MVXUSJJY009HMQ0). Retry with:

```bash
python3 $(skill_path implement)/scripts/implement.py finish <id>   # tree retained
python3 $(skill_path implement)/scripts/implement.py start <id>    # tree removed; then finish
python3 $(skill_path implement)/scripts/implement.py finish <id>
```

A commit that already reached `dev` makes the branch diff-free, so the retry
converges through the ordinary no-op-push path. Both the interruption record
and the completion comment are deduplicated on a stable marker plus the
commit hash, so a recovery never stacks a duplicate completion comment or a
second terminal transition in one run.

### Relocated: Step 2 — Safety gate: handle dirty working tree

`implement.py start` performs this gate automatically, after it fetches the
work item; the agent does not need to create the worktree manually.

- Run `git rev-parse --is-inside-work-tree` (worktree?) and
  `git status --porcelain=v1 -b` (uncommitted changes?).

**CRITICAL: Never stash, commit, or revert the user's uncommitted changes
without explicit permission** — they may be user-authored work; stashing them
without asking can strand that work and is forbidden. When uncommitted changes
exist, STOP and ask the operator how to proceed (commit, stash, revert, or
abort); act only after the operator explicitly chooses.

`implement.py start` implements this rule as a **Key-Files-aware dirty-tree
gate** (SA-0MV0NSOGE000OSMR). After fetching the work item it parses the
description's Key Files section (any of `## Key Files`, `## Key Files
(predicted)` or `**Key Files:**`) and compares every dirty path against those
paths:

- **Dirty files demonstrably irrelevant** (no overlap with Key Files): the
  script proceeds, creates a clean worktree from `HEAD`, leaves the dirty
  files untouched in the main checkout, logs the decision and the exact paths
  left behind in a work-item comment, and reports `dirty_worktree: false`.
  No operator intervention is required.
- **Any dirty file overlaps Key Files, or no Key Files section is present**
  (relevance cannot be judged): the script aborts with the original
  operator-ask behaviour, resets the work item to `open`, and creates no
  worktree. The agent must stop and ask the operator.

The gate protects the **main checkout** only. When `implement.py start` is
invoked from inside a linked worktree (e.g. resuming a driven child), the
gate is skipped: any dirty files there are the agent's own in-progress work.

- **Inside a worktree:** `.worklog/`-only → carry forward; otherwise resume
  normally (the main-checkout gate is skipped); never stash/commit/revert
  unilaterally.
- **Main checkout:** `.worklog/`-only → carry forward; otherwise rely on the
  Key-Files-aware gate above — it creates the worktree for isolation without
  touching the user's changes. If dirty files prevent worktree creation, stop
  and ask the operator (act only on their explicit choice).
- **Project-declared expected-dirty paths** (e.g. `.llm-wiki/`, declared via
  `.pi/skills_extensions/implement/extension.json` → `ignoreDirtyPaths`) do
  not stop the gate. Treat them as read-only background state: never stash,
  commit or revert them. Any dirty path outside the declaration still stops
  the workflow (see [Project-local extensions](#project-local-extensions)).

On abort: `StatusLifecycle.update_status(<work-item-id>, "open")`

### Relocated: Step 3 — Stash hygiene gate (warn on orphaned stashes)

After the dirty-tree check passes, `implement.py start` inspects
`git stash list` on the main checkout. Stashes that reference an open work
item (e.g. `stash@{0}: On dev: WIP: partial SA-0XXXXXXX`) are matched and
not flagged. Stashes with no work-item reference, or where the referenced
work item is not in an open state, are reported as **orphaned**.

- Orphaned stashes trigger a **WARNING** (not a hard error) — the gate is
  fail-open. Run with `--allow-orphaned-stashes` to acknowledge and proceed.
- Each orphaned stash should be triaged: restore-and-commit via a proper work
  item if valuable, or delete if stale. See the recovery playbook below.
- The hygiene check is also available as a periodic script:
  `scripts/hygiene_check.sh` (run via cron, CI, or manually).

**Example warning:**

```
⚠  Orphaned stash(es) detected
============================================================
WARNING: 1 orphaned stash(es) detected on the main checkout.

Orphaned stashes:
  - stash@{1}: On dev: WIP: forgotten experiment

Triage these stashes (restore-and-commit via a proper work item, or delete if stale).
Proceed anyway with --allow-orphaned-stashes.
============================================================
```

### Relocated: Step 5 — Create a worktree from dev and branch inside it

> **MANDATORY — worktree requirement:** All implementation work MUST be done
> in a git worktree created from `dev` — never edit, commit, or push from the
> main checkout. `implement.py finish` refuses if it detects changes outside
> the worktree; `implement.py start` creates it for you — `cd` into it and do
> all work there.
>
> **`cwd` is not sufficient.** Before your first write, verify you are
> *operating in* the worktree and that every write/edit path resolves inside
> it:
>
> ```bash
> expected="$(pwd)"   # after `cd` into .worklog/worktrees/wl-<WIP-id>-<slug>
> test "$(git rev-parse --show-toplevel)" = "$expected" \
>   || { echo "REFUSING: not inside the worktree"; exit 1; }
> # Driven child sessions can assert the injected root instead:
> test "$(git rev-parse --show-toplevel)" = "$IMPLEMENT_WORKTREE_PATH" || exit 1
> ```
>
> Never edit through an absolute path under the main checkout (e.g.
> `/…/main-checkout/src/…`) — `cwd` is only a hint an agent can override per
> command. `phase_parent`/`drive` detect a driven child whose work landed in
> the main checkout and fail closed, naming the offending paths. See
> [docs/dev/worktree-isolation.md](../../docs/dev/worktree-isolation.md) for
> the `wl`-vs-edits split (run `wl` from inside the worktree with
> `wl --worklog-dir <main-checkout>/.worklog …`; never `cd` to the main
> checkout to edit).

```bash
git worktree add --track -b wl-<WIP-id>-<short-slug> .worklog/worktrees/wl-<WIP-id>-<short-slug> dev
cd .worklog/worktrees/wl-<WIP-id>-<short-slug>
```

> **Resume is `cwd`-independent.** The default worktree path is relative to
> the **main checkout** (`.worklog/worktrees/wl-<id>-<slug>`) and
> `implement.py start` resolves it against the main checkout root, never
> against `cwd`. Running `start <id>` from inside the item's own worktree
> (the driven-child / post-crash resume) therefore resumes the registered
> tree instead of resolving a doubly-nested
> `<worktree>/.worklog/worktrees/…` path and failing in `git worktree add`
> (SA-0MVXUGXS5006M3HF). `--worktree-path` keeps its historical semantics:
> an absolute override is used as given, a relative one resolves against
> `cwd`.

> **`node_modules` is a worktree-local link farm:** `implement.py start`
> materialises `<worktree>/node_modules` as a **real directory** (not a
> whole-directory symlink) holding one symlink per entry of the main
> checkout's `node_modules`. Scoped packages (`@types/*`, `@worklog/*`, …) get
> a real scope directory with one symlink per package, and `.bin` is linked as
> a whole directory. Workspace packages consumed by path (e.g.
> `@worklog/shared`) are **overridden to the worktree's own `packages/*`**
> (discovered from the `workspaces` field), so every import — tests,
> `src/**`, and spawned CLI subprocesses — resolves to the code in *this*
> worktree instead of the main checkout's gitignored `dist` (introduced as a
> whole-directory symlink by SA-0MSGS763C006SM1B; superseded by this link farm
> in SA-0MVPI400Y001FOM1).
>
> **Resolution order:** `<worktree>/node_modules/<pkg>` → (symlink) the main
> checkout's `node_modules/<pkg>` for third-party dependencies, but → the
> worktree's `packages/<pkg>` for workspace packages consumed by path. The
> step is idempotent and re-materialises entries when the main checkout's
> dependency set changes. It also links **nested** `node_modules` directories
> whose parent package directory already exists in the worktree
> (`<worktree>/<pkg>/node_modules -> <main-checkout>/<pkg>/node_modules`,
> OSL-0MUFG3PJA00379CT). Discovery is bounded — it prunes `.git`/`.worklog`
> and never descends into a dependency tree; existing entries are never
> overwritten and a missing parent package directory is never fabricated.
> Repositories without a workspace package consumed by path degrade to the
> previous whole-directory symlink.
>
> **Refresh command.** After the main checkout's dependency set changes,
> re-run the link step by running `implement.py start <work-item-id>` (or
> `implement.py parent`/`drive`) from the **main checkout**; the step is
> idempotent, so an existing worktree is reconciled in place. The `start`
> JSON report records the outcome under `node_modules` (`link_created`) and
> `shared_resolution` / `shared_resolution_ok`.
>
> **Stale-resolution failure signature.** If a worktree changes
> `packages/shared/src/**` while `@worklog/shared` still resolves to the main
> checkout's `packages/shared` (a pre-link-farm worktree, a missing farm, or
> a stale one), the gate tests the wrong code:
>
> - **false red** — a correct new shared export is invisible; a test that
>   spies on it fails with “spy never called” (the WL-0MVK6S4HM005GBCO
>   symptom);
> - **false green** — a broken shared change still passes.
>
> `implement.py finish` now **fails closed** in this case (after the build,
> before the test gate) with a message naming the stale
> `<main-checkout>/packages/shared/dist` path and the remediation: re-run
> `implement.py start <work-item-id>` from the main checkout to refresh the
> link farm, then rebuild and re-run `finish` (SA-0MVPI41SR008JYXL). Repos
> without a workspace shared package no-op.
>
> **Do NOT run `npm install` inside a worktree** — writes pass through the
> farm into the shared install and corrupt it, and the link farm needs no
> install. A branch that changes `package.json` still resolves the main
> checkout's dependencies; reinstall in the main checkout if that matters.
>
> **The manual `rsync` workaround is superseded.** Copying a worktree's
> `packages/shared/dist` into the main checkout mutates the `dist` that the
> live `wl` CLI and every concurrent worktree execute (the
> WL-0MQRBT8BS00355AB incident class) and is no longer needed: the link farm
> makes the worktree's own build visible to its gate.

> **Git submodules are auto-initialised:** `implement.py start` runs
> ``git submodule update --init --recursive`` inside the new worktree (SA-0MSN52GGN002B0AZ).
> Failures produce a ``WARNING`` log message but **do not abort** — the worktree
> remains usable (best-effort). Repos without ``.gitmodules`` are unaffected.

See [AGENTS_GLOBAL](../../AGENTS_GLOBAL.md#implement-the-work-item).

### Relocated: Step 6.1 — Parent recursion (epic/parent items only)

**Preferred: drive every child in one invocation.** Run:

```bash
python3 $(skill_path implement)/scripts/implement.py drive <parent-id>
```

Pane-driving flags and variables (opt-in; headless `pi -p` stays the
default for every caller that does not opt in):

| Flag / variable | Effect |
|---|---|
| `--pane-sessions` | Drive each child as an interactive Pi TUI agent in a sibling Herdr pane of the calling tab. Requires Herdr (`HERDR_ENV=1` **and** a resolvable herdr binary). |
| `--no-pane-sessions` | Force the headless `pi -p` spawner; wins over an ambient `IMPLEMENT_DRIVE_PANE_SESSIONS=1`. |
| `IMPLEMENT_DRIVE_PANE_SESSIONS=1` | Environment equivalent of `--pane-sessions` (for a repeated/scripted drive). |
| `--focus` / `--no-focus` | Whether each new child pane takes focus (**default `--no-focus`**: driving an epic never moves the operator's focus). |
| `IMPLEMENT_DRIVE_ACTIVE=1` | Set by the driver on the child session (recursion guard; it also defers that child's worktree removal to the driver). |
| `IMPLEMENT_WORKTREE_PATH` | Set on the child session to its own worktree root, so the child can verify where it writes. |

`phase_drive()` loops over `phase_parent` reports and, for every startable
child, spawns a **fresh Pi session** (`pi -p "/skill:implement <child>"`) in
that child's own worktree, waits for it to exit, then continues. A single
`drive` call therefore completes every child and advances the parent, while
each child keeps its own clean context window and its own worktree. Options:
`--json` (machine-readable per-child report), `--max-child-sessions N`
(default 1) to bound retries, `--finish-attempt-limit N` (default: the
`--max-child-sessions` value) to bound failing `finish` retries,
`--infra-retry-limit N` (default `5`) to bound transient
infrastructure-refusal `finish` retries on a separate budget with backoff,
`--child-timeout S` (default 3600),
`--finalise-stranded` (opt-in, evidence-gated finalisation of stranded
children — see below), and `--pane-sessions` / `--no-pane-sessions` with
`--focus` / `--no-focus` (default `--no-focus`). When no Pi executable is
available it stops with actionable manual instructions rather than silently
skipping a child (`IMPLEMENT_DRIVE_PI_BIN` overrides the binary). A driven
session is marked `IMPLEMENT_DRIVE_ACTIVE=1` and refuses to re-enter `drive`.

**Pane transport (opt-in, fail-open).** With `--pane-sessions` (or
`IMPLEMENT_DRIVE_PANE_SESSIONS=1`) **and** Herdr detected (`HERDR_ENV=1` plus a
resolvable herdr binary), each child is instead driven as an interactive Pi TUI
agent in a sibling pane of the calling tab: `herdr pane split --current --cwd
<child worktree>` → `herdr agent start … -- --approve --no-extensions` →
`herdr agent prompt … "/skill:implement <child>" --wait`. The child pane gets
`IMPLEMENT_DRIVE_ACTIVE=1` and `IMPLEMENT_WORKTREE_PATH` but never the driving
session's `PI_SESSION_ID` / `PI_SESSION_FILE`. Focus never moves unless
`--focus` is passed. Without Herdr or without the opt-in, drive uses the
headless `pi -p` spawner unchanged — so automated callers (downtime worker,
audit dispatch, CI) are unaffected. Child terminality is still decided by the
next `phase_parent` stage re-read, never by process exit.

**A driven pane is closed or retained — never leaked
(SA-0MVPHZ1CI0045QR0).** The agent's settled state
(`agent prompt … --wait --until done --until blocked`) is a fast-path signal,
not the verdict: once the next `phase_parent` re-read confirms the child is
terminal, the pane is closed (`pane close <id>`). A pane whose child the parent
still sees non-terminal is kept, titled ⚠️ and listed in
`report["retained_panes"]` with its `herdr pane read <id>` remedy. An agent
that reports `blocked` **stops the chain**: `blocked_child` / `blocked_pane`
are recorded, the pane is **deliberately left open for inspection** and titled
🚫, and the message names it. A timeout closes the pane (after reconciling the
child's real stage) and stops the chain; any other failed session keeps its
pane as evidence.
Completion is never inferred from process exit. Every `children_driven` entry
carries `transport` and `pane_id` (`None` for the headless transport), and the
human summary lists retained panes with their inspection remedy.

**Worked example — driving an epic in a Herdr tab.** From a Pi session running
in a Herdr pane, in the epic's project:

```bash
python3 $(skill_path implement)/scripts/implement.py drive SA-0MVLKHY27005UK40 --pane-sessions
```

What the operator sees: one sibling pane opens to the **right** of the calling
pane for each child (never a new tab or workspace), running an interactive Pi
TUI agent named `drive-<child-id>`; focus stays in the invoking pane because
the default is `--no-focus`. The invoking pane prints a `transport: pane, pane
<id>` line per driven child, and closes that child's pane once the next
`phase_parent` re-read confirms the child is terminal.

A child agent that settles **`blocked`** needs a human decision: the chain
stops, `report["blocked_child"]` / `report["blocked_pane"]` name it, the pane
is **left open** and titled 🚫, and the message names the pane to look at. Read
it with the reported remedy:

```bash
herdr pane read <pane_id>     # retained_panes[].remedy prints this exact command
```

After intervening (answering in that pane, or resolving the blocker with
`wl`), re-run the **same** `drive` command. Terminal children are skipped and
the chain resumes at the first child with work left; a pane session that is
still alive holds its child's per-child lock, so the re-run reports that child
as `skipped-held` (naming the holder) instead of racing it.

**The driver owns the child's worktree (SA-0MVTE8XAJ002IUDR).** A driven
child's `finish` defers worktree removal (the tree is that session's own cwd,
so deleting it inside `finish` breaks every later command). Once the child's
session has exited, `drive` removes the tree itself — only when the child is
terminal **and** the tree is clean, never while a session may still be alive.
A non-terminal or dirty tree is retained and named in
`report["retained_worktrees"]` with the exact `git worktree remove … && git
worktree prune` remedy, and leftover trees of terminal children are swept at
the start of the next drive (recorded in `report["worktree_sweep"]`).

**A timeout is a bound, not a verdict (SA-0MVHXWLPG007OQVN).** After a failed
child spawn (timeout or non-zero exit) `drive` re-fetches the child and
classifies it: a child that already reached a terminal state is recorded as
`completed-late` and the chain continues; a genuinely incomplete child keeps
the hard failure. Report vocabulary, per-child entry fields and the stranded
child handling:

| `result` | Meaning |
|---|---|
| `finished` | The child session exited successfully. |
| `completed-late` | The session timed out/failed **but** the child is terminal (status `completed`/`done`/`in_review`, stage `in_review`, or soft-deleted). The chain continues. |
| `terminal-reconciled` | The child's `finish` failed but the child was already terminal; recorded as done, the chain continues. |
| `reset-for-retry` | The child was non-terminal after its finish-attempt bound; its worktree was reset to the parent-branch HEAD and its status set back to `open` for a fresh session. Only reachable for **uncommitted** WIP: a worktree carrying commits is preserved on `refs/wip/<id>`, never reset. |
| `finalised` | A stranded child was finalised via `--finalise-stranded` (verified pushed commit, still in-progress, no other agent, clean worktree). The chain continues. |
| `failed` | The child is not terminal — the chain stops and `failed_child` is set. |

Spawn-failure entries additionally carry `timed_out`, `timeout_seconds`,
`terminal`, `terminal_status` and `terminal_stage`; `report["completed_late"]`
counts the completed-late children and the summary message states how many
completed late. A non-terminal child whose work is already on the parent
branch appears in `report["stranded_children"]` with its pushed-commit
evidence (read-only: report plus one parent comment, no status mutation).
`--finalise-stranded` additionally finalises such children through
`StatusLifecycle` (`completed`/`in_review`) when every safety gate holds, and
lists them in `report["finalised_children"]`.

**A failing `finish` is bounded, reconciled, then reset
(SA-0MVLIZ33M009C5QY).** A child whose worktree is dirty after a dead session
makes `phase_parent` re-run the same failing `finish`, which used to livelock
the epic. Every failing `finish` is first **classified** by the single,
table-driven classifier `classify_finish_failure` into one of three kinds:

- `infrastructure` — a transient refusal that self-clears (a concurrent
  session's local-`dev` fast-forward, a held main-checkout lock, a dropped
  push connection, a failed ancestry assertion);
- `gate` — a genuine failure (red tests, a failed build, a
  worktree-placement violation, an empty diff);
- `unknown` — anything not listed in either vocabulary, which **fails
  safe**: it is never `infrastructure`, so drive can never retry an
  unclassified failure as infrastructure.

`phase_drive` recovers within per-child bounds, in this order:

- **1. Infrastructure retries — `--infra-retry-limit N`** (default `5`): a
   refusal classified `infrastructure` is retried on its **own** bounded
   budget with exponential backoff (2 s doubling to a 30 s cap) — it never
   consumes the finish-attempt budget and **never spawns a session**: the
   loop just re-runs `parent`. Each retry records a `children_driven` entry
   with `result: "infra-retry"` (plus `step`/`reason`).
- **2. Bounded finish retries — `--finish-attempt-limit N`** (default: the
   `--max-child-sessions` value): genuine failing `finish` executions are
   counted per child in `finish_attempts` and never exceed the bound.
- **3. Terminal reconciliation — `terminal-reconciled`**: when the bound is
   exhausted the child is re-fetched with `wl show`; a child that reached a
   terminal state anyway is recorded as done — never finished, reset or
   re-spawned.
- **4. Reset for retry — `reset-for-retry`** (at most once per child per
   invocation): a genuinely non-terminal child's **own** worktree is reset
   to the parent-branch HEAD (`git reset --hard <parent_branch>` + `git
   clean -fd`, isolation-gated) and its status set back to `open` via
   `StatusLifecycle`, with an audit comment naming the failure (with the
   classified failing `finish` step/reason when available), the reset and
   the discarded file list (never a commit hash). `reset-for-retry` is
   limited to **uncommitted** WIP: a worktree that carries commits is
   **preserved** — the **commit-preservation gate** parks its tip on
   `refs/wip/<id>` before any destructive command runs, refuses the reset,
   reports the preservation in `report["preserved_refs"]`
   (`{id, ref, commit}`) and stops the chain — a committed slice is never
   discarded by the recovery path.

Every verdict — `infra-retry`, `reset-for-retry`, `terminal-reconciled` and
the chain-stop messages — names the real failing `finish` step and reason
(`"<step>: <reason>"`, e.g. `local_dev_fast_forward: local_dev_diverged`),
plus the main-checkout lock holder and/or the guard's offending paths and
competing work item when the failing `finish` report carries them, instead of
a bare `Failed to finish child <id>` (LP-0MVWEP37K000JU0O, parent AC5). The
report surface grows accordingly: the failing parent report carries
`finish_failure` (`{kind, step, reason, message, steps}`); the drive report
adds `infra_retries` (per-child retry counts), `infra_retry_limit` (the
applied bound) and `preserved_refs` (the preservation entries) — all
additions are additive, existing keys keep their meaning and types.
`parent_advanced: false` therefore always means
real non-terminal or blocked work remains. Full contract:
[docs/dev/implement-skill-reference.md](../../docs/dev/implement-skill-reference.md#drive-failure-recovery-bounded-finish-attempts-reconciliation-and-reset).

> **Child sessions run extensions-disabled.** The spawner builds
> `pi -p "/skill:implement <child>" --approve --no-extensions` so a globally
> installed `turn_end` extension cannot trip Pi core's
> `_dispatchTurnEndBoundary` and flood the child's captured stderr
> (SA-0MUY9PYBD009Y7J5). Rationale, root cause and scope limits:
> [docs/dev/implement-skill-reference.md](../../docs/dev/implement-skill-reference.md#why-driven-child-sessions-run-with-extensions-disabled-sa-0muy9pybd009y7j5).

**Manual fallback: one `parent` invocation per child.** If you cannot spawn
sessions (e.g. headless tooling), recurse manually. A parent invocation
recurses into its children automatically. Run:

```bash
python3 $(skill_path implement)/scripts/implement.py parent <parent-id>
```

(`implement.py parent` — orchestrated by `phase_parent()`):

- **No children** → behaves like a leaf: use the standard `start`/`finish`
  workflow unchanged.
- **All children terminal** (`in_review`/`completed`/`done`) → the parent is
  advanced to `completed`/`in_review` (existing Step 6.1 advancement
  retained) and a per-child summary (ids, statuses) is commented.
- **Children remain** → `phase_parent` walks **all** children in dependency
  order in a single pass: it finishes every child whose worktree already
  contains changes, and starts the next unimplemented child. The report
  carries `children_processed` (per-child action/status), the started child
  (`next_child` plus its `worktree_path`/`branch`), or the parent
  advancement when every child is terminal.
- **The invoked item is itself blocked** → when the parent's **own** outbound
  (`depends-on`) blocker is unmet, or its own `status` is `blocked`, every
  child is reported as `blocked-parent` and **nothing** is started: no child
  claim, no worktree, no code. The report carries `parent_blocked_by` (blocker
  ids + statuses), `parent_blocked_reason` (`outbound-dependency`,
  `status-blocked`, or both joined by `+`), `blocked: true`, no `next_child`
  and no `parent_advanced`; `success` stays true because a blocked wait is not
  a failure. `drive` forwards the same fields and spawns no session. A
  `blocked` status with no unmet recorded dependency is still honoured (never
  silently overridden); the message names the remedy — `wl update <id>
  --status open` — because `wl dep add` sets `blocked` automatically and
  nothing clears it when the dependency completes.

**The invoked item's own outbound edges gate starting
(SA-0MVS1QS9J009DGPT).** `phase_parent`/`phase_drive` and `phase_start` all
resolve `wl_dep_blockers(<the invoked item>)` before doing any work. An unmet
(non-terminal) blocker — `_is_terminal_child` semantics, so `in_review`,
`completed`, `done`, `deleted` and soft-deleted items are terminal — means the
item waits. A producer's `depends-on` edge (or a `blocked` status) is therefore
never silently overridden: a blocked epic can no longer start editing files an
in-progress dependency still owns. This mirrors `wl next`, which skips
dependency-blocked items.

Scope of the gate: **same-project** edges (the same id prefix, e.g. `SA-` on
`SA-`) gate. An edge to another project (e.g. `OSL-…` on an `SA-…` item) is a
**coordination note**, reported in `coordination_blockers` and never blocking
— that is the SA-0MSQBM2FK005NW1T assumption, clarified here: it covers
*cross-project* edges only, **not** same-project edges recorded on the invoked
parent itself. A child blocked by a same-project item outside the epic is
reported as `blocked` (in `blocked_children`) and is not started; `phase_start`
refuses the equivalent leaf case with the blocker list, before the claim, so no
orphaned `in_progress` item and no worktree are left behind. A child whose own
`status` is `blocked` is additionally re-validated against its `Blocked by …`
line (see *Blocked-child re-validation* below), so a prose/cross-project
blocker holds it instead of being a note.

Then implement that child by recursing into this procedure (steps 1–8), and
re-run `implement.py parent <parent-id>`. The parent phase **finishes the
previous child for you** (`phase_finish` in its own subprocess) and starts
the next one — do **not** call `implement.py finish` separately. Repeat until
the parent reports all children terminal. Each child is implemented in its
own worktree (never the main checkout); sequential children reuse/rotate the
`.worklog/worktrees` machinery. (With `drive` this loop is performed for you;
the driven child session may call `finish` for its own item — `start` resumes
the pre-created worktree idempotently from **either** `cwd` — the main
checkout or inside the worktree — because the default worktree path is
resolved against the main checkout root, not `cwd`.)

**Blocked-child re-validation and its report vocabulary
(SA-0MVTUFL0U0000NV2).** A `blocked` child is re-validated on every run
instead of being treated as startable or honoured forever.

- **`Blocked by` parsing rule (narrow).** Only one explicit `Blocked by …`
  line is read — case-insensitive, tolerating the bold/labelled form
  (`**Blocked by (SorraAgents):** `SA-…``) and a plain `Blocked by:` — and only
  work-item ids on that line are collected. An `Unblock condition:` line or a
  `Related work` list is never parsed. Malformed or empty input yields "no
  blocker", never an exception: the verdict then stays fail-closed
  (`unvalidated`).
- **Per-child fields** (`report["children"]` and the `children_processed`
  verdicts): `action: "skip-blocked"`, `blocking_ids` (the ids that hold it),
  `stale_block_ids`, `unvalidated`, `blocker_details`
  (`[{id, status, source: dependency|prose, resolved, hint}]`) and
  `unresolved_blockers` (the entries whose id could not be resolved).
- **Report-level fields:** `stale_blocks` (`[{id, blocking_ids}]`),
  `unvalidated_blocks` (`[{id, reason}]`) and
  `unresolved_blockers` (`{child id: [blocker ids]}`). `blocked_children`
  keeps its existing id-list shape.
- **Messages.** The "no startable child" message names each blocked child's
  `blocking_ids`; an unresolved id is reported with where to check it
  ("check the owning project's worklog", with the id prefix as the hint).
- **`drive`.** A `skip-blocked` child is never spawned: `drive` forwards the
  verdicts as `blocked_child_details` (child id, status, `blocking_ids`,
  `stale`/`stale_block_ids`, `unvalidated`) alongside `blocked_children`, plus
  the report-level fields above, and its message names the blocking ids. If a
  parent report ever offered a `skip-blocked` child as `next_child`, the drive
  loop refuses to spawn it (no lock, no session) and stops.

**Single-pass, subprocess-isolated orchestration.** Every child's start and
finish runs through `_invoke_implement` — a **separate `implement.py`
subprocess** invoked serially, never a direct in-process call. The main
agent process keeps a clean view of the chain: it emits a periodic per-child
progress update and a final summary (`children_processed`), and advances the
parent last. This reduces a run of *N* children to *N* `parent` invocations
(plus the initial one) instead of *2N* `start`/`finish` invocations.

**One session per child (session isolation).** Invoking `/skill:implement
<parent-id>` on an epic may start a **new Pi session for each child** — do
not implement every child in one accumulating session. Each child's session
opens with a clean context window containing only that child's work-item
description, acceptance criteria, and relevant context. Session isolation
layers on top of the subprocess-isolated orchestration above:

- **Serial, dependency order.** Children are still implemented serially with
  blocking items first; the dependency, cycle, and blocked-child guards
  below are unchanged.
- **Worktree isolation preserved.** Every child is still implemented in its
  own worktree created by `phase_start`; session and worktree isolation are
  independent and both apply.
- **Verify the worktree root before the first edit.** A driven child is
  spawned with `cwd=<child worktree>` and `IMPLEMENT_WORKTREE_PATH=<child
  worktree>`, but `cwd` is not sufficient — before writing, assert
  `test "$(git rev-parse --show-toplevel)" = "$IMPLEMENT_WORKTREE_PATH"` and
  keep every write/edit path inside that root. A child that writes to the main
  checkout is detected by the worktree placement guard below and fails closed.
  See [docs/dev/worktree-isolation.md](../../docs/dev/worktree-isolation.md).
- **Session logging.** Each new session comments on the child work item with
  its session id (`<agent_action> - Session ID: <pi_session_id> -
  <path_to_sessions_log>`), per the AGENTS.md session-logging convention.
- **Error isolation.** A failure in one child's session does not affect the
  other children's sessions: the failed child is reset to `open`, the parent
  phase reports which children succeeded and which failed, and
  already-completed siblings are never regressed.
- **Parent advanced last.** The parent is advanced to `completed`/`in_review`
  only after **all** child sessions have reached a terminal stage.

**One child = one holder (per-child lock).** A child's critical section —
work-item claim plus worktree creation/resume — is serialised by an exclusive
`fcntl.flock` at `<worklog>/locks/child-<id>.lock`, acquired in `phase_start`
before any mutation and held for the holder's lifetime. `drive`, `parent` and
a manual `start` all share this choke point:

- **A held child is never started twice.** If another live session owns the
  lock, the child is classified `skipped-held`: the report carries
  `action: "skipped-held"`, `skipped_held: true`, the holder identity in
  `held_by` (`agent`, `pid`, `session_id`, `started_at`) and the child in
  `held_children`, and **no session is spawned** for it. Nothing is written —
  no claim, no status/assignee change, no comment, no worktree change.
- **Ownership is a token, not a pid.** The holder exports
  `IMPLEMENT_CHILD_LOCK_TOKEN` to its subprocesses and to the driven child
  session, so the driver's own `parent` → `start` chain and the child session
  re-enter the lock instead of blocking on themselves.
- **Release is kernel-guaranteed.** Normal exit, exceptions and
  `SIGINT`/`SIGTERM` release the lock explicitly; `SIGKILL`/crash release it
  when the kernel closes the descriptor. A recorded holder whose pid is dead is
  reclaimed automatically with a logged warning naming it **and surfaced in the
  report** (`lock_reclaimed`, `reclaimed_from`, `lock_path`, `warnings`, also
  forwarded by `parent`/`drive`).
- **Not the same as `--assignee`.** A non-empty assignee is a human-visible
  scheduling hold; the lock is proof of a live process. Never infer a live
  holder from `assignee`, and never treat a live lock as an assignee.

Operators diagnose a held child from the report's `held_by` (and `lock_path` in
a `start` report) or by
reading `<worklog>/locks/child-<id>.lock`, then check `ps -p <pid>`; see
[docs/dev/implement-skill-reference.md](../../docs/dev/implement-skill-reference.md#per-child-implementation-lock-sa-0mvq7vmrl00498yb).

This mirrors the "Epic/parent items — one session per child" guidance in
`AGENTS_GLOBAL.md`.

Guards (deterministic, in `phase_parent`):

- **Dependency order** — a child `blocked` by another item is implemented
  only after its blockers; the chain is resolved dependency-order correct.
- **The invoked item is never started while blocked** — the parent's own
  outbound blockers and its own `blocked` status are resolved *before* any
  child is classified; an unmet same-project outbound blocker or a `blocked`
  status yields the dedicated `blocked` verdict (`parent_blocked_by`) and no
  child worktree. `phase_start` applies the same outbound gate to a leaf,
  before the claim.
- **Cross-project edges on the invoked item are coordination notes** — an
  edge from the parent/leaf item to another project (different id prefix)
  never gates; it is reported in `coordination_blockers`. That is the
  SA-0MSQBM2FK005NW1T assumption and it is unchanged. It does **not** cover a
  `blocked` child's declared blocker: a cross-project id named on the child's
  explicit `Blocked by …` line holds that child's block
  (SA-0MVTUFL0U0000NV2), resolved through the prefix-to-sibling worklog scan so
  a landed external blocker makes the block stale instead of stranding the
  child forever.
- **Terminal children are never re-implemented** (skipped, reported).
- **Held children are never raced** — a child whose per-work-item lock is held
  by another live session is reported as `skipped-held` (with `held_by`) and no
  second session is started; the phase stops cleanly and can be re-run once the
  holder finishes.
- **In-progress by another agent** (no worktree) → skipped and reported,
  never clobbered. An in-progress child *we* started (worktree exists) is
  finished when its worktree has changes, or returned to the agent for
  implementation when it does not.
- **Completed children are never restarted** — a finished child joins
  `children_processed` and the loop moves on; already-completed siblings are
  never regressed.
- **Cycles fail fast** with a clear error (no infinite recursion).
- **Blocked children are re-validated, never honoured forever**
  (SA-0MVTUFL0U0000NV2) — a child whose `status` is `blocked` is never started
  while its block holds, and a stale block never strands it. Every run
  resolves the block against (a) the outbound `wl dep list` edges and (b) the
  ids on the child's explicit `Blocked by …` line, if present:
  - any non-terminal blocker (or an unresolvable cross-project id) ⇒ the child
    is classified `skip-blocked`, reported in `children_processed` with
    `action: "skip-blocked"` and its `blocking_ids`, listed in
    `blocked_children`, and **never started**;
  - every blocker terminal (`completed`/`done`/`in_review`/soft-deleted) ⇒
    the block is **stale**: the child becomes startable and the stale ids are
    reported in `stale_blocks` (plus a log line and a comment on the child —
    the phase never rewrites the child's `blocked` status, which is the
    lifecycle owner's decision);
  - no dependency edge and no parseable `Blocked by` line ⇒
    `unvalidated_blocks` and the child is held (fail closed; it never
    defaults to startable).

  Classification precedence is exactly `skip-terminal` → `skipped-held` →
  `skip-in-progress` → `skip-blocked` → `implement`. A child whose blocker is
  a same-project item outside the epic is unchanged: reported `blocked` with
  its `blocked_by` payload.
- **No premature parent advance** — if any non-terminal child remains
  (in-progress elsewhere or blocked), the parent is not advanced; the phase
  reports waiting instead.
- **Abort/failure** in a child resets THAT child to `open` (StatusLifecycle
  abort semantics) and stops the chain with a report of what completed and
  what failed; already-completed siblings are not regressed.
- **No orphaned `in_progress`** — a child start failure or abort leaves no
  in-progress state behind; re-run the parent phase after resolving the
  blocker to continue the chain.
- **Worktree placement guard** — a non-terminal child whose worktree holds no
  work (no dirty files, no commits ahead of the parent branch) while the main
  checkout is dirty outside `.worklog/` — **or a registered sibling worktree
  of the same repository holds changes** (SA-0MW5FIJNQ002Z24I) — has written
  outside its own worktree; the phase fails closed with the offending
  surfaces (and does not advance the parent) instead of silently proceeding.
- A parent with no children or all-terminal children behaves as today.

Worktree isolation per child is preserved: every child is implemented in
its own worktree created by `phase_start` (never the main checkout);
sequential children reuse/rotate the `.worklog/worktrees` machinery. The
parent itself gets no worktree.

- Write tests and code to meet ACs:
  - **Write tests first** (TDD preferred) — at least one test file before
    editing implementation code; minimal, focused changes.
  - External constraints prevent complete tests → harnesses/mocks/placeholders,
    documented; follow project style; comment on significant decisions.
  - Discovered additional work → `wl create "<title>" --deps discovered-from:<work-item-id> --json`
- Once all ACs are met: **build** (no errors); **validate via the [test skill](../../skill/test/SKILL.md) — the loop is scope-aware (SA-0MT6BYQHB008DOGC)**: `implement.py finish` runs a **changed-scope** validation (only the tests affected by this change — fast iteration). No full suite runs here: it is **deferred to the dev-push integration gate**, where the **pre-push hook enforces the full suite** (`--scope full`) on the push to `dev`, and the **ship release gate** re-runs it on the merged dev→main tree (SA-0MVECQUGF0064U81). The changed-scope execution delegates to the test skill's canonical `guarded_run_all` (the single source of truth for the outer live-repo guard, SA-0MUH1MJL6003767Q), so a green gate always implies the checkout was verified for mutation regardless of which entry point ran it. `run_tests.py` execution must go through the test-skill runner (`/skill:test`, `run_tests.py --scope full`): only the test-skill runner records runs in the per-repo test cache (keyed by git state + scope, 2h TTL) that the audit skill reads read-only via `query_cached()` to auto-verify execution-dependent ACs — an ad-hoc run (`npx vitest run`, `pytest`, …) is invisible to the audit, so the audit either auto-executes the suite itself on a cache miss (F3, SA-0MSTN5KRF0097TVP) or the operator attests with `--green-run HEAD` (it never hard-blocks — F4, SA-0MSTN8CWM003AAU9). If a gate reports full-suite failures: triage helper (`python3 $(skill_path triage)/scripts/check_or_create.py '{"test_name":"<name>", "stdout_excerpt":"...", "stack_trace":"...", "parent_work_item_id":"<this-work-item-id>"}'`); implement returned critical issues, re-run until green. Update docs (except `CHANGELOG.md`); summarize changes.

### Relocated: Step 9 — Commit, Push to dev and mark in_review

> **Ordering contract (SA-0MVXUSK02003ZMDN).** `finish` returns only when the
> run is **fully complete**: it performs the read-only post-push verification,
> the completion comment and (unless deferred) the worktree removal *before*
> the terminal write, and the terminal `completed`/`in_review` write plus the
> assignee release are its **last lifecycle action** (design A,
> SA-0MVXUSED6008J04R). The item stays `in_progress` with the assignee held
> for that whole window, so the herdr downtime dispatcher can never dispatch
> an audit that races a still-running implement run. Do **not** perform a
> lifecycle-relevant step after `finish` returns; only the non-lifecycle
> worktree cleanup and the end-of-session report remain. Full contract:
> [docs/dev/implement-skill-reference.md](../../docs/dev/implement-skill-reference.md#finalisation-ordering-and-the-audit-dispatch-hold-sa-0mvxusk02003zmdn).

- **Plain leaf flow — run the finish with the deferral flag:**

  ```bash
  python3 $(skill_path implement)/scripts/implement.py finish <work-item-id> --keep-worktree
  ```

  `--keep-worktree` (or `IMPLEMENT_KEEP_WORKTREE=1`) defers `finish`'s own
  worktree removal, so the worktree — this session's cwd — survives for the
  end-of-session report (the completion comment and the post-push
  verification already ran inside `finish`, before the terminal write).
  The session then removes it itself with `implement.py cleanup <work-item-id>`
  as its **last** bash command (see
  [Worktree removal ownership](#worktree-removal-ownership-sa-0mvte8xaj002iudr)
  below and the [final cleanup step](#final-cleanup-step-remove-the-retained-worktree-plain-leaf-sessions)
  at the end of this file). Driven child sessions never pass the flag:
  `IMPLEMENT_DRIVE_ACTIVE=1` defers automatically and `implement.py drive`
  removes the tree after the session exits. `finish` itself builds, tests,
  commits, pushes to `dev`, runs the read-only post-push verification and
  marks the item `in_review`; the manual steps below are the reference for
  what it does, in the order it does them.
- Follow the mandatory build → test → commit order before committing.
- **Add the `## Impact` sentence before finishing.** The description of a newly completed item must carry an `## Impact` section: one sentence describing the user-experience impact — what a user can now do, see, or stop worrying about — phrased for a non-technical reader, not as a technical summary of what was built. `finish` advances the item to `in_review`, so write the sentence before running it. Standup reports quote it verbatim. The convention, together with the intentional no-retrofit rule for already-completed items, is stated in `AGENTS_GLOBAL.md`.
- **Do NOT create a Pull Request to `main`** — work is integrated into `dev`; the `dev`→`main` promotion is handled by the release process.
- Push the feature branch into `dev` via the ship skill (`pushToDev()` from `$(skill_path ship)/scripts/ship.js`, preferred) or `git push origin HEAD:refs/heads/dev`. `dev` is **not** protected; only `main`, `master`, `HEAD` are blocked.

  > **Pushing from a worktree:** the repo's pre-push hook runs `wl sync`, which
  > refuses to run from a worktree (worktrees have no local `.worklog`; the
  > data lives in the main checkout). First run `wl sync` from the main
  > checkout, then push from the worktree with the hook's documented bypass:
  > `WORKLOG_SKIP_PRE_PUSH=1 git push origin HEAD:refs/heads/dev`. Nothing is
  > lost by skipping the sync at push time — the main checkout syncs the data
  > on its own pushes.

  > **The dev push runs from the worktree (SA-0MVTFMB7K002Y1F3).** `finish`
  > launches `git push` from the **worktree that holds the pushed commit**, on
  > the first attempt and the non-fast-forward retry, so the pre-push
  > full-suite gate resolves its project root with
  > `git rev-parse --show-toplevel` in the pushed tree — the main checkout's
  > working tree is never the gate's subject. A pre-push subject guard **fails
  > closed** (`PushSubjectMismatchError`, naming the commit, branch, worktree
  > and actual `HEAD`) when the worktree cannot hold the pushed commit, instead
  > of pushing from a checkout holding a different commit. On success the hook's
  > `stderr` is logged and one
  > `dev-push gate evidence: root=… head=… pushed=… full-suite=<cached|executed|no-evidence>`
  > line is emitted (also in `report["steps"]["push"]["gate_evidence"]`); the
  > hook's `mirror_suite_cache` copies the worktree's cache entries into the
  > main checkout's `.worklog/cache` so the pushed commit's green full-suite
  > entry stays visible to post-push verification and the audit. The mirroring
  > is behaviour of the **canonical pre-push hook template** shipped by the
  > `worklog` package (`PRE_PUSH_HOOK_CONTENT`, emitted by `wl init` and kept
  > current by `wl doctor upgrade`) — not a repository-local patch, so
  > `wl doctor upgrade --dry-run` reports `pending: []` and `--confirm` never
  > replaces the capability.

  > **The gate's subject is the pushed commit (SA-0MVTRBT50005DUEH).**
  > `.githooks/pre-push` forwards git's `<local_sha>` as
  > `run_tests.py --gate-commit <sha>`; the runner resolves a checkout whose
  > `HEAD` is that commit with **no dirty paths** (the push cwd when it
  > qualifies — the normal `finish` path — else any clean
  > `git worktree list` checkout), runs/caches the full suite there, and
  > **refuses the push** (exit 2, naming the commit and `TEST_SCOPE_SKIP=1`)
  > when no clean checkout holds it. So "the dev push succeeded" means the
  > full suite ran against what landed: a green `scope: full` cache entry for
  > any other state (the main checkout's `HEAD` plus its dirty `.worklog/`
  > churn, or another commit) cannot satisfy the gate. The runner emits
  > `run_tests: gate-subject` / `gate-root` / `gate-verdict` lines on stderr
  > (echoed by the hook) naming the validated checkout, its `git_state`,
  > `fingerprint_kind` and executed-vs-cached; the hook mirrors the entry from
  > the checkout actually gated, not merely from the push cwd.

- **Post-push local parent sync (SA-0MUGX1DIT000M7S1).** `implement.py finish`
  fast-forwards the main checkout's **local parent branch** (`dev`) to the
  pushed tip *after* a successful push, so the next `implement.py parent` /
  `start` forks the following child worktree from an up-to-date base instead
  of a stale one. The sync itself is **safe-skip**: it performs a `git fetch
  origin dev` followed by a `--ff-only` merge (when `dev` is checked out) or a
  non-checked-out ref fetch (`git fetch origin dev:dev`) — it never
  force-updates and never loses local commits. It is skipped (with a logged
  warning, and without failing the already-successful finish) when the main
  checkout is dirty, the remote is unreachable, or the local parent branch has
  diverged. `implement.py start` also refreshes the parent branch before
  `git worktree add`, so children never start from a stale base even when
  another actor pushed concurrently.

  That whole window — commit → local `dev` fast-forward → push → worktree
  removal → sync — is serialised by a **per-repository finish lock**, the
  advance only ever moves with `git merge --ff-only` and is **restored to
  `origin/dev` when the push fails**, and the push is followed by an
  **ancestry assertion** that fails closed when the commit never reached
  `origin/dev` (SA-0MVPZG3IQ009IJMO, SA-0MVPZG7A0000N66U). When any of them
  refuses, the worktree and branch are retained and the item stays `open` —
  see [Finish lock, verified sync and refusal
  recovery](#finish-lock-verified-sync-and-refusal-recovery) for the state
  left behind and the exact recovery commands.
- **Worktree removal ownership (SA-0MVTE8XAJ002IUDR).** After pushing, the
  worktree removal is owned by exactly one actor, decided by context — **no
  instruction ever tells a session to remove the worktree it is standing
  in**. Who removes the tree, by flow:

  - **Plain leaf flow** — the session removes its own tree with
    `implement.py cleanup <work-item-id>` as its **last** bash command (see
    the [final cleanup step](#final-cleanup-step-remove-the-retained-worktree-plain-leaf-sessions)
    below), after the end-of-session report. After that command no bash works
    in the session (the cwd is gone by design).
  - **Driven child session** (`IMPLEMENT_DRIVE_ACTIVE=1`) — **do not remove
    your own worktree:** `implement.py drive` removes it once this session
    has exited (terminal child, clean tree only, SA-0MVTE8XAJ002IUDR).
  - **Parent/parent-phase flow** — `phase_parent` finishes each child in its
    own subprocess and removes that child's worktree there; no session-level
    cleanup applies and the parent's session never holds a child's cwd.
  - **Plain leaf without the flag (default)** — `finish` removes the tree
    immediately after a successful dev push (unchanged).

  **Use `implement.py cleanup <id>`, not a raw `git worktree remove`**
  (SA-0MVTHIZE0007EAKZ). The action is the reviewed removal path: it refuses
  the main checkout and any directory that is not a linked worktree, retains
  a dirty tree (uncommitted work is never discarded), refuses a
  non-terminal item, and reports `removed` / `already_absent` /
  `retained_*` in JSON (`--json`) or human lines. Re-running it is
  harmless (an already-absent tree is a reported no-op success).

  ```bash
  python3 $(skill_path implement)/scripts/implement.py cleanup <work-item-id> --json
  ```

  `finish` records the deferral in `report["steps"]["worktree_removal"]`
  (`deferred`, `reason`, `worktree_path`) and names the retained path and
  the exact cleanup follow-up (`steps.worktree_cleanup.command`) in its
  completion comment. See
  [docs/dev/implement-skill-reference.md](../../docs/dev/implement-skill-reference.md#worktree-removal-ownership-sa-0mvte8xaj002iudr)
  and [docs/dev/worktree-isolation.md](../../docs/dev/worktree-isolation.md#worktree-removal-ownership).

  > **Why rebuild?** `dist/` is gitignored; `git pull` does not update it. See [[concepts/git-worktree-best-practices-for-agent-workflows]].
- **The completion comment is posted by `finish` before the terminal write**
  (commit hash, branch, verdict provenance, retained tree), so no separate
  post-`finish` comment is required. A manual comment added after `finish`
  returns bumps `updatedAt` on an already-audit-ready item and can invalidate
  a fresh audit's freshness window — avoid it; if you must record context, do
  so before the audit is persisted.

  > **Ordering vs. audit persistence (SA-0MTHC710X003ORZM):** when closing an audited work item, do NOT add a post-audit comment *after* the audit has been persisted (`wl audit-set` / `persist_audit.py`). A `wl comment add` bumps `workItem.updatedAt` (via `touchWorkItemUpdatedAt()`); if that bump falls after `auditedAt` it can invalidate the runner's 60 s freshness gate and the TUI shows a stale icon (⏳) on a passed audit. Record any audit-result commentary **before** calling `persist_audit.py`, or rely on the persisted audit report itself as the record — the runner's `_apply_terminal_lifecycle` plus the audit skill's Persistence Procedure ordering contract already cover this (see the audit skill docs and `$(skill_path audit)/scripts/audit_runner.py:_check_audit_freshness` / `persist_audit.py:_run_audit_text_update`).
- Close your response with: `<work-item-id>: <concise-summary>\n\nWork committed to dev`

  > **Parent/epic items already advanced at Step 6.1:** skip the status update.
  > **Manual (leaf items, or parents not yet advanced):** mark `in_review` (do **NOT** close): `StatusLifecycle.update_status(<work-item-id>, "completed", stage="in_review")`. `finish` already performs this terminal write as its last lifecycle action, so run it manually only when `finish` was not used — never as a step *after* `finish` returns.

  > The item stays `in_review` until release promotes `dev` to `main` (see `../ship/SKILL.md`).

- **Read-only post-push verification (inside `finish`, before the terminal
  write — SA-0MVXUSK02003ZMDN):** `finish` verifies, WITHOUT executing a
  suite, that the full suite is green at the exact pushed commit. The dev-push
  pre-push hook ran it moments ago (full suite, `--scope full`) and cached it;
  the verification resolves the cache entry for the pushed tree
  (SA-0MVECQUGF0064U81). `green` or `not_applicable` permits the terminal
  write; `red` or `missing` **refuses** it — `finish` releases the item to
  `open`, retains (never reverts) the pushed commit, records the refusal on
  the work item and routes the failure to triage (SA-0MVXUSJ2P009T97H AC4).
  The session does **not** run this check again after `finish` returns.

  - **Tree-keyed lookup (SA-0MWAXC5TD00461FN).** The dev-push gate binds its
    verdict to the pushed commit's tree SHA with `fingerprint_kind="tree"`
    (SA-0MW7C6BMZ00931JG). The verification derives that same tree
    (`git rev-parse <commit>^{tree}`), queries the tree-keyed entry through
    `query_cached(..., fingerprint=<tree>, fingerprint_kind="tree")` in the
    pushed-commit checkout and then the main checkout's mirrored cache, and
    only falls back to the historical loose `HEAD`+dirty query (for legacy
    entries) on a tree miss. Querying the loose fingerprint alone can never
    see the gate's tree entry, so it would report a false `missing` on every
    successful push and auto-file a spurious
    `[test-failure] post-push full-suite verification` item.

  - **If `finish` refused on the verification**, generate the evidence through
    the test skill and re-run `finish` (idempotent — the no-diff path
    converges):
    `python3 $(skill_path test)/scripts/run_tests.py --scope full` then
    `python3 $(skill_path implement)/scripts/implement.py finish <work-item-id>`.
  - A genuinely failing suite → triage helper:
    `python3 $(skill_path triage)/scripts/check_or_create.py
    '{"test_name":"<name>", "stdout_excerpt":"...", "stack_trace":"...",
    "parent_work_item_id":"<this-work-item-id>"}'` → fix → re-run `finish`.
  - **Parent/epic runs:** parent/epic items are validated per child at each
    child's Step 9; the parent itself is covered by the per-child test runs
    at Step 6.1.

### Relocated: Finish lock, verified sync and refusal recovery

Finish lock, verified sync and refusal recovery
-----------------------------------------------

`finish`'s main-checkout critical section — commit → local `dev` fast-forward
→ push → ancestry assertion → read-only post-push verification → worktree
removal → post-push parent-branch sync → completion comment → terminal write —
is serialised by a
**per-repository exclusive advisory lock**
(`<main-checkout>/.worklog/locks/main-checkout.lock`,
`$(skill_path shared)/repo_lock.py`). `start`'s pre-worktree parent-branch refresh
takes the same lock (`_sync_parent_branch_locked`), so two runs can never
interleave moves of the shared local `dev` ref — the 2026-10-25 shape where
finish A's commit became reachable only from its feature branch while both
runs reported success.

- **Bounded wait, never a hang.** A second `finish` fails fast by default
  (`IMPLEMENT_FINISH_LOCK_TIMEOUT=0`); a positive value waits at most that many
  seconds and then refuses with the lock path, the holder (work item, pid,
  start time, repo, host) and the recovery step. `start` waits at most
  `IMPLEMENT_START_LOCK_TIMEOUT` (default 30 s) and then **skips** the refresh
  fail-open (`reason: "lock_held"`) — a start is never blocked by a finish.
  An indefinite wait is rejected outright, so no code path blocks forever.
- **Crash-safe.** Ownership is the kernel's `flock`, released when the holder
  dies — `SIGKILL`, an exception or a hard crash included. The metadata in the
  lock file is for the refusal message only and never decides ownership, so a
  **stale lock file cannot block a later finish**.
- **Verified fast-forward.** Local `dev` only ever advances with `git merge
  --ff-only` (its history is never rewritten) and the pre-advance tip is
  recorded. When local `dev` moved since the worktree base, the commit is not
  based on the current `origin/dev`, the fetch fails or the checkout cannot
  switch, the helper **refuses loudly** (`local_dev_diverged`,
  `commit_not_based_on_remote_tip`, `fetch_failed`, `checkout_failed`) instead
  of silently skipping — so an unsafe advance is never taken and the refusal
  is reported rather than swallowed.
- **Reversible on push failure.** A failed push restores local `dev` to
  `refs/remotes/origin/dev`: `git reset --merge` when `dev` is checked out
  (uncommitted work in the shared checkout survives; an overlapping local
  modification is reported, never clobbered — `reset --hard` is never used),
  otherwise an atomic compare-and-swap `git update-ref`. An unpushed commit is
  therefore never left as the next worktree's base.
- **Ancestry assertion (fails closed).** After a successful push, `finish`
  resolves the commit to its full hash and requires `git merge-base
  --is-ancestor <commit> refs/remotes/origin/dev`. A commit that did not land —
  including the rebased replacement hash produced by non-fast-forward recovery
  — raises `AncestryAssertionError`, so `finish` can never report success for
  an orphaned commit.
- **Observability.** `report["steps"]["main_checkout_lock"]`,
  `["local_dev_fast_forward"]`, `["local_dev_restore"]` and
  `["ancestry_assertion"]` record each decision. Full contract:
  [docs/dev/implement-skill-reference.md](../../docs/dev/implement-skill-reference.md#verified-reversible-local-dev-sync-and-post-push-ancestry-assertion-sa-0mvpzg7a0000n66u).

### When `finish` refuses

**Lock refused.** Nothing changed: the checkout, the work item and the branch
are untouched (no status write, no comment). Read the holder from the refusal
message, `report["steps"]["main_checkout_lock"]`, or
`<main-checkout>/.worklog/locks/main-checkout.lock`, and check it is alive
(`ps -p <pid>`). A dead holder's lock file does not block you: re-run `finish`
(or `start`) — there is nothing to clean up.

**A transient transport/auth failure (SA-0MW22SEMV00230O5 AC1).** `finish`
retries it automatically on a bounded budget with backoff and only refuses
when the budget is spent — and even then it says so plainly: the push step
reports `reason: transport` (infrastructure), never a bare "push failed" and
never the misleading "commit is local and can be pushed manually" hint (an
auth refusal means the connection never completed; nothing was rejected).
Re-running `finish` retries the same way — no manual work needed.

**A non-fast-forward rejection (AC2).** `finish` rebases onto `origin/dev`
and re-attempts within the bounded budget; only a dev that *keeps* advancing
past the budget refuses (the push step records `retries`/`budget`), and a
rebase conflict stops with an actionable message. The recovery is otherwise
manual only in the exhausted/conflicted case:

```bash
cd <retained worktree>
git fetch origin dev
git rebase origin/dev          # resolve conflicts if dev advanced
git push origin HEAD:refs/heads/dev
python3 $(skill_path implement)/scripts/implement.py finish <work-item-id>
```

**The commit already landed (`already_pushed`, AC3/AC5).** If the commit is
remote-verified (fetch first — never a stale remote-tracking ref) to already
be an ancestor of `origin/dev`, `finish` treats the push as satisfied, skips
the re-push and proceeds straight to the terminal transition — so an item
whose commit landed in an earlier run is completed by a plain re-run, and a
transport failure can never take a landed item back to `open`. If the
read-only full-suite cache cannot prove a green verdict for that already-landed
tree, the run proceeds with a recorded notice (the dev-push gate ran when the
commit landed; the audit re-verifies, F3); a *red* cached verdict still
refuses (`push_landed_but_unverified`).

**Ancestry assertion refused.** The worktree and the feature branch are
**retained**, the work item stays `open` (never `in_review`), and a comment
records the commit, the target ref, the reason, the branch and the worktree
path. The commit is complete and already published on its feature branch —
only its arrival on `origin/dev` failed (recorded `push_landed_but_unverified`
when the push claimed success but ancestry could not be proven). Publish it,
then re-run `finish`.

```bash
cd <retained worktree>
git fetch origin dev
git rebase origin/dev          # resolve conflicts if dev advanced
git push origin HEAD:refs/heads/dev
python3 $(skill_path implement)/scripts/implement.py finish <work-item-id>
```

If the push is rejected again, another commit landed in the meantime: rebase
and retry once more. Re-running `finish` on the retained worktree is
idempotent — it re-verifies commit → ancestry → status and marks the item
`in_review`.

**When a manual merge is needed.** Only in two cases. First, when the rebase
conflicts: resolve them, commit, push, and re-run `finish`. Second, when the
pre-push fast-forward reports `local_dev_diverged` because the **main
checkout's local `dev` carries commits `origin/dev` does not have**:

```bash
git -C <main-checkout> log --oneline origin/dev..dev   # what is local-only
```

Those commits are someone's unpublished work, so `finish` neither discards nor
merges them for you. If they are intended, publish them (push `dev`, or merge
them into `origin/dev`) and re-run `finish`; if they are stale, restore local
`dev` deliberately with `git -C <main-checkout> reset --merge origin/dev` —
`--merge`, never `--hard`, because the shared checkout may hold uncommitted
work from another session. `finish` has already restored its **own** advance
after the failed push, so this is the residual case of a pre-existing local
divergence.

**Runtime copy.** These behaviours live in the installed skill copy
(`~/.pi/agent/skills/`). The fix takes effect in subsequent sessions **only
after** `scripts/install_pi.sh` has refreshed the runtime skill copy, so re-run
it after pulling a change to the implement skill — then later sessions run the
fixed `implement.py` and read the updated skill text.

### Relocated: Pre-push blocking check

Pre-push blocking check
-----------------------

The dev-push pre-push hook enforces the full suite **deterministically** at
push time — a push to `dev`/`main` with a red full tree is refused (see
`.githooks/pre-push`). You therefore do **not** pre-run the full suite during
implementation: `implement.py finish`'s changed-scope validation is the
development-time gate, and the full suite first runs at the push (integration)
(SA-0MVECQUGF0064U81). If the hook reports failures on your push: (1) create
a critical `test-failure` work item with
`python3 $(skill_path triage)/scripts/check_or_create.py
'{"test_name":"<name>", "stdout_excerpt":"...", "stack_trace":"...",
"parent_work_item_id":"<this-work-item-id>"}'`; (2) fix the failures in the
worktree; (3) re-run `implement.py finish` until the push succeeds.

### Relocated: Implement state file — `repo_root` is always the main checkout

## Implement state file: `repo_root` is always the main checkout

`<worktree>/.implement_state.json` carries `repo_root`, and it always means the
**main checkout root** — never the worktree, never a path inside it. Every
consumer acts on the main checkout: the `dev` fast-forward in `finish`, the
parent-branch sync, the signal handler's `_restore_repo_state`,
and `_remove_worktree(..., repo_root=…)`, whose safety gate refuses a path that
resolves to `repo_root`. A worktree recorded as `repo_root` therefore makes
`finish` raise `REFUSE to remove the repository root` *after* a successful push
and leaves the item `open` (MS-0MVM7NJF70037YU4).

- `phase_start` resolves the value with `_get_repo_root()`, which returns the
  main checkout even when `start` is invoked (or resumed) from **inside** a
  linked worktree. Do not derive it from `Path.cwd()`.
- `finish` and `abort` do not blindly trust an existing value: a `repo_root`
  that is the worktree or inside it is stale. It is rejected (WARNING logs the
  rejected and derived values) and `_resolve_main_repo_root` re-derives the
  main checkout, so already-stranded worktrees finish without hand-edited JSON.
- `_remove_worktree`'s gates stay untouched — the fix supplies the correct root
  rather than relaxing the check.

Full rationale and the resume-from-worktree case:
[docs/dev/worktree-isolation.md](../../docs/dev/worktree-isolation.md#the-state-file-repo_root-is-the-main-checkout).

### Relocated: Dirty main-checkout recovery playbook

## Dirty main-checkout recovery playbook

When `implement.py start` detects a dirty main checkout or orphaned stashes,
follow this decision tree to resolve the issue **without touching the operator's**
uncommitted changes without explicit permission.

### Decision tree: dirty working tree

```
Dirty main checkout detected?
│
├─ Only .worklog/ changes?
│  └─ YES → Safe to proceed. Carry forward.
│
└─ Other uncommitted changes?
   │
   ├─ Are they your own WIP from this session?
   │  ├─ YES → Commit them to a temporary branch or stash (with permission),
   │  │          then create a worktree. Never stash without asking.
   │  └─ NO (someone else's) → STOP. Ask the operator.
   │
   └─ Do you know whose changes these are?
      ├─ YES → Coordinate with the author: commit, revert, or reset.
      └─ NO (stale/unidentified) → Report to operator; DO NOT delete or
                                        stash without explicit permission.
```

### Decision tree: orphaned stashes

```
Orphaned stash detected (no matching open work item)?
│
├─ Stash message contains a work-item ID (SA-XXXXXXX)?
│  ├─ YES → Check if that work item is in_review/completed:
│  │         └─ YES (stale) → Delete: git stash drop stash@{N}
│  │         └─ NO (in-progress elsewhere) → Leave it (another agent owns it)
│  │
│  └─ NO → Stash has no work-item reference:
│         ├─ Can you reconstruct what's in it? (check reflog)
│         │  ├─ YES → Restore: git stash apply stash@{N}, then create a
│         │  │           work item and commit via a worktree.
│         │  └─ NO → Flag for operator review; delete only with permission.
│         └─ Is it clearly a forgotten experiment or test?
│            └─ YES → Safe to delete after documenting in comments.
│
└─ Multiple orphaned stashes?
   └─ Run: scripts/hygiene_check.sh --json for a structured report.
```

### Recovery examples

**Restore an orphaned stash:**
```bash
git stash apply stash@{0}
git stash drop stash@{0}
```

**Delete a confirmed-stale stash:**
```bash
git stash drop stash@{0}
```

**Periodic hygiene check:**
```bash
scripts/hygiene_check.sh
cron: 0 */4 * * * cd /path/to/repo && scripts/hygiene_check.sh >> /var/log/hygiene.log 2>&1
```

### Key rules (always apply)

1. **Never stash, commit, or revert** another user's changes without explicit
   permission — this is the foundational invariant that caused the original
   incident (SA-0MSALRZ3B006FPI5).
2. **Never delete stashes** that reference an open work item — they may be
   needed by another agent.
3. **Always document** stash dispositions in work-item comments for audit trail.
4. **Run `scripts/hygiene_check.sh`** periodically to catch issues early.
### Relocated: Test Anti-Patterns

## Test Anti-Patterns

Review the shared [Test Writing Guidelines](../../skill/shared/test-writing-guidelines.md)
before writing tests (six anti-patterns from a full audit of the
Tableau-Card-Engine suite; 32 low-value files removed). Never write tests
that: (1) grep source instead of asserting behaviour, (2) contain
`expect(true).toBe(true)` or zero assertions, (3) re-implement production
logic, (4) duplicate an existing core test, (5) assert type-level
satisfaction the compiler checks, (6) boot a browser/scene without asserting
anything. Every test must assert observable behaviour via the public API.

### Relocated: Best Practices

## Best Practices

- Follow the steps in order; do not skip steps.
- **Testing is required — TDD preferred, not mandatory.** Write tests first
  whenever practical; test-after is permitted when TDD would complicate
  implementation. When external constraints prevent complete tests, create
  harnesses/mocks and document the limitation. **Do NOT write placeholder
  tests** — track unimplemented features in a work item instead
  ([Test Writing Guidelines](../../skill/shared/test-writing-guidelines.md)).
- No search tools (grep/ripgrep/code search) — rely on work-item context and
  linked docs; if insufficient, run intake interview.
- Keep implementation focused on meeting ACs with minimal changes; never edit
  code outside `src/`/`tests/`/`docs/` unless essential config; never edit
  bundled libraries (`dist/`, `node_modules/`).
- CLI/API work always provides JSON formatted output.
- Document process/decisions/next steps in work item comments; handle errors
  gracefully with actionable remediation.
- Not well-defined → intake interview; implement blockers/dependencies first.
- Follow AGENTS.md policies for branch naming, commit discipline, worktree workflow, and push-to-dev ([AGENTS_GLOBAL](../../AGENTS_GLOBAL.md#implement-the-work-item)); after `in_review`, use the cleanup skill to tidy local feature branches (not `dev`/`main`).
- Use `StatusLifecycle` for all status transitions — never ad-hoc `wl update --status` commands.

### Relocated: Handling Assets

## Handling Assets

- **Graphics/audio:** create in `assets/images/` or `assets/audio/` with a `placeholder_` prefix; reference in comments and commit; optimize size/performance; only use assets you have rights to distribute (attribute where required).
- **Documentation:** update relevant markdown in `docs/`; keep changes clear and accurate.
- **Exception:** `CHANGELOG.md` excluded — managed by the ship skill's release pipeline.

### Relocated: Step 4 — Understand the work item

The item is already claimed from Step 1. Fetch `wl show <work-item-id> --json`;
pay attention to `description`, `acceptance criteria`, `comments`. Restate
ACs/status; surface blockers/dependencies/missing requirements; inspect linked
PRDs/plans/docs; confirm expected tests/validation.

**Checking for rejected producer audits:** If the work item has been returned
from a producer audit (status not `in_review`/`completed`, or the agent
suspects a prior audit rejected it), fetch the audit record and any related
comments to understand **why** it was rejected:

```bash
wl audit-show <work-item-id> --json
wl comment list <work-item-id> --json
```

- The audit record's `rawOutput` field (under `audit.rawOutput`) contains
  the full audit report including the `Ready to close:` verdict and per-AC
  verdicts with evidence for any `unmet`/`partial` criteria.
- Comments may contain additional context from the producer or previous
  agent sessions.
- **Always use the most recent rejection reason.** If multiple audit records
  or comments reference rejections, compare timestamps (`audit.auditedAt` and
  `comment.createdAt`) and act on the latest. Earlier rejections may have
  already been addressed by subsequent fixes — do not act on stale rejection
  reasons.

4.1. Definition gate (must pass before implementation)

- Verify: clear scope (in/out-of-scope); concrete, testable ACs; constraints and
  compatibility expectations; unknowns captured as explicit questions.
- **Risk/effort gate:** for `plan_complete` items missing `risk` or `effort`, the
  skill automatically runs the effort-and-risk evaluation and persists estimates
  via `wl update` — it does NOT fail the gate (SA-0MTTSWHQE0072N9J).  Items with
  both fields set proceed normally.  If evaluation fails, a warning is logged but
  the run proceeds.

If the gate fails (definition issues only, not missing estimates): (1)
`StatusLifecycle.update_status(<work-item-id>, "open")`; (2) not well-defined →
intake interview (`../intake/SKILL.md`); too large → plan interview
(`/skill:plan`); (3) inform the user and ask whether to restart.

**Producer review:** When the agent cannot proceed because the work item is
ill-defined (unclear scope, untestable ACs, missing constraints) and needs
producer input to resolve, mark the work item as needing producer review:

```bash
wl reviewed <work-item-id> true
```

This flags the item so the producer knows the work item needs clarification
before implementation can proceed. The agent should STOP and wait for the
producer's response.

**Missing risk/effort estimates.** When a `plan_complete` item is missing
`risk` and/or `effort` fields, the implement skill runs the
[effort-and-risk skill](../../skill/effort-and-risk/SKILL.md) automatically to produce
and persist estimates via `wl update --risk/--effort`.  Items with both fields
already set proceed normally — no gate error, no behaviour change for items
that are already sized.  The evaluation runs before the worktree is created
(Step 6.1) so the dispatcher can re-classify the item as implement-dispatchable
after estimates are persisted.  If the evaluation fails (orchestrator not
found, subprocess error, timeout), the skill logs a warning and proceeds
without estimates — it does **not** block the run (SA-0MTTSWHQE0072N9J).

4.2. Detect "already implemented" and close gaps (if applicable)

Before creating a worktree, check whether the work item has **already been
implemented** but is stuck in a wrong state/stage. Detection signals include:

- The implement skill has previously reported the work as completed with a
  commit hash (the skill's own output — not inferred from status alone).
- The item's status/stage is inconsistent (e.g. `in_progress` with a commit
  already on `dev`, or `completed` without `in_review`).
- A recent audit report indicates prior completion but unmet ACs or gaps.

If **any** detection signal applies:

1. Run the audit to get a current picture:
   `/skill:audit <work-item-id>` (reuse a recent audit if one exists from
   the same session; the most recent audit report may predate later fixes).
2. Review the audit report (`audit_report_<id>.md`) for gaps: unmet ACs,
   failing tests, missed requirements.
3. **If gaps exist:** fix them inline as part of the current item's
   implementation — write tests, code, or docs as needed. Do **NOT** create
   new work items for gaps (they are closed inline per the intake decision).
   Large gaps that exceed the scope of minimal remediation → record as a
   `discovered-from:<work-item-id>` work item instead.
4. **If the audit is clean** (no gaps): report the summary and skip further
   implementation — the item is done, just needs a status/stage update.
   Proceed directly to Step 9 (Commit, Push to dev and mark in_review).

If **no** detection signal applies (the item genuinely needs implementation):
proceed to Step 4.3.

4.3. Detect duplicate work items (if applicable)

Before creating a worktree, check whether an **overlapping completed work item**
exists that may already cover this item's scope. This is distinct from
Step 4.2 (which detects work *already implemented in the repository* by a prior
session). Step 4.3 detects work items that describe overlapping or duplicate
scope.

1. **Search:** `wl search <keywords> --json` using 3–6 keywords from the work
   item's title and description. Also try `wl list --status completed --json`
   for completed items with matching tags.
2. **Review:** For each candidate, read its description and acceptance criteria.
3. **Compare ACs:** Are all acceptance criteria of this item fully satisfied by
   the existing item?
   - **Yes (full duplicate):** Close the current item. Comment: `"Duplicate of
     <existing-item-id>: all acceptance criteria are met by that item. Closing
     to avoid redundant work."` Then:
     `wl close <id> --reason "Duplicate of <existing-item-id>: work already
     completed" --json`
   - **No (partial overlap):** Rewrite the description and ACs to address only
     the differences/gaps. Comment: `"Rewritten to address differences from
     <existing-item-id>: <summary of differences>."` Link as related work.
   - **No (no overlap found):** Proceed normally. Note:
     `"Duplicate check performed — no overlapping completed item found."`
4. **Idempotence:** Re-running produces the same result; do not re-close or
   re-rewrite an already-closed item.

After this step (or after closing/rewriting), proceed to Step 5.

### Relocated: Final cleanup (belt-and-suspenders)

Final cleanup (belt-and-suspenders)
---------------------------------------

Before exiting at any point, `wl show <work-item-id> --json`; if `status: in_progress` and work is incomplete (not at Step 9), reset via `StatusLifecycle.update_status(work_item_id, "open")` to prevent orphaned `in_progress` items blocking other agents.

### Relocated: Status Transition Matrix

## Status Transition Matrix

| Phase | Mechanism | Status | Stage |
|-------|-----------|--------|-------|
| Claim (Step 1) | `update_status(id, "in_progress", assignee="<AGENT>")` / `phase_start()` (status-only; stage unchanged) | in_progress | unchanged |
| Epic/parent all children done (5.1) | `update_status(id, "completed", stage="in_review", assignee="")` | completed | in_review |
| Final (Step 9) | `with StatusLifecycle(id, assignee="<AGENT>", target_stage="in_review"):` / `phase_finish()` (release is the helper's default). The terminal write is the run's **last lifecycle action**: the read-only post-push verification, the completion comment and the (non-deferred) worktree removal precede it (SA-0MVXUSK02003ZMDN) | completed | in_review |
| Abort (dirty/gate/user/error/termination) | `update_status(id, "open", assignee="")` via `phase_abort()` (error: context manager restores original; termination: final cleanup resets) | open | unchanged |
| Interrupted after the commit/push (signal or failure before the terminal write) | `_safety_reset_if_in_progress()` / the failure handler's `update_status(id, "open", assignee="")`, plus one deduplicated `Run interrupted:` record naming the commit and resume command (SA-0MVXUSJJY009HMQ0) | open | unchanged |

> **All abort/failure transitions reset to `open` and clear the assignee.** Never leave a work item in `in_progress` unless actively implementing; never leave a completed or released item on an assignee hold.

### Relocated: Scripts (canonical runner & modules)

## Scripts (canonical runner & modules)

This skill does not ship a single orchestrator script — implementation follows
the steps above and invokes project-local build/test and linters. When a
repository provides an "implement" helper script, prefer it for deterministic
behavior.

Build/test steps for repos without build/test tooling:
[docs/dev/implement-skill-reference.md](../../docs/dev/implement-skill-reference.md).

Example commands (documentation example, SA-0MPYMFZXO0004ZU4):

```bash
wl show SA-0MPYMFZXO0004ZU4 --json
git push origin HEAD:refs/heads/dev   # ship.js pushToDev preferred
python3 -c "from skill.shared.status_lifecycle import StatusLifecycle; StatusLifecycle.update_status('SA-0MPYMFZXO0004ZU4', 'completed', stage='in_review')"
```

### Relocated: Final step — standardized end-of-session report

## Final step: standardized end-of-session report

Render the canonical end-of-session report (helper: [`../report/SKILL.md`](../../skill/report/SKILL.md)) as the **last step**, replacing any ad-hoc end-of-session summary:

```bash
python3 $(skill_path report)/scripts/render_report.py <work-item-id> \
  --skill-name <skill_name> \
  --headline "<1-3 sentence headline summary>" \
  --ac "<AC# description>|<verification metric>|met" \
  --ac "<...>|<...>|unmet" \
  [--producer-actions "<producer actions needed to complete the CURRENT stage; blocking only — omit for 'None needed'>"] \
  [--notes "<freeform context/caveats/assumptions>"] \
  [--next-action <review|plan|implement|...>]
```

For plain leaf sessions the report render is followed by the
[final cleanup step](#final-cleanup-step-remove-the-retained-worktree-plain-leaf-sessions) — the session's **last** bash command (see Step 9).

The script prints the rendered report to stdout — **paste it verbatim into
your final response**, so the operator sees the report itself (not just the
tool call), then close with: `<work-item-id>: <one-line summary>`. Do NOT
re-summarize the report in a different format — the report is the summary. When the session ends in a terminal state with no open questions for the operator, end your final response with `</end_session>` on its own line as the very last line after the summary; if the session ends with questions for the operator, do not emit the marker.

### Relocated: Final cleanup step — remove the retained worktree (plain leaf sessions)

## Final cleanup step: remove the retained worktree (plain leaf sessions)

A plain leaf session that ran `implement.py finish <work-item-id> --keep-worktree`
(or exported `IMPLEMENT_KEEP_WORKTREE=1`) owns the removal of its own
worktree — no one else will do it. Run it after the end-of-session report
above, as the session's **very last** bash command:

```bash
python3 $(skill_path implement)/scripts/implement.py cleanup <work-item-id> --json
```

This must be the session's **last** bash command: `cleanup` deletes the tree
this session stands in (its cwd), so every later bash call would fail with
`Working directory does not exist` — which is why the report comes **first**.
After `cleanup` returns you may still render the final response text (that is
not a bash command), but you cannot run another command. The post-push
verification is not repeated here: it already ran inside `finish`, before the
terminal write (SA-0MVXUSK02003ZMDN).

**When `cleanup` reports `retained_dirty` or `retained_non_terminal`:** the
tree was kept on purpose — never force it (the action has no force switch).
Record the retention in a work-item comment (state the `reason` and `remedy`
from the report) and stop; a dirty or non-terminal tree is retained as a
reported outcome, and re-running `cleanup` later is safe after the tree is
clean and the item is terminal (`already_absent` is a reported no-op
success).

- **Driven child sessions** (`IMPLEMENT_DRIVE_ACTIVE=1`) — do **not** run
  `cleanup` here: `implement.py drive` removes the worktree after this
  session has exited (terminal child, clean tree only, SA-0MVTE8XAJ002IUDR).
- **Parent/parent-phase flows** — do **not** run `cleanup` here:
  `phase_parent` finishes each child in its own subprocess and removes that
  child's worktree there.

See [Worktree removal
ownership](#worktree-removal-ownership-sa-0mvte8xaj002iudr) in Step 9 and
[docs/dev/implement-skill-reference.md](../../docs/dev/implement-skill-reference.md#the-session-side-removal-action-implementpy-cleanup-id)
for the full contract.
