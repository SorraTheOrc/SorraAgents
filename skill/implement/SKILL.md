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

## Best Practices

- Follow the steps in order and do not skip steps.
- **Write tests before implementation code** (test-driven development). Always create at least one test file before editing implementation code. Tests may fail on first run; write implementation code to make them pass. When external constraints prevent complete tests, create harnesses/mocks and document the limitation as a temporary placeholder.
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

## Status Safety & Abort Handling

### Critical Rule: Always Reset Status on Abort

When an implementation is aborted, interrupted, or fails before reaching the
final commit/push step, the work item can remain stuck at `status: in_progress`,
blocking other agents from claiming or processing it. **Every abort/failure path
MUST reset status to `open`** to release the work item lock.

### Mandatory Abort Pattern

All abort paths follow the same two-step pattern:

1. **Reset status to open:** `wl update <work-item-id> --status open --json`
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

## Handling Assets

- **Graphics/audio:** Create in `assets/images/` or `assets/audio/` with a `placeholder_` prefix. Reference in work item comments and commit. Optimize for size/performance. Use only assets you have rights to distribute; provide attribution where required.
- **Documentation:** Update relevant markdown files in `docs/`. Ensure changes are clear and accurate.
- **Exception:** `CHANGELOG.md` is excluded — managed automatically by the ship skill's release pipeline.

## Steps

Execute the following steps in order. Do not skip steps. Use the live commands where applicable and record outputs in the work-item comments as you proceed.

1. Set status and safety gate

- **Before any other step**, claim the work item:
  `wl update <work-item-id> --status in_progress --json`
  This signals to other agents that this item is being worked on.

1. Safety gate: handle dirty working tree

Check the git context and handle uncommitted changes before proceeding.

- Run `git rev-parse --is-inside-work-tree` to detect if inside a worktree.
- Run `git status --porcelain=v1 -b` to check for uncommitted changes.
- **Inside a worktree:**
  - If changes are limited to `.worklog/`, carry them forward.
  - If other changes exist, present choices: carry, commit, stash, revert, or abort.
- **In the main checkout:**
  - If changes are limited to `.worklog/`, carry them forward.
  - Otherwise, report dirty files (they may be stale), proceed to create a worktree for isolation.
  - If dirty files prevent worktree creation, follow the carry/commit/stash/revert/abort prompt.

On abort: `wl update <work-item-id> --status open --json`

1. Understand the work item

If not already assigned: `wl update <work-item-id> --status in_progress --stage in_progress --assignee "<AGENT>" --json`

Check the most recent worklog action, comment, or audit entry:
- If a recent audit exists, reuse it to establish the work.
- If no recent audit exists, run `/skill:audit <work-item-id>` for a full audit.

Fetch details: `wl show <work-item-id> --json`. Pay attention to `description`, `acceptance criteria`, and `comments`.

Restate ACs and current status. Surface blockers, dependencies, missing requirements. Inspect linked PRDs, plans, or docs. Confirm expected tests or validation steps.

1.1. Definition gate (must pass before implementation)

Verify:
- Clear scope (in/out-of-scope).
- Concrete, testable ACs.
- Constraints and compatibility expectations.
- Unknowns captured as explicit questions.

If the gate fails:
1. `wl update <work-item-id> --status open --json`
2. If not well-defined → run intake interview (see `command/intake.md`).
3. If too large → run plan interview (`/skill:plan`) to decompose.
4. Inform the user and ask if they want to restart implementation review.

1. Create a worktree from dev and branch inside it

Follow the worktree convention in [[concepts/git-worktree-best-practices-for-agent-workflows]]:

```bash
git worktree add --track -b wl-<WIP-id>-<short-slug> .worklog/worktrees/wl-<WIP-id>-<short-slug> dev
cd .worklog/worktrees/wl-<WIP-id>-<short-slug>
```

See [AGENTS.md](../../AGENTS.md#implement-the-work-item) for the top-level policy.

1. Implement

- If the work item has open/in_progress blockers or dependencies, implement them first (recursively via this procedure).

4.1. Parent-advancement check (epic/parent items only)

After all recursive child implementations are complete, check whether this work item has children:

- Use `wl show <work-item-id> --children --json` to inspect children.
- **If all children are in a terminal stage** (`in_review`/`completed`/`done`):
  - Advance the parent: `wl update <work-item-id> --status completed --stage in_review --json`
- **If any children are NOT in a terminal stage**:
  - Set to open: `wl update <work-item-id> --status open --json`
  - Add a comment flagging the gap for producer attention:
    `wl comment add <work-item-id> --comment "Not all children are in a terminal stage. Needs producer review." --author "<AGENT>" --json`
  - Return control to the operator.

> **Under Ralph:** parent advancement is handled by Ralph's `_wl_update_stage()`. Skip the manual advancement above when operating under Ralph orchestration.

- Check for a recent audit record; if none, run `/skill:audit <work-item-id>` to establish work needed.
- Write tests and code to meet acceptance criteria:
  - Make minimal, focused changes.
  - **Write tests first** (TDD): create at least one test file before editing implementation code. Tests may fail initially; implement code to make them pass. If external constraints prevent complete tests, use harnesses/mocks and document the limitation.
  - Follow project style and conventions.
  - Comment on significant design decisions.
  - If additional work is discovered, create linked work items: `wl create "<title>" --deps discovered-from:<work-item-id> --json`
- Once all ACs are met:
  - **Build** the project and verify no errors.
  - **Run the full test suite**. Report results. Fix any failures.
  - If failing tests are outside this work item's scope, invoke the triage helper:
    `python3 ../triage/scripts/check_or_create.py '{"test_name":"<name>", "stdout_excerpt":"...", "stack_trace":"...", "parent_work_item_id":"<this-work-item-id>"}'`
    - If a new or incomplete critical issue is returned, implement it, fix the test, and re-run until all pass.
    - Under Ralph, failing tests are handled automatically.
  - Update documentation (excluding `CHANGELOG.md`, which is managed by the ship pipeline).
  - Summarize changes in the work item.
  - Wait for user confirmation before proceeding.

1. Error/exception handling (abort on unexpected errors)

On unexpected error (API failure, network error, exception):
1. **Reset status to open:** `wl update <work-item-id> --status open --json`
2. **Log the error:** `wl comment add <work-item-id> --comment "Error: <description>" --author "<AGENT>" --json`
3. **Return control** to the operator with error details.
4. If transient, the operator may retry.

### User-initiated abort

If the operator cancels after Step 0:
1. `wl update <work-item-id> --status open --json`
2. Return control to the operator.
3. Document: `wl comment add <work-item-id> --comment "Aborted by operator" --author "<AGENT>" --json`

---

1. Optional refactor step

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

1. Automated self-review

- Build and lint the code; fix any issues.
- Run all tests again using quiet test commands; fix any failures.
- Audit the work item: `/skill:audit <work-item-id>`. If ACs are unmet, inform the user and return to step 3.
- Perform sequential self-review passes: completeness, dependencies & safety, scope & regression, tests & acceptance, polish & handoff.
- For each pass, make small, goal-aligned edits. If intent changes are discovered, create an Open Question and stop.
- Run the full test suite; fix any failures before continuing.

1. Commit, Push to dev and mark in_review

- Follow the mandatory build → test → commit order before committing.
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

  > **Parent/epic items already advanced at Step 4.1:** if this item has children and parent advancement was already performed, skip the `wl update` below.
  > **Under Ralph:** do NOT mark `in_review` — Ralph handles it after the audit.
  > **Manual (leaf items, or parents not yet advanced):** mark `in_review` (do **NOT** close): `wl update <work-item-id> --status completed --stage in_review --json`

  > The work-item stays `in_review` until the release process promotes `dev` to `main`. See `../ship/SKILL.md` for push-to-dev workflow and `../ship/scripts/run-release.js` for release.

Pre-push blocking check
-----------------------

- Under Ralph: failing tests automatically create child work items via the triage helper, get fixed via `implement-single`, and re-run until all pass before push.
- Manual: invoke the triage helper and fix any failing tests before pushing.

Final cleanup (belt-and-suspenders)
---------------------------------------

Before exiting the implement skill at any point, check and reset status as a safety net:

```bash
wl show <work-item-id> --json
```

If `status: in_progress` and work is not complete (not at Step 7), run:

```bash
wl update <work-item-id> --status open --json
```

This prevents orphaned `in_progress` items from blocking other agents.

## Status Transition Matrix

The following table documents the expected status and stage transitions at each workflow phase for the `implement` skill.

| Phase | Command | Status | Stage |
|-------|---------|--------|-------|
| Start (Step 0 - Set status) | `wl update <id> --status in_progress --json` | in_progress | (unchanged) |
| Claim (Step 1) | `wl update <id> --status in_progress --stage in_progress --assignee "<AGENT>" --json` | in_progress | in_progress |
| Epic / parent: all children done (Step 4.1) | `wl update <id> --status completed --stage in_review --json` | completed | in_review |
| Final (Step 6 - Mark in_review) | `wl update <id> --status completed --stage in_review --json` | completed | in_review |
| Abort - dirty work tree | `wl update <id> --status open --json`, then abort | open | (unchanged) |
| Abort - definition gate failure | `wl update <id> --status open --json`, run intake/plan interview, update item | open | (unchanged) |
| Abort - user-initiated | `wl update <id> --status open --json`, return control to operator | open | (unchanged) |
| Abort - error/exception during implementation | `wl update <id> --status open --json`, log error to comment, return | open | (unchanged) |
| Abort - unexpected termination (Final cleanup) | Check status; if `in_progress` and work incomplete, reset | open | (unchanged) |
| Under Ralph (Step 6 note) | Skip in_review step; Ralph handles transition | in_progress | in_progress |

> **Abort/failure transitions always use `--status open` while keeping the stage unchanged.**
> This pattern is mandatory — never leave a work item in `in_progress` status
> unless actively implementing. See the "Status Safety & Abort Handling" section for details.

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

# Mark in_review
wl update SA-0MPYMFZXO0004ZU4 --status completed --stage in_review --json
```

End.
