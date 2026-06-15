# Refactor Step — Automated Code Smell Detection and Remediation

## Overview

The refactor step is an automated code quality feature that runs after
implementation and before the final commit. It detects code smells in files
modified during the current session and takes appropriate action:

- **Session-introduced smells** (e.g., unused imports, formatting issues) are
  fixed immediately.
- **Pre-existing smells** (e.g., complex functions, security issues) create
  Worklog work items with structured REFACTOR comments in the source files,
  tracking technical debt without blocking feature work.

## Benefits

- **Maintains code quality** automatically without manual intervention.
- **Prevents technical debt accumulation** by detecting issues early.
- **Separates concerns**: new issues are fixed; pre-existing issues are tracked
  for later resolution.
- **Integrates seamlessly** with the implement and implement-single workflows.
- **Configurable** via flags and config file.

## How It Works

The refactor step follows four phases:

```
Implementation → Refactor → Build → Test → Commit
                       │
                       ├─ Session boundary detection
                       ├─ Hybrid smell detection (linters + LLM)
                       ├─ Auto-fix session-introduced smells
                       └─ Create work items for pre-existing smells
```

### 1. Session Boundary Detection

The step identifies which files were modified in the current session by
comparing the current branch against a parent branch (default: `dev`). It uses:

- `git diff --name-status` to find changed files against the merge base.
- `git ls-files --others --exclude-standard` to find untracked files.

Only files modified in the current session are analyzed — pre-existing files
are not re-examined.

### 2. Hybrid Smell Detection

Two detection methods are combined:

| Method | What It Finds | Tools |
|--------|---------------|-------|
| **Linter-based** | Mechanical issues (unused imports, formatting, naming) | ruff (Python), eslint (JS/TS) |
| **LLM-based** | Design/architectural smells (god class, feature envy) | Configurable LLM client |

Results from both sources are merged and deduplicated.

### 3. Auto-Fix Session-Introduced Smells

Smells introduced in the current session that are low or medium severity and
match known auto-fixable patterns are fixed automatically:

- Unused imports
- Unused variables (low/medium severity)
- Formatting issues
- Naming issues

### 4. Work Item Creation for Pre-existing Smells

For smells that existed before the current session, the refactor step:

1. Checks for existing REFACTOR comments in the file to prevent duplicates.
2. Creates a Worklog work item with appropriate priority.
3. Injects a structured REFACTOR comment into the source file:

```python
# <!-- REFACTOR-SA-0MOCKXXXXX
# smell: security
# description: Hardcoded password found in configuration
# -->
```

This comment serves as a persistent marker so that the same smell is not
reported again in future refactor runs.

## Usage

### Via Implement Skills

When using the implement or implement-single skill, the refactor step runs
automatically after implementation:

```bash
# Refactor runs automatically after implementation
implement SA-0MPFD4SPC000MXWH

# Skip the refactor step
implement SA-0MPFD4SPC000MXWH --no-refactor
```

### Via Direct Invocation

The refactor can be invoked directly as a skill:

```
/skill:refactor <work-item-id>
```

Or via the canonical orchestration script:

```bash
# Human-readable output
python3 skill/refactor/scripts/refactor.py

# JSON output for agent consumption
python3 skill/refactor/scripts/refactor.py --json

# With verbose logging
python3 skill/refactor/scripts/refactor.py --verbose
```

### CLI Options

| Option | Description | Default |
|--------|-------------|---------|
| `work_item_id` | Optional work item ID for context | None |
| `--no-refactor` | Skip the refactor step entirely | False |
| `--config PATH` | Path to `.refactor.json` config file | None |
| `--parent-branch BRANCH` | Parent branch for session boundary | `dev` |
| `--json` | Output results as JSON | False |
| `--verbose` | Enable verbose logging | False |

## Configuration

### `--no-refactor` Flag

The simplest way to disable the refactor step is the `--no-refactor` flag:

```bash
implement SA-0MPFD4SPC000MXWH --no-refactor
```

### `.refactor.json` Config File

For fine-grained control, create a `.refactor.json` file in the project root.
An example file is provided at `.refactor.json.example`.

```json
{
  "enabled": true,
  "linter": {
    "enabled": true,
    "severity_overrides": {
      "F841": "low"
    }
  },
  "llm": {
    "enabled": true,
    "model": "default",
    "temperature": 0.1,
    "max_tokens": 2000
  },
  "severity_mapping": {
    "critical": { "priority": "critical", "color": "red" },
    "high": { "priority": "high", "color": "orange" },
    "medium": { "priority": "medium", "color": "yellow" },
    "low": { "priority": "low", "color": "green" }
  },
  "smell_types": [
    "unused_import",
    "unused_variable",
    "complex_function",
    "magic_number",
    "duplicate_code",
    "long_method",
    "god_class",
    "feature_envy"
  ]
}
```

| Key | Description | Default |
|-----|-------------|---------|
| `enabled` | Master switch for the refactor step | `true` |
| `linter.enabled` | Enable linter-based detection | `true` |
| `linter.severity_overrides` | Override severity for specific linter codes | `{}` |
| `llm.enabled` | Enable LLM-based detection | `true` |
| `llm.model` | LLM model name | `"default"` |
| `llm.temperature` | LLM sampling temperature | `0.1` |
| `llm.max_tokens` | Maximum tokens per LLM request | `2000` |
| `severity_mapping` | Map severity to priority/color | See above |
| `smell_types` | List of supported smell types | 11 types |

## Smell Types

The following code smells are currently detected:

| Smell Type | Source | Severity | Description |
|------------|--------|----------|-------------|
| `unused_import` | Linter | Low | Imported module not used |
| `unused_variable` | Linter | Low | Variable assigned but never used |
| `unused_function` | Linter | Medium | Function defined but never called |
| `complex_function` | Linter/LLM | Medium | Function with high cyclomatic complexity |
| `magic_number` | Linter | Low | Hardcoded numeric literal |
| `duplicate_code` | LLM | Medium | Similar code blocks found |
| `long_method` | LLM | Medium | Method exceeds recommended length |
| `god_class` | LLM | High | Class has too many responsibilities |
| `feature_envy` | LLM | Medium | Method uses more features of another class |
| `inappropriate_intimacy` | LLM | High | Class depends on internal details of another |
| `shotgun_surgery` | LLM | Medium | Single change requires many file modifications |

## Troubleshooting

### Refactor step takes too long

- Use `--no-refactor` to skip the step entirely for quick iterations.
- Disable LLM-based detection in `.refactor.json` (`llm.enabled: false`).
- Limit the number of files analyzed by committing frequently (smaller
  sessions = faster analysis).

### Linter not found

- Install the required linter (e.g., `pip install ruff` for Python).
- The step logs a warning and continues with available linters.
- Check linter availability with `skill/code_review/scripts/linter_runner.py`.

### Duplicate work items created

- The refactor step checks for existing REFACTOR comments before creating
  work items. If duplicates occur, check that the comment format matches
  the expected pattern.
- Manually close duplicate work items with `wl close <id> --reason "duplicate"`.

### LLM analysis is not running

- Ensure the LLM client is configured and has an `analyze()` method.
- LLM-based detection is optional — linter-based detection runs regardless.
- Set `llm.enabled: true` in `.refactor.json` if it was explicitly disabled.

## Examples

### Basic Usage

```bash
# Run refactor on current session
python3 skill/refactor/scripts/refactor.py --json
```

Output:

```json
{
  "session_files": [
    {"status": "M", "file": "src/main.py"},
    {"status": "A", "file": "tests/test_main.py"}
  ],
  "findings": [
    {"file": "src/main.py", "line": 42, "severity": "low",
     "message": "Unused import 'os'", "source": "linter",
     "smell_type": "unused_import", "code": "F401"}
  ],
  "auto_fixed": [
    {"file": "src/main.py", "line": 42, "severity": "low",
     "message": "Unused import 'os'", "smell_type": "unused_import"}
  ],
  "work_items_created": [],
  "comments_injected": [],
  "errors": [],
  "skipped": false
}
```

### Skip Refactor

```bash
# Disable refactor for a quick iteration
implement SA-0MPFD4SPC000MXWH --no-refactor
```

### Custom Configuration

```bash
# Use a custom config file
python3 skill/refactor/scripts/refactor.py --config .my-refactor.json
```

## Module Reference

| Module | Path | Purpose |
|--------|------|---------|
| Orchestrator | `skill/refactor/scripts/refactor.py` | Main entry point and workflow orchestration |
| Config | `skill/refactor/scripts/config.py` | Configuration loading (`RefactorConfig`) |
| Session Boundary | `skill/refactor/session_boundary.py` | Detect files changed in current session |
| Smell Detection | `skill/refactor/smell_detection.py` | Hybrid linter + LLM smell detection |
| Work Item Creation | `skill/refactor/workitem_creation.py` | Create work items for pre-existing smells |
| Comment Injection | `skill/refactor/comment_injection.py` | Inject REFACTOR comments into source files |
| Config Example | `.refactor.json.example` | Documented example configuration file |

## Related Documentation

- [Implement Skill](../skill/implement/SKILL.md)
- [Implement-Single Skill](../skill/implement-single/SKILL.md)
- [Refactor Skill](../skill/refactor/SKILL.md)
- [Code Review Skill](../skill/code-review/SKILL.md)
