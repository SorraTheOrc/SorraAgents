# STE100 session-log experiment tooling

Deterministic measurement tooling for the STE100 24-hour experiment
(`SA-0MSMC8KRS00164VD`). The experiment asks whether adding the single
sentence **"Always use ASD-STE100 Simplified Technical English."** to the
global `AGENTS.md` changes agent session length or token consumption.

This directory provides the *measurement* half of the experiment. It does not
decide anything about STE — STE *compliance* is explicitly out of scope.

## `scripts/analyze_sessions.py`

Reads pi session logs (`~/.pi/agent/sessions/**/*.jsonl`), extracts per-session
duration and token/cost metrics, and aggregates them over an optional time
window. Output is deterministic: the same log directory always yields the same
result, so an experiment run is reproducible (experiment AC 5).

### Usage

```bash
# Whole log, human-readable
python3 skill/ste100-experiment/scripts/analyze_sessions.py

# One 24h window, JSON (attribution is by session start timestamp)
python3 skill/ste100-experiment/scripts/analyze_sessions.py \
  --window-start 2026-09-27T00:00:00Z \
  --window-end   2026-09-28T00:00:00Z \
  --json --output /tmp/window.json
```

| Option | Meaning |
| --- | --- |
| `--sessions-dir DIR` | Session-log root to scan recursively (default `~/.pi/agent/sessions`). |
| `--window-start ISO8601` | Inclusive window start, compared against session **start**. |
| `--window-end ISO8601` | Exclusive window end, compared against session **start**. |
| `--format {text,json}` | Output format (default `text`). |
| `--json` | Shorthand for `--format json`. |
| `--output PATH` | Also write the rendered report to `PATH`. |

### Metrics

For the selected window the report contains:

- **Session count**.
- **Duration (minutes)** — mean, median, min, max, total.
- **Tokens** — `input`, `output`, `cacheRead`, `cacheWrite` and `totalTokens`,
  each with mean/median/min/max/sum.
- **Cost** — totals and per-component sums (where the log records `cost`).
- **Per-project** and **per-model** breakdowns.
- A per-session list, sorted by `(start, id)` for stable output.

### Rules and edge cases

- **Attribution is by session start** — a session that spans a window boundary
  belongs to the window containing its start timestamp.
- **Session end** is the latest timestamp in the log file.
- Missing `usage` blocks contribute zeros; the session is still counted.
- Malformed JSONL lines are skipped and counted (`malformed_lines`).
- A session with no parseable timestamps is counted only in an unbounded window.
- `cost` may be recorded as a number or as an object with
  `input`/`output`/`cacheRead`/`cacheWrite`/`total`; both are handled.

### Tests

```bash
python3 -m pytest skill/ste100-experiment/tests/test_analyze_sessions.py -q
```

The tests build synthetic JSONL fixtures under `tmp_path` and assert
observable behaviour (no live logs, no clock, no network).

## Experiment workflow

1. Apply the STE sentence to `AGENTS_GLOBAL.md`, commit it to `dev`, and record
   the commit hash and wall-clock time **T0**.
2. Let the window `[T0, T0+24h)` elapse with normal agent activity.
3. Measure both windows with the script:
   baseline `[T0-24h, T0)` and experiment `[T0, T0+24h)`.
4. Produce the comparison report (deltas, outliers, breakdowns, interpretation)
   and **revert** the sentence, recording the revert commit hash.

The comparison report for a specific run lives under
`docs/experiments/ste100-24h/`.
