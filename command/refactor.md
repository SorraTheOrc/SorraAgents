---
description: Refactoring session - discovers opportunities to improve code quality without changing behavior
subtask: true
---

# Refactor Mode - Code Quality Improvement

Identify opportunities to improve code quality, readability, and maintainability WITHOUT changing external behavior.

## Refactoring Target

Use `$ARGUMENTS` to specify the target code or module. Default: entire codebase.

## The Golden Rule

> **Refactoring changes HOW code works internally, never WHAT it does externally.**

Do not edit files during the assessment. Only record changes when confident behavior remains identical.

## Results and Outputs

- An Epic with children, each representing a specific refactoring opportunity
- Idempotence: re-running detects existing items and avoids duplicates (may update descriptions)

## Hard requirements

- Next-step recommendations MUST always progress to the next step in the protocol below, with a summary of what that step involves.
- Creates zero or more Worklog work items at project root, each describing a specific refactoring opportunity.

## Refactoring Protocol

### Phase 1: Assess

1. **Understand current behavior**
   - Read `docs/` (exclude `docs/dev`), `README.md`, and other high-level files for product context.
   - What does this code do, what are its inputs/outputs, what are the edge cases?

2. **Identify code smells** — Look for: long methods, duplicated code, complex conditionals, poor naming, large classes, feature envy, data clumps, unnecessary comments.

   Ignore (not refactoring opportunities): public API signatures, new features, large sweeping changes.

3. **Check test coverage** — Are there existing tests? Do they cover the code to be refactored? Are they reliable and fast?

### Phase 2: Plan

1. **Prioritize improvements** — Assess impact vs effort, risk, and dependencies. Assign priority:
   - `1` — Critical maintainability issues that hinder future work
   - `2` — High-impact improvements that enhance clarity and reduce complexity
   - `3` — Minor improvements with low impact

2. **Record** — Create a Worklog work item per opportunity with:
   - Title: `REFACTOR: <summary>`
   - Description including location (file, class, method), why it's a problem, the refactoring technique (e.g., Extract Method, Rename Variable)
   - Tests to validate unchanged behavior (or recommendations to improve coverage)
   - Rationale explaining why this change improves the code
   - Tags: `refactor` plus relevant module/component tags
   - Priority (1, 2, or 3)
