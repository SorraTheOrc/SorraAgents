---
name: agent-log-review
description: "Analyse recent pi session logs for agent-confusion patterns and auto-file skill-improvement items. Use when: 'review agent logs'."
---

# Agent Log Review

## Overview

Analyse the last 20 pi session logs (`~/.pi/agent/sessions/<project-slug>/*.jsonl`)
of the project from which the skill is invoked, detect repeated agent
confusion patterns (shell quoting mistakes, missing Python imports, skill
invocation confusion, permission issues, self-correction loops), and file
actionable skill-improvement recommendations as worklog comments + child work
items — without implementing them.

**Hybrid design:** deterministic heuristics extract distilled signals (tool
call distributions, thinking-keyword hits, tool-result errors, retry
patterns); a single non-interactive `pi --print --no-tools` call classifies
those signals into pattern categories and recommendations. Raw session logs
are never sent to the model and never persisted.

## When To Use

1. An operator/producer asks for a review of recent agent behaviour in this
   project.
2. As a periodic hygiene pass to catch recurring skill-invocation or shell
   quoting problems before they block other work.
3. When the same failure pattern appears repeatedly in sessions and needs a
   tracked improvement item.

## Inputs

- **Session logs** of the CWD project: `~/.pi/agent/sessions/<project-slug>/`
  v3 JSONL files, most-recent 20 by modification time (project slug derived
  from CWD via pi's `--<cwd-with-separators-replaced-->` convention).
- Optional flags (see below) to bound sessions, skip the LLM call, or target
  a work item for recommendation filing.
- Optional `--parent-id`: the work item to file recommendations against
  (comment + auto-created children).

## Outputs

- A deterministic **Markdown report** (default) or single **JSON document**
  (`--json`) on stdout, containing:
  - **Summary** — sessions analysed, date range, fail-soft counters.
  - **Top Patterns** — counted and sorted by frequency, with session
    citations.
  - **Recommended Improvements** — LLM-classified recommendations with
    rationale, evidence sessions, the affected **target skill**, and an
    **impact scope** (`global` = affects shared/all-project code,
    `local` = this project only).
  - **Evidence Citations** — which sessions showed which patterns.
- With `--parent-id`: a worklog comment on the parent item and one child work
  item per actionable recommendation (`chore`/`feature`, assigned to `Map`,
  description linked via `related-to:<parent-id>`). Idempotent: same-title
  children are reused, never duplicated.
- A full report may be written to a file with `--output-file` (Markdown).

## Process

1. **Parse** — `read_sessions()` reads the project's most-recent session
   logs (v3 format), extracting tool calls, tool results (incl. `isError`),
   thinking/text parts, usage and message timestamps. Malformed lines and
   missing headers degrade with counters, never crash.
2. **Extract** — `extract_signals()` produces distilled counts, keyword hits
   with context windows, tool-error signals and retry/self-correction
   patterns (deterministic, side-effect free).
3. **Classify** — `classify_patterns()` makes exactly **one** `pi --print
   --no-tools` call with the distilled signals and parses the JSON response
   into categories and recommendations (fail-soft on malformed output). Each
   recommendation carries `target_skill` and `impact_scope` (`global`/`local`)
   plus a `priority`, normalised defensively (`unknown` scope, issue-type
   default priority). The signals are deterministically trimmed to the 32 KB
   prompt budget so real 20-session runs always fit.
4. **Render** — `render_report()` emits the deterministic Markdown or JSON
   report with secrets redaction.
5. **File** — with `--parent-id`, `file_recommendations()` posts the report
   as a comment and auto-creates (or reuses) child work items.

## CLI Flags

| Flag | Default | Purpose |
|------|---------|---------|
| `--max-sessions N` | 20 | Most-recent session logs to analyse |
| `--errors-only` | off | Only analyse sessions containing an error result |
| `--no-classify` | off | Skip the LLM call (heuristic report only) |
| `--model MODEL` | `Local Proxy/code` (env `AGENT_LOG_REVIEW_MODEL`) | Model for the classification call |
| `--json` | off | Emit the report as a single JSON document |
| `--output-file PATH` | — | Write the Markdown report to PATH |
| `--parent-id ID` | — | Work item to comment on and attach recommendation children to |
| `--dry-run` | off | With `--parent-id`: show planned worklog actions without mutating |
| `--min-confidence X` | 0.0 | Only create recommendations at/above confidence X |
| `--cwd PATH` | cwd | Project directory for slug derivation |
| `--session-root PATH` | `~/.pi/agent/sessions` (env `PI_CODING_AGENT_SESSION_DIR`) | Session store root |

Example invocations:

```bash
python3 $(skill_path agent-log-review)/scripts/agent_log_review.py
python3 $(skill_path agent-log-review)/scripts/agent_log_review.py --json
python3 $(skill_path agent-log-review)/scripts/agent_log_review.py --parent-id SA-XXXX --dry-run
python3 $(skill_path agent-log-review)/scripts/agent_log_review.py --parent-id SA-XXXX --json
```

## Bundled Resources

- `scripts/agent_log_review.py` — parser, heuristic extractors, LLM
  classification, report rendering, worklog integration and the CLI.
- `tests/test_agent_log_review.py` + `tests/conftest.py` — offline unit and
  mocked-subprocess integration tests (no real `pi` or `wl` calls).

## Privacy & Redaction

- Only distilled signals (counts + capped excerpts) are sent to the model —
  never raw log content.
- Secrets (`sk-…` keys, `Bearer …` tokens, `.ssh`/`.env` paths) are redacted
  from every prompt, report and comment via `redact_secrets()`.
- Nothing is persisted outside `.worklog/tmp/`; worklog writes are limited to
  the `--parent-id` comment + child creation.

## References

- Maintainer reference: [docs/dev/agent-log-review-skill-reference.md](../../docs/dev/agent-log-review-skill-reference.md).
- Epic: SA-0MUEKTJRD0010CGC (children SA-0MV6U4PW4007R7FE → SA-0MV6UHXBS00019UE).

## Final step: standardized end-of-session report

Render the canonical end-of-session report (helper: [`../report/SKILL.md`](../report/SKILL.md)) as the **last step**, replacing any ad-hoc end-of-session summary:

```bash
python3 $(skill_path report)/scripts/render_report.py <work-item-id> \
  --skill-name agent-log-review \
  --headline "<1-3 sentence headline summary>" \
  --ac "<AC# description>|<verification metric>|met" \
  --ac "<...>|<...>|unmet" \
  [--producer-actions "<producer actions needed to complete the CURRENT stage; blocking only — omit for 'None needed'>"] \
  [--notes "<freeform context/caveats/assumptions>"] \
  [--next-action <review|plan|implement|...>]
```

The script prints the rendered report to stdout — **paste it verbatim into
your final response**, then close with: `<work-item-id>: <one-line summary>`. Do NOT re-summarize the report in a different format — the report is the summary.
