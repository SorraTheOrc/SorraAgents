---
description: Run an automated PR-focused review in an Ampa pool container.
agent: probe
tags:
  - command
  - review
---

Coordinate an automated PR-focused review for an existing pull request. Follow a deterministic workflow: inspect PR metadata, identify the canonical work item, start an isolated Ampa pool container, check out the PR branch, run checks, summarize the outcome, record follow-up work, and return the container.

## Inputs

- `$1` — PR reference (number or URL). Required; stop and ask if missing.
- `$ARGUMENTS` — Optional freeform arguments after `<pr-ref>` to guide the review.

## Results and Outputs

- A 1–2 sentence headline summary of the review outcome
- A concise PR comment summarizing test and audit outcomes
- A Worklog comment on the canonical work item for the PR
- 0+ new refactor/follow-up Worklog items for non-blocking improvements (when warranted)
- Release of the claimed review container back to the Ampa pool lifecycle

## Behavior

The command implements the procedural workflow below. Each numbered step is part of the canonical execution path; substeps describe concrete checks or commands to run.

## Hard requirements

- Do not merge the PR, push commits, or modify remote state beyond comments and Worklog updates.
- Do not proceed without a PR reference.
- Do not modify the host workspace checkout; operate only inside the claimed review container.
- Do not manipulate containers manually outside of Ampa commands.
- Ensure a pool container is available before starting (`wl ampa warm-pool` if none; retry).
- Determine a canonical Worklog id conservatively; record ambiguity rather than guessing.
- Do not start the container session until a canonical Worklog id is known (`wl ampa start-work` is work-item scoped).
- If multiple Worklog ids are found, call out the mismatch and state the selected id.
- Do not create/close Worklog items without clear, testable purpose; use follow-up items only for concrete non-blocking improvements.
- Do not print secrets or copy sensitive file contents into comments.
- Keep output concise and actionable; prefer one aggregate comment over many small ones.

## Process (must follow)

1. **Gather PR context** (agent responsibility)
   - Resolve `<pr-ref>` into a PR number/URL.
   - Read key PR metadata: title, body, changed files, branch/head info.
   - Examples:
     - `gh pr view "$ARGUMENTS" --json number,title,body,headRefName,headRepository --jq '{number, title, body, headRefName, headRepository: .headRepository.owner.login + "/" + .headRepository.name}'`
     - `gh pr view "$ARGUMENTS" --json files --jq '.files[].path'`
   - Output labelled lists: "Changed files", "Potentially related work items", "Potentially related docs".

2. **Determine canonical work item id** (agent responsibility)
   - Extract Worklog id (precedence: PR title > head branch > PR body > referenced issue).
   - Read prefix from `.worklog/config.yaml`; use conservative regex (e.g., `SA-[0-9]+`).
   - Multiple ids found? Post PR comment calling out the discrepancy; state chosen id.
   - No id found? Stop and ask user.
   - Examples:
     ```bash
     prefix=$(awk -F': ' '/^prefix:/ {print $2}' .worklog/config.yaml)
     title=$(gh pr view "$ARGUMENTS" --json title -q .title)
     body=$(gh pr view "$ARGUMENTS" --json body -q .body)
     branch=$(gh pr view "$ARGUMENTS" --json headRefName -q .headRefName)
     extract_wl_id() { printf '%s\n' "$1" | grep -o -E "${prefix}-?[0-9]+" | head -n1; }
     work_item_id=$(extract_wl_id "$title")
     [ -n "$work_item_id" ] || work_item_id=$(extract_wl_id "$branch")
     [ -n "$work_item_id" ] || work_item_id=$(extract_wl_id "$body")
     ```

3. **Manage work item status** (agent responsibility)
   - Start: `wl update <work_item_id> --status in_progress --json` (before any other step)
   - End: `wl update <work_item_id> --status open --json` (after cleanup)
   - Stage is NOT modified. Convention: `in_progress` → `open`.

4. **Ensure a Review child work item exists** (agent responsibility)
   - Confirm a child "Review PR #<pr-number>" exists. Create if missing (`critical`, `chore`, child of canonical work item).
   - Examples:
     ```bash
     review_item_id=$(wl show "$work_item_id" --json | jq -r --arg title "Review PR #${pr_number}" '.children[]? | select(.title==$title) | .id' | head -n1)
     if [ -z "$review_item_id" ]; then
       create_out=$(wl create -t "Review PR #${pr_number}" -d $'Run the /review command over the pr #${pr_number}' --priority critical --issue-type chore --parent "$work_item_id" --json)
       review_item_id=$(printf '%s' "$create_out" | jq -r '.id')
     fi
     ```

5. **Start review container** (agent responsibility)
   - `wl ampa start-work <review-item-id>`
   - `container_name=$(wl ampa list-containers --json | jq -r --arg id "$WORK_ITEM" '.containers[] | select(.workItemId==$id) | .name')`
   - Check: `wl ampa status --json`; warm pool: `wl ampa warm-pool`

6. **Check out the PR inside the container** (agent responsibility)
   - `distrobox enter "$container_name" -- bash -c 'gh pr checkout "<pr-ref>"'` (without `--login`/`-l`)

7. **Audit the code** (agent responsibility)
   - `audit_output=$(distrobox enter "$container" -- bash -c 'cd /workdir/project && . /etc/ampa_bashrc && opencode run "audit <work-item-id> against the currently checked out branch"' 2>&1)`
   - Do not post details in comments — only summarize outcome. Capture critical findings as structured data.

8. **Review the code** (agent responsibility)
   - `code_review_output=$(distrobox enter "$container" -- bash -c 'cd /workdir/project && . /etc/ampa_bashrc && opencode run "review code in the currently checked out branch using the code_review skill"' 2>&1)`
   - Do not post details in comments — only summarize outcome.

9. **Discover and run tests** (agent responsibility)
   - Read README to find test commands; prefer shared quiet test helper.
   - `test_output=$(distrobox enter "$container" -- bash -c 'cd /workdir/project && . /etc/ampa_bashrc && <quiet-test-command>' 2>&1)`
   - Consider timeout for long-running/flaky tests; capture only failures for the summary.
   - Do not post details in comments — only summarize outcome.

10. **Post results** (agent responsibility)
    Post summary report to the PR:

    ```markdown
    This PR is not ready to merge due to critical test failures and code issues that need to be addressed.

    - Tests: 309 passed, 1 failed. 
    - Audit: 1 critical issues, 1 improvement opportunity. 
    - Code Review: 1 critical issue, 1 smells, 1 nitpick. 

    ### Critical/Blocking issues
    - "Test failure in <test-file>: <short description>."
    - "Acceptance Criteria not met: <short description>. Suggested fix: <short suggestion>."
    - "Critical bug in <file>. Suggested fix: <short suggestion>."

    ### Non-blocking issues
    - "Improvement opportunity in <file>. Suggested improvement: <short suggestion>."
    - "Code smell in <file>. Suggested improvement: <short suggestion>."
    - "Nitpick: <short description>."
    ```

    Keep summary concise — postable directly to PR and Worklog. Only escape backticks.

11. **Cleanup** (must follow)
    - Close the review item with a comment containing the summary.
    - `wl ampa finish-work <work-item-id>` (syncs worklog, releases container)
    - Do not merge the PR or close the associated Worklog item.

## Traceability & idempotence

- Avoid duplicate comments and duplicate follow-up work when rerun.
- Canonical work item detection must be reproducible from the same PR metadata and precedence rules.
- The same PR and work item should not leave duplicate active review containers.

## Editing rules & safety

- Preserve author intent; summarize findings without overstating certainty.
- Keep automated comments minimal and conservative.
- Respect ignore rules; avoid quoting ignored or sensitive files.
- Prefer Ampa CLI commands over manual Podman/Distrobox manipulation.
- Surface explicit open questions or failure notes for ambiguous steps, rather than guessing.
