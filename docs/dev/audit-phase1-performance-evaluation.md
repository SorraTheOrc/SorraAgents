# Audit skill Phase 1 — Performance Evaluation Report (P7)

**Work item:** SA-0MSF3RXU8005CFGD (Audit skill: apply Phase 2 performance
treatment to Phase 1 AC review (P7))
**Parent:** SA-0MSADWWH3003N82D (Audit skill Phase 2: performance evaluation
and improvement)
**Date:** 2026-08-05
**Status:** Implementation complete — Phase 1 now mirrors the Phase 2
performance pattern (file-scope manifest + SCANNING block + read-only tools +
bounded parallelism + `child_audit_ready` reuse).

---

## 1. Summary

The Phase 2 performance epic (P1–P6) moved the audit bottleneck to **Phase 1**.
Measured live runs (see `docs/dev/audit-phase2-measured-report.md`) showed
Phase 1 (parent + child AC screening) consuming 13,483s = 88% of wall-clock for
a 10-child parent (avg 1,348s/call, max 2,400s), while Phase 2 was reduced to a
single bounded 1,817s call.

Phase 1 had the exact deficiencies Phase 2 had before the epic:

1. **No file-scope manifest** — Phase 1 prompts named no files, so the model
   could not target its reading.
2. **No SCANNING bounded-helper block** — no guardrail against unbounded
   `find`/`grep -r`/`ls -R` exploration.
3. **No tools** — Phase 1 ran Pi in bare LLM pipe mode
   (`enable_tools=False`), yet the prompt asked for file:line evidence. The
   model could only extrapolate from description + AC text.
4. **No parallelism** — child AC screening ran strictly sequentially.
5. **No `child_audit_ready` reuse** — children whose own fresh audit already
   reviewed their ACs were screened a second time by the parent.

This work item applies the Phase 2 treatment to Phase 1: prompts now carry the
file-scope manifest + SCANNING block, sessions enable the same read-only tool
set (`--tools read,bash,grep,find,ls --exclude-tools ask_question`), pending
child screenings run with bounded concurrency (default 2, configurable via
`AUDIT_PARALLELISM`), and ready children skip Phase 1 screening
entirely, reusing their persisted AC verdicts.

**Verdict semantics are unchanged** — Phase 1 verdicts remain
`met/unmet/partial/adjusted` with the same normalization and adjusted-verdict
guidance. Only the reading strategy (tools + manifest + parallelism + reuse)
changed.

---

## 2. Call-path changes

Before (per active child, sequential):

```
cmd_issue
  ├─ Phase 1 parent screening        (no manifest, no SCANNING, no tools)
  └─ for each child:
       └─ Phase 1 child AC review    (no manifest, no SCANNING, no tools,
                                      strictly sequential)
```

After:

```
cmd_issue
  ├─ pre-pass: per active child
  │    ├─ elapsed-guard check (unchanged)
  │    ├─ completed/done exemption -> child_audit_ready=True, reuse audit
  │    └─ wl audit-show verdict -> ready? reuse : queue for screening
  ├─ Phase 1 parent screening        (manifest + SCANNING + enable_tools=True)
  ├─ pending child screenings        (ThreadPoolExecutor, cap default 2,
  │                                    enable_tools=True, manifest + SCANNING)
  └─ Phase 2 (unchanged; reuses child_audit_ready=True as before)
```

The pre-pass verdicts are also reused by the auto-trigger loop (no second
`wl audit-show` per child).

---

## 3. Measured results (instrumented, mocked Pi)

Method: `cmd_issue` driven with mocked `wl` + mocked `_call_pi`; each simulated
Phase 1 child screening call takes 0.4s, parent/phase2 calls 0.2s; 4 children
at stage `in_review`. Per-call timing lines emitted by `_call_pi_and_maybe_log`
were captured from stderr (same `Per-call timing: issue_id=... context=...
elapsed_seconds=...` instrumentation used for Phase 2).

| Scenario | Parallelism | Phase 1 child calls | Wall-clock | Reduction vs sequential |
|----------|-------------|--------------------:|-----------:|-------------------------|
| A sequential | 1 (`AUDIT_PARALLELISM=1`) | 4 | 3.62s | baseline |
| B parallel | 2 (default) | 4 | 2.01s | 44% |
| C parallel | 4 | 4 | 1.21s | 67% |
| D ready-reuse | 2 | **0** | 0.40s | 89% |

Scenario D also skips the Phase 2 child deep-analysis calls (the parent Phase 2
already reuses `child_audit_ready=True`), so the whole run is a single parent
screening + single `phase2_deep` call.

Captured per-call timing lines (scenario B excerpt):

```text
Per-call timing: issue_id=TEST-1  context=parent          elapsed_seconds=0.20
Per-call timing: issue_id=CHILD-1 context=child:CHILD-1   elapsed_seconds=0.40
Per-call timing: issue_id=CHILD-2 context=child:CHILD-2   elapsed_seconds=0.40
Per-call timing: issue_id=CHILD-3 context=child:CHILD-3   elapsed_seconds=0.40
Per-call timing: issue_id=CHILD-4 context=child:CHILD-4   elapsed_seconds=0.40
```

Interpretation for the real-world baseline (10 children, ~1,348s per Phase 1
call): bounded parallelism (default 2) cuts the child-screening leg from
~10 × 1,348s to ~5 × 1,348s, and `child_audit_ready` reuse removes the leg
entirely for children whose own fresh audit already reviewed their ACs — the
dominant win. The manifest + SCANNING block additionally bound the per-call
cost itself (the live baseline's unbounded exploration is what made calls hit
1,348–2,400s).

---

## 4. Guard semantics preserved

- The cumulative elapsed-time guard (`PARENT_TIMEOUT_DEFAULT`, default 110s,
  `--parent-timeout` to override) is checked **per child inside the pre-pass,
  before any dispatch**, exactly as before; parallel batches are dispatched
  only after the per-child guard check passes.
- No timeout increases were made (calls keep the 1800s default).
- Phase 1 calls keep the default `_PI_MAX_RETRIES` budget (retry tuning was
  explicitly out of scope — that is P10).
- Phase 2 prompts and behavior are untouched (`test_phase2_prompt_unchanged`).

---

## 5. Verification

- 12 new unit tests in `skill/audit/tests/test_audit_runner.py` cover: manifest
  + SCANNING presence in Phase 1 parent/child prompts, `enable_tools=True`
  flag forwarding for Phase 1 contexts, ready-child reuse (reduced call count),
  bounded-parallel dispatch, `_parse_audit_report_acs` / fallback-met reuse
  evidence, and worker exception-safety (RuntimeError → `_record_script_failure`
  + diagnostic `partial` verdicts).
- Full audit suite: 433 passed (`skill/audit/tests/test_audit_runner.py` +
  `tests/test_audit_runner_{core,review,persist}.py` +
  `tests/test_audit_{skill,skill_doc,code_quality,pr}.py`).
- Existing verdict-semantics tests pass unchanged (`met/unmet/partial/adjusted`
  normalization, adjusted-verdict guidance, completed/done exemption,
  guard-skip diagnostic verdicts).

---

## 6. Docs

- `skill/audit/SKILL.md`: updated Phase 1 performance treatment section
  (tools-enabled invocation now covers Phase 1, new "Phase 1 performance
  treatment (P7)" subsection documenting manifest/SCANNING, reuse, parallelism).
- `docs/dev/audit-phase2-measured-report.md`: the live measured baseline this
  work item responds to (committed so references resolve).
