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
`implement.py finish` validates the worktree with **changed scope** (tests
affected by the change — fast iteration), then runs a **final `--scope full`
gate** before commit, and the **pre-push hook re-runs the full suite**
(`--scope full`) on the actual push to `dev`/`main` (SA-0MT6BYQHB008DOGC).
Changed-scope selection ignores deleted/non-existent test paths and falls
back to the full suite when nothing selectable remains; a scoped pytest
exit 4 ("file or directory not found") is likewise treated as selection
unavailable and falls back to full scope, while a genuine failure (exit 1)
still blocks the gate (LP-0MTZYRTNF0092JKW).
When in doubt, produce the exact
`git`/`gh`/`wl` commands for a human to run.

Privacy note: Avoid secrets/tokens/PII in comments or PR bodies — reference by
work-item id or document path; mask/redact sensitive values before writing to
logs or comments.

## StatusLifecycle Integration

Status transitions are managed by the shared `StatusLifecycle` context manager
from `../shared/status_lifecycle.py`. The orchestration script (`implement.py`)
uses it automatically: `phase_start()` claims (`in_progress`); `phase_finish()`
wraps build/test/commit/push in `with StatusLifecycle(..., target_stage="in_review"):`
(success → `completed`/`in_review`; error → original status restored);
`phase_abort()` resets to `open` (also `implement.py abort <WIP-id>`). Manual
use: `StatusLifecycle.update_status()` or the context-manager pattern in
`../shared/status_lifecycle.py`.

**Invariant (SA-0MTFTFUIH000UWM9): actively worked => `in_progress`.** No
`wl` mutation (description/comment/child creation) while `status: open`. Guard
with `StatusLifecycle.require_claimed(<id>)` before any mutation; on any
in-session resume, re-claim first via `StatusLifecycle.ensure_claimed(<id>)`
(idempotent).

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

## Test Timeout Configuration

The finish step runs the project test suite through `implement.py finish` with a
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
proceed to Step 5.

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
(default 1) to bound retries, `--child-timeout S` (default 3600). When no Pi
executable is available it stops with actionable manual instructions rather
than silently skipping a child (`IMPLEMENT_DRIVE_PI_BIN` overrides the
binary). A driven session is marked `IMPLEMENT_DRIVE_ACTIVE=1` and refuses to
re-enter `drive`.

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

This mirrors the "Epic/parent items — one session per child" guidance in
`AGENTS_GLOBAL.md`.

Guards (deterministic, in `phase_parent`):

- **Dependency order** — a child `blocked` by another item is implemented
  only after its blockers; the chain is resolved dependency-order correct.
- **Terminal children are never re-implemented** (skipped, reported).
- **In-progress by another agent** (no worktree) → skipped and reported,
  never clobbered. An in-progress child *we* started (worktree exists) is
  finished when its worktree has changes, or returned to the agent for
  implementation when it does not.
- **Completed children are never restarted** — a finished child joins
  `children_processed` and the loop moves on; already-completed siblings are
  never regressed.
- **Cycles fail fast** with a clear error (no infinite recursion).
- **Blocked children wait** — a child is only started once every in-chain
  blocker is terminal; otherwise it is reported in `blocked_children`.
- **No premature parent advance** — if any non-terminal child remains
  (in-progress elsewhere or blocked), the parent is not advanced; the phase
  reports waiting instead.
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
- Once all ACs are met: **build** (no errors); **validate via the [test skill](../test/SKILL.md) — the loop is scope-aware (SA-0MT6BYQHB008DOGC)**: `implement.py finish` first runs a **changed-scope** validation (only the tests affected by this change — fast iteration), then a **final full-suite gate** (`--scope full`) before commit, and the **pre-push hook enforces the full suite again on the push to `dev`**. The finish gate's suite execution delegates to the test skill's canonical `guarded_run_all` (the single source of truth for the outer live-repo guard, SA-0MUH1MJL6003767Q), so a green gate always implies the checkout was verified for mutation regardless of which entry point ran it. `run_tests.py` execution must go through the test-skill runner (`/skill:test`, `run_tests.py --scope full`): only the test-skill runner records runs in the per-repo test cache (keyed by git state + scope, 2h TTL) that the audit skill reads read-only via `query_cached()` to auto-verify execution-dependent ACs — an ad-hoc run (`npx vitest run`, `pytest`, …) is invisible to the audit, so the audit either auto-executes the suite itself on a cache miss (F3, SA-0MSTN5KRF0097TVP) or the operator attests with `--green-run HEAD` (it never hard-blocks — F4, SA-0MSTN8CWM003AAU9). Failures outside scope → triage helper (`python3 $(skill_path triage)/scripts/check_or_create.py '{"test_name":"<name>", "stdout_excerpt":"...", "stack_trace":"...", "parent_work_item_id":"<this-work-item-id>"}'`); implement returned critical issues, re-run until green. Update docs (except `CHANGELOG.md`); summarize changes.

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
  of a stale one. The sync is **safe-skip**: it performs a `git fetch origin
  dev` followed by a `--ff-only` merge (when `dev` is checked out) or a
  non-checked-out ref fetch (`git fetch origin dev:dev`) — it never
  force-updates and never loses local commits. It is skipped (with a logged
  warning, and without failing the already-successful finish) when the main
  checkout is dirty, the remote is unreachable, or the local parent branch has
  diverged. `implement.py start` also refreshes the parent branch before
  `git worktree add`, so children never start from a stale base even when
  another actor pushed concurrently.
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

- **Final validation — belt-and-suspenders (post-push, post-`in_review`):** run
  the full test suite against the committed state and ensure all tests pass.
  If any tests fail: create critical `test-failure` work items with the triage
  helper, fix the failures, and re-run until green before closing the response.

  ```bash
  /skill:test
  ```

  - This runs the full suite (not just changed-scope) against the exact pushed
    commit, catching any regression the pre-push gate missed.
  - Failures → triage helper:
    `python3 $(skill_path triage)/scripts/check_or_create.py
    '{"test_name":"<name>", "stdout_excerpt":"...", "stack_trace":"...",
    "parent_work_item_id":"<this-work-item-id>"}'` → fix → re-run.
  - All tests green → close your response.
  - **Parent/epic runs:** parent/epic items are validated per child at each
    child's Step 9; the parent itself is covered by the per-child test runs
    at Step 6.1.

Pre-push blocking check
-----------------------

Run the full test suite via the [test skill](../test/SKILL.md) (`/skill:test`).
If any tests fail: (1) create a critical `test-failure` work item with
`python3 $(skill_path triage)/scripts/check_or_create.py
'{"test_name":"<name>", "stdout_excerpt":"...", "stack_trace":"...",
"parent_work_item_id":"<this-work-item-id>"}'`; (2) fix the failures; (3)
re-run until green. Only then proceed to commit/push.

Final cleanup (belt-and-suspenders)
---------------------------------------

Before exiting at any point, `wl show <work-item-id> --json`; if `status: in_progress` and work is incomplete (not at Step 9), reset via `StatusLifecycle.update_status(work_item_id, "open")` to prevent orphaned `in_progress` items blocking other agents.

## Status Transition Matrix

| Phase | Mechanism | Status | Stage |
|-------|-----------|--------|-------|
| Claim (Step 1) | `update_status(id, "in_progress", assignee="<AGENT>")` / `phase_start()` (status-only; stage unchanged) | in_progress | unchanged |
| Epic/parent all children done (5.1) | `update_status(id, "completed", stage="in_review")` | completed | in_review |
| Final (Step 9) | `with StatusLifecycle(id, target_stage="in_review"):` / `phase_finish()` | completed | in_review |
| Abort (dirty/gate/user/error/termination) | `update_status(id, "open")` via `phase_abort()` (error: context manager restores original; termination: final cleanup resets) | open | unchanged |

> **All abort/failure transitions reset to `open`.** Never leave a work item in `in_progress` unless actively implementing.

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
  [--producer-actions "<actions for the producer, or omit for 'None needed'>"] \
  [--notes "<freeform context/caveats/assumptions>"] \
  [--next-action <review|plan|implement|...>]
```

The script prints the rendered report to stdout — **paste it verbatim into
your final response**, so the operator sees the report itself (not just the
tool call), then close with: `<work-item-id>: <one-line summary>`. Do NOT
re-summarize the report in a different format — the report is the summary. When the session ends in a terminal state with no open questions for the operator, end your final response with `</end_session>` on its own line as the very last line after the summary; if the session ends with questions for the operator, do not emit the marker.
