# RCA: Parent-run child audits demote `in_review` children (and the child-screen budget that feeds it)

**Work items:** SA-0MVI35BIU002MTZQ (demotion defect, high) · SA-0MVI3608M0080QLW (child-screen budget, medium)
**Type:** Root-cause analysis
**Observed:** 2026-10-21, ContextHub, work item `WL-0MVFNMSRW002FI6B` (11-child epic)
**Recurrence of:** SA-0MUJAPC680078396 (completed — "re-auditing an `in_review` child demotes it and can cascade a parent block") and SA-0MSGMR7CX00588TZ (completed — "flag infra-fallback verdicts, never demote")
**Status of findings:** F1 and F2 are proven from code + persisted state. F3 is proven as a code inconsistency; its role in the observed verdict is **not yet confirmed** (see §5).

---

## 1. Executive summary

A parent audit run in separate-process child mode (`--no-child-in-main-slot`) demoted
**all 11** of its children from `completed`/`in_review` to `open`/`blocked` at
`plan_complete`, and then reported its own verdict as `Ready to close: No` with the
sole blocker being those children — despite all six parent acceptance criteria
being `met`/`adjusted`.

Two previously-closed fixes were supposed to prevent exactly this:

* **SA-0MUJAPC680078396** introduced the child-exemption snapshot so a
  parent-triggered re-screen of an `in_review` child cannot block or demote it.
* **SA-0MSGMR7CX00588TZ** established that an infra-fallback verdict is not an
  explicit model "No" and must take the *restore* path, never the demote path.

Both guarantees are implemented **only in the runner's own lifecycle for the item
under audit** (`_apply_terminal_lifecycle`). Child audits persisted by a parent run
travel a completely different path — `_persist_child_audit()` → `persist_audit.py` →
`wl audit-set --ready-to-close no` — which is a **skill-unaware primitive** that
reverts the child's lifecycle directly. The guarantees are therefore bypassed, not
merely weakened.

The demotion was fed by a second, independent weakness: the 600 s child-screen
budget is **marginal** on a loaded host (5 of 11 child screens landed exactly on the
cap), so 8 of the 11 persisted child verdicts were `No` on infra-fallback-tainted
evidence (7 timeouts / 7 parse failures of 27 calls).

---

## 2. Observed sequence (timeline)

| Time (2026-10-21) | Event |
|---|---|
| 10:23 | Parent audit launched (`--no-child-in-main-slot`, HEAD `b4d5e0d2`). All 11 children are `completed`/`in_review`. |
| 11:08 | Phase 1 child screenings complete (4963 s). 5 of 11 screens capped at 600 s. |
| 11:08 → 11:45 | Phase 2 child deep analysis (9 children; F1 and F11 skipped — effort S/XS + risk Low). |
| **11:58:45 – 11:58:58** | Child audits persisted. **All 11 children demoted at these exact timestamps.** |
| 12:22 | Parent run completes: `Ready to close: No`, blocker = "11 children not yet in in_review/done stage". |

Pre-demotion state (all 11): `completed`/`in_review`.
Post-demotion state: F1, F8 → `open`/`plan_complete`; the other nine → `blocked`/`plan_complete`.

---

## 3. Finding F1 — `wl audit-set` reversion bypasses every audit-lifecycle guard — **CONFIRMED**

### Mechanism

```
audit_runner._persist_child_audit()            # audit_runner.py ~L6206
  └─ persist_audit.persist_audit()             # persist_audit.py ~L515-532
       └─ wl audit-set <child> --ready-to-close no
            └─ src/commands/audit-result.ts:207-214
                 └─ db.revertToPlanComplete(child)
                      └─ completed/in_review  →  open/plan_complete
```

`persist_audit.py` derives the flag from the report text:

```python
ready = "yes" if _extract_ready_to_close(report_text) else "no"   # persist_audit.py:515
...
cmd = [wl_bin, "audit-set", issue_id, "--ready-to-close", ready, ...]   # ~L527-533
```

and `wl audit-set` treats a negative verdict as an invitation to re-open the item:

```typescript
// src/commands/audit-result.ts:207-214
if (rtc === 'no') {
  reverted = db.revertToPlanComplete(normalizedId);
}
```

```javascript
// packages/shared — database.revertToPlanComplete
const eligible = item.status === 'completed' && item.stage === 'in_review';
if (!eligible) return null;
const updated = this.update(itemId, { status: 'open', stage: 'plan_complete' });
```

`revertToPlanComplete` is unconditional with respect to *why* the verdict was `No` —
it has no notion of child exemptions, cascade suppression, or fallback provenance.

### Proof (measured, not inferred)

| Child | stored `readyToClose` | lifecycle before | lifecycle after |
|---|---|---|---|
| F1 `WL-0MVFRR98A008MM19` | `true` | completed/in_review | **open/plan_complete** (see §5) |
| F2 `WL-0MVFRRAXD001E7AN` | `true` | completed/in_review | completed/in_review ✓ |
| F3 `WL-0MVFRRCP7007UWS7` | `false` | completed/in_review | **blocked/plan_complete** |
| F5 `WL-0MVFRREMZ0085G7I` | `false` | completed/in_review | **blocked/plan_complete** |
| F8 `WL-0MVFRRGGC001IQK1` | `false` | completed/in_review | **open/plan_complete** |
| F4, F6, F7, F9, F10 | `false` | completed/in_review | **blocked/plan_complete** |
| F11 `WL-0MVFRRTY5001KTNH` | `true` | completed/in_review | completed/in_review ✓ |

*All eight children persisted with `readyToClose=false` were reverted; both children
with `true` were not.* The `blocked` states are then produced by the worklog
dependency engine, not by the audit: F8 (no dependencies) became `open`, and its
dependents became `blocked`.

### Why this is a recurrence, not a new feature gap

* SA-0MUJAPC680078396 AC2 states the invariant directly: *"a parent-triggered
  re-screen must never silently push completed/in-review work back into the
  actionable queue or cascade `blocked` states through the dependency chain."*
* SA-0MSGMR7CX00588TZ AC2 requires an infra-fallback `No` to restore the captured
  pre-audit state.
* `_cascade_suppress_demotion` (audit_runner.py ~L12700) implements the suppression
  — but only when `_is_cascade_audit()` is true, i.e. when the child was audited by a
  **spawned child-audit subprocess** carrying the `AUDIT_CASCADE_AUDIT` marker. In
  `--no-child-in-main-slot` mode the parent persists child audits **in-process** via
  `persist_audit`, which never sets that marker and never consults those guards.

This explains an important observed asymmetry: the first run of the same item
(**in-main-slot mode**, the default) did **not** demote anything — it only emitted
pending `[AUDIT_IN_MAIN_SLOT_WORK]` screens. The demotion appears when the operator
opts into separate-process child audits.

---

## 4. Finding F2 — the child-screen budget is marginal, and a capped screen is not quarantined — **CONFIRMED**

`_CHILD_SCREEN_TIMEOUT_DEFAULT = 600` (audit_runner.py ~L631-642; env
`AUDIT_CHILD_SCREEN_TIMEOUT`). Measured child Phase-1 screens in this run:

| Screen | F1 | F2 | F3 | F4 | F5 | F6 | F7 | F8 | F9 | F10 | F11 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| seconds | **600.12** | 336.41 | **600.11** | 337.44 | **600.05** | 258.12 | 463.25 | 506.12 | **600.03** | **600.05** | 394.36 |

* **5 of 11 (45 %) landed exactly on the cap**; the other six completed in 258–506 s
  (median 389 s, **p95 ≈ 506 s**). The unconstrained distribution already sits within
  ~15 % of the cap, so any additional host load converts completions into timeouts.
  Children that capped had 6–7 ACs and wide FILE SCOPE manifests; the XS/docs child
  completed comfortably — i.e. the screens most in need of judgement are the ones
  that lose it.
* Each capped screen is followed by a `verdict_reask`, **2 of which also hit 600 s**.
* Run summary: `AUDIT_RELIABILITY_SUMMARY calls=27 parse_ok=20 parse_failed=7 timeouts=7`
  — 26 % of calls produced no usable verdict.
* Those tainted calls produced the 8 `No` child verdicts that F1 then turned into
  lifecycle changes.

**Compounding contract gap:** a capped screen currently degrades to an ordinary
`partial`, which is indistinguishable from a model judgement. Nothing in the run
summary distinguishes "the model assessed this and said no" from "the model was
killed at 600 s", so a downstream consumer cannot quarantine it — and per F1 the
downstream consumer (`wl audit-set`) acts on the verdict regardless.

---

## 5. Finding F3 — report text and closure verdict use different exemption sources — **CONFIRMED as inconsistency; role in this run unconfirmed**

Inside `_assemble_issue_report`:

```python
# audit_runner.py ~L5633 — VERDICT path: honours the snapshot
all_children_reviewed = all(
    _child_is_exempt(c, snapshot_exempt) or c.get("stage") in ("in_review", "done")
    for c in active_children
)

# audit_runner.py ~L5759 — TEXT path: ignores the snapshot
not_reviewed = [
    c for c in child_results
    if c.get("stage") not in ("in_review", "done", "")
]
```

`not_reviewed` feeds the "N children not yet in in_review/done stage" summary line.
Because it ignores `snapshot_exempt`, the report text can contradict the closure
decision it accompanies — an operator reading "11 children not yet in
in_review/done stage" cannot tell whether or not the snapshot was applied.

**Unconfirmed sub-question.** With a correctly populated snapshot, the parent's
`ready_before_cq` should have been `Yes` (all ACs acceptable, children
snapshot-exempt). It was `No`, so either the snapshot was empty (children already
demoted *before* `_capture_exempt_children_snapshot` ran), the snapshot was not
propagated to the closure path, or an additional exemption term failed. This is
called out as an acceptance criterion on SA-0MVI35BIU002MTZQ rather than asserted
here. Note the one-way nature of the reversion: `audit-set` reverts on `no` but
**never promotes on `yes`**, so any earlier tainted `no` write for a child is not
undone by a later passing persist — which is the most likely explanation for F1
reaching `open`/`plan_complete` despite a stored `readyToClose=true`.

---

## 6. Root cause

| Layer | Defect | Consequence |
|---|---|---|
| **Lifecycle primitive** | `wl audit-set --ready-to-close no` ⇒ `revertToPlanComplete`, with no awareness of audit exemptions or fallback provenance | Child audits persisted by a parent demote exempt children; `blocked` cascades |
| **Runner boundary** | The skill's lifecycle guards live in `_apply_terminal_lifecycle` (for the audited item) and `_cascade_suppress_demotion` (cascade-marker only), not on the child-persistence path | The invariant is enforced in one of three write paths |
| **Budget** | 600 s child-screen cap ≈ the p95 of completed screens on a loaded host | 45 % of screens tainted; verdicts are infra-fallback 'No's |
| **Observability** | A capped screen is recorded as a plain `partial`; `AUDIT_RELIABILITY_SUMMARY` counts `timeouts` only in aggregate | Downstream cannot quarantine tainted verdicts |
| **Report assembly** | `not_reviewed` (~L5759) ignores `snapshot_exempt` while `all_children_reviewed` (~L5633) honours it | Text and verdict can disagree; misleads operators |

The shared structural cause is the same one identified in
[`audit-reliability-rca.md`](audit-reliability-rca.md) §5, in a different guise:
**lifecycle decisions are made at more than one layer, and only one of those layers
knows the skill's contracts.** `audit-set` is a general-purpose worklog primitive
whose reversion is correct for an *item being deliberately re-audited* and wrong for
a *child being screened as a side effect of its parent*.

---

## 7. Recommendations

| # | Priority | Change | Expected effect | Tracked by |
|---|---|---|---|---|
| R1 | **High** | Make child-audit persistence lifecycle-neutral: persist via a path that writes the audit **without** the `audit-set` reversion (e.g. `--ready-to-close` omitted, or an explicit `--no-revert`), then let the runner's `_apply_terminal_lifecycle` own any transition — with the snapshot/fallback guards | Removes the demotion class entirely in both child-audit modes | SA-0MVI35BIU002MTZQ AC1 |
| R2 | **High** | Extend the "infra-fallback never demotes" rule (SA-0MSGMR7CX00588TZ) to child verdicts, and carry fallback provenance into `persist_audit` so a tainted 'No' cannot demote | Tainted runs become observable restores, not silent regressions | SA-0MVI35BIU002MTZQ AC2 |
| R3 | **High** | Add a regression test that snapshots every child's `(status, stage)` before a parent audit and asserts an identical set afterwards, including a `blocked`-cascade assertion | Makes the invariant executable; the observable signal is "zero lifecycle deltas" | SA-0MVI35BIU002MTZQ AC3/AC5 |
| R4 | Medium | Make `not_reviewed` (~L5759) honour `snapshot_exempt`, i.e. one `_child_is_exempt` helper for both the verdict and the text | Report text can no longer contradict the closure decision | SA-0MVI35BIU002MTZQ AC4 |
| R5 | Medium | Derive the child-screen budget from measured p95 × a documented safety factor (≈2× ⇒ 1,000–1,200 s here) and re-measure the cap-hit rate | Cuts capped screens from 45 % → ≤ 5 %, removing the main source of tainted child verdicts | SA-0MVI3608M0080QLW AC1/AC2 |
| R6 | Medium | Surface `child_screen_timeouts` in `AUDIT_RELIABILITY_SUMMARY` and name the timeout in the affected AC evidence | Lets operators and gates quarantine tainted verdicts | SA-0MVI3608M0080QLW AC3 |
| R7 | Low | Record the AC-count/FILE-SCOPE-width correlation for capped screens, to inform a future manifest-width bound | Data for the parent epic's Phase-1 reduction work | SA-0MVI3608M0080QLW AC5 |

**Rejected / out of scope**

* Making `wl audit-set --ready-to-close no` never revert: rejected — the reversion is
  deliberate and useful for a genuinely re-audited item (WL-0MSKHYI5U0069FVV). The
  fix belongs at the audit-skill boundary, not in the worklog primitive.
* Reducing Phase-1 call count / concurrency sizing: already scoped to
  SA-0MVF1NKZQ009P3ZG (AC3, AC5) and deliberately not duplicated here.

---

## 8. Operational mitigation (already applied)

The affected children were restored to their captured pre-audit state
(`completed`/`in_review`, assignee cleared) in dependency order, and verified: 0
children outside that state. The parent's persisted `Ready to close: No` was **not**
overridden — the audit skill treats the runner's verdict as authoritative — so the
parent remains blocked on this defect rather than on outstanding work. Once R1–R4
land, a single parent re-audit should return `Ready to close: Yes` with **no code
changes** to the audited project.

---

## Appendix A — Reproducible queries

**A1. Per-child stored verdict (the demotion input):**

```bash
# from the project that owns the worklog
for id in $(wl show <PARENT_ID> --children --json | jq -r '.children[].id'); do
  printf '%s ' "$id"
  wl audit-show "$id" --json | jq -c '{readyToClose: .audit.readyToClose, auditedAt: .audit.auditedAt}'
done
```

**A2. Child lifecycle state (before/after):**

```bash
wl show <PARENT_ID> --children --json \
  | jq -r '.children[] | "\(.id)\t\(.status)\t\(.stage)"'
```

**A3. Child-screen elapsed times and cap hits (from the run log):**

```bash
LOG=~/.audit_debug/<project>/<audit-run>.log
grep -E "context=child:" "$LOG" \
  | sed -E 's/.*issue_id=([A-Z0-9]+) context=child:[A-Z0-9]+ elapsed_seconds=([0-9.]+).*/\2 \1/' \
  | sort -rn
echo "capped (>=595s): $(grep -E 'context=child:' "$LOG" \
  | sed -E 's/.*elapsed_seconds=([0-9.]+).*/\1/' | awk '$1>=595' | wc -l)"
```

**A4. Reliability summary:**

```bash
grep -h "AUDIT_RELIABILITY_SUMMARY" "$LOG"
```

**A5. Confirm the reversion primitive (static check):**

```bash
rg -n "revertToPlanComplete" src/commands/audit-result.ts packages/shared/src
rg -n "ready-to-close|_extract_ready_to_close" skill/audit/scripts/persist_audit.py
```

---

## Appendix B — Source references

* `skill/audit/scripts/audit_runner.py`
  * `_persist_child_audit` (~L6206) — the in-process child-audit persistence path.
  * `_cascade_suppress_demotion` / `_is_cascade_audit` (~L12700) — suppression keyed to the `AUDIT_CASCADE_AUDIT` marker only.
  * `_capture_exempt_children_snapshot` (~L8568) and `_child_is_exempt` (~L7105).
  * `_assemble_issue_report`: `all_children_reviewed` (~L5633) vs `not_reviewed` (~L5759).
  * `_CHILD_SCREEN_TIMEOUT_DEFAULT` / `_resolve_child_screen_timeout` (~L631–642, ~L2770).
  * `_apply_terminal_lifecycle` (~L12640–12750) — where the guards actually live.
* `skill/audit/scripts/persist_audit.py` — `_extract_ready_to_close` (~L321), `audit-set` invocation (~L515–533).
* `src/commands/audit-result.ts:207-214` — reversion trigger.
* `packages/shared` — `database.revertToPlanComplete`.
* `skill/audit/SKILL.md` — Status Lifecycle, cascade-audit demotion suppression, freshness gate.
* Prior art: SA-0MUJAPC680078396 (child-exemption snapshot), SA-0MSGMR7CX00588TZ (infra-fallback never demotes), SA-0MT2XRGEU0009QRE (in-main-slot mode), WL-0MSKHYI5U0069FVV (audit-set reversion intent), SA-0MVF1NKZQ009P3ZG (audit wall-clock epic), `docs/dev/audit-reliability-rca.md`.
