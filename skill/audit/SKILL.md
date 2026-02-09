---
name: audit
description: "Provide concise project / work item status and run Worklog helpers to augment results. Trigger on user queries such as: 'What is the current status?', 'Status of the project?', 'What is the status of <work-item-id>?', 'status', 'status <work-item-id>', 'audit', 'audit <work-item-id>'"
---

# Audit

## Overview

Provide a concise, human-friendly summary of project status or a specific work item. When no work item id is provided, run `wl` CLI tool to summarize recent work and current work in progress.

## When To Use

- User asks general project status (e.g., "What is the current status?", "Status of the project?", "status", "audit the project", "audit").
- User asks about a specific work item id (e.g., "What is the status of wl-123?", "status wl-123", "audit wl-123").

## Best Practices

- Output should be formatted as markdown for readability.
- When summarizing work items, focus on actionable information: current status, blockers, dependencies.
- When a work item id is provided, ensure to include all relevant details and related work items including dependencies (`wl dep list <work-item-id> --json`) and subtasks (`wl show <work-item-id> --json`) in the summary.
- Always conclude with a clear summary of the status.
  - For individual work items, the last line of the summary should be a clear statement about whether the work item can be closed or not.
- Do not recommend next steps or actions; this skill focuses on reporting only.

## Steps

1. Detect whether the user provided a work item id in the request.
2. If no work item id is provided complete this step, otherwise skip to step 3:

- Run `wl list --json` to fetch work items in JSON format to get more information, but do not display it.
- Present a one line summary of the overall project status based on the JSON data. Including:
  - Total number of critical and high priority work items.
  - Total number of open work items.
  - Total number of in_progress work items.
  - Total number of blocked work items.
- Present a summary of actively in_progress work items (`wl in-progress --json`). For each in_progress item, include: title, id, assignee, priority, and a one line summary of the description.

Skip to step 4.

3. If a work item id is provided:

- Run `wl show <work-item-id> --json` to fetch work item details (with all comments).
- Extracts the acceptance criteria from the description (they are usually in a markdown section starting with `## Acceptance Criteria` and formatted as a list).
  - For each acceptance criterion, provide a concise summary of whether it is met or unmet based on the work item description and comments and, where appropriate, code in the repository. If any criterion is unmet, provide a concise summary of what is missing.
- Walk through all child work-items (subtasks) and list each items title, id, status and stage.
- Walk through all dependencies (`wl dep list <work-item-id> --json`) and list each dependent work-item's status, title (using strike through if the item has a "completed" status), id, and stage.

4. Provide a final section titled "# Summary" containing the following optional items as applicable:

- If the item cannot be closed, this will be stated along with a summary of the work that needs to be completed before the item can be closed. If the item can be closed, skip this point.
- If there is an open PR request a review and provide the URL. If there is no open PR, skip this point.
- If the item can be closed (all acceptance criteria met, all children completed, PR merged) a recommendation to close will be included. If the item cannot be closed, skip this point.

DO NOT output anything after the summary section.

## Notes

- Keep the output concise and actionable for quick human consumption.
- Handle errors gracefully: if `wl` or any other command is not available or return invalid JSON, present a helpful error and possible remediation steps.
