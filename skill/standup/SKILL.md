---
name: standup
description: "Generate a standup meeting report focused on work items in the queue. Use when asked for a standup report, daily standup, or team status."
disable-model-invocation: true
---

# Standup Skill

Produce a concise standup report in the **first person**, written to be read aloud by a TTS engine — three questions, user stories, under 2 minutes.
Source of truth for "most important item" is the **Herdr selection list**
(WL-0MTK1ILM2009QYB2): `fetcher.ts:fetchNextItems` → `smart-selection.ts:selectWorkItems`
→ `grouping.ts:regroupWorkItems`. The downtime dispatcher consumes the same list head
by construction — no second ranking.

## When to Use

- Daily standup meetings
- When asked for a standup report or team status

## Invocation

```bash
python3 $(skill_path standup)/scripts/generate_standup.py [--json] [--count N] [--verbose] [--output-path <path>] [--startTime <ISO>] [--duration <hours>] [--worklog-dir <path>]
```

| Flag | Default | Description |
|---|---|---|
| `--json` | off | Raw JSON output instead of markdown |
| `--count / -n N` | 20 (herdr config) | Herdr browse window |
| `--verbose` | off | Extra detail (priority, status, stage) |
| `--output-path <path>` | stdout | Write report to file |
| `--startTime <ISO>` | prev day 06:00 | Window start (e.g. `2026-09-03T06:00:00`) |
| `--duration <hours>` | 24 | Window length in hours |
| `--worklog-dir <path>` | cwd | Explicit `.worklog` dir (e.g. `/path/to/project/.worklog`); also honored via `WL_WORKLOG_DIR` env var. Bypasses cwd-based resolution so the report works from any directory |

Default time window: **24 hours starting at 06:00 the previous day** (yesterday 06:00 → today 06:00). Override with `--startTime` and/or `--duration`.

## Report Structure

Output only the report defined below — no preamble or postamble. Report is rendered in the first person for TTS. Dates are spoken-form (e.g. "5th September 2026", "4th September 2026 6:00 am"), never ISO timestamps.

```markdown
## Standup Report (5th September 2026)

### Yesterday I completed work on...

  - Work item description — primary use case
  - Work item description — primary use case

### Open critical items

  - Work item description — primary use case
  - Work item description — primary use case

### Additional focus items for today

  - Work item description — primary use case
  - Work item description — primary use case
  - Work item description — primary use case

### Regressions

  - No regressions — no items slipped back from completed.

or

  - Work item description — reopened during window

### Blockers

  - No immediate blockers

or

  - Blocked on work item description, because description of problem
```

Herdr selection rendering (smart-selection + grouping):
`selectWorkItems` keeps all `critical` ∪ `completed/in_review` mandatory and limits
only "other" items to `browseItemCount`; `regroupWorkItems` then assigns
`Critical Group N / Group N / Idea / Other / In Review` deterministically from
`Key Files` paths.

Today's focus list includes **all** open critical items (regardless of count) plus additional non-critical items to reach **5 total** where possible. If 5 or more critical items are open, no additional items are shown.

## Rules

1. **Scope**: Only items in the Herdr selection list. Items not in the Herdr window are excluded unless mentioned as blockers. No fallback ranking — if the Herdr window is empty, today is empty.
2. **Herdr list is sole ranking path** (WL-0MTK1ILM2009QYB2): `fetchNextItems` (`wl next -n N` merged with `wl list --priority critical --root-only` and `wl list --status completed --stage in_review --root-only`, then `selectWorkItems` + `regroupWorkItems`) is the only ordering. Safety gates (code-freeze, dispatched-marker, single-flight/CAS) are filters on that sequence, never a competing rank. Do not add or fall back to a separate `wl next` tier ordering.
3. **First person**: The report speaks as "I" — e.g. "Yesterday I completed...", "My focus today is...".
4. **TTS-friendly**: Dates as "5th September 2026" and times as "6:00 am", never "2026-09-05 06:00". Avoid markdown-heavy formatting (IDs, brackets, backticks) in spoken lines; keep each bullet as a natural spoken sentence: "Title — user story".
5. **Selection cap**: Always include every open critical item; fill remaining slots with the Herdr-ordered non-critical items up to 5 total.
6. **User stories first**: Focus on the *what* and *why*, not the *how*. Translate technical work into user value.
7. **Concise**: One line per item. 2 minutes reading time max. Head-of-list first.
8. **Honest**: Incomplete work should be stated. Blockers should be explicit.
9. **Template only**: Output only the report defined in Report Structure — no preamble, no postamble, no explanatory text before or after the report.

## Data Sources

```bash
python3 $(skill_path standup)/scripts/generate_standup.py --json   # Fetch data (Herdr list + deps)
wl dep list <id> --json   # Check for blocking dependencies
```

Run from any directory by pinning the worklog: `python3 $(skill_path standup)/scripts/generate_standup.py --worklog-dir /path/to/project/.worklog` or `WL_WORKLOG_DIR=/path/to/project/.worklog python3 $(skill_path standup)/scripts/generate_standup.py`. A bare project root (`/path/to/project`) is normalized to `/path/to/project/.worklog`.

Script internals mirror `fetcher.ts` exactly: `wl next -n N --include-in-progress`
+ the two mandatory `wl list` subsets → merged unique by id → `selectWorkItems`
(root-only, `stage != done`, mandatory-always) → `regroupWorkItems`.
- **Yesterday** (`wl list --status completed --stage in_review`) is *not* limited to the Herdr list — it fetches all completions in the window.
- **Regressions** compares the latest full `.worklog/worklog-data.jsonl` snapshot before the window start against current items to flag `completed/in_review` → `open/in_review` slips during the window.

## Example

```markdown
## Standup Report (5th September 2026)

### Yesterday I completed work on...

  - Fair round-robin work scheduling — So no single project monopolises agent attention.

### Open critical items

  - Auto-downgrade critical on completion — So finished work stops competing with urgent open items.
  - Dispatcher consistency — So automated work follows the priority I see.

### Additional focus items for today

  - Sort in review items — So the review queue is easier to scan.
  - Enable automatic crash reporting — So recurring crashes are surfaced promptly.
  - Replace binary author identity gate — So sync works across different commit authors.

### Regressions

  - No regressions — no items slipped back from completed.

### Blockers

  - No immediate blockers.
```
