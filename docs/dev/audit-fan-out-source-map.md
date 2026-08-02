# Audit / Agent Fan-Out Source Map

**Work item:** [Investigate concurrent audit fan-out causing extreme load (SA-0MSAEKOQE009TEB4)]

This document maps every known source of concurrent heavy-process fan-out
(pi agent sessions, vitest worker pools, `wl sync`, batch skills) with
origin file:line references. It is the AC #1 deliverable of the fan-out
investigation and the baseline for the concurrency-control work items.

Measured impact on the rgardler workstation (16-core, 30 GiB RAM,
8 GiB swap) at baseline — see
[`docs/dev/fanout-baseline-pre-control.json`](fanout-baseline-pre-control.json):

| Metric | Baseline value (pre-control) |
|--------|------------------------------|
| Load average (1m / 5m / 15m) | 247–265 / 273–279 / 204 |
| pi processes | 18 |
| node processes | 188–254 |
| vitest/tinypool workers | 39–69 |
| audit_runner.py processes | 6–7 |
| `wl sync` | 0 (occasional) |
| Swap used | 3.68 GiB / 8.00 GiB |

---

## 1. Audit runner: unbounded pi subprocess spawn (`_call_pi`)

- **File:** `skill/audit/scripts/audit_runner.py`
- **Line refs:** `_call_pi` defined at L544; `subprocess.Popen(cmd, ...)`
  for the pi binary at **L592**; retry loop L580–L611.
- **Mechanism:** every model call inside an audit launches a fresh
  `pi -p --mode json --model <model> <prompt>` subprocess via
  `subprocess.Popen`. There is **no concurrency limit**: N concurrent audit
  invocations × M model calls per audit = N×M pi processes.
- **Fan-out multiplier:** an audit with Phase 1 (verdict) + Phase 2 (deep
  analysis) issues multiple pi calls per work item; each call is one
  subprocess.
- **Related code:** `_call_pi_and_maybe_log` at L1542 wraps `_call_pi` for
  Phase 2 with logging.

## 2. Audit runner: nested child-trigger path (`audit_runner issue --force`)

- **File:** `skill/audit/scripts/audit_runner.py`
- **Line refs:** child loop at **L2634–L2687**; `subprocess.run(audit_cmd,
  ...)` spawning a *nested* `audit_runner.py issue <child> --force` at
  **L2665**.
- **Mechanism:** when auditing a parent work item with children lacking
  audits, the parent audit auto-triggers a **new, fully independent audit
  process per child** (up to the parent's elapsed-time budget, L2650).
  Each nested audit again spawns unbounded pi subprocesses.
- **Fan-out multiplier:** 1 parent audit → up to N child audits → N×M pi
  processes. Multiple concurrent parent audits multiply this further.

## 3. Vitest worker pools (tinypool)

- **File (ContextHub):** `packages/ContextHub/vitest.config.ts`
  (`/home/rgardler/projects/ContextHub/vitest.config.ts`)
- **Line refs:** `maxWorkers: 4` and `singleFork: true` (pool cap present).
- **File (Tableau-Card-Engine):** `vite.config.ts`
- **Line refs:** unit-test project at L33–L41 has **no `maxWorkers`/pool
  cap**; only `replay-e2e` sets `singleFork: true` (L46–L59).
- **Mechanism:** vitest by default sizes its tinypool worker pool to the
  number of CPUs; each worker is a node process. Under fan-out, multiple
  concurrent vitest runs × 15+ workers each = dozens–hundreds of node
  processes.
- **SorraAgents:** pytest-based; no vitest config exists — excluded from
  vitest capping.

## 4. `wl sync` — serialized via process-level file lock

- **File:** Worklog CLI (`worklog@1.0.6`), source
  `/home/rgardler/projects/ContextHub/src/commands/sync.ts`, shipped to
  `/home/rgardler/projects/ContextHub/dist/cli.js`, installed globally as
  `/usr/local/lib/node_modules/worklog` (symlink).
- **Line refs:** sync command at `src/commands/sync.ts` L350+; the entire
  pull/merge/push runs inside `withFileLock(getLockPathForJsonl(...))`
  (L367–L390) — a process-level `O_EXCL` mutex on
  `.worklog/worklog-data.jsonl.lock` with stale-lock cleanup
  (`src/file-lock.ts`). `--if-idle` skips (exit 0, `skipped: true`) when
  another sync holds the lock; otherwise a second sync waits up to 30 s then
  fails with a clear message.
- **Mechanism:** at most one sync runs per worklog at a time across all
  sessions; concurrent `wl sync` invocations serialize (verified by
  `tests/cli/sync-concurrent.test.ts`).

## 5. Batch skills: sequential per item, heavy children per step

- **Files:**
  - `skill/implementall/SKILL.md` (batch implementation loop)
  - `skill/intakeall/SKILL.md` (batch intake)
  - `skill/planall/SKILL.md` (batch planning)
- **Mechanism:** these skills process work items **sequentially by design**,
  but each step spawns heavy children (pi sessions via implement/intake,
  audit subprocesses, `wl sync`, test suites). When multiple batch runs
  overlap (e.g., operator session + agent batch + heartbeat audit), the
  per-step fan-out compounds without any global ceiling.
- **Note:** this is orchestration-level overlap; the per-item fan-out is
  covered by sources 1–4.

## 6. Operator-launched pi sessions

- **Mechanism:** interactive pi sessions (TUI/CLI) launched by the operator
  are independent of agent code and cannot be bounded by a semaphore.
  Documented here for completeness — mitigation is to cap automated
  sources (1–5) so operator sessions do not compound the load.

---

## Control levers (planned / in progress)

| Source | Control | Status |
|--------|---------|--------|
| 1–2 (audit runner) | Shared flock-based semaphore around pi spawns + child triggers, ceiling via `AUDIT_MAX_CONCURRENCY` | Child SA-0MSAK2P3J0065POO / SA-0MSAK2SNN005HCM5 |
| 3 (vitest) | `maxWorkers: 4` cap on Tableau-Card-Engine unit project (added, mirrors ContextHub); ContextHub already capped (`maxWorkers: 4`, `singleFork: true`); SorraAgents is pytest-based (no vitest) | Child SA-0MSAK2ZH6009Z3TW — DONE |
| 4 (`wl sync`) | Process-level file lock (O_EXCL mutex) serializes concurrent syncs; `--if-idle` skips; stale-lock cleanup | Child SA-0MSAK2W0F0027ZP7 — DONE |
| 5 (batch overlap) | Bounded via per-item controls above + configurable ceilings | Covered by parent |
| 6 (operator sessions) | Documented; not directly controllable | N/A |

## Related work items

- Parent: SA-0MSAEKOQE009TEB4 (this investigation)
- SA-0MSAK2G62002Z1RJ — Fan-out source map + baseline harness (this doc)
- SA-0MSAK2L5P0066GW8 — Concurrency guard test suite
- SA-0MSAK2P3J0065POO — Shared cross-process semaphore module
- SA-0MSAK2SNN005HCM5 — Audit runner concurrency cap
- SA-0MSAK2W0F0027ZP7 — wl sync serialization
- SA-0MSAK2ZH6009Z3TW — Vitest pool caps standardization
- SA-0MSAK33NN008NS95 — Load validation & no-regression check
- SA-0MSAEJCP7002LTIM — Optimize audit grep scans (related; CPU sink, not fan-out)
