Core principles for AI Agents working with work-items tracked in Worklog (wl) and the workflow to follow when completing tasks.

- **NEVER write or edit code without an explicit instruction to use the implement skill** (`/skill:implement <id>`). Non-implement invocations (e.g. `/intake`, `/plan`, `/audit`) must complete their own workflow and STOP — they must not proceed to implementation. When implementation IS authorized, always follow the implement skill's instructions exactly: worktree lifecycle, build → test → commit order (never reverse), and the StatusLifecycle status transitions.
- Tasks require a work-item id; if not provided, ask for one to be created or get permission to create one.
- When asked to complete a task, follow the workflow below: claim, define, plan, decide, implement, update, repeat, end.
- Do NOT ask unnecessary questions. Check existing information first before asking.
- Ensure excellent test coverage with every test serving a meaningful purpose. TDD is preferred but alternatives permitted; see the implement skill for testing guidance. If requirements are unclear, seek clarification.
- Reasonable assumptions are OK but must be documented in the work-item and communicated on completion.
- Do not stop working on a task until you hit an explicit gating step.
- A task is not complete until all acceptance criteria are satisfied, all tests pass, and the work-item is ready for review. Use the `audit` skill to verify before marking as ready.
- If you discover a blocker, create a new work-item, record the blocking relationship, and start working on it.
- When complete, report back concisely with relevant links (work-item id, commits, PRs).

<!-- WORKFLOW: start -->

## Workflow for AI Agents

If you already have a current work-item id, continue using it; otherwise ask the operator to create one or get permission to create one.

1. **Claim** — `wl update <id> --status in_progress --assignee <agent>`.
2. **Define** — `wl show <id> --children --json`; verify clear goal + testable ACs; advance stage `intake_complete`.
3. **Plan** — break into sub-tasks; `wl create --parent <id> --issue-type <type> --priority <level> --json`; advance stage `plan_complete`.
4. **Decide** — `wl next --json`; recurse into children until reaching a leaf item; if none remain, go to End session. (`/skill:implement <parent>` recurses automatically.)
5. **Implement** — see [Implement the work-item](#implement-the-work-item) below.
6. **Update** — close with `<WIP-id>: <summary>`.

   Work committed to dev

   Do not suggest next steps.
7. **Repeat** — return to step 4.
8. **End session** — inform operator, summarize remaining tasks, clean up worktrees.

> **Push policy:** Push only to `dev` — never to `main`. [ship skill](/home/rgardler/.pi/agent/skills/ship/SKILL.md) / `skill/ship/scripts/release/merge-dev-to-main.sh` promotes `dev` to `main`; see [docs/dev/release-process.md](docs/dev/release-process.md).

> **Do NOT close the work-item at this stage.** Closed only after the `dev`→`main` release. "Close a work item" means stage `in_review` or mark `completed` — NOT a release. Agents SHOULD NOT push directly to `main` unless explicitly authorized.

## Implement the work-item

**MANDATORY — worktree requirement:** all implementation MUST happen in the worktree created by `implement.py start <WIP-id>`; `cd` into `.worklog/worktrees/wl-<WIP-id>-<slug>` and make ALL changes there — never edit, commit, or push from the main checkout; `implement.py finish` refuses if it detects changes outside the worktree. See the [implement skill](/home/rgardler/.pi/agent/skills/implement/SKILL.md).

<!-- WORKFLOW: end -->

## CRITICAL RULES

- Use wl for ALL task tracking — never markdown TODOs or task lists.
- Never write directly to `.worklog/worklog-data.jsonl`; use `wl` commands only.
- A child may be closed independently; a parent only once all children closed, blockers resolved, ACs met, and a Producer approved.
- Keep work-items up to date — update descriptions, ACs, stages, comments throughout the lifecycle.
- Every work-item must have a clear goal (user story) with measurable, testable ACs.
- When writing content, escape only backticks; use Markdown formatting as needed.
- Never commit without: a work item association, a clean build, and all tests passing (build → test → commit order, never reversed).
- Always record the commit message and hash in a comment on the relevant work item(s).
- If push fails, resolve and retry until it succeeds.
- When displaying a work-item ID, always include the title: `Title Text (ID)`.
- **Session logging:** on new Pi session or work item, comment on the worklog item: `<agent_action> - Session ID: <pi_session_id> - <path_to_sessions_log>` (`PI_SESSION_ID` / `PI_SESSION_FILE`) via `wl comment add <id> --comment "<comment>" --author "<agent_name>" --json`.

## Important Rules

- wl is the primary source of truth; only source code is more authoritative. Always use `--json` for programmatic use. New work items discovered during work → `wl create`: child if blocking (`--parent <current-id>`), else `discovered-from:<current-id>` in the description. Check `wl next` before asking what to work on.

## Stage vs Status distinction

Two lifecycle axes, managed independently:

- **`status`** — lifecycle (open, in-progress, completed). Only set `completed` when formally closed (post-release).
- **`stage`** — workflow progress (idea, intake_complete, plan_complete, in_progress, in_review). When advancing to `in_review`, also set `status` to `completed`.
- **Epics/parent items:** once all children are terminal, advance the parent's `stage` to `in_review`.

## work-item Types

`--issue-type`: bug, docs, feature, task (tests/refactoring), epic (large feature with subtasks), chore (maintenance/deps/tooling).

## Priorities

- critical — security, data loss, broken builds
- high — major features, important bugs
- medium — default, nice-to-have
- low — polish, optimization

## Test-failure triage policy

Before `in_review`, run the full suite via `/skill:test`; triage failures into critical `test-failure` work items ([triage skill](/home/rgardler/.pi/agent/skills/triage/SKILL.md)). **All tests must pass** before `in_review`.

## Work-Item Management

Run `wl --help` / `wl <cmd> --help` for full docs (create, update, comment, close, dep, sync, github import/push, plugins). **NEVER run `wl list` without a search term** — use `wl list <term> --json` (substring, matches filenames/paths/ids) or `wl search <keywords> --json` (FTS).

## Coding Disciplines

1. **Think Before Coding** — Don't assume; state assumptions, push back, stop when confused and ask.
2. **Simplicity First** — Minimum code that solves the problem; nothing speculative.
3. **Surgical Changes** — Touch only what you must; match style; mention dead code, don't delete it.
4. **Repository Boundaries** — Don't edit `.pi/`, `skill/`, `command/`, or agent-infrastructure dirs unless explicitly instructed; report issues via work item/comment.
5. **Goal-Driven Execution** — Define success criteria and loop until verified; state a brief plan (`1. [Step] → verify: [check]`).

## Skill invocation map (hidden helper skills)

Skills with `disable-model-invocation: true` (internal helpers / always invoked explicitly) do **not** appear in the session's available-skills block; they remain invocable via `/skill:<name>` or orchestrating scripts — do not reimplement their logic inline.

| Skill | How it is invoked | Purpose |
|---|---|---|
| `owner-inference` | triage's `check_or_create.py` | Infer owner for a failing test file |
| `triage` | test skill's scripts (`check_or_create.py`) | Search/create critical `test-failure` work items |
| `find-related` | `/skill:find-related <id>` | Discover related work for a work item |
| `effort-and-risk` | `/skill:effort-and-risk <id>` (plan/implement) | Produce effort/risk estimates |
| `speak` | `/skill:speak <text>` or `./scripts/speak.sh` | Generate audible speech from text |
| `git-management` | `/skill:git-management` | Unified git feature-branch lifecycle management |
