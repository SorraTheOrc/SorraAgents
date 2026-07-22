# Baseline Measurements: Command Files Refactoring

> Generated: 2026-07-11
> Work Item: SA-0MRGV9AZH002OSSC (Validate command file refactoring)
> Branch: `wl-SA-0MRGV9AZH002OSSC-validate-command`

## Purpose

Record per-file baseline measurements for the 6 command files, create
deduplication mappings with their paired skill counterparts, enumerate
cross-references, and capture baseline test suite results.

---

## 1. Per-File Baseline Measurements

### 1.1 Summary Table

| File | Size (bytes) | Lines | Sections (H1/H2/H3+) | YAML Frontmatter |
|------|-------------|-------|---------------------|-----------------|
| `command/plan.md` | 26,252 | 375 | 14 sections | 2 blocks |
| `command/intake.md` | 18,832 | 232 | 14 sections | 2 blocks |
| `command/author_skill.md` | 16,028 | 216 | 19 sections | 2 blocks |
| `command/review.md` | 12,323 | 197 | 8 sections | 2 blocks |
| `command/doc.md` | 8,774 | 230 | 28 sections | 2 blocks |
| `command/refactor.md` | 3,285 | 82 | 8 sections | 2 blocks |
| **Total** | **85,494** | **1,332** | **—** | **—** |

### 1.2 Detailed Section Breakdown

#### command/plan.md (375 lines, 26,252 bytes)

| Line | Heading | Level |
|------|---------|-------|
| 20 | Inputs | H2 |
| 26 | Results and Outputs | H2 |
| 31 | Hard requirements | H2 |
| 50 | Note | H2 |
| 54 | Status lifecycle (first action) | H2 |
| 62 | Seed context | H2 |
| 70 | Pre-check: Effort/Risk Threshold | H2 |
| 131 | Automated review on existing content | H2 |
| 185 | Process (must follow) | H2 |
| 321 | Traceability & idempotence | H2 |
| 326 | Editing rules & safety | H2 |
| 335 | 8. Finishing (must do as the final step only) | H2 |
| 343 | Examples | H2 |
| 350 | Appendix: Clarifying questions & answers | H2 |

#### command/intake.md (232 lines, 18,832 bytes)

| Line | Heading | Level |
|------|---------|-------|
| 11 | Description | H2 |
| 16 | Inputs | H2 |
| 22 | Results and Outputs | H2 |
| 28 | Behavior | H2 |
| 32 | Hard requirements | H2 |
| 47 | Status lifecycle (first action) | H2 |
| 55 | Process (must follow) | H2 |
| 169 | Objective | H1 |
| 173 | Acceptance Criteria | H1 |
| 187 | Effort and Risk | H1 |
| 193 | Traceability & idempotence | H2 |
| 197 | Editing rules & safety | H2 |
| 204 | Appendix: Clarifying questions & answers | H2 |

#### command/author_skill.md (216 lines, 16,028 bytes)

| Line | Heading | Level |
|------|---------|-------|
| 6 | Skill Creator | H1 |
| 10 | About Skills | H2 |
| 17 | What Skills Provide | H3 |
| 24 | Anatomy of a Skill | H3 |
| 39 | SKILL.md (required) | H4 |
| 43 | Bundled Resources (optional) | H4 |
| 47 | Scripts (scripts/) | H5 |
| 57 | References (references/) | H5 |
| 68 | Assets (assets/) | H5 |
| 77 | Progressive Disclosure Design Principle | H3 |
| 85 | Results and Outputs | H2 |
| 96 | Hard requirements | H2 |
| 100 | Skill Creation/Update Process | H2 |
| 104 | Step 1: Understanding the Skill | H3 |
| 131 | Step 2: Planning the Reusable Skill | H3 |
| 148 | Step 3: Drafting SKILL.md and Resources | H3 |
| 165 | Step 4: Producer Review and Iteration | H3 |
| 171 | Step 5: Finalizing the Skill | H3 |
| 210 | Step 6: Finalizing the skill | H3 |

#### command/review.md (197 lines, 12,323 bytes)

| Line | Heading | Level |
|------|---------|-------|
| 11 | Description | H2 |
| 17 | Inputs | H2 |
| 23 | Results and Outputs | H2 |
| 31 | Behavior | H2 |
| 35 | Hard requirements | H2 |
| 49 | Process (must follow) | H2 |
| 185 | Traceability & idempotence | H2 |
| 191 | Editing rules & safety | H2 |

#### command/doc.md (230 lines, 8,774 bytes)

| Line | Heading | Level |
|------|---------|-------|
| 56 | Results and Outputs | H2 |
| 62 | Hard requirements | H2 |
| 120 | <Feature / Product Name> | H1 |
| 122 | Overview | H2 |
| 124 | Who This Is For | H2 |
| 126 | Quick Start | H2 |
| 128 | How It Works (User View) | H2 |
| 130 | Usage Examples | H2 |
| 132 | Error Messages & Fixes | H2 |
| 134 | Limitations | H2 |
| 136 | FAQ | H2 |
| 138 | Open Questions | H2 |
| 144 | <Feature / Component Name> (Developer) | H1 |
| 146 | Summary | H2 |
| 148 | Architecture | H2 |
| 150 | Data Model / State | H2 |
| 152 | Key Algorithms / Rules | H2 |
| 154 | Configuration | H2 |
| 156 | Integration Points | H2 |
| 158 | Failure Handling | H2 |
| 160 | Observability | H2 |
| 162 | Testing Notes | H2 |
| 164 | Rollout / Migration | H2 |
| 166 | Open Questions | H2 |

#### command/refactor.md (82 lines, 3,285 bytes)

| Line | Heading | Level |
|------|---------|-------|
| 6 | Refactor Mode - Code Quality Improvement | H1 |
| 10 | Refactoring Target | H2 |
| 14 | The Golden Rule | H2 |
| 20 | Results and Outputs | H2 |
| 25 | Hard requirements | H2 |
| 31 | Refactoring Protocol | H2 |
| 33 | Phase 1: Assess | H3 |
| 61 | Phase 2: Plan | H3 |

---

## 2. Deduplication Mapping

### 2.1 Pair: command/plan.md ↔ skill/plan/SKILL.md

**Overlap Assessment: HEAVY (~90% content overlap)**

| Section | Command | Skill | Verdict |
|---------|---------|-------|---------|
| Inputs | Lines 20-25 | Lines 13-20 | Near-identical |
| Results and Outputs | Lines 26-30 | Lines 21-28 | Near-identical |
| Hard requirements | Lines 31-51 | Lines 29-70 | Near-identical (10 shared requirements) |
| Status lifecycle | Lines 54-61 | Lines 71-78 | Near-identical |
| Seed context | Lines 62-69 | Lines 79-93 | Near-identical |
| Pre-check: Effort/Risk | Lines 70-130 | Lines 94-150 | Near-identical |
| Automated review | Lines 131-184 | Lines 151-204 | Near-identical |
| Process (must follow) | Lines 185-320 | Lines 205-385 | Near-identical (11-step process) |
| Traceability & idempotence | Lines 321-325 | Lines 386-397 | Near-identical |
| Editing rules & safety | Lines 326-334 | Lines 398-411 | Near-identical |
| 8. Finishing | Lines 335-342 | Lines 412-420 | Near-identical |
| Examples | Lines 343-349 | — | Command-only |
| Appendix | Lines 350-375 | Lines 450-480 | Near-identical |

**Key differences:**

- Command.md has a "Note" section (line 50) declaring itself a legacy adapter that delegates to the skill
- Skill has a "Bundled Resources" section (line 421) not in command
- Command uses legacy paths (`command/plan_helpers.py`); skill uses bundled paths (`plan_helpers.py`)

**Recommendation:** Make command/plan.md reference skill/plan/SKILL.md as canonical source and strip duplicated content.

### 2.2 Pair: command/intake.md ↔ skill/intakeall/SKILL.md

**Overlap Assessment: LOW (~20% content overlap)**

| Section | command/intake.md | skill/intakeall/SKILL.md | Verdict |
|---------|-------------------|-------------------------|---------|
| Description | Lines 11-15 | — | Command-only |
| Inputs | Lines 16-21 | — | Command-only |
| Results and Outputs | Lines 22-27 | — (skill has "Output" at line 34, different content) | Different |
| Behavior | Lines 28-31 | Lines 10-20 | Different focus |
| Hard requirements | Lines 32-46 | — | Command-only |
| Status lifecycle | Lines 47-54 | — | Command-only |
| Process (must follow) | Lines 55-168 | — (skill has auto-complete criteria, needs-input detection, etc.) | Different |
| Acceptance Criteria | Lines 173-186 | — | Template/workflow example content |
| — | — | Auto-complete criteria | Skill-only |
| — | — | Needs-input detection | Skill-only |
| — | — | Orphan recovery | Skill-only |
| — | — | Signal handling | Skill-only |
| — | — | CLI flags | Skill-only |
| Scripts | — | Lines 171-175 | Skill-only |
| Related skills | — | Line 176 | Skill-only |

**Key differences:**

- command/intake.md is the live interview process for gathering requirements and writing work items
- skill/intakeall/SKILL.md is an orchestrator that runs intake across multiple items, with batch-processing concerns

**Recommendation:** These files are genuinely different (one is the interview protocol, the other is a batch orchestrator). Low deduplication opportunity.

### 2.3 Pair: command/refactor.md ↔ skill/refactor/SKILL.md

**Overlap Assessment: LOW (~15% content overlap)**

| Section | command/refactor.md | skill/refactor/SKILL.md | Verdict |
|---------|--------------------|------------------------|---------|
| Purpose | Refactor Mode - Code Quality Improvement | Refactor | Same domain |
| Results and Outputs | Lines 20-24 | Lines 98-107 | Different |
| Hard requirements | Lines 25-30 | — | Command-only |
| Protocol | Phase 1: Assess, Phase 2: Plan | — | Different structure |
| — | — | Key Concepts, Architecture | Skill-only |
| — | — | When To Use | Skill-only |
| — | — | Status Management | Skill-only |
| — | — | Usage / Invocation | Skill-only |
| — | — | Configuration | Skill-only |
| — | — | Smell Types | Skill-only |
| — | — | REFACTOR Comments | Skill-only |
| — | — | Error Handling | Skill-only |

**Key differences:**

- command/refactor.md is a high-level protocol for identifying refactoring opportunities during assessment
- skill/refactor/SKILL.md is an automated code smell detection and remediation tool with scripts

**Recommendation:** These are different in structure and purpose. Low deduplication opportunity.

### 2.4 Pair: command/review.md ↔ skill/code-review/SKILL.md

**Overlap Assessment: LOW (~10% content overlap)**

| Section | command/review.md | skill/code-review/SKILL.md | Verdict |
|---------|------------------|---------------------------|---------|
| Description | PR-focused automated review in Ampa container | Local code review guidance | Different scope |
| Inputs | PR reference | — | Different |
| Process | Ampa pool container lifecycle | Code review workflow (determine target, prepare, analyze, provide feedback) | Different |
| — | — | Automated Linting pipeline | Skill-only |
| — | — | Scripts reference | Skill-only |

**Key differences:**

- command/review.md operates on GitHub PRs via Ampa pool containers
- skill/code-review/SKILL.md guides local code review with automated linting

**Recommendation:** These serve different purposes. Low deduplication opportunity.

### 2.5 Pair: command/author_skill.md ↔ skill/author-command/SKILL.md

**Overlap Assessment: LOW (~5% content overlap)**

| Section | command/author_skill.md | skill/author-command/SKILL.md | Verdict |
|---------|------------------------|------------------------------|---------|
| Title | Skill Creator | Author Command | Different domain |
| Content | Anatomy of a skill, skill creation process | How to author a command for the framework | Different |
| Process | 6-step skill creation | 7-step command authoring | Different |
| — | — | Special placeholders | Skill-only |
| — | — | Scripts | Skill-only |

**Key differences:**

- command/author_skill.md teaches how to create a skill (SKILL.md + bundled resources)
- skill/author-command/SKILL.md teaches how to author a command for the agent framework

**Recommendation:** Different domains despite similar names. Low deduplication opportunity.

### 2.6 Pair: command/doc.md ↔ (no explicit skill counterpart)

| Aspect | Detail |
|--------|--------|
| Coverage | command/doc.md has no direct skill/SKILL.md counterpart |
| Purpose | Documentation generation for features |
| Cross-refs | No cross-references to skill/ files found |

**Recommendation:** Standalone file; no deduplication mapping needed.

---

## 3. Cross-References

### 3.1 From Command Files to Other Files

| Source File | Target | Line | Type |
|-------------|--------|------|------|
| `command/plan.md` | `skill/plan/SKILL.md` | 14 | Reference (declares skill as canonical) |
| `command/plan.md` | `skill/plan/plan_helpers.py` | 74 | Reference (pre-check helper) |
| `command/plan.md` | `command/plan_helpers.py` | 86 | Reference (legacy delegation wrapper) |
| `command/intake.md` | `skill/ship/scripts/...` | 181 | Reference (CHANGELOG.md management) |
| `command/author_skill.md` | `command/plan.md` | 135 | Reference (follow plan process) |

### 3.2 From Skill Files to Command Files

| Source File | Target | Line | Type |
|-------------|--------|------|------|
| `skill/ralph/SKILL.md` | `command/plan_helpers.py` | 125 | Reference (delegation wrapper) |
| `skill/ralph/SKILL.md` | `command/plan.md` | 138 | Reference (plan command) |
| `skill/planall/SKILL.md` | `command/plan.md` | 140 | Reference (invokes plan for each item) |
| `skill/implement/SKILL.md` | `command/intake.md` | 176 | Reference (intake interview) |
| `skill/implement/SKILL.md` | `command/plan.md` | 177 | Reference (plan interview) |
| `skill/intakeall/SKILL.md` | `command/intake.md` | 178 | Reference (invokes intake for each item) |

### 3.3 Cross-Reference Map Summary

```
command/plan.md ──────► skill/plan/SKILL.md
                    ──► skill/plan/plan_helpers.py
                    ──► command/plan_helpers.py
                        ▲
skill/ralph/SKILL.md ──┘
skill/planall/SKILL.md ─┘

command/intake.md ────► skill/ship/scripts/... (transitive reference)
skill/intakeall/SKILL.md ──► command/intake.md
skill/implement/SKILL.md ──► command/intake.md
                        ──► command/plan.md

command/author_skill.md ──► command/plan.md
```

command/doc.md — no cross-references found
command/refactor.md — no cross-references found
command/review.md — no cross-references found

---

## 4. Baseline Test Suite Results

### Test Output

```
1069 passed, 2 skipped, 1 xfailed in 25.69s
```

| Metric | Value |
|--------|-------|
| Total tests | 1072 (1069 passed + 2 skipped + 1 xfailed) |
| Passed | 1069 |
| Skipped | 2 |
| Expected failures | 1 (xfailed) |
| Failed | 0 |
| Duration | 25.69s |

### Test Command

```
python3 -m pytest tests/ -x -q --tb=short
```

**Status: PASS — all tests pass on baseline**
