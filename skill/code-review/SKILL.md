---
name: code-review
description:
  Use this skill to review local code. It focuses on correctness, maintainability,
  and adherence to project standards.
---

# Code Reviewer

This skill guides the agent in conducting professional and thorough code reviews for local development.

## Workflow

### 1. Review Target

**Local Changes**: Target staged and unstaged changes in the working tree.

### 2. Preparation

- **Identify Changes**: `git status` to check state; `git diff` and `git diff --staged` to read diffs.

### 3. Analysis

Evaluate changes against:

- **Correctness**: Does the code achieve its purpose without bugs?
- **Maintainability**: Clean, well-structured, easy to modify?
- **Readability**: Well-commented and consistently formatted?
- **Efficiency**: Any performance bottlenecks?
- **Security**: Any vulnerabilities or insecure practices?
- **Edge Cases & Error Handling**: Appropriate handling?
- **Testability**: Adequately covered by tests?

### 4. Feedback

**Summary**: High-level overview.
**Findings**: Critical (bugs, security), Smells (quality/performance), Nitpicks (formatting, optional).

**Tone**: Constructive, professional, friendly. Explain *why* a change is requested.

## Automated Linting (Code Quality)

Provides **automated linting** via canonical Python scripts in `../code-review/scripts/`. Runs as part of an audit or standalone.

### Pipeline

1. **Language Detection** (`detection.py`) — Scans file extensions (`.py`→Python, `.ts`→TypeScript)
2. **Linter Probing** (`detection.py`) — Checks linter availability (ruff, eslint)
3. **Linting** (`linter_runner.py`) — Runs linters, parses JSON output
4. **Severity Classification** (`linter_runner.py`) — Maps to critical/high/medium/low
5. **Work Item Creation** (`create_quality_epics.py`) — Creates/reuses "Quality Improvement" epic

### Scripts

| Script | Purpose |
|--------|---------|
| `code_quality.py` | Orchestrator — runs full pipeline |
| `detection.py` | Language + linter detection |
| `linter_runner.py` | Linter execution + severity |
| `create_quality_epics.py` | Work item creation |

### Usage

```bash
python3 ../code-review/scripts/code_quality.py --path . --json
python3 ../code-review/scripts/create_quality_epics.py --findings '<json>' --dry-run
```

### Linter Prerequisites

| Language | Linter |
|----------|--------|
| Python | [ruff](https://docs.astral.sh/ruff/) |
| TypeScript/JS | [ESLint](https://eslint.org/) |
| Markdown | [markdownlint-cli](https://github.com/igmpaul/markdownlint-cli) |
| Shell | [ShellCheck](https://shellcheck.net/) |
| C# | [dotnet-format](https://github.com/dotnet/format) |

If a linter is unavailable, the corresponding language is skipped gracefully.

### Severity Classification

| Linter | Critical | High | Medium | Low |
|--------|----------|------|--------|-----|
| ruff | F | E, S | W, D, N, UP, ANN… | C, ISC, PIE |
| eslint | — | 2/"error" | 1/"warn" | 0/"off" |
| markdownlint | — | "error" | "warning" | — |
| shellcheck | — | "error" | "warning" | — |
| dotnet-format | — | — | (any) | — |

### Audit Integration

Via `../audit/scripts/audit_runner.py`: Critical/high findings block closure ("Ready to close: No"); medium/low are warnings only. Findings auto-create/reuse a quality epic. If module unavailable, audit continues with warning.

## Policy

- **Prefer canonical scripts** over ad-hoc linting commands.
- **Do NOT** auto-commit or push without explicit approval.

## Worklog context

```bash
wl show SA-0MPYMFZXO0004ZU4 --json
python3 ../code-review/scripts/code_quality.py --path . --json
```

End.
