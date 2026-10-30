---
name: implement
description: "Write tests, docs, and code for a Worklog item via a deterministic workflow. Use when: 'Implement <id>'."
---

## Purpose

Deterministic, step-by-step workflow for completing a Worklog work item
through code, tests, and docs.

## Inputs

- work-item id: required; validate `<prefix>-<hash>`, prompt if missing.
- Optional freeform guidance in the arguments may shape the approach.

## Outputs

- Tests and implementation code meeting ACs, committed and pushed to `dev`.
- Work item updated to `in_review` (NOT closed; stays open until release).

## References to Bundled Resources

- Intake/interview helpers: `intake`, `plan`, `interview`.

Security note — scope: this restriction applies to **protected branches**
(`main`/`master`/`HEAD`) and to **creating PRs**: do not push to them or open
PRs automatically without explicit operator permission (no operator-approved
credential exists for those actions). Pushing the feature branch into `dev`
(Step 9) is **pre-authorized by this workflow** — the repo's pre-push hook
enforces the same policy, blocking `main`/`master`/`HEAD` only — and requires
no additional approval, provided the build passes and the test gate is green:
`implement.py finish` validates the worktree with **changed scope only**
(tests affected by the change — fast iteration). The **full suite first runs
at the dev-push integration gate**, not during implementation: the
**pre-push hook** executes `run_tests.py --scope full` on the actual push to
`dev`/`main`, and the **ship release gate** re-runs the full suite on the
merged dev→main tree before a release (SA-0MVECQUGF0064U81,
SA-0MT6BYQHB008DOGC). The pushed-commit full run populates the per-repo test
cache that post-push verification and audits consume read-only. Changed-scope
selection ignores deleted/non-existent test paths and falls back to the full
suite when nothing selectable remains; a scoped pytest exit 4 ("file or
directory not found") is likewise treated as selection unavailable and falls
back to full scope, while a genuine failure (exit 1) still blocks the gate
(LP-0MTZYRTNF0092JKW).
When in doubt, produce the exact
`git`/`gh`/`wl` commands for a human to run.

Privacy note: Avoid secrets/tokens/PII in comments or PR bodies — reference by
work-item id or document path; mask/redact sensitive values before writing to
logs or comments.

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
the assignee when it ends, regardless of how it ends (assignee-hold
convention: non-empty assignee = scheduling hold; clear to release):

- successful `finish` clears the assignee together with the
  `completed`/`in_review` transition (`clear_assignee_on_exit=True`)
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

## Test Anti-Patterns

Review the shared [Test Writing Guidelines](../shared/test-writing-guidelines.md)
before writing tests (six anti-patterns from a full audit of the
Tableau-Card-Engine suite; 32 low-value files removed). Never write tests
that: (1) grep source instead of asserting behaviour, (2) contain
`expect(true).toBe(true)` or zero assertions, (3) re-implement production
logic, (4) duplicate an existing core test, (5) assert type-level
satisfaction the compiler checks, (6) boot a browser/scene without asserting
anything. Every test must assert observable behaviour via the public API.

## Best Practices

- Follow the steps in order; do not skip steps.
- **Testing is required — TDD preferred, not mandatory.** Write tests first
  whenever practical; test-after is permitted when TDD would complicate
  implementation. When external constraints prevent complete tests, create
  harnesses/mocks and document the limitation. **Do NOT write placeholder
  tests** — track unimplemented features in a work item instead
  ([Test Writing Guidelines](../shared/test-writing-guidelines.md)).
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

## Background Job Etiquette

The canonical guidance for running and waiting on long-running background
processes lives in the shared doc
[`../shared/background-job-etiquette.md`](../shared/background-job-etiquette.md)
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

Recovery: push manually from the main checkout:

```bash
git push origin <branch>:refs/heads/dev
```

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

## Handling Assets

- **Graphics/audio:** create in `assets/images/` or `assets/audio/` with a `placeholder_` prefix; reference in comments and commit; optimize size/performance; only use assets you have rights to distribute (attribute where required).
- **Documentation:** update relevant markdown in `docs/`; keep changes clear and accurate.
- **Exception:** `CHANGELOG.md` excluded — managed by the ship skill's release pipeline.

## Steps

Execute the following steps in order. Do not skip steps. Use the live commands where applicable and record outputs in the work-item comments as you proceed.

1. Set status and safety gate

- **Before any other step**, claim the work item with a **status-only** update:
  `StatusLifecycle.update_status(<work-item-id>, "in_progress", assignee="<AGENT>")` (or `implement.py start`).
  Do **not** pass `stage="in_progress"` — `in_progress` is a status, not a stage;
  `wl` applies status and stage atomically and would reject the whole update. The
  claim leaves the existing stage unchanged (`in_progress` is never a stage).

> **Code Freeze gate:** `implement.py start <id>` checks the Code Freeze marker
> (`.worklog/code-freeze.json`, contract WL-0MSBU4KMA004PKSR) **before**
> claiming; if a release is in progress it refuses ("Project is in Code Freeze —
> implementation blocked until the release completes"), exits non-zero, and
> does **not** change the item status. No `--force` bypass; fail-open: a
> missing/corrupt marker never blocks.

2. Safety gate: handle dirty working tree

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

3. Stash hygiene gate (warn on orphaned stashes)

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

4. Understand the work item

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
[effort-and-risk skill](../effort-and-risk/SKILL.md) automatically to produce
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

5. Create a worktree from dev and branch inside it

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

> **`node_modules` is auto-symlinked:** `implement.py start` creates
> `<worktree>/node_modules -> <repo-root>/node_modules` when the main checkout
> has one (SA-0MSGS763C006SM1B). It also symlinks **nested** `node_modules`
> directories: every `<pkg>/node_modules` found in the main checkout whose
> parent package directory already exists in the worktree gets a matching
> symlink (`<worktree>/<pkg>/node_modules -> <main-checkout>/<pkg>/node_modules`),
> so workspace/monorepo packages and pi packages resolve their dependencies in
> the worktree (OSL-0MUFG3PJA00379CT). Discovery is bounded — it prunes `.git`
> and `.worklog` and never descends into a `node_modules` tree. Existing
> entries are never overwritten and a missing parent package directory is never
> fabricated. **Do NOT run `npm install` inside a worktree** — writes pass
> through the symlink, corrupting the shared tree. A branch that changes
> `package.json` still resolves the main checkout's dependencies; reinstall in
> the main checkout if that matters.

> **Git submodules are auto-initialised:** `implement.py start` runs
> ``git submodule update --init --recursive`` inside the new worktree (SA-0MSN52GGN002B0AZ).
> Failures produce a ``WARNING`` log message but **do not abort** — the worktree
> remains usable (best-effort). Repos without ``.gitmodules`` are unaffected.

See [AGENTS_GLOBAL](../../AGENTS_GLOBAL.md#implement-the-work-item).

6. Implement

- Open/in_progress blockers or dependencies → implement them first (recursively via this procedure).

6.1. Parent recursion (epic/parent items only)

**Preferred: drive every child in one invocation.** Run:

```bash
python3 $(skill_path implement)/scripts/implement.py drive <parent-id>
```

`phase_drive()` loops over `phase_parent` reports and, for every startable
child, spawns a **fresh Pi session** (`pi -p "/skill:implement <child>"`) in
that child's own worktree, waits for it to exit, then continues. A single
`drive` call therefore completes every child and advances the parent, while
each child keeps its own clean context window and its own worktree. Options:
`--json` (machine-readable per-child report), `--max-child-sessions N`
(default 1) to bound retries, `--finish-attempt-limit N` (default: the
`--max-child-sessions` value) to bound failing `finish` retries,
`--child-timeout S` (default 3600),
`--finalise-stranded` (opt-in, evidence-gated finalisation of stranded
children — see below). When no Pi executable is available it stops with
actionable manual instructions rather than silently skipping a child
(`IMPLEMENT_DRIVE_PI_BIN` overrides the binary). A driven session is marked
`IMPLEMENT_DRIVE_ACTIVE=1` and refuses to re-enter `drive`.

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
| `reset-for-retry` | The child was non-terminal after its finish-attempt bound; its worktree was reset to the parent-branch HEAD and its status set back to `open` for a fresh session. |
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
the epic. `phase_drive` now (1) counts failing finish executions per child and
retries only up to `--finish-attempt-limit` (default
`--max-child-sessions`); (2) re-fetches the child with `wl show` and records
`terminal-reconciled` when it reached a terminal state anyway; (3) otherwise
resets the child's **own** worktree to the parent-branch HEAD (`git reset
--hard <parent_branch>` + `git clean -fd`, isolation-gated) and its status to
`open` via `StatusLifecycle`, with an audit comment naming the failure and the
reset, then continues with a fresh session (`reset-for-retry`, at most once
per child per invocation). `parent_advanced: false` therefore always means
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
orphaned `in_progress` item and no worktree are left behind.

Then implement that child by recursing into this procedure (steps 1–8), and
re-run `implement.py parent <parent-id>`. The parent phase **finishes the
previous child for you** (`phase_finish` in its own subprocess) and starts
the next one — do **not** call `implement.py finish` separately. Repeat until
the parent reports all children terminal. Each child is implemented in its
own worktree (never the main checkout); sequential children reuse/rotate the
`.worklog/worktrees` machinery. (With `drive` this loop is performed for you;
the driven child session may call `finish` for its own item — `start` resumes
the pre-created worktree idempotently.)

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
- **Cross-project edges are coordination notes** — an edge to another project
  (different id prefix) never gates; it is reported in
  `coordination_blockers` (parent/leaf report) or `external_blockers`
  (per-child classification).
- **Terminal children are never re-implemented** (skipped, reported).
- **`do-not-delegate` children are never auto-started** — a child tagged
  `do-not-delegate` (matched case- and whitespace-insensitively; a missing or
  `None` `tags` field is simply unguarded) classifies as
  `skip-do-not-delegate`. `phase_parent` neither claims it, nor creates a
  worktree for it, nor spawns a session for it; it records
  `action: "skipped-do-not-delegate"` in `children_processed` and lists the
  ids in the additive `guarded_children` report field, then continues with
  the next startable sibling. Guarded ≠ terminal: the child still counts as
  pending, so the parent is **never** advanced past it, and when only guarded
  children remain the waiting message names them and the tag (never the
  dependency-blocked wording) and no `next_child` is reported — a re-run is
  idempotent (no claim, no worktree, no status change, same report).
  `phase_drive` spawns no session for such a child and forwards
  `guarded_children`. This mirrors `wl delegate`'s own guard rail (it refuses
  a tagged item without `--force`); naming the child explicitly
  (`implement.py start|finish|drive <child>`) is the documented override and
  is unaffected — only automatic selection inside parent recursion is gated.
  Terminal and in-progress tagged children keep their previous
  classification, so an operator's manual work is still finished, never
  re-started (SA-0MUWNKUIU007N876).
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
- **Blocked children wait** — a child is only started once every in-chain
  blocker is terminal; otherwise it is reported in `blocked_children`. The
  same applies to a same-project blocker outside the epic (reported with its
  `blocked_by` payload) — cross-project blockers stay coordination notes and
  never gate.
- **No premature parent advance** — if any non-terminal child remains
  (in-progress elsewhere, guarded by `do-not-delegate`, or blocked), the
  parent is not advanced; the phase reports waiting instead.
- **Abort/failure** in a child resets THAT child to `open` (StatusLifecycle
  abort semantics) and stops the chain with a report of what completed and
  what failed; already-completed siblings are not regressed.
- **No orphaned `in_progress`** — a child start failure or abort leaves no
  in-progress state behind; re-run the parent phase after resolving the
  blocker to continue the chain.
- **Worktree placement guard** — a non-terminal child whose worktree is clean
  at the parent-HEAD while the main checkout is dirty outside `.worklog/` has
  written to the main checkout; the phase fails closed with the offending
  paths (and does not advance the parent) instead of silently proceeding.
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
- Once all ACs are met: **build** (no errors); **validate via the [test skill](../test/SKILL.md) — the loop is scope-aware (SA-0MT6BYQHB008DOGC)**: `implement.py finish` runs a **changed-scope** validation (only the tests affected by this change — fast iteration). No full suite runs here: it is **deferred to the dev-push integration gate**, where the **pre-push hook enforces the full suite** (`--scope full`) on the push to `dev`, and the **ship release gate** re-runs it on the merged dev→main tree (SA-0MVECQUGF0064U81). The changed-scope execution delegates to the test skill's canonical `guarded_run_all` (the single source of truth for the outer live-repo guard, SA-0MUH1MJL6003767Q), so a green gate always implies the checkout was verified for mutation regardless of which entry point ran it. `run_tests.py` execution must go through the test-skill runner (`/skill:test`, `run_tests.py --scope full`): only the test-skill runner records runs in the per-repo test cache (keyed by git state + scope, 2h TTL) that the audit skill reads read-only via `query_cached()` to auto-verify execution-dependent ACs — an ad-hoc run (`npx vitest run`, `pytest`, …) is invisible to the audit, so the audit either auto-executes the suite itself on a cache miss (F3, SA-0MSTN5KRF0097TVP) or the operator attests with `--green-run HEAD` (it never hard-blocks — F4, SA-0MSTN8CWM003AAU9). If a gate reports full-suite failures: triage helper (`python3 $(skill_path triage)/scripts/check_or_create.py '{"test_name":"<name>", "stdout_excerpt":"...", "stack_trace":"...", "parent_work_item_id":"<this-work-item-id>"}'`); implement returned critical issues, re-run until green. Update docs (except `CHANGELOG.md`); summarize changes.

7. Automated self-review

- Build and lint; fix any issues.
- Sequential passes: completeness, dependencies & safety, scope & regression,
  tests & acceptance, polish & handoff. Small, goal-aligned edits; intent
  changes → Open Question and stop.

8. Optional refactor step

Before final commit, an automated refactor step may detect/remediate code smells (files modified this session; linters for mechanical issues + LLM for design smells). **Session-introduced smells fixed immediately; pre-existing smells create Worklog items with REFACTOR comments.** Skip with ``--no-refactor``:

```bash
python3 $(skill_path refactor)/scripts/refactor.py <work-item-id>
```

See ``../refactor/SKILL.md``.

9. Commit, Push to dev and mark in_review

- Follow the mandatory build → test → commit order before committing.
- **Do NOT create a Pull Request to `main`** — work is integrated into `dev`; the `dev`→`main` promotion is handled by the release process.
- Push the feature branch into `dev` via the ship skill (`pushToDev()` from `$(skill_path ship)/scripts/ship.js`, preferred) or `git push origin HEAD:refs/heads/dev`. `dev` is **not** protected; only `main`, `master`, `HEAD` are blocked.

  > **Pushing from a worktree:** the repo's pre-push hook runs `wl sync`, which
  > refuses to run from a worktree (worktrees have no local `.worklog`; the
  > data lives in the main checkout). First run `wl sync` from the main
  > checkout, then push from the worktree with the hook's documented bypass:
  > `WORKLOG_SKIP_PRE_PUSH=1 git push origin HEAD:refs/heads/dev`. Nothing is
  > lost by skipping the sync at push time — the main checkout syncs the data
  > on its own pushes.

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
- After pushing, clean up the worktree:

  ```bash
  git worktree remove .worklog/worktrees/wl-<WIP-id>-<short-slug>
  git worktree prune
  git checkout dev && git pull origin dev
  npm run build 2>/dev/null || echo "No build script, skipping rebuild"
  ```

  > **Why rebuild?** `dist/` is gitignored; `git pull` does not update it. See [[concepts/git-worktree-best-practices-for-agent-workflows]].
- Add a work-item comment with the commit hash: `wl comment add <work-item-id> --comment "Completed work pushed to dev, see commit <hash>." --author "<AGENT>" --json`

  > **Ordering vs. audit persistence (SA-0MTHC710X003ORZM):** when closing an audited work item, do NOT add a post-audit comment *after* the audit has been persisted (`wl audit-set` / `persist_audit.py`). A `wl comment add` bumps `workItem.updatedAt` (via `touchWorkItemUpdatedAt()`); if that bump falls after `auditedAt` it can invalidate the runner's 60 s freshness gate and the TUI shows a stale icon (⏳) on a passed audit. Record any audit-result commentary **before** calling `persist_audit.py`, or rely on the persisted audit report itself as the record — the runner's `_apply_terminal_lifecycle` plus the audit skill's Persistence Procedure ordering contract already cover this (see the audit skill docs and `$(skill_path audit)/scripts/audit_runner.py:_check_audit_freshness` / `persist_audit.py:_run_audit_text_update`).
- Close your response with: `<work-item-id>: <concise-summary>\n\nWork committed to dev`

  > **Parent/epic items already advanced at Step 6.1:** skip the status update.
  > **Manual (leaf items, or parents not yet advanced):** mark `in_review` (do **NOT** close): `StatusLifecycle.update_status(<work-item-id>, "completed", stage="in_review")`

  > The item stays `in_review` until release promotes `dev` to `main` (see `../ship/SKILL.md`).

- **Read-only post-push verification (belt-and-suspenders, post-push,
  post-`in_review`):** verify, WITHOUT executing a suite, that the full
  suite is green at the exact pushed commit. The dev-push pre-push hook ran
  it moments ago (full suite, `--scope full`) and cached it in the
  main-checkout `.worklog/cache`; this step asserts that green entry exists
  (SA-0MVECQUGF0064U81):

  ```bash
  python3 $(skill_path test)/scripts/run_tests.py --summary --suite all --json
  ```

  - A `missing` result means no cached full-suite entry exists for HEAD
    (e.g. TTL expired or the push bypassed the hook) — then, and only then,
    run the suite to generate the evidence:
    `python3 $(skill_path test)/scripts/run_tests.py --scope full --json`
    (or `/skill:test`).
  - A non-green summary (cached failures) or a genuinely failing run →
    triage helper:
    `python3 $(skill_path triage)/scripts/check_or_create.py
    '{"test_name":"<name>", "stdout_excerpt":"...", "stack_trace":"...",
    "parent_work_item_id":"<this-work-item-id>"}'` → fix → re-run.
  - All green → close your response.
  - **Parent/epic runs:** parent/epic items are validated per child at each
    child's Step 9; the parent itself is covered by the per-child test runs
    at Step 6.1.

Finish lock, verified sync and refusal recovery
-----------------------------------------------

`finish`'s main-checkout critical section — commit → local `dev` fast-forward
→ push → worktree removal → post-push parent-branch sync — is serialised by a
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

**Ancestry assertion or non-fast-forward recovery refused.** The worktree and
the feature branch are **retained**, the work item stays `open` (never
`in_review`), and a comment records the commit, the target ref, the reason,
the branch and the worktree path. The commit is complete and already
published on its feature branch — only its arrival on `origin/dev` failed.
Publish it, then re-run `finish`:

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

Final cleanup (belt-and-suspenders)
---------------------------------------

Before exiting at any point, `wl show <work-item-id> --json`; if `status: in_progress` and work is incomplete (not at Step 9), reset via `StatusLifecycle.update_status(work_item_id, "open")` to prevent orphaned `in_progress` items blocking other agents.

## Status Transition Matrix

| Phase | Mechanism | Status | Stage |
|-------|-----------|--------|-------|
| Claim (Step 1) | `update_status(id, "in_progress", assignee="<AGENT>")` / `phase_start()` (status-only; stage unchanged) | in_progress | unchanged |
| Epic/parent all children done (5.1) | `update_status(id, "completed", stage="in_review", assignee="")` | completed | in_review |
| Final (Step 9) | `with StatusLifecycle(id, target_stage="in_review", clear_assignee_on_exit=True):` / `phase_finish()` | completed | in_review |
| Abort (dirty/gate/user/error/termination) | `update_status(id, "open", assignee="")` via `phase_abort()` (error: context manager restores original; termination: final cleanup resets) | open | unchanged |

> **All abort/failure transitions reset to `open` and clear the assignee.** Never leave a work item in `in_progress` unless actively implementing; never leave a completed or released item on an assignee hold.

## Implement state file: `repo_root` is always the main checkout

`<worktree>/.implement_state.json` carries `repo_root`, and it always means the
**main checkout root** — never the worktree, never a path inside it. Every
consumer acts on the main checkout: the `dev` fast-forward and push in
`finish`, the parent-branch sync, the signal handler's `_restore_repo_state`,
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


## Final step: standardized end-of-session report

Render the canonical end-of-session report (helper: [`../report/SKILL.md`](../report/SKILL.md)) as the **last step**, replacing any ad-hoc end-of-session summary:

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

The script prints the rendered report to stdout — **paste it verbatim into
your final response**, so the operator sees the report itself (not just the
tool call), then close with: `<work-item-id>: <one-line summary>`. Do NOT
re-summarize the report in a different format — the report is the summary. When the session ends in a terminal state with no open questions for the operator, end your final response with `</end_session>` on its own line as the very last line after the summary; if the session ends with questions for the operator, do not emit the marker.
