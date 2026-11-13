---
name: refactor
description: "Detect and fix code smells. EXECUTE immediately when no ID (full scan); /skill:refactor <id> for session only. Use when refactor."
---

# Refactor

## ⚠️ Agent instruction: run immediately, never ask

**When invoked without a work-item ID** (`/skill:refactor` with no ID):
→ Run `python3 $(skill_path refactor)/scripts/refactor.py` **immediately**.
→ It performs a full project scan, auto-fixes, detects smells, and files work items.
→ **Do NOT ask the user what to refactor, which files to target, or how to proceed.**
→ Report results when done.

**When invoked with a work-item ID** (`/skill:refactor <id>`):
→ Run `python3 $(skill_path refactor)/scripts/refactor.py <id>` — session-only analysis.

## Overview

The refactor skill provides automated code smell detection and remediation for
agent-implemented code changes. It runs after implementation completes (but
before the final commit) to identify and address code quality issues.

### Key Concepts

- **Full-project scan** (default, no work-item ID): All source files in the
  project are analysed for lint errors and code smells.
- **Session boundary** (with work-item ID): Only files modified in the
  current session are analyzed (git diff against parent branch).
- **Hybrid detection**: Combines linter-based mechanical checks with LLM-based
  design/architectural analysis
- **Auto-fix**: Auto-fixable linters (ruff, eslint) resolve mechanical issues
  in-place before detection
- **Pre-existing smells**: Non-auto-fixable issues become Worklog work items
  with REFACTOR comments to prevent duplicates
- **Sync-first**: `wl sync --if-idle` runs as the first worklog operation (see
  [Worklog sync](#worklog-sync)) so analysis and work-item creation never race
  a stale local store

### Architecture

```
refactor/
├── SKILL.md                   # This file
├── __init__.py                # Package init
├── path_filters.py           # Scan-scope filtering (external paths, .gitignore, exclude/include)
├── session_boundary.py        # Git diff session boundary detection
├── smell_detection.py         # Hybrid linter + LLM smell detection
├── workitem_creation.py       # Worklog work item creation
├── comment_injection.py       # Structured REFACTOR comment injection
└── scripts/
    ├── __init__.py            # Scripts package init
    └── refactor.py            # Main orchestration script
```

## Scan scope — which files are analysed

The refactor skill analyses **only code that belongs to the project**. Both
scan modes (full-project and session-boundary) drop external paths before
auto-fix and detection, so the report, the work items, and the injected
`REFACTOR` comments never mention code the project does not own.

Excluded by default, at **any depth**:

| Group | Directory names / patterns |
|---|---|
| VCS, build output, environments, worklog state | `.git/`, `__pycache__/`, `build/`, `dist/`, `.venv/`, `venv/`, `.tox/`, `.eggs/`, `*.egg-info/`, `.worklog/` |
| Vendored code and npm-installed packages | `node_modules/`, `vendor/`, `third_party/`, `third-party/`, `site-packages/`, `bower_components/`, `jspm_packages/`, `packages/` |

In addition, **`.gitignore` rules are honoured even for tracked files** (a
vendored file that was force-added but is gitignored is still excluded). This
uses one batched `git check-ignore --no-index --stdin -z` call; when git is
unavailable or the directory is not a git repository, a warning is logged and
the remaining filters still apply.

Project-owned directories (`skill/`, `command/`, `scripts/`, `pi-client/`,
`tests/`, `src/`, …) always stay in scope.

`packages/` is treated as npm-installed/external by default. A monorepo that
keeps its own source there can re-include it (see
[Path filters](#path-filters-exclude--include)).

### Precedence

Path decisions are applied in this order for each candidate file:

1. **`include`** (from `.refactor.json`) — an explicit re-include wins over
   every exclusion, including the defaults, `exclude`, and `.gitignore`.
2. Source-extension check (full-project scans only: `.py`, `.js`, `.jsx`,
   `.ts`, `.tsx`, `.mjs`, `.cjs`). Session scans keep every touched file so
   non-source changes are still reported.
3. Default external/build directory exclusion (table above).
4. **`exclude`** (from `.refactor.json`) — additive to the defaults.
5. **`.gitignore`** filtering via `git check-ignore`.

## Worklog sync

Before doing anything else, the script runs `wl sync` (via the shared
`worklog_sync` helper) so the local store includes remote
changes. This prevents duplicate `REFACTOR` work items and stale analysis.

- The sync is **lock-aware**: it passes `--if-idle`, so if another sync is in
  progress the command skips (exit 0) instead of blocking and piling up.
- The sync is **non-fatal**: offline, unborn-HEAD, author-gate, or
  lock-contention failures log a warning and the script continues with local
  data. The script's exit-code semantics are unchanged.
- When no `.worklog` context is resolvable the sync is skipped gracefully.
- Pass `--no-sync` to skip the sync entirely (offline or deterministic runs).

## When To Use

- As a post-implementation quality check in the implement workflow.
- Manually via `/refactor <work-item-id>` to run code smell analysis on
  session changes only.
- Run **without** a work-item ID to do a complete scan of the entire project
  looking for lint errors and refactor opportunities.
- Integrated into CI/CD pipelines for automated code quality gates.

## Verification

Refactoring changes must be verified against the full project test suite
before completion. Run the suite via the
[test skill](../test/SKILL.md) (`/skill:test` — run → triage → evaluate →
loop until green), following the test skill's output contract, so refactor fixes do
not silently break behavior.

## Status Management

The refactor script manages work item status automatically via the shared
`StatusLifecycle` context manager when invoked with a work-item-id.

- **On entry:** Status is set to `in_progress`
- **On success:** Original status is restored (`restore_on_exit=True` — this
  read-only skill never advances the item to `completed`, which wl only
  allows for `in_review`/`done` stages)
- **On error:** Status is restored to its original value

No manual `wl update --status` commands are needed — the script handles
lifecycle transparently. Stage is NOT modified by this skill. The item is
never left in `in_progress` when the script exits.

> In `--dry-run` mode, status management is skipped entirely, since no
> changes are being made and no work item context is expected.

## Invocation

```bash
python -m skill.refactor.scripts.refactor [<work-item-id>] [--no-llm] [--no-linter] [--dry-run] [--json] [--parent-branch <branch>] [--config <path>] [--no-sync]
```

| Invocation | Scan scope | Status management |
|---|---|---|
| `refactor.py` (no args) | **Entire project** — every source file | None |
| `refactor.py <work-item-id>` | Session changes only (git diff) | `StatusLifecycle` |
| `refactor.py --dry-run` | **Entire project** (same as no args) | None |

- If a `<work-item-id>` is provided and `--dry-run` is not set, the script will manage the work item's status using `StatusLifecycle`.
- If no `<work-item-id>` is provided or `--dry-run` is set, the script runs a full-project scan without any work item status interaction.

## Usage

### Invocation

```bash
python -m skill.refactor.scripts.refactor [<work-item-id>] [--no-llm] [--no-linter] [--dry-run] [--json] [--parent-branch <branch>] [--config <path>] [--no-sync]
```

Agent invocation: `/refactor <work-item-id>` (session-only mode)
or `/skill:refactor` with no ID for a full-project scan.

| Invocation | Scan scope | Status management |
|---|---|---|
| `/skill:refactor` (no ID) | **Entire project** — every source file | None |
| `/skill:refactor <work-item-id>` | Session changes only (git diff) | `StatusLifecycle` |

### Output

A human-readable report titled ``=== <project_name> Refactor Report ===``,
where ``<project_name>`` is resolved from ``.worklog/config.yaml``'s
``projectName`` key (falling back to the project directory name).  The report
includes: files analyzed, smells detected, smells fixed, work items created,
REFACTOR comments injected.

The ``--json`` flag outputs a structured JSON dict instead of the human-readable
report.  The ``--dry-run`` flag renders the same ``=== <project_name> Refactor
Report ===`` heading without making any changes.

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
| `--no-sync` | Skip the initial `wl sync` (offline/deterministic runs) |

### `.refactor.json` Configuration

Example config (project root; automatically discovered, or pass `--config`):

```json
{
  "linter": { "enabled": true, "severity_overrides": {} },
  "llm": { "enabled": true, "model": "default", "temperature": 0.1, "max_tokens": 2000 },
  "severity_mapping": { "critical": "high", "high": "high", "medium": "medium", "low": "low" },
  "smell_types": ["unused_import", "unused_variable", "complex_function", "magic_number",
    "duplicate_code", "long_method", "god_class", "feature_envy", "shotgun_surgery"],
  "exclude": ["src/generated", "*.min.js"],
  "include": ["packages/my-app-package"]
}
```

### Path filters (`exclude` / `include`)

Two optional keys control the scan scope (see
[Scan scope](#scan-scope--which-files-are-analysed) for the defaults):

| Key | Effect |
|---|---|
| `exclude` | Extra directories/globs to exclude. **Additive** to the built-in defaults. |
| `include` | Directories/globs to re-include. **Overrides** the defaults, `exclude`, and `.gitignore` for the matched paths. |

Entries may be directory names (`vendor`), directory paths (`src/legacy`),
whole-path globs (`src/generated/**`, `*.min.js`), or component globs
(`packages`, `*.generated.ts`). Slash-free entries match at any depth. Both
keys are optional: a config file without them keeps the default behaviour, so
existing config files need no change.

```json
{
  "exclude": ["src/legacy", "*.min.js"],
  "include": ["packages/my-app-package"]
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
| scene-gym divergence | Game scenes and Gym Scenes implementing different functionality — they must remain functionally equivalent; divergence indicates a bug in one or both implementations. Cross-references the [test-writing-guidelines.md](../shared/test-writing-guidelines.md) anti-patterns "Self-Referential Simulations" and "Duplicates of Existing Core Coverage" | Compare functionality between gym scene tests and their corresponding game scene tests (LLM review) | High |
| inappropriate_intimacy | Classes that know too much about each other | LLM | Medium |

## Finding identity and dedupe

Every work item the refactor skill files for a detected smell carries a
**machine-readable finding identity** so a later scan recognises the same
smell instead of filing a clone.

### Identity definition

The identity of a finding is:

```
(<rule/smell code>, <repository-relative path>)
```

- The **rule/smell code** is the finding's `code` (e.g. `S112`, `F401`), or
  its `smell_type` when no code is present.
- The **path** is always repository-relative, normalised with the shared
  `_repo_relative_path` helper. An absolute path, a `.worklog/worktrees/**`
  prefix, and the line number are **never** part of the identity. The same
  file scanned from an implement worktree and from the main checkout therefore
  has one identity.
- The identity is written as `<code>:<path>` and split on the **first** colon.
  File paths containing a colon are out of scope (matching the audit-side
  `CFP-Chore-Key` precedent).

### Description markers

Newly filed items embed the identity near the top of the description, so
listing truncation cannot hide it:

```
Refactor-Finding-Key: <code>:<repository-relative path>
Refactor-Fingerprint: <sha256>
Refactor-Affected-Lines: <n>, <m>
```

- `Refactor-Finding-Key` is the exact identity the pre-creation guard matches.
- `Refactor-Fingerprint` is the **sha256** of the target file's bytes, a NUL
  separator, and the rule code. It changes when the file content or rule
  changes and is stable otherwise; the path is not hashed, so worktree and
  main-checkout scans agree. Re-verification (SA-0MW7Z1JCE009G6J3) consumes
  this value to decide whether a finding still reproduces.
- `Refactor-Affected-Lines` is the full, deduplicated, sorted list of observed
  lines. It is preserved when duplicates are consolidated so line-level
  reporting survives grouping.

### Duplicate prevention

Before creating an item the skill makes **one** lookup:

```bash
wl list "Refactor-Finding-Key:" --json
```

It parses every marker locally, ignores terminal (`completed`/`deleted`)
items, normalises the paths, and compares the identity **exactly** — there is
no substring matching anywhere. A re-found smell at a new line, from a
different worktree, or with a truncated title still matches its existing item.

When a batch run detects the same identity more than once, the findings are
grouped into a **single** item whose `Refactor-Affected-Lines` marker is the
union of every occurrence.

### Cross-family twin suppression (audit false-positive chores)

The audit skill creator files `ruff <RULE> in <path>:<line> — candidate
false positive` chores for ruff diagnostics it deems false positives. The
refactor producer consults that family too, so the two producers cannot mint
blind twins for the same diagnostic (SA-0MWE1EAR30045VHO):

```bash
wl list "candidate false positive" --json
```

The guard parses each audit chore's title, ignores the trailing line number,
normalises the path, and suppresses creation when the (rule, repository-
relative path) identity matches an open chore. Terminal (`completed`/
`deleted`) audit chores never suppress. The audit chore itself is **never**
closed or mutated — it carries `needsProducerReview` by design; only the
refactor creation is skipped.

In the reverse direction the audit producer consults open `Refactor-Finding-
Key` items (one lookup per run) before minting a chore and embeds a
`Refactor-Twin: <refactor-item-id>` cross-reference in the chore description
when the identity matches — the chore is still created (a producer decision
is required), but the twin is visible and linked.

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
- [Code Review](../code-review/SKILL.md) — Complementary code quality skill


## Final step: standardized end-of-session report

Render the canonical end-of-session report (helper: [`../report/SKILL.md`](../report/SKILL.md)) as the **last step**, replacing any ad-hoc end-of-session summary:

```bash
python3 $(skill_path report)/scripts/render_report.py <work-item-id> \
  --skill-name refactor \
  --headline "<1-3 sentence headline summary>" \
  --ac "Code smells detected across changed scope | smell report | met" \
  --ac "Refactoring plan drafted and reviewed | plan note | met" \
  --ac "Changes applied and verified | diffs/tests | met" \
  [--producer-actions "<producer actions needed to complete the CURRENT stage; blocking only — omit for 'None needed'>"] \
  [--notes "<freeform context/caveats/assumptions>"] \
  [--next-action <review|plan|implement|...>]
```

> **Note:** ACs record the *skill session's* own deliverables — not the work item's feature
> acceptance criteria (surfaced in the Use Cases section). Passing feature ACs would render
> the session as "Not ready to ship" even though it completed successfully.

The script prints the rendered report to stdout — **paste it verbatim into
your final response**, so the operator sees the report itself (not just the
tool call), then close with: `<work-item-id>: <one-line summary>`. Do NOT
re-summarize the report in a different format — the report is the summary. When the session ends in a terminal state with no open questions for the operator, end your final response with `</end_session>` on its own line as the very last line after the summary; if the session ends with questions for the operator, do not emit the marker.
