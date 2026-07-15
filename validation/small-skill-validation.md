# Small Skill File Refactoring — Baseline Validation Report

**Work Item:** SA-0MRGV9AZN004J6SQ — Validate small skill file refactoring  
**Parent Epic:** SA-0MRGTMMBB003H1QT — Refactor AGENTS.md, commands, and skill files for conciseness and to remove duplication  
**Date:** 2026-07-15  
**Scope:** 13 smaller skill SKILL.md files (total ~81.5KB)

---

## AC 1: Per-file Baseline Measurements

| # | File | Lines | Bytes (KB) | % of Total (13 files) |
|---|------|-------|-----------|----------------------|
| 1 | `skill/cleanup/SKILL.md` | 178 | 9.3 | 11.4% |
| 2 | `skill/intakeall/SKILL.md` | 185 | 8.8 | 10.8% |
| 3 | `skill/resolve-pr-comments/SKILL.md` | 308 | 7.5 | 9.2% |
| 4 | `skill/refactor/SKILL.md` | 227 | 7.3 | 9.0% |
| 5 | `skill/effort-and-risk/SKILL.md` | 141 | 6.9 | 8.5% |
| 6 | `skill/find-related/SKILL.md` | 162 | 6.7 | 8.2% |
| 7 | `skill/git-management/SKILL.md` | 157 | 6.5 | 8.0% |
| 8 | `skill/code-review/SKILL.md` | 154 | 6.5 | 8.0% |
| 9 | `skill/implementall/SKILL.md` | 165 | 6.5 | 8.0% |
| 10 | `skill/planall/SKILL.md` | 141 | 4.8 | 5.9% |
| 11 | `skill/triage/SKILL.md` | 102 | 3.6 | 4.4% |
| 12 | `skill/author-command/SKILL.md` | 54 | 2.7 | 3.3% |
| 13 | `skill/owner-inference/SKILL.md` | 59 | 2.0 | 2.5% |
| | **Total (13 files)** | **2,133** | **81.5 KB** | **100%** |

### Reference Files for Overlap Analysis

| File | Lines | Bytes (KB) | Purpose |
|------|-------|-----------|---------|
| `AGENTS.md` | 312 | 16.2 | Project-wide policies & workflow steps |
| `command/intake.md` | 200 | 12.0 | Command counterpart for intakeall skill |
| `command/review.md` | 147 | 7.8 | Command counterpart for code-review skill |
| `command/refactor.md` | 57 | 2.4 | Command counterpart for refactor skill |
| `command/doc.md` | 132 | 6.5 | Command counterpart (no direct skill match) |

> **Note:** `command/plan.md` (26.3KB) and `command/author_skill.md` (16KB) were previously removed as part of other refactoring tasks under this epic. `skill/planall/SKILL.md` and `skill/author-command/SKILL.md` no longer have direct command file counterparts.

---

## AC 2: Content Overlap with AGENTS.md or Command Files

### 2.1 Overlap Pairs Summary

The following small skill files have **direct command file counterparts**:

| Skill File | Command Counterpart | Overlap Severity |
|---|---|---|
| `skill/intakeall/SKILL.md` | `command/intake.md` | **Low** — Different purposes (batch intake vs. interactive intake) |
| `skill/refactor/SKILL.md` | `command/refactor.md` | **Low** — Command file is minimal (57 lines); skill is detailed (227 lines) |
| `skill/code-review/SKILL.md` | `command/review.md` | **Low** — Different purposes (local code review vs. PR review in Ampa) |
| `skill/author-command/SKILL.md` | ~~`command/author_skill.md`~~ (removed) | N/A — No command counterpart remains |

The remaining 9 skills have **no direct command file counterpart**:
- `skill/cleanup/SKILL.md`, `skill/resolve-pr-comments/SKILL.md`, `skill/effort-and-risk/SKILL.md`, `skill/find-related/SKILL.md`, `skill/git-management/SKILL.md`, `skill/implementall/SKILL.md`, `skill/planall/SKILL.md`, `skill/triage/SKILL.md`, `skill/owner-inference/SKILL.md`

### 2.2 `skill/intakeall/SKILL.md` ↔ `command/intake.md` Overlap

| Section | skill/intakeall/SKILL.md | command/intake.md | Overlap |
|---|---|---|---|
| Purpose | Batch intake for `idea`-stage items | Interactive one-by-one intake interview | **Low** — fundamentally different use cases |
| Workflow steps | 6-item numbered behavior list | 12-step interview-driven process | **Low** — different workflows |
| Status lifecycle | `wl update <id> --status in_progress --json` | Same claim pattern | **Full** — identical status claim command |
| CLI flags | `--json`, `--dry-run`, `--parent-id`, `--max`, `--item-timeout` | No equivalent batch flags | **None** |
| Output format | Summary report with totals, per-item outcomes | Structured intake brief document | **Low** — different output |

**Assessment:** Minimal overlap beyond the standard `wl update <id> --status in_progress` claim pattern shared across all skills. `intakeall/SKILL.md` is a batch automation skill with no overlap in behavior or process flow with `command/intake.md`.

### 2.3 `skill/refactor/SKILL.md` ↔ `command/refactor.md` Overlap

| Section | skill/refactor/SKILL.md | command/refactor.md | Overlap |
|---|---|---|---|
| Scope | Automated post-implementation code smell detection & fix | Interactive refactoring discovery session | **Low** — different workflows |
| Status lifecycle | `wl update <id> --status in_progress` (start) → `open` (end) | No status management | **Low** |
| Output | Structured report + work items + REFACTOR comments | Worklog items only | **Medium** — both create work items, but via different mechanisms |
| Smell types | Both reference code smells (long methods, duplication, etc.) | Similar smell categories | **Medium** — conceptual overlap but different treatment |

**Assessment:** Low-to-medium overlap. The two files serve complementary but distinct purposes: `refactor/SKILL.md` is an automated post-implement checker, while `command/refactor.md` is an interactive discovery session. The smell type lists have conceptual overlap but are presented differently.

### 2.4 `skill/code-review/SKILL.md` ↔ `command/review.md` Overlap

| Section | skill/code-review/SKILL.md | command/review.md | Overlap |
|---|---|---|---|
| Purpose | Local code review for correctness, maintainability | Automated PR-focused review in Ampa pool container | **Low** — different contexts |
| Workflow | 4-step local review (determine target → prepare → analyze → provide feedback) | 11-step Ampa container workflow | **None** — completely different processes |
| Tooling | `git status`, `git diff`, manual reading | `gh pr`, `wl ampa`, `distrobox` | **None** |
| Linting | Both reference automated linting / code quality | Both reference code quality pipeline | **Medium** — code-review/SKILL.md has detailed linting pipeline section; command/review.md mentions audit runner integration |

**Assessment:** Low-to-medium overlap. The two files have completely different workflows and targets. The linting/code quality pipeline documentation in `code-review/SKILL.md` (lines 60-130) is unique content not found in `command/review.md`.

### 2.5 `skill/author-command/SKILL.md` Overlap

`command/author_skill.md` (16KB) was previously removed as part of dedicated epic work items. No command counterpart remains. `skill/author-command/SKILL.md` (54 lines, 2.7KB) is self-contained with no overlap to evaluate.

### 2.6 AGENTS.md Workflow Overlap (All 13 Small Skills)

The small skill files are largely **self-contained operational skills** with minimal overlap with AGENTS.md workflow instructions. Key observations:

| Skill File | AGENTS.md Overlap | Details |
|---|---|---|
| `skill/cleanup/SKILL.md` | **Minimal** | Unique git branch cleanup workflow. No AGENTS.md duplication. |
| `skill/intakeall/SKILL.md` | **Minimal** | Batch automation, unique behavior details. No AGENTS.md duplication. |
| `skill/resolve-pr-comments/SKILL.md` | **Minimal** | GitHub PR workflow, unique content. No AGENTS.md duplication. |
| `skill/refactor/SKILL.md` | **Minimal** | Code smell detection pipeline. `wl update <id> --status in_progress` claim pattern only. |
| `skill/effort-and-risk/SKILL.md` | **Minimal** | Estimation workflow. `wl update <id> --status in_progress` claim pattern. |
| `skill/find-related/SKILL.md` | **Minimal** | Search + report workflow. `wl update <id> --status in_progress` claim pattern. |
| `skill/git-management/SKILL.md` | **Low** | References AGENTS.md as one of several policy sources (line 81). Otherwise unique. |
| `skill/code-review/SKILL.md` | **Minimal** | Unique code review workflow. No AGENTS.md duplication. |
| `skill/implementall/SKILL.md` | **Low** | Batch implementation skill, references implement/SKILL.md. Minimal AGENTS.md overlap. |
| `skill/planall/SKILL.md` | **Low** | Batch planning skill. References `/plan` invocation pattern (similar to AGENTS.md plan workflow). |
| `skill/triage/SKILL.md` | **Minimal** | Self-contained test-failure triage workflow. No significant AGENTS.md overlap. |
| `skill/author-command/SKILL.md` | **Minimal** | Brief command authoring guide. No AGENTS.md duplication. |
| `skill/owner-inference/SKILL.md` | **Minimal** | Brief deterministic heuristic file. No AGENTS.md duplication. |

**Key finding:** Unlike the large skill files (implement, plan, ship), the small skill files have **very low overlap** with AGENTS.md. The only duplicated pattern across multiple skills is the standard `wl update <id> --status in_progress --json` claim command, which is a universal pattern.

### 2.7 Batch Automation Skill Family Overlap (implementall, intakeall, planall)

The three batch automation skills (`implementall`, `intakeall`, `planall`) share a common structural pattern:

| Feature | implementall | intakeall | planall |
|---|---|---|---|
| Behavior numbered list | Yes (6 items) | Yes (6 items) | Yes (5 items) |
| Command invocation table | Yes | Yes | Yes |
| Summary report format | Same structure | Same structure | Same structure |
| JSON output format | Same schema | Same schema | Same schema |
| Producer-input detection | Same mechanism | Same mechanism | Same mechanism |
| Error handling & recovery | Same patterns | Same patterns | Same patterns |
| Signal handling | Yes | Yes | Yes |
| `--max` flag section | Yes | Yes (CLI flags) | Yes (CLI flags) |
| Idempotence section | Yes | Yes | Yes |
| CLI flags table | Yes | Yes | Yes |
| Examples | 8 examples | 8 examples | 6 examples |
| Scripts section | Yes | Yes | Yes |
| Related skills | 4 refs | 4 refs | 2 refs |

**Assessment:** The three batch automation skills share ~70% structural overlap in their documentation format. They follow the same template pattern (behavior → invocation → output → error handling → examples → scripts → related skills). This is the single largest deduplication opportunity among the small skill files — the common pattern could be consolidated into a shared reference document or template.

Notable duplication details:
- All three have nearly identical "Producer-input detection" sections
- All three have nearly identical "Error handling and recovery" sections with same patterns
- All three have nearly identical "Signal handling" sections (SIGINT/SIGTERM)
- All three have nearly identical "Idempotence" sections
- Summary report and JSON output formats follow the same schema with different outcome labels

---

## AC 3: Cross-References Enumerated

### 3.1 Per-File Cross-Reference Summary

| # | Skill File | Cross-References | Type |
|---|-----------|-----------------|------|
| 1 | `skill/cleanup/SKILL.md` | 0 file refs, 14 script refs | All self-contained (local `./scripts/`) |
| 2 | `skill/intakeall/SKILL.md` | 1 command ref, 2 skill refs, script refs | `command/intake.md`, `../planall/SKILL.md`, `../ralph/SKILL.md` |
| 3 | `skill/resolve-pr-comments/SKILL.md` | 0 cross-refs | Self-contained |
| 4 | `skill/refactor/SKILL.md` | 3 skill refs | `../implement/SKILL.md`, `../implement-single/SKILL.md`, `../code-review/SKILL.md` |
| 5 | `skill/effort-and-risk/SKILL.md` | 0 file refs, script refs | Self-contained (local `./scripts/`) |
| 6 | `skill/find-related/SKILL.md` | 0 file refs, script refs | Self-contained (local `./scripts/`) |
| 7 | `skill/git-management/SKILL.md` | 1 AGENTS.md, 2 script refs, wiki link | `../../AGENTS.md`, `../ship/scripts/git-helpers.js`, `../ship/scripts/ship.js`, `[[concepts/...]]` |
| 8 | `skill/code-review/SKILL.md` | 3 script refs | `../code-review/scripts/*` (self-referencing) |
| 9 | `skill/implementall/SKILL.md` | 3 skill refs | `../implement/SKILL.md`, `../planall/SKILL.md`, `../intakeall/SKILL.md`, `../ralph/SKILL.md` |
| 10 | `skill/planall/SKILL.md` | 2 skill refs | `skill/plan/SKILL.md`, `../ralph/SKILL.md` |
| 11 | `skill/triage/SKILL.md` | 1 skill ref, script refs | `../owner-inference/SKILL.md` |
| 12 | `skill/author-command/SKILL.md` | 0 cross-refs | Self-contained |
| 13 | `skill/owner-inference/SKILL.md` | 1 skill ref, 1 doc ref | `../triage/SKILL.md`, `../triage/resources/runbook-test-failure.md` |

### 3.2 Detailed Cross-References

#### `skill/intakeall/SKILL.md`
| Reference | Type | Location |
|---|---|---|
| `command/intake.md` | Command file ref | Related skills section |
| `../planall/SKILL.md` | Skill ref | Related skills section |
| `../ralph/SKILL.md` | Skill ref | Related skills section |
| `./scripts/intakeall.py` | Script | CLI invocation, examples |

#### `skill/refactor/SKILL.md`
| Reference | Type | Location |
|---|---|---|
| `../implement/SKILL.md` | Skill ref | Related skills section |
| `../implement-single/SKILL.md` | Skill ref | Related skills section |
| `../code-review/SKILL.md` | Skill ref | Related skills section |

#### `skill/git-management/SKILL.md`
| Reference | Type | Location |
|---|---|---|
| `[[concepts/git-worktree-best-practices-for-agent-workflows]]` | Wiki link | Actions table, safety constraints |
| `[AGENTS.md](../../AGENTS.md)` | AGENTS.md ref | Safety constraints |
| `../ship/scripts/git-helpers.js` | Script ref | Branch naming/policy |
| `../ship/scripts/ship.js` | Script ref | Push validation |

#### `skill/implementall/SKILL.md`
| Reference | Type | Location |
|---|---|---|
| `../implement/SKILL.md` | Skill ref | Related skills section |
| `../planall/SKILL.md` | Skill ref | Related skills section |
| `../intakeall/SKILL.md` | Skill ref | Related skills section |
| `../ralph/SKILL.md` | Skill ref | Related skills section |
| `./scripts/implementall.py` | Script | CLI invocation, examples |
| `./scripts/failure_notice.py` | Script | Error handling section |
| `./tests/test_implementall.py` | Test file | Scripts section |

#### `skill/planall/SKILL.md`
| Reference | Type | Location |
|---|---|---|
| `skill/plan/SKILL.md` | Skill ref | Related skills section |
| `../ralph/SKILL.md` | Skill ref | Related skills section |
| `./scripts/planall.py` | Script | CLI invocation, examples |

#### `skill/triage/SKILL.md`
| Reference | Type | Location |
|---|---|---|
| `../owner-inference/SKILL.md` | Skill ref | References section |
| `./scripts/check_or_create.py` | Script | Scripts section |
| `./resources/test-failure-template.md` | Template | References section |
| `./resources/runbook-test-failure.md` | Runbook | References section |

#### `skill/owner-inference/SKILL.md`
| Reference | Type | Location |
|---|---|---|
| `../triage/SKILL.md` | Skill ref | References section |
| `../triage/resources/runbook-test-failure.md` | Doc ref | References section |
| `./scripts/infer_owner.py` | Script | Scripts section |

### 3.3 Cross-Reference Summary Table

| Skill File | Total Refs | To AGENTS.md | To Command Files | To Other Skills | To Scripts/Docs | Wiki Links |
|---|---|---|---|---|---|---|
| cleanup/SKILL.md | 14 | 0 | 0 | 0 | 14 | 0 |
| intakeall/SKILL.md | 5 | 0 | 1 | 2 | 2 | 0 |
| resolve-pr-comments/SKILL.md | 0 | 0 | 0 | 0 | 0 | 0 |
| refactor/SKILL.md | 3 | 0 | 0 | 3 | 0 | 0 |
| effort-and-risk/SKILL.md | 6 | 0 | 0 | 0 | 6 | 0 |
| find-related/SKILL.md | 4 | 0 | 0 | 0 | 4 | 0 |
| git-management/SKILL.md | 4 | 1 | 0 | 0 | 2 | 1 |
| code-review/SKILL.md | 7 | 0 | 0 | 0 | 7 | 0 |
| implementall/SKILL.md | 7 | 0 | 0 | 4 | 3 | 0 |
| planall/SKILL.md | 4 | 0 | 0 | 2 | 2 | 0 |
| triage/SKILL.md | 5 | 0 | 0 | 1 | 4 | 0 |
| author-command/SKILL.md | 0 | 0 | 0 | 0 | 0 | 0 |
| owner-inference/SKILL.md | 3 | 0 | 0 | 1 | 2 | 0 |
| **Total** | **62** | **1** | **1** | **13** | **46** | **1** |

> **Notable:** Only `git-management/SKILL.md` references AGENTS.md. Most cross-references are to local `./scripts/` (50+). The batch automation skills (implementall, intakeall, planall) account for most inter-skill references. `resolve-pr-comments/SKILL.md` and `author-command/SKILL.md` have zero cross-references.

---

## AC 4: Baseline Test Suite Results

Test runner: vitest (v4.1.10)  
Command: `npx vitest run`

### Test Results

```
Test Files  1 failed (1)
     Tests  no tests
```

### Analysis

The project has **one test file**: `plugins/tests/ralph-compaction.test.js`. This file uses a runtime `assert`/`export default` pattern rather than vitest's `test()`/`describe()` framework, so vitest reports "No test suite found" and marks it as failed.

**Direct execution with `node` confirms the test passes:**

```
node plugins/tests/ralph-compaction.test.js
→ "Ralph compaction test: OK"
```

### Verdict

- **The underlying test logic passes** when run directly with Node.js.
- Vitest reports the file as failed because it uses a non-standard test format (exported `run()` function + manual `assert` + manual exit code handling).
- **This is the same pre-existing issue** documented in the large skill file validation report (SA-0MRGV9AZC006HNZL).
- **Baseline status: Pre-existing test runner incompatibility.** The `ralph-compaction.test.js` file needs to be converted to use vitest's `test()`/`assert` or `expect()` API to integrate properly with the test runner.

### Other Test Directories (currently excluded from vitest discovery)

The following directories contain test files but are not configured in vitest's `include` pattern:
- `command/tests/`
- `skill/ralph/tests/`
- `skill/implementall/tests/`
- `skill/intakeall/tests/`
- `skill/planall/tests/`
- `skill/plan/tests/`
- `tests/`

These are Python test directories excluded from the vitest-based JS test runner.

---

## Summary of Findings

### Overlap Severity Matrix

| Skill File | AGENTS.md Overlap | Command File Overlap | Cross-File Dedup Priority | Notes |
|---|---|---|---|---|
| cleanup/SKILL.md | Minimal | No command file | **Low** | Unique content, self-contained |
| intakeall/SKILL.md | Minimal | Low (with intake.md) | **Low** | Different purpose from intake.md |
| resolve-pr-comments/SKILL.md | Minimal | No command file | **Low** | Self-contained, GitHub workflow |
| refactor/SKILL.md | Minimal | Low (with refactor.md) | **Low** | Different workflow from refactor.md |
| effort-and-risk/SKILL.md | Minimal | No command file | **Low** | Self-contained estimation skill |
| find-related/SKILL.md | Minimal | No command file | **Low** | Self-contained search skill |
| git-management/SKILL.md | Low | No command file | **Low** | Orchestrates existing scripts |
| code-review/SKILL.md | Minimal | Low (with review.md) | **Low** | Different workflows |
| implementall/SKILL.md | Low | No command file | **Medium** — see batch family note |
| planall/SKILL.md | Low | No command file | **Medium** — see batch family note |
| triage/SKILL.md | Minimal | No command file | **Low** | Self-contained, brief |
| author-command/SKILL.md | Minimal | No counterpart | **Low** | Very brief (2.7KB) |
| owner-inference/SKILL.md | Minimal | No command file | **Low** | Very brief (2.0KB) |

### Key Insights

1. **The small skill files have very low overlap** with AGENTS.md or command files — unlike the large skill files (implement, plan, ship), these are operational skills with unique, self-contained content.

2. **The batch automation trio (implementall, intakeall, planall) represent the biggest deduplication opportunity** — They share ~70% structural overlap in documentation format (behavior list, invocation, output format, error handling, signal handling, idempotence, examples). Consider extracting the common pattern into a shared reference, or using a template approach.

3. **50+ local script references** mean most of the "heaviness" in these files is functional content (script documentation), not duplicated boilerplate. Conciseness edits should focus on trimming verbose examples and tightening prose, not cross-file deduplication.

4. **`resolve-pr-comments/SKILL.md` is the largest file** (308 lines, 7.5KB) with zero cross-references and unique GitHub workflow content. It's a candidate for targeted conciseness editing (verbose workflow steps, inline command examples).

5. **`refactor/SKILL.md` has significant inline content** (227 lines, 7.3KB) that could be tightened — the smell type tables and configuration sections are verbose.

6. **`git-management/SKILL.md` is the only file** that references AGENTS.md among the 13 small skills.

7. **All cross-references currently resolve to valid targets.** No broken links were detected.

### Total Estimated Repeat Content

~5-10% of the combined 81.5KB across these 13 files is duplicated between files (primarily the batch automation family structural overlap). This represents approximately 4-8 KB of potentially deduplicatable content — significantly less than the large skill files.

### Pre-existing Test Issue

The `ralph-compaction.test.js` test format incompatibility with vitest is documented in the large skill file validation. Same pre-existing issue — no change.
