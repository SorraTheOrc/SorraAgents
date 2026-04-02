# Ralph Compaction Plugin

The `ralph` plugin customizes OpenCode compaction to preserve the original
session intent.

## Behavior

- Implements the `experimental.session.compacting` hook.
- Reads the earliest user message text in the current session.
- If an override pattern matches, sets `output.prompt` to a derived instruction.
- If no override matches, appends the original prompt to `output.context`.
- On malformed input or runtime errors, returns safe defaults and does not throw.

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

- Plugin implementation: `.opencode/plugins/ralph.js`
- Tests: `tests/node/test-ralph-plugin.mjs`
