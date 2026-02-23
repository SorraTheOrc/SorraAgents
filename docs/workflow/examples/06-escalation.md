# Example 6: Escalation — Repeated Audit Failures

## Scenario

A work item fails audit twice. After the first failure, AMPA retries delegation automatically. After the second failure, the engine determines the retry threshold has been exceeded and escalates to a Producer for human review. The Producer provides guidance, de-escalates, and the item re-enters the delegation cycle for a third — and this time successful — attempt.

This example demonstrates the `escalate` and `de_escalate` commands and their interaction with the retry/audit loop.

## Initial State

| Field | Value |
|---|---|
| Work Item ID | WL-EXAMPLE-006 |
| Title | Add webhook signature verification |
| Status | in_progress |
| Stage | in_review |
| State Alias | `review` |
| Assignee | Patch |
| Audit Count | 0 |
| Retry Threshold | 2 (after 2 failed audits, escalate) |

The work item was previously delegated and Patch has submitted a PR. The item is now awaiting audit.

---

## Flow

### Step 1: First Audit — Finds Gaps (`audit_fail`)

The engine's triage audit cycle picks up WL-EXAMPLE-006 in `in_review` state.

| Field | Value |
|---|---|
| **Command** | `audit_fail` |
| **Actor** | QA (AMPA via audit skill) |
| **State Before** | `in_progress / in_review` (alias: `review`) |
| **State After** | `in_progress / audit_failed` (alias: `audit_failed`) |

| Invariant | Check | Result |
|---|---|---|
| `requires_audit_result` | Audit comment present | PASS |
| `audit_does_not_recommend_closure` | "Can this item be closed? No" | PASS |

**Effects:** `add_tags: [audit_failed]`

**Audit Output (recorded as comment):**
```
# AMPA Audit Result

- Work item: WL-EXAMPLE-006 — "Add webhook signature verification"
- Acceptance Criteria:
  - [x] Incoming webhooks are validated against HMAC-SHA256 signature — Met
  - [x] Invalid signatures return 401 Unauthorized — Met
  - [ ] Signature key is configurable via environment variable — Unmet: key is hardcoded
  - [ ] Replay attacks prevented with timestamp validation — Unmet: no timestamp check
  - [x] Webhook processing logs include verification result — Met
- Can this item be closed? No — 2 acceptance criteria are not met.
- What remains: Make signature key configurable and add timestamp replay protection.
```

**Engine Actions:**
```bash
wl update WL-EXAMPLE-006 --stage audit_failed
wl comment add WL-EXAMPLE-006 --comment "Audit 1: 2/5 AC unmet..." --author "ampa-scheduler"
```

### Step 2: First Retry (`retry_delegation`)

The engine checks the failure count (1) against the retry threshold (2). First failure — schedule retry.

| Field | Value |
|---|---|
| **Command** | `retry_delegation` |
| **Actor** | PM (AMPA scheduler) |
| **State Before** | `in_progress / audit_failed` (alias: `audit_failed`) |
| **State After** | `open / plan_complete` (alias: `plan`) |
| **Effects** | `remove_tags: [audit_failed]` |

**Engine Actions:**
```bash
wl update WL-EXAMPLE-006 --status open --stage plan_complete
```

### Step 3: Re-delegation (`delegate`)

Next scheduler cycle picks up WL-EXAMPLE-006 at stage `plan_complete`.

| Field | Value |
|---|---|
| **Command** | `delegate` |
| **Actor** | PM (AMPA scheduler) |
| **State Before** | `open / plan_complete` (alias: `plan`) |
| **State After** | `in_progress / delegated` (alias: `delegated`) |
| **Input: action** | `implement` |

| Invariant | Check | Result |
|---|---|---|
| `requires_work_item_context` | `length(description) > 100` | PASS |
| `requires_acceptance_criteria` | AC section present | PASS |
| `requires_stage_for_delegation` | `plan_complete` in allowed stages | PASS |
| `not_do_not_delegate` | No `do-not-delegate` tag | PASS |
| `no_in_progress_items` | No other in-progress items | PASS |

**Engine Actions:**
```bash
wl update WL-EXAMPLE-006 --status in_progress --stage delegated
opencode run "work on WL-EXAMPLE-006 using the implement skill"
```

Patch reads the prior audit comment and attempts fixes:
- Makes the signature key configurable (reads from `WEBHOOK_SECRET` env var)
- Does **not** add timestamp replay protection (misunderstands the requirement)

### Step 4: Patch Completes and Submits Review

| # | Command | Actor | State Before | State After |
|---|---|---|---|---|
| 4a | `complete_work` | Developer (Patch) | `delegated` | `building` |
| 4b | `submit_review` | Developer (Patch) | `building` | `review` |

### Step 5: Second Audit — Still Has Gap (`audit_fail`)

| Field | Value |
|---|---|
| **Command** | `audit_fail` |
| **Actor** | QA (AMPA via audit skill) |
| **State Before** | `in_progress / in_review` (alias: `review`) |
| **State After** | `in_progress / audit_failed` (alias: `audit_failed`) |

| Invariant | Check | Result |
|---|---|---|
| `requires_audit_result` | Audit comment present | PASS |
| `audit_does_not_recommend_closure` | "Can this item be closed? No" | PASS |

**Effects:** `add_tags: [audit_failed]`

**Audit Output (recorded as comment):**
```
# AMPA Audit Result

- Work item: WL-EXAMPLE-006 — "Add webhook signature verification"
- Acceptance Criteria:
  - [x] Incoming webhooks are validated against HMAC-SHA256 signature — Met
  - [x] Invalid signatures return 401 Unauthorized — Met
  - [x] Signature key is configurable via environment variable — Met (reads WEBHOOK_SECRET)
  - [ ] Replay attacks prevented with timestamp validation — Unmet: no timestamp header parsing
  - [x] Webhook processing logs include verification result — Met
- Can this item be closed? No — 1 acceptance criterion is not met.
- What remains: Implement timestamp validation to prevent replay attacks.
```

**Engine Actions:**
```bash
wl update WL-EXAMPLE-006 --stage audit_failed
wl comment add WL-EXAMPLE-006 --comment "Audit 2: 1/5 AC still unmet..." --author "ampa-scheduler"
```

### Step 6: Escalation Triggered (`escalate`)

The engine checks the failure count (2) against the retry threshold (2). Threshold reached — escalate to Producer.

| Field | Value |
|---|---|
| **Command** | `escalate` |
| **Actor** | PM (AMPA scheduler) |
| **State Before** | `in_progress / audit_failed` (alias: `audit_failed`) |
| **State After** | `blocked / escalated` (alias: `escalated`) |

| Invariant | Check | Result |
|---|---|---|
| `audit_does_not_recommend_closure` | "Can this item be closed? No" | PASS |

**Input:**
```
reason: "2 audit failures. Remaining gap: timestamp replay protection not implemented
         despite clear AC. Escalating for Producer guidance."
```

**Effects:**
- `set_assignee: Producer`
- `add_tags: [escalated]`
- Notification: Discord bot notification fires

**Engine Actions:**
```bash
wl update WL-EXAMPLE-006 --status blocked --stage escalated --assignee Producer
wl comment add WL-EXAMPLE-006 --comment "Escalated after 2 audit failures. ..." --author "ampa-scheduler"
# Discord notification sent automatically via effects.notifications
```

**Discord Message:**
```
Escalation: 'Add webhook signature verification' requires producer review —
2 audit failures. Remaining gap: timestamp replay protection not implemented
despite clear AC. Escalating for Producer guidance.
```

### Step 7: Producer Reviews and Provides Guidance

The Producer reviews the work item, reads the audit comments, and determines the issue:
- The acceptance criterion "Replay attacks prevented with timestamp validation" needs a more specific description — Patch did not understand *how* to implement it.

The Producer adds a comment with implementation guidance:

```
# Producer Review

The timestamp validation requirement needs clarification. Implementation should:
1. Parse the X-Webhook-Timestamp header from incoming requests
2. Reject requests where |now - timestamp| > 300 seconds (5 min window)
3. Use a nonce cache (TTL 5 min) to reject duplicate timestamps

Updated AC: "Replay attacks prevented with timestamp validation —
reject requests older than 5 minutes via X-Webhook-Timestamp header
and maintain a nonce cache to prevent duplicate delivery."
```

### Step 8: Producer De-escalates (`de_escalate`)

| Field | Value |
|---|---|
| **Command** | `de_escalate` |
| **Actor** | Producer (human) |
| **State Before** | `blocked / escalated` (alias: `escalated`) |
| **State After** | `open / plan_complete` (alias: `plan`) |

**Effects:** `remove_tags: [escalated]`

**Engine Actions (executed by Producer):**
```bash
wl update WL-EXAMPLE-006 --status open --stage plan_complete
wl comment add WL-EXAMPLE-006 --comment "De-escalated. Added implementation guidance..." --author "producer"
```

### Step 9: Third Delegation (`delegate`)

Next scheduler cycle picks up WL-EXAMPLE-006, now with the Producer's guidance in the comments.

| Field | Value |
|---|---|
| **Command** | `delegate` |
| **Actor** | PM (AMPA scheduler) |
| **State Before** | `open / plan_complete` (alias: `plan`) |
| **State After** | `in_progress / delegated` (alias: `delegated`) |
| **Input: action** | `implement` |

All 5 delegation invariants pass.

Patch reads the Producer's guidance and implements:
- Parses `X-Webhook-Timestamp` header
- Rejects requests where `|now - timestamp| > 300`
- Adds a TTL-based nonce cache

### Step 10: Patch Completes and Submits Review

| # | Command | Actor | State Before | State After |
|---|---|---|---|---|
| 10a | `complete_work` | Developer (Patch) | `delegated` | `building` |
| 10b | `submit_review` | Developer (Patch) | `building` | `review` |

### Step 11: Third Audit — All Criteria Met (`audit_result`)

| Field | Value |
|---|---|
| **Command** | `audit_result` |
| **Actor** | QA (AMPA via audit skill) |
| **State Before** | `in_progress / in_review` (alias: `review`) |
| **State After** | `completed / audit_passed` (alias: `audit_passed`) |

| Invariant | Check | Result |
|---|---|---|
| `requires_audit_result` | Audit comment present | PASS |
| `audit_recommends_closure` | "Can this item be closed? Yes" | PASS |

**Audit Output (recorded as comment):**
```
# AMPA Audit Result

- Work item: WL-EXAMPLE-006 — "Add webhook signature verification"
- Acceptance Criteria:
  - [x] Incoming webhooks are validated against HMAC-SHA256 signature — Met
  - [x] Invalid signatures return 401 Unauthorized — Met
  - [x] Signature key is configurable via environment variable — Met
  - [x] Replay attacks prevented with timestamp validation — Met (5 min window + nonce cache)
  - [x] Webhook processing logs include verification result — Met
- Can this item be closed? Yes — all 5 acceptance criteria are met.
```

### Step 12: Close and Approve

| # | Command | Actor | State Before | State After |
|---|---|---|---|---|
| 12a | `close_with_audit` | PM (AMPA) | `audit_passed` | `completed / in_review` |
| 12b | `approve` | Producer | `completed / in_review` | `shipped` |

---

## Final State

| Field | Value |
|---|---|
| Status | closed |
| Stage | done |
| State Alias | `shipped` |
| Assignee | Producer |
| Tags | `[delegated, implementation_complete, audit_closed]` |

## State Transition Summary

```
review → audit_failed → plan → delegated → review → audit_failed → escalated → plan → delegated → review → audit_passed → completed/in_review → shipped
```

## Key Observations

1. **Escalation threshold**: The engine tracks audit failure count per work item. After the configured threshold (2 in this example), it stops retrying and escalates to a human.
2. **`escalate` vs `retry_delegation`**: Both originate from `audit_failed` state. The engine decides which to execute based on failure count. `escalate` moves to `blocked/escalated`; `retry_delegation` moves back to `open/plan_complete`.
3. **Producer adds value**: The escalation is not just a notification — the Producer provides concrete implementation guidance that resolves the ambiguity that caused repeated failures.
4. **`de_escalate` resets to `plan`**: After Producer review, `de_escalate` moves the item to the same state as `retry_delegation` (`open/plan_complete`), allowing it to re-enter the normal delegation cycle.
5. **Audit trail preserved**: All three audit comments remain on the work item, documenting the progression from 2 unmet criteria → 1 unmet → all met. The escalation reason and Producer guidance are also preserved as comments.
6. **Discord notifications**: The `escalate` command fires a Discord bot notification, ensuring the Producer is notified promptly rather than having to poll the worklog.
7. **No infinite loops**: The escalation threshold guarantees that the engine cannot retry indefinitely. A human must intervene to unblock progress.
