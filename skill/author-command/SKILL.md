---
name: author-command
description: "Authors a brand new command for the agent framework following project best practices and conventions. Trigger on user queries such as: 'Create a new command to <do something>', 'Author a command that <does something>', 'I need a command that <does something>'."
---

# Author Command

## Overview

You are authoring a new command for the agent framework that implements a specific functionality as requested by the user. You will ensure that the command follows project best practices and conventions, and that it is well-documented and tested.

## When To Use

- User requests the creation of a new command (e.g., "Create a new command to <do something>", "Author a command that <does something>", "I need a command that <does something>").

## Behavior

1. Review command authoring documentation at <https://opencode.ai/docs/commands>
2. Review example commands at <https://claude.ai/public/artifacts/e2725e41-cca5-48e5-9c15-6eab92012e75>
3. Gather requirements from the user about the desired command functionality, inputs, outputs, and any specific constraints or considerations.
4. Draft the command code in markdown format, ensuring it adheres to project coding standards and conventions. Use the format in the examples at in <https://claude.ai/public/artifacts/e2725e41-cca5-48e5-9c15-6eab92012e75>
5. Review the command markdown with the user for feedback and make necessary revisions. Do not proceed until the user approves the draft.
6. Once approved, finalize the command markdown and place in the `.opencode/commands` directory
7. Document the command in the README.md file and any other relevant documentation.

## Special placeholders supported by the agent framework

- `$ARGUMENTS` — the full argument string passed to the command.
- `$1`, `$2`, ... — individual positional arguments.
- `!`command`` — runs a shell command and injects its stdout into the prompt. Use sparingly and document side effects.
- `@path/to/file` — includes the contents of a repository file in the prompt.

## Scripts (canonical runner & modules)

This skill does not ship a canonical CLI runner script. The recommended invocation is via Pi prompts or the agent command framework using the provided templates.

- Template: `skill/author-command/assets/command-template.md`

Preferred execution behaviour (policy)

- Agents SHOULD prefer using the repository's canonical scripts when available. For skills that do not provide scripts, use the documented prompt invocation or command framework.
- Do NOT perform ad-hoc commits, pushes, or repository edits without explicit human approval.

Usage examples

- Prompt-based (recommended):

  /skill:author-command "Create a command to format dates for display"

- Worklog context example (use this example Worklog id in documentation):

  wl show SA-0MPYMFZXO0004ZU4 --json

End.
