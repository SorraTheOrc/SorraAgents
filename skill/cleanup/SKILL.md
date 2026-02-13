---
name: cleanup
description: "Clean up completed work: inspect branches, update main, remove merged branches (local and optionally remote), reconcile work items, and produce a concise report. Trigger on queries like: 'clean up', 'tidy up', 'prune branches', 'housekeeping'."
---

# Cleanup Skill

Triggers

- "clean up"
- "tidy up"
- "cleanup"
- "housekeeping"

## Purpose

- Inspect repository branches and Worklog work items, identify merged or stale work, remove safely deletable branches, propose work item closures, and produce a concise report of actions and next steps.

## Required tools

- `git` (required)
- `wl` (Worklog CLI) — optional but recommended for work item metadata
- `gh` (GitHub CLI) — optional for PR summaries

Scripts (implementation)

- The skill ships a set of deterministic scripts under `skill/cleanup/scripts/` that implement the non-interactive behaviour described below. These scripts are the canonical implementation for automation and CI:
  - `skill/cleanup/scripts/prune_local_branches.py`
  - `skill/cleanup/scripts/cleanup_stale_remote_branches.py`
  - `skill/cleanup/scripts/reconcile_worklog_items.py`
  - `skill/cleanup/scripts/run_cleanup.py` (aggregator)

Each script supports `--dry-run`, `--yes`, `--report <path>`, `--quiet`, and `--verbose`.

## Runtime flags (recommended)

- `dry-run` (default): list actions without performing deletes or closes
- `confirm_all`: allow single confirmation to apply a group of safe actions

## Preconditions & safety

- Never rewrite history or force-push without explicit permission.
- Default protected branches: `main`, `develop` (do not delete or target for deletion).
- Detect default branch dynamically when possible (check `git remote show origin` or fallback to `main`).
- Use conservative merge checks (`git merge-base --is-ancestor`) to determine whether a branch's HEAD is contained in the default branch.

Integration notes

- Before deleting remote branches, the scripts attempt to detect open PRs targeting the default branch using `gh pr list --state open --base <default> --json headRefName,url`. If `gh` is not available the scripts will log a warning and skip remote deletion unless explicit opt-in is provided.
- Branch reports produced by the scripts include parsed work item tokens (branch name parsing) and — when `wl` is available — an enrichment section with `wl show <id> --json` output (title, status, assignee).

## High-level Steps

1. Inspect current branch

- Show current branch: `git rev-parse --abbrev-ref HEAD`.
- Detect default branch (recommended): `git remote show origin` and parse "HEAD branch". Fallback to `main`.
- If current branch is not the default branch:
  - Fetch remote: `git fetch origin --prune`.
  - Check whether current branch is merged into `origin/<default>`:
    - `git merge-base --is-ancestor HEAD origin/<default>` (exit code 0 => merged)
  - If not merged: present summary (branch name, last commit, unpushed commits, associated work item) and ask user: keep working / open PR / merge / skip deletion.
  - If merged and user permits (or in `confirm_all`), allow continuing to default branch.

2. Switch to default branch and update

- `git fetch origin --prune`.
- `git checkout <default>`.
- `git pull --ff-only origin <default>` (if fast-forward fails, report and ask).

3. Summarize open PRs targeting default

- If `gh` available: `gh pr list --state open --base <default> --json number,title,headRefName,url,author`.
- Present any open PRs and their head branches; skip deleting branches that have open PRs unless user explicitly authorizes.

4. Delete local merged branches

- List local branches merged into `origin/<default>` using conservative check per branch:
  - For each branch `b` (excluding protected names and current):
    - `git merge-base --is-ancestor b origin/<default>` (exit code 0 => merged)
- Present branch deletion list with metadata: last commit date, upstream (if any), work item id (if parseable), and open PR presence.
- If not `dry-run` delete branches: `git branch -d <branch>` (safe delete). If `-d` fails, report and offer `-D` only with explicit permission.

Example (script invocation)

```bash
# Dry-run and produce JSON report
python skill/cleanup/scripts/prune_local_branches.py --dry-run --report reports/cleanup/local.json

# Run aggregator in dry-run
python skill/cleanup/scripts/run_cleanup.py --dry-run --report reports/cleanup/combined.json
```

5. Delete remote merged branches

- For each deleted or candidate local branch with a remote `origin/<branch>`:
  - Verify no open PR references it and that it is merged (use `git merge-base --is-ancestor origin/<branch> origin/<default>`).
  - Present branch deletion list with metadata: last commit date, upstream (if any), work item id (if parseable), and open PR presence.
  - If not `dry-run` delete branches: `git push origin --delete <branch>`.

6. Summarize remaining branches

- Produce a table of remaining local and remote branches with: name, upstream, last commit, merged? (yes/no), work item id (if any), and open PR links (if available).
- For each remaining branch, offer actions: keep / delete / create PR / assign work item / rebase / merge.

7. Temporary File Rremoval

- If any temporary files were created (e.g., branch lists, reports), remove them to avoid clutter.

8. Final report

- Produce concise report including:
  - Branches deleted (local + remote)
  - Branches kept and reasons
  - Work items closed
  - Any operations skipped or requiring manual intervention
- Offer to save report: `history/cleanup-report-<timestamp>.md` (write only with confirmation).

Branch ↔ Work item mapping

- Parse branch names for work item tokens using the project's convention: `<prefix>-<id>/...` (example: `wl-123/feature`).
- If found: `wl show <id> --json` to include title, status, priority, and comments.
- If not found: flag branch for manual review and present guidance for associating to a work item.

Commands (examples)

- Detect default: `git remote show origin` or `git symbolic-ref refs/remotes/origin/HEAD`.
- Conservative merge check: `git merge-base --is-ancestor <branch> origin/<default>`.
- List branches for manual checking: `git for-each-ref --format='%(refname:short) %(committerdate:iso8601)' refs/heads/`.
- PR summary: `gh pr list --state open --base <default> --json number,title,headRefName,url,author`.
- Worklog: `wl list --status in_progress --json`, `wl show <id> --json`, `wl close <id> --reason "Completed" --json`, `wl sync`.

Safety prompts (always asked)

- If default branch cannot be fast-forwarded, ask how to proceed (pause or abort).

Outputs

- Human-readable summary printed to terminal.

Example short dialogue

- Agent: "I can inspect merged branches and propose deletions in dry-run mode. Shall I proceed? (yes/no)"
- User: "Yes — run dry-run and show candidates, do not delete remotes."
- Agent: "Dry-run complete. Candidate local deletions: A, B. Remote candidates: C. Would you like to delete local A, B? (yes/no)"

Notes for operators

- Skill assumes `wl` and `gh` may be available; proceed gracefully if not.
- Follow AGENTS.md work item/branch naming conventions. If conventions differ, flag branches for manual mapping.
- Do not create or modify commits, force-push, or change remote configuration without explicit permission.

End.
