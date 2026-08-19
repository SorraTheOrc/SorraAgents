---
name: cleanup
description: "Clean up completed work: prune merged branches and report. Use when asked: 'clean up', 'prune branches'."
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

### 0. Work-item audit gate

Before any branch operations, determine whether the current branch is associated with a work item and, if so, verify its acceptance criteria are met via the audit skill. This gate applies to interactive skill invocations; scheduled non-interactive runs of the cleanup scripts are unaffected.

1. **Inspect the current branch.** Run `./scripts/inspect_current_branch.py --report /tmp/cleanup/inspect_current.json` (the same script used in Step 1) and read the `work_item_id` field from the JSON report.

   - **No `work_item_id`** (e.g. on `main` or a branch without a work-item token) → skip this step and proceed to Step 1.
   - **`work_item_id` present** → continue below.

2. **Invoke the audit skill.** Audit the work item using the existing audit skill — e.g. `/skill:audit <work-item-id>` or the canonical runner `python3 ./scripts/audit_runner.py issue <work-item-id>` (see `../audit/SKILL.md`). Do not implement audit logic here; reuse the audit skill as-is. For long-running audits, follow the audit skill's Monitored Run Execution contract.

3. **Apply the decision rule.** Read the audit report's `Ready to close:` verdict and its `## Acceptance Criteria Status` table (or `No acceptance criteria defined.` when none exist):

   - **`Ready to close: Yes`** (every criterion `met` or `adjusted`) → the work is verified complete:
     1. Transition the work item: `wl update <work-item-id> --status completed --stage in_review --json`
     2. Add a comment: `wl comment add <work-item-id> --comment "Cleanup audit passed on branch <branch-name>. Work item transitioned to in_review." --author <agent-name> --json`
     3. Proceed to Step 1.
   - **Any criterion `unmet` or `partial`** → **abort cleanup**. Output a clear message listing which criteria failed and their verdicts, referencing the audit report. Do not perform any branch operations — skip Steps 1-8 entirely. Suggest next steps (e.g. "Fix the unmet criteria and re-run cleanup").
   - **No acceptance criteria defined** → **abort cleanup**. Output a message explaining that the work item's completion cannot be verified and cleanup is aborted. Suggest adding acceptance criteria to the work item and re-running cleanup.
   - **No parseable verdict / audit failure** → **abort cleanup** (completion cannot be verified). Follow the audit skill's failure handling and report the outcome to the user.

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


## Final step: standardized end-of-session report

Render the canonical end-of-session report (helper: [`../report/SKILL.md`](../report/SKILL.md)) as the **last step**, replacing any ad-hoc end-of-session summary:

```bash
python3 ~/.pi/agent/skills/report/scripts/render_report.py <work-item-id> \
  --skill-name <skill_name> \
  --headline "<1-3 sentence headline summary>" \
  --ac "<AC# description>|<verification metric>|met" \
  --ac "<...>|<...>|unmet" \
  [--producer-actions "<actions for the producer, or omit for 'None needed'>"] \
  [--notes "<freeform context/caveats/assumptions>"] \
  [--next-action <review|plan|implement|...>]
```

Then close with: `<work-item-id>: <one-line summary>`. Do NOT re-summarize the report in a different format — the report is the summary.
