# Agent log review skill — implementation reference

The `agent-log-review` skill analyses the most-recent pi session logs of the
CWD project for repeated agent-confusion patterns and files skill-improvement
recommendations into the worklog. This document is the maintainer reference:
architecture, the consumed session-log format, privacy/redaction rules, the
test strategy, and extension points. The operational brief lives in
[`skill/agent-log-review/SKILL.md`](../../skill/agent-log-review/SKILL.md).

Epic: SA-0MUEKTJRD0010CGC. Built as six child features in dependency order:

| Child | Feature | Public surface |
|-------|---------|----------------|
| SA-0MV6U4PW4007R7FE | Session log parser (v3 JSONL) | `read_sessions`, `parse_session`, `find_session_files`, `project_slug`, `session_dir_for`, dataclasses |
| SA-0MV6UDSAF0064B85 | Heuristic confusion-pattern extraction | `extract_signals`, `extract_tool_distribution`, `extract_thinking_signals`, `extract_tool_errors`, `extract_retry_patterns` |
| SA-0MV6UETWV002CF5T | LLM-assisted pattern classification | `classify_patterns`, `_call_pi`, `redact_secrets` |
| SA-0MV6UFVV30059ZPC | Structured report rendering | `render_report`, `build_analysis` |
| SA-0MV6UGWC4000GU0G | Worklog integration | `file_recommendations` |
| SA-0MV6UHXBS00019UE | SKILL.md + reference docs | this document + `SKILL.md` |

## Architecture

```
                        ┌───────────────────────────────────────────────┐
   ~/.pi/agent/sessions │  ┌─────────────────┐    ┌───────────────────┐  │
   /<project-slug>/*.   │  │ 1. Parser       │───▶│ 2. Heuristics     │  │
   jsonl (v3)           │  │ read_sessions / │    │ extract_signals   │  │
                        │  │ parse_session   │    │ (counts+excerpts) │  │
                        │  └─────────────────┘    └────────┬──────────┘  │
                        └──────────────────────────────────┼─────────────┘
                                                           │ distilled signals only
                                                           ▼
                           ┌─────────────────────────────────────────────┐
                           │ 3. LLM classification (ONE pi --print call)  │
                           │    categories + recommendations (JSON)       │
                           └──────────────────────┬──────────────────────┘
                                                  ▼
                           ┌─────────────────────────────────────────────┐
                           │ 4. render_report (Markdown | --json)        │
                           │ 5. file_recommendations (--parent-id)        │
                           │    comment + child work items (idempotent)   │
                           └─────────────────────────────────────────────┘
```

The pipeline is deliberately **hybrid**: heuristics do the extraction (cheap,
deterministic, private), and a single LLM call does the classification (one
call per run — cost control). Heuristics alone are too noisy; LLM-only
analysis of multi-megabyte logs is too expensive and violates the privacy
constraint (raw logs never leave the machine).

## V3 session-log format consumed

Pi v3 session files are JSONL under `~/.pi/agent/sessions/<project-slug>/`
named `<timestamp>_<session-id>.jsonl`; the `<project-slug>` folder is pi's
own CWD encoding: `--` + CWD with leading separator stripped and every
`/`, `\` or `:` replaced by `-` + `--` (e.g.
`/home/operator/project` → `--home-operator-project--`). The parser mirrors
pi's `getDefaultSessionDirPath` exactly (`project_slug`, deterministic).

Each line is one of:

- `{"type": "session", "version": 3, "id", "timestamp", "cwd"}` — the header.
- `{"type": "model_change", ..., "provider", "modelId"}` — model metadata.
- `{"type": "thinking_level_change", ..., "thinkingLevel"}` — thinking level.
- `{"type": "message", ..., "message": {"role": ..., "content": [...], "usage"}}`

For `message` entries the parser recognises the roles and content parts:

- **assistant** — `content` is a list of parts: `{"type": "thinking",
  "thinking"}` (private chain-of-thought), `{"type": "toolCall", "id",
  "name", "arguments"}` and `{"type": "text", "text"}`. Assistant messages
  may carry an optional `usage` block that is aggregated.
- **toolResult** — carries `toolCallId`, `toolName`, a text-part `content`,
  and `isError`.
- **user / system** — ignored by the signal extraction.

Fail-soft guarantees:

- A line that is not valid JSON is skipped and counted (`malformed_lines`);
  parsing continues.
- A file without a `session` header yields empty ids/cwd (never crashes).
- Assistant messages without a `usage` block keep the zeroed usage dict and
  contribute to the scan-level `missing_usage` counter.

## Heuristic extraction

`extract_signals(sessions, max_sessions=None, errors_only=False)` bundles
four deterministic, side-effect-free extractors:

1. **Tool distribution** — per-tool call counts and error rates. A result
   counts as an error when `isError` is set OR the content matches a
   `TOOL_ERROR_PATTERNS` pattern.
2. **Thinking signals** — keyword hits (`THINKING_KEYWORDS`) inside thinking
   content, each with a centred context window (`CONTEXT_RADIUS` = 120 ch).
   Legitimate uses like "error handling" remain visible so the LLM can
   confirm; per-session excerpt size is capped (`EXCERPT_BYTES_PER_SESSION`
   = 2 KB) to bound the prompt.
3. **Tool errors** — `toolResult` entries with `isError` or error-pattern
   content, classified by `TOOL_ERROR_PATTERNS` kinds (traceback, permission
   denied, command not found, shell expansion, import error, python error).
4. **Retry / self-correction patterns** —
   - `repeated_call`: same tool + canonicalised arguments ≥
     `REPEATED_CALL_MIN_COUNT` (2) times;
   - `call_error_modify`: a tool call → error result → same tool with
     *different* arguments within `ERROR_MODIFY_WINDOW` (3) events.

All ordering is explicit (sorted by session/name/count), so the same input
directory always produces the same signals.

## LLM classification

`classify_patterns(signals, model=..., runner=..., prompt_budget=32 KB)`:

- Builds a prompt containing **only** the distilled signals (counts +
  capped excerpts + session file names) — never raw log content.
- Makes exactly **one** subprocess call:
  `pi -p --print --model <model> --no-tools --no-context-files --no-skills
  --no-extensions <prompt>` (the audit skill's `_call_pi()` pattern; the
  context-reduction flags keep the call cheap and self-contained).
- Instructs the model to reply with a JSON object of exactly this shape:
  `{"categories": [{"name", "confidence", "evidence_sessions", "summary"}],
  "recommendations": [{"title", "issue_type", "target_skill",
  "impact_scope", "priority", "rationale", "evidence_sessions"}]}`. Each
  recommendation is normalised (`_normalise_recommendation`): `impact_scope`
  defaults to `unknown` and `priority` to the issue-type default when the
  model omits or mangles them.
- **Bounds the prompt**: `_trim_signals_for_prompt()` deterministically
  caps the rows carried per signal kind and the excerpt sizes so a real
  20-session run (which can serialise to hundreds of KB) always fits
  `PROMPT_BUDGET_BYTES` (32 KB). Counts and session names on the retained
  rows are preserved and per-kind totals are recomputed. Only a
  pathological payload that still exceeds the budget after trimming
  raises `ClassificationError`.
- `runner` is injectable for deterministic tests (duck-typed:
  `returncode`/`stdout`/`stderr`).

Model selection: `--model` flag or `AGENT_LOG_REVIEW_MODEL` env, default
`Local Proxy/code`.

## Report rendering

`render_report(analysis, fmt="markdown" | "json")` renders a deterministic
document with four sections: **Summary** (sessions analysed, date range from
session start timestamps, fail-soft counters, classification status),
**Top Patterns** (sorted by count desc, then name asc, with session file-name
citations), **Recommended Improvements** (LLM recommendations with rationale
and evidence sessions), and **Evidence Citations** (per session, the pattern
names / recommendation titles citing it). Evidence always cites session
**file names**, never full paths; the JSON output is the same document as a
single parseable document. `--output-file` writes the Markdown report
(commonly to `.worklog/tmp/`).

## Worklog integration

`file_recommendations(recommendations, parent_id, report_markdown=...,
dry_run=False, min_confidence=0.0, ...)`:

1. Reads the parent's existing children (`wl show <parent> --children
   --json`) for idempotence.
2. Files the report as a worklog comment on `parent_id`.
3. For each recommendation at/above `min_confidence`: auto-creates a child
   work item with `--parent <parent_id>`, issue-type `chore`/`feature`,
   priority (from the recommendation, defaulting to `low`/`medium` by
   issue-type), assignee `Map`, and a description that links back via
   `related-to:<parent_id>` and records `target_skill` + `impact_scope`.
   Same-title existing children are **reused**, never duplicated. `--dry-run` prints the planned actions
   without mutating.

All `wl` commands route through the worklog-safe runner: `--worklog-dir` is
resolved via `shared.status_lifecycle.resolve_worklog_flags` (prefix-to-
sibling scan, then cwd chain), so the skill works from worktrees and
non-project cwds. No parent ACs are implemented here — creation only.

## Privacy & redaction

`redact_secrets()` replaces `sk-…` API keys, `Bearer …` tokens and
`.ssh`/`.env` paths with placeholders (module constant
`SECRET_PATTERNS`). It is applied to:

- every prompt sent to the classification model,
- every report (Markdown and JSON output — leaf strings are redacted
  recursively before rendering),
- every worklog comment body.

Raw log content is read only for parsing and is never persisted outside
`.worklog/tmp/`.

## Test strategy

`skill/agent-log-review/tests/test_agent_log_review.py` runs **offline** — no
real `pi`, `wl`, or network calls:

- **Parser (child 1)** — synthetic v3 fixtures via `tmp_path`: mixed content
  parts, `toolResult`/`isError`, malformed JSONL skip+counter, missing usage
  degradation, empty session dir, slug derivation, mtime-sorted max-20
  selection, no-header degradation.
- **Heuristics (child 2)** — distribution counts/error rates, keyword hits
  with context, a borderline false-positive case ("proper error handling")
  kept visible for the LLM, tool-error pattern matching, retry patterns
  (repeated call, call→error→modify, unchanged-args negative case), excerpt
  cap, and `max_sessions`/`errors_only` orchestrator args.
- **Classification (child 3)** — a mocked `pi` runner asserts the prompt
  excludes raw content, stays under the 32 KB budget, parses well-formed
  responses, degrades malformed/empty/failing responses, uses the exact
  context-reduced command, and redacts secrets before sending.
- **Report (child 4)** — required sections in Markdown, deterministic
  ordering, JSON structure, redaction, and end-to-end `build_analysis` +
  render.
- **Worklog (child 5)** — mocked `wl` runner: comment filing, child creation
  with correct issue-type/assignee/`related-to` linking, idempotent re-run
  reusing same-title children, `--dry-run` performing no mutations,
  confidence-threshold skipping, and `--worklog-dir` flag injection.
- **CLI smoke** — `--help` prints usage; `--no-classify --json` runs
  end-to-end against a fixture session directory; `--dry-run --parent-id`
  mutates nothing.

## Extension points

- `THINKING_KEYWORDS` / `TOOL_ERROR_PATTERNS` — module-level constants;
  extend for new error vocabulary without code changes.
- `EXCERPT_BYTES_PER_SESSION`, `CONTEXT_RADIUS`, `REPEATED_CALL_MIN_COUNT`,
  `ERROR_MODIFY_WINDOW`, `PROMPT_BUDGET_BYTES` — tuning knobs.
- `classify_patterns(runner=...)` and `file_recommendations(wl_runner=...)` —
  injectable runners keep tests deterministic and allow alternative backends.
- `AGENT_LOG_REVIEW_MODEL` / `PI_CODING_AGENT_SESSION_DIR` /
  `AUDIT_BENCH_SCRATCH_DIR`-style env overrides — deployment configuration
  without code edits.
