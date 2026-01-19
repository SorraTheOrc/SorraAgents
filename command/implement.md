# implement

## Description

You are implementing a Beads issue identified by a provided id. You will fully implement this issue and all dependent work required to satisfy its acceptance criteria, following project rules and best practices.  

## Quick inputs

  - The beads issue id is $1.
  - Optional additional freeform arguments will be used to guide the implementation effots. Freeform arguments are found in the entire arguments string: "$ARGUMENTS".

## Behavior

The command implements the procedural workflow below. Each numbered step is part of the canonical execution path; substeps describe concrete checks or commands that implementors or automation should run.

## Prerequisites & project rules

- Use `bd` for ALL task tracking; do not create markdown TODOs.
- Keep changes between `git push` invocations minimal and scoped to the issue.
- Always run tests and validation before committing changes.
- Use a git branch + PR workflow (no direct-to-main changes).
- Ensure the working branch is pushed to `origin` before finishing.
- Do NOT close the Beads issue until the PR is merged.

Live context commands (use to gather runtime state)

- `bd show <bead-id> --json`
- `bd show <bead-id> --thread --refs --json`
- `git status --porcelain=v1 -b`
- `git rev-parse --abbrev-ref HEAD`
- `git remote get-url origin`

## Procedure

0) Safety gate: handle dirty working tree

- Inspect `git status --porcelain=v1 -b`.
- If uncommitted changes are limited to `.beads/`, carry them into the new working branch and commit there.
- If other uncommitted changes exist, pause and present explicit choices: carry them into the issue branch, commit first, stash (and optionally pop later), revert/discard (explicit confirmation), or abort.

1) Understand the issue

- Claim by running `bd update $1 --status in_progress --add-label "stage:in_progress" --assignee "@AGENT" --json` (omit `--assignee` if not applicable).
- Fetch the issue JSON if not already present: `bd show $1 --json` and `bd show $1 --thread --refs --json`.
- Restate acceptance criteria and constraints from the issue JSON.
- Surface blockers, dependencies and missing requirements.
- Inspect linked PRDs, plans or docs referenced in the issue.
- Confirm expected tests or validation steps.

1.1) Definition gate (must pass before implementation)

- Verify:
  - Clear scope (in/out-of-scope).
  - Concrete, testable acceptance criteria.
  - Constraints and compatibility expectations.
  - Unknowns captured as explicit questions.
- If the issue is not well-defined, run the intake interview to update the existing bead (see `command/intake.md`) and update the issue `description` or `acceptance` fields with the intake output.

2) Create a working branch

- inspect the current branch name via `git rev-parse --abbrev-ref HEAD`.
- If the current branch was created for a bead that is an ancestor of $1, continue on that branch (that is if the name has an ancestor bead id).
- Otherwise create a new branch named `feature/$1-<short>` or `bug/$1-<short>` (include the bead id).
- Never commit directly to `main`.

3) Implement

- If the bead has any open or in-progress blockers or dependencies:
  - Select te most appropriate bead to work on next (blocker > dependency; most critical first).
  - Claim the bead by running `bd update <bead-id> --status in_progress --add-label "stage:in_progress" --assignee "@AGENT" --json`
  - Recursively implement that bead as described in this procedure.
  - When a bead is completed commit the work and update the labels `bd update <bead-id> --add-label "stage:in_review" --json`
- Write tests and code to ensure all acceptance criteria defined in or related to the current bead are met:
  - Make minimal, focused changes that satisfy acceptance criteria.
  - Follow a test-driven development approach where applicable.
  - Ensure code follows project style and conventions.
  - Add comments to the bead describing any significant design decisions, code edits or tradeoffs.
  - If additional work is discovered, create linked beads: `bd create "<title>" --deps discovered-from:$1 --json`
- Once all acceptance criteria for the primary bead and all blockers and dependents are met:
  - Run the entire test suite.
    - Fix any failing tests before continuing.
  - Update or create relevant documentation.
  - Summarize changes made in the bead description or comments.
  - Do not proceed to the next step until the user confirms it is OK to do so.

4) Automated self-review

- Perform sequential self-review passes: completeness, dependencies & safety, scope & regression, tests & acceptance, polish & handoff.
- For each pass, produce a short note and limit edits to small, goal-aligned changes. If intent changes are discovered, create an Open Question and stop automated edits.
- Run the entire test suite.
    - Fix any failing tests before continuing.
- Commit the work and update the labels `bd update $1 --add-label "stage:in_review" --json`

5) Commit, Push and create PR

- Push the branch to `origin`.
- Create a Pull Request against the repository's default branch. 
  - Use a title in the form of "WIP: <bead-title> (b<bead-id>)" and a body that contains a concise summary of the goal and of the work done and reviewer instructions.
  - Link the PR to the bead via `--external-ref`.

6) Human PR review

- Notify the human reviewer(s) that the PR is ready for review.
- Address any review comments or requested changes.
- Once merged, proceed to the next step.

7) Cleanup

- Only take the following actions after the PR is merged:
- Close the bead and all its depdents and blockers by running `bd update <bead-id> --status done --add-label "stage:done" --remove-label "stage:in_review" --json`.
- Cleanup using the cleanup skill

## Exit codes & errors

- Exit non-zero when missing or invalid arguments.
- Error messages (verbatim where useful):
  - `Error: missing bead id. Run implement <bead-id>.`
  - `Error: bead $1 is not actionable — missing acceptance criteria. Run the intake interview to update the bead before implementing.`

S