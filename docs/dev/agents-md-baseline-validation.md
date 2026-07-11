# AGENTS.md Baseline Validation

> **Work Item:** Validate AGENTS.md refactoring (SA-0MRGV90RP0088XM4)
> **Date:** 2026-07-11
> **Commit:** Current HEAD on `dev` at `c29eddf` (SA-0MRGKZ3QY003498O: Clarify CHANGELOG.md is managed by ship skill, not implementing agents)

## 1. Baseline File Measurements

| Metric | Value |
|--------|-------|
| File size | **32,445 bytes** |
| Line count | **464 lines** |

## 2. Section Structure

### H2 Sections (Top-Level)

| # | Line | Section | Description |
|---|------|---------|-------------|
| 1 | 15 | `## Workflow for AI Agents` | Step-by-step workflow for completing tasks |
| 2 | 138 | `## work-item Tracking with Worklog (wl)` | Work-item tracking guidance |
| 3 | 142 | `## CRITICAL RULES` | Critical operational rules |
| 4 | 162 | `## Important Rules` | Important operational rules |
| 5 | 178 | `## Stage vs Status distinction` | Lifecycle management |
| 6 | 195 | `## work-item Types` | Issue type taxonomy |
| 7 | 205 | `## Work Item Descriptions` | Description format guidance |
| 8 | 220 | `## Priorities` | Priority definitions |
| 9 | 229 | `## Dependencies` | Dependency management |
| 10 | 242 | `## Workflow management` | Stage/assignee/tags guidance |
| 11 | 252 | `## Test-failure triage policy` | Test failure triage process |
| 12 | 281 | `## Work-Item Management` | wl command reference (bash examples) |
| 13 | 319 | `## Project Status` | wl command reference for status queries |
| 14 | 386 | `## Coding Disciplines` | Five coding principles |

### H3 Sub-Sections

| # | Line | Section | Parent |
|---|------|---------|--------|
| 1 | 366 | `### Team` | Project Status |
| 2 | 377 | `### Plugins` | Project Status |
| 3 | 381 | `### Help` | Project Status |
| 4 | 392 | `### 1. Think Before Coding` | Coding Disciplines |
| 5 | 403 | `### 2. Simplicity First` | Coding Disciplines |
| 6 | 417 | `### 3. Surgical Changes` | Coding Disciplines |
| 7 | 435 | `### 4. Repository Boundaries` | Coding Disciplines |
| 8 | 444 | `### 5. Goal-Driven Execution` | Coding Disciplines |

### Workflow Steps (under ## Workflow for AI Agents)

The workflow contains **8 numbered steps** (1-8):

1. **Claim the work-item** (line 16)
2. **Ensure the work-item is clearly defined** (line 19)
3. **Plan the work** (line 33)
4. **Decide what to work on next** (line 43)
5. **Implement the work-item** (line 53)
6. **Update the operator** (line 108)
7. **Repeat** (line 115)
8. **End session** (line 118)

### Policy/Rule Blocks

| # | Section | Type | Description |
|---|---------|------|-------------|
| 1 | Lines 1-10 | Core Principles | Bullet-list principles for AI agents |
| 2 | Lines 143-160 | CRITICAL RULES | 13 critical rules (numbered implicit) |
| 3 | Lines 163-176 | Important Rules | 14 important rules (numbered implicit) |
| 4 | Lines 178-193 | Stage vs Status | Two-axis lifecycle documentation |
| 5 | Lines 195-203 | work-item Types | Type definitions |
| 6 | Lines 205-218 | Work Item Descriptions | Description requirements |
| 7 | Lines 220-227 | Priorities | Priority definitions |
| 8 | Lines 229-240 | Dependencies | Dependency guidelines |
| 9 | Lines 242-250 | Workflow management | Stage/assignee/tags guidance |

### Coding Disciplines (5 principles each with sub-instructions)

1. **Think Before Coding** - 4 bullet points
2. **Simplicity First** - 6 bullet points + test
3. **Surgical Changes** - 6 bullet points + test
4. **Repository Boundaries** - 4 bullet points
5. **Goal-Driven Execution** - Table + brief plan example

## 3. Cross-Reference Verification

### Markdown Links (file references)

| # | Link Text | Target | Status | Notes |
|---|-----------|--------|--------|-------|
| 1 | `skills/ship/SKILL.md` | `skills/ship/SKILL.md` (line 70) | ❌ **BROKEN** | Path should be `skill/ship/SKILL.md` (singular "skill") |
| 2 | `skills/ship/SKILL.md` | `skills/ship/SKILL.md` (line 111) | ❌ **BROKEN** | Same broken path as #1 |
| 3 | `docs/dev/release-process.md` | `docs/dev/release-process.md` (line 112) | ✅ Valid | File exists |

### External URLs

| # | URL | Status |
|---|-----|--------|
| 1 | https://github.com/multica-ai/andrej-karpathy-skills | ✅ External |
| 2 | https://x.com/karpathy/status/2015883857489522876 | ✅ External |

### Wiki-style References ([[...]])

| # | Reference | Line | Notes |
|---|-----------|------|-------|
| 1 | `[[concepts/git-worktree-best-practices-for-agent-workflows]]` | 58 | Not a file link — wiki-style cross-ref |
| 2 | `[[concepts/git-worktree-best-practices-for-agent-workflows]]` | 133 | Not a file link — wiki-style cross-ref |

### Plain-Text File References (not markdown links)

| # | Reference | Line | File Exists? | Notes |
|---|-----------|------|-------------|-------|
| 1 | `scripts/release/merge-dev-to-main.sh` | 71 | ❌ **Not found** | Actual path: `skill/ship/scripts/release/merge-dev-to-main.sh` |
| 2 | `scripts/release/merge-dev-to-main.sh` | 107 | ❌ **Not found** | Same as above |

### Summary

| Metric | Count |
|--------|-------|
| Broken internal links | 3 (2 × `skills/ship/SKILL.md`, 1 × `scripts/release/merge-dev-to-main.sh`) |
| Valid internal links | 1 (`docs/dev/release-process.md`) |
| External URLs | 2 |
| Wiki-style references | 2 |

## 4. Baseline Test Suite Results

| Metric | Value |
|--------|-------|
| **Total tests** | **1,777 passed** |
| Skipped | 2 |
| Expected failures (xfailed) | 1 |
| Duration | 40.22s |
| Command used | `python3 -m pytest --tb=short --junitxml=test-results-full.xml` |
| Test environment | Python 3.14.4, pytest 9.1.1 |

## 5. Validation Checklist

- [x] **AC1**: Validation checklist created documenting all policy and rule content sections present in AGENTS.md
  - 14 H2 sections, 8 H3 sub-sections, 8 workflow steps, 9 policy/rule blocks, 5 coding disciplines documented above
- [x] **AC2**: Baseline measurements recorded
  - File size: 32,445 bytes, Line count: 464, Section structure: see Section 2 above
- [x] **AC3**: All cross-references from AGENTS.md to skill/command files enumerated and verified
  - 3 broken internal links found (details in Section 3)
  - 1 valid internal link found
  - 2 external URLs, 2 wiki-style references
- [x] **AC4**: Full test suite passes on unmodified baseline
  - 1,777 passed, 2 skipped, 1 xfailed in 40.22s

## 6. Identified Issues (Pre-Refactoring)

The following issues were discovered during validation and should be addressed during the refactoring:

| # | Issue | Severity | Location |
|---|-------|----------|----------|
| 1 | `skills/ship/SKILL.md` should be `skill/ship/SKILL.md` (typo: "skills" vs "skill") | Medium | Lines 70, 111 |
| 2 | `scripts/release/merge-dev-to-main.sh` referenced as absolute path but file is at `skill/ship/scripts/release/merge-dev-to-main.sh` | Low | Lines 71, 107 |

## 7. Change Log

| Date | Change | Author |
|------|--------|--------|
| 2026-07-11 | Initial baseline validation document created | pi |