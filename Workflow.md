# PRD-Driven Workflow (Human + Agent Team)

## Introduction

This document describes a issue and PRD-driven workflow for building new products and features using a mix of human collaborators (PM / Producer) and agent collaborators (planning, coding, documents, test, shipping). The workflow emphasizes:

- A single source of truth about the intent and steps taken (the Worklog)
- A single source of truth about the implementation (the code)
- A clear mapping from intent to implementation (PRD -> Epics -> Features -> Code)
- Small, vertical slices of end-to-end value delivered frequently
- A rapid iterative workflow that leverages agentic development and human led feedback loops
- Clear handoffs and auditability (who decided what, and why)
- Keeping `main` always releasable via feature flags and quality gates

## Prerequisites

You need the following available to follow this workflow end-to-end:

- Repo access with permission to create/edit files, either in a shared repo or in a fork.
- A shared issue tracking mechanism that is optimized for humans and agents alike (this document assumes Worklog is the tool of choice).
- Access to the agentic development environment with the necessary permissions and configurations.

## Project Decomposition

It is important to understand that unlike traditional workflows where work is often done in large batches, this workflow emphasizes small, vertical slices of end-to-end value delivered frequently. This approach reduces integration risk, makes progress visible, and allows for rapid feedback and iteration. It assumes an agent driven rapid tune-build-test-deploy cycle, often measured in hours or days rather than weeks or months.

The workflow focuses on clarity where it is needed to enable agents to deliver value autonomously, while keeping forward planning and coordination manageable for human collaborators. The breakdown below is deliberately modeled against traditional software development concepts to make it easier for human collaborators to understand and manage. However, despite how it may look at first glance, this is not a traditional waterfall approach. Instead, it is a flexible framework that allows for rapid iteration and adaptation as new information emerges during development.

The goal is to create a clear mapping from high-level intent (the PRD) to low-level implementation (code) through a series of well-defined steps and artifacts. That allow for ambiguities at the higher, mostly human-led levels to be resolved through rapid iteration and feedback at the lower, mostly agent-led levels.

- A **Project** is the overall initiative, defined by a PRD that captures scope, success metrics, constraints, and risks. A project consistes of one or more milestones.
- A **Milestone** is a major deliverable within a project. Milestones map to end-to-end user experiences and have cross-functional ownership. A milestone consists of one or more epics.
- An **Epic** is a large body of work that can be broken down into one or more new features or feature enhancements. Features represents a significant user outcome within a milestone and are often cross-functional.
- A **Feature** is a discrete unit of user value that can be delivered independently (e.g., a new command, UI screen, or API endpoint). Features consist of one or more tasks and are sometimes cross-functional.
- A **Task** is a specific task that needs to be completed to deliver a feature (e.g., implementation, testing, documentation). Tasks are the smallest unit of work and are typically assigned to a single individual or agent.

Each of these levels must be clearly documented in the Worklog with appropriate links and references to ensure traceability from project definition to code implementation. Each level should also have clear acceptance criteria to validate completion and success.

The tooling that accompanies this workflow ensures that agents keep copious notes about decisions made, code generated, and tests run. This creates an auditable trail that can be reviewed by human collaborators to ensure alignment with the original intent and, perhaps more importantly, enable agents to rebuild required context quickly when switching between tasks. That is the Worklog becomes the agents "water cooler" where they can pick up context and understand the rationale behind decisions.

## Steps

This section describes the detailed steps of the workflow, from project definition to feature implementation and review. In the earlier steps the emphasis is on high-level planning and clarification with the human being the holder of truth, while later steps focus on detailed implementation and validation, with agents becoming increasingly autonomous.

First is a high-level summary of each step, followed by a more detailed breakdown with specific actions and agent commands.

- Record the project idea: create a tracking work-item to capture the project title and basic info; humans simply drop their thoughts into the work-item; mark the status `idea`.
- Expand the idea into a high-level project overview; humans refine and clarify the idea and its motivations; Agents assist with structure and authoring through an interview process; mark status `intake_complete`.
- Frame the project with a PRD: capture scope, success signals, constraints, and top risks; Agents assist with authoring through an interview process, humans bring creativity and the domain knowledge; mark status `prd_complete`.
- Define milestone epics: map end-to-end outcomes, owners, and epic-level acceptance criteria; Agents build the plan with guidance through an interview process, humans bring clarity and the domain knowledge; mark status `milestones_defined`.
- Decompose each epic into sub-epics and features with acceptance criteria, a minimal plan, and prototype/experiment design where needed; Agents build the plan with validation from human collaborators; mark status `plan_complete`.
- Implement one feature at a time: Agents write code, tests and documentation with human validation through acceptance testing; move through `in_progress` → `in_review` → `done`.

During execution, work proceeds one feature at a time. During this process new features may be added, removed, or reprioritized based on feedback and learning. This flexible approach allows the team to adapt to changing requirements and ensures that the most valuable work is always being prioritized. When new features are identified they pass through the stages `idea` → `intake_complete` → `plan_complete` before being slotted into the implementation queue. That is they (usually) skip the PRD and milestone definition steps.

The outputs of each of these steps are recorded in the Worklog as linked work-items, creating a clear trace from project definition to code implementation. Each step includes specific agent commands to facilitate the process, ensuring that both human and agent collaborators can work together effectively.

Each step also provided a clear "go-no-go" decision point, allowing human collaborators to review and validate the work before proceeding to the next stage. This ensures that the project remains aligned with its original intent and can adapt to new information as it emerges during development.

Each step is described in more detail below. With respect to the useful commands those that start with a `/` are agent commands that can be issued in the agentic development environment, while those that do not are commands to be run in the shell, whether that be by human or agent collaborators.

### Idea Capture

Record the initial feature or project idea in a tracking work-item. This work-item serves as the central hub for all subsequent planning and execution activities. This step is typically initiated by a human collaborator who has identified a new opportunity or requirement. The more details provided at this stage, the easier it will be to refine and expand the idea in subsequent steps. On the other hand, the goal is to lower the barrier to entry, so even a brief note is sufficient to get started.

- **Motivation:** capture the initial thought or requirement that sparked the idea, as quickly as possible.
- **Acceptance Criteria:** a single work item with idea title and a brief description of the idea.
- **Status Tag:** `idea`
- **Useful Commands:**
  - `wl create -t "<Project Title>" -d "<Brief Description>"`
  - optionally add a parent work-item ID to link to a larger initiative (`--parent <Parent Work-Item ID>`).
- **Next Step:** If the idea is a project go to the `Project Definition` step; if a smaller feature go to `Feature Intake` step.

### Feature Intake (for small features)

For smaller features that do not require a full PRD, expand the initial idea into a more detailed feature intake work-item. This step captures the motivation, user impact, and high-level requirements for the feature. Agents assist with structuring and authoring the intake through an interview process, while humans provide the creativity and domain knowledge.

- **Motivation:** describe the problem being solved and why it matters to users.
- **Acceptance Criteria:** work-item contains a clear understanding of the feature's purpose and requirements.
- **Status Tag:** `intake_complete`
- **Useful Commands:**
  - `/intake <Work-Item ID>`
- **Next Step:** proceed to the `Define Milestones` step.

### Project Definition (for larger projects)

Expand the initial idea into a comprehensive Project Requirements Document (PRD). This document captures the scope, success metrics, constraints, and top risks associated with the project. Agents assist with structuring and authoring the PRD through an interview process, while humans provide creativity and domain knowledge.

- **Motivation:** describe the problem being solved and why it matters to users.
- **Acceptance Criteria:** work-item contains a clear understanding of the feature's purpose and requirements.
- **Status Tag:** `intake_complete`
- **Useful Commands:**
  - `/intake <Work-Item ID>`
- **Next Step:** proceed to the `Define Milestones` step.

- **Success signals:** precise, automatable metrics and baseline measurements to evaluate the outcome.
- **Constraints:** timeline, budget, compatibility, and regulatory limits that affect tradeoffs.
- **Top risks:** short list of the highest-impact uncertainties and a proposed first-mitigation.
- **Status Tag:** `intake_complete`

Agent Commands:

1. Create initial tracking work-item: `/intake <Project Title>`
2. Create PRD via interview: `/prd <work-item ID>`

Summary: a clear, testable project definition that guides epics and prioritization.

### Define Milestones

Map the end-to-end user outcomes into one or more master epics that represent deliverable milestones (for example `milestone:M0`, `milestone:M1`). For each master epic record cross-functional owners and high-level milestones.

- **Outcome map:** list the user flows the epic must enable and the acceptance criteria at the epic level.
- **Milestones:** define at least one short feedback milestone (M0) and one fuller delivery milestone (M1).
- **Ownership:** assign an owner for PM, engineering, infra, security and UX per epic.
- **Status Tag:** `milestones_defined`

Agent Commands:

1. Decompose the PRD into master epic(s): `/milestones <work-item-id>`

Summary: master epics turn the project definition into parallel, owned workstreams.

### Feature Decomposition

Break each epic into discrete features: each feature should have a concise acceptance criteria statement, a minimal implementation plan, and—where applicable—a prototype or experiment to validate assumptions.

- **Acceptance:** expressable, pass/fail acceptance criteria suitable for automated tests or a short manual checklist.
- **Prototype:** when assumptions are risky, describe a lightweight experiment (fake-API, mock UI, A/B) and success thresholds.
 - **Taskization:** create `wl` tasks for implementation, infra, docs, and tests; link to the PRD and epic.
- **Status Tag:** `plan_complete`

Tackle a single Milestone/Epic at a time. Do not attempt to decompose more than one epic at a timte. This allows each milestone to feed into the next, correcting any poor assumptiosn made in previous steps.

Agent Commands:

1. Decompose epics into features and tasks: `/plan <Epic ID>`

Summary: features make epics executable and testable in small increments.

### Feature Implementation

Implement each feature one at a time. Each issue will have a set of child tasks for (at least) implementation, infra, docs, and tests. Workthrough each feature as a vertical slice that delivers end-to-end user value.

- **Complete slice:** include code, unit/integration tests, CI configuration, deployment config, runtime observability (metrics/logs), and a rollback/feature-flag plan.
- **Demo-ready:** each slice should be deployable to a staging environment and demoable with a short script.
- **Status Tag:** `in_progress`

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
- **Status Tag:** `in_review`

### Cleanup (Agent Step)

After merging the PR, clean up the repository by closing the work-item, removing temporary files, checking out and updating `main`, and deleting local and remote branches.

- **Status Tag:** `done`

Agent Commands:

1. `/cleanup`
