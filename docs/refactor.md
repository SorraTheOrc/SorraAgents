# Refactor Step — Automated Code Smell Detection and Remediation

## Overview

The refactor step is an automated code quality feature that runs after
implementation and before the final commit. It detects code smells in files
modified during the current session and takes appropriate action:

- **Session-introduced smells** (e.g., unused imports, formatting issues) are
  auto-fixed by running linters with --fix on session files before detection.
- **Pre-existing smells** (e.g., complex functions, security issues) that
  cannot be auto-fixed create Worklog work items with structured REFACTOR
  comments in the source files, tracking technical debt without blocking
  feature work.

## Benefits

- **Maintains code quality** automatically without manual intervention.
- **Prevents technical debt accumulation** by detecting issues early.
- **Separates concerns**: new issues are fixed; pre-existing issues are tracked
  for later resolution.
- **Integrates seamlessly** with the implement workflow.
- **Configurable** via flags and config file.

## How It Works

The step follows five phases:

```
Implementation → Refactor → Build → Test → Commit
                       │
                       ├─ Worklog sync (`wl sync --if-idle`, non-fatal)
                       ├─ Session boundary detection
                       ├─ Hybrid smell detection (linters + LLM)
                       ├─ Auto-fix session-introduced smells
                       └─ Create work items for pre-existing smells
```

### 0. Worklog Sync

The step begins by running `wl sync --if-idle` (through the shared
`worklog_sync` helper) so the local store includes remote
changes before any work item is read or created. This prevents duplicate
`REFACTOR` work items. The sync is lock-aware (skips if another sync is in
progress) and non-fatal (a failure logs a warning and the step continues with
local data). Pass `--no-sync` to skip it for offline/deterministic runs.

### 1. Session Boundary Detection

The step identifies which files were modified in the current session by
comparing the current branch against a parent branch (default: `dev`). It uses:

- `git diff --name-status` to find changed files against the merge base.
- `git ls-files --others --exclude-standard` to find untracked files.

Only files modified in the current session are analyzed — pre-existing files
are not re-examined.

Both scan modes (session-boundary and full-project) then apply the same
[scan-scope filter](#scan-scope-external-code-is-ignored) so external code is
never analysed.

### Scan scope: external code is ignored

The refactor step analyses **only code that belongs to the project**. Vendored
code and npm-installed packages committed to a repository are not project
code, so they are dropped before auto-fix and smell detection — the report,
the work items, and the injected `REFACTOR` comments never mention them.

Excluded by default, at any depth:

| Group | Directory names / patterns |
|---|---|
| VCS, build output, environments, worklog state | `.git/`, `__pycache__/`, `build/`, `dist/`, `.venv/`, `venv/`, `.tox/`, `.eggs/`, `*.egg-info/`, `.worklog/` |
| Vendored code and npm-installed packages | `node_modules/`, `vendor/`, `third_party/`, `third-party/`, `site-packages/`, `bower_components/`, `jspm_packages/`, `packages/` |

In addition, **`.gitignore` rules are honoured for tracked files too**: a
vendored file that was committed (`git add -f`) but matches a `.gitignore`
pattern is excluded, including patterns from nested `.gitignore` files. This
runs as one batched `git check-ignore --no-index --stdin -z` call — never one
subprocess per file. When git is unavailable, the directory is not a git
repository, or the command fails, a warning is logged and the remaining
filters still apply (the scan never errors).

Project-owned directories (`skill/`, `command/`, `scripts/`, `pi-client/`,
`tests/`, `src/`, …) always stay in scope.

`packages/` is treated as npm-installed/external by default. A monorepo that
keeps its own source there can re-include it — see
[Path filters](#path-filters-exclude--include).

Filtering is implemented by `skill/refactor/path_filters.py`
(`filter_scannable_paths()`), which is applied once in
`detect_session_files()` before any auto-fix or detection step, so report
counts (`files_analyzed`) and remediation counts are always computed from the
filtered set.

#### Filter precedence

For each candidate file, decisions apply in this order:

1. **`include`** (`.refactor.json`) — an explicit re-include wins over every
   exclusion: the defaults, `exclude`, and `.gitignore`.
2. Source-extension check (full-project scans only: `.py`, `.js`, `.jsx`,
   `.ts`, `.tsx`, `.mjs`, `.cjs`). Session scans keep every touched file so
   non-source changes are still reported.
3. Default external/build directory exclusion (table above).
4. **`exclude`** (`.refactor.json`) — additive to the defaults.
5. **`.gitignore`** filtering via `git check-ignore`.

### 2. Hybrid Smell Detection

Two detection methods are combined:

| Method | What It Finds | Tools |
|--------|---------------|-------|
| **Linter-based** | Mechanical issues (unused imports, formatting, naming) | ruff (Python), eslint (JS/TS) |
| **LLM-based** | Design/architectural smells (god class, feature envy) | Configurable LLM client |

Results from both sources are merged and deduplicated.

### 3. Auto-Fix Session-Introduced Smells

Before running smell detection, the refactor step runs auto-fixable linters
with the --fix flag on session files:

- **ruff check --fix** is run on Python files to auto-fix issues like
  unused imports and formatting problems.
- **eslint --fix** is run on JS/TS files to auto-fix issues like
  unused variables and formatting problems.

After auto-fix, smell detection runs on the cleaned files. Any remaining
smells are non-auto-fixable and are treated as pre-existing (creating work
items). Auto-fixed issues are reported in the refactor output under
`auto_fix.fixed_findings`.

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

When using the implement skill, the refactor step runs
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
| `--no-linter` | Skip linter-based smell detection | False |
| `--no-llm` | Skip LLM-based smell detection | False |
| `--config PATH` | Path to `.refactor.json` config file | None |
| `--parent-branch BRANCH` | Parent branch for session boundary | `dev` |
| `--dry-run` | Show what would be changed without making changes | False |
| `--json` | Output results as JSON | False |
| `--verbose` | Enable verbose logging | False |
| `--no-sync` | Skip the initial `wl sync` (offline/deterministic runs) | False |

## Configuration

### Skipping the Refactor Step

The simplest way to disable the refactor step is passing the `--no-refactor`
flag to the implement skill:

```bash
implement SA-0MPFD4SPC000MXWH --no-refactor
```

This tells the agent to skip invoking the refactor step entirely.
To disable specific detection modes within the refactor step itself,
use the refactor.py CLI flags (`--no-linter`, `--no-llm`).

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
  ],
  "exclude": ["src/generated", "*.min.js"],
  "include": ["packages/my-app-package"]
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
| `exclude` | Extra directories/globs excluded from scans (additive to defaults) | `[]` |
| `include` | Directories/globs re-included in scans (overrides defaults, `exclude`, `.gitignore`) | `[]` |

`.refactor.json` in the project root is discovered automatically; pass
`--config <path>` to use a different file. Existing config files without the
path keys keep working unchanged.

### Path filters (`exclude` / `include`)

Two optional keys narrow or widen the scan scope (see
[Scan scope](#scan-scope-external-code-is-ignored) for the defaults):

| Key | Effect |
|-----|--------|
| `exclude` | Extra directories/globs to exclude. **Additive** to the built-in defaults. |
| `include` | Directories/globs to re-include. **Overrides** the defaults, `exclude`, and `.gitignore` for the matched paths. |

Entries may be directory names (`vendor`), directory paths (`src/legacy`),
whole-path globs (`src/generated/**`, `*.min.js`), or component globs
(`packages`, `*.generated.ts`). Slash-free entries match at any depth.

```json
{
  "exclude": ["src/legacy", "*.min.js"],
  "include": ["packages/my-app-package"]
}
```

A re-include is the escape hatch for a project that deliberately keeps its own
source under a default-excluded directory (e.g. a monorepo whose packages live
in `packages/`).

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

- Use `implement <id> --no-refactor` to skip the step entirely for quick
  iterations.
- Use `--no-llm` to skip LLM-based detection (linter-only mode).
- Disable LLM-based detection in `.refactor.json` (`llm.enabled: false`).
- Limit the number of files analyzed by committing frequently (smaller
  sessions = faster analysis).

### Linter not found

- Install the required linter (e.g., `pip install ruff` for Python).
- The step logs a warning and continues with available linters.
- Check linter availability with `skill/code_review/scripts/linter_runner.py`.

### Duplicate work items created

- The refactor step syncs the worklog first (`wl sync --if-idle`) and checks
  for existing REFACTOR comments before creating work items. If duplicates
  still occur, check that the comment format matches the expected pattern and
  that the sync was not skipped (`--no-sync`) or failed (offline/author gate) —
  a failed sync is non-fatal and logs a warning.
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
  "auto_fix": {
    "fixes_applied": true,
    "fixed_findings": [
      {"file": "src/main.py", "line": 42, "severity": "low",
       "message": "Unused import 'os'", "smell_type": "unused_import",
       "code": "F401"}
    ]
  },
  "smells_detected": [],
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
| Path Filters | `skill/refactor/path_filters.py` | Scan-scope filtering: default external directory exclusions, `.gitignore` (batched `git check-ignore`), config `exclude`/`include` precedence |
| Smell Detection | `skill/refactor/smell_detection.py` | Hybrid linter + LLM smell detection |
| Work Item Creation | `skill/refactor/workitem_creation.py` | Create work items for pre-existing smells |
| Comment Injection | `skill/refactor/comment_injection.py` | Inject REFACTOR comments into source files |
| Config Example | `.refactor.json.example` | Documented example configuration file |

## Related Documentation

- [Implement Skill](../skill/implement/SKILL.md)
- [Refactor Skill](../skill/refactor/SKILL.md)
- [Code Review Skill](../skill/code-review/SKILL.md)
