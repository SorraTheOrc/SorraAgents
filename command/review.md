---
description: Run an automated PR-focused review in an Ampa pool container.
agent: probe
tags:
  - command
  - review
---

You are coordinating an automated PR-focused review for an existing pull request.

## Description

You are reviewing a pull request and producing a concise, actionable result for the PR and its associated Worklog item.

You will follow a deterministic review workflow: inspect the PR metadata, identify the canonical work item, start an isolated Ampa pool container for that work item, check out the PR branch inside the container, run repository checks, summarize the outcome, record follow-up work where needed, and return the container to the Ampa pool lifecycle.

## Inputs

- The supplied <pr-ref> is $1.
  - The command requires a valid PR reference (PR number or PR URL). If no PR reference is provided, stop and ask the user to provide one.
- Optional additional freeform arguments may be provided to guide the review. Freeform arguments are found in the arguments string "$ARGUMENTS" after the <pr-ref> ($1).

## Results and Outputs

- A 1-2 sentence headline summary of the review outcome.
- A concise PR comment summarizing the test outcome and audit outcome.
- A Worklog comment on the canonical work item for the PR.
- 0 or more new refactor or follow-up Worklog items for non-blocking improvements when warranted.
- Release of the claimed review container back to the Ampa pool lifecycle after the review is complete.

## Behavior

The command implements the procedural workflow below. Each numbered step is part of the canonical execution path; substeps describe concrete checks or commands that implementors or automation should run.

## Hard requirements:

- Do not merge the PR, push commits, or otherwise modify remote state beyond comments and Worklog updates required by the review.
- Do not proceed without a PR reference.
- Do not modify the host workspace checkout as part of the review flow.
- Only operate inside the claimed review container once it is started, and do not manipulate containers manually outside of Ampa commands.
- Ensure a pool container is available before starting review work. If no container is available, run `wl ampa warm-pool` and retry.
- Determine a canonical Worklog id conservatively and record any ambiguity rather than guessing.
- Do not start the container review session until a canonical Worklog id is known, because `wl ampa start-work` is work-item scoped.
- If multiple different Worklog ids are found, call out the mismatch and state which id was selected.
- Do not create or close Worklog items without clear, testable purpose; use follow-up items only for concrete non-blocking improvements.
- Do not print secrets or copy sensitive file contents into comments.
- Keep review output concise and actionable; prefer one aggregate comment over many small comments.

## Process (must follow)

1. Gather PR context (agent responsibility)

- Resolve the supplied <pr-ref> into a PR number or PR URL that can be used consistently in downstream commands.
- Read key PR metadata before starting any container session:
  - PR title
  - PR body
  - changed files
  - branch/head information
- Example commands:
  - `gh pr view "$ARGUMENTS" --json number,title,body,headRefName,headRepository --jq '{number, title, body, headRefName, headRepository: .headRepository.owner.login + "/" + .headRepository.name}'`
  - `gh pr view "$ARGUMENTS" --json files --jq '.files[].path'`
- Output clearly labelled lists with single line summaries when useful:
  - "Changed files"
  - "Potentially related work items"
  - "Potentially related docs"
- Read and summarize any directly relevant artifacts needed to interpret the review results.

2. Determine canonical work item id (agent responsibility)

- Extract a Worklog id and treat it as the canonical work item for the review container, audit, and comments.
- Check these sources in precedence order and stop at the first match:
  1. PR title
  2. PR head branch name
  3. PR description/body
  4. Any issue the PR closes or references
- Read the project Worklog prefix from `.worklog/config.yaml` and use that prefix when matching work item ids.
- Use a conservative regex built from the configured prefix. With the current `prefix: SA`, example patterns are `SA-[0-9]+` and `SA[0-9]+`.
- If multiple different ids are found, post a short PR comment calling out the discrepancy and state which id was chosen using the precedence above.
- If no canonical Worklog id is found, stop and ask the user to provide one before continuing.
- Example commands:
  - `prefix=$(awk -F': ' '/^prefix:/ {print $2}' .worklog/config.yaml)`
  - `title=$(gh pr view "$ARGUMENTS" --json title -q .title)`
  - `body=$(gh pr view "$ARGUMENTS" --json body -q .body)`
  - `branch=$(gh pr view "$ARGUMENTS" --json headRefName -q .headRefName)`
  - `extract_wl_id() { printf '%s\n' "$1" | grep -o -E "${prefix}-?[0-9]+" | head -n1; }`
  - `work_item_id=$(extract_wl_id "$title"); [ -n "$work_item_id" ] || work_item_id=$(extract_wl_id "$branch"); [ -n "$work_item_id" ] || work_item_id=$(extract_wl_id "$body")`

3. Ensure a Review child work item exists (agent responsibility)

  - Confirm the canonical work item has a child titled "Review PR #<pr number>". This child is used to track the specific review task and record audit comments if desired.
  - If the child does not exist, create it as a child of the canonical work item with a brief body, `critical` priority, and `chore` issue-type.
  - Example commands (assumes `work_item_id` and `pr_number` variables are set from earlier steps):
    - Find an existing child and capture its id as `review_item_id` if present:
      - `review_item_id=$(wl show "$work_item_id" --json | jq -r --arg title "Review PR #${pr_number}" '.children[]? | select(.title==$title) | .id' | head -n1)`
    - If not found, create the child and capture the new id into `review_item_id`:
      - `if [ -z "$review_item_id" ]; then create_out=$(wl create -t "Review PR #${pr_number}" -d $'Run the /review command over the pr #${pr_number}' --priority critical --issue-type chore --parent "$work_item_id" --json) && review_item_id=$(printf '%s' "$create_out" | jq -r '.id'); fi`


4. Start review container (agent responsibility)

- Start an isolated work container for the review item:
  - `wl ampa start-work <review-item-id>`
- Find the container name:
  - `container_name=$(wl ampa list-containers --json | jq -r --arg id "$WORK_ITEM" '.containers[] | select(.workItemId==$id) | .name')`
- Treat the claimed container as the only workspace for checkout and test execution;
- Example commands:
  - `wl ampa status --json` - to check for available containers and their status
  - `wl ampa warm-pool` - to warm the pool if no containers are available
  - `wl ampa start-work "$review_item_id"` - to (re)start the review container for the specific review item


5. Check out the PR inside the container (agent responsibility)

- Enter the claimed container and perform all Git operations there.
- Check out the PR head branch inside the container (do not use --login.-l).
  - `distrobox enter "$container_name" -- bash -c 'gh pr checkout "<pr-ref>"`

6. Audit the code against acceptance criteria (agent responsibility)

- Invoke the audit skill (do not use --login.-l)
  - `audit_output=$(distrobox enter "$container" -- bash -c 'cd /workdir/project && . /etc/ampa_bashrc && opencode run "audit <work-item-id> against the currently checked out branch"' 2>&1)`
- Do not post any details of the audit output in comments, we will only summarize the outcome in the final review comment and the Worklog item.
- Capture any critical audit findings as structured data for the final summary, but do not post them directly in comments.

7. Review the code (agent responsibility)
   
- Review the code using the code_review skill (do not use --login.-l):
  - `code_review_output=$(distrobox enter "$container" -- bash -c 'cd /workdir/project && . /etc/ampa_bashrc && opencode run "review code in the currently checked out branch using the code_review skill"' 2>&1)`
- Do not post any details of the code review output in comments, we will only summarize the outcome in the final review comment and the Worklog item.
- Capture any critical code review findings as structured data for the final summary, but do not post them directly in comments.

8. Discover and run the repository test suite (agent responsibility)

  - Read the README to establish the test commands used in the project
    - If necessary detect common test commands 
  - Run tests inside the claimed container, capturing full output and a short pass/fail summary (do not use --login.-l):
    - `test_output=$(distrobox enter "$container" -- bash -c 'cd /workdir/project && . /etc/ampa_bashrc && <test-command>' 2>&1)`
  - If tests are long-running or flaky, consider running them with a timeout and capturing only the failing tests for the summary.
  - Do not post any details of the test output in comments, we will only summarize the outcome in the final review comment and the Worklog item.
  - Capture any critical test failures as structured data for the final summary, but do not post them directly in comments.


9. Post results (agent responsibility)

- Post summary report, in the format below, to the PR.
  ```markdown
  This PR is not ready to merge due to critical test failures and code issues that need to be addressed.

  - Tests: 309 passed, 1 failed. 
  - Audit: 1 critical issues, 1 improvement opportunity. 
  - Code Review: 1 critical issue, 1 smells, 1 nitpick. 

  ### Critical/Blocking issues
  - "Test failure in <test-file>: <short description of failure>."
  - "Acceptance Criteria not met: <short description>. Suggested fix: <short suggestion>."
  - "Critical bug in <file> related to <issue>. Suggested fix: <short suggestion>."

  ### Non-blocking issues
  - "Improvement opportunity in <file> related to <issue>. Suggested improvement: <short suggestion>."
  - "Code smell in <file> related to <issue>. Suggested improvement: <short suggestion>."
  - "Nitpick: <short description of minor issue>."
  ```
- Keep the summary short enough to post directly to the PR and Worklog without further editing.
  - Do not include the full test output or audit output in the comments
- Only escape backticks in comment content.
  
10.   Cleanup (must follow)

- Close the review item with a comment containing the review summary.
- Exit the review session in the claimed container by running `wl ampa finish-work <work-item-id>`.
  - This will sync worklog edits and release the container back to the Ampa pool lifecycle and make it available for other work items.
- Do not merge the PR.
- Do not close the associated Worklog item.
- Output the review summary for the user.
  
## Traceability & idempotence

- When the command posts comments or creates follow-up items, it should avoid duplicate comments and duplicate follow-up work where the same review has already been recorded.
- Any canonical work item detection should be reproducible from the same PR metadata and precedence rules.
- The same PR and work item should not leave duplicate active review containers when the command is rerun.

## Editing rules & safety

- Preserve author intent in PR comments and Worklog entries; summarize findings without overstating certainty.
- Keep automated comments minimal and conservative.
- Respect ignore rules and avoid quoting ignored or sensitive files.
- Prefer Ampa CLI commands over manual Podman or Distrobox manipulation when both can accomplish the same step.
- If an automated step fails or a repository command is ambiguous, surface an explicit open question or failure note rather than guessing.

