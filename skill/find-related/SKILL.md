---
name: find-related
disable-model-invocation: true
description: "Find related work for a Worklog item; generate an auditable report. Use when discovering related work."
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
| `MAX_KEYWORDS_PER_FILE` | 5 | Maximum matched keywords listed per repo file match. Raw keyword word-lists are the dominant source of report bloat (measured ~58% of the related-work section), so lists are capped with a `(+N more)` marker to keep descriptions/prompts compact. |

### Prompt-size management (P11)

The related-work section is deliberately **compact**: keyword word-lists per repo file are capped at `MAX_KEYWORDS_PER_FILE` (default 5) with a `(+N more)` marker, so descriptions stay small and any prompt that carries the description stays within token limits.

The **full** (untruncated) report — with every matched keyword — is persisted to `.worklog/tmp/find-related-full-<id>.md` on every run, so no related-work data is lost even though the description carries only the summary. The sidecar path is returned in JSON output as `fullReportPath`.

## Inputs / Outputs

**Input**: work-item id (required)

**Output**: JSON with keys `found`, `addedIds`, `reportInserted`, `updatedDescription`

## Status management

Status transitions are handled **automatically** by the `StatusLifecycle` context
manager from `../shared/status_lifecycle.py`:

- **On entry:** Status is set to `in_progress` (original value captured)
- **On success:** Original status is restored (`restore_on_exit=True` — this
  read-only skill never advances the item to `completed`, which wl only
  allows for `in_review`/`done` stages)
- **On exception:** Original status is restored

> Stage is NOT modified. The item is never left in `in_progress` when the
> script exits — every path restores the pre-run status.

> **Note:** The script probes semantic search availability and auto-detects the correct `wl search` response format. No manual configuration needed.

## Worklog resolution

`find_related.py` pins the target worklog store from the work-item id and
injects the resolved `--worklog-dir` into **every** `wl` subprocess call
(show, update, search, and the `--semantic` probe) via the shared resolution
in `../shared/status_lifecycle.py`:

1. **Explicit `--worklog-dir` value** (from a CLI flag / caller)
2. **Prefix-to-sibling scan** — the work-item id prefix (e.g. `OSL`) is matched
   against sibling projects' `config.yaml` so a non-SorraAgents item resolves
   to its own worklog store even when the harness cwd is the framework repo
3. **cwd chain** — `<cwd>/.worklog`, git root, nearest initialized ancestor
4. **No flag** — `wl` resolves from cwd (failures surface real error detail)

Search and the semantic probe carry no work-item id of their own, so their
store is pinned from the id of the item being analyzed (`_wl_flags_for`). The
script resolves the correct worklog store regardless of the directory it is
invoked from. See `docs/dev/worklog-sync.md` for the shared resolution order.

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
| `--repo-path` | No | auto | Repository root. Defaults to the analyzed work item's own project (parent of its resolved `.worklog` store, prefix-to-sibling scan); falls back to the framework repo when no store resolves. An explicit path always overrides the default. |

### Output (default)

```
Work item: <id> | Related: 3 | Repo matches: 2 | Added IDs: REL-001, REL-002
```

### Output (JSON)

```json
{"workItemId": "<id>", "found": true, "addedIds": [...], "reportInserted": true, "keywords": [...], "relatedItemCount": 3, "repoMatchCount": 2, "fullReportPath": ".worklog/tmp/find-related-full-<id>.md"}
```

`fullReportPath` points at the persisted full (untruncated) report; it is `null` if sidecar persistence failed (best-effort).

### Exit codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | Error |

### Idempotency

Safe to re-run: ALL prior automated report sections are removed before the
new one is inserted (duplicates from earlier runs never accumulate). Manual
"Related work" sections (without the automated marker) are preserved.

### Design

Fully offline (local `wl` + filesystem). Conservative keyword matching. Scans `.md`, `.py`, `.js`, `.mjs`, `.txt` files only.

### Changes in v2 (scoring, ranking, and limits)

- **Semantic search integration:** Automatically probes `wl search --semantic` availability. When available, uses hybrid lexical+semantic ranking for work item search. Falls back gracefully to keyword-only search.
- **Work item ranking:** Items are ranked by their `score` field (BM25/hybrid) and capped at `MAX_WORK_ITEM_RESULTS`.
- **Repo file ranking:** Files are ranked by keyword match count, capped at `MAX_REPO_FILE_RESULTS`.
- **Configurable limits:** Both limits are Python constants, easily adjustable.
- **Bug fix:** `run_wl_search` now correctly parses the `workItems` key from `wl search --json` output.

End.


## Final step: standardized end-of-session report

Render the canonical end-of-session report (helper: [`../report/SKILL.md`](../report/SKILL.md)) as the **last step**, replacing any ad-hoc end-of-session summary:

```bash
python3 ../report/scripts/render_report.py <work-item-id> \
  --skill-name <skill_name> \
  --headline "<1-3 sentence headline summary>" \
  --ac "<AC# description>|<verification metric>|met|unmet" \
  --ac "<...>|<...>|met|unmet" \
  [--producer-actions "<actions for the producer, or omit for 'None needed'>"] \
  [--notes "<freeform context/caveats/assumptions>"] \
  [--next-action <review|plan|implement|...>]
```

Then close with: `<work-item-id>: <one-line summary>`. Do NOT re-summarize the report in a different format — the report is the summary.
