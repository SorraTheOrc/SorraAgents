---
name: author-command
description: "Authors a brand new command for the agent framework following project best practices and conventions. Trigger on user queries such as: 'Create a new command to <do something>', 'Author a command that <does something>', 'I need a command that <does something>'."
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
