---
description: Create an intake brief (Workflow step 1)
tags:
  - workflow
  - intake
agent: build
---

You are coordinating an intake brief as part of the [development workflow](docs/dev/Workflow.md).

## Description

You are authoring a new Bead issue that describeds a feature or a bug fix to be implemented. You will ensure that the details in the Bead issue are sufficient to allow a developer to complete the work. You will follow an interview-driven approach to gather requirements, constraints, success criteria, and related work.

## Quick inputs

- You _must_ be provided with a short intake phrase as $ARGUMENTS.
  - Example: `/intake As a product manager I need to add an onboarding tutorial so that new users complete setup faster`
- If $ARGUMENTS is empty, you must ask one brief question to obtain a brief description of the product/epic/feature before starting the interview

## Behavior

The command implements the procedural workflow below. Each numbered step is part of the canonical execution path; substeps describe concrete checks or commands that implementors or automation should run.

## Hard requirements:

- Use an interview style: concise, high-signal questions grouped to a soft-maximum of three per iteration.
- Do not invent requirements or constraints; if unknown, ask the user.
- Respect ignore boundaries: do not include or quote content from files excluded by `.gitignore` or OpenCode ignore rules.
- Prefer short multiple-choice suggestions where possible, but always allow freeform responses.
- If the user indicates uncertainty at any point, add clarifying questions rather than guessing.

## Process (must follow)

1. Gather context (agent responsibility)

- Read `docs/` (excluding `docs/dev`), `README.md`, and other high-level files for product context.
- Derive 2–6 keywords from the user's working title and early answers to guide repository and Beads searches.
- Use derived keywords to search source and docs for related artifacts and to surface possible duplicates:
  - Scan `docs/`, `README.md`, and `src/` (or equivalent top-level code folders).
  - Use ripgrep (`rg`) where available. If `rg` is unavailable, use a best-effort scan and ask the user to install `rg` for future runs.
- Search Beads for related issues (`bd list --status open --json | rg -i "<keyword>"`) and `bd ready` when appropriate.
- Output clearly labelled lists with single line summaries:
  - "Likely duplicates / related docs" (file paths)
  - "Related issues" (ids + titles)
- If any likely duplicates are found:
  - Highlight them to the user and ask if any represent the work to be done.
- If a likely parent issue is found:
  - Note it for later use when creating the bead (as a sub-issue).
- Read each of these related artifacts to extract key details for later reference.
- If a single existing artifact clearly represents the work, prefer UPDATE over NEW and ask the user to confirm.

2. Interview

- In interview iterations (≤ 3 questions each) build a full understanding of the work, offering templates/examples informed by repo context where possible.
- If anything is ambiguous, ask for clarification rather than guessing.
- Keep asking the user questions until all core information is captured and clarifications are made.
- Once you feel you are able to do so, write a clear intake brief (including noting any areas that could benefit from further expansion), present it to the user and ask the user to review it and provide feedback.
- The user may:
  - Respond with edits or clarifications, in which case you must incorporate them, and go back to the previous step of drafting the intake brief,
  - Ask you to continue asking questions, in which case you must continue the interview to gather more information, or
  - Approve the current draft, in which case you must proceed to the next step.

3. Decide next step (agent + user confirmation)

- Recommend NEW PRD or UPDATE and confirm with the user.
- If UPDATE: propose the PRD file path to update; if uncertain, ask the user to confirm the path.
- If NEW: propose a PRD filename under `docs/prd` using the convention `docs/prd/<feature>_PRD.md` and ask the user to confirm.

4. Draft Beads "Key details" (agent responsibility)

- Produce a short, copy-pastable "Key details" draft suitable for the Beads issue description. Use this template:
  - Problem
  - Users
  - Success criteria
  - Constraints
  - Existing state (if applicable)
  - Desired change (if applicable)
  - Likely duplicates / related docs
  - Related issues (Beads ids)
  - Recommended next step (NEW PRD at: <path> OR UPDATE PRD at: <path>)

- Save the draft to a temporary Markdown file at `.opencode/tmp/intake-draft-<title>.md`.
- Present the draft to the user and ask for any alterations or clarifications. Do not proceed until the user approves the draft or supplies edits.

5. Five mini-review stages (agent responsibility; must follow)

After the user approves the Key details draft, run five review iterations. Each review will make any necessary changes to `.opencode/tmp/intake-draft-<title>.md`.

- Each review stage the agent should apply only conservative edits. If a proposed change could alter intent, ask a clarifying question instead of changing content.

- After each stage print a clear "finished" message as follows: "Finished <Stage Name> review: <brief notes of improvements>" or "Finished <Stage Name> review: no changes needed"

- The five Intake review mini-prompts (names and intents):
  1. Completeness review
     - Ensure Problem, Success criteria, Constraints, and Suggested next step are present and actionable. Add missing bullets or concise placeholders when obvious.
  2. Capture fidelity review
     - Verify the user's answers are accurately and neutrally represented. Shorten or rephrase only for clarity; do not change meaning.
  3. Related-work & traceability review
     - Confirm related docs/issues are correctly referenced and that the recommended next step references the correct path/issue ids.
  4. Risks & assumptions review
     - Add missing risks, failure modes, and assumptions in short bullets. Do not invent mitigations beyond note-level comments.
  5. Polish & handoff review
     - Tighten language for reading speed, ensure copy-paste-ready commands, and produce the final 1–2 sentence summary used as the issue body headline.

6. Present final artifact for approval (human step)

- After the five reviews, present:
  - The 1–2 sentence headline summary for the issue
  - The full key details document in Markdown
- Ask the user to approve the final artifact or request further changes. Do not create the Beads issue until the user gives explicit approval.

7. Create or update the Beads issue (must do)

- When the user approves, create a Beads issue (or update an existing one). The issue must:
  - Type: `epic` or `feature`
  - Priority: Prioritise according to your review of related content and your understanding of the work.
  - Title: the working title
  - Description: include the full contents of `.opencode/tmp/intake-draft-<title>.md`
  - Assignee: "Build"
  - Labels: "stage:idea"

- If creating a new issue and a parent has been identified, create it as a sub-issue (`--parent <id>`).
- Add dependencies with `bd dep add` as appropriate.

8. Closing (must do)

- Run `bd sync` to sync bead changes.
- Run `bd show <bead-id>` (not --json) to show the entire bead.
- End with: "This completes the Intake process for <bd-id>".
- Remove all temporary files created during the process, including `.opencode/tmp/intake-draft-<title>.md`.
- Output the new issue id, a 1–2 sentence summary
- Finish with "This completes the Intake process for <bd-id>"

## Traceability & idempotence

- When the agent updates or creates a Beads issue, it must do so idempotently: running the command again should not create duplicate links or duplicate clarifying-question entries.

## Editing rules & safety

- Preserve author intent; where the agent is uncertain, add a clarifying question instead of making assumptions.
- Keep edits minimal and conservative.
- Respect `.gitignore` and other ignore rules when searching the repo.
- If any automated step fails or is ambiguous, surface an explicit Open Question and pause for human guidance.
