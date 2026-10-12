# Test-Churn Worklog Migration — ContextHub → SorraAgents

> **Work item:** `SA-0MV28WSLP000WOJ8` (child of epic `SA-0MV28WNFY009DF6Y`)
> **Audience:** worklog maintainers tracing the provenance of the test-churn
> capability.

`wl` worklog stores are per-repository (`.worklog/`), so cross-repository
items cannot be re-parented or linked with dependency edges. The migration was
therefore performed **manually**, recording the relationships in descriptions
and comments in **both** stores. This document is the SorraAgents-side record.

## Mapping

| ContextHub item | Status before | SorraAgents replacement | Notes |
|---|---|---|---|
| `WL-0MUZRNLFT004H8XJ` — Analyse test code rewrite patterns to evaluate TDD value | completed / in_review | epic `SA-0MV28WNFY009DF6Y` (port `SA-0MV28WROY007NAB4`, skill `SA-0MV28WS5G004YNNS`) | Capability re-homed; original description and ACs carried over below. |
| `WL-0MV274I3R009XVZR` — Fix test churn analysis status detection and rewrite-ratio denominator | deleted | `SA-0MV2JE6MG003B0SW` | Re-homed before this migration and later closed as superseded by `SA-0MV28WROY007NAB4`. |

## Commit provenance

| Commit | Repository | Purpose |
|---|---|---|
| `141ceb8b` | ContextHub | Source artefact `scripts/test_churn_analysis.py` at the time of the port. |
| `b8e4eded` | ContextHub | Removes the analysis script, data and report from ContextHub. |
| `ce121b8b` | SorraAgents | Ports the corrected analyser and its unit tests. |
| `8bac40bb` | SorraAgents | Adds the reproducible skill, install wiring and reference documentation. |

## Decisions

- The capability is owned by SorraAgents (the canonical pi agent
  infrastructure repository); ContextHub retains no copy of the tooling.
- Generated artefacts (`test_churn_report.md`, `test_churn_data.json`) are no
  longer committed to ContextHub; the skill generates them on demand for any
  project root.
- `CHANGELOG.md` is owned by the ship skill's release pipeline and is **not**
  hand-edited as part of this migration; the release process produces the
  changelog entry.

## Original description and acceptance criteria (context)

The following is the original `WL-0MUZRNLFT004H8XJ` brief, carried over for
provenance.

### Problem statement

Quantitatively evaluate whether test-driven development (TDD) has delivered
value by measuring how often existing test code is rewritten (modified with
both additions and deletions) when new features are added. Focus specifically
on changes to existing tests — not new test additions — to distinguish between
test evolution and test rework.

### Acceptance criteria

1. **Primary metric delivered** — report the proportion of test code rewritten
   (lines deleted + lines re-added in existing test files) during feature
   commits.
2. **Multi-angle analysis delivered** — at least 8 distinct analyses covering
   different dimensions.
3. **TDD value assessment delivered** — a clear interpretation of what the data
   suggests.
4. **Methodology documented** — scope, definitions and limitations stated.
5. **All project test suites pass** — no regressions from the analysis tooling.

### Constraints

- Mainline (`dev`) only, non-merge commits.
- "Rewrite" = an existing test file modified with both additions and
  deletions.
- New test files are excluded from rewrite counts.

The corrected implementation and metric definitions are documented in
[test-churn-skill-reference.md](test-churn-skill-reference.md); the original
ContextHub figures (e.g. a 46.2% / 21.1% rewrite ratio and a temporal rate
above 100%) were produced by the defective tooling and are superseded by the
corrected figures.
