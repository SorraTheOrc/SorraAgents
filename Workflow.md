# PRD-Driven Workflow (Human + Agent Team)

## Introduction

This document describes a issue and PRD-driven workflow for building new products and features using a mix of human collaborators (PM / Producer) and agent collaborators (PM, coding, documents, test, shipping). The workflow emphasizes:

- A single source of truth in the repo (PRDs, issues, code, release notes)
- Clear handoffs and auditability (who decided what, and why) (see wf-ba2.8)
- Keeping `main` always releasable via feature flags and quality gates

By default, this workflow is tool-agnostic about the implementation stack (language, framework, test runner). It assumes this repository is the system of record.

## Prerequisites

You need the following available to follow this workflow end-to-end:

- Repo access with permission to create/edit files.
- A shared issue tracking mechanism.
  - In this repo, use `bd` (beads) for issue tracking.
- An agreed PRD template.
  - In this repo, use the OpenCode command `/prd` to create an PRD via Agent based interview (stored under `.opencode/command/prd.md`). (see wf-ba2.3)
- A minimal quality bar for “releasable `main`” (tests/coverage gates, feature-flag policy, and review policy).

## Steps

The following steps describe the end-to-end workflow for delivering a new feature or product. A product is a collection of features that deliver end-user value. A feature is a discrete unit of user value that can be delivered independently (e.g., a new command, UI screen, or API endpoint). An epic is a collection of related features that deliver a larger user story.

In summary the workflow is:

- Define the project: state scope, measurable success metrics, constraints, and top risks.
- Define Milestone(s): map end-to-end user outcomes, milestones (M0/M1), and cross-functional owners.
- Decompose each epic into features: for each feature specify acceptance criteria, minimal implementation, and the prototype or experiment to validate assumptions.
- Implement each milestone/epic as vertical, end-to-end slices: deliver the smallest end-to-end feature (code, tests, infra, docs, observability) per iteration.

In more detail, the steps are:

### Project/Feature Definition

Define the bounding intent for the work: scope, measurable success metrics, constraints, and the top risks. Capture a one-paragraph project summary at the top of the PRD and include:

- **Success signals:** precise, automatable metrics and baseline measurements to evaluate the outcome.
- **Constraints:** timeline, budget, compatibility, and regulatory limits that affect tradeoffs.
- **Top risks:** short list of the highest-impact uncertainties and a proposed first-mitigation.
- **Status Label:** `idea`, `prd_complete`

Agent Commands:

1. Create initial tracking bead: `/intake <Project Title>`
2. Create PRD via interview: `/prd <Bead ID>`

Summary: a clear, testable project definition that guides epics and prioritization.

### Define Milestones

Map the end-to-end user outcomes into one or more master epics that represent deliverable milestones (for example `milestone:M0`, `milestone:M1`). For each master epic record cross-functional owners and high-level milestones.

- **Outcome map:** list the user flows the epic must enable and the acceptance criteria at the epic level.
- **Milestones:** define at least one short feedback milestone (M0) and one fuller delivery milestone (M1).
- **Ownership:** assign an owner for PM, engineering, infra, security and UX per epic.
- **Status Label:** `milestones_defined`

Agent Commands:

1. Decompose the PRD into master epic(s): `/milestones <bead-id>`

Summary: master epics turn the project definition into parallel, owned workstreams.

### Feature Decomposition

Break each epic into discrete features: each feature should have a concise acceptance criteria statement, a minimal implementation plan, and—where applicable—a prototype or experiment to validate assumptions.

- **Acceptance:** expressable, pass/fail acceptance criteria suitable for automated tests or a short manual checklist.
- **Prototype:** when assumptions are risky, describe a lightweight experiment (fake-API, mock UI, A/B) and success thresholds.
- **Taskization:** create `bd` tasks for implementation, infra, docs, and tests; link to the PRD and epic.
- **Status Label:** `plan_complete`

Tackle a single Milestone/Epic at a time. Do not attempt to decompose more than one epic at a timte. This allows each milestone to feed into the next, correcting any poor assumptiosn made in previous steps.

Agent Commands:

1. Decompose epics into features and tasks: `/plan <Epic ID>`

Summary: features make epics executable and testable in small increments.

### Feature Implementation

Implement each feature one at a time. Each issue will have a set of child tasks for (at least) implementation, infra, docs, and tests. Workthrough each feature as a vertical slice that delivers end-to-end user value.

- **Complete slice:** include code, unit/integration tests, CI configuration, deployment config, runtime observability (metrics/logs), and a rollback/feature-flag plan.
- **Demo-ready:** each slice should be deployable to a staging environment and demoable with a short script.
- **Status Label:** `in_progress`

Agent Commands:

1. For the test issue, generate test plan: `/testplan <Issue ID>`
2. For the docs issue, generate user documentation: `/doc <Issue ID>`
3. Implement the feature and tests: `implement <Issue ID>`

Summary: vertical slices reduce integration risk and make progress visible.

### PR Review (Human Step)

Review each Pull Request for completeness, correctness, and adherence to the PRD acceptance criteria. Use a mix of automated checks and human review to ensure quality.

- **Automated checks:** ensure all tests pass, coverage gates are met, and lint/build checks succeed.
- **Human review:** verify the implementation meets the acceptance criteria and includes necessary documentation and observability.
- **Merge:** once approved, merge the PR and deploy to staging/production as per the deployment plan.
- **Status Label:** `in_review`

### Cleanup (Agent Step)

After merging the PR, clean up the repository by closing the bead, removing temporary files, checking out and updating `main`, and deleting local and remote branches.

- **Status Label:** `done`

Agent Commands:

1. `/cleanup`
