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

## Decision logic

1. Fetch item: `wl show <id> --json`
2. Derive keywords from title/description; stop words excluded, 3+ chars
3. Probe `wl search --semantic` — use hybrid ranking if available
4. Search Worklog for each keyword, aggregate results, deduplicate
5. **Rank** work items by descending `score` field (BM25 or hybrid), cap at `MAX_WORK_ITEM_RESULTS`
6. Search repo files (`.md`, `.py`, `.js`, `.mjs`, `.txt`, excluding `.git`, `node_modules`, etc.)
7. **Rank** repo files by distinct keyword match count, cap at `MAX_REPO_FILE_RESULTS`
8. Filter out the current work item from results
9. Generate report under "## Related work (automated report)"
10. Update item description (replace existing automated report section, preserving manual content)
11. Return JSON summary

**Policy**: Conservative — prefer false negatives over false positives. Only include truly related items.

### Ranking heuristics

| Section | Heuristic | Detail |
|---------|-----------|--------|
| Work items | `score` field from `wl search --json` | BM25 score (keyword) or hybrid BM25+semantic. Higher (less negative) = more relevant. Unscored items sort last. |
| Repo files | Distinct keyword match count (descending) | Files matching more distinct keywords rank higher. Ties broken alphabetically. |

### Configurable limits

| Constant | Default | Description |
|----------|---------|-------------|
| `MAX_WORK_ITEM_RESULTS` | 3 | Maximum related work items shown. Soft limit — may be replaced by minimum-relevance thresholds when semantic/embedding-based scoring is available. |
| `MAX_REPO_FILE_RESULTS` | 3 | Maximum repo file matches shown. Same soft-limit semantics. |

## Inputs / Outputs

**Input**: work-item id (required)

**Output**: JSON with keys `found`, `addedIds`, `reportInserted`, `updatedDescription`

## Status management

1. Start: `wl update <id> --status in_progress --json`
2. End: `wl update <id> --status open --json` (success or failure)

> Stage is NOT modified.

> **Note:** The script probes semantic search availability and auto-detects the correct `wl search` response format. No manual configuration needed.

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

### Changes in v2 (scoring, ranking, and limits)

- **Semantic search integration:** Automatically probes `wl search --semantic` availability. When available, uses hybrid lexical+semantic ranking for work item search. Falls back gracefully to keyword-only search.
- **Work item ranking:** Items are ranked by their `score` field (BM25/hybrid) and capped at `MAX_WORK_ITEM_RESULTS`.
- **Repo file ranking:** Files are ranked by keyword match count, capped at `MAX_REPO_FILE_RESULTS`.
- **Configurable limits:** Both limits are Python constants, easily adjustable.
- **Bug fix:** `run_wl_search` now correctly parses the `workItems` key from `wl search --json` output.

End.
