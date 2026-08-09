# Audit skill Phase 2 — Performance Evaluation Report

**Work item:** SA-0MSAHR63100415PM (Phase 2 performance evaluation report)
**Parent:** SA-0MSADWWH3003N82D (Audit skill Phase 2: performance evaluation and improvement)
**Date:** 2026-08-01
**Status:** Evaluation complete — baseline established, levers ranked

---

## 1. Summary

Phase 2 deep code analysis (`_run_phase2_deep_analysis` in
`skill/audit/scripts/audit_runner.py`) is the dominant cost of an audit run
and the source of recurring timeouts. It executes **N+1 sequential
agent-mode Pi calls** (one for the parent + one per active child), each a
fresh `pi -p --mode json --tools read,bash,grep,find,ls` session, with a
prompt that instructs the model to "read the actual implementation files"
but provides **no file scope** — no Key Files manifest, no git diff, no repo
index. The result is unbounded tool exploration (`find`/`grep`/`ls`/`read`
across the whole repo), which is the dominant per-call cost.

Prior timeout fixes (900s → 1200s → 1800s) masked the underlying
inefficiency instead of reducing actual work. Per-call timing
instrumentation is now in place (SA-0MSAHQZN4004ZFKQ) and establishes a
measurable baseline.

**Headline findings:**

1. **Call count is structural:** one parent + one call per active child,
   executed strictly sequentially in a single loop. Wall-clock scales
   linearly with child count with zero parallelism.
2. **Per-call duration is unbounded by design:** the Phase 2 prompt names no
   files, so a tools-enabled agent session may explore the entire repo.
   Real measured agent-mode calls (see §3) complete in seconds only when the
   task is tiny; a genuine "verify this AC against the implementation" task
   against a large repo is what historically blew the 900→1200→1800s budget.
3. **Retries multiply worst-case cost:** a provider error on a long call
   restarts the entire call (up to 2 extra attempts with backoff), so the
   worst case for a single call is ~3 × 1800s.
4. **Duplicated verification:** `cmd_issue` auto-triggers a full child audit
   subprocess (own Phase 1 + Phase 2) for each child missing a fresh audit,
   then the parent's Phase 2 re-runs deep analysis on those same children —
   the same code is verified twice.

---

## 2. How Phase 2 works today (call path)

```
cmd_issue (issue mode)
  ├─ Phase 1 screening (parent AC review — 1 bare call, no tools)
  ├─ child AC review (1 bare call per active child)
  ├─ auto-triggered child audit subprocesses (children with no/stale audit)
  │    └─ child gets its own Phase 1 + Phase 2 (recursive)
  └─ decision gate (blocked? → skip Phase 2; else run)
       └─ _run_phase2_deep_analysis
            ├─ call #0  context="phase2_deep"     (parent, agent mode)
            ├─ call #1  context="phase2_child:0"  (child 1, agent mode)
            ├─ call #2  context="phase2_child:1"  (child 2, agent mode)
            └─ ... (one per active, non-closed child)
```

Key facts:

- **Agent mode:** each Phase 2 call appends
  `--tools read,bash,grep,find,ls --exclude-tools ask_question` (introduced
  by SA-0MS3HPN4I004KVNK), which is inherently slower than bare LLM pipe
  mode and is what made Phase 2 the slow phase.
- **Per-call timeout:** `CALL_PI_TIMEOUT = 1800` (raised 900→1200→1800 in
  SA-0MS1SPGL3008G3BN, SA-0MS3FNIUA00385DE, SA-0MS0SQQTW006GNX9).
- **Retries:** provider errors (`stopReason: "error"`) retried up to
  `_PI_MAX_RETRIES = 2` extra times with linear backoff. A late provider
  error restarts the whole call — worst case ~3 × 1800s per call.
- **Prompt has no scope:** the Phase 2 prompt says "Read the actual
  implementation files mentioned in or implied by the criterion" without
  listing any files. The model must discover the repo layout first.
- **No parallelism:** the child loop in `_run_phase2_deep_analysis` is a
  plain `for` loop; calls are strictly sequential.
- **Cumulative elapsed guard:** the 110s guard in `cmd_issue` protects the
  parent bash-tool execution, but it only skips *child* audits near the
  limit; it does not bound Phase 2 deep-analysis calls (which have their own
  1800s per-call budget). The guard is being made configurable separately
  (SA-0MSABZO2T004B95X).

---

## 3. Measured baseline (AC2)

Instrumentation added in SA-0MSAHQZN4004ZFKQ emits per-call timings to
stderr and debug logs:

```text
Per-call timing: issue_id=<id> context=<context> elapsed_seconds=<seconds>
```

### 3.1 Real measurements (Local Proxy, `Local Proxy/plan` model)

| Measurement | Call type | Wall-clock |
|-------------|-----------|------------|
| Trivial bare-mode call (single JSON reply) | `parent` | ~21s |
| Small agent-mode call (list files in one dir, then reply) | agent mode | ~12s |

The agent-mode figure (~12s) is for a *tiny* task against a *small*
directory; it demonstrates tool-session startup/overhead is small. The
problem is the **unbounded case**: when the model searches the whole repo
for files relevant to a real AC, calls grow from seconds to minutes. The
historical 900→1200→1800s timeout bumps are direct evidence that
production calls reached those magnitudes.

### 3.2 Mocked run through the real call path (call-count baseline)

Running the instrumented `_run_phase2_deep_analysis` against a parent with
3 active children produces exactly 4 sequential calls:

```text
Per-call timing: issue_id=SA-PARENT-1 context=phase2_deep     elapsed_seconds=...
Per-call timing: issue_id=SA-CHILD-1  context=phase2_child:0  elapsed_seconds=...
Per-call timing: issue_id=SA-CHILD-2  context=phase2_child:1  elapsed_seconds=...
Per-call timing: issue_id=SA-CHILD-3  context=phase2_child:2  elapsed_seconds=...
phase2_completed: True
```

**Call-count model:** `C = 1 + N_active_children` sequential calls, each
bounded only by the 1800s per-call timeout (and, on provider error, by
3 × 1800s). Worst-case wall-clock = `C × 1800s`; even at a "healthy" 60s per
call, a parent with 5 children takes ~6 minutes of wall-clock on Phase 2
alone, before Phase 1 and child-audit subprocess time.

### 3.3 Where time is spent (dominant costs, in order)

1. **Unbounded repo exploration inside each agent-mode call** — the single
   largest lever. The prompt provides no file scope, so tool use is
   unconstrained (`find .`/`grep -r`/`ls -R`/`read` of arbitrary files).
   This is what grew per-call time into the thousands of seconds.
2. **N+1 sequential call structure** — wall-clock is strictly additive;
   there is no parallelism and no reuse.
3. **Full-call provider-error retries** — worst case triples any single
   call's duration, and a retry late in a long run is maximally wasteful.
4. **Duplicated child verification** — children auto-audited by `cmd_issue`
   (fresh child audit, own Phase 2) are deep-analysed again by the parent's
   Phase 2.

---

## 4. Prioritized improvement opportunities (AC3)

Ranked by (impact × probability of success) / (risk + effort). All preserve
Phase 2's mandatory-execution and final-verdict semantics.

### P1 — Bound exploration with a file-scope manifest *(high impact, low risk, medium effort)*

Inject a scoped file manifest into the Phase 2 prompt: work-item **Key
Files**, the **git diff / changed-file list**, and a lightweight **repo
index** (relevant paths). Instruct the model to read only in-scope files and
restrict `bash` to read-only commands (`ls`, `cat`, `grep` without
`-r`-beyond-scope; no `git` mutations — already excluded).

- **Expected impact:** high — directly attacks the #1 cost driver (per-call
  duration). A scoped prompt typically cuts agent tool-call counts from tens
  to a handful.
- **Risk:** low-medium. Verdict-quality risk if the manifest misses the
  relevant file; mitigated by including git diff + Phase 1 file:line
  evidence (P4) and instructing the model to say so when scope seems
  incomplete.
- **Effort:** medium — manifest builder (Key Files + git diff + index),
  prompt template change, tests.

### P2 — Reuse fresh child audit verdicts *(high impact, low risk, low effort)*

Skip parent-Phase-2 deep analysis for children whose own fresh child audit
(`cmd_issue` auto-trigger) already produced a ready verdict — their Phase 2
already verified the code. Currently those children are deep-analysed again
inside the parent call.

- **Expected impact:** high for parents with children — eliminates
  `N` agent-mode calls whenever children have fresh audits.
- **Risk:** low — the child's own Phase 2 is the same authority; the parent
  report can reference the child verdict. Must keep verdict semantics:
  children with `child_audit_ready=False` still require parent deep analysis.
- **Effort:** low — gate the child loop on `child_audit_ready` (already
  computed in `cmd_issue`), with tests for ready/not-ready/stale cases.
- **Note:** must not conflict with the elapsed-guard sibling item
  (SA-0MSABZO2T004B95X); it only changes which children are deep-analysed.

### P3 — Parallelize independent child deep-analysis calls *(medium-high impact, medium risk, low effort)*

Run the independent child calls in `_run_phase2_deep_analysis` concurrently
with bounded concurrency (`concurrent.futures.ThreadPoolExecutor`, cap via
env var, default 2–3). Parent call stays first (it defines the scope).

- **Expected impact:** medium-high — wall-clock for `N` children drops from
  `N × t` to `ceil(N/cap) × t`.
- **Risk:** medium — provider/rate-limit pressure from concurrent calls
  against the Local Proxy; mitigated by bounded cap and sequential fallback
  on failure.
- **Effort:** low-medium — executor plumbing, per-child isolation preserved,
  sequential fallback, tests.

### P4 — Feed Phase 1 evidence forward *(medium impact, low risk, low effort)*

Include Phase 1 file:line evidence in the Phase 2 prompt so the model
verifies *named* files rather than re-discovering them. Natural complement
to P1 (can be implemented together as part of the scope-manifest work).

- **Expected impact:** medium — removes the "find the file" step from
  Phase 2 exploration.
- **Risk:** low.
- **Effort:** low.

### P5 — Tune retries for long agent-mode calls *(medium impact, low risk, low effort)*

Cap or avoid full-call provider-error retries on long Phase 2 agent-mode
calls: retry once, or degrade to partial with a provider-error diagnostic
instead of restarting the entire call.

- **Expected impact:** medium — worst case drops from ~3 × 1800s to
  ~1 × 1800s per call, and avoids throwing away a nearly-complete run.
- **Risk:** low — verdicts already degrade to `partial` on provider error
  today; this only avoids the wasteful restart.
- **Effort:** low.

### P6 — Batch child deep analysis into the parent call *(medium impact, medium-high risk, medium effort)*

Fold active-child ACs into the single parent Phase 2 session (indexed AC
list covering parent + children), eliminating N+1 calls entirely.

- **Expected impact:** high in the limit (1 call instead of N+1), but
  bounded by a single call's practical size.
- **Risk:** medium-high — larger prompt/single-call duration, provider
  concurrency not applicable, single point of failure; mitigations: scope
  manifest (P1) keeps it bounded, fall back to per-child calls on batch
  failure/timeout.
- **Effort:** medium — prompt restructure, result routing back to per-child
  AC lists, fallback logic, tests.
- **Recommendation:** implement after P1–P5; P1+P2+P3 already collapse the
  call count and duration with much lower risk.

---

## 5. Recommendation

Implement in this order (each creates its own child work item per the
operator directive):

1. **P1 — file-scope manifest** (with P4 evidence-forward folded in) —
   attacks the dominant cost with lowest risk.
2. **P2 — reuse fresh child audit verdicts** — cheap, high impact for
   parents with children.
3. **P3 — bounded parallelism** for remaining child calls.
4. **P5 — retry tuning** for long agent-mode calls.
5. **P6 — batch (optional, later)** — only if P1–P5 leave unacceptable
   wall-clock; keep per-child fallback.

No per-call timeout increase beyond the current 1800s default is justified
by this evaluation; the levers above reduce actual work instead.

---

## 6. Method & limitations

- Baseline uses the instrumentation from SA-0MSAHQZN4004ZFKQ (per-call
  `elapsed_seconds`, emitted to stderr and `--debug-log`).
- Real numbers: two real Local Proxy calls (bare + small agent-mode) for
  startup/overhead reference; call-count structure verified with a mocked
  run through the actual instrumented call path.
- Production-scale per-call timings (full repo, real ACs) were not captured
  in this session because a full real Phase 2 run would itself take the
  thousands of seconds being evaluated; the timeout-bump history
  (900→1200→1800) is cited as direct evidence of production call durations.
- Assumption (to be confirmed by Phase B measurement): per-call duration is
  dominated by unbounded tool exploration rather than model inference.
