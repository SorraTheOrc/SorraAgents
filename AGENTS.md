Core principles for AI Agents working with work-items tracked in Worklog (wl) and the workflow to follow when completing tasks.

- Tasks require a work-item id; if not provided, ask for one to be created or get permission to create one.
- When asked to complete a task, follow the workflow below: claim, define, plan, decide, implement, update, repeat, end.
- Do NOT ask unnecessary questions. Check existing information first before asking.
- Write tests *before* writing code. If requirements are unclear, seek clarification before proceeding.
- Reasonable assumptions are OK but must be documented in the work-item and communicated upon completion.
- Do not stop working on a task until you hit an explicit gating step.
- A task is not complete until all acceptance criteria are satisfied, all tests pass, and the work-item is ready for review. Use the `audit` skill to verify before marking as ready.
- If you discover a blocker, create a new work-item, record the blocking relationship, and start working on it.
- When complete, report back concisely with relevant links (work-item id, commits, PRs).

<!-- WORKFLOW: start -->

## Workflow for AI Agents

Follow the steps below when completing tasks. If you already have a current work-item id, continue using it. Otherwise, ask the operator to create one or give permission to create one. If the operator allows skipping work-item creation, proceed without tracking steps.

1. **Claim the work-item** — Run `wl update <id> --status in_progress --assignee <agent>`.
2. **Ensure the work-item is clearly defined** — Fetch with `wl show <id> --children --json`. Verify it has a clear goal (user story) and testable acceptance criteria. If unclear, search worklog/repo for context, clarify with the operator, or document open questions. Advance stage: `wl update <id> --stage intake_complete`. See [skill/intakeall/SKILL.md](skill/intakeall/SKILL.md).
3. **Plan the work** — Break into sub-tasks. Verify descriptions and ACs are clear, measurable, and testable. Create child work-items: `wl create -t "<title>" -d "<description>" --parent <id> --issue-type <type> --priority <level> --json`. Advance stage: `wl update <id> --stage plan_complete`. See [skill/plan/SKILL.md](skill/plan/SKILL.md).
4. **Decide what to work on next** — Use `wl next --json`. If the recommended item has children, claim it and recurse until reaching a leaf item. If no descendants remain, go to End session.
5. **Implement the work-item** — Use the implement orchestration script to manage the deterministic lifecycle:

   ```bash
   # Start: claim, safety gate, create worktree
   python3 /home/rgardler/.pi/agent/skills/implement/scripts/implement.py start <WIP-id>

   # Switch to the worktree
   cd .worklog/worktrees/wl-<WIP-id>-<slug>
   ```

   Write tests first, then code. Follow build → test → commit order (never reverse). When done:

   ```bash
   # Finish: refactor, build, test (fix loop), commit, cleanup, push, mark in_review
   python3 /home/rgardler/.pi/agent/skills/implement/scripts/implement.py finish <WIP-id>
   ```

   To abort at any point:
   ```bash
   python3 /home/rgardler/.pi/agent/skills/implement/scripts/implement.py abort <WIP-id>
   ```

   After committing and pushing changes, close your response to the operator with: `<WIP-id>: <concise-summary>`

   Work committed to dev

   See [skill/implement/SKILL.md](skill/implement/SKILL.md) for the full implementation workflow (test-driven development, commit discipline, worktree lifecycle, error handling).

6. **Update the operator** — Provide a concise summary with relevant links (id, commits, PRs). Do not suggest next steps.
7. **Repeat** — Return to step 4.
8. **End session** — When no descendants remain, inform operator, summarize remaining tasks, clean up worktrees. See [skill/cleanup/SKILL.md](skill/cleanup/SKILL.md).

> **Push policy:** Push only to `dev` — never to `main`. The release process ([skill/ship/SKILL.md](skill/ship/SKILL.md) / `skill/ship/scripts/release/merge-dev-to-main.sh`) promotes `dev` to `main`. See also [docs/dev/release-process.md](docs/dev/release-process.md).

> **Do NOT close the work-item at this stage.** Work-items are closed only after the `dev`→`main` release is complete. When a human operator says "close a work item", they mean update the stage to `in_review` or mark as `completed` — NOT initiate a release. Agents may perform the release by invoking the Ship skill, or a Release Manager may do it manually. Agents SHOULD NOT push directly to `main` unless explicitly authorized.

<!-- WORKFLOW: end -->

## work-item Tracking with Worklog (wl)

IMPORTANT: This project uses Worklog (wl) for ALL work-item tracking. Do NOT use markdown TODOs, task lists, or other tracking methods.

## CRITICAL RULES

- Use wl for ALL task tracking — never markdown TODOs, task lists, or other methods.
- Never write directly to `.worklog/worklog-data.jsonl`. Use `wl` commands to interact with worklog data to ensure data integrity and consistency.
- A child work-item may be closed independently; a parent work-item can only be closed once all children are closed, all blockers resolved, and a Producer has reviewed and approved.
- Keep work-items up to date — update descriptions, ACs, stages, and comments throughout the lifecycle.
- Every work-item must have a clear goal (preferably a user story) with measurable, testable ACs. Seek clarification if unclear.
- When writing content for work-items, do not escape special characters EXCEPT backticks. Use markdown formatting as needed. Do not add unnecessary escaping.
- Never commit changes without associating them with a work item.
- Never commit without ensuring the build completes without errors and all tests pass.
- Always follow build → test → commit order. Never reverse or skip steps.
- Before reporting work as done, rebuild and run the full test suite. Confirm the build succeeds and no tests fail.
- Always record the commit message and hash in a comment on the relevant work item(s).
- When making comments, include the changes made, files affected, and the commit hash.
- If push fails, resolve and retry until it succeeds.
- When using backticks in shell command arguments, ALWAYS escape them properly.
- Never close a work item without ensuring all ACs are met, all children closed, all blockers resolved, and a Producer has reviewed/approved.
- When displaying a work-item ID in any output, always include the item title alongside the ID using the format `Title Text (ID)` (e.g., `Per-project isolation for .env and scheduler_store.json with global installs (SA-0MLU57S7D1KX8CU7)`). This ensures every reference is self-describing.

## Important Rules

- Use wl as the primary source of truth; only source code is more authoritative.
- Always use `--json` for programmatic use of wl commands.
- When new work items are discovered during work, create them with `wl create`:
  - If it must be completed before the current item, add as child (`wl create --parent <current-id>`)
  - If related but not blocking, add `discovered-from:<current-id>` in the description
- Check `wl next` before asking "what should I work on?" and offer the result as a suggestion with explanation.
- Run `wl --help` and `wl <cmd> --help` to discover available wl flags and capabilities.
- Use work items for all significant work: bugs, features, tasks, epics, chores.
- Use clear, concise titles and detailed descriptions.
- Use parent/child relationships for dependencies and subtasks.
- Use priorities to indicate importance.
- Use stages to track workflow progress.
- Do NOT clutter repo root with planning documents.

## Stage vs Status distinction

Work items have two lifecycle axes that agents must manage independently:

- **`status`** tracks the work-item lifecycle (open, in-progress, completed). Only set `status` to `completed` when the work-item is formally closed (post-release).
- **`stage`** tracks workflow progress (idea, intake_complete, plan_complete, in_progress, in_review). Advance `stage` to `in_review` as soon as implementation is ready for human review. When advancing to `in_review`, set `status` to `completed` to leave the work item in a consistent `completed/in_review` state.
- **Epics/parent items:** Once all children are in a terminal stage (`in_review` or `completed`), advance the parent's `stage` to `in_review`. The parent's `status` should remain `in-progress` until formal post-release closure.

## work-item Types

Track with `--issue-type`:

- bug — Something broken
- feature — New functionality
- task — Work item (tests, docs, refactoring)
- epic — Large feature with subtasks
- chore — Maintenance (dependencies, tooling)

## Work Item Descriptions

- Use clear, concise titles summarizing the work item.
- Do not escape special characters.
- The description must provide sufficient context for understanding and implementing.
- All descriptions **must be written in Markdown**; comments must also use Markdown formatting.
- At a minimum include:
  - A summary of the problem or feature
  - Example User Stories if applicable
  - Expected behaviour and outcomes
  - Steps to reproduce (for bugs)
  - Suggested implementation approach if relevant
  - Links to related work items or documentation
  - Measurable and testable acceptance criteria

## Priorities

- critical — Security, data loss, broken builds
- high — Major features, important bugs
- medium — Default, nice-to-have
- low — Polish, optimization

## Dependencies

Use parent/child relationships to track blocking dependencies.

- Child items must be completed before the parent can be closed.
- If an item blocks another, make it a child of the blocked item.
- If an item blocks multiple items, create the parent/child relationships with the highest priority item as the parent unless one is in_progress (that item becomes the parent). If in doubt, raise for product manager review.

Other dependency types can be tracked in descriptions: `discovered-from:<id>`, `related-to:<id>`, `blocked-by:<id>`. Worklog does not enforce these but they help with planning.

## Workflow management

- Use `--stage` to track workflow stages (e.g., idea, intake_complete, plan_complete, in_progress, done).
- Use `--assignee` to assign items to agents.
- Use `--tags` for filtering and organization (avoid over-tagging).
- Use comments to document progress, decisions, and context.
- Use `risk` and `effort` fields for complexity tracking. If available, use the `effort_and_risk` agent skill to estimate.

## Test-failure triage policy

When an agent discovers a failing test outside its ownership/scope, call the triage helper script:

```bash
python3 skill/triage/scripts/check_or_create.py '{"test_name":"<name>", "stdout_excerpt":"...", "stack_trace":"...", "parent_work_item_id":"<current-id>"}'
```

- Any incomplete work item tagged `test-failure` matching the test name is linked/updated.
- If no match exists, a `critical` work item is created using the template at `skill/triage/resources/test-failure-template.md`.
- The child is then implemented via `implement-single`, fixed, committed, and tests re-run.
- **All tests must pass** before a work item reaches `in_review` — including pre-existing failures.

## Work-Item Management

Use `wl --help` and `wl <cmd> --help` for full documentation. Common operations:

```bash
# Create work items
wl create --title "Bug title" --description "<details>" --priority high --issue-type bug --json
wl create --title "Subtask" --parent <parent-id> --priority medium --json

# Update
wl update <id> --status in_progress --json
wl update <id> --priority high --json

# Comments
wl comment list <id> --json
wl comment add <id> --comment "<text>" --author "<name>" --json

# Close
wl close <id> --reason "PR #123 merged" --json

# Dependencies
wl dep add <dependent-id> <prereq-id>
```

## Project Status

```bash
# Next ready work item (recommendation)
wl next --json

# In progress items
wl in_progress --json
wl in_progress --assignee "<agent>" --json

# Recently created or updated
wl recent --json
wl recent --number 10 --children --json

# List items (default excludes completed)
wl list --json
wl list --status open --json
wl list --priority high --json
wl list --tags "frontend,bug" --json
wl list --assignee "<name>" --json
wl list --stage review --json

# Search
wl search <keywords> --json
wl search <keywords> --status open --json

# Show details
wl show <id> --format full --json
```

### Team

```bash
wl sync                          # Sync local data with remote
wl github import                 # Import issues from GitHub into worklog
wl github push                   # Push worklog changes to GitHub issues
```

### Plugins

Check available plugins with `wl --help` (See plugins section). For plugin features run `wl <plugin-command> --help`.

### Help

Run `wl --help` for general help and available commands. Run `wl <command> --help` for help on any specific command.

## Coding Disciplines

> Source: [Karpathy-Inspired Claude Code Guidelines](https://github.com/multica-ai/andrej-karpathy-skills) on LLM coding pitfalls.

These four principles complement the workflow and core principles above by addressing common LLM coding pitfalls: making unchecked assumptions, overcomplicating solutions, making unnecessary side-effect edits, and executing vague goals without verifiable success criteria. They bias toward **caution over speed** — for trivial tasks (simple typo fixes, obvious one-liners) use judgment; not every change needs the full rigor.

### 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

LLMs often pick an interpretation silently and run with it. This principle forces explicit reasoning:

- **State assumptions explicitly** — If uncertain, ask rather than guess
- **Present multiple interpretations** — Don't pick silently when ambiguity exists
- **Push back when warranted** — If a simpler approach exists, say so
- **Stop when confused** — Name what's unclear and ask for clarification

### 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

Combat the tendency toward overengineering:

- No features beyond what was asked
- No abstractions for single-use code
- No "flexibility" or "configurability" that wasn't requested
- No error handling for impossible scenarios
- If 200 lines could be 50, rewrite it

**The test:** Would a senior engineer say this is overcomplicated? If yes, simplify.

### 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:

- Don't "improve" adjacent code, comments, or formatting
- Don't refactor things that aren't broken
- Match existing style, even if you'd do it differently
- If you notice unrelated dead code, mention it — don't delete it

When your changes create orphans:

- Remove imports/variables/functions that YOUR changes made unused
- Don't remove pre-existing dead code unless asked

**The test:** Every changed line should trace directly to the user's request.

### 4. Repository Boundaries

**Stay in your lane. Don't modify the tooling.**

- Do NOT edit files in `.pi/`, `skill/`, `command/`, or any agent-infrastructure directory unless explicitly instructed.
- The skills and commands under these paths are part of the agent framework itself — modifying them is equivalent to modifying the tool you're holding.
- If a skill or command behaves unexpectedly, report the issue to the operator via a work item or comment instead of patching it yourself.
- If you are given permission to modify infrastructure, create a work item in the relevant project (e.g., SorraAgents) first to track the change.

### 5. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform imperative tasks into verifiable goals:

| Instead of... | Transform to... |
|--------------|-----------------|
| "Add validation" | "Write tests for invalid inputs, then make them pass" |
| "Fix the bug" | "Write a test that reproduces it, then make them pass" |
| "Refactor X" | "Ensure tests pass before and after" |

For multi-step tasks, state a brief plan:

```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let the LLM loop independently. Weak criteria ("make it work") require constant clarification.
