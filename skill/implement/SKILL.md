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
- Do not use search tools such as grep, ripgrep, or code search in the implementation process. Rely on the context provided in the work item, linked documentation, and your understanding of the codebase. If you find that you do not have enough context to implement, use the intake interview to gather more information and update the work item before proceeding.
- Keep implementation focused on meeting acceptance criteria with minimal changes.
- Never edit code outside of the src/, tests/ and docs/ for this project unless they are essential configuration files.
- Never edit code in bundled libraries such as dist/ and node_modules/.
- When implementing a CLI or API always provide a way to obtain a JSON formatted output for agents to consume.
- Use work item comments to document your process, decisions, and next steps.
- Handle errors gracefully and provide actionable messages for remediation.
- If the work item is not well-defined, do not proceed with implementation. Instead, run the intake interview to clarify and update the work item before implementing.
- If the work item has blockers or dependencies, implement those first before proceeding with the main work item.
- Never commit directly to `main`. Always create a feature or bug branch for implementation.
- When implementing a CLI or API always provide a way to obtain a JSON formatted output for agents to consume.
- When creating branches, include the work item id in the branch name for traceability (e.g., `feature/WL-123-add-auth`).
- When creating a commit message, review the diff and write a concise message summarizing the changes made and the reason for the change, referencing the work item id.
- When committing add a comment to the work item with the commit message and hash.
- Do NOT create a Pull Request to `main`. Work is integrated into `dev`; the `dev`→`main` promotion is handled separately by the release process.
- When writing work-item comments or commit messages, include a concise summary of the goal, work done, and any important review notes.
- Do not escape content in commit messages or work-item descriptions; use markdown formatting as needed for clarity and readability.
- After implementation is complete and the work-item is in `in_review`, use the cleanup skill to tidy up local feature branches. Do not clean up `dev` or `main`.

## Handling Assets

- If the implementation requires the creation of assets such as graphics or audio files, create these assets in an appropriate subfolder of the `assets` directory (e.g., `assets/images/`, `assets/audio/`) and use a name that has the prefix "placeholder_" followed by a descriptive name (e.g., `placeholder_player_explosion_spritesheet.png` or `placeholder_player_jump.wav`).
  - always reference new assets in the work item comments and PR description. Ensure that any generated assets are included in the commit and pushed to the repository.
  - when creating assets, ensure they are optimized for size and performance, and follow any project guidelines for asset creation and management.
  - you can discover assets on the web as part of your implementation, but ensure that you have the right to use and distribute any assets you include in the project. Always provide proper attribution if required by the asset's license.
- If the implementation requires changes to documentation, update the relevant markdown files in the `docs` directory and reference these changes in the work item comments and PR description.
  - ensure that documentation changes are clear, concise, and accurately reflect the implementation changes. Include examples or screenshots if they help clarify the documentation.

## Steps

Execute the following steps in order. Do not skip steps. Use the live commands where applicable and record outputs in the work-item comments as you proceed.

1. Safety gate: handle dirty working tree

- Inspect `git status --porcelain=v1 -b`.
- If uncommitted changes are limited to `.worklog/`, carry them into the new working branch and commit there.
- If other uncommitted changes exist, pause and present explicit choices: carry them into the work item branch, commit first, stash (and optionally pop later), revert/discard (explicit confirmation), or abort. If abort is chosen, first run `wl update <work-item-id> --status open --json` to mark the item as open.

1. Understand the work item

- If the work item is not already assigned to you, claim it by running `wl update <work-item-id> --status in_progress --stage in_progress --assignee "<AGENT>" --json` (omit `--assignee` if not applicable).
- After you have the work item id, first look for the most recent worklog action, comment, or audit entry associated with the work item.
  - If the most recent action on the work item is a recent audit record, reuse that audit to understand what work has already been done and what remains.
  - If there is no recent audit record, run a full audit using `/skill:audit <work-item-id>` to understand what work has already been done and what remains.
- Fetch the work item JSON if not already present: `wl show <work-item-id> --json` and `wl show <work-item-id> --json`.
  - pay particular attention to the `description`, `acceptance criterria` and `comments`
- Restate acceptance criteria and current status along with any constraints from the work item JSON.
- Surface blockers, dependencies and missing requirements.
- Inspect linked PRDs, plans or docs referenced in the work item.
- Confirm expected tests or validation steps.

1.1. Definition gate (must pass before implementation)

- Verify:
  - Clear scope (in/out-of-scope).
  - Concrete, testable acceptance criteria.
  - Constraints and compatibility expectations.
  - Unknowns captured as explicit questions.
- If the work item fails the definition gate, first run `wl update <work-item-id> --status open --json` to mark the item as open, then take the appropriate action:
  - If the work item is not well-defined, run the intake interview to update the existing work item (see `command/intake.md`) and update the work item `description` or `acceptance` fields with the intake output.
  - If the work item is too large to implement in one pass, run plan interview (see `command/plan.md`) to break it into smaller work items, create those work items, link them as blockers/dependencies, and pick the highest-priority work item to implement next.
  - If you ran the intake interview, update the current work item with the new definition and inform the user of your actions and ask if you should restart the implementation review.
  - If you ran the plan interview, convert this work item to an epic and inform the user that implementation should move to the first child work item created.
- If you ran the intake interview, update the current work item with the new definition and inform the user of your actions and ask if you should restart the implementation review.

1. Create a working branch

- Inspect the current branch name via `git rev-parse --abbrev-ref HEAD`.
- If the current branch was created for a work item that is an ancestor of <work-item-id>, continue on that branch (that is if the name has an ancestor work item id).
- Otherwise create or switch to a branch named `feature/<work-item-id>-<short>` or `bug/<work-item-id>-<short>` (include the work item id).
- Never commit directly to `main`.

1. Implement

- If the work item has any open or in_progress blockers or dependencies:
  - Select the most appropriate work item to work on next (blocker > dependency; most critical first).
  - Claim the work item by running `wl update <work-item-id> --status in_progress --stage in_progress --assignee "<AGENT>" --json`
  - Recursively implement that work item as described in this procedure.
  - When a work item is completed, follow the mandatory build → test → commit order: first build the project and verify no errors, then run all tests and verify they pass, and only then commit the work. Update the stage: `wl update <work-item-id> --status in_progress --stage in_review --json`

- If the work item has a recent audit record, review the audit notes and address any unmet acceptance criteria or other issues identified.
- If there is no recent audit record, run `/skill:audit <work-item-id>` and use the resulting audit output to establish the work that needs to be done.
- Once the audit selection is complete, continue to step 4 and write tests and code to ensure all acceptance criteria defined in or related to the current work item are met:
  - Make minimal, focused changes that satisfy acceptance criteria.
  - Follow a test-driven development approach where applicable.
  - Ensure code follows project style and conventions.
  - Add comments to the work item describing any significant design decisions, code edits or tradeoffs.
  - If additional work is discovered, create linked work items: `wl create "<title>" --deps discovered-from:<work-item-id> --json`
- Once all acceptance criteria for the primary work item and all blockers and dependents are met:
  - Build the project and verify the build completes without errors.
  - Run the entire test suite using the shared quiet test helper or the project's quiet test command.
    - Report the results.
    - Fix any failing tests before continuing.
    - If the test run discovers failing tests that appear to be outside the scope or ownership of the current work item (e.g., failures in files not modified by this branch), invoke the triage helper with `parent_work_item_id` to create a **blocking child work item**:
      - Example: `python3 skill/triage/scripts/check_or_create.py '{"test_name":"<name>", "stdout_excerpt":"...", "stack_trace":"...", "parent_work_item_id":"<this-work-item-id>"}'`
      - If `check_or_create` returns that it created a NEW critical issue, or matched an existing incomplete one, the agent should implement that child work item to fix the test failure, commit the fix, and re-run tests until all pass before proceeding.
      - If running under Ralph, failing tests are automatically handled: Ralph will create child work items, implement fixes, and ensure all tests pass before marking the parent as `in_review`.
  - Update or create relevant documentation.
  - Summarize changes made in the work item description or comments.
  - Do not proceed to the next step until the user confirms it is OK to do so.

1. Automated self-review

- Build and lint the code to catch basic issues, fix any issues raised before proceeding.
- Run all tests again using quiet test commands to ensure nothing is broken, fix any failing tests before proceeding.
- Audit the work item to confirm all acceptance criteria are met: `audit <work-item-id> using the audit skill`.
  - If the audit reveals any unmet acceptance criteria, inform the user of the findings and return to step 3 to address them.
- Perform sequential self-review passes: completeness, dependencies & safety, scope & regression, tests & acceptance, polish & handoff.
- For each pass, produce a short note and limit edits to small, goal-aligned changes. If intent changes are discovered, create an Open Question and stop automated edits.
- Run the entire test suite using the shared quiet test helper or quiet project commands.
  - Fix any failing tests before continuing.

1. Commit, Push to dev and mark in_review

- Before committing, follow the mandatory build → test → commit order: build the project and verify no errors, then run all tests and verify they pass, and only then commit changes.
- Ensure all work has been committed on the feature branch.
- Do NOT create a Pull Request to `main`. Work is integrated into `dev`; the `dev`→`main` promotion is handled separately by the release process.
- Push the feature branch into `dev` using one of the following:
  - Using the ship skill: `pushToDev()` from `skill/ship/scripts/ship.js` (preferred)
  - Direct git command: `git push origin HEAD:refs/heads/dev`
  - The push target `dev` is **not** a protected branch; the `.githooks/pre-push` hook only blocks `main`, `master`, and `HEAD`.
- After pushing, switch to the `dev` branch locally and pull the latest:

  ```bash
  git checkout dev
  git pull origin dev
  ```

  This ensures subsequent operations begin from the current HEAD of the integration branch.
- Add a work-item comment recording the commit hash and that the work has been pushed to dev:
  `wl comment add <work-item-id> --comment "Completed work pushed to dev, see commit <hash>. The work-item stays open until the release process merges dev to main." --author "<AGENT>" --json`
- Close your response to the operator with a suggested commit message:
  `If you want to commit this work now I suggest the following commit message:\n\n<work-item-id>: <concise-summary-of-changes>`

  > **Note:** When running under **Ralph** (the target work item's stage is `in_progress` or `plan_complete`), **do NOT** mark the work item as `in_review`. Ralph will handle the stage transition after the audit passes. When running manually (not under Ralph), mark the work item as `in_review` after pushing to dev:

  > **When running under Ralph:** Skip the `wl update --stage in_review` step. Ralph will mark the item as `in_review` after a successful audit.
  > **When running manually:** Mark the work item as `in_review` (do **NOT** close it):
  `wl update <work-item-id> --stage in_review --json`

  > **Important:** The work-item is **not closed** at this stage. It remains `in_review` until the release process promotes `dev` to `main`. Agents may perform the release by invoking the Ship skill's release command (`skill/ship/scripts/run-release.js`), or a Release Manager may perform it manually. Agents should not push directly to `main` unless explicitly authorized.
  > See `skill/ship/SKILL.md` for the push-to-dev workflow and `skill/ship/scripts/run-release.js` (safe wrapper) for the release process. The wrapper detects when a repository lacks `scripts/release/merge-dev-to-main.sh` and prints a clear human fallback message.

Pre-push blocking check
-----------------------

- Before pushing to `dev`, the Ralph orchestration loop automatically ensures all tests pass. If tests fail:
  1. Child work items are created via triage helper (with `parent_work_item_id`)
  2. Fixes are implemented via `implement-single`
  3. Tests are re-run until all pass
  4. Only then does Ralph proceed with the push
- When running manually (not under Ralph), the agent should manually invoke the triage helper and fix any failing tests before pushing.

## Status Transition Matrix

The following table documents the expected status and stage transitions at each workflow phase for the `implement` skill.

| Phase | Command | Status | Stage |
|-------|---------|--------|-------|
| Start (Step 1 - Claim) | `wl update <id> --status in_progress --stage in_progress --assignee "<AGENT>" --json` | in_progress | in_progress |
| Blocker complete (Step 4) | `wl update <id> --status in_progress --stage in_review --json` | in_progress | in_review |
| Final (Step 6 - Mark in_review) | `wl update <id> --stage in_review --json` | in_progress | in_review |
| Abort - dirty work tree (Step 0) | `wl update <id> --status open --json`, then abort | open | (unchanged) |
| Abort - definition gate failure | Run intake/plan interview, update item | open | (unchanged) |
| Under Ralph (Step 6 note) | Skip in_review step; Ralph handles transition | in_progress | in_progress |

Abort/failure transitions use `--status open` while keeping the stage unchanged.

## Scripts (canonical runner & modules)

This skill does not ship a single orchestrator script. Implementation is carried out by following the steps above and invoking project-local build/test and linters. When a repository provides an "implement" helper script, prefer that script for deterministic behavior.

Example Worklog-oriented commands using SA-0MPYMFZXO0004ZU4 (documentation example):

```bash
# Fetch the work item
wl show SA-0MPYMFZXO0004ZU4 --json

# After implementing locally, push to dev (preferred via ship skill):
# (JS example) node -e "require('./skill/ship/scripts/ship.js').pushToDev('origin')"
# or direct push
git push origin HEAD:refs/heads/dev

# Mark in_review
wl update SA-0MPYMFZXO0004ZU4 --stage in_review --json
```

End.
