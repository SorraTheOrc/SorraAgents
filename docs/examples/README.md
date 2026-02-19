# Delegation Flow Examples

This directory contains structured examples showing how the AMPA engine uses the workflow descriptor (`docs/workflow.yaml`) to delegate work items to Patch agents and manage the lifecycle through to completion.

Each example traces a work item through state transitions, showing the workflow commands executed, invariants checked, and external actions triggered at each step.

## Pattern

The AMPA engine uses a **unidirectional delegation** pattern:
1. AMPA selects a work item via `wl next`
2. AMPA delegates with full context — Patch works autonomously
3. AMPA audits on completion and closes or escalates

## Examples

| # | File | Scenario | Key Commands |
|---|---|---|---|
| 1 | [01-happy-path.md](01-happy-path.md) | Full lifecycle from idea to closure | delegate, complete_work, audit_result, close_with_audit, approve |
| 2 | [02-audit-failure.md](02-audit-failure.md) | Audit finds unmet acceptance criteria | audit_fail, retry_delegation, escalate |
| 3 | [03-blocked-flow.md](03-blocked-flow.md) | Blocker during implementation | block, unblock |
| 4 | [04-no-candidates.md](04-no-candidates.md) | No work items available for delegation | (no commands — idle state) |
| 5 | [05-work-in-progress.md](05-work-in-progress.md) | Existing in-progress items prevent delegation | (no commands — precondition failure) |
| 6 | [06-escalation.md](06-escalation.md) | Repeated audit failures trigger escalation to Producer | escalate, de_escalate |

## Structure

Each example follows a consistent format:
- **Scenario** description
- **Initial State** of the work item
- **Step-by-step flow** with state transition tables showing:
  - Step number and description
  - Command executed and actor
  - Pre/post invariants checked
  - State before and after (`status/stage`)
  - Engine actions (wl commands, opencode run, Discord webhooks)
  - Audit comment recorded
- **Final State** summary
