---
name: code-review
description:
  Use this skill to review local code. It focuses on correctness, maintainability,
  and adherence to project standards.
---

# Code Reviewer

This skill guides the agent in conducting professional and thorough code reviews for local development.

## Workflow

### 1. Determine Review Target

**Local Changes**: Target the current local file system states (staged and unstaged changes).

### 2. Preparation

1. **Identify Changes**:
    * Check status: `git status`
    * Read diffs: `git diff` (working tree) and/or `git diff --staged` (staged).

### 3. In-Depth Analysis

Analyze the code changes based on the following pillars:

* **Correctness**: Does the code achieve its stated purpose without bugs or logical errors?
* **Maintainability**: Is the code clean, well-structured, and easy to understand and modify in the future? Consider factors like code clarity, modularity, and adherence to established design patterns.
* **Readability**: Is the code well-commented (where necessary) and consistently formatted according to our project's coding style guidelines?
* **Efficiency**: Are there any obvious performance bottlenecks or resource inefficiencies introduced by the changes?
* **Security**: Are there any potential security vulnerabilities or insecure coding practices?
* **Edge Cases and Error Handling**: Does the code appropriately handle edge cases and potential errors?
* **Testability**: Is the new or modified code adequately covered by tests (even if preflight checks pass)? Suggest additional test cases that would improve coverage or robustness.

### 4. Provide Feedback

Structure:
**Summary**: A high-level overview of the review.
**Findings**:

* **Critical**: Bugs, security issues, or breaking changes.
* **Smells**: Suggestions for better code quality or performance.
* **Nitpicks**: Formatting or minor style issues (optional).

#### Tone

* Be constructive, professional, and friendly.
* Explain *why* a change is requested.

## Automated Linting (Code Quality)

In addition to AI-driven analysis, this skill provides **automated linting** through a set of canonical Python scripts in `skill/code_review/`. These scripts detect project languages, probe for available linters, run them, and classify findings by severity.

### Pipeline

The code quality pipeline runs automatically as part of an audit (via `audit_runner.py`) but can also be invoked standalone:

1. **Language Detection** (`detection.py`): Scans the project tree for known file extensions (`.py` → Python, `.ts`/`.tsx` → TypeScript).
2. **Linter Probing** (`detection.py`): Checks if recommended linters (ruff for Python, eslint for TypeScript) are available on `PATH`.
3. **Linting** (`linter_runner.py`): Runs each available linter and parses its JSON output.
4. **Severity Classification** (`linter_runner.py`): Maps raw linter output to standardised severity levels (critical, high, medium, low).
5. **Work Item Creation** (`create_quality_epics.py`): Creates or reuses a "Quality Improvement - Refactoring" epic and adds child tasks for each finding.

### Canonical Scripts

| Script | Purpose |
|--------|---------|
| `skill/code_review/scripts/code_quality.py` | Orchestrator — runs the full pipeline and outputs JSON |
| `skill/code_review/scripts/detection.py` | Language detection and linter probing |
| `skill/code_review/scripts/linter_runner.py` | Linter execution and severity classification |
| `skill/code_review/scripts/create_quality_epics.py` | Work item creation for findings |

### Standalone Usage

Run the full code quality check on a project:

```bash
python3 skill/code_review/scripts/code_quality.py --path /path/to/project --json
```

Filter to specific languages:

```bash
python3 skill/code_review/scripts/code_quality.py --languages python --json
```

Dry-run quality epic creation:

```bash
python3 skill/code_review/scripts/create_quality_epics.py \
    --findings '<json-array>' --dry-run
```

### Linter Prerequisites

| Language | Linter | Requirement |
|----------|--------|-------------|
| Python | [ruff](https://docs.astral.sh/ruff/) | `ruff` on PATH (install via pip) |
| TypeScript | [ESLint](https://eslint.org/) | `eslint` on PATH (install via npm) |
| Markdown | [markdownlint-cli](https://github.com/igmpaul/markdownlint-cli) | `markdownlint` on PATH (install via npm) |
| Shell | [ShellCheck](https://shellcheck.net/) | `shellcheck` on PATH (install via apt/brew) |
| JavaScript/Node | [ESLint](https://eslint.org/) | `eslint` on PATH (install via npm) |
| C# | [dotnet-format](https://github.com/dotnet/format) | `dotnet` on PATH (install via dotnet SDK) |

If a linter is not available, the corresponding language is skipped gracefully with empty findings (no error).

### Severity Classification

| Linter | Raw Severity | Normalised |
|--------|-------------|------------|
| ruff | F (Pyflakes error) | critical |
| ruff | E (pycodestyle error), S (security) | high |
| ruff | W (warning), D (docstring), N (naming), UP, ANN, B, SIM, T20, PL, RUF | medium |
| ruff | C (complexity), ISC, PIE, COM | low |
| eslint | 2 / "error" | high |
| eslint | 1 / "warn" | medium |
| eslint | 0 / "off" | low |
| markdownlint | "error" | high |
| markdownlint | "warning" | medium |
| shellcheck | "error" | high |
| shellcheck | "warning" | medium |
| dotnet-format | (any) | medium |

### Audit Integration

When used via the audit runner (`skill/audit/scripts/audit_runner.py`):

* The code quality check runs **before** acceptance criteria verification.
* **Critical and high** severity findings block closure ("Ready to close: No").
* **Medium and low** findings are reported as warnings but do not block closure.
* Findings automatically create/reuse a "Quality Improvement - Refactoring" epic with child tasks.
* If the `code_quality` module is unavailable, the audit continues with a warning.

## Scripts (canonical runner & modules)

This skill now ships several canonical in-repo CLI scripts (see [Canonical Scripts](#canonical-scripts) above). Agents should prefer these over ad-hoc linting commands.

Preferred execution behaviour (policy)

* Agents SHOULD prefer running the repository's canonical linters and test scripts rather than issuing ad-hoc checks.
* Do NOT make automatic commits or push changes without explicit human approval.

Usage example (worklog context)

* To fetch the work item context before a review:

  wl show SA-0MPYMFZXO0004ZU4 --json

* To run code quality checks programmatically (e.g., from an audit):

  python3 -m skill.code_review.scripts.code_quality --path . --json

End.
