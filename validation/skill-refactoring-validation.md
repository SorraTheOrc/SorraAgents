# Large Skill File Refactoring — Baseline Validation Report

**Work Item:** SA-0MRGV9AZC006HNZL — Validate large skill file refactoring  
**Parent Epic:** SA-0MRGTMMBB003H1QT — Refactor AGENTS.md, commands, and skill files for conciseness and to remove duplication  
**Date:** 2026-07-14  
**Scope:** 6 largest skill SKILL.md files (total ~124.5KB)

> **Historical note:** `skill/implement-single/` was removed from the repo in
> work item SA-0MSHX92090037TUF (Remove the implement-single skill). The rows
> and sections below referencing it are struck through and are kept only as a
> record of the original baseline.

---

## AC 1: Per-file Baseline Measurements

| File | Lines | Bytes (KB) | % of Total (6 files) | Slug |
|------|-------|-----------|---------------------|------|
| `skill/audit/SKILL.md` | 492 | 27.9 KB | 22.4% | audit |
| `skill/implement/SKILL.md` | 419 | 27.9 KB | 22.4% | implement |
| `skill/plan/SKILL.md` | 499 | 24.7 KB | 19.9% | plan |
| `skill/ship/SKILL.md` | 530 | 21.7 KB | 17.4% | ship |
| ~~`skill/implement-single/SKILL.md`~~ | ~~207~~ | ~~10.1 KB~~ | ~~8.1%~~ | ~~implement-single~~ (skill removed in SA-0MSHX92090037TUF) |
| **Total (5 files)** | **2,147** | **112.5 KB** | **100%** | |
| `AGENTS.md` (reference) | 312 | 16.2 KB | — | |

> **Note:** The epic description stated ~119KB total. Actual total is ~124.5KB due to slight changes from ongoing work.

### Additional Context Files (for overlap analysis)

| File | Lines | Bytes (KB) | Purpose |
|------|-------|-----------|---------|
| `command/plan.md` | 375 | 26.3 KB | Command counterpart for plan skill |
| ~~`command/intake.md`~~ | ~~200~~ | ~~12.3 KB~~ | Command counterpart for intake skill — migrated to `skill/intake/SKILL.md` (SA-0MS3CIU0F00066CD) |
| `command/refactor.md` | 82 | 3.3 KB | Command counterpart for refactor skill |
| `AGENTS.md` | 312 | 16.2 KB | Project-wide policies & workflow steps |

---

## AC 2: Content Overlap with AGENTS.md Workflow Steps

### 2.1 `skill/implement/SKILL.md` — Overlap Analysis

| AGENTS.md Section | Overlapping in SKILL.md | Nature of Overlap |
|---|---|---|
| Workflow step 5: "Never commit to main" | Best Practices: "Never commit directly to `main`" | Near-duplicate, slightly expanded in skill |
| Workflow step 5: "Write tests first, build → test → commit" | Best Practices + Step 4: "Write tests first (test-driven development)" | Skill elaborates with 3 sub-bullets, but core rule is identical |
| Workflow step 5: "git push origin HEAD:refs/heads/dev" | Step 7: "Push the feature branch into `dev`" | Identical command, skill adds ship.js alternative |
| Workflow step 5: worktree commands `git worktree add --track -b wl-<id>-<slug>` | Step 3: same worktree creation commands | Near-identical commands |
| "Never commit without ensuring build passes, all tests pass" | Step 4, Step 7: "Build project and verify no errors, run entire test suite" | Skill has more detail (quiet test helper, triage helper) |
| "Always record commit message and hash in work-item comment" | Best Practices: "When committing add a comment to the work item with the commit message and hash" | Near-duplicate |
| AGENTS.md stage/status lifecycle | Status Transition Matrix | Skill has a full matrix table absent from AGENTS.md (skill is more detailed) |
| "Do NOT close the work-item at this stage" | Step 7 note: "The work-item is **not closed** at this stage" | Near-duplicate wording |
| "Agents SHOULD NOT push directly to `main`" | "Never commit directly to `main`" | Consistent but differently worded |

**Assessment:** 8 overlapping areas identified. The skill file is more detailed than AGENTS.md in most cases. The status transition matrix in the skill is a superset of AGENTS.md lifecycle descriptions. The worktree commands and build→test→commit order are directly duplicated.

### 2.2 `skill/audit/SKILL.md` — Overlap Analysis

| AGENTS.md Section | Overlapping in SKILL.md | Nature of Overlap |
|---|---|---|
| `wl update <id> --status in_progress --json` | Status Lifecycle: same command | Same status claim command |
| `wl list --json`, `wl in_progress --json` | Step 1 (scan mode): uses same commands | Minimal overlap — audit uses these for discovery |
| Stage/status lifecycle (stage transitions) | Implicit in audit's `in_progress` → `open` pattern | Audit has its own lifecycle (returns to `open`) |

**Assessment:** Minimal overlap. Audit has unique content (AC scanning, evidence collection, report generation). The only significant overlap is the standard `wl update ... in_progress` claim pattern shared across all skills.

### 2.3 `skill/plan/SKILL.md` — Overlap Analysis

| AGENTS.md Section | Overlapping in SKILL.md | Nature of Overlap |
|---|---|---|
| Workflow step 3: "Plan the work — Break into sub-tasks" | Process step 2: "If planning is required, break down into features/tasks" | Core concept duplicated; skill has ~200 lines of detailed process |
| "Create child work-items: `wl create -t ... --parent <id>`" | Process step 4: uses same `wl create --parent` pattern | Commands and flags are identical |
| "Advance stage: `wl update <id> --stage plan_complete`" | Process step 8: same `--stage plan_complete` | Near-identical |
| Stage/status lifecycle descriptions | Status lifecycle section | Skill expands on the lifecycle with same core rules |
| "Every work-item must have a clear goal... testable ACs" | Hard requirements: "All work items must have... clear goal and testable ACs" | Same principle, different wording |

**Assessment:** 5 overlapping areas. The plan skill duplicates the core workflow steps from AGENTS.md but provides significantly more detail. The `wl create --parent` and `--stage plan_complete` commands are directly duplicated.

### 2.4 `skill/ship/SKILL.md` — Overlap Analysis

| AGENTS.md Section | Overlapping in SKILL.md | Nature of Overlap |
|---|---|---|
| Workflow step 5: "git push origin HEAD:refs/heads/dev" | `pushToDev()`: uses same push mechanism | Ship skill wraps this in a JS function |
| Workflow step 5: worktree cleanup | Worktree section: same cleanup steps | Skill elaborates on worktree lifecycle |
| "Do NOT close the work-item at this stage" | Integration with AGENTS.md section: describes same policy | Skill has explicit "Integration with AGENTS.md" section |
| "Agents SHOULD NOT push directly to `main`" | `validatePushTarget()`: blocks main/master | Skill implements this as code, AGENTS.md states it as policy |
| Push policy: "Push only to `dev`" | "The push target `dev` is **not** a protected branch" | Consistent policy, differently expressed |

**Assessment:** 5 overlapping areas. Ship skill has a dedicated "Integration with AGENTS.md" section (lines 513-522) that explicitly describes how its workflow complements AGENTS.md. The push-to-dev commands are duplicated but skill wraps them in script functions.

### ~~2.5 `skill/implement-single/SKILL.md`~~ — ~~Overlap Analysis~~ (skill removed in SA-0MSHX92090037TUF)

| AGENTS.md Section | Overlapping in SKILL.md | Nature of Overlap |
|---|---|---|
| Workflow step 5: worktree commands | Step 2: same worktree commands | Near-identical commands |
| "Write tests first, then code. Follow build → test → commit order" | Step 3: "Write tests first (test-driven development)" | Near-identical, skill has slightly less detail than full implement |
| "Never commit without ensuring build passes, all tests pass" | Step 4: "Build and run all tests" | Same core principle |
| "Never commit directly to `main`" | "Do NOT create a Pull Request to `main`" | Same policy |
| "git push origin HEAD:refs/heads/dev" | Step 4: same push command | Identical command |

**Assessment:** ~~5 overlapping areas. implement-single is a streamlined subset of implement/SKILL.md, so overlaps with AGENTS.md mirror the implement skill overlaps but with less detail.~~

---

## AC 3: Content Overlap with Corresponding Command Files

### 3.1 `skill/plan/SKILL.md` ↔ `command/plan.md`

| Section | SKILL.md | command/plan.md | Overlap |
|---|---|---|---|
| Inputs | Same structure: work-item id, optional guidance | Same structure | **Heavy** — near-identical |
| Results and Outputs | Same: child work-items with ACs | Same: child work-items with ACs | **Heavy** — near-identical |
| Hard requirements | 6 requirements | 6 requirements | **Heavy** — same requirements, slightly different wording |
| Status lifecycle | `wl update <id> --status in_progress` | Same command | **Full** — identical commands |
| Seed context | Read docs, PRDs | Same instructions | **Heavy** — near-identical |
| Pre-check: Effort/Risk | Same script invocation | Same script invocation | **Full** — identical |
| Automated review (auto-complete) | 5 review stages | 5 review stages | **Full** — identical structure and criteria |
| Process (7 steps) | Same numbered steps | Same numbered steps | **Full** — identical workflow steps with same content |
| Traceability & idempotence | Same policy | Same policy | **Heavy** — nearly identical text |
| Editing rules & safety | Same rules | Same rules | **Heavy** — nearly identical |
| 8. Finishing | `--stage plan_complete` | Same command | **Full** — identical |
| Appendix: clarifying questions | Same structure | Same structure | **Full** — identical appendix format |

**Assessment:** **Heavy to Full overlap** across all sections. These two files are largely duplicates. The command file is the authoritative version (26.3 KB), and the skill file (24.7 KB) mirrors it closely with slightly more inline guidance. This represents the single largest deduplication opportunity.

### 3.2 Other Skills Without Direct Command Counterparts

The remaining 3 skill files (implement, audit, ship) do NOT have corresponding command files. Their overlap analysis is scoped to AGENTS.md only (covered in AC 2 above).

| Skill File | Has Command Counterpart? | Command File |
|---|---|---|
| `skill/implement/SKILL.md` | No | — |
| `skill/audit/SKILL.md` | No | — |
| `skill/plan/SKILL.md` | Yes | `command/plan.md` (26.3 KB) |
| `skill/ship/SKILL.md` | No | — |
| ~~`skill/implement-single/SKILL.md`~~ | ~~No~~ | ~~—~~ (skill removed in SA-0MSHX92090037TUF) |

> **Note:** The epic description identifies these command↔skill pairs: `command/plan.md` ↔ `skill/plan/SKILL.md`, ~~`command/intake.md`~~ ↔ `skill/intake/SKILL.md` (migrated in SA-0MS3CIU0F00066CD, replaced the ~~`skill/intakeall/SKILL.md`~~ mapping, which was removed in commit `1b56b64`), `command/refactor.md` ↔ `skill/refactor/SKILL.md`, `command/author_skill.md` ↔ `skill/author-command/SKILL.md`. Of these, only `plan/SKILL.md` is among the 5 largest files.

---

## AC 4: Cross-References Enumerated

### 4.1 `skill/implement/SKILL.md` — Cross-References

| Reference | Type | Location in File |
|---|---|---|
| `[AGENTS.md](../../AGENTS.md#implement-the-work-item)` | Outbound | Step 3 (worktree section) |
| `[[concepts/git-worktree-best-practices-for-agent-workflows]]` | Wiki link | Best Practices + Step 7 |
| ~~`command/intake.md`~~ → `skill/intake/SKILL.md` | Skill reference | Definition gate (step 1.1) — updated in SA-0MS3CIU0F00066CD |
| `command/plan.md` | Command reference | Definition gate (step 1.1) |
| `../refactor/SKILL.md` | Skill reference | Optional refactor step |
| `../ship/scripts/ship.js` | Script reference | Step 7 push instructions |
| `../ship/SKILL.md` | Skill reference | Step 7 notes |
| `../ship/scripts/run-release.js` | Script reference | Step 7 notes |

**Total: 8 cross-references** (1 outbound to AGENTS.md, 2 to command files, 2 to other skills, 3 to scripts)

### 4.2 `skill/audit/SKILL.md` — Cross-References

| Reference | Type | Location |
|---|---|---|
| `./scripts/persist_audit.py` | Script reference | Status Lifecycle section |
| test fixture files (inline) | Data files | Step 2 (AC scanning) |

**Total: 2 cross-references** (all self-contained, no cross-skill or AGENTS.md refs)

### 4.3 `skill/plan/SKILL.md` — Cross-References

| Reference | Type | Location |
|---|---|---|
| `../effort-and-risk/SKILL.md` | Skill reference | Pre-check section |
| `./scripts/plan_helpers.py` | Script reference | Multiple sections |
| `../triage/SKILL.md` | Skill reference | Error handling |
| `skill/ship/SKILL.md` | Skill reference | Outputs section |

**Total: 4 cross-references** (all to other skills or local scripts, no direct AGENTS.md refs)

### 4.4 `skill/ship/SKILL.md` — Cross-References

| Reference | Type | Location |
|---|---|---|
| `[[concepts/git-worktree-best-practices-for-agent-workflows]]` | Wiki link | Step 3 & Integration section |
| `[AGENTS.md](../../AGENTS.md#implement-the-work-item)` | Outbound to AGENTS.md | Step 3 |
| `[AGENTS.md](../../AGENTS.md)` | Outbound to AGENTS.md | Integration section |
| `docs/dev/release-process.md` | Doc reference | Release process section |
| `docs/dev/release-tests.md` | Doc reference | Release tests section |
| `./scripts/ship.js` | Script reference | Usage section |
| `./scripts/git-helpers.js` | Script reference | Scripts section |

**Total: 7 cross-references** (2 outbound to AGENTS.md, 1 wiki link, 2 to project docs, 2 to scripts)

### ~~4.5 `skill/implement-single/SKILL.md`~~ — ~~Cross-References~~ (skill removed in SA-0MSHX92090037TUF)

| Reference | Type | Location |
|---|---|---|
| `../refactor/SKILL.md` | Skill reference | Optional refactor step |

**Total: 1 cross-reference** (all self-contained)

### Cross-Reference Summary

| Skill File | Total Refs | To AGENTS.md | To Command Files | To Other Skills | To Scripts/Docs | Wiki Links |
|---|---|---|---|---|---|---|
| implement/SKILL.md | 8 | 1 | 2 | 2 | 3 | 1 |
| audit/SKILL.md | 2 | 0 | 0 | 0 | 2 | 0 |
| plan/SKILL.md | 4 | 0 | 0 | 3 | 1 | 0 |
| ship/SKILL.md | 7 | 2 | 0 | 0 | 4 | 1 |
| ~~implement-single/SKILL.md~~ | ~~1~~ | ~~0~~ | ~~0~~ | ~~1~~ | ~~0~~ | ~~0~~ (skill removed in SA-0MSHX92090037TUF) |
| **Total** | **21** | **3** | **2** | **5** | **10** | **2** |

> **Notable:** implement/SKILL.md has the most cross-references (8). plan/SKILL.md has zero references to AGENTS.md despite being one of the most duplicated files.

---

## AC 5: Baseline Test Suite Results

Test runner: vitest (v4.1.10)  
Command: `npx vitest run`

### Test Results

```
Test Files  1 failed (1)
     Tests  no tests
```

### Analysis

The project has no test files configured for vitest discovery, so the baseline shows zero tests.

### Verdict

- **No test files are currently discovered by vitest.**
- Test files exist in `command/tests/`, `skill/*/tests/`, and `tests/` directories but are excluded from vitest's `include` pattern.
- **This is a pre-existing configuration issue**, not introduced by recent changes.

### Other Test Directories (currently excluded from vitest discovery)

The following directories contain test files but are not configured in vitest's `include` pattern:

- `command/tests/`
- ~~`skill/implementall/tests/`~~ (skill removed in commit `1b56b64`)
- ~~`skill/intakeall/tests/`~~ (skill removed in commit `1b56b64`)
- ~~`skill/planall/tests/`~~ (skill removed in commit `1b56b64`)
- `skill/plan/tests/`
- `tests/`

> **Recommendation:** For the refactoring epic to satisfy AC#6 ("Full project test suite must pass"), vitest's configuration should be updated to discover all test directories.

---

## Summary of Findings

### Overlap Severity Matrix

| Skill File | AGENTS.md Overlap | Command File Overlap | Cross-Refs | Dedup Priority |
|---|---|---|---|---|
| implement/SKILL.md | Heavy (8 areas) | No command file | 8 (highest) | **High** — duplicate AGENTS.md workflow steps in detail |
| audit/SKILL.md | Minimal (2 areas) | No command file | 2 | **Low** — mostly unique content |
| plan/SKILL.md | Medium (5 areas) | **Full** (near-identical to command/plan.md) | 4 | **Highest** — combined AGENTS.md + command file overlap |
| ship/SKILL.md | Medium (5 areas) | No command file | 7 | **Medium** — moderate AGENTS.md overlap, many refs |
| ~~implement-single/SKILL.md~~ | ~~Medium (5 areas)~~ | ~~No command file~~ | ~~1~~ | ~~**Medium** — moderate AGENTS.md overlap~~ (skill removed in SA-0MSHX92090037TUF) |

### Key Insights

1. **plan/SKILL.md is the highest deduplication priority** — It has near-full overlap with `command/plan.md` (identical structure across all sections) plus 5 overlapping areas with AGENTS.md.

2. **implement/SKILL.md has the most AGENTS.md overlap by line count** — 8 overlapping areas including near-identical worktree commands, build→test→commit order, and push-to-dev instructions.

3. ~~**implement-single/SKILL.md is a condensed version of implement/SKILL.md** — Its 5 AGENTS.md overlaps are a subset of implement's, with most of the unique implement content stripped out.~~ (skill removed in SA-0MSHX92090037TUF)

4. **Total estimated repeat content:** ~15-25% of the combined 124.5 KB across these 5 files is duplicated from either AGENTS.md or command files. This represents approximately 19-31 KB of redundant text.
