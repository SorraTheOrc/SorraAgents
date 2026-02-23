# Example 5: Work In Progress — Delegation Skipped

## Scenario

The AMPA scheduler runs its delegation cycle but finds that another work item is already in progress. The single-concurrency constraint prevents new delegation. The engine skips delegation and reports the current state.

## Initial State

| Work Item | Status | Stage | Assignee |
|---|---|---|---|
| WL-200 | in-progress | delegated | Patch | 
| WL-201 | open | plan_complete | (none) |
| WL-202 | open | intake_complete | (none) |

WL-200 was delegated to Patch and is currently being implemented. WL-201 and WL-202 are valid delegation candidates but cannot be delegated while WL-200 is active.

---

## Flow

### Step 1: Check for In-Progress Items

| Field | Value |
|---|---|
| **Engine Action** | `wl in_progress --json` |
| **Result** | 1 item found: WL-200 |

### Step 2: Pre-Invariant Fails

| Invariant | Check | Result |
|---|---|---|
| `no_in_progress_items` | `count(work_items, status="in_progress") == 0` | **FAIL** — WL-200 is in_progress |

### Step 3: Delegation Skipped

The engine does not proceed to candidate selection. No commands are executed.

| Field | Value |
|---|---|
| **Commands Executed** | None |
| **State Changes** | None |
| **Candidates Evaluated** | None (skipped before candidate fetch) |
| **Discord** | (no notification — silent skip to avoid noise) |

**Engine Inspection Result:**
```json
{
  "status": "in_progress",
  "in_progress_items": [
    {
      "id": "WL-200",
      "title": "Implement user dashboard",
      "status": "in-progress",
      "stage": "delegated",
      "assignee": "opencode-patch-1"
    }
  ]
}
```

**Return Value from `_run_idle_delegation`:**
```
"Delegation: skipped (in_progress items)"
```

---

## Variant: In-Progress Check Fails (Error)

If `wl in_progress --json` fails (e.g., Worklog CLI error):

### Step 1b: First Attempt Fails

| Field | Value |
|---|---|
| **Engine Action** | `wl in_progress --json` |
| **Result** | CLI error (exit code 1) |

### Step 2b: Retry

| Field | Value |
|---|---|
| **Engine Action** | `wl in_progress --json` (retry) |
| **Result** | CLI error again (exit code 1) |

### Step 3b: Abort

| Field | Value |
|---|---|
| **Commands Executed** | None |
| **State Changes** | None |
| **Result** | `{"status": "error"}` — delegation cycle aborted |

The engine will retry on its next scheduled cycle.

---

## Key Observations

1. The single-concurrency constraint is the **first** check in the delegation cycle, before candidate selection. This avoids unnecessary `wl next` calls.
2. Discord notifications are suppressed for the "in_progress" skip case to reduce noise — this is a normal operating condition, not an error.
3. The engine distinguishes between "in_progress items exist" (normal) and "CLI error" (abnormal) — the latter triggers a retry.
4. Candidates WL-201 and WL-202 remain available for the next cycle after WL-200 completes.
