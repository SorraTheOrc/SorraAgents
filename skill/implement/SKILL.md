---
name: implement
description: |
  Write tests, docs and code for a Worklog work item by following a
  deterministic workflow. Ensure implementation meets defined acceptance
  criteria. Trigger on user queries such as: 'Implement <work-item-id>',
  'Complete <work-item-id>', 'Work on <work-item-id>'.
---

## Purpose

Provide a deterministic, step-by-step implementation workflow for completing a
Worklog work item through the creation of code, tests, and documentation.

## Inputs

- work-item id: required. Validate id format `<prefix>-<hash>` and prompt if
  missing.
- Optional freeform guidance in the arguments string may be used to shape the
  implementation approach.

## Outputs

- Tests and implementation code meeting acceptance criteria (committed to a
  branch and pushed to `dev`).
- Work item updated to `in_review` stage (work item is NOT closed; it stays
  open until the release process promotes the changes to `main`).

## References to Bundled Resources

- Intake/interview helpers: `intake`, `plan`.

Security note: Do not push or create PRs automatically unless the invoking
agent has explicit permission to push to the repository and open pull
requests. Require explicit confirmation before performing remote actions
(push/pr creation) when operating without an operator-approved credential.
When in doubt, produce the exact `git`/`gh`/`wl` commands for a human to run.

Privacy note: Avoid including secrets, tokens, or personally-identifiable data
in work item comments or PR bodies. If such data must be referenced, reference
it by work-item id or document path instead of pasting values. Mask or redact
any sensitive values before writing them to logs or comments.

## StatusLifecycle Integration

Status transitions are managed by the shared `StatusLifecycle` context manager
from `../shared/status_lifecycle.py`. The implement skill's orchestration
script (`implement.py`) uses `StatusLifecycle` for automatic status management:

- **`phase_start()`** uses `StatusLifecycle.update_status()` to claim the
  work item (`status → in_progress`)
- **`phase_finish()`** wraps build/test/commit/push in
  `with StatusLifecycle(..., target_stage="in_review"):` which:
  - On success: sets `status=completed`, `stage=in_review`
  - On error: restores original status (no orphaned `in_progress` items)
- **`phase_abort()`** uses `StatusLifecycle.update_status()` to reset to `open`

For agents following this SKILL.md manually (without the orchestration script),
use the `StatusLifecycle.update_status()` static method or the context manager
pattern described in `../shared/status_lifecycle.py`.

## Test Anti-Patterns

Before writing tests, review the shared [Test Writing Guidelines
](../shared/test-writing-guidelines.md) which documents six anti-patterns
identified during a full audit of the Tableau-Card-Engine test suite (32
low-value files removed). Never write tests that:

1. Grep source code instead of asserting behaviour (source-code-grep tests).
2. Contain `expect(true).toBe(true)` or zero assertions (placeholder tests).
3. Re-implement production logic inside the test (self-referential simulations).
4. Duplicate an existing core test under a different name (duplicates).
5. Assert type-level satisfaction the compiler already checks (structural-only tests).
6. Boot a browser / scene without asserting anything (zero-assertion tests).

Follow the positive guidance in the same document: every test must assert
observable behaviour via the public API.

## Best Practices

- Follow the steps in order and do not skip steps.
- **Testing is required — TDD is preferred, not mandatory.** Ensure code has excellent test coverage with meaningful tests. Write tests first (test-driven development) whenever practical; alternative strategies (e.g., test-after) are permitted when TDD would complicate implementation. When external constraints prevent complete tests, create harnesses/mocks and document the limitation. **Do NOT write placeholder tests** — if the feature is not yet implemented, track the work in a work item instead (see [Test Writing Guidelines](../shared/test-writing-guidelines.md)).
- Do not use search tools (grep, ripgrep, code search). Rely on work-item context and linked docs. If insufficient context, run intake interview.
- Keep implementation focused on meeting acceptance criteria with minimal changes.
- Never edit code outside `src/`, `tests/`, `docs/` unless essential configuration files.
- Never edit bundled libraries (`dist/`, `node_modules/`).
- When implementing a CLI or API, always provide JSON formatted output.
- Use work item comments to document process, decisions, and next steps.
- Handle errors gracefully with actionable remediation messages.
- If the work item is not well-defined, run intake interview before proceeding.
- If blockers or dependencies exist, implement those first.
- Follow AGENTS.md policies for branch naming, commit discipline, worktree workflow, and push-to-dev integration. See [AGENTS.md](../../AGENTS.md#implement-the-work-item).
- After implementation is `in_review`, use the cleanup skill to tidy up local feature branches (do not clean up `dev` or `main`).
- Use `StatusLifecycle` for all status transitions — never use ad-hoc `wl update --status` commands.

## Status Safety & Abort Handling

### Critical Rule: Always Reset Status on Abort

When an implementation is aborted, interrupted, or fails before reaching the
final commit/push step, the work item can remain stuck at `status: in_progress`,
blocking other agents from claiming or processing it. **Every abort/failure path
MUST reset status to `open`** to release the work item lock.

### Mandatory Abort Pattern

All abort paths follow the same two-step pattern:

1. **Reset status to open:** `StatusLifecycle.update_status(<work-item-id>, "open")`
2. **Stop execution:** Return control to the operator with a clear explanation

> **Why this matters:** Work items in `in_progress` status are filtered by `wl next`
> and are invisible to other agents. An orphaned `in_progress` item blocks all
> downstream work on that item until a human intervenes. Always resetting to `open`
> on abort ensures the work item is visible and claimable by the next agent.

### Abort Scenarios

The implement skill covers **five** abort/failure scenarios, each with explicit
status-reset instructions documented in the sections below:

| # | Scenario | Description |
|---|----------|-------------|
| 1 | Dirty work tree abort | User aborts when uncommitted changes exist in the working tree |
| 2 | Definition gate failure | Work item fails definition gate (unclear scope, untestable ACs) |
| 3 | User-initiated abort | Operator cancels the implementation mid-process |
| 4 | Error/exception during implementation | API failure, network error, or unexpected exception during coding |
| 5 | Unexpected termination | Agent crash, network failure, or external interruption (covered by Final cleanup step) |

> **Status reset is conditional, not blind.** The orchestration script's
top-level error/signal handlers and the signal handler use
`_safety_reset_if_in_progress()` — they reset an item to `open` **only if**
it is currently `in-progress`. This prevents clobbering items that already
reached a valid terminal state (`open`/`blocked`/`completed`).

> **Signal handling covers all phases.** Since the work-item id is tracked
for `start`, `finish`, and `abort` alike, a SIGINT/SIGTERM during any phase
releases the item back to `open`. The handler uses `os._exit` so the reset
cannot be undone by a `StatusLifecycle` context-manager rollback during
exception unwinding.

## Handling Assets

- **Graphics/audio:** Create in `assets/images/` or `assets/audio/` with a `placeholder_` prefix. Reference in work item comments and commit. Optimize for size/performance. Use only assets you have rights to distribute; provide attribution where required.
- **Documentation:** Update relevant markdown files in `docs/`. Ensure changes are clear and accurate.
- **Exception:** `CHANGELOG.md` is excluded — managed automatically by the ship skill's release pipeline.

## Steps

Execute the following steps in order. Do not skip steps. Use the live commands where applicable and record outputs in the work-item comments as you proceed.

1. Set status and safety gate

- **Before any other step**, claim the work item using the orchestration script
  or `StatusLifecycle`:
  `StatusLifecycle.update_status(<work-item-id>, "in_progress")`
  This signals to other agents that this item is being worked on.

> **Code Freeze gate:** when using `implement.py start <id>`, the script checks
> the Code Freeze marker (`.worklog/code-freeze.json`, contract
> WL-0MSBU4KMA004PKSR) **before** claiming the work item. If a release is in
> progress the script refuses with "Project is in Code Freeze — implementation
> blocked until the release completes", exits non-zero, and does **not** change
> the work item status. There is no `--force` bypass. Fail-open: a missing or
> corrupt marker never blocks implementation. `implement-single` inherits the
> gate because it delegates to `implement.py`.

2. Safety gate: handle dirty working tree

Check the git context and handle uncommitted changes before proceeding.

- Run `git rev-parse --is-inside-work-tree` to detect if inside a worktree.
- Run `git status --porcelain=v1 -b` to check for uncommitted changes.

**CRITICAL: Never stash, commit, or revert the user's uncommitted changes
without explicit permission.** Uncommitted changes in the working tree may be
user-authored work; stashing them without asking can strand that work and is
forbidden. When uncommitted changes exist, STOP and ask the operator how to
proceed (commit, stash, revert, or abort) — act only after the operator
explicitly chooses an option.

- **Inside a worktree:**
  - If changes are limited to `.worklog/`, carry them forward.
  - If other changes exist, stop and ask the operator how to proceed (commit,
    stash, revert, or abort); never stash/commit/revert unilaterally.
- **In the main checkout:**
  - If changes are limited to `.worklog/`, carry them forward.
  - Otherwise, report the dirty files to the operator (they may be stale) and
    proceed to create a worktree for isolation — without touching the user's
    uncommitted changes.
  - If dirty files prevent worktree creation, stop and ask the operator how to
    proceed, and act only on their explicit choice.

On abort: `StatusLifecycle.update_status(<work-item-id>, "open")`

3. Understand the work item

If not already assigned (or when using the orchestration script, this is handled
by `phase_start()`): `StatusLifecycle.update_status(<work-item-id>, "in_progress", stage="in_progress", assignee="<AGENT>")`

Check the most recent worklog action, comment, or audit entry:

- If a recent audit exists, reuse it to establish the work.
- If no recent audit exists, run `/skill:audit <work-item-id>` for a full audit.

Fetch details: `wl show <work-item-id> --json`. Pay attention to `description`, `acceptance criteria`, and `comments`.

Restate ACs and current status. Surface blockers, dependencies, missing requirements. Inspect linked PRDs, plans, or docs. Confirm expected tests or validation steps.

3.1. Definition gate (must pass before implementation)

Verify:

- Clear scope (in/out-of-scope).
- Concrete, testable ACs.
- Constraints and compatibility expectations.
- Unknowns captured as explicit questions.

If the gate fails:

1. `StatusLifecycle.update_status(<work-item-id>, "open")`
2. If not well-defined → run intake interview (see `../intake/SKILL.md`).
3. If too large → run plan interview (`/skill:plan`) to decompose.
4. Inform the user and ask if they want to restart implementation review.

4. Create a worktree from dev and branch inside it

> **MANDATORY — worktree requirement:** All implementation work MUST be done
> in a git worktree created from `dev`. Never edit, commit, or push directly
> from the main checkout. `implement.py finish` enforces this: it refuses to
> complete when it detects changes outside the worktree. When using the
> orchestration script, `implement.py start` creates the worktree for you —
> you must `cd` into it and do all work there.

Follow the worktree convention in [[concepts/git-worktree-best-practices-for-agent-workflows]]:

```bash
git worktree add --track -b wl-<WIP-id>-<short-slug> .worklog/worktrees/wl-<WIP-id>-<short-slug> dev
cd .worklog/worktrees/wl-<WIP-id>-<short-slug>
```

See [AGENTS.md](../../AGENTS.md#implement-the-work-item) for the top-level policy.

5. Implement

- If the work item has open/in_progress blockers or dependencies, implement them first (recursively via this procedure).

5.1. Parent-advancement check (epic/parent items only)

After all recursive child implementations are complete, check whether this work item has children:

- Use `wl show <work-item-id> --children --json` to inspect children.
- **If all children are in a terminal stage** (`in_review`/`completed`/`done`):
  - Advance the parent: `StatusLifecycle.update_status(<work-item-id>, "completed", stage="in_review")`
- **If any children are NOT in a terminal stage**:
  - Set to open: `StatusLifecycle.update_status(<work-item-id>, "open")`
  - Add a comment flagging the gap for producer attention:
    `wl comment add <work-item-id> --comment "Not all children are in a terminal stage. Needs producer review." --author "<AGENT>" --json`
  - Return control to the operator.

- Check for a recent audit record; if none, run `/skill:audit <work-item-id>` to establish work needed.
- Write tests and code to meet acceptance criteria:
  - Make minimal, focused changes.
  - **Test adequately — TDD preferred.** Writing tests first (TDD) is the preferred approach, but alternative strategies (e.g., test-after) are acceptable when they better suit the implementation. Ensure excellent test coverage with meaningful tests; if external constraints prevent complete tests, use harnesses/mocks and document the limitation.
  - Follow project style and conventions.
  - Comment on significant design decisions.
  - If additional work is discovered, create linked work items: `wl create "<title>" --deps discovered-from:<work-item-id> --json`
- Once all ACs are met:
  - **Build** the project and verify no errors.
  - **Run the full test suite via the [test skill](../test/SKILL.md) (`/skill:test`)** — run → triage → evaluate → loop until green. Report results. Fix any failures.
  - If failing tests are outside this work item's scope, invoke the triage helper:
    `python3 ../triage/scripts/check_or_create.py '{"test_name":"<name>", "stdout_excerpt":"...", "stack_trace":"...", "parent_work_item_id":"<this-work-item-id>"}'`
    - If a new or incomplete critical issue is returned, implement it, fix the test, and re-run until all pass.
  - Update documentation (excluding `CHANGELOG.md`, which is managed by the ship pipeline).
  - Summarize changes in the work item.
  - Wait for user confirmation before proceeding.

### Error/exception handling (abort on unexpected errors)

On unexpected error (API failure, network error, exception):

1. **Reset status to open:** `StatusLifecycle.update_status(<work-item-id>, "open")`
2. **Log the error:** `wl comment add <work-item-id> --comment "Error: <description>" --author "<AGENT>" --json`
3. **Return control** to the operator with error details.
4. If transient, the operator may retry.

### User-initiated abort

If the operator cancels after Step 2:

1. `StatusLifecycle.update_status(<work-item-id>, "open")`
2. Return control to the operator.
3. Document: `wl comment add <work-item-id> --comment "Aborted by operator" --author "<AGENT>" --json`

---

7. Optional refactor step

After implementation completes and before final commit, an automated refactor step may detect and remediate code smells:

- Analyzes only files modified in the current session (git diff against parent).
- Hybrid approach: linters for mechanical issues + LLM for design/architectural smells.
- **Session-introduced smells** are fixed immediately.
- **Pre-existing smells** create Worklog items with REFACTOR comments.
- Skip with ``--no-refactor`` flag.
- Invoke directly:

  ```bash
  python3 ../refactor/scripts/refactor.py <work-item-id>
  ```

- See ``../refactor/SKILL.md`` for full documentation.

6. Automated self-review

- Build and lint the code; fix any issues.
- Run all tests again using the [test skill](../test/SKILL.md) (`/skill:test`) quiet-run discipline; fix any failures.
- Audit the work item: `/skill:audit <work-item-id>`. If ACs are unmet, inform the user and return to step 5.
- Perform sequential self-review passes: completeness, dependencies & safety, scope & regression, tests & acceptance, polish & handoff.
- For each pass, make small, goal-aligned edits. If intent changes are discovered, create an Open Question and stop.
- Run the full test suite via the [test skill](../test/SKILL.md) (`/skill:test`); fix any failures before continuing.

8. Commit, Push to dev and mark in_review

- Follow the mandatory build → test → commit order before committing.
- **Do NOT create a Pull Request to `main`.** Work is integrated into `dev`; the `dev`→`main` promotion is handled separately by the release process.
- Push the feature branch into `dev` using:
  - Ship skill (preferred): `pushToDev()` from `../ship/scripts/ship.js`
  - Direct: `git push origin HEAD:refs/heads/dev`
- The push target `dev` is **not** a protected branch; only `main`, `master`, and `HEAD` are blocked.
- After pushing, clean up the worktree:

  ```bash
  cd /path/to/repo/root
  git worktree remove .worklog/worktrees/wl-<WIP-id>-<short-slug>
  git worktree prune
  git checkout dev
  git pull origin dev
  npm run build 2>/dev/null || echo "No build script, skipping rebuild"
  ```

  > **Why rebuild?** `dist/` is gitignored; a `git pull` does not update it.

  See [[concepts/git-worktree-best-practices-for-agent-workflows]] for the full worktree lifecycle.
- Add a work-item comment with the commit hash:
  `wl comment add <work-item-id> --comment "Completed work pushed to dev, see commit <hash>." --author "<AGENT>" --json`
- Close your response with: `<work-item-id>: <concise-summary>\n\nWork committed to dev`

  > **Parent/epic items already advanced at Step 5.1:** if this item has children and parent advancement was already performed, skip the status update.
  > **Manual (leaf items, or parents not yet advanced):** mark `in_review` (do **NOT** close): `StatusLifecycle.update_status(<work-item-id>, "completed", stage="in_review")`

  > The work-item stays `in_review` until the release process promotes `dev` to `main`. See `../ship/SKILL.md` for push-to-dev workflow and `../ship/scripts/run-release.js` for release.

Pre-push blocking check
-----------------------

Invoke the triage helper and fix any failing tests before pushing.

Final cleanup (belt-and-suspenders)
---------------------------------------

Before exiting the implement skill at any point, check and reset status as a safety net:

```bash
wl show <work-item-id> --json
```

If `status: in_progress` and work is not complete (not at Step 7), reset via `StatusLifecycle`:

```python
StatusLifecycle.update_status(work_item_id, "open")
```

This prevents orphaned `in_progress` items from blocking other agents.

## Status Transition Matrix

The following table documents the expected status and stage transitions at each
workflow phase. All transitions are managed via `StatusLifecycle` — ad-hoc
`wl update --status` commands (without `StatusLifecycle`) should never be used.

| Phase | Mechanism | Status | Stage |
|-------|-----------|--------|-------|
| Start (Step 0 - Set status) | `StatusLifecycle.update_status(id, "in_progress")` or `phase_start()` | in_progress | (unchanged) |
| Claim (Step 1) | `StatusLifecycle.update_status(id, "in_progress", stage="in_progress", assignee="<AGENT>")` | in_progress | in_progress |
| Epic / parent: all children done (Step 5.1) | `StatusLifecycle.update_status(id, "completed", stage="in_review")` | completed | in_review |
| Final (Step 6 - Mark in_review) | `with StatusLifecycle(id, target_stage="in_review"):` or `phase_start()`+`phase_finish()` | completed | in_review |
| Abort - dirty work tree | `StatusLifecycle.update_status(id, "open")` via `phase_abort()` | open | (unchanged) |
| Abort - definition gate failure | `StatusLifecycle.update_status(id, "open")` | open | (unchanged) |
| Abort - user-initiated | `StatusLifecycle.update_status(id, "open")` via `phase_abort()` | open | (unchanged) |
| Abort - error/exception during implementation | `StatusLifecycle` context manager restores original status | open | (unchanged) |
| Abort - unexpected termination (Final cleanup) | Check status; if `in_progress` and work incomplete, reset | open | (unchanged) |

> **All abort/failure transitions reset to `open`.** The `StatusLifecycle`
> context manager handles this automatically on exception. For manual fallback,
> use `StatusLifecycle.update_status(id, "open")`. Never leave a work item
> in `in_progress` status unless actively implementing.

## Scripts (canonical runner & modules)

This skill does not ship a single orchestrator script. Implementation is carried out by following the steps above and invoking project-local build/test and linters. When a repository provides an "implement" helper script, prefer that script for deterministic behavior.

Example Worklog-oriented commands using SA-0MPYMFZXO0004ZU4 (documentation example):

```bash
# Fetch the work item
wl show SA-0MPYMFZXO0004ZU4 --json

# After implementing locally, push to dev (preferred via ship skill):
# (JS example) node -e "require('../ship/scripts/ship.js').pushToDev('origin')"
# or direct push
git push origin HEAD:refs/heads/dev

# Mark in_review via StatusLifecycle
python3 -c "from skill.shared.status_lifecycle import StatusLifecycle; StatusLifecycle.update_status('SA-0MPYMFZXO0004ZU4', 'completed', stage='in_review')"
```

End.
