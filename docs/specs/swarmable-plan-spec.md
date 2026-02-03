Title: Swarmable Plans — Spec & Design

Overview
--------
This document captures the Spec & Design for the Swarmable Plans feature (work item: SA-0ML4EDHTD09ZZD7Y). It formalizes detection rules, the blocking decision algorithm, CLI behaviour, permission considerations, and acceptance tests.

Goals
-----
- Detect exact-path overlaps among sibling tasks' `allowed_files` entries.
 - Deterministically decide a single blocker per overlapping group using: `sortIndex` (highest wins) -> `createdAt`.
- Record dependency edges and human-readable comments (automation) rather than converting into parent/child relationships.
- Be idempotent: repeated runs produce no duplicate operations.

Data model
----------
- `allowed_files`: recommended representation — an array of exact file paths on the work item JSON (string[]). Example: ["src/app/main.py", "assets/ui/logo.png"].
 
Blocking decision algorithm
---------------------------
1. For a given parent (epic) examine all direct children (sibling tasks).
2. For each exact file path present in two or more siblings mark an overlap group.
3. Select the blocker as the work item in the overlap group with the highest `sortIndex` value. If multiple items share the same `sortIndex`, select the one with the earliest `createdAt` timestamp.
4. For each non-blocker in the group create a dependency relation that marks it as blocked by the blocker and add a comment with the canonical format (see CLI examples). Do not convert items into parent/child relationships.

Dependency recording & comment format
-------------------------------

When the automation records a blocking decision it must perform two actions (in this order, when permissions allow):

1) Create the WorkLog dependency edge (or record the intended action in the report if permission is not available).
   - Example CLI / API intent: `wl dep add <blockedId> <blockerId>` (or equivalent API call).
2) Post a human-readable comment on the blocked work item describing the reason and unblock criteria.

Canonical comment template (use exact phrasing for idempotence):

"Blocked by automation: <blockerId> — reason: overlapping exact file path(s): <path1>, <path2>."

Example:

```
Blocked by automation: SA-0ML4EDHTD09ZZD7Y — reason: overlapping exact file path(s): src/config.yaml.
```

When the automation cannot create an edge due to permissions, post a clear actionable comment instead:

```
Detected intended dependency: SA-0ML4EE9NH05OG9TI <- SA-0ML4EE9SQ1I39EZ3
Action skipped: automation lacks permission to create dependency edges. Please run: wl dep add <blocked> <blocker>
```

CLI behaviour & reporting
-------------------------
- Command: `/plan` (existing driver) — accepts a work item id or implied parent context.
- Report: deterministic machine-readable summary and concise human summary. Example human output:

```
Detected 2 overlaps:
- overlap: src/config.yaml
  - blocker: SA-0AAA111 (sortIndex: 200, createdAt: 2026-01-01T00:00Z)
  - blocked: SA-0BBB222, SA-0CCC333
Actions taken: added 2 dependency edges, added 2 comments.
```

Idempotence notes (practical guidance)
-------------------------------------
- Match exact canonical comment text before posting a comment to avoid duplicates; use the canonical template above.
- When possible, inspect the WorkLog API for existing dependency references before issuing `wl dep add`. If the API surface is limited, rely on exact comment matching and the generated parent block state to detect prior runs.
- Re-running `/plan` on an unchanged parent should be a no-op and return "no changes".

Example dependency graph excerpt (human-readable):

```
SA-0ML4EDHTD09ZZD7Y (Spec & Design)
  └─ SA-0ML4EE9NH05OG9TI (Detection Engine) [blocked by SA-0ML4EDHTD09ZZD7Y]
  └─ SA-0ML4EE9SQ1I39EZ3 (Dependency Recording) [blocked by SA-0ML4EE9NH05OG9TI]
```

Cross-epic handling
-------------------
- When overlaps span different parent epics, each parent run should detect and record the overlap in its generated report. Do not automatically create cross-epic dependency edges unless an explicit configuration/option is provided to enable cross-epic blocking.

Acceptance tests (definition)
-----------------------------
Unit tests
- detection_unit_tests
  - verify `allowed_files` parsing and exact path matching (no globbing)
  - verify tie-breaker logic: `sortIndex` (highest wins) then `createdAt`

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
-- Confirm exact comment text format.
