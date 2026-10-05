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

| Metric | Source | Classification basis |
|---|---|---|
| Load average | `/proc/loadavg` vs. core count | Configurable multiplier |
| CPU pressure | `/proc/pressure/cpu` | Percentage thresholds |
| Memory pressure | `/proc/pressure/memory` | Percentage thresholds |
| I/O pressure | `/proc/pressure/io` | Percentage thresholds |
| Free memory | `/proc/meminfo` | Percentage thresholds |
| Swap usage | `/proc/meminfo` | Percentage thresholds |
| Process count | `/proc` scan | Core-count multiplier |
| Thread count | `/proc` scan | Core-count multiplier |
| Per-command RSS (top 10) | `/proc/[pid]/status` | Absolute RSS bytes |
| Runnable processes | `/proc` state scan | Core-count multiplier |
| Stale devices | Zombie + lock detection | Absolute count |

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
  --skill-name <skill_name> \
  --headline "<1-3 sentence headline summary>" \
  --ac "<AC# description>|<verification metric>|met" \
  --ac "<...>|<...>|unmet" \
  [--producer-actions "<actions for the producer, or omit for 'None needed'>"] \
  [--notes "<freeform context/caveats/assumptions>"] \
  [--next-action <review|plan|implement|...>]
```

The script prints the rendered report to stdout — **paste it verbatim into
your final response**, so the operator sees the report itself (not just the
tool call), then close with: `<work-item-id>: <one-line summary>`. Do NOT
re-summarize the report in a different format — the report is the summary. When the session ends in a terminal state with no open questions for the operator, end your final response with `</end_session>` on its own line as the very last line after the summary; if the session ends with questions for the operator, do not emit the marker.
