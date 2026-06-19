---
name: find-related
description: Discover related work for a Worklog work item and generate a concise, auditable "Related work" report that can be appended to the work item description.
---

## Purpose

Provide a deterministic, agent-friendly way to discover existing or prior work related to a
work item. The skill searches Worklog (open + closed), inspects repository files and docs with
conservative heuristics, and can optionally generate an LLM-backed "Related work (automated report)"
that is inserted into the work item description under a clearly-marked section.

## When to use

- When a work item needs evidence of related or precedent work before planning or implementation.
- When the intake process wants to augment context with an automated report without replacing human authored intake drafts.
- When a user or agent asks questions like "has this been done before?", "what's related?", or "is there existing context I should be aware of?" in relation to a work item.

## Inputs

- work-item id (required)

## Outputs

- JSON summary printed to stdout, keys:
  - found (boolean)
  - addedIds (array of work item ids appended)
  - reportInserted (boolean)
  - updatedDescription (string)

## Status management

This skill manages the work item status during execution to signal that the item is being processed.

1. **Capture** the current status before making any changes: `wl show <id> --json` (extract the `status` field).
2. **Set** the status to `in_progress` at the start of execution: `wl update <id> --status in_progress`.
3. **Reset** the status to the original status at the end of execution (whether success or failure): `wl update <id> --status <original-status>`.

> Stage is NOT modified by this skill. Only `--status` is used.

## Decision logic

1. Fetch work item: `wl show <id> --json`.
2. If the description already contains related markers (e.g., `related-to:`) extract these as existing related items and carry them into the following steps.
3. Derive conservative keywords from the title/description/comments and run `wl search <keyword> --json` to collect candidates related issus.
4. For each candidate, fetch details with `wl show <id> --json` and review title, description, acceptance criteria, and comments to determine if it is truly related. Only include items that have clear relevance to the work item goals or context.
5. Use the `wl deps list <id> --json` command to identify any dependencies and include these in the list of repo matches to review for relevance.
6. Search repository documentation and code files for matching keywords; include those as repo matches.

- ignore data directories such as `node_modules`, `.git` and most "." named folders.

1. Produce a short informational report describing related work in the repository, using the previously discovered items as seeds. The report MUST:

- be clearly labeled and inserted under the heading "Related work (automated report)".
- include links to any related work items or docs discovered, along with their titles or file paths.
- Describe the relevance of each related item or doc in 1–2 sentences. This is the key value-add of the report, so it should not just be a list of links but should provide insight into why each item is related.

1. Update the item description by appending the generated report. Note, if the existing description already contains a related items report or markers, the new report can replace this content, but ONLY this content. Use `wl update` to perform the update.
2. Return the JSON summary.

## Hard requirements

- Default behaviour must be conservative: prefer false negatives over false positives when writing the report.
- Review each candidate item to ensure it is truly related before including it in the report. Do not include items that are only tangentially related or have low relevance.

## Scripts (canonical runner & modules)

This skill includes a bundled Python script that automates the related-work discovery
and report generation. The script runs the decision logic described above, updates the
work item description, and returns a structured result.

### Script location

`skill/find-related/scripts/find_related.py`

### Requirements

- Python 3.8+
- [Worklog CLI (`wl`)](https://github.com/sorrathec/worklog) installed and accessible on PATH

### Usage

```bash
# Basic usage (find related items for a work item and update its description)
python3 skill/find-related/scripts/find_related.py --work-item-id SA-0MPYMFZXO0004ZU4

# JSON output (for programmatic consumption)
python3 skill/find-related/scripts/find_related.py --work-item-id SA-0MPYMFZXO0004ZU4 --json

# Verbose mode (prints debug information to stderr)
python3 skill/find-related/scripts/find_related.py --work-item-id SA-0MPYMFZXO0004ZU4 --verbose

# Specify a custom repository path
python3 skill/find-related/scripts/find_related.py --work-item-id SA-0MPYMFZXO0004ZU4 --repo-path /path/to/repo
```

### Arguments

| Argument | Required | Default | Description |
|----------|----------|---------|-------------|
| `--work-item-id` | Yes | — | ID of the work item to find related items for |
| `--verbose` | No | `false` | Enable verbose debug output to stderr |
| `--json` | No | `false` | Output results as JSON |
| `--repo-path` | No | Auto-detected | Path to the repository root |

### Output (default mode)

```
Work item: SA-0MPYMFZXO0004ZU4
Related items found: 3
Repository matches: 2
Added IDs: REL-001, REL-002, REL-003
Report inserted: True
```

### Output (JSON mode)

```json
{
  "workItemId": "SA-0MPYMFZXO0004ZU4",
  "found": true,
  "addedIds": ["REL-001", "REL-002", "REL-003"],
  "reportInserted": true,
  "keywords": ["script", "automation", "related"],
  "relatedItemCount": 3,
  "repoMatchCount": 2
}
```

### Exit codes

| Code | Meaning |
|------|---------|
| `0` | Success — report was generated and description updated |
| `1` | Error — failed to fetch work item, or unexpected failure |

### Report format

The script generates a Markdown section under **Related work (automated report)**
that is appended to or replaces the existing automated report section in the
work item description. The report includes:

- A list of related work items with their IDs, titles, and statuses
- A list of repository file matches with file paths and matched keywords

### Idempotency

Re-running the script on the same work item is safe:

- The existing **Related work (automated report)** section is replaced, not duplicated
- Manual **Related work** sections (without the `(automated report)` suffix) are preserved

### Design notes

- The script is fully offline — it uses only the local Worklog CLI and repository file system
- It conservatively matches keywords (preferring false negatives over false positives)
- `.git`, `node_modules`, `__pycache__`, and similar directories are excluded from file scanning
- Only `.md`, `.py`, `.js`, `.mjs`, and `.txt` files are scanned for keyword matches

## End
