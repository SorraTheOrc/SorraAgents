---
description: Guide for creating effective skills. This skill should be used when users want to create a new skill (or update an existing skill) that extends Claude's capabilities with specialized knowledge, workflows, or tool integrations.
agent: forge
---

# Skill Creator

You are helping create effective agent skills.

## About Skills

Skills are modular, self-contained packages that extend Claude's capabilities by providing
specialized knowledge, workflows, and tools. Think of them as "onboarding guides" for specific
domains or tasks—they transform Claude from a general-purpose agent into a specialized agent
equipped with procedural knowledge that no model can fully possess.

### What Skills Provide

1. Specialized workflows - Multi-step procedures for specific domains
2. Tool integrations - Instructions for working with specific file formats or APIs
3. Domain expertise - Company-specific knowledge, schemas, business logic
4. Bundled resources - Scripts, references, and assets for complex and repetitive tasks

### Anatomy of a Skill

Every skill consists of a required SKILL.md file and optional bundled resources:

```
skill-name/
├── SKILL.md (required)
│   ├── YAML frontmatter metadata with name and description (required)
│   ├── Markdown instructions (required)
│   └── Examples (required)
├── scripts/          - Executable code (Python/Bash/etc.)
├── references/       - Documentation intended to be loaded into context as needed
└── assets/           - Files used in output (templates, icons, fonts, etc.)
```

#### SKILL.md (required)

**Metadata Quality:** The `name` and `description` in YAML frontmatter determine when an agent will use the skill. Be specific about what the skill does and when to use it. Use the third-person (e.g. "This skill should be used when..." instead of "Use this skill when...").

#### Bundled Resources (optional)

These optional directories contain reusable resources that an agent can leverage when executing the skill. These resources help manage context window size while providing necessary functionality. Only include resources that are truly reusable across multiple invocations of the skill. If in doubt ask the producer for guidance.

##### Scripts (`scripts/`)

Executable code (Python/Bash/etc.) for tasks that require deterministic reliability or are repeatedly rewritten.

- **When to include**: When the same code is being rewritten repeatedly or deterministic reliability is needed
- **Example**: `scripts/regression_analysis.py` for regression analsis of supplied data, `scripts/generate_report.sh` for generating standardized reports, `scripts/data_focus.py` for retrieving data and extracting only the relevant fields needed by the skill
- **Use cases**: Data processing, file manipulation, API interactions, determinstic report generation, complex calculations
- **Benefits**: Token efficient, deterministic, may be executed without loading into context
- **Note**: Scripts may still need to be read by an agent for patching or environment-specific adjustments

##### References (`references/`)

Documentation and reference material intended to be loaded as needed into context to inform an agent's process and thinking.

- **When to include**: Materials that an agent may need to read, but are not always required for every invocation
- **Examples**: `references/API.md` the api documentation of a library the skill specialized in, `references/issue_template.md` a template for issue creation, `references/coding_practices.md` for coding practices the skill should follow
- **Use cases**: Database schemas, API documentation, domain knowledge, company policies, detailed workflow guides
- **Benefits**: Keeps SKILL.md lean, by only loading resources when the skill determines it's needed
- **Best practice**: If files are large (>10k words), include grep search patterns in SKILL.md
- **Avoid duplication**: Information should live in either SKILL.md or references files, not both. Prefer references files for detailed information unless it's truly core to the skill—this keeps SKILL.md focussed on process while making information discoverable without filling the context window. Keep only essential procedural instructions and workflow guidance in SKILL.md; move detailed reference material, schemas, and examples to references files.

##### Assets (`assets/`)

Files not intended to be loaded into context, but rather used within the output an agent produces.

- **When to include**: When the skill needs files that will be used in the final output
- **Examples**: `assets/logo.png` for brand assets, `assets/page.html` for web page templates, `assets/frontend-template/` for HTML/React boilerplate, `assets/font.ttf` for typography
- **Use cases**: Templates, images, icons, boilerplate code, fonts, sample documents that get copied or modified
- **Benefits**: Separates output resources from documentation, enables an agent to use files without loading them into context

### Progressive Disclosure Design Principle

Skills use a three-level loading system to manage context efficiently:

1. **Metadata (name + description)** - Always in context (~100 words)
2. **SKILL.md body** - When skill triggers (<5k words)
3. **Bundled resources** - As needed by an agent (Unlimited since scripts can be executed without reading into context window)

## Results and Outputs

- A directory scaffold and drafted SKILL.md and README.md describing the new or updated skill and concrete examples.
- Worklog side-effects: Optional — the process may create or update a work item to track the skill creation request if the producer requested tracking.
- Repository side-effects:
  - New skill directory created with `SKILL.md` and `README.md`; optional `scripts/`, `references/`, and `assets/` subfolders populated as planned.
  - Files are ready to be committed and proposed via a PR if requested.
- Machine-readable artifacts: Any `wl` JSON output when a tracking work item is created/updated; filesystem metadata for created files.
- Idempotence: Re-running the skill-creation flow updates existing files in-place and avoids creating duplicate skill directories by name.
- Audit/logging: SKILL.md and created files should include a short changelog entry and the producer-confirmed examples for traceability.

## Hard requirements

- Whenever you are recommending next steps you MUST make the first one a progression to the next step in the process defined below, with a summary of what that step involves.

## Skill Creation/Update Process

To create or update a skill, follow the "Skill Creation/Update Process" below, in order, without skipping steps.

### Step 1: Understanding the Skill with Concrete Examples

To create an effective skill, you must fully understand what is intended and what functionality it should provide. This starts with an interview process to gather as much information as possible about the desired use cases from the producer requesting the skill.

If you are updating an inexisting skill, begin by reviewing the current SKILL.md and any associated resources to understand its current functionality and limitations.

Concrete examples of how the skill will be used can help build this understanding. When you feel you have enough information, summarize your understanding back to the producer and provide some synthetic examples to allow the producer to confirm accuracy or provide feedback.

- In interview iterations (≤ 3 questions each) build a full understanding of the desired skill functionality. For each example, ask clarifying questions to understand:
  - The context in which the skill will be used
  - The specific inputs the skill will receive
  - The expected outputs or results from the skill
  - Any constraints or special considerations for the skill's operation

- If anything is ambiguous, ask for clarification rather than guessing.

- Keep asking the user questions until you can create 2-3 concrete examples of how the skill should function.

- Summarize your understanding of the skill, propose a name for it and present 2-3 concrete examples of how the skill should function, and ask the user to confirm accuracy or provide feedback. Each example should include:
  - A brief description of the use case
  - Sample inputs the skill would receive
  - Expected outputs or results from the skill

- If feedback makes it necessary to do so, go back to asking more questions to refine your understanding.

- Write a README.md file in the root of the skill directory (using the skill name as the directory name). The content of the README.md should summarize your understanding of the skill, including the confirmed concrete examples. This README.md will serve as a reference throughout the skill creation process to ensure alignment with the producer's expectations.

### Step 2: Planning the Reusable Skill Contents

Before creating or editing the skill, plan out the workflow, procedures and decision points as well as what reusable resources the skill will need. This includes scripts, reference documents, and assets that will help Claude execute the skill effectively.

Follow the `command/plan.md` process, using the the README.md in place of the issue content.

Consider the following:

- **Process and workflows**: Identify any multi-step procedures or workflows that an agent will need to follow. Plan these steps out clearly in SKILL.md.
- **Scripts**: Identify any tasks that require deterministic reliability or are repeatedly rewritten. Plan scripts that can handle these tasks efficiently.
- **References**: Determine what documentation or reference materials an agent may need to read to inform its process. Plan reference files that can be loaded as needed.
- **Assets**: Identify any files that will be used in the output, such as templates, images, or boilerplate code. Plan asset files that will be included in the skill package.

Create a directory structure for the skill that includes placeholders for these reusable resources. Do not create folders if no contentsfor it have been identified.

This structure will guide the development of the skill and ensure that all necessary components are included.

### Step 3: Drafting the SKILL.md and Bundled Resources

Create the initial draft of SKILL.md and any identified bundled resources based on the plan developed in Step 2.

The SKILL.md should include:

- **YAML Frontmatter**: Fill in the `name` and `description` fields with clear, specific information about the skill's purpose and when to use it.
- **Markdown Instructions**: Under the heading "## Instructions" write detailed instructions that outline the workflows, procedures, and decision points for the skill. Use imperative/infinitive form (verb-first instructions) and objective, instructional language.
- **References to Bundled Resources**: Under the heading "## References to Bundled Resources" clearly indicate where and how the bundled resources (scripts, references, assets) should be used within the skill's workflows.
- **Examples**: Under the heading "## Examples" include concrete examples of how the skill should be used, based on the confirmed examples from Step 1.

Create the bundled resources as planned:

- **Scripts**: Write the executable code for any tasks that require deterministic reliability or are repeatedly rewritten.
- **References**: Compile the necessary documentation and reference materials that an agent may need to read.
- **Assets**: Gather and organize the files that will be used in the output.

### Step 4: Producer Review and Iteration

Share the draft SKILL.md and bundled resources with the producer for review. Solicit feedback on the clarity, completeness, and accuracy of the skill's instructions and resources.

Incorporate the producer's feedback into the skill, making necessary revisions to SKILL.md and bundled resources. Repeat the review and revision process until the producer is satisfied with the skill.

### Step 5: Finalizing the Skill

Automated review stages (must follow; no human intervention required)

After the user approves the draft materials, run five review iterations. Each review MAY edit the draft materials.

- "Finished <Stage Name> review: <brief notes of improvements>"
  - If no improvements were made: "Finished <Stage Name> review: no changes needed"

- General requirements for the automated reviews:
  - Run without human intervention.
  - Each stage runs sequentially in the order listed below.
  - When the stage completes the command MUST output exactly: "Finished <Stage Name> review: <brief notes of improvements>"
    - If no improvements were made, the brief notes MUST state: "no changes needed".
  - Improvements should be conservative and clearly scoped to the stage. If an automated improvement could change intent, the reviewer should avoid making those changes and instead should return to the interview step to gather more information.

- Review stages and expected behavior:
  1. Structural review
     - Purpose: Validate the skill and materials follows the required outline and check for missing or mis-ordered sections.
     - Actions: Ensure the workflow and processes are clear and that any content described is present in the appropriate folder; validate the presence of at least two example applications of the skill; ensure YAML frontmatter is complete and accurate.
  2. Clarity & language review
     - Purpose: Improve readability, clarity, and grammar without changing meaning. Ensure writing style uses imperative/infinitive form (verb-first instructions), rather than second person. Use objective, instructional language (e.g., "To accomplish X, do Y" rather than "You should do X" or "If you need to do X"). This maintains consistency and clarity for agent consumption.
     - Actions: Apply non-destructive rewrites (shorten long sentences, fix grammar, clarify ambiguous phrasing). Do NOT change intent or add new functional requirements.
  3. Technical consistency review
     - Purpose: Check the requirements recorded in the README.md against the definition in the SKILL.md.
     - Actions: Detect contradictions between requirements and the defined skill; ensure there are no lingering assumptions or ambiguities; where safe, adjust wording to remove contradictions (e.g., normalize terminology) where uncertain return to the interview stage.
  4. Security & compliance review
     - Purpose: Surface obvious security, privacy, and compliance concerns and ensure the SKILL.md includes at least note-level mitigations where applicable.
     - Actions: Scan for missing security/privacy considerations in relevant sections and add short mitigation notes (labelled "Security note:" or "Privacy note:"). Do not invent security requirements beyond conservative, informational notes.
  5. Lint, style & polish
     - Purpose: Run automated formatting and linting (including markdown lint) and apply safe autofixes.
     - Actions: Run `remark` with autofix enabled, apply whitespace/formatting fixes, ensure consistent bulleting and code block formatting. Summarize what lint fixes were applied.

- Failure handling:
  - If any automated review encounters an error it cannot safely recover from, the command MUST stop and surface a clear error message indicating which stage failed and why. Do not attempt destructive fixes in that case; instead return to the interview step to gather more information.

- Producer handoff:
  - Although the reviews are automated, the output messages and changelog entries MUST be sufficient for a human reviewer to understand what changed and why.

### Step 6: Finalizing the skill

Once the producer has approved the reviewed materials, finalize the skill by completing the following steps:

- Remove any placeholder text or example files that are not needed for the skill (the README.md can be left in place).
- Ensure all bundled resources are properly organized and referenced in SKILL.md.
- Perform a final read-through of SKILL.md to ensure clarity and completeness.
