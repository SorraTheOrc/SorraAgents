---
name: find-related
description: Discover related work for a Worklog work item and generate a concise, auditable "Related work" report that can be appended to the work item description.
---

## Purpose

Discover related work for a work item via Worklog search, file inspection, and optionally generate a "Related work (automated report)" section.

## When to use

- Before planning or implementing: gather evidence of related/precedent work
- During intake: augment context with automated report
- When asked "what's related?" or "has this been done before?"

## Inputs / Outputs

**Input**: work-item id (required)

**Output**: JSON with keys `found`, `addedIds`, `reportInserted`, `updatedDescription`

## Status management

1. Start: `wl update <id> --status in_progress --json`
2. End: `wl update <id> --status open --json` (success or failure)

> Stage is NOT modified.

## Decision logic

1. Fetch item: `wl show <id> --json`
2. Extract existing related markers (`related-to:`)
3. Derive keywords from title/description/comments; `wl search <keyword> --json` for candidates
4. Review each candidate: fetch details, confirm true relevance
5. Check `wl deps list <id> --json` for dependencies
6. Search repo files (ignore `node_modules`, `.git`, most `.`-prefixed dirs)
7. Generate report under "## Related work (automated report)" with links and 1-2 sentence relevance descriptions
8. Update item description (replace existing automated report section)
9. Return JSON summary

**Policy**: Conservative — prefer false negatives over false positives. Only include truly related items.

## Script

`./scripts/find_related.py` (Python 3.8+, `wl` CLI required)

### Usage

```bash
python3 ./scripts/find_related.py --work-item-id <id> [--json] [--verbose] [--repo-path <path>]
```

### Arguments

| Argument | Required | Default | Description |
|----------|----------|---------|-------------|
| `--work-item-id` | Yes | — | Work item to search |
| `--verbose` | No | false | Debug output to stderr |
| `--json` | No | false | JSON output |
| `--repo-path` | No | auto | Repository root |

### Output (default)
```
Work item: <id> | Related: 3 | Repo matches: 2 | Added IDs: REL-001, REL-002
```

### Output (JSON)
```json
{"workItemId": "<id>", "found": true, "addedIds": [...], "reportInserted": true, "keywords": [...], "relatedItemCount": 3, "repoMatchCount": 2}
```

### Exit codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | Error |

### Idempotency

Safe to re-run: existing automated report is replaced, not duplicated. Manual "Related work" sections preserved.

### Design

Fully offline (local `wl` + filesystem). Conservative keyword matching. Scans `.md`, `.py`, `.js`, `.mjs`, `.txt` files only.

End.
