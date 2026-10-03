# Downtime idle-gate evaluation for audit dispatch

**Work item:** SA-0MTG5UPBR0028K50 (parent SA-0MTG0Y34T007ZHPF)
**Date:** 2026-10-03
**Verdict:** **Keep the strict idle gate — do NOT relax it for audits.**

This is the research deliverable for the idle-gate evaluation. It collects
the metrics the parent plan called for, interprets them, and records the
recommendation and its follow-up.

---

## 1. Question

The herdr downtime dispatcher only dispatches **any** work (including
audits) while a strict idle window is open: the local LLM must be
continuously idle for the configured threshold (default 60 s) **and** have
at least the required free slots. The parent item's problem statement
hypothesised that under sustained multi-agent traffic this gate never opens,
so audits starve and items stay `in_review`. The question for this item was
whether the gate should be **relaxed for audit-only dispatch**.

Operator constraint (parent plan, 2026-08-30): *"We cannot loosen the gate."*
This evaluation therefore tests whether relaxing the gate would actually
help, or whether the residual contention has a different cause.

---

## 2. Method

Metrics are collected by the read-only tool
[`skill/shared/audit_gate_metrics.py`](../../skill/shared/audit_gate_metrics.py)
(a pure-Python, write-free metrics collector). It performs no writes and has
no side effects.

Three sources are read:

| Source | What it measures |
|--------|------------------|
| `<root>/.worklog/downtime-dispatches.log` (JSONL, bounded rolling log) | Dispatch events → **gate windows**, items drained per window, audit dispatch frequency |
| `.audit_debug/**` `Audit slot acquired:` stderr lines | **Queue admission waits** per priority tier (the bounded priority queue, SA-0MTG5RYH8005RQNM) |
| Worklog SQLite `audit_results` table (opened read-only) | Persisted **concurrency-limit verdicts**: legacy fail-fast, bounded-queue saturation, host-wide saturation |

Reproduce the figures below with:

```bash
python3 skill/shared/audit_gate_metrics.py \
  --root /home/rgardler \
  --root /home/rgardler/projects/SorraAgents \
  --root /home/rgardler/projects/ContextHub \
  --root /home/rgardler/projects/Tableau-Card-Engine \
  --worklog-db /home/rgardler/projects/SorraAgents/.worklog/worklog.db \
  --worklog-db /home/rgardler/projects/ContextHub/.worklog/worklog.db \
  --worklog-db /home/rgardler/projects/Tableau-Card-Engine/.worklog/worklog.db \
  --worklog-db /home/rgardler/projects/llm-manager/.worklog/worklog.db \
  --human
```

**Deployment boundary.** The bounded priority queue and batch drain
(SA-0MTG5RYH8005RQNM, SA-0MTG5TP5Z008QBL5) shipped on **2026-08-30/31**.
Metrics are therefore split into *pre-deployment* and *post-deployment*.

---

## 3. Metrics collected

### 3.1 Concurrency-limit verdicts (north-star AC1)

Counted per work item from persisted `audit_results` (an item is counted
once per category even when the string repeats in its report):

| Verdict category | Matched text | Pre-deployment | Post-deployment |
|------------------|--------------|---------------:|----------------:|
| Legacy fail-fast | `no slot free within 0.0s` | **27** | **0** |
| Bounded-queue saturation | `concurrency queue 'audit' saturated` | 0 | **14** |
| Host-wide saturation | `host-wide audit concurrency limit reached` | 0 | 0 |

- Legacy fail-fast verdicts occur only on 2026-08-07 (2 items), 2026-08-17
  (1), 2026-08-21 (4), 2026-08-22 (3), 2026-08-23 (8) and 2026-08-24 (9) —
  all **before** the bounded queue shipped.
- Post-deployment the fail-fast count is **zero**. The remaining 14
  bounded-queue saturation verdicts occur on 2026-09-17 (2), 2026-09-24 (2),
  2026-09-25 (1), 2026-09-26 (8) and 2026-10-01 (1) — these waited the full
  queue bound (≥ 90 s) before returning `unmet`.

**Finding:** the bounded queue eliminated *fail-fast* re-queueing exactly as
designed. The north-star "zero concurrency-limit unmet verdicts" is
**improved but not fully met**: 14 items still time out after the bounded
wait under peak saturation.

### 3.2 Queue-admission wait distribution

448 audit slot admissions were observed across `.audit_debug`:

| Priority | Admissions | Avg wait | p95 wait | Max wait | Slow (> 30 s) |
|----------|-----------:|---------:|---------:|---------:|--------------:|
| critical | 177 | 19.34 s | 75.94 s | 565.46 s | 21 |
| high | 128 | 6.36 s | 0.05 s | 599.82 s | 2 |
| medium | 112 | 1.39 s | 0.01 s | 80.45 s | 2 |
| low | 31 | 0.00 s | 0.00 s | 0.00 s | 0 |
| **all** | **448** | **9.81 s** | **32.5 s** | **599.8 s** | **25** |

**Finding:** admission is instant for the large majority of audits
(medium/low p95 ≈ 0). Slow admissions cluster on `critical` items, which are
dispatched most aggressively during saturation windows. Priority ordering is
working — `low` never waited — and waits are bounded, not fail-fast.

### 3.3 Gate windows and throughput

Clustering dispatch events with a 30-minute gap threshold gives 90
approximate gate windows in the retained log sample, draining 268 dispatch
events (57 audit) — **2.98 items per window**. Per-day audit dispatch counts
on the busiest days were 3–17; the retained-window average window duration
is 1,131 s (~19 min), peak 18,399 s (~5.1 h).

Audits **completed** per day across all projects (from `audit_results`) in
the week to 2026-10-03: 72, 95, 17, 71, 24, 36, 5 — **320 audits in 7 days**.
The gate is therefore opening regularly and audits are flowing.

### 3.4 Backlog drain

The parent's problem statement cited **28 `in_review` items** stuck in
Tableau-Card-Engine (up to 4,178 h old). As of 2026-10-03 the open
(non-completed) `in_review` backlog across all on-host worklogs is **5 items**
(dev-scripts 3, llm-manager 1, tce-main-street 1); Tableau-Card-Engine has
**0**. The backlog the item was created to clear has drained.

---

## 4. Throughput analysis: current gate vs relaxed gate

The gate controls **when** a new dispatch may start. It does **not** control
how fast an already-running audit completes. The host-wide audit semaphore
(`--max-concurrency` / `AUDIT_MAX_HOST_AUDITS`) is the throughput ceiling
once dispatch starts.

Observed evidence that the ceiling, not the gate, is binding:

1. Admission is instant for most audits (`medium`/`low` p95 ≈ 0) — the queue
   is usually empty, so the gate is not starving audits.
2. Saturation only appears during **peak bursts** (2026-09-26/27, when
   65–123 admissions arrived in a day) and produces waits up to 600 s plus
   14 timeouts — i.e. more concurrent audits than the semaphore admits.
3. The dispatcher already requires ≥ 2 free slots before an audit dispatch
   (`DOWNTIME_AUDIT_MIN_FREE_SLOTS`), which is itself a fan-out brake.

Relaxing the gate (e.g. 10 s idle, or "dispatch whenever ≥ 2 slots free")
would admit **more concurrent audit starts**. Given the semaphore is already
briefly saturated under the *current* dispatch rate, a relaxed gate would
increase — not decrease — queue waits, saturation timeouts and the provider
errors that accompany contention. It would move the bottleneck from a
controllable, slot-aware gate to an uncontrolled queue.

A relaxed gate therefore **does not advance the north-star goal**. The
residual `unmet` verdicts are a capacity/timeout-tuning problem, not a
gate-opening problem.

---

## 5. Recommendation

**Keep the strict idle gate unchanged; do not relax it for audits.**

Supporting reasons:

- The gate is protective: it reserves free slots for interactive use and
  keeps concurrent audit fan-out within the semaphore ceiling.
- The bounded priority queue already removed fail-fast (27 → 0), and the
  backlog has drained (28 → 0 in TCE; 5 open `in_review` host-wide).
- Saturation is capacity-bound. Relaxing the gate would increase concurrent
  starts and worsen contention.
- The operator constraint ("we cannot loosen the gate") is consistent with
  the evidence.

**Actionable next steps (no gate change):**

1. Track the residual bounded-queue saturation (14 items) as a capacity
   follow-up: tune `AUDIT_QUEUE_TIMEOUT` upward and/or `AUDIT_MAX_CONCURRENCY`
   / `AUDIT_MAX_HOST_AUDITS` to the host's real slot capacity, so peak bursts
   wait instead of timing out. Tracked as a `discovered-from` work item.
2. If further gate analysis is wanted, instrument durable **gate-open
   events** in the herdr dispatcher (the rolling dispatch log records
   successful dispatches only, not gate openings that found no candidate),
   so `N` in the parent's AC2 can be measured directly over a 7-day window.
3. No `dev`→`main`-scope gate change and no ContextHub dispatcher change is
   recommended by this evaluation.

---

## 6. Limitations

- The downtime dispatch log is a **bounded rolling log** (100 entries per
  root), so it cannot yield a full continuous 7-day window; window counts
  are a lower bound derived from retained entries.
- "Gate window" is an *approximation*: a cluster of dispatches is treated as
  one opening because the log records dispatches, not idle transitions.
- Queue-admission lines are stderr text captured opportunistically in
  `.audit_debug`; audits whose stderr was not retained are not counted.
- The host was running concurrent experiments (tce-\* repositories) during
  the sample, so peak-day figures are upper bounds.
- Time-to-audit per priority is not separately derivable: the worklog does
  not record the timestamp an item entered `in_review`, so only
  dispatch-to-completion counts (not per-item latency) are reported.
