---
description: Review a document using a 5-step gated process
---

You are reviewing a document using a structured 5-step process. Each step is gated on the completed sign-off of the previous step.

Inputs:

- The user will provide the path to the file to be reviewed, which will reference a work item ID for tracking, or a work item ID which provides a link to the PRD to be reviewed.
- If you are not able to identify both the file path and work item ID, ask the user for the missing information.

## Argument parsing

- Pattern: If the raw input begins with a slash-command token (a leading token that starts with `/`, e.g., `/review`), strip that token first.
- The command expects either a file path or a work item id; the first meaningful token after any leading slash-command is available as `$1`. `$ARGUMENTS` contains the full arguments string (everything after the leading command token, if present).
- If `$1` is missing or ambiguous, ask the user to provide the missing file path or work item id.

Process (must follow):

Perform the following 5 steps sequentially. Do not proceed to the next step until the current step is completed, the file is updated, changes are summarized in a work item comment, and lint checkers have been run.

### Step 1: Broad Structure and High-Level Detail

- Focus on the general structure and high-level detail of the document.
- Ensure the document flows logically and covers the intended scope at a high level.
- **Action:** Edit the file to improve structure and high-level content.
- **Lint:** Run lint checkers on the file (e.g., `remark` with autofix).
- **Report:** Summarize changes in a comment on the related work item.

### Step 2: Major Sections and Ambiguities

- Check for missing major sections.
- Identify and resolve any ambiguities in the high-level view.
- **Action:** Edit the file to add missing sections and clarify ambiguities.
- **Lint:** Run lint checkers on the file.
- **Report:** Summarize changes in a comment on the related work item (if applicable).

### Step 3: Clarity, Duplication, and Related Content

- Focus on clarity and identify missing content/sections.
- Look for and address any duplication and/or contradictions.
- Actively search for related content within the project and on the web that might be useful.
- **Action:** Edit the file to improve clarity, remove duplication/contradictions, and add referenced/linked sources.
- **Lint:** Run lint checkers on the file.
- **Report:** Summarize changes in a comment on the related work item.

### Step 4: Detail Review

- Review from the perspective of detail.
- Ensure the reader has enough detail to understand the topic and take next steps.
- **Action:** Edit the file to add necessary details.
- **Lint:** Run lint checkers on the file.
- **Report:** Summarize changes in a comment on the related work item.

### Step 5: Conciseness and Summaries

- Review for conciseness. Can we say more with less?
- Ensure the document has a suitable executive summary at the start and a summary at the end.
- Ensure longer sections also have an executive summary at the start and a brief summary at the end.
- **Action:** Edit the file to improve conciseness and add/refine summaries.
- **Lint:** Run lint checkers on the file.
- **Report:** Summarize changes in a comment on the related work item

Once completed, output the content of the 5 comments made on the work item during each step.

End your report with a single line saying "This concludes my work on reviewing the document at <file path>."
