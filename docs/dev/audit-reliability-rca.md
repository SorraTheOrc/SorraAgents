# RCA: Audit Pipeline Reliability — Why Phase 1 / Phase 2 Frequently Fail

**Work item:** SA-0MU32H6KY0021SJL
**Type:** Root-cause analysis (read-only; no production code changes)
**Evidence window:** 2026-08-09 → 2026-09-26 (48 days; requirement: ≥30 days)
**Motivating audit:** SA-0MSQ7MQEJ0064ZB0 (2026-09-15)
**Status of findings:** all counts below are reproducible from the local corpora
with the commands in [Appendix A](#appendix-a--reproducible-queries).

---

## 1. Executive summary

The audit pipeline does **not** fail mainly because the model returns
unparseable JSON. It fails mainly for **infrastructure and observability**
reasons:

1. **The `parse_failure` debug label is wrong.** `_call_pi_and_maybe_log`
   labels *every* call that carries `raw_stdout`/`raw_stderr` as
   `parse_failure` — and every successful call carries `raw_stdout`. In the
   48-day SorraAgents corpus, **97.4 % (265/272) of `parse_failure` entries
   actually contain a valid JSON array** when re-parsed with the runner's own
   extractor. The label is essentially a "call returned" marker, not a failure
   signal.
2. **The dominant genuine failure mode is wall-clock / budget exhaustion and
   concurrency saturation**, not schema drift. These are recorded as `unmet`
   or `partial` verdicts and force manual re-audits.
3. **A correction to the preliminary findings:** the evidence cited for **F5**
   ("in-main-slot child screens leave children unverified") was
   **mis-attributed**. The quoted log string `pending deep code review (Phase 1
   blocked)` comes from `_demote_met_to_partial` (Phase 1 found blocking
   issues), which is unrelated to the in-main-slot mechanism. The in-main-slot
   mechanism is real (default `AUDIT_CHILD_IN_MAIN_SLOT=true`) but is not
   evidenced as a frequent failure in this window (0 `main-session review`
   hits).

Because F1 corrupts the primary failure signal, **any reliability work that is
prioritised from the debug log alone is mis-prioritised**. Fixing the taxonomy
(R1) is a prerequisite for measuring everything else.

---

## 2. Evidence sources

| Source | Path | Volume | Date range |
|---|---|---|---|
| Debug corpus (Pi raw streams + `reason`) | `~/.audit_debug/SorraAgents/audit_debug_*.jsonl` | 93 files, 952 entries | 2026-08-09 → 2026-09-26 |
| Audit run logs (verdicts + diagnostics) | `~/.audit_debug/SorraAgents/*.log` | 93 files | 2026-08-09 → 2026-09-26 |
| Runner source | `skill/audit/scripts/audit_runner.py` | — | commit `c32a20e2` |
| Stored reports / checkpoints | `.worklog/worklog.db`, `.worklog/audit-checkpoints/` | 22 checkpoints | — |

**Corpus hygiene note.** Of the 952 debug entries, **672 belong to automated
test fixtures** (`SA-TEST-001/002/003`). These inflate the raw
`parse_failure` count (943) and must be excluded from reliability analysis.
All counts in this report are **real-project entries only (281)** unless
stated otherwise.

---

## 3. Failure taxonomy by phase (real-project entries)

Measured with the runner's own functions
(`extract_pi_text()` → `_extract_json_array()`):

| Phase group | Entries | `parse_failure` | False positive (JSON present) | Genuine (no JSON) | `provider_error` |
|---|---:|---:|---:|---:|---:|
| Phase 1 parent screen (`context=parent`) | 58 | 54 | 53 | 1 | 4 |
| Phase 1 child screen (`context=child:*`) | 86 | 86 | 85 | 1 | 0 |
| Phase 2 parent deep (`context=phase2_deep`) | 31 | 28 | 27 | 1 | 3 |
| Phase 2 child deep (`context=phase2_child:*`) | 72 | 71 | 67 | 4 | 1 |
| False-positive screen (`context=false-positive-screen`) | 31 | 31 | 31 | 0 | 0 |
| Verdict re-ask (`context=verdict_reask`) | 3 | 2 | 2 | 0 | 1 |
| **Total** | **281** | **272** | **265 (97.4 %)** | **7 (2.6 %)** | **9** |

**Reading the table:** the `parse_failure` column is noise. The columns that
matter are **genuine** (7) and **provider_error** (9) — together
**5.7 % of all calls**. Everything else labelled `parse_failure` parsed fine.

### 3.1 Genuine JSON-contract failures (7, all reproduced)

| Work item | Phase | Symptom | Cause |
|---|---|---|---|
| SA-0MSOK041Z0027964 | Phase 2 child deep | 0-character output | model returned empty |
| SA-0MSWIGJSG006AXN7 | Phase 2 child deep | 0-character output | model returned empty |
| SA-0MT6CEN8D0073F61 | Phase 2 child deep | 2-character whitespace output | model returned whitespace |
| SA-0MSXVXVUL0011JKX | Phase 2 child deep | 135 chars of prose, no array | model narrated, stopped early |
| SA-0MSN2ULOF007JJ35 | Phase 2 parent deep | 593 chars of prose, no array | model narrated, stopped early |
| SA-0MSOK041Z0027964 | Phase 2 child deep | fenced ` ```json ` block | fenced block not tolerated by extractor |
| SA-0MSKB697P000T3HG | Phase 1 parent screen | array starts correctly but is truncated | unterminated / malformed array |

The dominant genuine shape is **the model stops after narrative analysis and
never emits (or truncates) the required JSON array** — it is *not* "valid JSON
that the parser rejects". The one fenced-block case is a parser-tolerance gap
addressed by R2.

---

## 4. Findings, verified against the 48-day corpus

### F1 — `reason: parse_failure` fires on successful calls — **CONFIRMED**

`_call_pi_and_maybe_log` (`skill/audit/scripts/audit_runner.py`, ~L5164):

```python
elif isinstance(result, dict) and (result.get("raw_stdout") or result.get("raw_stderr")):
    reason = "provider_error" if result.get("_provider_error") else "parse_failure"
    target = _default_debug_log_path(issue_id, context)
```

* `raw_stdout` is populated on **every** call that returned, success or not.
* The label is therefore assigned **before** the call site parses the output,
  and there is **no `parse_ok` field** recording the actual parse outcome.
* Measured: **265 of 272** real `parse_failure` entries contain a valid JSON
  array (97.4 %). The label cannot be used to count failures.

**Effect on observability:** every prior "parse failure" statistic derived
from this label is invalid (including the "~92–95 %" estimate in the intake
brief and the "39,619 parse_failure entries" figure). F1 is the **highest
leverage single fix** because it unblocks trustworthy measurement of every
other finding.

*Examples:* `~/.audit_debug/SorraAgents/audit_debug_CG-0MSLXJCHH001DLIO.jsonl`
(`reason=parse_failure`, valid JSON present); `audit_debug_SA-0MSKB697P000T3HG.jsonl` (parent call, valid array emitted).

### F2 — Genuine JSON-contract failures are real but rare (~2.6 %) — **CONFIRMED, refined**

The preliminary figure of ~5–8 % / 34 genuine failures is **not reproduced** in
the current 48-day corpus: the measured rate is **7/272 = 2.6 %**. The
difference is corpus size (the earlier figure covered 285+ files, including
rotated logs) — the qualitative conclusion holds: genuine failures are rarer
than the misleading label suggests, and are dominated by **empty/narrative
output**, not by malformed-but-present JSON.

*Example:* `audit_debug_SA-0MT6CEN8D0073F61.jsonl` (2-char response);
`audit_debug_SA-0MSXVXVUL0011JKX.jsonl` (prose only).

### F3 — Wall-clock / budget exhaustion is the dominant genuine failure — **CONFIRMED (partially remediated)**

Runner diagnostics in the window:

| Diagnostic | Occurrences | Representative file / item |
|---|---:|---|
| `semaphore 'audit' busy: no slot free within 0.0s (max_workers=1)` | 60 | `audit_run_SA-0MSW6PG6Q002S4M6_20260822T011615.log` (SA-0MSW6PG6Q002S4M6) |
| `audit concurrency queue 'audit' saturated: no slot within 90s` | 12 | `SA-0MUF833QL001WJXD-20260924T180742.log` (SA-0MUF833QL001WJXD) |
| `pending deep code review (Phase 1 blocked)` | 38 | `SA-0MSN52GGN002B0AZ-1786966151.log` (SA-0MSN52GGN002B0AZ) |
| `Pi provider error` | 7 | `audit_run_SA-0MSJELSWS002UF60_r4_20260809T224842.log` (SA-0MSJELSWS002UF60) |
| `Pi model call timed out after 1800s` | 5 | `audit_run_SA-0MSJI53RX006E2PS_20260811T102316.log` (SA-0MSJI53RX006E2PS) |
| `skipped this child after … total elapsed time` | 2 | `SA-0MSOIS36N0024ORI-20260812-035752.log` (1216 s / 710 s budget) |
| `parent-process budget exceeded` | 1 | `SA-0MSKQERKH002IBLG-close.log` (1514 s / 710 s budget) |

Observed parent budgets vary widely in historical logs (~120 s / 710 s /
900 s / 1500 s / 3110 s). The **R3 fix has landed** (the
`partial (budget exceeded)` contract is now emitted, see
`SA-0MSKQERKH002IBLG-close.log`), so F3 is **partially remediated** — the
remaining risk is budget *right-sizing* and resume coverage, not silent skips.

### F4 — Concurrency saturation aborts Phase 1 parent screens — **CONFIRMED**

The `semaphore 'audit' busy` path fails fast (`AUDIT_LOCK_TIMEOUT_DEFAULT =
0.0`) and returns an `unmet` verdict with **zero Pi calls made**, so a parent
screen can be blocked purely by another audit holding the slot. The priority
queue path (`audit concurrency queue 'audit' saturated: no slot within 90s`)
waits up to 90 s and then also gives up. Prior art: SA-0MSGEAZMC009LHKL
(introduced queue admission), SA-0MU32T8SH001R8MZ / R4 (priority reserve for
the Phase 1 parent screen — `completed`).

*Examples:* `SA-0MUF833QL001WJXD-20260924T180742.log` (90 s wait, no slot);
`audit_run_SA-0MSW6PG6Q002S4M6_20260822T011615.log` (`max_workers=1`,
immediate abort).

### F5 — In-main-slot child screens leave children unverified — **REFUTED as evidenced; mechanism real**

The preliminary finding cited `pending deep code review (Phase 1 blocked)` as
evidence. That string is produced by `_demote_met_to_partial` when **Phase 1
detected blocking issues**, and is unrelated to the in-main-slot execution
mode. The in-main-slot mode (default) emits `[AUDIT_IN_MAIN_SLOT_WORK]` and
marks child ACs `partial` with evidence `main-session review`; in this 48-day
corpus there are **0** such hits. The mechanism exists and is covered by
`skill/audit/tests/test_audit_runner_child_in_main_slot.py`, but it is **not
evidenced as a frequent failure mode**. The correct framing is a **contract
gap** (no explicit "N child screens pending" gate), addressed by R5.

### F6 — Provider errors are conflated with parse failures — **CONFIRMED**

9 real `provider_error` entries (1.1 % of calls): HTTP 500
(SA-0MSL1Z70C007B9VZ, ×3), HTTP 503 `mode_switch_drain`
(SA-0MSNYMKV7005P0H9), and `Connection error.` (SA-0MSM6VU2K001IMVP,
SA-0MSRWAY9A005Q0CB, SA-0MSUT8GQP004WSYN, SA-0MU8EKJYY007PT42,
SA-0MUDUXXMK006ANA0, plus 1 in `verdict_reask`). These *are* labelled
`provider_error` in `_call_pi_and_maybe_log`, but the call-site debug writes
(e.g. `child_ac_fallback`) and the summary surfaces do not separate them from
parse failures, so downstream counts still conflate the two.

---

## 5. Root-cause analysis

The failure modes share three structural causes:

1. **The debug log records "the call returned", not "the call succeeded".**
   The parse outcome is only known at the call site, after
   `_call_pi_and_maybe_log` has already written the entry. The taxonomy is
   therefore assigned at the wrong layer (observability defect → F1, F2, F6).
2. **Budget and concurrency are handled as verdicts, not as resumable state.**
   When the parent budget or the audit semaphore is exhausted, the runner
   degrades to `unmet`/`partial` *inside* the audit rather than checkpointing
   and resuming (→ F3, F4). The R3/R4 work addressed the worst of this; the
   remaining gaps are right-sizing and resume coverage.
3. **The child-audit execution contract has no explicit pending gate.**
   In-main-slot mode is a silent state transition: if the invoking session does
   not run the emitted work, children stay `partial (main-session review)` with
   no "pending" summary or resume affordance (→ F5 contract gap).

---

## 6. Prioritised recommendations (impact × effort)

Ranked by impact × effort. Each row names the concrete change, the expected
reliability effect, and the observable signal that confirms improvement.

| # | Priority | Impact | Effort | Change | Expected effect | Confirming signal | Follow-up |
|---|---|---|---|---|---|---|---|
| R1 | High | High | Low | Replace the blanket `parse_failure` label in `_call_pi_and_maybe_log` with a neutral `call_trace` default plus an explicit `parse_ok` flag set by the call site | Makes every downstream failure statistic trustworthy; unblocks measurement | Re-run corpus query: `parse_failure` drops from ~97 % to the genuine ~2.6 %; successful entries carry `parse_ok: true` | SA-0MU32TAMB007HI99 |
| R2 | High | High | Medium | JSON-first prompting + tolerant extraction (strip ` ```json ` fences, balanced-array scan) + one bounded "re-ask for JSON only" | Recovers the empty/narrative/fenced genuine failures (7/272 measured) | Genuine parse-failure rate over a re-run corpus; re-ask recovers ≥1 fixture | SA-0MU32TCFM003B78I |
| R3 | High | High | Medium | Derive budgets from measured `Per-call timing` p50/p95 with margin; checkpoint and resume instead of bare skips | Removes the dominant genuine failure (budget exhaustion) and rework | Child-skip count falls; resumed runs reuse checkpoints (R3 fix already in review) | SA-0MU32T6O0001UALR |
| R4 | High | High | Low-Med | Reserve/priority a concurrency slot for the Phase 1 parent screen; surface queue depth/wait | Phase 1 parent no longer aborts under 2 concurrent audits | Phase 1 parent completes while another audit holds a slot | SA-0MU32T8SH001R8MZ |
| R5 | Med-High | High | Low | Execute child screens in-process, or gate parent closure on an explicit "N child screens pending" with resume | No parent report ends with silent pending child screens | Parent report always states pending count or completes | SA-0MU32TE120012T49 |
| R6 | Medium | Medium | Low | Feed the R1 taxonomy so provider errors are counted/surfaced separately in reports, and make the bounded provider-error retry policy (`_call_pi` `max_retries` + `_PI_RETRY_BACKOFF_SECONDS`) explicit and observable per run | Distinguishes flaky provider from schema-contract problems | Provider-error count and retry attempts visible per run | SA-0MU32TFKB0012ALC |
| R7 | Medium | Medium | Low | Emit one machine-greppable per-run reliability summary (calls, parse_ok, parse_failed, timeouts, child_skips, provider_errors, concurrency_waits) | Removes log archaeology for future RCAs | Summary line present per run | SA-0MU32TH89000YHP9 |

**Rejected / out-of-scope**

* R8 (project-local full-suite timeout): already covered by open item
  **SA-0MU2PXILZ000RPR1** (`.pi/test-config.json` `timeoutPerCommand`); not
  duplicated.
* Changing audit verdict semantics: rejected — R1/R6/R7 must stay
  observability-only so they cannot weaken fail-closed behaviour.

---

## 7. Follow-up work items (AC6)

All recommendations are captured as children of this item (created 2026-09-15):

| Rec | Work item | Status at time of writing |
|---|---|---|
| R1 | SA-0MU32TAMB007HI99 | open (idea) |
| R2 | SA-0MU32TCFM003B78I | open (idea) |
| R3 | SA-0MU32T6O0001UALR | completed / in_review |
| R4 | SA-0MU32T8SH001R8MZ | completed / done |
| R5 | SA-0MU32TE120012T49 | open (idea) |
| R6 | SA-0MU32TFKB0012ALC | blocked on R1 |
| R7 | SA-0MU32TH89000YHP9 | blocked on R1, R6 |

R3's budget work has landed; its remaining subtasks (budget baseline,
budget-exceeded formatting, resume, docs, tests) are still open/blocked and
are tracked under SA-0MU32T6O0001UALR.

---

## 8. Limitations and corrections to the preliminary findings

* **Corpus rotation.** The corpus available on 2026-09-26 (93 files / 281 real
  entries) is smaller than the 2026-09-15 corpus referenced in the intake
  brief (285+ real-project logs, "~34 genuine"). Counts here are internally
  consistent and reproducible; the difference is volume, not method.
* **F5 corrected.** The `pending deep code review (Phase 1 blocked)` evidence
  does not support the in-main-slot claim (see F5).
* **F2 refined.** Genuine failure rate is ~2.6 %, not ~5–8 %.
* **Provider-error conflation** at call-site summary surfaces remains even
  though `_call_pi_and_maybe_log` sets the correct reason (F6).
* **"parse_ok" is absent today.** Until R1 lands, no count of genuine parse
  failures can be derived from the debug corpus alone; this RCA derived them
  by re-running the production extractor over `raw_stdout`.

---

## Appendix A — Reproducible queries

Run from the repository root. Python snippets are self-contained.

**A1. Reason counts and phase-grouped false-positive rate (real-project only):**

```bash
python3 - <<'PY'
import sys, os, glob, json, re, collections
root = os.getcwd()
sys.path.insert(0, os.path.join(root, "skill"))
from scripts.pi_utils import extract_pi_text
src = open(os.path.join(root, "skill/audit/scripts/audit_runner.py")).read()
ns = {"json": json, "re": re}
s = src.index("def _extract_json_array("); e = src.index("\n_CHECKBOX_MARKER_RE", s)
exec(src[s:e], ns); extract = ns["_extract_json_array"]          # production parser
def grp(c):
    if c == "parent": return "Phase 1 parent screen"
    if c.startswith("child:"): return "Phase 1 child screen"
    if c == "phase2_deep": return "Phase 2 parent deep"
    if c.startswith("phase2_child:"): return "Phase 2 child deep"
    return c or "other"
by = collections.defaultdict(collections.Counter)
for f in sorted(glob.glob(os.path.expanduser("~/.audit_debug/SorraAgents/audit_debug_*.jsonl"))):
    for line in open(f, errors="replace"):
        line = line.strip()
        if not line: continue
        try: d = json.loads(line)
        except Exception: continue
        if str(d.get("issue_id", "")).startswith("SA-TEST"):      # exclude fixtures
            continue
        g = by[grp(d.get("context"))]; g["total"] += 1
        if d.get("reason") == "parse_failure":
            g["parse_failure"] += 1
            text = extract_pi_text(d.get("raw_stdout") or "") or (d.get("extracted_text") or "")
            g["false_positive" if extract(text) is not None else "genuine"] += 1
        elif d.get("reason") == "provider_error":
            g["provider_error"] += 1
for g, c in sorted(by.items()):
    print(g, dict(c))
PY
```

**A2. Runner diagnostic phrase counts in the run logs:**

```bash
LOGDIR=~/.audit_debug/SorraAgents
for p in "no slot within" "semaphore 'audit' busy" "pending deep code review (Phase 1 blocked)" \
         "Pi provider error" "Pi model call timed out" "skipped this child after" \
         "parent-process budget exceeded"; do
  printf '%-48s %s\n' "$p" "$(grep -rhoF "$p" "$LOGDIR"/*.log 2>/dev/null | wc -l)"
done
```

**A3. Corpus coverage / date range:**

```bash
ls -l --time-style=+%Y-%m-%d ~/.audit_debug/SorraAgents/audit_debug_*.jsonl | awk '{print $6}' | sort | uniq -c
```

**A4. One genuine failure, re-parsed end to end:**

```bash
python3 - <<'PY'
import sys, os, json
root = os.getcwd(); sys.path.insert(0, os.path.join(root, "skill"))
from scripts.pi_utils import extract_pi_text
p = os.path.expanduser("~/.audit_debug/SorraAgents/audit_debug_SA-0MT6CEN8D0073F61.jsonl")
for line in open(p):
    d = json.loads(line)
    print(d["issue_id"], d["context"], "extracted_len=", len(extract_pi_text(d["raw_stdout"] or "")))
PY
```

---

## Appendix B — Source references

* `skill/audit/scripts/audit_runner.py`
  * `_call_pi_and_maybe_log` — misleading reason taxonomy (~L5079–L5195).
  * `_extract_json_array` — tolerant extractor (~L3888).
  * `_demote_met_to_partial` — Phase-1-blocked demotion (~L5200).
  * `_resolve_child_in_main_slot` / `_emit_in_main_slot_work` — in-main-slot mode (~L3225–L3290).
  * Concurrency: `_audit_semaphore_max_workers`, `_audit_semaphore_lock_timeout`, admission queue (~L2559–L2800).
  * Parent budget: `_default_parent_timeout`, `_resolve_parent_timeout` (~L1749, ~L1991).
* `docs/dev/audit-skill-reference.md`, `skill/audit/SKILL.md`.
* Prior art: SA-0MPOGKY8U007K206 (introduced the label), SA-0MQ4GGKUR00752Z8
  and SA-0MRW8T054007QH0A (JSON parsing), SA-0MSL1Z15600760KC (parser
  consolidation, open), SA-0MSF4AFXF000M5DN / SA-0MSABZO2T004B95X (timeout
  guards), SA-0MSGEAZMC009LHKL (queue admission), SA-0MS0SQQTW006GNX9 (Phase 2
  timeout), SA-0MT2XRGEU0009QRE (in-main-slot mode).
