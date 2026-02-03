# Product Requirements Document

## Introduction

### One-liner

Automated PM Agent (APMA): a non-destructive discovery, definition, and delegation engine that turns high-level briefs into auditable, executable work plans and progress reports.

### Problem statement

Teams often lose time on ambiguous briefs, uneven task definition, and unclear delegation; this slows delivery and creates risk. There is no lightweight, auditable automation that discovers candidate work and composes unambiguous child work-items with acceptance criteria. There is also no widely adopted tool that produces delegation plans and progress reports while operating within a clear role boundary (APMA does not author production code).

### Goals

* Produce complete, auditable plans from briefs with clear acceptance criteria (target: 99% completeness for acceptance criteria in generated child work-items). Planning outputs require explicit Producer (human) sign-off before they are considered approved plans.
* Generate delegation recommendations and actionable delegation artifacts after a plan is approved; delegation of an approved plan may proceed without additional Producer sign-off.
* Create draft epics and child work-items in `wl` with proposed agent-groups/roles for human review.
* Provide on-demand discovery and read-only reporting; support CI dry-run checks for added context.
* Maintain an auditable comment trail for all proposed delegations and recommendations.
* Coordinate review of delivered artifacts and manage approvals to commit/push: APMA facilitates artifact review, captures explicit approvals, and produces auditable delegation artifacts so specialist agents or humans may perform commits/pushes when authorized.

### Non-goals

* APMA is not responsible for performing code changes, building tests, or making repository code commits. Repository mutations are the responsibility of specialist agents or humans with the appropriate roles. APMA may create or update `wl` work-items and author design or planning documents, but it will not itself author production code or run builds as part of the MVP.
* The MVP will not attempt to integrate with a wide set of external tools beyond `wl` and CI systems for read-only checks.

## Users

### Primary users

* Producer: submits briefs, validates and signs off plans, and coordinates delegation.
* Engineer / sub-agent: receives child work-items with acceptance criteria and executes delegated work.
* Stakeholder / approver: monitors progress, reviews delegation rationale, and verifies outcomes.
* Safety officer / compliance: ensures APMA operates within role boundaries and that audit trails exist.

### Secondary users (optional)

* Release managers and operations staff (for later phases when rollout and runbooks are required).

### Key user journeys

* Idea → Intake → Plan: A Producer has an idea and submits an intake brief → APMA discovers candidate items, scores readiness, and creates a draft plan with measurable acceptance criteria → Success when the Producer signs off the plan.
* Delegate → Execute: After Producer approval, APMA produces delegation recommendations (rationale and suggested agent-groups/roles) and generates actionable delegation artifacts; success when delegates acknowledge and begin work.
* Review → Approve to Commit: APMA coordinates review of delivered artifacts (designs, documentation, build outputs), captures explicit approvals required to authorize commits/pushes, and records auditable approval metadata. Success when approvals are recorded and specialist agents or humans perform commit/push actions according to policy.
* Status → Report: Stakeholder requests status → APMA returns an on-demand report summarizing open/blocked items, percent complete, and top risks → Success when report reflects current `wl` state and recent activity.

## Requirements

### Functional requirements (MVP)

* Parse and ingest a project brief and identify candidate work-items.
* Score candidate items for readiness and rank recommendations.
* Create a draft epic and draft child work-items in `wl` including measurable acceptance criteria and suggested agent-groups/roles.
* Produce a delegation plan with a rationale for each proposed assignment and record it as `wl` comments and/or attached artifacts.
* Run read-only CI checks (lint/test dry-run) and attach results as context to recommendations.
* Provide on-demand reporting API/command that returns: current stage, recent activity, open/blocked items, percent complete, and top risks.
* Implement a version of the workflow documented in `~/.config/opencode/Workflow.md` so APMA's behavior matches the established agent/process expectations.
* Support a reviewed-artifact approval workflow: track delivered artifacts, surface review comments, capture approval decisions (Producer and other required roles), and attach approval metadata to delegation artifacts so downstream specialist agents can safely commit/push.

### Non-functional requirements

* Performance: end-to-end plan generation should complete in a human-usable timeframe (informal target: under 24 hours for non-trivial briefs; typical aim: minutes for small briefs). (KPI focus is accuracy/auditability rather than raw speed.)
* Reliability: operations that modify `wl` must be idempotent and include clear audit metadata (who/when/reason).
* Scalability: handle multiple briefs without cross-contamination of recommendations; sequential multi-tasking (processing briefs one-at-a-time) is acceptable for the MVP.
* Accessibility: generated artifacts (work-items, comments, reports) should be consumable via `wl` CLI and basic web UIs.

### Integrations

* Worklog (`wl`) — required: create and comment on epics and child work-items.
* CI systems — read-only dry-run support for linting and tests (MVP: prioritize GitHub Actions for dry-run integrations; other providers supported later).

### Security & privacy

Security note: APMA is read-only by default with respect to repository code changes; it does not author production code or run builds. Repository mutations (pushes, merges, commits) are the responsibility of specialist agents or humans with the appropriate roles and require explicit, auditable delegation.

Clarification on "approvals": this document uses the term only for two narrow, auditable actions:

* Producer plan sign-off: the Producer explicitly accepts a generated plan. A plan must be signed off by the Producer before it is considered "approved" and actionable for delegation.
* Approve-to-commit (artifact approval): explicit approval of delivered artifacts (designs, build outputs, documentation) that authorizes a downstream specialist agent or human to perform repository mutations (commit/push).

Important: APMA producing delegation recommendations, posting rationale, or creating draft `wl` work-items does NOT require approval and may occur without additional sign-off.

Approval metadata APMA must capture (minimal structured audit schema):

* `approver`: identity and role (e.g., `user:alice`, `role:Producer`)
* `approval_type`: `plan_signoff` | `approve_to_commit`
* `timestamp`: ISO 8601 UTC
* `artifact_id`: work-item id, document path, or build id the approval covers
* `artifact_version`: checksum, build id, or commit-hash the approval references
* `scope`: files, branches, or work-item ids included in the approval
* `decision`: `approved` | `rejected` | `conditional`
* `conditions`: short text or checklist when `conditional`
* `evidence`: links to CI runs, test reports, diffs, or artifacts
* `delegated_executor`: expected agent or agent-group to perform commit/push (optional)
* `audit_id`: internal immutable audit reference
* `ttl_or_expiry`: optional expiry timestamp
* `comments`: freeform reviewer notes

Storage & usage: APMA must attach a compact JSON approval record to the relevant `wl` work-item (as a comment or artifact) and include a brief human-readable summary for quick inspection. Approval records should be immutable once recorded and referenceable by `audit_id` for traceability.

Privacy note: Reports and work-items may include sensitive design or roadmap data; store and transmit only metadata necessary for delegation and redaction controls should be implemented by host environments.

## Release & Operations

### Rollout plan

* Alpha: internal testing with a small group of Producers and engineers using a staging `wl` instance and read-only CI runs.
* Beta: expand to a broader set of teams; collect acceptance and adjust delegation heuristics.
* GA: enable production `wl` and CI integrations; optionally enable role-based apply approvals once identity/permission integrations are in place.

### Quality gates / definition of done

* End-to-end demo: given a sample brief, APMA creates an epic and child work-items in `wl` with acceptance criteria and delegation rationale.
* Acceptance tests: automated checks that verify `wl` items are created as drafts, comments contain rationale, and CI dry-run attachments are present.
* Reviewed-artifact gate: acceptance tests must verify that approvals are captured before any commit/push delegation is executed and that approval metadata is auditable.
* Security review: confirmation that no unauthorized repository mutations occurred in the demo and audit metadata is present on all created `wl` artifacts.

### Risks & mitigations

* Risk: Incorrect or incomplete acceptance criteria leading to rework. Mitigation: require APMA to mark uncertain fields and surface recommended owner-review checkpoints; target 99% completeness KPI and flag low-confidence items.
* Risk: Accidental repository mutation if apply flows are misconfigured. Mitigation: enforce default read-only mode, require explicit approval flow for any apply actions, and log all delegation steps.
* Risk: Integration drift across CI providers. Mitigation: abstract CI interface; limit MVP to a small set of supported providers and require manual configuration per environment.

## Open Questions

* None at this time. The following have been decided for the MVP: Producer sign-off is required for plans; delegation of an approved plan may be automated; prioritize GitHub Actions for CI dry-runs; APMA proposes agent-groups/roles rather than individual assignees.
