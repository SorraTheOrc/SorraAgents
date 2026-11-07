---
name: standup
description: "Generate a standup meeting report focused on work items in the queue. Use when asked for a standup report, daily standup, or team status."
disable-model-invocation: true
---

# Standup Skill

Produce a concise standup report in the **first person**, written to be read aloud by a TTS engine — three questions, clustered into categories, under 2 minutes.
The script is the deterministic data layer; **you, the invoking agent, cluster
and render** the narrative from its structured JSON.
Source of truth for "most important item" is the **Herdr selection list**
(WL-0MTK1ILM2009QYB2): `fetcher.ts:fetchNextItems` → `smart-selection.ts:selectWorkItems`
→ `grouping.ts:regroupWorkItems`. The downtime dispatcher consumes the same list head
by construction — no second ranking.

## When to Use

- Daily standup meetings
- When asked for a standup report or team status

## Invocation

```bash
python3 $(skill_path standup)/scripts/generate_standup.py [--json] [--count N] [--verbose] [--output-path <path>] [--startTime <ISO>] [--duration <hours>] [--worklog-dir <path>] [--no-sync] [--no-commit]
```

| Flag | Default | Description |
|---|---|---|
| `--json` | off | Raw JSON output instead of markdown. Streamed to **stdout** unless `--output-path` is given (then written to that file) |
| `--count / -n N` | 20 (herdr config) | Herdr browse window |
| `--verbose` | off | Extra detail (priority, status, stage) |
| `--output-path <path>` | `./standups/YYYY_MM_DD.md` (markdown only) | Write the rendered report to file; defaults to today's dated file in `./standups/` relative to the project root (the worklog parent when `--worklog-dir` / `WL_WORKLOG_DIR` is set, otherwise `cwd`), created if missing. `--output-path` overrides the default; default uses `YYYY_MM_DD` from the report generation date (local datetime, zero-padded). With `--json`, `--output-path` writes the raw JSON to the given path instead of stdout |
| `--startTime <ISO>` | prev day 06:00 | Window start (e.g. `2026-09-03T06:00:00`) |
| `--duration <hours>` | 24 | Window length in hours |
| `--worklog-dir <path>` | cwd | Explicit `.worklog` dir (e.g. `/path/to/project/.worklog`); also honored via `WL_WORKLOG_DIR` env var. Bypasses cwd-based resolution so the report works from any directory |
| `--no-sync` | off | Skip the initial `wl sync` (offline/deterministic runs and tests) |
| `--no-commit` | off | Skip the git commit + push of the report file just written (offline/deterministic runs and tests). By default the file is committed and the current branch pushed, non-fatally |

Default time window: **24 hours starting at 06:00 the previous day** (yesterday 06:00 → today 06:00). Override with `--startTime` and/or `--duration`.

### Sync-first

Before its first `wl next` / `wl list` fetch, the script runs `wl sync` (via the
shared `worklog_sync` helper) so the Herdr selection list and
completions reflect the shared remote state.

- **Lock-aware**: passes `--if-idle`, so a concurrent sync makes this one skip
  (exit 0) rather than block or pile up.
- **Non-fatal**: offline, unborn-HEAD, author-gate, lock-contention, or a
  missing `.worklog` context is logged as a warning; the report is still
  generated from local data and the exit code is unchanged.
- **Disable** with `--no-sync` for offline or deterministic runs (tests use
  this to stay hermetic).

### Commit-and-push

When the report is written to a file (the default dated path under `standups/`
or an explicit `--output-path`), the script commits **only that file** and
pushes the current branch so standup history is versioned automatically.

- **Surgical**: `git add -- <file>` → `git commit -m "…" -- <file>` →
  `git push`, anchored to the resolved project root (the worklog parent when
  `--worklog-dir` / `WL_WORKLOG_DIR` is set, otherwise cwd). An unrelated
  staged change is never swept into the commit.
- **`main` push guard**: on `main`/`master` the report is committed locally but
  **not** pushed (warning); the repo policy is "push only to `dev`".
- **Non-fatal**: a non-git directory, unborn/empty repo, unset author identity,
  missing upstream, or an offline/rejected push is logged as a warning; the
  report is still written and the exit code is unchanged.
- **Idempotent**: re-running with an unchanged report is a no-op ("nothing to
  commit").
- **`--json` to stdout writes no file**, so it performs no commit/push.
- **Disable** with `--no-commit` for offline/deterministic runs.

> The machine-local `standups/` entry in `.git/info/exclude` was a stopgap and
> has been removed (SA-0MUDYRHVZ005PQP4) — an ignored path would make `git add`
> silently skip the file and the commit a silent no-op.

## Report Rendering

The report is **rendered by you, the invoking agent — not by the script**. The
script is the deterministic data layer: it fetches, windows, selects and emits
the items as structured JSON. You cluster those items into categories chosen at
report time and write the prose. That keeps the data layer testable and the
narrative readable aloud.

### 1. Fetch the structured data

```bash
python3 $(skill_path standup)/scripts/generate_standup.py --json
```

Each section arrives as its own array — `yesterday_items`,
`today_critical_items`, `today_additional_items`, `regressions_items` — plus
`blockers_tts` for blockers and `project_name` / `window` for the header. Every
item carries `title`, `impact` (empty when the item has no `## Impact`
section), `status` and `priority`; `id` exists for your own bookkeeping only and
must never reach the rendered report. The pre-formatted `*_tts` arrays give the
same per-item sentences already impact-first, ID-free and sanitised for speech —
use them when you do not need to re-derive a sentence.

### 2. Cluster each section independently

- Each section is clustered **on its own**: the categories you choose for
  "Yesterday I completed work on…" say nothing about those for "Open critical
  items" or "Blockers". Never pick one taxonomy for the whole report.
- Category labels are **free-form, chosen at report time** from the items
  actually present (for example "Scheduling fairness", "Crash resilience",
  "Review queue ergonomics"). There is no fixed taxonomy and no approved list.
- Use as many categories as the items genuinely fall into — normally one to
  three per section. A section holding a single item needs no label at all.
- Group by the user-visible theme the operator cares about, not by technical
  module: the report says where progress happened and what it means for them.

### 3. One paragraph per category, one sentence per item

- Render each category as **one paragraph** of spoken prose that opens with a
  short spoken label, e.g. "On scheduling fairness, …". No bullets, no nested
  lists, no tables in the rendered report.
- Each item contributes exactly **one sentence** inside that paragraph.
- That sentence is the item's `## Impact` sentence when present; otherwise fall
  back to the established `Title — user story` phrasing taken from the item's
  description (the script's `*_tts` lines already apply both rules). An item
  with an Impact sentence is never described by its technical "what was built"
  summary.
- Trim a multi-sentence Impact to its first sentence so the read budget holds.

### 4. Keep the fixed section order

```markdown
## <project_name> Standup Report (5th September 2026)

### Yesterday I completed work on...

### Open critical items

### Additional focus items for today

### Regressions

### Blockers
```

Keep every heading even when a section is empty, and say so in one spoken line:
"Nothing completed in this window.", "No open critical items.", "No additional
focus items scheduled.", "No regressions — no items slipped back from
completed." or "No immediate blockers." Never drop, merge or reorder sections.

### 5. Speech rules (non-negotiable)

- **No work item IDs anywhere** in the rendered report — no `(WL-…)` or
  `(SA-…)`, no ID brackets, backticks or underscores in spoken lines.
- **First person**: "Yesterday I completed…", "My focus today is…".
- **Spoken dates and times**: "5th September 2026", "6:00 am" — never ISO
  timestamps.
- **Under 2 minutes** read aloud: one sentence per item and tight paragraphs.
  If the report runs long, cut adjectives — never items or their impact.
- **Report only**: no preamble, no postamble, no explanation of the clustering.

### 6. Persisting the report

The script's own markdown rendering (`format_report`) remains the deterministic
fallback for scheduled, non-agent runs: it writes the dated file
(`standups/YYYY_MM_DD.md`, or `--output-path`) and commits and pushes it. When
your clustered rendering is the versioned report, write it to that same dated
path and commit only that file (`git add -- <path>`, `git commit -m
"chore(standup): add standup report YYYY_MM_DD" -- <path>`, then push unless on
`main`/`master`). `--json` to stdout writes no file and performs no commit.

The project name in the header is resolved by the shared
`skill.shared.project_name.resolve_project_name()` helper: it reads
`projectName` from `<worklog_dir>/config.yaml` (honouring
`--worklog-dir` / `WL_WORKLOG_DIR`), falls back to the `.worklog` parent
directory name, then the current working directory name. The JSON output of
generate_report() also carries a `project_name` field for downstream consumers.

Today's focus list includes **all** open critical items (regardless of count) plus additional non-critical items to reach **5 total** where possible. If 5 or more critical items are open, no additional items are shown.

## Rules

1. **Scope**: Only items in the Herdr selection list. Items not in the Herdr window are excluded unless mentioned as blockers. No fallback ranking — if the Herdr window is empty, today is empty.
2. **Herdr list is sole ranking path** (WL-0MTK1ILM2009QYB2): `fetchNextItems` (`wl next -n N` merged with `wl list --priority critical --root-only` and `wl list --status completed --stage in_review --root-only`, then `selectWorkItems` + `regroupWorkItems`) is the only ordering. Safety gates (code-freeze, dispatched-marker, single-flight/CAS) are filters on that sequence, never a competing rank. Do not add or fall back to a separate `wl next` tier ordering.
3. **First person**: The report speaks as "I" — e.g. "Yesterday I completed...", "My focus today is...".
4. **TTS-friendly prose**: Dates as "5th September 2026" and times as "6:00 am", never "2026-09-05 06:00". Render grouped paragraphs rather than bullets, and keep IDs, brackets, backticks and underscores out of every spoken line.
5. **Selection cap**: Always include every open critical item; fill remaining slots with the Herdr-ordered non-critical items up to 5 total.
6. **User stories first**: Focus on the *what* and *why*, not the *how*. Prefer each item's `## Impact` sentence, falling back to its user story, and translate technical work into user value.
7. **Concise**: One sentence per item, one paragraph per category. 2 minutes reading time max. Head-of-list first.
8. **Honest**: Incomplete work should be stated. Blockers should be explicit.
9. **Template only**: Output only the report defined in Report Rendering — no preamble, no postamble, no explanatory text before or after the report.

## Data Sources

```bash
python3 $(skill_path standup)/scripts/generate_standup.py --json   # Structured data for agent rendering
wl dep list <id> --json   # Check for blocking dependencies
```

The `--json` payload is the whole data contract for rendering: one item array
per section (`yesterday_items`, `today_critical_items`,
`today_additional_items`, `regressions_items`), `blockers_tts` for blockers,
and the pre-formatted `*_tts` line arrays for each section. `project_name` and
`window` supply the header and the report window; `herdr`/`queue` and the
`format_item` fields remain for back-compat consumers.

Run from any directory by pinning the worklog: `python3 $(skill_path standup)/scripts/generate_standup.py --worklog-dir /path/to/project/.worklog` or `WL_WORKLOG_DIR=/path/to/project/.worklog python3 $(skill_path standup)/scripts/generate_standup.py`. A bare project root (`/path/to/project`) is normalized to `/path/to/project/.worklog`.

Script internals mirror `fetcher.ts` exactly: `wl next -n N --include-in-progress`
+ the two mandatory `wl list` subsets → merged unique by id → `selectWorkItems`
(root-only, `stage != done`, mandatory-always) → `regroupWorkItems`.
- **Yesterday** (`wl list --status completed --stage in_review`) is *not* limited to the Herdr list — it fetches all completions in the window.
- **Regressions** compares the latest full `.worklog/worklog-data.jsonl` snapshot before the window start against current items to flag `completed/in_review` → `open/in_review` slips during the window.

## Example

```markdown
## Sorra Agents Standup Report (5th September 2026)

### Yesterday I completed work on...

On scheduling fairness, I made round-robin work scheduling the default, so no single project monopolises my agent attention.

On how I hear about progress, I reworked the standup summary so it leads with what each change gave users.

### Open critical items

On keeping automated work moving, I stopped finished critical work from competing with urgent open items, so the priority I see matches the work that actually runs.

### Additional focus items for today

On the review queue, I am sorting in-review items so the queue is easier to scan.

On keeping crashes visible, I am making crash reporting automatic so recurring crashes surface promptly.

On working across authors, I am replacing the binary author identity gate so sync works whatever author committed.

### Regressions

No regressions — no items slipped back from completed.

### Blockers

No immediate blockers.
```
