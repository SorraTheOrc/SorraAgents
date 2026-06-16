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

- **Session boundary**: Only files modified in the current implementation
  session are analyzed (determined via git diff against the parent branch).
- **Hybrid detection**: Combines linter-based mechanical checks with LLM-based
  design/architectural analysis.
- **Auto-fix**: Before smell detection, auto-fixable linters (ruff --fix for
  Python, eslint --fix for JS/TS) are run on session files to resolve
  mechanical issues (unused imports, formatting) in-place.
- **Pre-existing smells**: After auto-fix, any remaining non-auto-fixable
  issues are tracked as Worklog work items with structured REFACTOR comments
  in the source code to prevent duplicate work items.

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

## Usage

### Invocation

```bash
# Run refactor on the current session (auto-detects parent branch)
python -m skill.refactor.scripts.refactor

# Run refactor for a specific work item (uses git diff against parent)
python -m skill.refactor.scripts.refactor <work-item-id>

# Disable LLM-based detection (linter only)
python -m skill.refactor.scripts.refactor --no-llm

# Disable linter detection (LLM only)
python -m skill.refactor.scripts.refactor --no-linter

# Dry-run mode: show what would be changed without making changes
python -m skill.refactor.scripts.refactor --dry-run

# Skip linter-based detection (LLM only)
python -m skill.refactor.scripts.refactor --no-linter

# Skip LLM-based detection (linter only)
python -m skill.refactor.scripts.refactor --no-llm

# Get JSON output for programmatic consumption
python -m skill.refactor.scripts.refactor --json
```

### Agent invocation

```bash
/refactor <work-item-id>
```

### Output

The refactor step produces a structured report with:

- **Files analyzed**: List of files modified in the current session
- **Smells detected**: Count and details of each code smell found
- **Smells fixed**: Session-introduced smells that were auto-fixed
- **Work items created**: Pre-existing smells tracked as work items
- **Comments injected**: REFACTOR comments added to source files

## Configuration

### Command-line flags

| Flag | Description |
|------|-------------|
| `--no-llm` | Disable LLM-based detection (linter only) |
| `--no-linter` | Disable linter detection (LLM only) |
| `--dry-run` | Show what would be changed without making changes |
| `--json` | Output results in JSON format |
| `--parent-branch <branch>` | Override the parent branch for diff (default: dev) |
| `--config <path>` | Path to custom `.refactor.json` config file |

### `.refactor.json` Configuration

Create a `.refactor.json` file in the project root to customize behavior:

```json
{
  "linter": {
    "enabled": true,
    "severity_overrides": {}
  },
  "llm": {
    "enabled": true,
    "model": "default",
    "temperature": 0.1,
    "max_tokens": 2000
  },
  "severity_mapping": {
    "critical": "high",
    "high": "high",
    "medium": "medium",
    "low": "low"
  },
  "smell_types": [
    "unused_import", "unused_variable", "unused_function",
    "complex_function", "magic_number", "duplicate_code",
    "long_method", "god_class", "feature_envy",
    "inappropriate_intimacy", "shotgun_surgery"
  ]
}
```

## Smell Types

### Linter-detectable smells

| Code | Type | Description | Severity |
|------|------|-------------|----------|
| F401 | Unused import | Module imported but never used | Critical |
| F841 | Unused variable | Local variable assigned but never used | Critical |
| E302 | Missing blank lines | Expected 2 blank lines after imports | High |
| W292 | No newline at end | No newline at end of file | Medium |
| C901 | Complex function | Function too complex (mccabe) | Low |

### LLM-detectable smells

| Type | Description | Severity |
|------|-------------|----------|
| unused_function | Function defined but never called | Medium |
| magic_number | Numeric literal without named constant | Low |
| complex_function | Function with high cyclomatic complexity | Medium |
| god_class | Class with too many responsibilities | High |
| feature_envy | Method overly interested in another class | Medium |
| shotgun_surgery | Single change requires many file modifications | Medium |
| inappropriate_intimacy | Classes that know too much about each other | Medium |

## REFACTOR Comments

When a pre-existing smell is detected, a structured REFACTOR comment is
injected into the source file to prevent duplicate work items. The comment
format varies by file type:

**Python:**

```python
# <!-- REFACTOR-SA-0MOCK9999
# smell: security
# severity: high
# description: Hardcoded API key detected in source code
# -->
```

**JavaScript/TypeScript:**

```javascript
// <!-- REFACTOR-SA-0MOCK9999
// smell: security
// severity: high
// description: Hardcoded API key detected in source code
// -->
```

**Markdown/HTML:**

```html
<!-- REFACTOR-SA-0MOCK9999
smell: security
severity: high
description: Hardcoded API key detected in source code
-->
```

## Error Handling

- **Missing git repository**: Returns empty session (no files to analyze).
- **No linter installed**: Falls back to LLM-only detection.
- **No LLM client**: Falls back to linter-only detection.
- **File permission errors**: Skips the file and logs a warning.
- **Binary files**: Handled gracefully without crashing.

## Related Skills

- [Implement](../implement/SKILL.md) — The refactor step is integrated
  into the implement workflow.
- [Implement Single](../implement-single/SKILL.md) — Also includes the
  refactor step for single work item implementations.
- [Code Review](../code-review/SKILL.md) — Complementary code quality
  review skill.
