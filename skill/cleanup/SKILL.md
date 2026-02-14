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

## Preferred execution behaviour (policy)

- By default the agent MUST run the repository's official cleanup scripts listed in this document (for example, `inspect_current_branch.py`, `switch_to_default_and_update.py`, `summarize_branches.py`, `prune_local_branches.py`, `delete_remote_branches.py`). The agent SHOULD NOT substitute its own ad-hoc git commands for these scripts during normal operation.
- The agent may fall back to built-in git inspections or other local checks ONLY in narrowly defined edge cases and only after explicit human instruction. Edge cases include:
  - the expected script is missing or not executable,
  - the script fails with an unexpected error and the user explicitly asks the agent to attempt a local-git fallback,
  - the user explicitly requests a quick read-only inspection instead of running the scripts.
- Before running any repository-provided script the agent MUST verify:
  - the working tree is clean (no uncommitted changes) OR the human explicitly approves running with uncommitted changes (the agent must present the specific changed files and a clear warning), and
  - the target script file exists and is readable. If a script is present but its contents differ from a known-good checksum (if available), the agent MUST warn the human and obtain explicit approval to proceed.
- The agent MUST refuse to automatically run repository scripts when it detects potentially risky conditions (uncommitted changes, missing scripts, modified scripts) without explicit human confirmation.
- Rationale: preferring the canonical in-repo scripts improves consistency and auditability while the guardrails reduce risk from modified or missing scripts.

## Preconditions & safety

- Never rewrite history or force-push without explicit permission.
- Default protected branches: `main`, `develop` (do not delete or target for deletion).

## High-level Steps

1. Inspect current branch

Use `skill/cleanup/scripts/inspect_current_branch.py` to inspect the current branch, detect the default branch, fetch `origin --prune` when needed, determine merge status, last commit, unpushed commits, and parse work item token. The agent MUST run this script by default and only perform inline git inspections if an edge case (see "Preferred execution behaviour") applies and the human instructs it to.

The report includes `requires_interaction`, `recommended_action`, and `interactive_prompt` which indicates whether to ask a user question or proceed.

If there are any uncommitted, or unpushed changes the script will flag the branch for manual review. The agent MUST present the script's structured report to the human and provide sensible options with a recommendation based on the state (e.g., "Branch has unpushed commits. Would you like to push, stash, or skip?"). The agent should NOT proceed without human approval when uncommitted changes are present unless the human explicitly authorises it.

If you offer options are offered to the user one of those options MUST be to use the audit skill to review the branch in more detail before proceeding. If the user chooses to review with the audit skill, present the report to the user and offer options for next steps.

Examples:

```bash
python skill/cleanup/scripts/inspect_current_branch.py --report /tmp/cleanup/inspect_current.json
```

2. Switch to default branch and update

Run `skill/cleanup/scripts/switch_to_default_and_update.py` to fetch, check out the default branch, and perform a fast-forward pull. The agent MUST run this script by default (see Preferred execution behaviour) and only attempt manual git switch/pull sequences when explicitly instructed by the human in an allowed edge case.

If the pull fails (e.g., due to conflicts), the script will report the issue and prompt for user intervention.

Example:

```bash
python skill/cleanup/scripts/switch_to_default_and_update.py --report /tmp/cleanup/switch_default.json
```

3. Summarize branches and open PRs

Run `skill/cleanup/scripts/summarize_branches.py` to list local branches and include any open PRs targeting the default branch. The agent MUST run this script by default and present the script-generated report to the human for any deletion decisions.

For branches with open PRs, present the PR details and skip deletion unless explicitly authorized.

Example:

```bash
python skill/cleanup/scripts/summarize_branches.py --report /tmp/cleanup/branches.json
```

4. Delete local merged branches

Use `skill/cleanup/scripts/prune_local_branches.py` with an explicit branch list derived from the summarize report and user input. The summarize report and user choice are the authoritative source; the prune script only deletes branches you pass in. The agent MUST NOT delete branches outside of the explicit branch list produced by the script and approved by the human.

Example:

```bash
# delete branches identfied by the previous step
python skill/cleanup/scripts/prune_local_branches.py \
  --branches-file /tmp/cleanup/branches_to_delete.json \
  --report /tmp/cleanup/prune_local.json

# Dry-run and produce JSON report
python ./scripts/prune_local_branches.py --dry-run \
  --branches-file /tmp/cleanup/branches_to_delete.json \
  --report /tmp/cleanup/local.json
```

5. Delete remote merged branches

Run `skill/cleanup/scripts/delete_remote_branches.py` — deletes remote branches that are merged into default and older than a threshold (default 14 days). The agent MUST run this script by default when performing remote cleanup and must present a dry-run report before any destructive remote deletions.

Example:

```bash
# Delete all remote branches merged into default and older than 14 days
python skill/cleanup/scripts/delete_remote_branches.py --days 14 --report /tmp/cleanup/delete_remote.json

# Dry-run mode
python skill/cleanup/scripts/delete_remote_branches.py --days 14 --dry-run --report /tmp/cleanup/delete_remote.json
```

6. Handle edge cases and manual review:

Provide interactive options for handling remaining branches such as rebase, merge, create PR, or assign work item for any remaining branches. Where possible, provide guidance on next steps (e.g., "Branch X is not merged but has no open PR. Would you like to create a PR, rebase onto default, or assign to a work item?").

7. Temporary File Removal

If any temporary files were created (e.g., branch lists, reports), remove them to avoid clutter.

8. Final report

- Produce concise report including:
  - Branches deleted (local + remote)
  - Branches kept and reasons
  - Any operations skipped or requiring manual intervention

Safety prompts (always asked)

- If default branch cannot be fast-forwarded, ask how to proceed (pause or abort).

Outputs

- Human-readable summary printed to terminal.

End.
