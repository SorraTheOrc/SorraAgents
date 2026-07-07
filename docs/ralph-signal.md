# Ralph Signal & Notification System

Ralph writes a **signal file** and optionally sends a **Discord webhook notification** when major events occur during the loop lifecycle.

## Overview

When Ralph detects a significant event (loop start/complete, phase transition, error, cancellation, max attempts, status change), it:

1. Writes a JSON signal file (default: `.ralph/event.pending`) — always.
2. Optionally sends a Discord embed via webhook — only when `discord.webhook_url` is configured.

Both actions are **fire-and-forget**: errors are logged at WARNING level and never propagated. The Ralph loop never blocks on signal or webhook I/O.

---

## Signal File

### Default Path

```
.ralph/event.pending
```

### Configurable Path

The signal file path can be overridden in `.ralph.json`:

```json
{
  "signal": {
    "file_path": ".ralph/my-events.json"
  }
}
```

### JSON Schema

```json
{
  "event_type": "string (required)",
  "timestamp": "string (ISO8601, required)",
  "work_item_ids": ["string (optional)"]
}
```

Field               | Type            | Description
--------------------|-----------------|-----------------------------------------------
`event_type`        | `string`        | One of the 7 event types (see below).
`timestamp`         | `string`        | ISO8601 UTC timestamp (e.g. `2026-06-05T22:00:00.123456+00:00`).
`work_item_ids`     | `[string]`      | List of related work-item IDs. Empty list `[]` when none provided.

### Event Types

| Enum Member          | Value                | Description                                      |
|----------------------|----------------------|--------------------------------------------------|
| `STATUS_TRANSITION`  | `status_transition`  | Work item status changed (audit pass/fail, etc.) |
| `PHASE_CHANGE`       | `phase_change`       | Ralph loop phase transitioned                    |
| `ERROR`              | `error`              | Ralph encountered an error or stall              |
| `MAX_ATTEMPTS`       | `max_attempts`       | Ralph exhausted maximum loop attempts            |
| `CANCELLED`          | `cancelled`          | Ralph loop was cancelled by operator             |
| `COMPLETED`          | `completed`          | Ralph loop completed successfully                |
| `STARTED`            | `started`            | Ralph loop started                               |

### Behaviour

- **Overwrite, not append**: Each new event replaces the previous signal file contents entirely. The file always contains exactly one JSON object.
- **Fire-and-forget**: I/O errors are logged at WARNING level. The Ralph loop never blocks or crashes on signal file writes.
- **Parent directories**: Created automatically if they don't exist.

### Example Signal File

```json
{
  "event_type": "started",
  "timestamp": "2026-06-05T22:00:00.123456+00:00",
  "work_item_ids": ["SA-001", "SA-002"]
}
```

---

## Discord Webhook Notification

### Configuration

Set `discord.webhook_url` in `.ralph.json`:

```json
{
  "discord": {
    "webhook_url": "https://discord.com/api/webhooks/123456/abcdef"
  }
}
```

When the key is missing, empty, or null, no webhook notification is sent and no errors are logged.

### Embed Payload Format

The webhook POST body follows the [Discord Embed](https://discord.com/developers/docs/resources/message#embed-object) format:

```json
{
  "embeds": [
    {
      "title": "Ralph: My Work Item Title",
      "description": "Ralph loop completed successfully after passing audit",
      "color": 5797046,
      "timestamp": "2026-06-05T22:00:00.123456+00:00",
      "fields": [
        {
          "name": "Event Type",
          "value": "completed",
          "inline": true
        },
        {
          "name": "Work Item IDs",
          "value": "SA-001, SA-002",
          "inline": true
        }
      ]
    }
  ]
}
```

The embed title is constructed as follows:

- When a work-item title is available (resolved from the work item), the title is `"Ralph: <work-item-title>"`.
- When no title can be resolved (e.g., fetch failure or no single work-item ID), it falls back to `"Ralph Event: <event_type>"` (e.g., `"Ralph Event: Completed"`).

```

### Behaviour

- **Fire-and-forget**: HTTP failures (network errors, timeouts, 404s, rate limits) are logged at WARNING level with no retries.
- **No-op when unconfigured**: If `discord.webhook_url` is not set, no HTTP call is made and no errors are logged.
- **Independent pipeline**: Webhook notifications do NOT write to the signal file, and signal file writes do NOT trigger webhooks. Each channel is independent.

---

## RalphRuntime Context Extension

When Ralph is launched via `ralph launch`, the resolved signal file path is stored in the runtime context (`.worklog/ralph/current.json`) so Pi can discover it:

```json
{
  "signal_file_path": ".ralph/event.pending"
}
```

This allows Pi to read the context file and know exactly where to look for the signal file.

---

## Pi Integration Specification

This section defines the required Pi-side behaviour for consuming Ralph's signal file. A separate work item should implement this specification.

### Overview

Pi periodically checks the signal file. When a signal is present, Pi runs `ralph status` and relays the output to the user. After processing, Pi clears the signal to prevent re-triggering.

### File Polling

1. Read `signal_file_path` from Ralph's runtime context (`.worklog/ralph/current.json` → field `signal_file_path`, default `.ralph/event.pending`).
2. Check if the signal file exists.
3. If the file exists, read and parse the JSON payload.
4. Compare the event metadata (event_type + timestamp) with the last processed event to deduplicate.
5. If this is a new event, run `ralph status` and relay the output to the user.
6. After successful processing, **clear the signal file** by either:
   - Deleting it, or
   - Overwriting it with an empty sentinel (e.g., `{}` or a processed marker).

### Deduplication

- Store the last processed event's `event_type` + `timestamp` (or a hash thereof) so that re-reading the same signal file does not trigger duplicate notifications.
- The signal file is overwritten on each new event, so the stored dedup key should be compared against the current file content.

### Invocation

When a new signal is detected:

```
ralph status
```

- Preserve the existing `ralph status` output format.
- Relay the output verbatim to the user.
- Do NOT re-interpret or reformat the status report.

### Sequence Diagram

```
┌──────────┐     ┌──────────────┐     ┌──────────────┐
│  Ralph   │     │  Signal File │     │  Pi (Host)   │
│  Loop    │     │  (.ralph/)   │     │              │
└────┬─────┘     └──────┬───────┘     └──────┬────────┘
     │                  │                    │
     │ Event occurs     │                    │
     │─────────────────>│                    │
     │ write event      │                    │
     │ (overwrite)      │                    │
     │                  │                    │
     │                  │     Poll interval  │
     │                  │<───────────────────│
     │                  │  Check file exists │
     │                  │───────────────────>│
     │                  │   Read + parse     │
     │                  │───────────────────>│
     │                  │                    │
     │                  │   ralph status     │
     │                  │<───────────────────│
     │                  │───log output──────>│
     │                  │                    │
     │                  │  Clear signal      │
     │                  │<───────────────────│
     │                  │  (delete/overwrite)│
     │                  │───────────────────>│
```

### Configuration (Pi-side)

Pi should store:

- The path to Ralph's runtime directory (default `.worklog/ralph/`).
- The last processed event key for deduplication.
- The poll interval (default: every 30 seconds, configurable).

### Error Handling

- If the signal file is not valid JSON, log a warning and delete/ignore it.
- If `ralph status` fails (Ralph process not found, missing context), log a warning and clear the signal file to prevent repeated failures.
- If the runtime context file does not exist, assume no Ralph run is active and skip polling.

### Work Item Description Template

Below is a ready-to-copy work item description for implementing Pi-side integration:

---

**Title**: Pi-Side Signal Consumption and User Notification

**Summary**: Implement periodic polling of Ralph's signal file in Pi to automatically relay `ralph status` output to the user when a significant event occurs.

**Acceptance Criteria**:

1. Pi reads the `signal_file_path` from Ralph's runtime context (`.worklog/ralph/current.json`).
2. Pi checks the signal file at a configurable interval (default: 30 seconds).
3. When a new signal event is detected, Pi runs `ralph status` and relays the output to the user.
4. Pi deduplicates signals by event_type + timestamp to prevent duplicate notifications.
5. Pi clears the signal file after processing.
6. Invalid or missing signal files are handled gracefully (logged and cleared).
7. Failing `ralph status` calls do not crash Pi; errors are logged and the signal is cleared.

**Priority**: High

**Dependencies**: Depends on SA-0MPLQVA45002BOAR (Notify ralph manager when a significant change has occurred)

**Related Files**:

- `docs/ralph-signal.md` — signal file format and integration spec
- `.worklog/ralph/current.json` — runtime context with `signal_file_path` field
- `.ralph/event.pending` — signal file (default path)
