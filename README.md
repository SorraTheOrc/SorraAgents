# OpenCode — Workflow & Skills Repository

A lightweight collection of workflow guides, command patterns, and skill templates for building and operating small automation agents.

## Purpose
- Centralize documentation and reusable "skills" for agent development and operational workflows.
- Provide templates and checklists to guide feature implementation, testing, and release.

## Repository structure
- agent/: workflow and agent-focused reference guides (e.g., [agent/forge.md](agent/forge.md)).
- command/: design, intake, implementation and review process documents (see [command/implement.md](command/implement.md)).
- skill/: skill templates and utilities to scaffold and package agent skills (see [skill/skill-creator/SKILL.md](skill/skill-creator/SKILL.md)).
- Workflow.md: high-level workflow for using this repository.
- package.json: basic metadata used by tooling.

## Getting started
1. Read the main workflow: [Workflow.md](Workflow.md).
2. Pick a folder to work in (e.g., `skill/` or `agent/`).
3. Follow the appropriate guide (see files inside each folder) to implement, test, and package your work.

## Contributing
- Open an issue describing the change you'd like to make.
- Follow the relevant guide under `command/` for design and review steps.
- If adding a new skill, consider using the scripts in `skill/skill-creator/scripts` to scaffold and package it.

## Next steps / Suggestions
- Add a CI workflow to validate new skills and docs.
- Add example usage for each skill in `skill/` to make onboarding easier.

## License
See individual files for licenses. Some folders include a LICENSE.txt (for example: [skill/skill-creator/LICENSE.txt](skill/skill-creator/LICENSE.txt)).

---
If you'd like, I can commit this file, add a short changelog entry, or expand any section into more detailed docs.