# Example 1: Happy Path — Idea to Closure

## Scenario

A new work item progresses through the complete lifecycle: idea capture, intake, planning, delegation to Patch, autonomous implementation, successful audit, and Producer approval. This is the ideal flow with no errors or blockers.

## Initial State

| Field | Value |
|---|---|
| Work Item ID | WL-EXAMPLE-001 |
| Title | Add user preference export API |
| Status | open |
| Stage | idea |
| State Alias | `idea` |
| Description | "Add an API endpoint to export user preferences as JSON" |
| Assignee | (none) |

---

## Flow

### Step 1: AMPA Selects Candidate

The AMPA scheduler runs its delegation cycle and queries for available work.

| Field | Value |
|---|---|
| Engine Action | `wl next -n 3 --json` |
| Result | Candidate returned: WL-EXAMPLE-001, stage=idea |
| Discord | (none yet — inspection phase) |

### Step 2: Pre-Invariant Check for Delegation

The engine evaluates the `delegate` command's pre invariants against WL-EXAMPLE-001.

| Invariant | Check | Result |
|---|---|---|
| `requires_work_item_context` | `length(description) > 100` | PASS (description has sufficient context) |
| `requires_acceptance_criteria` | `regex(description, "(?i)(acceptance criteria\|\\- \\[[ x]\\])")` | PASS (AC section present) |
| `requires_stage_for_delegation` | `stage in ["idea", "intake_complete", "plan_complete"]` | PASS (stage=idea) |
| `not_do_not_delegate` | `"do-not-delegate" not in tags` | PASS (no blocking tags) |
| `no_in_progress_items` | `count(work_items, status="in_progress") == 0` | PASS (no WIP items) |

All pre invariants pass. Command execution proceeds.

### Step 3: Execute `delegate` Command (Intake)

| Field | Value |
|---|---|
| **Command** | `delegate` |
| **Actor** | PM (AMPA scheduler) |
| **State Before** | `open / idea` (alias: `idea`) |
| **State After** | `in_progress / delegated` (alias: `delegated`) |
| **Input: action** | `intake` (determined by stage=idea) |
| **Input: work_item_id** | WL-EXAMPLE-001 |
| **Engine Action** | `opencode run "/intake WL-EXAMPLE-001 do not ask questions"` |
| **Effects** | `set_assignee: Patch`, `add_tags: [delegated]` |
| **Discord** | `"Delegating 'intake' task for 'Add user preference export API' (WL-EXAMPLE-001)"` |

**Audit Comment Recorded:**
```
Command: delegate (intake)
Actor: PM -> ampa-scheduler
Timestamp: 2026-02-19T10:00:00Z
Outcome: success
Agent: opencode-patch-1
Model: claude-opus-4
Prompt ref: prompts/delegate.md
```

**Engine Action:**
```bash
wl update WL-EXAMPLE-001 --status in_progress --stage delegated
wl comment add WL-EXAMPLE-001 --comment "..." --author "ampa-scheduler"
```

Patch completes intake autonomously. Work item description is expanded with motivation, user impact, and requirements. Patch updates the work item stage to `intake_complete`.

### Step 4: Next Delegation Cycle — Plan

AMPA's next scheduler cycle finds WL-EXAMPLE-001 at stage `intake_complete`.

| Field | Value |
|---|---|
| **Command** | `delegate` |
| **Actor** | PM (AMPA scheduler) |
| **State Before** | `open / intake_complete` (alias: `intake`) |
| **State After** | `in_progress / delegated` (alias: `delegated`) |
| **Input: action** | `plan` |
| **Engine Action** | `opencode run "/plan WL-EXAMPLE-001"` |
| **Discord** | `"Delegating 'plan' task for 'Add user preference export API' (WL-EXAMPLE-001)"` |

Patch decomposes the work into sub-tasks with acceptance criteria and updates stage to `plan_complete`.

### Step 5: Next Delegation Cycle — Implement

AMPA's next scheduler cycle finds WL-EXAMPLE-001 at stage `plan_complete`.

| Field | Value |
|---|---|
| **Command** | `delegate` |
| **Actor** | PM (AMPA scheduler) |
| **State Before** | `open / plan_complete` (alias: `plan`) |
| **State After** | `in_progress / delegated` (alias: `delegated`) |
| **Input: action** | `implement` |
| **Engine Action** | `opencode run "work on WL-EXAMPLE-001 using the implement skill"` |
| **Discord** | `"Delegating 'implement' task for 'Add user preference export API' (WL-EXAMPLE-001)"` |

### Step 6: Patch Implements Autonomously

Patch works without any back-and-forth:
- Writes implementation code
- Writes unit and integration tests
- Creates documentation
- Creates a branch and pushes commits
- Creates a pull request

### Step 7: Execute `complete_work` Command

| Field | Value |
|---|---|
| **Command** | `complete_work` |
| **Actor** | Patch |
| **State Before** | `in_progress / delegated` (alias: `delegated`) |
| **State After** | `in_progress / in_progress` (alias: `building`) |
| **Effects** | `remove_tags: [delegated]`, `add_tags: [implementation_complete]` |

### Step 8: Execute `submit_review` Command

| Field | Value |
|---|---|
| **Command** | `submit_review` |
| **Actor** | Patch |
| **State Before** | `in_progress / in_progress` (alias: `building`) |
| **State After** | `in_progress / in_review` (alias: `review`) |
| **Pre Invariant** | `requires_tests` — PASS (test plan link present) |

### Step 9: AMPA Runs Audit — `audit_result` Command

| Field | Value |
|---|---|
| **Command** | `audit_result` |
| **Actor** | QA (AMPA via audit skill) |
| **State Before** | `in_progress / in_review` (alias: `review`) |
| **State After** | `completed / audit_passed` (alias: `audit_passed`) |
| **Pre Invariant** | `requires_audit_result` — PASS (audit comment present) |
| **Engine Action** | `opencode run "/audit WL-EXAMPLE-001"` |

**Audit Output:**
```
AMPA Audit Result

- Work item: WL-EXAMPLE-001 — "Add user preference export API"
- All acceptance criteria met
- PR #42 merged
- Can this item be closed? Yes
```

### Step 10: Execute `close_with_audit` Command

| Field | Value |
|---|---|
| **Command** | `close_with_audit` |
| **Actor** | PM (AMPA scheduler) |
| **State Before** | `completed / audit_passed` (alias: `audit_passed`) |
| **State After** | `completed / in_review` |
| **Pre Invariant** | `audit_recommends_closure` — PASS |
| **Effects** | `set_needs_producer_review: true`, `add_tags: [audit_closed]` |
| **Discord** | `"Audit Completed -- 'Add user preference export API' ready for producer review"` |

**Engine Action:**
```bash
wl update WL-EXAMPLE-001 --status completed --stage in_review
```

### Step 11: Producer Approves — `approve` Command

| Field | Value |
|---|---|
| **Command** | `approve` |
| **Actor** | Producer (human) |
| **State Before** | `completed / in_review` |
| **State After** | `closed / done` (alias: `shipped`) |
| **Post Invariant** | `requires_approvals` — PASS ("Approved by Producer") |
| **Effects** | `set_needs_producer_review: false` |

---

## Final State

| Field | Value |
|---|---|
| Status | closed |
| Stage | done |
| State Alias | `shipped` |
| Assignee | (cleared) |
| Tags | `[intake, planned, delegated, implementation_complete, audit_closed]` |
| needs_producer_review | false |

## Commands Executed (in order)

| # | Command | Actor | From | To |
|---|---|---|---|---|
| 1 | delegate (intake) | PM | idea | delegated |
| 2 | delegate (plan) | PM | intake | delegated |
| 3 | delegate (implement) | PM | plan | delegated |
| 4 | complete_work | Patch | delegated | building |
| 5 | submit_review | Patch | building | review |
| 6 | audit_result | QA | review | audit_passed |
| 7 | close_with_audit | PM | audit_passed | completed/in_review |
| 8 | approve | Producer | completed/in_review | shipped |
