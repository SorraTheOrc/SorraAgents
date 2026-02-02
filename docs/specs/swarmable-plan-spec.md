Title: Swarmable Plans — Spec & Design

Overview
--------
This document captures the Spec & Design for the Swarmable Plans feature (work item: SA-0ML4EDHTD09ZZD7Y). It formalizes detection rules, the blocking decision algorithm, CLI behaviour, permission considerations, and acceptance tests.

Goals
-----
- Detect exact-path overlaps among sibling tasks' `allowed_files` entries.
- Deterministically decide a single blocker per overlapping group using: Worklog priority -> createdAt.
- Record dependency edges and human-readable comments (automation) rather than converting into parent/child relationships.
- Be idempotent: repeated runs produce no duplicate operations.

Data model
----------
- `allowed_files`: recommended representation — an array of exact file paths on the work item JSON (string[]). Example: ["src/app/main.py", "assets/ui/logo.png"].
- `dependency_metadata`: optional per-work-item field to store automation decisions; includes {source: "plan-automation", createdAt, reason, blockerId}.

Blocking decision algorithm
---------------------------
1. For a given parent (epic) examine all direct children (sibling tasks).
2. For each exact file path present in two or more siblings mark an overlap group.
3. Select the blocker as the work item in the overlap group with the highest Worklog priority (critical > high > medium > low). If multiple items share the same priority, select the one with earliest `createdAt` timestamp.
4. For each non-blocker in the group create a dependency relation that marks it as blocked by the blocker and add a comment with the canonical format (see CLI examples). Do not convert items into parent/child relationships.

Cross-epic behaviour
---------------------
- Cross-epic overlaps should be reported, not auto-blocked by default. The report should list overlaps across epic boundaries and recommend manual triage.

CLI behaviour & reporting
-------------------------
- Command: `/plan` (existing driver) — accepts a work item id or implied parent context.
- Report: deterministic, machine-readable summary and human-readable summary. Example human output:

```
Detected 2 overlaps:
- overlap: src/config.yaml
  - blocker: SA-0AAA111 (priority: high, createdAt: 2026-01-01T00:00Z)
  - blocked: SA-0BBB222, SA-0CCC333
Actions taken: added 2 dependency edges, added 2 comments.
```

CLI idempotence rules
---------------------
- Before creating a `wl dep` edge or posting a comment, the automation must detect existing edges/comments created by itself (match by exact comment text or `dependency_metadata` field) and skip if present.
- When updating the parent work item with a generated Milestones/Plan block, replace the content between explicit markers: `<!-- PLAN:START -->` and `<!-- PLAN:END -->`.

Acceptance tests (definition)
-----------------------------
Unit tests
- detection_unit_tests
  - verify `allowed_files` parsing and exact path matching (no globbing)
  - verify tie-breaker logic: priority then createdAt

Integration tests
- e2e_overlap_blocking
  - create a test parent with 3 children where two share the exact file path
  - run `/plan` (mock wl command if necessary) and assert that:
    - the correct `wl dep` action would be issued (or simulated) for the blocked items
    - the correct comment text is posted to blocked items
    - re-running `/plan` produces no additional `wl` operations and returns "no changes"
  - cross-epic overlap case: assert report includes cross-epic entries and does not auto-block

Deliverables
------------
- this spec file (`docs/specs/swarmable-plan-spec.md`)
- example CLI outputs and test scenarios (above)
- acceptance test definitions (above)

Open Questions
--------------
- Confirm `allowed_files` storage format and whether it should be normalized to a separate table vs inline array.
- Confirm exact comment text format and whether `dependency_metadata` is acceptable in the WorkLog schema.
