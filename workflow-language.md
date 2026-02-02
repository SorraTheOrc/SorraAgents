# Machine-Readable Workflow Language (Draft)

Design a small, deterministic, agent-friendly language for describing stateful work-item workflows. The goal is to let humans author a concise spec that machines (human- or AI-driven agents) can validate and execute (e.g., bots, CLIs, CI checks). Agent capabilities are defined externally (see `agent/`), and workflows reference roles that engines map to agents or humans.

## Goals

- Encode valid state transitions for work items with both `status` and `stage` dimensions.
- Declare which commands are allowed in each state combo and what state changes they perform.
- Be easy to parse (JSON first; YAML allowed as a superset), validate, and version.
- Make invariants explicit so tools can enforce them before/after commands run.

## Core Concepts

- **Work item**: an entity with `id`, `status`, `stage`, optional metadata.
- **Status**: coarse lifecycle (`open`, `in_progress`, `blocked`, `closed`).
- **Stage**: finer-grained phase (`idea`, `intake_completed`, `prd_completed`, `milestones_defined`, `plan_completed`, `in_progress`, `in_review`, `done`).
- **State tuple**: the pair `(status, stage)`; transitions are defined on tuples, not on either dimension alone.
- **Command**: a named action that can run only from specified state tuples and yields a new tuple if it succeeds.
- **Invariant**: a boolean rule that must hold before and/or after a command (e.g., required fields, approvals, links).

## File Format

- Canonical: JSON. YAML is permitted for authoring but must round-trip to the JSON model.
- Suggested file name: `workflow.json` (or `workflow.yaml`).
- Required top-level keys:
  - `version`: semantic version of the spec, e.g., `1.0.0`.
  - `metadata`: `name`, `description`, `owner`, optional `links`, required `roles` (list of role identifiers such as `Producer`, `PM`, `Developer`, `TechnicalWriter`, `Tester`, `DevOps`). The executor maps roles to concrete humans or agents.
  - `status`: ordered list of allowed statuses.
  - `stage`: ordered list of allowed stages.
  - `states`: optional map of friendly aliases to `{status, stage}`.
  - `invariants`: list of named invariant definitions.
  - `commands`: map of command definitions keyed by command name.

## Command Definition Shape

Each command entry uses the following fields:

- `description`: short human-readable summary.
- `from`: list of allowed source state tuples. Each entry is either a state alias or `{status, stage}`.
- `to`: target state tuple (alias or `{status, stage}`) applied when the command succeeds.
- `actor`: who runs the command; must reference a role declared in `metadata.roles` (e.g., `Developer`, `Reviewer`, `PM`). The workflow engine maps roles to humans or agents and supplies tools/models/guardrails accordingly.
- `pre`: list of invariant names that must pass before the command may run.
- `post`: list of invariant names that must pass after the transition is applied.
- `inputs`: optional schema-like object describing required arguments (names, types, required/optional, enums).
- `prompt_ref`: optional path to a versioned prompt template (e.g., `prompts/command-name.md`); template variables must correspond to `inputs` for validation.
- `effects`: optional additional side effects to assert/emit (e.g., tags to add/remove, events to emit, audit tags such as prompt hash, `agent_id`, chosen model, response IDs, trace/event hooks). These are descriptive; executors decide how to implement.
- Naming convention: commands should be imperative verbs describing the action (e.g., `intake`, `plan`, `approve`), not the resulting state.

## Invariant Definition Shape

- `name`: unique identifier.
- `description`: human-readable intent.
- `when`: `pre` or `post` or both.
- `logic`: machine-checkable rule. Keep this declarative to allow multiple executors (e.g., regex on descriptions/comments to assert required links, tags, approvals, PII bans, or human-approval-before-AI). Exact expression language is implementation-specific but must be documented alongside the workflow.

## Execution Semantics (per command attempt)

1. Confirm the work item is in a `from` state tuple.
2. Evaluate `pre` invariants; abort if any fail.
3. Perform command logic (outside this spec) and propose transition to `to`.
4. Apply the new tuple.
5. Evaluate `post` invariants; if any fail, roll back to the prior tuple and report the failure.
6. Record the attempted transition (command name, actor, timestamp, outcome) as a comment on the work item for auditability.

Assignment is engine-defined. A typical pattern: when a work item enters a new state, the engine assigns the item per its controller policy; while a command runs, temporary ownership belongs to the command `actor` role; on success or failure, ownership returns per the engine's policy and the audit comment is appended.

For AI-driven commands, the engine resolves the `actor` role to an agent and supplies tools/models/guardrails. The recorded comment should include audit details such as prompt reference (or hash), `actor` role, resolved agent, chosen model, response IDs, and trace/event hooks if available.

## Example (YAML for readability; JSON is canonical)

```yaml
version: 1.0.0
metadata:
  name: prd_workflow
  description: PRD-driven work item states and commands
  owner: workflow-team
  roles: [Producer, PM, Developer, TechnicalWriter, Tester, DevOps]
status: [open, in_progress, blocked, closed]
stage:
  - idea
  - intake_completed
  - prd_completed
  - milestones_defined
  - plan_completed
  - in_progress
  - in_review
  - done
states:
  idea: { status: open, stage: idea }
  intake: { status: open, stage: intake_completed }
  prd: { status: open, stage: prd_completed }
  milestones: { status: open, stage: milestones_defined }
  plan: { status: open, stage: plan_completed }
  building: { status: in_progress, stage: in_progress }
  review: { status: in_progress, stage: in_review }
  shipped: { status: closed, stage: done }
invariants:
  - name: requires_prd_link
    description: PRD URL must be present before planning
    when: pre
    logic: regex(description, "PRD:\s*https?://")
  - name: requires_tests
    description: Test plan link must exist before entering review
    when: pre
    logic: regex(description, "Test Plan:\s*https?://")
  - name: requires_approvals
    description: At least one reviewer approved before closing
    when: post
    logic: regex(comments, "Approved by\\s+\\w+")
commands:
  intake:
    description: Capture intake details
    from: [idea]
    to: intake
    actor: PM
    inputs:
      summary: { type: string, required: true }
    effects:
      add_tags: [intake]
  author_prd:
    description: Produce PRD draft and link it
    from: [intake]
    to: prd
    actor: PM
    pre: [requires_prd_link]
  define_milestones:
    description: Break PRD into milestones/epics
    from: [prd]
    to: milestones
    actor: PM
  plan_features:
    description: Decompose milestones into planned features
    from: [milestones]
    to: plan
    actor: Architect
  start_build:
    description: Begin implementation on a planned feature
    from: [plan]
    to: building
    actor: Developer
    effects:
      add_tags: [in_progress]
  block:
    description: Mark item as blocked with reason
    from:
      - { status: in_progress, stage: in_progress }
      - { status: open, stage: plan_completed }
    to: { status: blocked, stage: in_progress }
    actor: Developer
    inputs:
      reason: { type: string, required: true }
  unblock:
    description: Resume work after resolving block
    from: [{ status: blocked, stage: in_progress }]
    to: building
    actor: Developer
  submit_review:
    description: Send for code/feature review
    from: [building]
    to: review
    actor: Developer
    pre: [requires_tests]
  approve:
    description: Approve and mark as done
    from: [review]
    to: shipped
    actor: Producer
    post: [requires_approvals]
  reopen:
    description: Reopen a closed item for follow-up
    from: [shipped]
    to: plan
    actor: Producer
```

## Validation Rules

- All `from`/`to` tuples must use declared `status` and `stage` values (or aliases).
- Every command must have at least one `from` state and exactly one `to` state.
- Invariants referenced in `pre`/`post` must exist.
- No unreachable states: every non-initial state should be the `to` of some command.
- No dead-end states unless explicitly marked terminal (e.g., `shipped`).
- Optional: enforce acyclic progression via stage ordering if desired.

## Extensibility

- Add `roles` or `permissions` per command (who may execute).
- Add `notifications` under `effects` to emit events.
- Add `guard` expressions directly on `from` entries for richer branching.
- Version the schema (`version`) and use semver to signal compatibility.

## Suggested Tooling

- Provide a JSON Schema for editors/CI to validate workflow files.
- Supply a reference interpreter that enforces `pre`/`post` invariants and logs transitions.
- Generate human-readable diagrams (state graph) from the machine-readable file for reviews.
- Emit traces/events for AI-executed commands (including `actor` role, resolved agent, prompt hash/ref, model, response IDs) to aid debugging and compliance.
