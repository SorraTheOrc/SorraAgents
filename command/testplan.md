---
description: Design an automated end-to-end test plan and create test-case beads
tags:
  - workflow
  - testplan
  - qa
agent: probe
---

You are helping the team design a **comprehensive automated test plan** that focuses on **end-user experience** and full feature fidelity. The plan must produce actionable test cases and create them as child beads under the designated test implementation bead.

## Quick inputs

- The user should run this as `/tstplan <bd-id>`.
  - Use $1 as the initial Beads ID (first argument).
  - $ARGUMENTS contains all arguments passed to the command.
  - This ID will be either the ID of a **Docs:** task or the ID of its parent feature. You will need both IDs, so fetch the parent/child as needed.
- No other options/arguments are supported.
  - If $1 is empty/undefined, ask for the missing id and stop.
  - If additional arguments are provided (e.g. `$2` exists), ask the user to re-run with exactly one argument and stop.
- This issue ID corresponds to either the feature to be tested, or it is the test implementation bead itself, which will be a child of the feature bead.
- You will need both the feature bead and the test implementation bead. If you cannot identify both then report this to the user and ask them to confirm they have run the `/plan` command first.
- If $1 is empty, print: "I cannot parse the issue id from your input '$ARGUMENTS'" and ask the user for a valid bead id in your first interview question.

## Argument parsing

- Pattern: If the raw input begins with a slash-command token (a leading token that starts with `/`, e.g., `/tstplan`), strip that token first.
- The first meaningful token after any leading slash-command is available as `$1` (the first argument). `$ARGUMENTS` contains the full arguments string (everything after the leading command token, if present).
- This command expects a single beads id as the first argument. Validate that `$1` is present and that `$2` is empty; if not, ask the user to re-run with a single bead id argument.

## Hard requirements

- Optimize for **user-visible behaviour**: tests must validate the feature exactly as a user would experience it (happy paths, edge cases, errors, accessibility, localization, rollout/flags).
- Favor **automated** coverage first (unit → integration → end-to-end); include minimal manual checks only when automation is impractical, and note a plan to automate later.
- Plan for both manually run tests (during development) and automated tests (via CI using GitHub actions).
- Use an interview style: concise, high-signal questions grouped to a soft-maximum of three per iteration.
- Do not invent requirements, commitments (dates), or owners — propose options and ask the user to confirm.
- Respect ignore boundaries: do not include or quote content from files excluded by `.gitignore` or any OpenCode ignore rules.
- If the user indicates uncertainty, add clarifying questions rather than guessing.

## Seed context

- Read `docs/` (excluding `docs/dev`), `README.md`, and other high-level files for context.
- Fetch and read the feature bead details using beads CLI: `bd show <beadId> --json` and treat the bead description, acceptance criteria, and any referenced artifacts as authoritative seed intent.
- Ensure you have identified the **test implementation bead** to use for creating test-case children (see Quick Inputs, above).
- Prepend a short “Seed Context” block to the interview that includes the fetched bead title, type, current labels, one-line description, and whether a test implementation bead was found or selected.

## Process (must follow)

1) Fetch & summarise (agent responsibility)

- Run `bd show <beadId> --json` and summarise the bead in one paragraph: title, type (epic/feature/task), headline, acceptance criteria, and any existing plan/milestone links.
- Read any PRD or plan linked in this bead or its parents to extract key user flows, acceptance criteria, and constraints.
- Read the documentation identified in the sibling bead which focuses on documentation for this feature.
- Identify the **test implementation bead** id to use for creating test-case children (see Quick Inputs, above). If ambiguous, stop and ask for clarification, asking the user to confirm they ran the `/plan` command, before proceeding.

2) Interview

In interview iterations (≤ 3 questions each), gather what is needed to produce an actionable automated test plan. Focus on:

- Primary user journeys and personas; platforms/locales/feature-flag states to cover.
- Data setup: fixtures, seeded data, and external integrations (APIs, auth, payments, storage, queues). Identify what can be mocked vs. must be real.
- Non-functional expectations: latency budgets, throughput, resilience/failover, error messaging quality.
- Observability: required metrics, logs, traces to assert during tests.
- Rollout/guardrails: flags, kill-switch behaviour, migration/backfill steps, compatibility with previous versions.
- Accessibility/usability requirements (keyboard, screen readers, color contrast) and security/permissions/abuse cases.

Keep asking questions until the test plan is clear.

3) Propose test plan (agent responsibility + user confirmation)

- Produce a draft test plan that includes:
  - **Scope & Out-of-Scope** (concise bullets)
  - **Test Matrix** (personas/platforms/locales/feature-flag states)
  - **Test Cases** grouped by category: happy path, edge cases, failure handling, accessibility/usability, security/permissions, performance/responsiveness, telemetry/analytics, regression/compatibility.
  - Each test case must specify: Short title (canonical, ≤ 7 words), Purpose, Preconditions/Data, Steps, Expected Results, Automation Level (unit/integration/e2e/manual), Tool/Framework, Mocking vs. real services, and Coverage tags (e.g., `a11y`, `l10n`, `perf`).
  - **Test Data & Fixtures**: how data is created/reset; idempotence rules; anonymization if needed.
  - **Environments & Execution**: how to run locally/CI; required secrets; parallelism considerations; flake mitigations (retries/timeouts/recordings).
  - **Exit/Entry Criteria**: when the test suite is considered complete and when failures block release.
- Present the draft as a numbered list of test cases and supporting sections. Ask the user to: accept, edit titles/scopes, reorder, split/merge cases, or adjust automation levels.
- If the user requests changes, iterate until the test plan is approved.

4) Automated review stages (must follow; no human intervention required)

After draft approval, run five review iterations. Each review MUST provide a new draft if any changes are recommended and print: "Finished <Stage Name> review: <brief notes of improvements>" (or "no changes needed").

  1) Completeness review
     - Ensure every section exists (Scope, Test Matrix, Test Cases, Data/Fixtures, Environments, Exit/Entry Criteria).
     - Add placeholders only when obvious; otherwise record Open Questions.

  2) Coverage & user-focus review
     - Confirm end-user experience coverage: happy path, error states, accessibility, localization, permissions, rollback/flags.
     - Flag missing negative cases or platform/state gaps as Open Questions.

  3) Sequencing & dependency review
     - Check that test data/setup dependencies are coherent; detect cycles or missing prerequisites.
     - Ensure ordering supports reliable automation (e.g., migrations before e2e). Record uncertainties.

  4) Automation & tooling review
     - Validate that each case has an automation level and tooling; suggest least-coupled approach (unit where possible, e2e where necessary).
     - Note flakiness risks and mitigation (retries, timeouts, recordings) without changing intent.

  5) Polish & handoff review
     - Tighten wording, standardize titles (canonical, stable), and ensure the plan is copy-pasteable and rerunnable without duplication.

5) Create beads (agent)

- Determine the **test implementation bead** (`<testBeadId>`). If not confidently resolved, stop and ask the user.
- Create child beads (type: task) for each approved test case under `<testBeadId>`:
  - `bd create "Test Case: <Short Title>" --description "<Test case details including Preconditions/Data, Steps, Expected Results, Automation Level, Tooling, Mocking vs real, Coverage tags, ## Acceptance Criteria>" --parent <testBeadId> -t task --json --labels "test-case,test" --priority P1 --assignee Probe --validate`

  Note: this command includes an explicit assignee following repository conventions: tests are assigned to Probe. If the test case represents a different type (e.g., performance or infra), override assignee after creation with `bd update`.
- Idempotence rules:
  - Before creating, fetch existing children: `bd list --parent <testBeadId> --json` and reuse any child whose canonical title matches the planned test case.
  - Do not create duplicates; update descriptions instead when reuse occurs.
- Update the **test implementation bead** description to add or replace a well-marked "Test Plan" block that lists the approved test cases, their bead ids (created/reused), automation levels, and Open Questions.
- If helpful, add dependencies to reflect ordering (e.g., e2e blocked by fixtures): `bd dep add <DependentTestCaseId> <PrereqTestCaseId>`.

## Traceability & idempotence

- Re-running `/testplan <bd-id>` should not create duplicate test-case beads or duplicate "Test Plan" blocks.
- If the command makes changes, include a changelog block in the test bead (labelled "Test Plan: changelog") summarising actions and timestamps.

## Editing rules & safety

- Preserve author intent; where uncertain, create Open Questions rather than guessing.
- Keep changes minimal and conservative; do not alter existing acceptance criteria without confirmation.
- Respect `.gitignore` and other ignore rules when scanning files for context.
- **Beads validation**: ensure created test-case descriptions include a `## Acceptance Criteria` section to satisfy validator expectations.
- If any automated step fails or is ambiguous, surface an explicit Open Question and pause for human guidance.

## Finishing steps (must do)

- Add label to the test implementation bead: `bd update <testBeadId> --add-label "Status: Test Plan Created" --json` (leave existing labels intact).
- Run `bd sync` to sync bead changes.
- Run `bd show <testBeadId>` (not --json) to show the entire bead.
- End with: "This completes the Test Plan process for <testBeadId>".

