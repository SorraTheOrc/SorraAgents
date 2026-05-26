# Ralph Compaction Plugin

The `ralph` plugin customizes the agent framework's session compaction to preserve the original
session intent.

## Behavior

- Implements the `experimental.session.compacting` hook.
- Reads the earliest user message text in the current session.
- If an override pattern matches, sets `output.prompt` to a derived instruction.
- If no override matches, appends the original prompt to `output.context`.
- On malformed input or runtime errors, returns safe defaults and does not throw.

## Ralph loop invocation semantics

When Ralph detects a child transition to `in_review`, it invokes `/compact` with no explicit work-item id. The plugin uses session history (earliest user prompt such as `implement SA-123`) to derive the correct context.

Operational guarantees in Ralph:

- `/compact` is best-effort: failures are logged and the loop continues.
- Compaction output is not persisted to worklog comments (logs only).
- Ralph logs cumulative counters as `compact.invocations` and `compact.failures`.

## Default override

Without any configuration, `ralph` includes this override:

- `^implement\s+(\S+)$` -> `audit {1} and address any issues the audit identifies`

Example:

- Original prompt: `implement SA-123`
- Derived compaction prompt starts with:
  `audit SA-123 and address any issues the audit identifies`

## Optional configuration

When loaded with plugin options, configure additional pattern-template rules:

```json
{
  "overrides": [
    {
      "pattern": "^implement (\\S+)$",
      "template": "audit {1} and address any issues the audit identifies"
    }
  ]
}
```

Template placeholders use regex capture groups:

- `{1}` = first capture group
- `{2}` = second capture group

To disable built-in defaults, set:

```json
{
  "disableDefaultOverrides": true
}
```

## File locations

- Plugin implementation: `plugins/ralph.js`
- Tests: `tests/node/test-ralph-plugin.mjs`
