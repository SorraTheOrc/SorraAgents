---
name: cleanup
description: "Clean up completed work: inspect branches, update main, remove merged branches (local and optionally remote), and produce a concise report. Trigger on queries like: 'clean up', 'tidy up', 'prune branches', 'housekeeping'."
---

# Cleanup Skill

Triggers

- "clean up"
- "tidy up"
- "cleanup"
- "housekeeping"

## Purpose

Inspect repository branches, identify merged or stale work, remove safely deletable branches, and produce a concise report of actions and next steps.

## Required tools

- `git` (required)
- `gh` (GitHub CLI) — optional for PR summaries

Scripts (implementation)

- The skill ships a set of deterministic scripts under `./scripts/` that implement the non-interactive behaviour described below. Each script supports `--dry-run`, `--yes`, `--report <path>`, `--quiet`, and `--verbose`.

## Policy

- **Prefer canonical scripts** over ad-hoc git commands. Run the repo's cleanup scripts (`inspect_current_branch.py`, `switch_to_default_and_update.py`, `summarize_branches.py`, `prune_local_branches.py`, `delete_remote_branches.py`) by default.
- **Fall back** to manual git only in edge cases (script missing/unexpected error) and only after explicit user instruction.
- **Refuse** to run scripts when risky conditions exist (uncommitted/modified scripts) without confirmation.
- **Offer audit skill** as an option when presenting choices to the user.

## Preconditions & safety

- Never rewrite history or force-push without explicit permission.
- Protected branches: `main`, `develop` — never target for deletion.

## Steps

### 1. Inspect current branch

Run `./scripts/inspect_current_branch.py --report /tmp/cleanup/inspect_current.json` to detect the default branch, merge status, uncommitted changes, and unpushed commits.

Display the report to the user before any prompts. Include: branch name, default branch, merge status, uncommitted changes, unpushed commits count+summary, and report file path. If no uncommitted/unpushed changes, skip to step 3.

### 2. Handle uncommitted/unpushed changes

Present the inspection report and offer options (push, stash, skip, or audit-branch review). Do not proceed without approval. If unresolvable, pause and guide the user.

### 3. Switch to default and update

Run `./scripts/switch_to_default_and_update.py --report /tmp/cleanup/switch_default.json` to fetch and fast-forward the default branch. If pull fails (conflicts), ask the user how to proceed — do not auto-resolve.

### 4. Summarize branches

Run `./scripts/summarize_branches.py --report /tmp/cleanup/branches.json` to list local branches and open PRs targeting default. Present the report for deletion decisions. Branches merged with no open PRs are deletion candidates; branches with unmerged commits or open PRs need explicit authorization.

### 5. Delete local merged branches

Run `./scripts/prune_local_branches.py --branches-file <file> --report /tmp/cleanup/prune_local.json` with an explicit branch list from the summarize report and user input. Never delete outside that list. Use `--dry-run` for preview.

### 6. Delete remote merged branches

Run `./scripts/delete_remote_branches.py --days 14 --report /tmp/cleanup/delete_remote.json` to delete remote branches merged into default and older than the threshold. Use `--dry-run` for preview.

### 7. Handle remaining branches

Offer interactive options for unmerged branches: rebase, merge, create PR, or assign to a work item. Provide guidance on next steps.

### 8. Clean up temp files and report

Remove temporary files. Produce a concise report: branches deleted (local + remote), kept (with reasons), and any skipped or manual-intervention items.

**Safety:** If default branch cannot be fast-forwarded, pause or abort.

**Output:** Human-readable summary.

## Worklog context

Fetch relevant work item context before cleanup decisions: `wl show <id> --json`. Include clear comments referencing branches and actions in any work item updates.

End.
