---
name: refactor
description: "Automated code smell detection and remediation. Detects code smells using a hybrid approach (linters + LLM), fixes session-introduced smells immediately, and creates Worklog work items for pre-existing smells with structured REFACTOR comments to prevent duplicates."
---

# Refactor

## Overview

The refactor skill provides automated code smell detection and remediation for
agent-implemented code changes. It runs after implementation completes (but
before the final commit) to identify and address code quality issues.

### Key Concepts

- **Session boundary**: Only files modified in the current session are analyzed (git diff against parent branch)
- **Hybrid detection**: Combines linter-based mechanical checks with LLM-based design/architectural analysis
- **Auto-fix**: Auto-fixable linters (ruff, eslint) resolve mechanical issues in-place before detection
- **Pre-existing smells**: Non-auto-fixable issues become Worklog work items with REFACTOR comments to prevent duplicates

### Architecture

```
refactor/
├── SKILL.md                   # This file
├── __init__.py                # Package init
├── session_boundary.py        # Git diff session boundary detection
├── smell_detection.py         # Hybrid linter + LLM smell detection
├── workitem_creation.py       # Worklog work item creation
├── comment_injection.py       # Structured REFACTOR comment injection
└── scripts/
    ├── __init__.py            # Scripts package init
    └── refactor.py            # Main orchestration script
```

## When To Use

- As a post-implementation quality check in the implement/implement-single
  workflow.
- Manually via `/refactor <work-item-id>` to run code smell analysis on
  session changes.
- Integrated into CI/CD pipelines for automated code quality gates.

## Status Management

When invoked with a work-item-id, this skill manages the work item status during execution to signal that the item is being processed.

1. **Set** the status to `in_progress` at the start of execution (before any other action):
   `wl update <id> --status in_progress --json`
2. **Set** the status to `open` at the end of execution (whether success or failure):
   `wl update <id> --status open --json`

> Stage is NOT modified by this skill. Only `--status` is used. The new convention is:
> `in_progress` at start → `open` at end (no longer restoring the original status).

## Usage

### Invocation

```bash
python -m skill.refactor.scripts.refactor [<work-item-id>] [--no-llm] [--no-linter] [--dry-run] [--json] [--parent-branch <branch>] [--config <path>]
```

Agent invocation: `/refactor <work-item-id>`

### Output

Structured report with: files analyzed, smells detected, smells fixed, work items created, REFACTOR comments injected.

## Configuration

### Command-line flags

| Flag | Description |
|------|-------------|
| `--no-llm` | Disable LLM-based detection (linter only) |
| `--no-linter` | Disable linter detection (LLM only) |
| `--dry-run` | Show what would be changed without making changes |
| `--json` | Output results in JSON format |
| `--parent-branch <branch>` | Override parent branch for diff (default: dev) |
| `--config <path>` | Path to custom `.refactor.json` config file |

### `.refactor.json` Configuration

Example config (project root):
```json
{
  "linter": { "enabled": true, "severity_overrides": {} },
  "llm": { "enabled": true, "model": "default", "temperature": 0.1, "max_tokens": 2000 },
  "severity_mapping": { "critical": "high", "high": "high", "medium": "medium", "low": "low" },
  "smell_types": ["unused_import", "unused_variable", "complex_function", "magic_number",
    "duplicate_code", "long_method", "god_class", "feature_envy", "shotgun_surgery"]
}
```

## Smell Types

| Code / Type | Description | Detection | Severity |
|---|---|---|---|
| F401 | Unused import | Linter | Critical |
| F841 | Unused variable | Linter | Critical |
| E302 | Missing blank lines | Linter | High |
| C901 | Complex function (mccabe) | Linter | Low |
| unused_function | Function defined but never called | LLM | Medium |
| magic_number | Numeric literal without named constant | LLM | Low |
| god_class | Class with too many responsibilities | LLM | High |
| feature_envy | Method overly interested in another class | LLM | Medium |
| shotgun_surgery | Single change requires many file modifications | LLM | Medium |
| inappropriate_intimacy | Classes that know too much about each other | LLM | Medium |

## REFACTOR Comments

When a pre-existing smell is detected, a structured REFACTOR comment is injected:

```
<!-- REFACTOR-SA-0MOCK9999
smell: <type>
severity: <level>
description: <text>
-->
```

Comment delimiters vary by file type — `#` for Python, `//` for JS/TS, `<!-- -->` for HTML/Markdown.

## Error Handling

- **Missing git repo**: Returns empty session (no files to analyze)
- **No linter**: Falls back to LLM-only detection
- **No LLM client**: Falls back to linter-only detection
- **File permission errors**: Skips file, logs warning
- **Binary files**: Handled gracefully

## Related Skills

- [Implement](../implement/SKILL.md) — Integrated refactor step
- [Implement Single](../implement-single/SKILL.md) — Also includes refactor step
- [Code Review](../code-review/SKILL.md) — Complementary code quality skill
