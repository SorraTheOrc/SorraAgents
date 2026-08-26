---
name: author-command
description: "Author a new agent-framework command per project conventions. Use when asked: 'Create a new command to <do>'."
---

# Author Command

## Overview

Author a new command for the agent framework following project best practices and conventions.

## When To Use

User requests a new command ("Create a command to <do something>", "I need a command that...").

## Behavior

1. Review [command authoring docs](https://docs.pi.ai/commands) and [examples](https://claude.ai/public/artifacts/e2725e41-cca5-48e5-9c15-6eab92012e75)
2. Gather requirements: functionality, inputs, outputs, constraints
3. Draft command in markdown following example format
4. Review with user, revise until approved (do not proceed without approval)
5. Place final command in `./command/` directory
6. Document in README.md
7. **Verify the new command against the full project test suite** via the [test skill](../test/SKILL.md) (`/skill:test` — run → triage → evaluate → loop until green) before declaring completion, so the authored command does not break existing behaviour.

## Framework placeholders

- `$ARGUMENTS` — full argument string
- `$1`, `$2`... — positional arguments
- `!command` — inject command stdout into prompt (use sparingly)
- `@path/to/file` — include file contents in prompt

## Scripts

No CLI runner script. Use Pi prompt invocation or agent command framework.

- Template: `./assets/command-template.md`

### Policy

- Prefer canonical scripts where available
- No ad-hoc commits/pushes without explicit approval

### Examples

```
/skill:author-command "Create a command to format dates for display"
wl show SA-0MPYMFZXO0004ZU4 --json
```

End.


## Final step: standardized end-of-session report

Render the canonical end-of-session report (helper: [`../report/SKILL.md`](../report/SKILL.md)) as the **last step**, replacing any ad-hoc end-of-session summary:

```bash
python3 $(skill_path report)/scripts/render_report.py <work-item-id> \
  --skill-name <skill_name> \
  --headline "<1-3 sentence headline summary>" \
  --ac "<AC# description>|<verification metric>|met" \
  --ac "<...>|<...>|unmet" \
  [--producer-actions "<actions for the producer, or omit for 'None needed'>"] \
  [--notes "<freeform context/caveats/assumptions>"] \
  [--next-action <review|plan|implement|...>]
```

The script prints the rendered report to stdout — **paste it verbatim into
your final response**, so the operator sees the report itself (not just the
tool call), then close with: `<work-item-id>: <one-line summary>`. Do NOT
re-summarize the report in a different format — the report is the summary.
