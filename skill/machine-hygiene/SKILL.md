---
name: machine-hygiene
description: "Evaluate machine performance pressure and propose safe, approved remediations. Use when checking host or machine hygiene."
---

# Machine-Hygiene Skill

## Purpose

Automates the manual performance-pressure analysis performed on hosts running
many concurrent pi agent sessions and test suites. It collects metrics,
analyses root causes, proposes concrete remediations, requires explicit
operator approval before any destructive action, executes the approved subset,
and reports before-and-after state.

**Read-only by default** — no process is killed, deleted, or modified without
explicit operator/producer approval.

## Triggers

- `/skill:machine-hygiene`
- "machine hygiene", "machine-hygiene", "check machine health"

## Usage

```bash
python3 $(skill_path machine-hygiene)/scripts/evaluate.py
```

When invoked via `/skill:machine-hygiene` from the Pi chat, the skill runs
the evaluate script and displays the output in the chat window.

## Workflow (6 steps)

### Step 1 — Diagnostic Evaluation (Before Table)

Collects and renders a **before table** with the following metrics, each
classified as **low** (green), **warning** (amber), or **critical** (red):

| Metric | Source | PSI line (basis) | Classification basis |
|---|---|---|---|
| Load average | `/proc/loadavg` vs. core count | — | Configurable multiplier |
| CPU pressure | `/proc/pressure/cpu` | `some` avg10 (% of time *any* task stalled on CPU) | Percentage thresholds (`cpu_pressure`) |
| Memory pressure | `/proc/pressure/memory` | `full` avg10 (% of time *all* tasks stalled on memory) | Percentage thresholds (`memory_pressure`) |
| I/O pressure | `/proc/pressure/io` | `full` avg10 (% of time *all* tasks stalled on I/O) | Percentage thresholds (`io_pressure`) |
| Free memory | `/proc/meminfo` | — | Percentage thresholds (`memory_usage`) |
| Swap usage | `/proc/meminfo` | — | Percentage thresholds (`swap_usage`), gated on current memory pressure (see below) |
| Process count | `/proc` scan | — | Core-count multiplier (`process_count`: warn `cores*48`, crit `cores*96`) |
| Thread count | `/proc` scan | — | Core-count multiplier (`thread_count`: warn `cores*160`, crit `cores*320`) |
| Per-command RSS (top 10) | `/proc/[pid]/status` | — | Absolute RSS bytes |
| Runnable processes | `/proc` state scan | — | Core-count multiplier (`runnable_processes`: warn `cores*2`, crit `cores*4`) |
| Stale devices | Zombie + validated `/var/lock` scan | — | Absolute count of *genuinely* stale locks (see below) |

### Pressure stall (PSI) semantics

`/proc/pressure/*` files expose up to two lines per resource:

- **`some`** — the percentage of the last 10 s during which *at least one*
task was stalled on the resource. It fires long before capacity is
exhausted and is the only line the CPU file provides (a CPU is never
stalled for every task at once).
- **`full`** — the percentage of the last 10 s during which *all* runnable
tasks were stalled. This is the "nothing made progress" signal and is what
makes memory and I/O pressure actionable.

Line selection is deliberate and explicit in `metrics.py`
(`_read_proc_pressure(key, which)`): **cpu → `some`, memory → `full`,
io → `full`**. Each metric's `description` in the rendered table names the
selected line and its basis, so a row is self-documenting.

**No fallback.** If the kernel does not expose the requested line (e.g. a
pre-4.20 PSI backport that only ships `some` for memory), the metric
degrades to `n/a` with a description naming the line that was requested —
never a silent fallback to the other line, because mixing `some` and `full`
would misstate severity. The same `n/a` degradation applies on non-Linux
hosts and to kernels without pressure-stall info; the workflow continues and
no metric crashes.

**Stall percentage is not usage percentage.** `memory_usage` (from
`/proc/meminfo`) is a *capacity* figure, so it is calibrated at 80/95 %.
`memory_pressure` (the `full` PSI line) is a *stall* figure: a host can sit
at 50 % RAM and still stall 20 % of wall time under reclaim churn, while 95 %
RAM used with no stalls is not a memory stall problem at all. The two are not
comparable, which is why `memory_pressure` has its own block
(`warning: 5`, `critical: 20`) rather than borrowing the memory-usage
thresholds — the latter would leave the metric permanently green and throw
away the signal.

**Threshold provenance.** `cpu_pressure` (70/85) and `io_pressure` (50/80)
are inherited from the pre-parser-fix configuration and have not yet been
validated against real stall data; `memory_pressure` (5/20) is a starting
calibration for `full`-line semantics. Tune them in `config.yaml` from
observed runs.

#### Multi-agent host calibration (process and thread counts)

`process_count` and `thread_count` have their **own** blocks; neither reuses
the `runnable_processes` scheduler thresholds. A herdr multi-agent host runs
16–21 concurrent pi sessions, and each session plus its test/build children
legitimately contributes dozens of processes and ~100+ threads:

- `process_count`: warn `cores * 48`, crit `cores * 96`. On a 16-core host:
  768 / 1536 — a busy 560-process host is green; >1536 is critical.
- `thread_count`: warn `cores * 160`, crit `cores * 320`. On a 16-core host:
  2560 / 5120 — ~2250 threads is green; >5120 is critical.

Both blocks are independently overridable in `config.yaml`, so a thread-heavy
host can be retuned without loosening the process alarm (and vice versa).
This recalibrates the alarm rather than disabling it: values above each
critical multiplier still classify `critical`.

#### Stale-lock validation (`/var/lock`)

Each regular file in `/var/lock` is validated before it can count as stale:

1. **Kernel-owned boot locks** (`asound.state.lock`, `card0.lock`,
   `card1.lock`, `card2.lock` — ALSA locks created at boot) are never stale.
2. **Ownership** — root-owned lock files are system/operator-managed and are
   never reported as stale.
3. **Age** — a lock touched within the last 24 h is treated as active.
4. **Owning-process liveness** — when the lock file records a bare PID, a
   live PID means the lock is held; only a dead PID (with age and ownership
   satisfied) is stale.

Only genuinely stale locks increment `stale_lock_files` and feed the
pressure-analysis prune proposal. `actions.py` additionally refuses to
remove stale locks automatically ("stale lock removal is
operator-managed"), as defence in depth, so a misclassified lock is never
removed.

#### Swap/memory cross-check

`swap_usage` is a **historical** figure: swap accumulates over days of paging
and stays occupied after pressure has passed. Swap occupancy therefore only
warns when memory is under *current* pressure — `memory_usage` at/above its
warning threshold, or the PSI `memory_pressure` (`full` avg10) at/above its
warning threshold. A warning-level swap reading is escalated to `critical`
when `memory_usage` is critical. If PSI memory data is unavailable, the
capacity figure (`memory_usage`) is the sole signal (graceful degradation).
This is why a host at 69 % swap with 35 % RAM and no memory stalls now reads
green instead of warning.

### Step 2 — Pressure Analysis

Identifies the **primary pressures** — which processes and resources
contribute most to the observed metrics. For each pressure:

- Names the contributing processes (by name, PID, parent, session)
- Attributes pressure to the responsible systemd unit or session where possible
- Quantifies the attributable impact (e.g. "grep consumed 4.4 GB RSS")

### Step 3 — Remediation Proposal

For each identified pressure, proposes **concrete remediation actions**:

| Action type | Description |
|---|---|
| kill | Terminate a runaway process (with ancestry verification) |
| renice | Lower priority of a non-urgent process |
| prune | Close stale sessions, remove stale lock files |
| pace | Suggest throttling concurrent work |

Each action includes the target PID/path, justification, and expected impact.

### Step 4 — Approval Gate

The skill **stops** and presents the full findings (before table, analysis,
proposed actions) to the operator/producer. **No destructive action is
taken** before explicit approval.

The operator reviews the proposed actions and approves or declines each one.
Only explicitly approved actions are executed.

### Step 5 — Conditional Action Execution

Executes only the **approved subset** of proposed remediation actions.
Tracks and reports which actions were approved vs. declined, and the
outcome of each executed action.

### Step 6 — After Table

Renders an **after table** alongside the before table showing the current
state for each metric, with per-metric priority annotations, and reports
residual/legitimate load (e.g. active test suites, ongoing sessions).

## Scripts

- `$(skill_path machine-hygiene)/scripts/config.py` — Configuration loader; reads `config.yaml` with fallback defaults.
- `$(skill_path machine-hygiene)/scripts/metrics.py` — Metric collection; gathers system metrics and classifies them.
- `$(skill_path machine-hygiene)/scripts/pressure_analysis.py` — Pressure analysis and remediation proposal.
- `$(skill_path machine-hygiene)/scripts/tables.py` — Before/after table rendering.
- `$(skill_path machine-hygiene)/scripts/actions.py` — Approved remediation execution (kill, renice, prune).
- `$(skill_path machine-hygiene)/scripts/evaluate.py` — Main orchestrator; runs Steps 1–6.

## Configuration

Thresholds are configurable via `config.yaml` in the skill directory
(`~/.pi/agent/skills/machine-hygiene/config.yaml`). See the config file
for documented defaults and descriptions.

Each pressure metric has its own block: `cpu_pressure`, `memory_pressure`
and `io_pressure` apply to the `avg10` value of the PSI line named in the
metric table above; `memory_usage` and `swap_usage` are `/proc/meminfo`
percentages; `process_count`, `thread_count`, `runnable_processes` and
`load_average` are core-count multipliers. Threshold provenance, the
`some` vs `full` rationale, the multi-agent calibration, stale-lock
validation and the swap/memory cross-check are documented both above and in
`config.yaml`.

## Exit Codes

- `0` — Success (diagnosis or approved actions completed)
- `1` — General error (see stderr)
- `2` — Invalid arguments
- `3` — Approval declined / no action executed

## Constraints

- **Linux only** — requires `/proc/pressure/` and standard process tools
  (`/proc` filesystem). Non-Linux hosts degrade gracefully with a warning.
- **Process management scope only** — no cgroup or systemd resource-limiting.
- **Read-only by default** — destructive actions gated behind approval.
- **No direct .worklog mutation** — uses `wl` commands for any side effects.

## Safety

- Kill actions include process ancestry verification (parent, session, uid)
  before execution.
- Zombie process detection is informational only — flagged for approval,
  never auto-remediated.
- All actions are logged with timestamps and outcomes.

## Related Work Items

- `WL-0MUJL0PTS009MC8F` — Audit debug log retention
- `WL-0MUJL1NAH0042GOS` — Session close lifecycle

## Final step: standardized end-of-session report

Render the canonical end-of-session report (helper: [`../report/SKILL.md`](../report/SKILL.md)) as the **last step**, replacing any ad-hoc end-of-session summary:

```bash
python3 $(skill_path report)/scripts/render_report.py <work-item-id> \
  --skill-name machine-hygiene \
  --headline "<1-3 sentence headline summary>" \
  --ac "Machine pressure assessed (CPU/memory/disk) | hygiene report | met" \
  --ac "Safe remediations proposed | remediation list | met" \
  --ac "Actions logged and approval requested where needed | wl comment | met" \
  [--producer-actions "<producer actions needed to complete the CURRENT stage; blocking only — omit for 'None needed'>"] \
  [--notes "<freeform context/caveats/assumptions>"] \
  [--next-action <review|plan|implement|...>]
```

> **Note:** ACs record the *skill session's* own deliverables — not the work item's feature
> acceptance criteria (surfaced in the Use Cases section). Passing feature ACs would render
> the session as "Not ready to ship" even though it completed successfully.

The script prints the rendered report to stdout — **paste it verbatim into
your final response**, so the operator sees the report itself (not just the
tool call), then close with: `<work-item-id>: <one-line summary>`. Do NOT
re-summarize the report in a different format — the report is the summary. When the session ends in a terminal state with no open questions for the operator, end your final response with `</end_session>` on its own line as the very last line after the summary; if the session ends with questions for the operator, do not emit the marker.
