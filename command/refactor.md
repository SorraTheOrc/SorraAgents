---
description: Refactoring session - discovers opportunities to improves code quality without changing behavior
agent: patch
subtask: true
---

# Refactor Mode - Code Quality Improvement

Your mission is to identify opportunities to improve code quality, readability, and maintainability WITHOUT changing external behavior.

## Refactoring Target

Use `$ARGUMENTS` to specify the target code or module for refactoring. If no target is specified, analyze the entire codebase for refactoring opportunities.

## The Golden Rule

> **Refactoring changes HOW code works internally, never WHAT it does externally.**

You will not edit any files during the assessment. Instead you will try to identify opportunities for improvements that improve code quality and maintainability but ensures that existing behavior remains identical. If unsure, do not record the proposed change.

This command will create zero or more Worklog work items at the root of the project, each describing a specific refactoring opportunity.

## Refactoring Protocol

### Phase 1: Assess

1. **Understand current behavior**
   - Read `docs/` (excluding `docs/dev`), `README.md`, and other high-level files for product context.
   - What does this code do?
   - What are its inputs and outputs?
   - What are the edge cases?

2. **Identify code smells**
   - Long methods/functions
   - Duplicated code
   - Complex conditionals
   - Poor naming
   - Large classes
   - Feature envy
   - Data clumps
   - Unnecessary comments

   The following are not considered refactoring opportunities (and should be ignored):
   - Public API signatures
   - New features
   - Large sweeping changes

3. **Check test coverage**
   - Are there existing tests?
   - Do they cover the code to be refactored?
   - Are the tests reliable and fast?

### Phase 2: Plan

1. **Prioritize improvements**
   - Impact vs effort analysis
   - Risk assessment
   - Dependencies between changes
   - Priority will be set to:
   - `1` for critical maintainability issues that hinder future work
   - `2` for high-impact improvements that enhance clarity and reduce complexity
   - `3` for minor improvements that have low impact

2. **Record** - Create a Worklog work item for each refactoring opportunity with:
   - A title in the form "REFACTOR: <summary>"
   - A clear description of the code smell to be addressed. Including:
     - Location (file, class, method)
     - Explanation of why it is a problem
     - The specific refactoring technique to be applied (e.g., Extract Method, Rename Variable, etc.).
   - Tests that will validate behavior remains unchanged.
     - Recommendations for improving existing tests if coverage is insufficient.
   - A rationale explaining why this change improves the code.
   - Labels: `refactor` plus any relevant module/component tags.
   - Priority (1, 2, or 3).
