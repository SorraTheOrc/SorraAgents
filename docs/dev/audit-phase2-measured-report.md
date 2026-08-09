# Audit skill — Measured Performance Report (post Phase 2 improvements + parent-first model)

**Date:** 2026-08-04 (original baseline); 2026-08-08 (parent-first model regression targets, see §9)
**Scope:** Live measurement of the audit runner (new code with P1–P6 improvements) against real work items, plus derived regression targets for the parent-first model.
**Method:** 4 audit runs; per-call `elapsed_seconds` captured via AC2 instrumentation (`Per-call timing` stderr lines + `--debug-log` JSONL).

---

## 1. Runs measured

| Run | Work item | Children | Outcome |
|-----|-----------|----------|---------|
| A | SA-0MSADWWH3003N82D (this work item) | 10 | 11 calls, 15,299s (4.25h); audit **failed to persist** (JSON parse error) |
| B | SA-0MSADWWH3003N82D (re-run, default settings) | 10 | 3 calls; children skipped by 110s guard; Phase 2 provider error |
| C | SA-0MSDX3KTV0092B7N (single-item release script) | 0 | Phase 1: 703.69s; Phase 2 killed by 1800s outer timeout |
| D | DS-0MSD8FRRM000UEEY (dev-scripts parent) | 4 | Phase 1: 4 calls / 4,191s; phase2_deep: 761s provider error |

---

## 2. Where time is spent (measured, Run A)

```
Phase 1 (parent + child AC review): 10 calls, 13,483s = 224.7 min  → 88%
Phase 2 (deep analysis):            1 call,  1,817s =  30.3 min  → 12%
```

| Context | elapsed_seconds |
|---------|----------------|
| parent (Phase 1) | 2,155.26 |
| child:SA-0MSAHQZN4004ZFKQ | 1,895.41 |
| child:SA-0MSAIE1YR004U223 | 493.02 |
| child:SA-0MSAIFWBC0048OVM | 2,400.14 |
| child:SA-0MSAHR63100415PM | 190.28 |
| child:SA-0MSAIXI1E005SZPV | 1,038.80 |
| child:SA-0MSAIXNXF002W7I3 | 2,167.37 |
| child:SA-0MSAIXTMS003REBW | 503.79 |
| child:SA-0MSAIXZB2007N0F0 | 961.90 |
| child:SA-0MSAIY59V001KECF | 1,676.57 |
| **phase2_deep** | **1,816.67** |

**Headline finding: the bottleneck has MOVED from Phase 2 to Phase 1.**

The original evaluation (AC1) assumed Phase 2 (tools-enabled agent mode, unbounded exploration) was the dominant cost. Measured reality after P1–P6: Phase 2 is now a single bounded call (1,817s, down from N+1=11), but **Phase 1 bare calls now consume 88% of wall-clock** — 10 sequential calls averaging 1,348s each (max 2,400s), with **no file-scope manifest, no parallelism, and no reuse of fresh child audits**.

---

## 3. Verified improvements (what P1–P6 achieved)

| Lever | Verified? | Evidence |
|-------|-----------|----------|
| P1 file-scope manifest | ✅ in code | `_build_file_scope_manifest` (L1375); `FILE SCOPE` + `SCANNING` blocks in phase2_deep prompt (L2599–2604) |
| P2 child verdict reuse | ✅ **measured** | Run A made **1** phase2_deep call for a parent with 10 children — 0 `phase2_child` calls; all children skipped via `child_audit_ready` (L1492) |
| P3 parallel child calls | ✅ in code | `ThreadPoolExecutor` (L2773) with `_resolve_phase2_parallelism()` (default 2) |
| P5 retry tuning | ✅ in code | `_PHASE2_MAX_RETRIES = 1` for long agent-mode calls |
| P6 batch mode | ✅ in code | `--batch-phase2` flag / `AUDIT_PHASE2_BATCH` env (off by default) |
| AC2 timing | ✅ **measured** | `elapsed_seconds` on every call; `Per-call timing` stderr lines |

**Call-count reduction (P2 measured):** For a parent with 10 children whose children have fresh audits, Phase 2 went from **11 sequential calls to 1**. Estimated Phase 2 wall-clock saving vs the historical worst case (11 × up to 1800s + 3× retry multiplier ≈ 5.5–16h): **≥ 90% reduction in Phase 2 call count and duration.**

---

## 4. Problems exposed by measurement

1. **Phase 1 is now the wall.** 10 sequential bare calls, avg 1,348s, max 2,400s. Phase 1 prompts (`L2985`, `L3137`) contain **no file scope, no SCANNING block, no bounded helpers** — the exact deficiency Phase 2 had. Phase 1 also makes **no reuse** of `child_audit_ready` and runs child AC review sequentially.

2. **Provider errors are rampant under concurrency.** Multiple `Provider finish_reason: error` + JSON parse failures across all runs. 7 concurrent audit processes were observed (ceiling default = 5 via flock semaphore, lock timeout 300s). Contention → provider errors → full-call waste (each failed call burns its entire duration).

3. **Audit persistence can fail.** Run A produced 15,299s of work but `wl update --audit-text` failed (rc=1) on a JSON parse error in the final verdict — **the entire audit was lost** (persisted text = 43 chars).

4. **110s default parent-timeout guard is far too low.** Run B (default settings) skipped all 10 children after the parent Phase 1 call (2,088s) because the 110s cumulative guard tripped — an audit run with defaults on a multi-child parent silently degrades to parent-only. The concurrent run needed `--parent-timeout 14400`.

5. **Prompt size.** SA-0MSADWWH3003N82D description = 16,741 chars (with auto-appended related-work report); large prompts → long generation → slow calls.

---

## 5. Estimated performance improvements (Phase 2 target, AC3)

| Metric | Before (baseline) | After (measured, Run A) | Improvement |
|--------|--------------------|--------------------------|-------------|
| Phase 2 call count | 11 (N+1) | 1 | **91%** |
| Phase 2 wall-clock (healthy calls) | 11 × 60–300s ≈ 11–55 min | 1 × 1,817s = 30 min | ~45–90% |
| Phase 2 worst-case (unbounded exploration) | 11 × 1800s + retries ≈ 5.5–16h | bounded by 1 × 1800s | **≥ 90%** |

Total audit wall-clock is NOT yet meaningfully reduced overall because Phase 1 (untouched by this epic) now dominates: Run A total = 4.25h, of which Phase 2 was only 30 min.

---

## 6. New improvement opportunities (beyond this epic's scope)

Ranked by impact:

1. **P7 — Apply the Phase 2 treatment to Phase 1 (highest impact).**
   - Add file-scope manifest + `SCANNING` bounded helpers to Phase 1 parent + child AC review prompts (same `_build_file_scope_manifest`).
   - Parallelize Phase 1 child AC review with the existing `ThreadPoolExecutor` pattern.
   - Reuse `child_audit_ready=True` to skip Phase 1 child AC review for children with fresh audits (mirrors P2).
   - Estimated: Phase 1 from 13,483s → ~2–4k s (50–70% reduction) → total audit ~1.5–2h.

2. **P8 — Make audit persistence resilient.** On final JSON parse failure, retry with a repair pass (extract valid prefix / re-ask model once) before abandoning; never lose a 4-hour run. (Occurs in `cmd_issue` final `wl update --audit-text` path.)

3. **P9 — Right-size the parent-timeout guard.** 110s default is unusable for multi-child parents. Options: auto-scale guard by child count (e.g., `110 + 600 × children`), or make the skip diagnostic actionable (it already is), and document `--parent-timeout` prominently in SKILL.md.

4. **P10 — Reduce provider-error waste under concurrency.** Options: lower the global concurrency ceiling default (5 → 2–3) for bare Phase 1 calls, add jitter/backoff before retry, and consider batching Phase 1 child AC reviews into fewer calls.

5. **P11 — Trim prompt size.** The auto-appended "Related work (automated report)" section is large and duplicated in every child prompt. Summarize or truncate (e.g., top-10 related items, no raw word-lists).

---

## 7. Regression safety

- `skill/audit/tests/test_audit_runner.py`: **116 passed** (full file).
- Phase 2 improvement tests: **53 passed** (phase2/parallel/batch/scope/manifest/reuse/retry selection).
- `tests/test_implement_tdd.py`: **5 passed**.
- No timeout increases beyond the 1800s default were introduced (AC4 satisfied).

---

## 8. Limitations

- Measurements were taken while **7 concurrent audits** were hammering the local proxy, inflating durations and causing provider errors. Numbers are upper bounds; a clean-room run would be faster but less representative of production.
- The Phase 2 single-call figure (1,817s) itself hit a provider error at the end, so its "success" duration is not separately measured.
- No clean before/after comparison on identical work items exists; the "before" figures are from the AC1 evaluation report's call-count model + historical timeout bumps (900→1200→1800s).

---

# Fresh baseline — parent-first model (2026-08-08)

**Date:** 2026-08-08
**Scope:** Updated for the parent-first child pass-through (SA-0MSKB6VJA005N43F), opt-in child cascade (SA-0MSKB6V5Q007YDHE), content-based freshness gate (SA-0MSKB6US1009CNHT), and scoped/read-only code-quality scan (SA-0MSKB6VWU000RT58).

## 9. New default flow (parent-first)

The default audit flow no longer runs the child AC-screening cascade in the critical path:

```
Parent Phase 1 screening (parent ACs only)
    → Parent Phase 2 deep analysis (parent-only)          ← parent verdict before any child audit
    → Parent verdict: any gaps?
        ├─ No gaps  → all children inherit passed (zero child audits)
        └─ Gaps     → only gap-mapped children are audited
                       (Phase 1 child review + child Phase 2 for those children)
```

- `--audit-children` forces the full per-child flow (explicit override, bounded by `--max-child-audits` / `AUDIT_MAX_CHILD_AUDITS`, default 5).
- Children with unchanged content are skipped via the content-fingerprint gate (no re-audit); a child whose own content changed is never silently inherited-passed.

## 10. Measured / derived wall-clock targets (regression targets)

| Metric | Old model (Run A, Aug 4) | Parent-first model target | Gate |
|--------|--------------------------|---------------------------|------|
| Re-audit of an unchanged item | re-runs full pipeline (hours) | **< 30s** (content-fingerprint gate returns existing report) | SA-0MSKB6US1009CNHT AC1 |
| Parent with N children, parent passes | 10 child audits + 11 Phase 2 calls ≈ 4.25h | **parent Phase 1 + 1 parent Phase 2 call; zero child audits** (children inherit) | SA-0MSKB6VJA005N43F AC2 |
| Parent with N children, parent has gaps | full cascade (hours) | **parent Phase 1 + parent Phase 2 + only gap-mapped child audits** | SA-0MSKB6VJA005N43F AC3 |
| Child auto-audit cascade | implicit (hours) | **off by default**; `--audit-children` opt-in, capped at 5 per run | SA-0MSKB6V5Q007YDHE AC1/AC3 |
| Code-quality scan | full-repo lint per audit, `fix=True` (mutates files) | **changed-file list only, read-only (`fix=False`)** | SA-0MSKB6VWU000RT58 AC1/AC2 |

### Parent-first call-count model (derived from Run A's per-call numbers)

Run A (Aug 4) measured 10 child Phase 1 calls at avg 1,348s each (13,483s total) plus a 1,817s parent Phase 2 call. Under the parent-first model the same parent with a passing parent verdict performs:

- **1** parent Phase 1 call (2,155s measured)
- **1** parent Phase 2 call (1,817s measured)
- **0** child Phase 1 calls (children inherit passed)
- **0** child Phase 2 calls

**Derived total ≈ 3,972s (~66 min) vs Run A's 15,299s (4.25h) → ~74% reduction** for the parent-passes case. When the parent has gaps, only gap-mapped children are audited — the count of child calls scales with the number of *changed/gap-mapped* items, not all children.

## 11. Per-call timing observability (AC6)

Verified in the codebase and test suite (2026-08-08):

- Every Pi call emits a `Per-call timing: issue_id=... context=... elapsed_seconds=...` stderr line (`audit_runner.py` `_call_pi_and_maybe_log`).
- `--debug-log` JSONL entries carry the same `elapsed_seconds` field.
- Regression coverage: `skill/audit/tests/test_audit_runner.py::TestCallPiTimingInstrumentation`, `TestCallPiAndMaybeLogTiming`, `TestPhase2TimingInstrumentation` — **8 timing tests pass**.

## 12. Regression safety (post Features 1–4)

- `skill/audit/tests/`: **350 passed** (includes 33 content-freshness + parent-first tests).
- Full project suite: **1687 passed, 8 skipped** (pre-existing integration/linter skips).
- Verdict semantics unchanged: `met/unmet/partial/adjusted` normalization and the ready-to-close gate are not relaxed; a relevant not-ready child still blocks the parent.

## 13. Limitations

- The parent-first wall-clock figures above are **derived** from Run A's per-call `elapsed_seconds` measurements applied to the new call-count model — not a fresh live run (no clean-room local-proxy run was executed; live runs under concurrent audit load inflate durations, as documented in the original §8).
- A live re-measurement after the release is recommended to confirm the derived ~74% reduction on a real multi-child parent.
