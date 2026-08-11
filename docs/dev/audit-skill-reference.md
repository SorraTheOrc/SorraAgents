# Audit skill — implementation reference

Deep implementation-reference detail relocated from `skill/audit/SKILL.md`
(relocation tracked by SA-0MSLK7SAE0032V9K). The SKILL.md is the agent-facing
operational brief; this document preserves the full implementation reference
for maintainers and for debugging runner behavior. Workflow semantics are
unchanged — every command/flag documented here is still valid.

## Monitored Run Execution

Audits invoked via the canonical runner (`python3 ./scripts/audit_runner.py
issue <id>` / `project <id>`) can legitimately run for **hours** — Phase 1 +
Phase 2 with children, a child audit cascade (`--audit-children`), or a large
project audit. This section defines the agent-side
**launch → monitor → abort** contract so that:

- a default bash-tool timeout can never kill a long run mid-audit,
- all output is captured to a log file the agent can inspect, and
- a run that goes astray is stopped cleanly with the work item left in a
  recoverable state.

This is **guidance for the agent invoking the runner** — it does not change
`audit_runner.py` / `persist_audit.py` behavior.

### Launch

1. **Capture the pre-audit state.** Before launching, record the work item's
   current `status`/`stage` so an abort can restore them exactly:

   ```bash
   wl show <id> --json   # note workItem.status and workItem.stage
   ```

2. **Launch detached with output captured.** Start the runner in the
   background, fully detached from the bash tool, with stdout+stderr
   redirected to a **unique** log file under `~/.audit_debug/<project>/`
   (the same directory family as the runner's own debug logs):

   ```bash
   mkdir -p ~/.audit_debug/<project>
   LOG=~/.audit_debug/<project>/audit_run_<work-item-id>_$(date +%Y%m%dT%H%M%S).log
   nohup python3 ./scripts/audit_runner.py issue <id> [flags] > "$LOG" 2>&1 &
   disown
   echo "PID $! LOG $LOG"   # record both for the monitor step
   ```

   - The log name is **always unique per run**
     (`audit_run_<work-item-id>_<timestamp>.log`) — never a fixed shared
     path (e.g. never `/tmp/audit.log`).
   - Record the PID and the absolute log path; both are needed by the
     monitor step.

3. **Enforce a 180-minute hard budget.** The whole run must finish within
   **10800s (180 min)** of launch. This is the agent-side outer budget —
   distinct from the runner's per-call `CALL_PI_TIMEOUT` (1800s default,
   `--timeout` / `AUDIT_PI_TIMEOUT`) and its child-count-scaled parent
   elapsed-time guard (`--parent-timeout` / `AUDIT_PARENT_TIMEOUT`). When the
   180-minute budget is exhausted the run MUST be aborted (see Abort &
   Mitigate below).

4. **Foreground fallback.** If a detached launch is not possible (e.g. the
   harness does not keep background processes between bash-tool calls) and a
   foreground call is used instead, the bash-tool call MUST be given a
   timeout `>= 10800s` (180 min). If neither a detached launch nor a
   `>= 10800s` tool timeout is available, **refuse to start the run**: an
   unmonitorable audit must not be launched.

### Monitor

While the run is active, **report progress every 3 minutes**:

1. **Alive check** — `kill -0 <pid>` (exit 0 = still running).
2. **Tail the log** — `tail -50 <log>` and look for the runner's stderr
   progress markers:
   - `Phase 1 passed: running Phase 2 deep code analysis...`
     (Phase 1 → Phase 2 transition),
   - `Per-call timing: issue_id=... context=... elapsed_seconds=...` lines
     (per-call instrumentation; a run stuck on one long call shows a
     growing elapsed_seconds with no new context).
3. **Confirm log growth** — the log file must grow between checks; a log
   that stopped growing is a stall signal (see Abort & Mitigate).
4. **Report to the operator** — on each check, report the elapsed time since
   launch and the current phase (e.g. "Phase 1", "Phase 2 deep analysis",
   "child audit cascade").

### Abort & Mitigate

**Going-astray triggers** — any one of the following requires stopping the
run:

- **(a) Stall** — no new log output for ≥10 minutes (the log size has not
  grown and no new phase/timing line appeared).
- **(b) Repeated failures** — ≥3 consecutive `Warning: Pi call failed` /
  provider-error retries with no progress (no new phase marker since the
  failures began).
- **(c) Other agent-determined signals** — unexpected process exit, error
  loops, or output that violates the audit contract (e.g. an attempt to
  execute state-modifying commands outside the authorized flow).
- **(d) Budget exhaustion** — 180 minutes elapsed since launch.

**Abort procedure** — on any trigger:

1. **Kill the process tree.** `pkill -TERM -P <pid>` (children first), then
   `kill <pid>`; escalate to `kill -KILL <pid>` if the process does not exit.
2. **Restore the pre-audit state** — ONLY when the runner did not already
   complete its verdict-driven lifecycle (no verdict line was printed and no
   terminal status transition was observed). Mirror the runner's own failure
   fallback semantics: restore the `status`/`stage` captured before launch
   and clear the assignee:

   ```bash
   wl update <id> --status <pre-audit-status> --stage <pre-audit-stage> --assignee "" --json
   ```

   A transient abort must **never demote** an `in_review` item to `open` —
   restore exactly what was captured (fall back to `open`/`plan_complete`
   only when the pre-audit state could not be determined).
3. **Append a failure notice** to the run log with a progress summary:
   elapsed time, the last phase marker seen, and the trigger that caused the
   abort.
4. **Report the outcome** to the operator (run id, log path, trigger,
   restored status/stage).

**Never** persist a fabricated audit report on abort, and **never** override
or contradict a runner verdict that already completed its lifecycle: if the
runner finished, its verdict and status transitions stand untouched.

## Freshness Gate

Short-circuits item-level audits when a recent, valid audit exists to avoid unnecessary model calls.

### Behavior

1. **Content-based gate (primary):** each audit captures a content fingerprint — git HEAD sha + work-item description hash + Key Files list — and embeds it in the persisted report (`Audit content fingerprint: <sha256hex>`). Re-auditing an item whose fingerprint is unchanged returns the existing report in seconds instead of re-running the pipeline (SA-0MSKB6US1009CNHT). A change in ANY fingerprint component (new commit, edited description/ACs, changed Key Files) invalidates freshness and re-runs the full audit.
2. **Time gate (floor):** audits persisted without a fingerprint (legacy reports) fall back to the 60s timestamp gate — compare ``auditedAt`` against ``updatedAt + 60s``.
3. If fresh: prints ``Skipping: audit still fresh`` + existing report, exits code 0 **without** status lifecycle.
4. If stale or error: falls through to normal full audit.
5. ``--force`` bypasses the gate. Applies only to item-level audits (``cmd_issue``).

Configuration: ``AUDIT_FRESHNESS_BUFFER_SECONDS = 60`` (in ``./scripts/audit_runner.py``).

```
Skipping: audit still fresh
<existing rawOutput>
```

No status lifecycle transitions occur, and no persistence is performed. An explicit ``Ready to close: No`` verdict in the stored report is returned verbatim — it is never masked by a freshness skip.

## Scripts

- **Runner:** `./scripts/audit_runner.py` — `python3 ./scripts/audit_runner.py issue|project <id> [--do-not-persist] [--timeout SECONDS] [--parent-timeout SECONDS] [--batch-phase2] [--max-concurrency N] [--green-run SHA|HEAD] [--run-tests] [--audit-children] [--max-child-audits N] [--pi-bin] [--model] [--model-source] [--debug-log] [--json] [--force] [--worklog-dir DIR]`
- **Persister:** `./scripts/persist_audit.py` — persist from stdin, file, or CLI string

**Cwd-independence (`--worklog-dir`):** every `wl` invocation made by the runner
(including the status lifecycle and `persist_audit`) targets the correct
worklog store regardless of the caller's working directory. Resolution order
for each `wl` command:

1. Explicit `--worklog-dir DIR` (highest precedence — overrides everything).
2. Prefix-to-project sibling scan: the work-item id prefix (e.g. `OSL-…`)
   is matched against sibling projects' `.worklog/config.yaml`
   (relative to the framework repo root's parent — the scan base is derived
   from this module's own location, never from the caller's cwd, so it works
   from the skill install directory or any other non-project cwd).
3. Cwd-chain fallback: detect the worklog root from the current directory,
   git root, or nearest ancestor.
4. No flag — `wl` resolves from cwd.

This means the runner can be launched from the skill install directory (or any
other cwd) and still audit items in a *different* project's worklog, e.g.:

```bash
python3 ./scripts/audit_runner.py issue OSL-0MSABC7SB001NVUN --do-not-persist
```

If auto-resolution cannot determine the target store, pass an explicit dir:

```bash
python3 ./scripts/audit_runner.py issue OSL-0MSABC7SB001NVUN \
    --worklog-dir /path/to/project/.worklog
```

Failure diagnostics surface the real `wl` error (stdout JSON error field first,
then stdout text, then stderr) instead of empty stderr.

**Timeout:** `CALL_PI_TIMEOUT`=1800s per Pi call (default). Override with `--timeout SECONDS` or the `AUDIT_PI_TIMEOUT` env var (e.g. `AUDIT_PI_TIMEOUT=3600`). Precedence: `--timeout` flag > `AUDIT_PI_TIMEOUT` env var > 1800s default. Cumulative elapsed-time guard skips remaining child audits to prevent silent kill; the default scales with the number of active children (`110s` base + `600s` per child — e.g. ~710s for a single child, ~6,110s for a 10-child parent), so multi-child audits with default settings attempt child auto-audits instead of silently degrading to parent-only. Override with an exact value via `--parent-timeout SECONDS` or the `AUDIT_PARENT_TIMEOUT` env var (e.g. `AUDIT_PARENT_TIMEOUT=3600`) to audit items with many children in one pass on harnesses whose bash tool allows longer runs. Precedence: `--parent-timeout` flag > `AUDIT_PARENT_TIMEOUT` env var > child-count-scaled default. When the guard does trip, the skip diagnostic names the computed budget and the `--parent-timeout` / `AUDIT_PARENT_TIMEOUT` override. On timeout, returns `unmet` with evidence "Pi model call timed out."

**Parent-first child pass-through (default):** item audits run a **full parent-only audit first** — Phase 1 screens parent ACs only (no child AC screening) and Phase 2 parent deep analysis completes before any child audit is considered (SA-0MSKB6VJA005N43F). The parent verdict then drives the child pass-through:

- **Parent passes with no gaps** (all ACs `met`/`adjusted`, no blocking CQ findings) → all children **inherit passed** by virtue of the parent — zero child audits. Children whose own content changed (content-fingerprint mismatch, Feature 1) are never silently inherited-passed: they are audited.
- **Parent has gaps** (`unmet`/`partial` ACs or blocking CQ findings) → only the child(ren) mapped to the gap files (via the Phase 1/2 file-scope manifest and child Key Files) receive full audits; unrelated children are not audited.
- Inherited/not-audited children are marked **explicitly** in the report (`Inherited from parent pass` / `Not audited (unrelated to parent gaps)`) — never silent.
- Verdict semantics are unchanged: a relevant not-ready child still blocks the parent (`Ready to close: No`).

`--audit-children` is the **explicit override** that forces the full per-child flow below regardless of the parent result.

**Child audit cascade (opt-in via `--audit-children`):** the recursive child-audit cascade — where a parent with unaudited children spawns a full child audit per child — is **OFF by default** (SA-0MSKB6V5Q007YDHE). A parent with children that lack fresh audits no longer implicitly spawns a cascade that can take hours. Enable it explicitly:

```bash
python3 ./scripts/audit_runner.py issue SA-123 --audit-children
```

- `--audit-children` forces the full per-child flow (override of the default parent-first pass-through): each child without a fresh audit is independently reviewed; children without fresh audits that stay not-ready block the parent, verdict semantics unchanged.
- `--max-child-audits N` (env `AUDIT_MAX_CHILD_AUDITS`) bounds the number of child audits a single run may auto-trigger (default: `5`).
- Children with unchanged content are skipped via the content-based freshness gate — their stored verdict is reused instead of re-auditing.

**Operator-attested green test run (`--green-run` / `AUDIT_GREEN_RUN`):** Some acceptance criteria are inherently execution-dependent — e.g. "Full project test suite passes with the new changes" — and the audit's read-only mandate forbids the runner (and its Phase 1/2 models) from executing the suite. Without external evidence such criteria can NEVER be verified inside the audit, so they always return `partial`. Operators should run the full suite via the [test skill](../test/SKILL.md) (`/skill:test` — run → triage → evaluate → loop until green) so the run is quiet-mode, triaged, and genuinely green. An operator who has verifiably run the full suite at the audited commit can then attest that fact and unblock those criteria:

```bash
python3 ./scripts/audit_runner.py issue SA-123 --green-run HEAD
# or with an exact sha
python3 ./scripts/audit_runner.py issue SA-123 --green-run <full-commit-sha>
# or via env var
AUDIT_GREEN_RUN=HEAD python3 ./scripts/audit_runner.py issue SA-123
```

Semantics:

- The value is an exact commit sha or the alias `HEAD` (resolved to the current HEAD sha). Precedence: `--green-run` flag > `AUDIT_GREEN_RUN` env var > unset.
- The value MUST match the audited HEAD (resolved via `git rev-parse HEAD`). A mismatch (or an unresolvable HEAD when git is unavailable) prints a clear error naming both shas and the run proceeds WITHOUT the attestation — execution-dependent ACs stay `partial`. The runner never silently accepts a mis-attested sha.
- When accepted, a **GREEN-RUN attestation block** is injected into the Phase 1 parent screening prompt and ALL Phase 2 prompts (`phase2_deep`, `phase2_child`, `phase2_batch`): the model MAY mark execution-dependent criteria (e.g. "full test suite passes") met based on the attestation, but MUST NOT execute the test suite or any other state-modifying command — the read-only mandate otherwise remains in force.
- The accepted sha is recorded in the persisted report as a `Green run attestation: <sha>` line near the `Ready to close:` header, so the report remains auditable.
- **The runner never executes the test suite.** The attestation is external, operator-provided evidence; a false attestation is an operator/process violation the runner cannot detect — the operator is responsible for attesting truthfully and only for commits whose full suite they have actually run green.
- Without an attestation, execution-dependent ACs remain `partial` and block closure exactly as before — this flag only adds an evidence path, it never relaxes the default read-only behavior.

**Automatic full-suite verification (read-only test cache):** The runner can also verify execution-dependent criteria automatically — without any operator attestation — by consuming a green full-suite run from the per-repo test cache (`../test_cache.py`, SA-0MSGNUWQ9002LSMS / SA-0MSGN5OJ4002OZKY). When `/skill:test` (or any `run_tests.py` invocation) ran the full suite green at the audited git state within the cache TTL (2h), the runner looks the cached result up **read-only** via `query_cached()` — which never executes anything — and, when EVERY suite command (pytest + all node suite dirs) has a cached entry with exit code 0, injects an **AUTO-VERIFIED GREEN RUN** block into the Phase 1 parent prompt and ALL Phase 2 prompts. The model MAY then mark execution-dependent criteria (e.g. "full test suite passes") met based on the verified cached result, and the sha is recorded in the persisted report as an `Automatic green run evidence: <sha> (cached full-suite run)` line near the `Ready to close:` header.

Semantics:

- **No flag needed** — the automatic path is always attempted when no `--green-run`/`AUDIT_GREEN_RUN` attestation is present, and a valid operator attestation takes precedence (the automatic path augments rather than replaces SA-0MSGLAVCZ002LVZ4).
- **Fail-closed:** a cache miss, a non-zero (or timed-out) cached run, a partially cached suite set (e.g. pytest but not node), an unresolvable HEAD, or any cache/infra error yields NO evidence — execution-dependent ACs stay `partial` and the audit completes normally (never crashes, never fabricates a green verdict).
- **Read-only by construction:** `query_cached()` executes nothing, so the audit's read-only mandate is preserved unconditionally — the suite is never executed inside the audit. The cached run must match the audited git state exactly (HEAD sha + working-tree fingerprint) and be within the 2h TTL, so the evidence is a genuine full-suite result at the audited commit.
- **Workflow:** run the full suite once via the [test skill](../test/SKILL.md) (`/skill:test` — run → triage → evaluate → loop until green; this populates the cache), then audits at the same git state within the TTL automatically verify execution-dependent ACs. This removes the manual attestation step for automated/read-only pipelines (e.g. the herdr downtime worker's auto-audit dispatches).

**Auto-invoked test skill (`--run-tests`, SA-0MSJELSWS002UF60):** For environments that authorize test execution during audits (e.g. the herdr downtime worker's auto-audit dispatches, where the manual `/skill:test` round-trip stalls the pipeline), the `--run-tests` flag removes the operator round-trip entirely:

```bash
python3 ./scripts/audit_runner.py issue SA-123 --run-tests
```

- When no `--green-run`/`AUDIT_GREEN_RUN` attestation is present AND the read-only cache holds no green full-suite run at the audited git state, the runner **invokes the test skill** (the `run_tests.py` machinery) to execute the full project test suite in quiet mode, triage any failures per the test skill, and refresh the per-repo cache. When the executed run is green, a **TEST-SKILL GREEN RUN** block is injected into the Phase 1 parent prompt and ALL Phase 2 prompts, the sha is recorded in the persisted report as `Test skill run evidence: <sha> (executed full-suite run, --run-tests)`, and execution-dependent ACs MAY be marked met — no operator round-trip needed.
- **OFF by default (AC2):** without the flag the audit stays strictly read-only and never executes the suite — environments that forbid test execution during audits are unaffected (execution-dependent ACs stay `partial` with the operator instruction). The flag is an explicit, operator-authorized deviation from the read-only mandate, consistent with the implement skill's "run the full suite via the test skill before `in_review`" discipline.
- **Clear log lines (AC3):** the runner prints when the test skill is invoked (`Invoking test skill (run_tests.py) — --run-tests enabled: executing the full project test suite at <cwd> in quiet mode...`) and what it returned (`Test skill run completed: success=... commands=... failures=... triaged=... notice=...`).
- **Failures are triaged, never silently ignored (AC4):** each structured failure record from the executed run is passed to the triage helper (`check_or_create.py`), which links/creates a critical `test-failure` work item for the failing test — exactly the test skill's run → triage discipline. The audit then completes fail-closed with no green evidence.
- **Fail-closed:** a non-green executed run (failures, non-zero exit, timeout, missing binary) yields NO evidence — execution-dependent ACs stay `partial` and the audit completes normally (never crashes, never fabricates a green verdict).
- A green cache hit at the audited state short-circuits the invocation entirely (the suite is only executed when the cache cannot satisfy the evidence).

**Concurrency:** `--max-concurrency N` bounds the number of concurrent pi/audit subprocesses host-wide (default: `AUDIT_MAX_CONCURRENCY` env var or 2). Each pi launch holds one audit slot; when the ceiling is saturated the call **fails fast by default** (no wait) and returns `unmet` with evidence "Audit concurrency limit reached" immediately, so the audit completes gracefully and the operator can retry later. To opt into a bounded wait instead, set the `AUDIT_LOCK_TIMEOUT` env var (seconds), e.g. `AUDIT_LOCK_TIMEOUT=30` waits up to 30s for a free slot before returning the `unmet` verdict. Precedence: `--max-concurrency` flag > `AUDIT_MAX_CONCURRENCY` env var > 2 default.

**Provider-error retry:** Pi calls that end in a provider error (`stopReason: "error"` / `errorMessage` on the last assistant message of `agent_end`, e.g. Local Proxy `finish_reason: error`) are retried automatically up to `_PI_MAX_RETRIES` (2) times with linear backoff (`_PI_RETRY_BACKOFF_SECONDS`). Timeouts and unparseable-but-otherwise-healthy responses are NOT retried. If a provider error persists after retries, ACs fall back to `partial` with evidence like "Pi provider error: <errorMessage> — criterion could not be evaluated." rather than the misleading "Pi model output could not be parsed" message, so operators can distinguish a transient model outage from a genuine parse failure.

**Context reduction:** every Pi call (`_call_pi`) runs with `--no-context-files --no-skills`, in both tool-enabled and tool-less modes. Audit prompts are fully self-contained — they carry the read-only mandate, JSON output format, FILE SCOPE manifest, SCANNING block, and criteria — so the global+project AGENTS.md load (~40KB, byte-identical duplication) and the skills section (~7KB) are dropped from each session's static context, cutting per-call startup from ~49KB (~12.3K tokens) to ~2KB. Prompts must never depend on AGENTS.md or skill descriptions: that is an invariant of this skill (context-reduction item SA-0MSISKM8F004NW1U).

**Per-call timing instrumentation:** Every Pi call (`_call_pi`) records its wall-clock duration via `time.monotonic` and attaches `elapsed_seconds` to the returned result dict (all return paths, including timeouts and provider errors). `_call_pi_and_maybe_log` emits a per-call timing line to stderr in the form:

```text
Per-call timing: issue_id=<id> context=<context> elapsed_seconds=<seconds>
```

where `<context>` is the call type (e.g. `parent`, `phase2_deep`, `phase2_child:<i>`, `child:<id>`, `project`). This establishes a performance baseline for Phase 2 deep analysis (N+1 sequential agent-mode calls: one parent + one per active child) and makes regressions visible. The same `elapsed_seconds` value is written into `--debug-log` JSONL entries alongside `issue_id`, `context`, and `provider_error`.

**File-scope manifest (Phase 2):** Each Phase 2 prompt (parent `phase2_deep` and every child `phase2_child:<i>`) now includes a **FILE SCOPE** section built from the work item's **Key Files** section, the **git changed-file list** (`git diff --name-only HEAD` + `git status --porcelain`), **Phase 1 evidence file:line references** (so the model verifies named files rather than re-discovering them), and a lightweight **repository index** (top-level layout with file counts). The prompt instructs the model to read ONLY in-scope files and to avoid unbounded `find`/`grep -r`/`ls -R` exploration. This bounds the dominant Phase 2 cost (unbounded repo exploration) without changing verdict semantics. If git is unavailable, the manifest degrades gracefully to the Key Files/evidence/index entries that can still be determined. See `docs/dev/audit-phase2-performance-evaluation.md` (SA-0MSAHR63100415PM) for the underlying evaluation.

**Child verdict reuse (Phase 2):** When a child's own fresh audit already produced a ready verdict (`child_audit_ready=True`, from `cmd_issue`'s auto-triggered child audit), the parent Phase 2 **skips** the duplicated child deep-analysis call (`phase2_child:<i>`) and reuses the child's existing `ac_results`. The same skip applies to children whose own fresh audit returned an explicit **'not ready to close'** verdict (`child_audit_not_ready=True`, P12): their own pipeline already ran deep analysis on the same ACs, so the parent Phase 2 reuses the child's own persisted audit findings (parsed from the child's audit report AC table, falling back to the Phase 1 screening results when the table cannot be parsed). Children with no fresh audit verdict (stale / no audit) still get parent deep analysis. This eliminates duplicated child verification work without changing verdict semantics — a 'not ready' child still blocks the parent's Ready-to-close evaluation.

**Parallel child deep analysis (Phase 2):** Independent child deep-analysis calls (`phase2_child:<i>`) run concurrently with bounded concurrency (default 2; configurable via the `AUDIT_PHASE2_PARALLELISM` env var, set to `1` for strictly-sequential historical behavior). The parent deep-analysis call always runs first and is never parallelized. Child workers are exception-isolated: a failure or timeout in one child degrades that child to `partial` (or falls back to its existing ACs) without affecting the others; on persistent executor failure the runner falls back to sequential execution. This collapses Phase 2 wall-clock from N sequential calls to ~N/cap while preserving per-child verdict isolation.

**Retry tuning (Phase 2):** Long agent-mode Phase 2 calls (`phase2_deep` / `phase2_child`) retry provider errors at most once (`_PHASE2_MAX_RETRIES = 1`), instead of the `_PI_MAX_RETRIES`=2 budget used by short Phase 1 bare calls. A provider error late in a long agent-mode call no longer restarts it multiple times (worst case was ~3 x 1800s before this change); the call degrades to `partial` with the existing provider-error diagnostic after the bounded retry.

**Batch deep analysis (Phase 2, P6):** When enabled via the `--batch-phase2` flag or the `AUDIT_PHASE2_BATCH` env var (truthy `1`/`true`/`yes`/`on`; CLI flag wins, default OFF), Phase 2 folds the parent's acceptance criteria and each pending child's criteria into ONE indexed `phase2_batch` pi call, then routes the indexed verdicts back to the parent and per-child AC lists. This replaces the N+1 `phase2_deep` + `phase2_child:<i>` sequence with a single call for audit runs that have pending children. The batch call uses the same file-scope manifest, read-only tools, and verdict semantics as the per-call path. If the batch call fails (provider error, timeout, or unparseable output), the runner **falls back** to the existing per-call deep-analysis path, so batching can never make an audit worse than the default behavior. Batching only applies when there are pending (not-ready) children; children with fresh ready audits are reused as in the standard path.

**Tools-enabled invocation (Phase 1 and Phase 2):** Phase 1 (parent AC screening + child AC screening) and Phase 2 deep analysis call Pi with
`enable_tools=True`, which appends
`--tools read,bash,grep,find,ls --exclude-tools ask_question` to the pi command.
This gives the model file-reading capabilities to verify ACs against
implementation code. Only the project-level audit summary call remains in bare
LLM pipe mode (`enable_tools=False`).

**Bounded scanning (Phase 2):** The Phase 2 prompts include a **SCANNING** block
that directs agents to the bounded helper `./scripts/scan.py` instead
of improvised recursive greps:

- Worklog lookups: `python3 ./scripts/scan.py find-workitem <id>`
  (delegates to `wl search`; never greps the `.worklog/` tree).
- Code search: `python3 ./scripts/scan.py search-code <pattern> --path <dir> --type py`
  (bounded rg with prunes for node_modules/.git/.worklog/.audit_debug and
  `audit_debug_*.jsonl`, max file size, explicit path).
- File listing: `python3 ./scripts/scan.py list-files --path <dir> --type py`
  (maxdepth 2, same prunes).

Unbounded recursive greps over the repo root or `.worklog/` (e.g.
`grep -r ... .` or `grep -r ... .worklog/`) are forbidden. Worklog lookups use
`wl search <keywords> --json` or `wl list <term> --json` for substring
matching, `scan.py find-workitem <id>` for exact match.
See `docs/dev/audit-grep-scan-patterns.md` (SA-0MSBR06GX0051T1Q) for the
pattern catalogue and benchmark.

**Debug logs are transient (Phase 2):** Debug files (`audit_debug_*.jsonl`)
are written only on parse_failure/provider_error or explicit `--debug-log`, live
under `~/.audit_debug/<project>/` (outside `.worklog/` and the repo tree, so
scans never walk them), and are swept by
`./scripts/cleanup_debug_logs.py` (dry-run default, `--apply`,
`--older-than N` days, default 14). Successful audit runs delete their own
debug file; failed runs keep full-content forensics. Never read them back
programmatically — use `scan.py find-workitem` / `wl search` instead.

**Phase 1 performance treatment (P7):** Phase 1 (automated screening) now
mirrors the Phase 2 performance pattern, which removed the dominant Phase 1
wall-clock cost (unbounded repository exploration during AC screening):

- **File-scope manifest + SCANNING block (Phase 1 prompts):** Both the parent
  AC screening prompt (`context=parent`) and every Phase 1 child AC screening
  prompt (`context=child:<id>`) now include a **FILE SCOPE** manifest (Key
  Files + git changed-file list + repository index) and the same **SCANNING**
  block used by Phase 2, so the model verifies in-scope files with the bounded
  `scan.py` helpers instead of unbounded `find`/`grep -r`/`ls -R` exploration.
  Phase 1 prompts keep the same verdict guidance (met/unmet/partial/adjusted
  with the same normalization) — only the reading strategy changed.
- **Child verdict reuse (Phase 1):** The child persisted-audit verdict
  (`child_audit_ready`) is now computed **before** the Phase 1 child AC review
  loop. A child whose own fresh audit already produced a ready verdict
  (`child_audit_ready=True`) **skips the Phase 1 child AC screening call** and
  reuses the AC verdicts persisted in its own audit report (parsed from the
  report's AC table; if the table cannot be parsed, each extracted AC falls
  back to `met` with a reuse note, since a fresh ready audit deems all ACs
  acceptable). Completed/done children remain exempt (AC5). The auto-trigger
  loop reuses these pre-computed verdicts instead of re-querying
  `wl audit-show` per child.
- **Parallel Phase 1 child screening:** Pending (no-audit / not-ready)
  children are reviewed concurrently with the same bounded concurrency used by
  Phase 2 (`_resolve_phase2_parallelism()` — default 2, configurable via the
  `AUDIT_PHASE2_PARALLELISM` env var; set to `1` for strictly-sequential
  behavior). A single pending child or parallelism=1 falls back to the
  sequential path. Workers are exception-isolated exactly like Phase 2: a Pi
  failure degrades that child to diagnostic `partial` verdicts without
  affecting the others, and the elapsed-time guard still skips remaining
  children near the parent timeout.

See `docs/dev/audit-phase1-performance-evaluation.md`
(SA-0MSF3RXU8005CFGD) for the measured Phase 1 wall-clock reduction.

### Code Quality Integration

Runner performs code quality checks before AC verification (invokes `../code_review/scripts/code_quality.py`):

1. Language detection → linter probing (ruff, eslint, markdownlint, shellcheck) → findings classified by severity
2. **Scoped to the git changed-file list** (SA-0MSKB6VWU000RT58): the scan lints only the changed files (the same file-scope manifest used for Phase 1/2) instead of the whole repo, bounding the dominant full-repo lint cost.
3. **Read-only (never auto-fixes)**: audits call with `fix=False` — linters may not mutate files during an audit. Findings that were previously auto-fixed now surface as findings (severity classification unchanged).
4. Critical/high findings → "Ready to close: No"; medium/low are warnings
5. Quality epics ("Quality Improvement - Refactoring") created/reused for findings
6. If `code_quality` module unavailable, continues with warning

- Persist from stdin: `cat report.md | python3 ./scripts/persist_audit.py --issue-id SA-123`
- Persist from a file: `python3 ./scripts/persist_audit.py --issue-id SA-123 --file report.md`
- Persist from a CLI string: `python3 ./scripts/persist_audit.py --issue-id SA-123 --report "Ready to close: Yes\n..."`

**Unique report file naming convention:**

- When persisting an audit report from a *file* (or writing a report to disk for later
  persistence), the file MUST use a unique name per work item:
  `audit_report_<work-item-id>.md` (e.g. `audit_report_OSL-0MSABC7SB001NVUN.md`).
- Never use a fixed/shared path such as `/tmp/audit_report.md` — a stale report left over
  from a previous audit of a *different* work item can be read back and persisted to the
  wrong item (cross-work-item contamination).
- This convention applies to any file-based persistence flow: the runner, manual agent
  workflows, and `persist_audit.py --file`.

**Identity guard (mandatory):**

- `persist_audit.py` rejects a report that clearly references a *different* work item than
  the target `--issue-id` (it names one or more work-item IDs but not the target). The
  rejection prints a clear error to stderr and exits non-zero — the report is NOT persisted.
- A report that mentions the target ID is always accepted, even when it also references
  other IDs (e.g. parent reports that include child-audit sections).
- A report that mentions *no* work-item ID is accepted with a warning (conservative: absence
  of any ID does not "clearly reference a different work item", so it must not block
  persistence).

Notes:

- **Persistence + readback verification is an invariant of the runner.** Unless `--do-not-persist` is given, the runner ALWAYS persists the audit and then performs a readback verification via `wl audit-show --json` to confirm the stored audit is retrievable. If either step fails, the runner exits non-zero. Use `--do-not-persist` for dry runs. The `--require-persist` flag has been removed — persist+verify is now unconditional.
- **Resilient audit persistence (P8):** the final `wl update <id> --audit-text <report>` step is the last write of the run. If it rejects the assembled verdict content (malformed JSON / validation error), `persist_audit()` never leaves the audit text field as the 43-char stub (`Audit result persisted via persist_audit.py`). It (1) runs a repair pass that salvages broken JSON fragments (valid JSON prefixes extracted; per-AC rows preserved; zero model calls) and retries once; (2) if the retry fails, persists a compact markdown fallback notice (with a clear failure notice naming the work item, so the identity/readback guards pass) and returns `PERSIST_CONTENT_INVALID` (4); (3) the runner then performs a *bounded* re-ask — at most **one** additional model call to re-emit the verdict array in valid JSON — reassembles the report and retries persistence. The repair never re-runs the full audit pipeline. If every attempt fails, `persist_audit()` returns non-zero and the run reports failure.
- **Priority normalization on 'Ready to close: Yes':** when the persisted report says `Ready to close: Yes` and the work item currently carries `critical` priority, `persist_audit.py` lowers it to `high` (via `wl update <id> --priority high`) before calling `wl audit-set`, so resolved items leave the critical queue. This is best-effort: a failed priority fetch/update logs a warning and never blocks persistence. It applies to both parent and child audit persistence (single `persist_audit()` entry point).
- The persister (and the runner when persisting) call: `wl audit-set <issue-id> --ready-to-close <yes|no> --summary <text> --raw-output "<report>" --json` and return a non-zero exit code on failure. After a successful return code, the runner calls `wl audit-show <issue-id> --json` and exits non-zero if the stored audit is null, has empty `rawOutput`/`summary`, **or the stored content does not reference the target work-item ID** (content identity check — catches a stale report persisted to the wrong item).
- **Child item audit persistence:** When auditing a parent work item with children, the runner also persists an individual audit report to each child work item. Each child receives a focused report covering only its own acceptance criteria. Child persistence is controlled by the same `--do-not-persist` flag — if persistence is disabled for the parent, child persistence is also skipped. Child persist failures are logged as warnings to stderr but do not prevent the parent audit from succeeding. Child readback verification is out of scope for the current release.

### Agent-mode response parsing

- Phase 2 deep analysis now runs Pi in agent mode (with `--tools`). The agent-mode
  JSON-stream output may contain additional event types (`agent_start`, `turn_start`,
  `message_start`, `tool_execution_start`, `tool_execution_end`, `agent_end`) not
  present in bare LLM pipe mode. The `_extract_pi_text()` and `_parse_pi_json_line()`
  functions handle these transparently by extracting text content from
  `message_update` events (same as bare LLM mode).
- If response parsing fails, check debug logs (use `--debug-log`) to see the raw
  agent output. The runner automatically falls back to Phase 1 results on Phase 2
  failure.
- The `_extract_json_array()` function strips prose text before/after JSON arrays,
  so it works correctly regardless of whether the model wraps its output in
  explanatory text (common in agent mode).
