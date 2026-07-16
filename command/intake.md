---
description: Create an intake brief (Workflow step 1)
tags:
  - workflow
  - intake
agent: build
---

You are authoring a new Worklog work item for a feature or bug fix, following an interview-driven approach to gather requirements, constraints, Acceptance Criteria (synonym: Success Criteria), and related work — ensuring sufficient detail for a developer to complete the work.

## Inputs

- `$1` — The work-item-id (format `<prefix>-<hash>`). If valid, fetch and use it. If missing or invalid, treat `$ARGUMENTS` as the seed intent and create a new work item as needed. If the user intended to reference an existing item but provided an invalid id, ask for a valid one.
- `$ARGUMENTS` — Optional freeform arguments after `<work-item-id>` to guide your work.

## Results and Outputs

- A 1–2 sentence headline summary of the intake brief
- Final brief text and the new or updated work item
- Idempotence: rerunning `/intake` reuses existing work items when they represent the same item

## Behavior

The command implements the procedural workflow below. Each numbered step is part of the canonical execution path; substeps describe concrete checks or commands to run.

## Hard requirements

- Do not create a work item for this intake process itself; the output is a completed description for the target work item.
- If additional information is needed, use an interview style: concise, high-signal questions, max three per round.
- Do not invent requirements — ask the user. Do not ask leading questions or unnecessary questions if an obvious answer exists.
- If a response is unclear or ambiguous, ask for clarification rather than guessing.
- Respect `.gitignore` and agent framework ignore rules.
- Prefer short multiple-choice suggestions, but always allow freeform responses.
- All work-item descriptions and comments **must be written in Markdown**.
- The goal is sufficient detail to create a clear work item — not an exhaustive spec.
- Do not include procedural next steps (e.g., "Proceed to planning") in the intake brief or work item description. Workflow progression is handled by stage transitions, not by work item content.

## Status lifecycle (first action)

- **Before any other step**, claim the work item:
  `wl update <work-item-id> --status in_progress --json`
  This must be done before any evaluation, context gathering, or preflight checks. The status signals that this item is being processed and prevents concurrent claims.

## Process (must follow)

1. Evaluate whether intake is required (agent responsibility)

- Before performing full intake, run a lightweight evaluation to determine whether the work item already contains sufficient information to skip the interview/draft process.
- Suggested heuristics (conservative, idempotent):
  - If `stage` is already `intake_complete` or later, skip.
  - If the description has a clear one-line headline, an "## Acceptance Criteria" section with 1–3 measurable bullets, and concise implementation notes (≤~200 words), it is likely well-defined enough to skip.
  - If the item is small (`task` or `bug`, not `epic`) with explicit ACs and a minimal implementation sketch, prefer to mark intake complete.
  - If parent/child relationships already express the required context, consider skipping.
- If intake is not needed:
  - `wl update <work-item-id> --stage intake_complete --status open --json`
  - Optionally add a comment: `wl comment add <work-item-id> "Intake auto-complete: work item appears sufficiently defined (ACs present / small task)." --actor Map --json`
- If uncertain, fall back to the normal intake process (do not auto-complete on borderline evidence).

1. Gather context (agent responsibility)

- Derive 2–6 keywords from `<seed-context>` and user input.
- Search work items (`wl search <keywords> --json`) and the repository for additional context (ignore `node_modules`, `.git`, and most `.`-prefixed folders).
- If duplicates are found:
  - Highlight them and ask if any represent the work to be done.
  - If confirmed as duplicates, ask the user to resolve instead of proceeding.
  - If confirmed as parent/child, create the appropriate relationship when creating work items.
- Output labelled lists:
  - "Potentially related docs" (file paths)
  - "Potentially related work items" (titles + IDs)
- Read and summarize each related artifact for later reference.

1. Work Item prep (agent responsibility)

- If `<work-item-id>` was provided:
  - `wl update $1 --stage idea --assignee Map --json` (status was already set to `in_progress` — see Status lifecycle above)
  - Review the item's `issueType`. If it doesn't match the nature of the work, update: `wl update <work-item-id> --issue-type <correct-type> --json`
    - `bug` — problem/fix | `feature` — new capability | `chore` — maintenance | `task` — general work | `epic` — large scope with subtasks
- If no id was provided:
  - Extract a working title from `<seed-intent>` (one line).
  - Infer the issue type from context (bug/feature/chore/epic/task).
  - Create: `wl create --stage idea --status in_progress --title "<title>" --description "<seed-context>" --issue-type <type> --assignee Map --json`
  - Remember the returned id.

1. Interview

If the seed context is sufficient to draft a clear intake brief, skip this step. Otherwise, proceed with the interview.

- Soft limit of 3 questions per round, 1 or more rounds as needed.
- Do not ask questions answerable by repo search — use gathered context. If context is insufficient, ask for the specific missing piece.
- Goal: build sufficient understanding to draft a problem definition with user stories, ACs, and related work — not a complete spec.
- If anything is ambiguous, ask for clarification rather than guessing.
- Do not proceed until sufficient information is gathered.

1. Draft intake brief (agent responsibility)

- Write a brief to `.worklog/tmp/intake-draft-<title>-<work-item-id>.md` with these sections:
  - **Problem statement:** 1–2 sentences summarizing the problem.
  - **Users:** who benefits, with example user stories.
  - **Acceptance Criteria:** 3–5 measurable bullets describing success.
  - **Constraints:** technical, business, or regulatory.
  - **Existing state:** current state of affairs.
  - **Desired change:** likely changes needed.
  - **Key Files (predicted):** files likely to change, with brief explanations. Published as a `**Key Files:**` section in the work item description; update if it already exists (e.g., ``- `path/to/file.py` — Needs new function for X feature``).
  - **Related work:** related docs or work items with descriptions and links/ids.
- Present the draft to the user and invite feedback. Incorporate edits when supplied, but don't block waiting for approval — proceed automatically to review stages.

1. Five mini-review stages (agent responsibility; must follow)

Run five conservative review iterations on the draft brief. If a proposed change could alter intent, ask a clarifying question first.

After each stage: "Finished <type> review: <changes>" or "Finished <type> review: no changes needed"

1. **Completeness** — Ensure Problem, ACs, and Constraints are present and actionable. Add missing bullets or concise placeholders when obvious.
2. **Capture fidelity** — Verify user answers are accurately and neutrally represented. Shorten only for clarity; don't change meaning.
3. **Related-work & traceability** — Confirm related docs/work items are correctly referenced.
4. **Risks & assumptions** — Add missing risks, mitigations, failure modes, and assumptions in short bullets. Include a scope-creep risk: record extra opportunities as linked work items rather than expanding scope. Don't invent mitigations beyond note-level.
5. **Polish & handoff** — Tighten language, ensure copy-paste-ready commands, produce the final 1–2 sentence headline.

1. Call the `find_related` skill to collect related work and add a report to the work item description.

2. Review the new issue in project context and consider:
- Adding dependencies: `wl comment add <work-item-id> --comment "Blocks:<blocked-id>" --json` / `--comment "Blocked-by:<blocking-id>" --json`
- Adjusting priority: `wl update <work-item-id> --priority <level> --json`

1. Update the work item: `wl update <work-item-id> --description-file .worklog/tmp/intake-draft-<title>-<work-item-id>.md --stage intake_complete --status open --json`

2. Calculate Effort and Risk (agent responsibility; must follow)

- Call the `effort_and_risk` skill on the new or updated work item to produce an estimate.

1. Finishing (must do as the final step only)

- Set status to open (DO NOT close): `wl update <work-item-id> --status open --json`
- `wl sync` to sync changes.
- `wl show <work-item-id>` (not --json) to display the full work item.
- Remove temporary files: `.worklog/tmp/intake-draft-<title>-<work-item-id>.md`
- Output a structured summary:

# Objective

  Headline summary of the issue

# Acceptance Criteria

  Complete list of measurable acceptance criteria. If any are not measurable, add a clarifying question to the Appendix and mark as "TBD pending clarification".

  Always include:
  - At least one criterion related to testing and validation.
  - "All related documentation is updated to reflect the changes, including code comments, README, and any relevant wiki or docs site entries."
  - "Full project test suite must pass with the new changes."

  > **Note:** CHANGELOG.md is **excluded** from this list. It is managed automatically by the ship skill's release pipeline (`skill/ship/scripts/release/generate-changelog.js`). Implementing agents should not manually update CHANGELOG.md.

  Do not include CI/CD pipeline tests.

# Effort and Risk

  T-shirt sizing and one-line description of the biggest risks

- Finish with "This completes the Intake process for <work-item-id> <work-item-title>"

## Traceability & idempotence

- All work item updates or creations must be idempotent: rerunning `/intake` must not create duplicate links or clarifying-question entries.

## Editing rules & safety

- Preserve author intent; if uncertain, add a clarifying question instead of assuming.
- Keep edits minimal and conservative.
- Respect `.gitignore` and other ignore rules when searching the repo.
- If any automated step fails or is ambiguous, surface an explicit Open Question and pause for guidance.

## Appendix: Clarifying questions & answers (must include)

- **Purpose:** Every interview-driven intake must produce an auditable Appendix listing all clarifying questions asked and the answers provided. Append the complete Appendix to the final draft file AND include it in the work item description when running `wl update --description-file`.

- **Required contents per entry** (one line acceptable; context paragraphs where needed):
  - The question text as asked.
  - The answer, answering party, and evidence/link (work item id, file path, PR).
  - If the answer changed, record earlier answers and the final accepted answer.
  - If the question led to research, include a concise summary (1–6 sentences) with links.

- **Example format:**
  - Q: "Who is the primary user?" — Answer (user@acme): "Internal support engineers". Source: interactive reply.
  - Q: "Is migration required?" — Answer (user@acme): "No, data model unchanged". Source: interactive reply.
  - Q: "Can we reuse service X?" — Answer (engineer@acme): "Partially; need a small wrapper. Research: inspected services/x, found no adapter — created follow-up wl-789".

- **Behavior and placement:**
  - Append the complete Appendix to the draft file before final approval.
  - Include it in the `wl update --description-file` content.
  - **Idempotent:** rerunning `/intake` must not duplicate earlier entries — update existing records instead.
  - Open questions: mark as "OPEN QUESTION" with context.
  - Respect `.gitignore` and agent framework ignore rules.

- **Privacy & scope:**
  - Record only information provided by the user or authorized stakeholders. Redact secrets with a note (e.g., "[REDACTED sensitive snippet]").
  - If a user pastes sensitive content by mistake, redact and note.

- **Traceability:**
  - Each entry should be linkable from the work item. When practical, include `related-to:<work-item-id>` or file path references.
