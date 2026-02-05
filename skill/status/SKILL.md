---
name: status
description: "Provide concise project / work item status and run Worklog helpers to augment results. Trigger on user queries such as: 'What is the current status?', 'Status of the project?', 'What is the status of <work-item-id>?', 'audit', 'audit <work-item-id>'"
---

# Status

## Overview

Provide a concise, human-friendly summary of project status or a specific work item. When no work item id is provided, run `wl` CLI tool to summarize recent work and current work in progress. When a work item id is provided, run `wl show <work-item-id> --json` and provide a detailed explanation of that work item (title, status, assignee, description, blockers, and related links).

## When To Use

- User asks general project status (e.g., "What is the current status?", "Status of the project?", "audit the project", "audit").
- User asks about a specific work item id (e.g., "What is the status of wl-123?", "audit wl-123").
- The `/audit <work-item-id>` command is invoked.

## Behavior

1. Detect whether the user provided a work item id in the request.
2. Run git status to what branch we are on and whether there are uncommitted changes and include a note if any are found.
3. If no work item id is provided:

- Run `waif in_progress --json` to fetch in_progress work JSON format to get more information, but do not display it.
- Present a one line summary of the overall project status based on the JSON data.
- Present a summary of actively in_progress work items (ignore items that are open or closed). Start with the one deepest in the dependency chain and work upwards. Include the last updated date and a summary of the most recent comment if applicable.
- List the files referenced in the in_progress work items.

4. If a work item id is provided:

- Run `wl show <work-item-id> --json` and `wl show <work-item-id> --json` to fetch work item details (with all comments).
- Parse and present: title, status, assignee, priority, description, blockers, dependencies, summary of all comments, and relevant links.
- Walk through all open and in_progress subtasks, children, and blockers, summarizing their status as well.
  - Never skip any related work item that is open or in_progress.
- Make a very clear statement about whether the work item can be closed or not. If it cannot be closed, explain why (e.g., blockers, dependencies, incomplete tasks).

5. Provide numbered actionable next steps based on the status information.

- If no work item id is provided, always offer to run `audit <work-item-id>` (do not mention `wl show`) against the most important in_progress work item (show ID and title), add one or two alternative next actions relevant to the current status.
- If a work item id is provided, suggest appropriate next steps to complete the work item (if not already completed).
- Do not provide an alternative set of actions. There should only be 3 numbered next steps and a free-form response allowed.

6. Provide a final section titled "# Summary" containing the following optional items as applicable:

- If the item is blocked this will be stated and a list of work-items that are not yet completed and block this item will be provided. If the item is not blocked, skip this point.
- If there is an open PR a note requesting review and the URL for this PR will be returned. If there is no open PR, skip this point.
- If the item can be closed (all acceptance criteria met, all children completed, PR merged) a recommendation to close will be included. If the item cannot be closed, skip this point.
- If none of the above apply a note stating that the item is in good standing and no action is required at this time. If any of the above apply, skip this point.

DO NOT output anything after the summary section.

## Notes

- Keep the output concise and actionable for quick human consumption.
- Handle errors gracefully: if `wl` or any other command is not available or return invalid JSON, present a helpful error and possible remediation steps.
