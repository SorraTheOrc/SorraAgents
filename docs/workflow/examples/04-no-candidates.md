# Example 4: No Candidates — Idle State

## Scenario

The AMPA scheduler runs its delegation cycle but `wl next` returns no candidates. All work items are either completed, blocked, or tagged `do-not-delegate`. The engine enters an idle state and notifies via Discord.

## Initial State

No work items are in a delegatable state:

| Work Item | Status | Stage | Tags |
|---|---|---|---|
| WL-100 | closed | done | — |
| WL-101 | closed | done | — |
| WL-102 | blocked | in_progress | — |
| WL-103 | open | idea | do-not-delegate |

---

## Flow

### Step 1: Check for In-Progress Items

| Field | Value |
|---|---|
| **Engine Action** | `wl in_progress --json` |
| **Result** | Empty list — no in-progress items |

Precondition `no_in_progress_items` is satisfied. Engine proceeds to candidate selection.

### Step 2: Fetch Candidates

| Field | Value |
|---|---|
| **Engine Action** | `wl next -n 3 --json` |
| **Result** | Empty list — no candidates |

### Step 3: Idle State

No commands are executed. The engine enters idle state.

| Field | Value |
|---|---|
| **Commands Executed** | None |
| **State Changes** | None |
| **Discord** | `"Agents are idle: no actionable items found"` (channel: `command`) |

**Return Value:**
```json
{
  "note": "Delegation: skipped (no wl next candidates)",
  "dispatched": false,
  "rejected": [],
  "idle_notification_sent": true,
  "delegate_info": null
}
```

---

## Variant: Candidates Exist but All Rejected

If `wl next` returns candidates but all are rejected by pre-invariant checks:

### Step 2b: Fetch Candidates (returns results)

| Field | Value |
|---|---|
| **Engine Action** | `wl next -n 3 --json` |
| **Result** | 2 candidates returned |

### Step 3b: Evaluate Candidates

| Candidate | Stage | Check | Result | Reason |
|---|---|---|---|---|
| WL-103 | idea | `not_do_not_delegate` | FAIL | Tagged `do-not-delegate` |
| WL-104 | in_review | `requires_stage_for_delegation` | FAIL | Stage `in_review` not in allowed list |

### Step 4b: All Candidates Rejected

| Field | Value |
|---|---|
| **Commands Executed** | None |
| **State Changes** | None |
| **Discord** | Detailed rejection report: |

```
Agents are idle: 2 candidates evaluated, 0 eligible

Rejected:
  WL-103 "Fix login page" — do-not-delegate tag
  WL-104 "Update docs" — unsupported stage (in_review)
```

**Return Value:**
```json
{
  "note": "Delegation: skipped (all candidates rejected)",
  "dispatched": false,
  "rejected": [
    {"id": "WL-103", "title": "Fix login page", "reason": "do-not-delegate tag"},
    {"id": "WL-104", "title": "Update docs", "reason": "unsupported stage"}
  ],
  "idle_notification_sent": true,
  "delegate_info": null
}
```

---

## Key Observations

1. No state transitions occur — the engine only reads, never writes, when idle.
2. Discord notifications distinguish between "no candidates at all" and "candidates exist but all rejected."
3. Rejection reasons are specific and actionable (tag name, stage value).
4. The engine will retry on its next scheduled cycle.
