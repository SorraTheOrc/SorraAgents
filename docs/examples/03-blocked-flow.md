# Example 3: Blocked Flow — Blocker During Implementation

## Scenario

A work item is delegated to Patch for implementation. During work, Patch encounters a blocking dependency (a required API endpoint from another service is not yet deployed). Patch blocks the item, the blocker is resolved by another team, and work resumes.

## Initial State

| Field | Value |
|---|---|
| Work Item ID | WL-EXAMPLE-003 |
| Title | Integrate payment webhook handler |
| Status | in_progress |
| Stage | delegated |
| State Alias | `delegated` |
| Assignee | Patch |

---

## Flow

### Step 1: Patch Encounters Blocker

During autonomous implementation, Patch discovers that the payment service's webhook endpoint is not yet deployed. Patch cannot complete integration testing.

### Step 2: Execute `block_delegated` Command

| Field | Value |
|---|---|
| **Command** | `block_delegated` |
| **Actor** | Patch |
| **State Before** | `in_progress / delegated` (alias: `delegated`) |
| **State After** | `blocked / delegated` (alias: `blocked_delegated`) |
| **Input: reason** | "Payment service webhook endpoint (POST /webhooks/payment) not deployed. Required for integration testing. See INFRA-456." |

**Engine Action:**
```bash
wl update WL-EXAMPLE-003 --status blocked --stage delegated
wl comment add WL-EXAMPLE-003 --comment "Blocked: Payment service webhook endpoint not deployed. Required for integration testing. See INFRA-456." --author "patch-agent"
```

**Audit Comment Recorded:**
```
Command: block_delegated
Actor: Patch -> opencode-patch-1
Timestamp: 2026-02-19T14:30:00Z
Outcome: success
Reason: Payment service webhook endpoint not deployed
```

### Step 3: AMPA Scheduler Detects Blocked Item

On the next scheduler cycle, AMPA's `_inspect_idle_delegation` finds no in-progress items (the blocked item is in `blocked` status). However, `wl next` may or may not return other candidates.

| Field | Value |
|---|---|
| **Engine Action** | `wl in_progress --json` → empty (blocked items excluded) |
| **Engine Action** | `wl next -n 3 --json` → may return other candidates |
| **Discord** | If no other candidates: `"Agents are idle: 1 item blocked (WL-EXAMPLE-003)"` |

### Step 4: Blocker Resolved

The DevOps team deploys the payment service webhook endpoint. A team member or automated system updates the work item.

### Step 5: Execute `unblock_delegated` Command

| Field | Value |
|---|---|
| **Command** | `unblock_delegated` |
| **Actor** | Patch (or DevOps) |
| **State Before** | `blocked / delegated` (alias: `blocked_delegated`) |
| **State After** | `in_progress / delegated` (alias: `delegated`) |

**Engine Action:**
```bash
wl update WL-EXAMPLE-003 --status in_progress --stage delegated
wl comment add WL-EXAMPLE-003 --comment "Unblocked: Payment service webhook endpoint now deployed. Resuming implementation." --author "patch-agent"
```

### Step 6: AMPA Re-delegates

On the next scheduler cycle, AMPA sees WL-EXAMPLE-003 is in `delegated` state but the opencode run process is no longer active. The engine needs to re-delegate.

| Field | Value |
|---|---|
| **Command** | `delegate` |
| **Actor** | PM |
| **State Before** | `in_progress / delegated` |
| **Input: action** | `implement` (resume) |
| **Engine Action** | `opencode run "work on WL-EXAMPLE-003 using the implement skill"` |

Patch picks up where it left off, reading the work item comments for context about the blocker and its resolution.

### Step 7: Implementation Completes

| # | Command | Actor | State Before | State After |
|---|---|---|---|---|
| 7a | `complete_work` | Patch | delegated | building |
| 7b | `submit_review` | Patch | building | review |
| 7c | `audit_result` | QA | review | audit_passed |
| 7d | `close_with_audit` | PM | audit_passed | completed/in_review |
| 7e | `approve` | Producer | completed/in_review | shipped |

---

## Final State

| Field | Value |
|---|---|
| Status | closed |
| Stage | done |
| State Alias | `shipped` |

## Key Observations

1. The `block_delegated` command preserves the `delegated` stage in the blocked state, distinguishing it from items blocked during manual building.
2. Work item comments provide context continuity — Patch can read the blocker reason and resolution on resumption.
3. The engine does not attempt to delegate new work while an item is blocked (blocked items have `blocked` status, not `in_progress`).
4. The blocker and resolution are recorded as audit trail comments on the work item.
