# Example 2: Audit Failure — Unmet Acceptance Criteria

## Scenario

A work item completes implementation and enters review, but the AMPA audit finds that 2 of 5 acceptance criteria are not met. AMPA documents the gaps and retries delegation. On the second attempt, 1 criterion remains unmet. The flow demonstrates the `audit_fail` and `retry_delegation` commands.

## Initial State

| Field | Value |
|---|---|
| Work Item ID | WL-EXAMPLE-002 |
| Title | Implement rate limiting middleware |
| Status | in_progress |
| Stage | in_review |
| State Alias | `review` |
| Assignee | Patch |

The work item has been delegated and implemented. Patch has pushed a PR and the item is in review.

---

## Flow

### Step 1: AMPA Runs Audit

The engine's triage audit cycle picks up WL-EXAMPLE-002 in `in_review` state.

| Field | Value |
|---|---|
| **Engine Action** | `opencode run "/audit WL-EXAMPLE-002"` |
| **Cooldown Check** | Last audit > 6 hours ago — proceed |

### Step 2: Audit Finds Gaps — `audit_fail` Command

| Field | Value |
|---|---|
| **Command** | `audit_fail` |
| **Actor** | QA (AMPA via audit skill) |
| **State Before** | `in_progress / in_review` (alias: `review`) |
| **State After** | `in_progress / audit_failed` (alias: `audit_failed`) |
| **Pre Invariants** | |

| Invariant | Check | Result |
|---|---|---|
| `requires_audit_result` | Audit comment present | PASS |
| `audit_does_not_recommend_closure` | "Can this item be closed? No" | PASS |

**Effects:** `add_tags: [audit_failed]`

**Audit Output (recorded as comment):**
```
# AMPA Audit Result

- Work item: WL-EXAMPLE-002 — "Implement rate limiting middleware"
- Acceptance Criteria:
  - [x] Rate limiting middleware intercepts requests — Met
  - [x] Configurable rate limits per endpoint — Met
  - [ ] Rate limit headers included in response — Unmet: no X-RateLimit-* headers found
  - [x] 429 status code returned when limit exceeded — Met
  - [ ] Rate limit state persisted across restarts — Unmet: using in-memory store only
- Can this item be closed? No — 2 acceptance criteria are not met.
- What remains: Add rate limit response headers and implement persistent storage.
```

**Engine Action:**
```bash
wl update WL-EXAMPLE-002 --stage audit_failed
wl comment add WL-EXAMPLE-002 --comment "..." --author "ampa-scheduler"
```

### Step 3: Retry — `retry_delegation` Command

The engine decides to retry (first failure, below escalation threshold).

| Field | Value |
|---|---|
| **Command** | `retry_delegation` |
| **Actor** | PM (AMPA scheduler) |
| **State Before** | `in_progress / audit_failed` (alias: `audit_failed`) |
| **State After** | `open / plan_complete` (alias: `plan`) |
| **Effects** | `remove_tags: [audit_failed]` |

This moves the work item back to a delegatable state.

### Step 4: Re-delegation — `delegate` Command

Next scheduler cycle picks up WL-EXAMPLE-002 at stage `plan_complete`.

| Field | Value |
|---|---|
| **Command** | `delegate` |
| **Actor** | PM |
| **State Before** | `open / plan_complete` (alias: `plan`) |
| **State After** | `in_progress / delegated` (alias: `delegated`) |
| **Input: action** | `implement` |
| **Pre Invariants** | All 5 pass |
| **Engine Action** | `opencode run "work on WL-EXAMPLE-002 using the implement skill"` |

Patch reads the audit comment, addresses the gaps:
- Adds X-RateLimit-* response headers
- Implements Redis-backed rate limit storage

### Step 5: Patch Completes and Submits Review

| # | Command | State Before | State After |
|---|---|---|---|
| 5a | `complete_work` | delegated | building |
| 5b | `submit_review` | building | review |

### Step 6: Second Audit — `audit_result` Command (Pass)

| Field | Value |
|---|---|
| **Command** | `audit_result` |
| **Actor** | QA |
| **State Before** | `in_progress / in_review` (alias: `review`) |
| **State After** | `completed / audit_passed` (alias: `audit_passed`) |

**Audit Output:**
```
# AMPA Audit Result

- All 5 acceptance criteria now met
- PR #57 merged
- Can this item be closed? Yes
```

### Step 7: Close and Approve

| # | Command | Actor | State Before | State After |
|---|---|---|---|---|
| 7a | `close_with_audit` | PM | audit_passed | completed/in_review |
| 7b | `approve` | Producer | completed/in_review | shipped |

---

## Final State

| Field | Value |
|---|---|
| Status | closed |
| Stage | done |
| State Alias | `shipped` |
| Tags | `[delegated, implementation_complete, audit_closed]` |

## Key Observations

1. The audit comment provides specific, actionable feedback that Patch can use on retry.
2. The `retry_delegation` command resets the state to `plan_complete`, allowing the normal delegation flow to re-engage.
3. The audit cooldown prevents redundant audits — at least 6 hours between attempts.
4. Tags track the history: `audit_failed` is removed on retry, but the audit comments remain as an audit trail.
