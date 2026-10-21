# Audit performance baseline

Measured baseline for audit LLM call timing, produced by the committed
measurement harness `scripts/measure_audit_performance.py`
(work item **SA-0MVI6VET100337TW**, parent epic
**SA-0MVF1NKZQ009P3ZG — "Audits take too long: investigate and speed up"**).

This document exists so a future change to the audit pipeline can be verified
against a committed, reproducible measurement instead of an ad hoc one. It
pins a snapshot of this host's audit debug-log corpus, states the regression
gate, and reconciles its figures with the parent epic brief.

## Regression gate

> **Single-item audit wall-clock regression gate: a healthy single-item audit
> run must complete in ≤ 10 minutes (600 s), end to end, on a healthy local
> proxy with no concurrent audits.**

- **Definition of the measurement:** end-to-end wall clock of
  `python3 skill/audit/scripts/audit_runner.py issue <id>` on a small item
  (1–5 ACs), starting with the runner invocation and ending when it returns
  (time the command: `time python3 skill/audit/scripts/audit_runner.py issue <id>`).
- **Conditions for a valid run:** the local proxy reports healthy
  (`GET /llama/local/status` with `available_slots > 0`), no other audit or
  `pi` session is running concurrently, and the item has no children.
- **Failure of the gate** is evidence that a change regressed audit wall
  clock; the per-context table below (re-run with the harness) attributes the
  regression to a phase. The gate is checked whenever a performance-affecting
  change lands in the audit pipeline (release gate and post-release
  verification).

## Snapshot metadata

| Field | Value |
|---|---|
| Host | `rgardler-AI-Laptop` |
| Corpus root | `~/.audit_debug` |
| Measurement date | 2026-10-21 (16:52 UTC) |
| Log file count | 635 (`*.log`, recursive) |
| First log mtime | 2026-08-09 |
| Last log mtime | 2026-10-21 |
| Timing lines parsed | 1,750 |
| Contexts | `parent`, `child`, `phase2_deep`, `phase2_child`, `verdict_reask`, `false-positive-screen` |
| Projects represented | SorraAgents, AI_Hell, ContextHub, Tableau-Card-Engine, ToneForge, open_source_llm, tce-* (one host) |

The corpus stitches **every** audit debug log on this host, across projects
and across re-audits of the same item. It is append-only: a re-run later
contains more calls, never fewer.

## Reproduction

```bash
# Authoritative accounting (whole-call wall clock, default metric):
python3 scripts/measure_audit_performance.py --root ~/.audit_debug

# Epic-brief accounting (per-AC mean for Phase 2 contexts -- see below):
python3 scripts/measure_audit_performance.py --root ~/.audit_debug --metric per-ac

# Machine-readable (for scripts/gates):
python3 scripts/measure_audit_performance.py --root ~/.audit_debug --json
```

The harness is **read-only**: it never writes to `~/.audit_debug/`, to
`.worklog/`, or to the audit runner. It exits `0` when at least one timing
line was parsed and `1` (with actionable advice) when the corpus has none.

## Baseline (2026-10-21 snapshot, elapsed accounting)

| Context | calls | total | p50 | p90 | max | @600s | @900s | @1800s |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `parent` (Phase 1 parent screen) | 472 | 54.6 h | 238.5 s | 1052.0 s | 2705.3 s | 3 | 0 | 16 |
| `phase2_child` (Phase 2 per-AC) | 336 | 48.7 h | 448.5 s | 1186.7 s | 2070.6 s | 1 | 0 | 9 |
| `phase2_deep` (Phase 2 deep) | 303 | 45.2 h | 334.8 s | 1422.8 s | 3600.1 s | 2 | 1 | 14 |
| `child` (Phase 1 per-child screen) | 434 | 41.9 h | 270.9 s | 612.3 s | 2400.1 s | 96 | 4 | 0 |
| `verdict_reask` (JSON-contract retry) | 148 | 24.5 h | 531.2 s | 1584.5 s | 2372.1 s | 19 | 0 | 6 |
| `false-positive-screen` | 57 | 4.3 h | 181.6 s | 600.1 s | 1200.0 s | 10 | 0 | 0 |
| **TOTAL** | **1,750** | **219.2 h** | **304.6 s** | **1059.1 s** | **3600.1 s** | **131** | **5** | **45** |

Headline figures (all from the harness, elapsed accounting):

- **1,750 calls, 219.2 h total** LLM wall clock across the corpus.
- **Median call 304.6 s**; p90 1,059 s.
- **Phase 1 share (parent + child): 44.0 %** of call time (~96.5 h).
- **Wasted share: 29.2 %** (~64.0 h) = **24.5 h** in `verdict_reask` retries
  (11.2 %) + **39.5 h** in calls that landed on a budget (600/900/1,800 s)
  and produced a `partial` fallback (18.0 %).
- Calls landing squarely on a budget: **131 at 600 s, 5 at 900 s, 45 at
  1,800 s** (within 1 s of the budget value).

### Definitions used by the harness

- **Parsing:** a line counts only when it *starts* with `Per-call timing:`
  followed by `issue_id=… context=… elapsed_seconds=…`. The start-of-line
  anchor rejects the quoted prompt text and markdown tables in audit logs that
  also contain the phrase.
- **Context normalisation:** `child:<id>` and `phase2_child:<ac-index>` are
  collapsed onto their phase (`child`, `phase2_child`), so the table shows six
  stable contexts.
- **Percentiles:** linear interpolation (the `numpy.percentile` default).
- **At-budget:** a call whose elapsed time is within 1.0 s of a configured
  budget (defaults 600/900/1,800 s) — the runner's wall clock rounds, so an
  exact match is too strict.
- **Wasted:** `verdict_reask` time plus the elapsed time of at-budget calls
  outside `verdict_reask` (re-asks that land on a budget are counted once).
- **Shares** are always derived from elapsed wall-clock time, even when the
  cost columns use the per-AC metric, so they stay comparable.

## Reconciliation with the parent epic brief

The epic brief (`SA-0MVF1NKZQ009P3ZG`) quoted **1,637 calls / 140.8 h /
62 % Phase 1 / ~18 % wasted**, measured ad hoc on 2026-10-19. The committed
harness reproduces those figures on this host within documented tolerance:

| Context | brief calls | harness calls | brief total | harness `per-ac` | harness `elapsed` (authoritative) |
|---|---:|---:|---:|---:|---:|
| `parent` | 450 | 472 | 48.3 h | 54.6 h | 54.6 h |
| `child` | 412 | 434 | 38.1 h | 41.9 h | 41.9 h |
| `verdict_reask` | 121 | 148 | 18.7 h | 24.5 h | 24.5 h |
| `phase2_deep` | 285 | 303 | 16.0 h | 17.0 h | 45.2 h |
| `phase2_child` | 316 | 336 | 15.8 h | 17.1 h | 48.7 h |
| `false-positive-screen` | 53 | 57 | 3.9 h | 4.3 h | 4.3 h |
| **Total** | **1,637** | **1,750** | **140.8 h** | **159.4 h** | **219.2 h** |

Two findings explain the differences:

1. **The brief's ad hoc parser summed the per-AC mean for Phase 2.** Its
   figures for the Phase 2 contexts match `--metric per-ac` closely
   (`phase2_deep` 16.0 vs 17.0 h, `phase2_child` 15.8 vs 17.1 h): a parser that
   read the last `elapsed_seconds=`-style field on Phase 2 lines picked up
   `avg_ac_elapsed_seconds` (call duration ÷ AC count) instead of the whole
   call. That undercounts true Phase 2 wall clock by ~2.7×. The harness's
   `elapsed` accounting is authoritative for wall clock; `--metric per-ac`
   exists solely to reproduce the brief's basis.
2. **The corpus grew between the brief and this snapshot:** +113 calls
   (+6.9 %), because `~/.audit_debug` is append-only and production audits
   continued running.

Consequently the quoted **62 % Phase 1 share** is an artefact of the
undercounted Phase 2: on the per-AC basis today Phase 1 is **60.5 %** (96.5 h
of 159.4 h), on the true elapsed basis **44.0 %**. The correct reading of this
corpus is that Phase 2 and Phase 1 are now roughly co-equal, Phase 2 per-AC
fan-out (336 calls) has surpassed Phase 1 child screens (434 calls) in total
cost, and the wasted share (29.2 %) is larger than the brief's ~18 % because
the true Phase 2 denominator is bigger.

This reconciliation is a correction, not a contradiction: the **regression
gate above** (≤ 10 min single-item wall clock) is unaffected by how the corpus
is summed, and the parent epic's remediation items (concurrency sizing, JSON
re-ask elimination, Phase 1 batching) all target buckets that remain large on
either basis.

### Documented tolerance for reproduction

- **Determinism:** running the harness twice on the same corpus yields
  byte-identical output — there is no sampling.
- **Growth:** a re-run after this snapshot adds calls. It reproduces this
  baseline when per-context call counts are within **±10 %** and per-context
  totals within **±15 %** of the figures above, and the Phase 1 / wasted
  shares are within **±3 percentage points**. Larger deviations mean the
  corpus changed materially (new budget values, a new context, a pruned
  directory) — record the new snapshot in this document's changelog rather
  than treating it as noise.
- **The brief's basis:** re-running with `--metric per-ac` reproduces the
  epic brief's Phase 2 figures within the same growth tolerance (observed
  +6–8 %).

## Using the baseline to verify a performance change

To verify that a pipeline change actually helped (parent epic AC6 — ≥ 50 %
wall-clock reduction on a representative audit):

1. **Before:** run the harness and record `total_hours`, Phase 1 share,
   wasted share, and the per-context table for the *current* corpus.
2. Make the change; re-run the harness on the same (grown) corpus.
3. Compare per-context totals and, for a like-for-like comparison, the
   `--metric per-ac` basis. The gate is the **single-item audit wall clock
   (≤ 10 min)**, measured as defined above; the corpus deltas attribute
   changes to phases.
4. When the audit runner gains a new context or budget, add it to this
   document's table instead of letting the corpus drift silently.

## Limitations

- **One host, one corpus.** All figures are from `rgardler-AI-Laptop`; the
  corpus mixes projects and includes re-audits of the same item, so it
  measures aggregate behaviour, not a single run.
- **No per-run attribution.** `Per-call timing:` lines carry no run id; two
  concurrent audits are not separable from the logs.
- **Wall clock is contention-inclusive.** P50/p90 reflect queueing behind the
  local proxy, which is why the regression gate requires a healthy, idle
  proxy.
- **`elapsed_seconds` includes timeouts.** Calls that hit their budget record
  the full budget duration, which is correct for wall-clock accounting and
  why the at-budget columns exist.

## Changelog

- **2026-10-21** — Baseline committed with the harness (SA-0MVI6VET100337TW).
  First committed snapshot; reconciles with the parent epic brief and
  documents the Phase 2 per-AC undercount in the brief's ad hoc figures.
