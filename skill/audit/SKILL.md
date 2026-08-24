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
2. **No ID found (project-level)** — scoped status overview only (never a bare full dump): `wl next --json`, `wl in_progress --json`, `wl list --stage in_review --json`, `wl list --priority critical --status open --json`, `wl recent --number 10 --json` ("status", "audit" queries).
3. **ID found (item-level)** — `wl show <id> --children --json`.

## Pre-flight affirmation

Implemented as a lightweight entry guard in `cmd_issue` (SA-0MSL1Z1WU005O5IY). Before the status lifecycle runs, the runner checks the item's current status:

- **Item already `in_progress` at entry + no `--force`** → abort with exit 1: `Error: Refusing to audit <id>: the work item is already in_progress (a concurrent audit or implementation owns it). Pass --force to bypass this pre-flight guard.` No report is produced, nothing is persisted, and the pre-audit status/stage are untouched (the guard runs before the runner sets `in_progress`). This prevents two audits of the same item racing — both would set `in_progress`, run the pipeline, and the last writer would win on persist + status transition. The concurrency semaphore (`../shared/process_semaphore.py`) caps pi subprocesses host-wide but cannot see per-item claims.
- **`--force`** → bypass the guard and proceed (also bypasses the freshness gate).
- **Implementation self-review audits** (implement skill Step 6) audit in-progress items, so they pass `--force`.

## Status Lifecycle

The runner manages the item's `status`/`stage` during execution to prevent concurrent audits and leave it consistent with the verdict (via `try/finally`, guaranteed even on failure):

- Capture original status+stage at `cmd_issue()` start; set `in_progress`.
- `Ready to close: Yes` → `completed`/`in_review` (keep `done` if terminal); `No` → `open`/`plan_complete`. A top-level item (no parent) with a `Yes` verdict also gets `needsProducerReview: true` (via `--needs-producer-review yes`) so the release gate (`closeWorkItemsAfterRelease`) blocks until the producer reviews it; child items (with a parent) are **not** flagged — their parent's review covers the subtree (SA-0MSSVKYEW008PJ9H).
- A completed `Yes` verdict advances **even when AC evidence was fallback-tainted** (e.g. a read-only test skip with a variance note) — a fallback on some ACs does not invalidate an explicit model `Yes` (WL-0MSN7XAUS008WOPQ). The fallback flag only forces the restore path for a `No` verdict (an infra-fallback `No` is not an explicit model assessment).
- Failure/timeout/unparseable → restore captured pre-audit status/stage (fallback only if undeterminable) and **clear the assignee**. Only an explicit `No` moves to `open` — a transient timeout never demotes an `in_review` item. If a completed `Yes` run is ever restored (script failure during the run), a visible warning is printed — never a silent divergence.
- **Verified transitions (WL-0MSVVFBJ2003RRYK):** after the terminal `wl update` the runner reads back the item via `wl show <id> --json` and confirms the status/stage actually changed to the expected values (`completed`/`in_review`, `open`/`plan_complete`, or the restored pre-audit state). A `wl update` that exits 0 without applying (silently swallowed) is retried up to 3 times with short delays; if still unverified the runner prints a loud diagnostic, best-effort restores the captured pre-audit state, and exits **non-zero** — a passing audit can never leave an item stranded in its pre-audit stage silently.
- `--do-not-persist` doesn't affect the lifecycle; status-update failures are retried and verified, then surfaced loudly (never silently caught).

### Manual Fallback

Without `audit_runner.py` (always `--json`; `ORIG_STATUS` = pre-audit status):

```bash
wl update <id> --status in_progress --json
# Yes (top-level, no parent):  wl update <id> --status completed --stage in_review --needs-producer-review yes --json
# Yes (child item):            wl update <id> --status completed --stage in_review --json
# No:   wl update <id> --status open --stage plan_complete --json
# Fail: wl update <id> --status "$ORIG_STATUS" --assignee "" --json
```

## Fail-Fast Launch Contract

An audit MUST be launched from the project root that owns the work item
(LP-0MSQ32HNR007AI6B). Before any phase runs — and before any pi/model call —
the runner verifies the launch context:

1. **Owning project check:** the work item's id prefix is resolved to its
   owning project via the worklog prefix-to-sibling scan (explicit
   `--worklog-dir` takes precedence: its parent is the expected project).
   If the launch cwd's git root (`TARGET_PROJECT_ROOT`) does not own the
   item, the run aborts with `Error: Audit launch-context error: ...` and a
   non-zero exit — zero pi calls, no status lifecycle, no persisted report.
2. **FILE SCOPE manifest check:** before Phase 1 and again before Phase 2,
   the FILE SCOPE manifest must reference the item repository's files. A
   manifest built from the audit skill's own tree (or lacking the item
   repo) aborts with `Error: Audit scope error: ...` and a non-zero exit
   instead of emitting misleading "unmet" verdicts.
3. **Child persistence is fatal:** a child audit that cannot be persisted
   (`wl audit-set` rc!=0, e.g. "Work item not found") aborts the run with a
   non-zero exit — a parent report whose child audits never landed is
   misleading. `PERSIST_CONTENT_INVALID` (fallback notice persisted) stays
   a warning; the child audit is usable.

A mis-scoped audit is indistinguishable from a failed audit, so it MUST fail
fast (seconds, no pi calls) rather than waste model time. To re-launch
correctly, cd into the owning project root (a worktree of the owning project
counts as owning).

**Git scope follows the worklog (SA-0MSLLGDW00098UCC).** The runner's
git-derived content — the file-scope manifest (changed-file list + repo
index), HEAD attestations, working-tree hashes, and `--green-run` evidence
— resolves against the **worklog-derived owning project root**
(`--worklog-dir` parent, else prefix-to-sibling scan), not the launch cwd.
Launching from any cwd, the git-derived content reflects the audited
project's repository, and an undeterminable owning root aborts with
`Error: Undeterminable project scope: ...` before any phase runs (never a
silent fallback to the launch cwd's repo). Launching from inside the
owning project — or a worktree of it (same git repository) — keeps git
resolving to that checkout, so worktree-only changes and the worktree
branch HEAD stay correct. The remaining `TARGET_PROJECT_ROOT` consumers
(code-quality scan and debug-log path) are still launch-cwd-bound.

## Monitored Run Execution

Audits can run for **hours**; the **launch → monitor → abort** contract is in [docs/dev/audit-skill-reference.md](../../docs/dev/audit-skill-reference.md). Summary:

- **Launch:** detached, unique log under `~/.audit_debug/<project>/`; **180-min budget**.
- **Monitor:** every 3 min — `kill -0 <pid>`, `tail -50 <log>` (markers: `Phase 1 passed: running Phase 2 deep code analysis...`, `Skipping Phase 2 deep analysis: effort=... risk=...`, `Per-call timing:`); confirm log growth.
- **Abort:** stale log ≥10 min, ≥3 provider-error retries w/o progress, unexpected exits/loops, or 180-min budget → kill tree (`pkill -TERM -P` → `kill` → `-KILL`); restore pre-audit status/stage only if the runner didn't complete its lifecycle (never demote `in_review`→`open`); append failure notice (progress summary: elapsed time, last phase marker, trigger) and **report the outcome to the operator** (run id, log path, trigger, restored status/stage). Never fabricate a report or override a completed verdict.

### In-process per-call safeguards (complementary to the external monitor)

The runner also protects individual Pi calls in-process, so the external monitor is a **backstop** rather than the primary abort mechanism (LP-0MSQ32S2M001EA74):

- **Short child Phase-1 screen budget:** lightweight child Phase-1 AC-review screens use a short per-call budget (default 600 s; `AUDIT_CHILD_SCREEN_TIMEOUT` env or `--child-screen-timeout` flag). A screen that exceeds its budget returns a clean timeout verdict (`_timeout` marker + timeout evidence) — never a full 1800 s burn. Phase 2 calls (parent + child deep analysis) and parent Phase-1 screens keep the 1800 s budget.
- **In-process stall abort:** any single Pi call (Phase 1 or Phase 2) that produces no output for ≥ `AUDIT_STALL_TIMEOUT` seconds (default 600 = 10 min) is aborted in-process inside `_call_pi` (kill + drain) with a `_timeout` verdict and stall evidence, instead of waiting out the remaining per-call budget. The external stale-log abort (≥10 min) remains as a backstop.
- **Slot-aware child-call concurrency:** the parallel child-call ceiling (Phase-1 child AC review and Phase-2 child deep analysis) is derived dynamically from the local proxy slot status (`/llama/local/status` → `available_slots`/`total_slots`; `AUDIT_SLOT_STATUS_URL`, short 1 s timeout, fail-open). The ceiling is `min(free-slots, configured_max)` with a floor of 1; when the slot query fails the runner degrades to the configured static ceiling (`AUDIT_MAX_CHILD_CONCURRENCY` > `AUDIT_PARALLELISM` > 2).

## Freshness Gate

Short-circuits item-level audits when a recent, valid audit exists. Full behavior in [docs/dev/audit-skill-reference.md](../../docs/dev/audit-skill-reference.md):

1. **Content-based (primary):** fingerprint = HEAD sha + description hash + Key Files + working-tree state (`git status --porcelain` + `git diff --name-only HEAD`); unchanged → existing report (SA-0MSKB6US1009CNHT).
2. **Time gate (floor):** legacy reports use the 60s gate (`auditedAt` vs `updatedAt + 60s`).
3. Fresh → `Skipping: audit still fresh`, exit 0, **no** lifecycle/persistence.
4. `--force` bypasses. Config: `AUDIT_FRESHNESS_BUFFER_SECONDS = 60`.

## Safety and prompt design

- Audit executions are read-only except explicit persistence, the automatic status lifecycle, and the ruff config remediation loop. Mark read-only phases `[READ-ONLY AUDIT]`; persistence `[PERSIST-AUDIT]`.
- **READ-ONLY exception — ruff config remediation (SA-0MSSSNOZN000LQKR, Phase B):** when the false-positive screen classifies a critical/high ruff finding `confident-false-positive`, the pipeline MAY apply a MINIMAL, surgical config fix to silence it: `per-file-ignores` entries for the flagged file+rule pairs only, in `ruff.toml` or the `pyproject.toml` `[tool.ruff]` section (created if absent), committed locally (no push) with the work item referenced; the content fingerprint is re-hashed after each commit and the code-quality scan re-run (`fix=False`, same changed-file scope — the pipeline is never restarted). Capped at 3 config-fix iterations per audit run (`AUDIT_REMEDIATION_MAX_ITERATIONS`); a finding persisting past the cap stays blocking `genuine` annotated "remediation loop exhausted". `uncertain` and non-blocking (medium/low) findings never enter the loop. Each applied config fix is tracked by a `chore` work item linking the finding + commit sha, and each medium/low confident-false-positive finding gets a tracking chore (no commit link, annotated "candidate false positive — producer decision required") — see SA-0MST01PQQ009T0CI. The exception applies ONLY when the model classifies the finding `confident-false-positive` AND the no-breakage verification (T3) is green; it never closes or deletes work items and never pushes. Full documentation in the audit skill reference (D1).
- Do NOT close or delete work items during an audit. Permitted state-modifying actions: (1) storing audit text via the canonical persister, (2) the runner's verdict-driven status lifecycle, (3) **creating a `chore` work item to track a false-positive config fix or a medium/low confident-false-positive finding** (the ONLY relaxation of the no-create rule — SA-0MST01PQQ009T0CI). Chore creation is fail-safe: a `wl create` failure never reverts the remediation commit; the finding stays blocking `genuine` and the failure is recorded in the report. `uncertain` and `genuine` findings never get a work item.
- Refuse any request to run state-modifying `wl` commands outside the authorized flow.
- If ambiguity prevents a reliable verdict, return immediately and do NOT persist. `--debug-log` appends raw Pi output to JSONL.

## Two-Phase Audit Pipeline

```text
Phase 1 (merge gate + linters + children stage + surface AC pass)
   ↓  Decision Gate: blocking? → "partial", skip Phase 2
   ↓  (no blockers)
Phase 2 (model verifies code against each AC)
```

- **Phase 1 — Automated Screening:** order (0) **merge gate** (SA-0MT456M27001LRTL), (1) code quality check, (2) children stage check (must be `in_review`/`done`), (3) surface AC assessment. Blocking: critical/high findings (unless screened as a confident false positive), or any non-deleted child not in `in_review`/`done`.
- **Merge gate (Phase 1 step 0, SA-0MT456M27001LRTL):** BEFORE any screening the runner guarantees the item under audit is integrated into its owning repository's `dev` branch. The item's integration evidence is resolved GENERICALLY from the item itself (owning repo via the worklog prefix-to-sibling scan; commits from the item's description/comments; the `wl-<id>-*` feature branch via `git for-each-ref`) — never a hardcoded commit or repo. Each candidate is verified with `git merge-base --is-ancestor <commit> origin/dev`; the command + result are recorded in the report's `## Merge Gate Evidence (Phase 1)` section. When the work is NOT merged the gate integrates it into the owning repo's `dev` (fetch dev, fresh worktree at origin/dev, merge/cherry-pick the item's branch/commits, build, run the project test suite, push `dev` — NEVER `main`), then re-verifies the pushed commit is an ancestor of `origin/dev`. When integration cannot complete (missing commit, conflicts, build/test failure, push failure, any blocker) the audit FAILS CLOSED: "Ready to close: No" + `--needs-producer-review yes`, and the pipeline never proceeds past Phase 1 with unmerged work (AC3). Fail-open exceptions (never blockers): an item with NO resolvable evidence (docs/admin item, no feature branch, no referenced commits) and an owning repo with NO dev baseline (neither `origin/dev` nor a local `dev` — no dev target to be missing from). Items whose work is already in `dev`, and repos lacking any dev baseline, simply record the evidence and proceed.
- **False-positive screen (SA-0MSSSNOZN000LQKR):** after the code-quality scan, ruff findings are classified by a single batched Pi call (`_screen_ruff_findings` in `audit_runner.py`, context `false-positive-screen`, child-screen timeout budget) into `genuine` / `confident-false-positive` / `uncertain` with per-finding written justifications, surfaced in the report (`#### False-positive screen` table) and `_build_issue_json` (`code_quality.false_positive_screen`). Caution-first: a finding missing from the batch, unparseable output, provider error, timeout, concurrency-limit marker, or pi failure defaults EVERY finding to `uncertain` (never `confident-false-positive`) and marks infra-failure provenance (`ac_fallback_used`) so a failed screen restores the pre-audit state instead of demoting. Only `confident-false-positive` critical/high findings stop blocking closure; `uncertain` findings stay blocking annotated `candidate false positive — producer decision required`. Medium/low confident-false-positives are classified and reported but never flagged remediable (remediation is blocking-severity only — F2/T2 scope). The screen is skipped entirely (zero Pi calls) when the scan yields no ruff findings; non-ruff findings are never sent to it.
- **Decision Gate:** blocking → all "met" ACs → "partial" ("pending deep code review"), skip Phase 2, "Ready to close: No" — **FINAL**, MUST NOT be overridden. No blockers → proceed to Phase 2 (**MANDATORY**), **except** the narrow low-risk/small-item skip below.
- **Phase 2 — Deep Code Analysis:** **MANDATORY when reached** — model reads actual implementation files, verifies each AC, provides file:line evidence. Never skipped once the gate passes, except for the single, unconditional exception in the next bullet.
- **Defensive evidence handling (SA-0MSKM2LSP006L0K8):** models occasionally emit `evidence` as a structured JSON object (`{file, line, note}`) instead of the requested `path/file:line` string. All evidence consumers normalize through the shared `_evidence_text()` helper (dict/list → `json.dumps`, other scalars → `str()`), and Phase 2 merge sites normalize before writing into `ac_results` — so gap mapping, file-scope refs, infra-marker detection, and report assembly never crash or silently miss on non-string evidence. Verdict semantics and conservative fail-closed gap mapping are unchanged.
- **Evidence-scope cap (LP-0MSQ32WM5000NCB7):** the Phase 2 deep prompts (parent `phase2_deep`, child `phase2_child`, batch `phase2_batch`) instruct the model to cite **at most N file:line references per criterion, minimum 1** — a prompt-level bound that shortens evidence-JSON generation (the dominant Phase 2 cost) without changing the model or verdict semantics. Default N is 5; resolve via `--max-citations-per-ac N` (highest), the `audit.max_citations_per_ac` key in the CWD `.ralph.json`/`ralph.config.json`, or the hardcoded default. Invalid values (0/negative/non-int) fail closed to the default with a warning. Parsed evidence/verdicts are never mutated — the canonical report format is preserved. Trade-off: fewer citations per AC shortens deep analysis but narrows evidence breadth; the ≥1 file:line floor keeps every verdict substantiated.
- **Low-risk/small-item exception (SA-0MSQ026T3009QY2L):** when a work item has `effort` ∈ {Extra Small, Small} **and** `risk` = Low, Phase 2 deep analysis is skipped — Phase 1 verdicts stand unchanged (`met` remains `met`) and the report/evidence records the skip reason. The rule applies tree-wide: the parent and every child in the cascade are evaluated independently against the criterion. **Fail-closed:** missing/unknown `effort` or `risk` ⇒ Phase 2 runs as usual (never skip on absent data). No override flag or env var forces deep analysis for a qualifying node — the skip is unconditional.
- **Final Verdict:** "met" only when BOTH phases confirm; disagreement → "partial".
- **Ready-to-close criteria:** (1) all ACs `met`/`adjusted`, (2) all active children `in_review`/`done` (empty stage excluded), (3) no critical/high findings.

> **IMPORTANT:** Release process constraints are NOT audit concerns. The ONE exception is the Phase 1 merge gate (SA-0MT456M27001LRTL) above: it verifies the audited item's work is integrated into its owning repo's `dev` and fails the audit closed when integration cannot complete. Every other deployment/release criterion stays out of scope.

### Tiered Phase 1 model (SA-0MSKB697P000T3HG)

Phase 1 parent + child AC screening can run on a fast/cheap model while Phase 2 deep analysis keeps the full model:

- **Config key:** `model.audit_phase1` in the CWD `.ralph.json` / `ralph.config.json` (same resolution shape as `model.audit` — dotted, nested, or `model.remote.audit_phase1` / `model.local.audit_phase1` source-mapped).
- **Default (safe):** when `model.audit_phase1` is absent, Phase 1 resolves to the full `model.audit` model — behavior is byte-for-byte identical to a single-model audit. The flag defaults OFF.
- **Resolution order (Phase 1 model):** 1. `--phase1-model` CLI flag (explicit phase-1 override), 2. `--model` CLI flag, 3. `model.audit_phase1` config, 4. `model.audit` (full model), 5. `DEFAULT_MODEL` (`Local Proxy/plan`). Phase 2 always resolves via `model.audit` (1. `--model`, 2. config, 3. default).
- **Wall-clock target:** Phase 1 per-call < **60s** on a healthy proxy when a fast model is configured (baseline: 1,348s avg / max 2,400s — see `docs/dev/audit-phase2-measured-report.md`). Per-call `Per-call timing:` stderr lines remain the observability surface.
- **Safe runtime fallback (AC4):** when the fast Phase 1 model cannot produce reliable batched verdict JSON (unparseable output, provider error, or concurrency-limit timeout) AND a distinct full model is configured, the SAME Phase 1 screen is retried once with the full model before falling back to `partial` diagnostics. With the default config (`audit_phase1` absent) the retry is a no-op. An infra failure on the fast attempt that succeeds on the full-model retry keeps the conservative `ac_fallback_used` provenance (restore-not-demote).

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

- **Runner:** `$(skill_path audit)/scripts/audit_runner.py` — `audit_runner.py issue <id>` / `audit_runner.py project`; flags: `--do-not-persist`, `--timeout`, `--parent-timeout`, `--batch-phase2`, `--max-concurrency N`, `--green-run` (SHA|HEAD), `--run-tests`, `--no-execute`, `--audit-children`, `--max-child-audits N`, `--max-citations-per-ac N`, `--pi-bin`, `--model`, `--phase1-model`, `--model-source`, `--debug-log`, `--json`, `--force`, `--worklog-dir DIR`.
- **Persister:** `$(skill_path audit)/scripts/persist_audit.py` — persist from stdin, file, or CLI string; cwd-independent — the worklog store is auto-resolved from the work-item id prefix (prefix-to-sibling scan, cwd-chain fallback) when `--worklog-dir` is omitted, so it persists to the item's own store from any cwd (SA-0MSKQERKH002IBLG).

Flag semantics and env-var overrides (timeouts, concurrency, retry, green-run, test-cache auto-verification, `--run-tests`, batch/parallel Phase 2, tools-enabled invocation, bounded scanning, debug logs, file-scope manifest, child verdict reuse, phase-1/2 performance) are fully documented in [docs/dev/audit-skill-reference.md](../../docs/dev/audit-skill-reference.md). Execution-dependent ACs can also be verified via the [test skill](../test/SKILL.md) (`/skill:test`).

**Automatic full-suite verification (SA-0MSIU5HFI0024D7W / SA-0MSJELL44009XYIL):** the runner auto-verifies execution-dependent ACs from a green cached full-suite run (read-only `query_cached()`, never executes the suite). Suite commands are repo-aware — only node suite dirs that exist under the target repo are required (`tests/node`, `tests/cli`, `tests/unit`; missing dirs skipped), so any layout can auto-verify. When verification fails, the runner prints a clear diagnostic distinguishing a cache miss (run `/skill:test` / `run_tests.py --force` once at HEAD to populate the cache, then re-audit) from a non-zero cached run (suite is red — fix or attest with `--green-run HEAD`); execution-dependent ACs stay `partial`. Failed runs get a short 5-min cache TTL so transient infra failures are not re-served as current results.

**Default auto-execution on cache miss (F3, SA-0MSTN5KRF0097TVP):** when the read-only cache cannot satisfy the evidence (a cache **miss** — no cached green full-suite run at HEAD and no `--green-run` attestation), the audit now AUTO-EXECUTES the repo's actual suite via the test skill (`run_tests.py` / `full_suite_commands`), triages any failures per the test skill, and refreshes the per-repo cache so subsequent audits auto-verify read-only. A green executed run injects the **TEST-SKILL GREEN RUN** evidence block (execution-dependent ACs MAY be marked met); a red run is fail-open — no block, execution-dependent ACs stay `partial` with failure evidence, and the audit continues (never blocks). Operators can opt out with `--no-execute` / `AUDIT_NO_EXECUTE=1` to proceed fail-open partial without executing the suite; `--run-tests` remains the explicit override that executes on ANY non-green state.

**Never-block guarantee (F4, SA-0MSTN8CWM003AAU9):** the audit NEVER exits with a hard block solely because it cannot run tests — no cache, no test runner, no configured suite commands, execution impossible. The old pre-flight hard gate (SA-0MSQ72BVV0011SRU) was removed; every such case degrades to a fail-open `partial` verdict with a clear diagnostic. Verification order for execution-dependent ACs: **(1) read-only cache** (`query_cached()`, green within TTL → AUTO-VERIFIED GREEN RUN) → **(2) auto-execute** on a cache miss via the test skill (green → TEST-SKILL GREEN RUN; red → partial + triaged `test-failure` items) → **(3) partial with documented reason** (red/error/empty cache states, `--no-execute`, or an unresolvable suite command set).

**Per-project suite extension (F2, SA-0MSTMYE79006NA61):** a `.pi/test-config.json` file at the repo root overrides convention detection — `{"suiteCommands": ["..."], "timeoutPerCommand": 600}` — so a bespoke suite (e.g. a monorepo package command) is executed exactly as configured. Resolution order: extension file > npm-test convention (`npm --silent test`) > pytest (only when the repo declares a pytest suite) + node suite dirs (`tests/{unit,node,cli}` that exist).

**Context reduction (SA-0MSISKM8F004NW1U):** every `_call_pi` runs with `--no-context-files --no-skills` in both tool-enabled and tool-less modes. Audit prompts are fully self-contained — they carry the read-only mandate, JSON output format, FILE SCOPE manifest, SCANNING block, and criteria — so the duplicated global+project AGENTS.md load and the skills section are dropped from each session's static context (an 88% reduction, ~23x margin under the 10K-token bound; prompts must never depend on AGENTS.md or skill descriptions — that is an invariant of this skill). Per-call timing + verification script + recorded AC2/AC3 evidence: [docs/dev/audit-skill-reference.md](../../docs/dev/audit-skill-reference.md), [evidence/](evidence/).

**Session-id traceability (SA-0MSNYMKV7005P0H9):** every pi subprocess invocation carries a descriptive `--session-id` so sessions can be traced back to the work item being audited. `_call_pi()` builds `audit-{issue_id}-{context}-{uuid8}` (8-hex-char UUID, fresh per invocation; colons in context values like `child:SA-XXX` are replaced with underscores so the id passes `assertValidSessionId`); `audit_pr.py`'s `run_audit_in_worktree()` builds `audit-{wl_id}-entrypoint-{uuid8}` but only when `dry_run=False`. Sessions appear under `~/.pi/agent/sessions/` keyed by the descriptive id instead of a random UUID.

## Guidance for models

### Authority and Runner Verdicts (CRITICAL)

- **The audit runner (`audit_runner.py`) is the CANONICAL audit path** — its verdict is **authoritative** and MUST NOT be overridden later.
- If the runner produced "Ready to close: No"/"partial"/"pending deep code review", you MUST NOT produce a contradictory override. The verdict stands.
- Re-audit only when explicitly requested (`--force` or clear directive); never demote a runner "Yes" without fresh, documented evidence; don't run the manual path if the runner already reported.

### Two-Phase Pipeline (MANDATORY)

- Return a structured markdown report with `Ready to close:` header and canonical sections (pipeline sections above are normative).
- **Phase 2 is MANDATORY when Phase 1 passes — never skip it**; verify each AC against actual code with file:line evidence. **Sole exception (SA-0MSQ026T3009QY2L):** items with `effort` ∈ {Extra Small, Small} **and** `risk` = Low skip Phase 2 — Phase 1 verdicts stand unchanged, evidence notes the skip, and the rule is fail-closed (missing/unknown values ⇒ deep analysis runs). Applies independently to the parent and every child in the cascade; unconditional (no override flag/env).
- **Blocking issues are narrow:** only (1) **critical/high** findings and (2) an active child not in `in_review`/`done` block Phase 2. AC ambiguity, medium warnings, or preference are NOT valid reasons to skip.
- **Ready-to-close criteria:** all ACs `met`/`adjusted`, all active children `in_review`/`done`, no critical/high findings. **Children in `in_review` do NOT block closure** — only pre-review stages do.
- **Do NOT add release-process or merge-status constraints** — not audit concerns. Sole exception: the Phase 1 merge gate (SA-0MT456M27001LRTL) verifies the item's work is integrated into its owning repo's `dev` and fails closed when integration cannot complete; everything else stays out of scope.
- If ACs are ambiguous, return immediately and do NOT persist.
- **Persistence is mandatory** — runner or `$(skill_path audit)/scripts/persist_audit.py` with `[PERSIST-AUDIT]`.

### Persistence Procedure (MUST FOLLOW)

1. **Print** the complete audit report to stdout.
2. **Persist** via `python3 $(skill_path audit)/scripts/persist_audit.py --issue-id <id> --report "<report>"` (or echo-pipe; runner `audit_runner.py issue <id>` persists **and verifies** unless `--do-not-persist`). The persister targets the work-item's own worklog store from any cwd (auto-resolved via the shared prefix-to-sibling scan when `--worklog-dir` is omitted; an explicit `--worklog-dir` keeps highest precedence).
   > **Readback verification is an invariant:** runner reads back via `wl audit-show <id> --json` (audit exists, `rawOutput` non-empty, content references the ID) or exits non-zero.
3. **Verify persistence** — exit 0 does NOT guarantee storage: `wl audit-show <id> --json` must show `success=true`, audit not null, `rawOutput` non-empty with `Ready to close:` marker.
4. **On failure:** re-print, report the error, do NOT mark as recorded.
5. **Closing sentence** (issue-level): `Yes` → "Audit passed. The item is ready for release."; otherwise → "Work item is not ready to close (see above), would you like me to address the gaps in the audit?"

> **Critical:** `persist_audit.py` / `wl audit-set` may return success without storing — **always verify with `wl audit-show`**.
- Do NOT run arbitrary `wl`/`git` commands outside the authorized flow; use `--debug-log` for debugging.

## Examples

```bash
python3 $(skill_path audit)/scripts/audit_runner.py issue SA-123                  # audit + persist
python3 $(skill_path audit)/scripts/audit_runner.py issue SA-123 --do-not-persist  # dry run
python3 $(skill_path audit)/scripts/audit_runner.py issue SA-123 --force           # in-progress item (bypasses pre-flight guard + freshness)
```

## Script Execution Failure Notice

On runner failure (non-zero exit, timeout, exception), the report is wrapped with an `⚠ Script Execution Failure: <script_name> — <reason>` banner above and below (informational, no state changes; Python module `scripts.failure_notice` from the shared skills-root `scripts/` dir — note it lives one level above the audit scripts, unlike the `$(skill_path audit)/scripts/...` runner/persister; JSON key `script_failure`).

## Common failure modes

- **Silent persistence failure:** `persist_audit.py` / `wl audit-set` returns success without storing — **always verify with `wl audit-show --json`**.
- `wl` unavailable/invalid JSON → report the error, do not claim success.
- Agent-mode response parsing (Phase 2 JSON streams, `_extract_json_array`) is documented in [docs/dev/audit-skill-reference.md](../../docs/dev/audit-skill-reference.md).


## Final step: standardized end-of-session report

The audit's **persisted** machine-verified report (``Ready to close:`` header,
``## Summary``, ``## Acceptance Criteria Status``) is unchanged and remains
the authoritative audit record. Only the agent's end-of-session summary
reconciles with the canonical template — render it as the **last step**, then
close with: `<work-item-id>: <one-line summary>`.

```bash
python3 $(skill_path report)/scripts/render_report.py <work-item-id> \
  --skill-name audit \
  --headline "<1-3 sentence headline summary>" \
  --ac "<AC# description>|<verification metric>|met" \
  --ac "<...>|<...>|unmet" \
  [--producer-actions "<actions for the producer, or omit for 'None needed'>"] \
  [--notes "<freeform context/caveats/assumptions>"] \
  [--next-action <review|plan|implement|...>]
```

Do NOT re-summarize the report in a different format — the report is the summary.
