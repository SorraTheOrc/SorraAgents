---
description: Create an intake brief (Workflow step 1)
tags:
  - workflow
  - intake
agent: build
---

You are coordinating an intake brief for a new Worklog work item.

## Description

You are authoring a new Worklog work item that describes a feature or a bug fix to be implemented. You will ensure that the details in the Worklog work item are sufficient to allow a developer to complete the work.
You will follow an interview-driven approach to gather requirements, constraints, success criteria, and related work.

## Inputs

- The supplied <work-item-id> is $1.
  - If a valid <work-item-id> is provided (ids are formatted as '<prefix>-<hash>'), fetch and use it. If no work-item id is provided or the id is not valid, the command may still proceed: treat `$ARGUMENTS` as the authoritative seed intent and create a new work item as needed. If the user intended to reference an existing work item but provided an invalid id, ask the user to provide one.
- Optional additional freeform arguments may be provided to guide your work. Freeform arguments are found in the arguments string "$ARGUMENTS" after the <work-item-id> ($1).

## Results and Outputs

- A 1–2 sentence headline summary of the intake brief.
- Final intake brief text and the new or updated work item $1.
- Idempotence: Rerunning `/intake` reuses existing work items when they are considered to be the same item.

## Behavior

The command implements the procedural workflow below. Each numbered step is part of the canonical execution path; substeps describe concrete checks or commands that implementors or automation should run.

## Hard requirements:

- Do not create a work item for this intake process itself; the output of this command is the completion of a description for the work item of interest.
- Use an interview style: concise, high-signal questions grouped to a soft-maximum of three per iteration.
- Do not invent requirements or constraints; if unknown, ask the user.
- Do not ask leading questions that bias the user towards a particular answer.
- If a response is unclear or ambiguous, ask for clarification rather than guessing or asking a largely similar question.
- Respect ignore boundaries: do not include or quote content from files excluded by `.gitignore` or OpenCode ignore rules.
- Prefer short multiple-choice suggestions where possible, but always allow freeform responses.
- The goal is not to capture an exhaustive spec, but to gather sufficient detail to create a clear Worklog work item that will be used to either seed a PRD, update an existing one, or if the work is small and well-defined, be implemented directly from the Worklog work item.

- Whenever you are recommending next steps you MUST make the first one a progression to the next step in the process defined below, with a summary of what that step involves.

## Note

- This Hard requirements section is populated with the mandatory progression rule above; review the rest of the hard requirements for task-specific constraints.

## Process (must follow)

1. Gather context (agent responsibility)

- Derive 2–6 keywords from the <seed-context> and user input to guide repository.
- Use derived keywords to search work items (`wl list <search> --json`) and the repository for additional context.
  - ignore data directories such as `node_modules`, `.git` and most "." named folders.
- If any likely duplicates are found:
  - Highlight them to the user and ask if any represent the work to be done.
  - If they are confirmed as duplicates ask the user to resolve the duplicate instead of proceeding.
  - if any are confirmed as a parent/child work item, remember this and, when creating work items, create the appropriate parent/child relationship.
- Output clearly labelled lists with single line summaries:
  - "Potentially related docs" (file paths)
  - "Potentially related work items" (titles followed by ID)
- Read and summarize each of these related artifacts for later reference.

2. Work Item prep (agent responsibility)

- If a <work-item-id> was provided:
  - Mark the work item as in progress and at stage idea by running `wl update $1 --stage idea --status in_progress --assignee Map --json`.
- If no work item id was provided:
  - Extract a working title from the <seed-intent> (one line).
  - Create a new Worklog work item using `wl create --stage idea --status in_progress --title "<working-title>" --description "<seed-context>" --type epic --assignee Map --json`
  - Remember the returned <work-item-id> for later steps.

3. Interview

- In user interview iterations with a soft limit of 3 questions per round, build a full understanding of the work, offering suggested answers and examples informed by repo context where possible.
- If anything is ambiguous, ask for clarification rather than guessing.
- Keep asking the user questions until all core information is captured and clarifications are made.
  - The goal is not a complete spec but a sufficient understanding to draft a problem definition with user stories, success criteria, and related work.
- Do not proceed until you have gathered sufficient information to draft an intake brief.

4. Draft intake brief (agent responsibility + user confirmation)

- Write a clear intake brief with at `.opencode/tmp/intake-draft-<title>-<work-item-id>.md` the following sections:
  - Problem statement: one or two sentences summarizing the problem to be solved.
  - Users: who will benefit from or use the feature and examples of their user stories.
  - Success criteria: 3–5 concise, measurable bullets describing how success will be evaluated.
  - Constraints: any known constraints (technical, business, regulatory) that must be considered.
  - Existing state: brief summary of the current state of affairs related to the problem.
  - Desired change: brief summary of the likely changes needed.
  - Related work: list of related documents or work items with brief descriptions and links/ids.
- Present the draft brief to the user and ask the user to review it and provide feedback.
- The user may:
  - Respond with edits or clarifications, in which case you must incorporate them, and re-present the updated draft for approval, or
  - Approve, in which case you must proceed to the next step.

5. Five mini-review stages (agent responsibility; must follow)

After the user approves the draft brief, run five review iterations. Each review will make any necessary changes to `.opencode/tmp/intake-draft-<title>-<work-item-id>.md`.

In each review stage apply only conservative edits. If a proposed change could alter intent, ask a clarifying question before making any changes.

After each stage output: "Finished <review-type> review: <brief notes of changes>" or "Finished <review-type> review: no changes needed"

- The five Intake review types are:
  1. Completeness
     - Ensure Problem, Success criteria, Constraints, and Suggested next step are present and actionable. Add missing bullets or concise placeholders when obvious.
  2. Capture fidelity
     - Verify the user's answers are accurately and neutrally represented. Shorten or rephrase only for clarity; do not change meaning.
  3. Related-work & traceability
  - Confirm related docs/work items are correctly referenced and that the recommended next step references the correct path/work item ids.
  4. Risks & assumptions
     - Add missing risks and mitigations, failure modes, and assumptions in short bullets.
     - Ensure that a risk addressing scope screep is present. The mitigaation is to record opportunities for additional features/refactorings as work items linked to the main item, rather than expanding the scope of the current item.
     - Do not invent mitigations beyond note-level comments.
  5. Polish & handoff
  - Tighten language for reading speed, ensure copy-paste-ready commands, and produce the final 1–2 sentence summary used as the work item body headline.

6. Present final artifact for approval (human step)

- After the five reviews, output the content of `.opencode/tmp/intake-draft-<title>-<work-item-id>.md` for the user.
- Ask the user to approve the final artifact or request further changes.
- Only proceed to the next step when the user approves the final intake brief.

7. Update the Worklog work item (agent responsibility; must follow)

Update the description of the Worklog work item with the final intake brief from `.opencode/tmp/intake-draft-<title>-<work-item-id>.md` using `wl update <work-item-id> --description-file .opencode/tmp/intake-draft-<title>-<work-item-id>.md --stage intake_complete --json`.

8. Call the `find_related` skill to collect related work and add a report to the work item description.

9. Review the new issue in the overall context of the project and consider:

- Adding dependencies with `wl comment add <work-item-id> --comment "Blocks:<blocked-item-id>" --json` and `wl comment add <work-item-id> --comment "Blocked-by:<blocking-item-id>" --json`
- Adjusting priority to better match the new understanding of scope and impact using `wl update <work-item-id> --priority <level> --json`

10. Finishing (must do)

- DO NOT close the issue
- Run `wl sync` to sync work item changes.
- Run `wl show <work-item-id>` (not --json) to show the entire work item.
- End with: "This completes the Intake process for <work-item-id>".
- Remove all temporary files created during the process, including `.opencode/tmp/intake-draft-<title>.md`.
- Output the new work item id, a 1–2 sentence summary headline
- Finish with "This completes the Intake process for <work-item-id>"

## Traceability & idempotence

- When the agent updates or creates a Worklog work item, it must do so idempotently: running the command again should not create duplicate links or duplicate clarifying-question entries.

## Editing rules & safety

- Preserve author intent; where the agent is uncertain, add a clarifying question instead of making assumptions.
- Keep edits minimal and conservative.
- Respect `.gitignore` and other ignore rules when searching the repo.
- If any automated step fails or is ambiguous, surface an explicit Open Question and pause for human guidance.
