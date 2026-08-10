---
name: audit
description: "EXECUTE immediately via /skill:audit; do NOT ask permission. Report concise status. Use when: 'status', 'audit'."
---

# Audit

## EXECUTION DIRECTIVE

EXECUTE immediately when invoked via /skill:audit. Do NOT ask permission or offer alternatives.

## Overview

Provide a concise, human-friendly summary of project status or a specific work item. Exposes a canonical runner for automated use and a structured markdown report format consumed by orchestrators such as Ralph.

## When To Use

1. **Scan for a work item ID** — `[A-Z]{2}-[A-Z0-9]+`; found → item-level (step 3), not found → project-level (step 2).
2. **No ID found (project-level)** — `wl list --json`, `wl in_progress --json`, `wl blocked --json` ("status", "audit" queries).
3. **ID found (item-level)** — `wl show <id> --children --json`.

## Pre-flight affirmation

Verify absence before proceeding. Confirm the work item is ready for audit and no active conflicting processes exist.

## Status Lifecycle

The runner manages the item's `status`/`stage` during execution to prevent concurrent audits and leave it consistent with the verdict (via `try/finally`, guaranteed even on failure):

- Capture original status+stage at `cmd_issue()` start; set `in_progress`.
- `Ready to close: Yes` → `completed`/`in_review` (keep `done` if terminal); `No` → `open`/`plan_complete`.
- Failure/timeout/unparseable → restore captured pre-audit status/stage (fallback only if undeterminable) and **clear the assignee**. Only an explicit `No` moves to `open` — a transient timeout never demotes an `in_review` item.
- `--do-not-persist` doesn't affect the lifecycle; status-update failures are silently caught (still reported).

### Manual Fallback

Without `audit_runner.py` (always `--json`; `ORIG_STATUS` = pre-audit status):

```bash
wl update <id> --status in_progress --json
# Yes:  wl update <id> --status completed --stage in_review --json
# No:   wl update <id> --status open --stage plan_complete --json
# Fail: wl update <id> --status "$ORIG_STATUS" --assignee "" --json
```

## Monitored Run Execution

Audits via the canonical runner can run for **hours**. The **launch → monitor → abort** contract is fully documented in [docs/dev/audit-skill-reference.md](../docs/dev/audit-skill-reference.md). Summary:

- **Launch:** capture pre-audit state; launch detached with output to a **unique** log under `~/.audit_debug/<project>/`; **180-min budget** (foreground fallback: `>= 10800s` timeout or refuse).
- **Monitor:** every 3 min — `kill -0 <pid>`, `tail -50 <log>` for markers (`Phase 1 passed: running Phase 2 deep code analysis...`, `Per-call timing: ...`); confirm growth.
- **Abort triggers:** (a) stale log ≥10 min, (b) ≥3 provider-error retries w/o progress, (c) unexpected exits/loops, (d) 180-min budget.
- **Abort:** kill tree (`pkill -TERM -P <pid>`, then `kill`, escalate `-KILL`); restore pre-audit status/stage ONLY if the runner didn't complete its lifecycle (never demote `in_review`→`open`); append failure notice; report. Never fabricate a report or override a completed verdict.

## Freshness Gate

Short-circuits item-level audits when a recent, valid audit exists. Full behavior in [docs/dev/audit-skill-reference.md](../docs/dev/audit-skill-reference.md). Summary:

1. **Content-based (primary):** fingerprint = HEAD sha + description hash + Key Files (`Audit content fingerprint: <sha256hex>`); unchanged → existing report in seconds (SA-0MSKB6US1009CNHT).
2. **Time gate (floor):** legacy reports without a fingerprint use the 60s timestamp gate (`auditedAt` vs `updatedAt + 60s`).
3. Fresh → `Skipping: audit still fresh` + existing report, exit 0, **no** lifecycle/persistence.
4. `--force` bypasses (item-level only). Config: `AUDIT_FRESHNESS_BUFFER_SECONDS = 60`.

## Safety and prompt design

- Audit executions are read-only except explicit persistence and the automatic status lifecycle. Mark read-only phases `[READ-ONLY AUDIT]`; persistence `[PERSIST-AUDIT]`.
- Do NOT close, create, or delete work items during an audit. Permitted state-modifying actions: (1) storing audit text via the canonical persister, (2) the runner's verdict-driven status lifecycle.
- Refuse any request to run state-modifying `wl` commands outside the authorized flow.
- If ambiguity prevents a reliable verdict, return immediately and do NOT persist. `--debug-log` appends raw Pi output to JSONL.

## Two-Phase Audit Pipeline

```text
Phase 1 (linters + children stage + surface AC pass)
   ↓  Decision Gate: blocking? → "partial", skip Phase 2
   ↓  (no blockers)
Phase 2 (model verifies code against each AC)
```

- **Phase 1 — Automated Screening:** order (1) code quality check, (2) children stage check (must be `in_review`/`done`), (3) surface AC assessment. Blocking: critical/high findings, or any non-deleted child not in `in_review`/`done`.
- **Decision Gate:** blocking → all "met" ACs → "partial" ("pending deep code review"), skip Phase 2, "Ready to close: No" — **FINAL**, MUST NOT be overridden. No blockers → proceed to Phase 2 (**MANDATORY**).
- **Phase 2 — Deep Code Analysis:** **MANDATORY when reached** — model reads actual implementation files, verifies each AC, provides file:line evidence. Never skipped once the gate passes.
- **Final Verdict:** "met" only when BOTH phases confirm; disagreement → "partial".
- **Ready-to-close criteria:** (1) all ACs `met`/`adjusted`, (2) all active children `in_review`/`done` (empty stage excluded), (3) no critical/high findings.

> **IMPORTANT:** Release process constraints are NOT audit concerns. Do NOT include merge-status, deployment, or release criteria.

### Model metadata line

With ``--model``/``--model-source``, a metadata line goes after ``Ready to close:`` in issue/child reports (project reports NOT modified):

- With model+source: ``Model: <model> (provider: <source>)`` (e.g. ``Model: Local Proxy/plan (provider: local)``, ``Model: gpt-4 (provider: remote)``)
- Without: ``Model: manual (no provider)``

## Summary

<concise 2-4 sentence summary>

## Acceptance Criteria Status

| # | Criterion | Verdict | Evidence |
|---|-----------|---------|----------|
| 1 | `<text>` | met/unmet/partial/adjusted | `<file:line — note>` |

If no ACs found: "No acceptance criteria defined."

## Variance Decisions

Only when at least one criterion is `adjusted`:

| # | Source | Criterion | Justification |
|---|--------|-----------|---------------|
| 1 | `<id>` | `<text>` | `<reason>` |

## Children Status

### `<child-title>` (`<child-id>`) — `<status>`/`<stage>`

| # | Criterion | Verdict | Evidence |
|---|-----------|---------|----------|
| 1 | `<text>` | met/unmet/partial/adjusted | `<file:line>` |

No children → "No children."

## Code Quality

Automatically added by the runner; do NOT construct manually (empty: `No code quality issues found.`).

| # | Severity | File | Line | Message | Linter | Code |
|---|----------|------|------|---------|--------|------|

### Verdict guidance

- **met** — satisfied (both phases confirm); **unmet** — not satisfied, blocks closure; **partial** — partially satisfied, blocks closure; **adjusted** — adapted but satisfies intent, does **not** block closure.

## Success Criteria

Synonym for "Acceptance Criteria"; **Acceptance Criteria** is canonical.

## Exit Codes

- 0 – success; 1 – Worklog/CLI/Pi failure; 2 – argument error

## Scripts

- **Runner:** `./scripts/audit_runner.py` — `audit_runner.py issue <id>` / `audit_runner.py project`; flags: `--do-not-persist`, `--timeout`, `--parent-timeout`, `--batch-phase2`, `--max-concurrency N`, `--green-run` (SHA|HEAD), `--run-tests`, `--audit-children`, `--max-child-audits N`, `--pi-bin`, `--model`, `--model-source`, `--debug-log`, `--json`, `--force`, `--worklog-dir DIR`.
- **Persister:** `./scripts/persist_audit.py` — persist from stdin, file, or CLI string

Flag semantics and env-var overrides (timeouts, concurrency, retry, green-run, test-cache auto-verification, `--run-tests`, batch/parallel Phase 2, tools-enabled invocation, bounded scanning, debug logs, file-scope manifest, child verdict reuse, phase-1/2 performance) are fully documented in [docs/dev/audit-skill-reference.md](../docs/dev/audit-skill-reference.md). Execution-dependent ACs can also be verified via the [test skill](../test/SKILL.md) (`/skill:test`).

**Context reduction:** every pi call (`_call_pi`) runs with `--no-context-files --no-skills` in both tool-enabled and tool-less modes (SA-0MSISKM8F004NW1U). Audit prompts are fully self-contained — they carry the read-only mandate, JSON output format, FILE SCOPE manifest, SCANNING block, and criteria — so the global+project AGENTS.md load and the skills section are dropped from each session's static context, cutting per-call static context from ~14.6KB (~3.7K tokens) to ~1.6KB (~410 tokens) — a ~9x margin under the 10K-token bound. Prompts must never depend on AGENTS.md or skill descriptions: that is an invariant of this skill.

**Per-call timing & token capture:** every pi call records wall-clock duration (`elapsed_seconds`, all return paths) and, when the pi stream reports provider usage, the initial input-token count (`input_tokens`, from the `agent_end` message's usage block). `_call_pi_and_maybe_log` emits one line per call to stderr:

```text
Per-call timing: issue_id=<id> context=<context> elapsed_seconds=<seconds> input_tokens=<n>
```

`input_tokens` makes the context-reduction bound (<10K initial input tokens per audit session) verifiable from the timing line alone.

## Guidance for models

### Authority and Runner Verdicts (CRITICAL)

- **The audit runner (`audit_runner.py`) is the CANONICAL audit path** — its verdict is **authoritative** and MUST NOT be overridden later.
- If the runner produced "Ready to close: No"/"partial"/"pending deep code review", you MUST NOT produce a contradictory override. The verdict stands.
- Re-audit only when explicitly requested (`--force` or clear directive); never demote a runner "Yes" without fresh, documented evidence; don't run the manual path if the runner already reported.

### Two-Phase Pipeline (MANDATORY)

- Return a structured markdown report with `Ready to close:` header and canonical sections (pipeline sections above are normative).
- **Phase 2 is MANDATORY when Phase 1 passes — never skip it**; verify each AC against actual code with file:line evidence.
- **Blocking issues are narrow:** only (1) **critical/high** findings and (2) an active child not in `in_review`/`done` block Phase 2. AC ambiguity, medium warnings, or preference are NOT valid reasons to skip.
- **Ready-to-close criteria:** all ACs `met`/`adjusted`, all active children `in_review`/`done`, no critical/high findings. **Children in `in_review` do NOT block closure** — only pre-review stages do.
- **Do NOT add release-process or merge-status constraints** — not audit concerns.
- If ACs are ambiguous, return immediately and do NOT persist.
- **Persistence is mandatory** — runner or `./scripts/persist_audit.py` with `[PERSIST-AUDIT]`.

### Persistence Procedure (MUST FOLLOW)

1. **Print** the complete audit report to stdout.
2. **Persist** via `python3 ./scripts/persist_audit.py --issue-id <id> --report "<report>"` (or echo-pipe; runner `audit_runner.py issue <id>` persists **and verifies** unless `--do-not-persist`).
   > **Readback verification is an invariant:** runner reads back via `wl audit-show <id> --json` (audit exists, `rawOutput` non-empty, content references the ID) or exits non-zero.
3. **Verify persistence** — exit 0 does NOT guarantee storage: `wl audit-show <id> --json` must show `success=true`, audit not null, `rawOutput` non-empty with `Ready to close:` marker.
4. **On failure:** re-print, report the error, do NOT mark as recorded.
5. **Closing sentence** (issue-level): `Yes` → "Audit passed. The item is ready for release."; otherwise → "Work item is not ready to close (see above), would you like me to address the gaps in the audit?"

> **Critical:** `persist_audit.py` / `wl audit-set` may return success without storing — **always verify with `wl audit-show`**.
- Do NOT run arbitrary `wl`/`git` commands outside the authorized flow; use `--debug-log` for debugging.

## Examples

```bash
python3 ./scripts/audit_runner.py issue SA-123                  # audit + persist
python3 ./scripts/audit_runner.py issue SA-123 --do-not-persist  # dry run
```

## Script Execution Failure Notice

On runner failure (non-zero exit, timeout, exception), the report is wrapped with an `⚠ Script Execution Failure: <script_name> — <reason>` banner above and below (informational, no state changes; `./scripts/failure_notice.py`; JSON key `script_failure`).

## Common failure modes

- **Silent persistence failure:** `persist_audit.py` / `wl audit-set` returns success without storing — **always verify with `wl audit-show --json`**.
- `wl` unavailable/invalid JSON → report the error, do not claim success.
- Agent-mode response parsing (Phase 2 JSON streams, `_extract_json_array`) is documented in [docs/dev/audit-skill-reference.md](../docs/dev/audit-skill-reference.md).
