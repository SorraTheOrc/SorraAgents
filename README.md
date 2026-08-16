# Workflow & Skills Repository

A lightweight collection of workflow guides, command patterns, and skill templates for building and operating small automation agents.

## Purpose

- Centralize documentation and reusable "skills" for agent development and operational workflows.
- Provide templates and checklists to guide feature implementation, testing, and release.

## Terminology Policy

"Acceptance Criteria" is the canonical term for work-item requirements. "Success Criteria" is an accepted synonym and may be used interchangeably. When defining work items, prefer the heading **Acceptance Criteria** (synonym: Success Criteria) for consistency.

## Repository structure

- agent/: workflow and agent-focused reference guides (e.g., [agent/forge.md](agent/forge.md), [agent/ship.md](agent/ship.md)).
- command/: design, intake, implementation and review process documents (see [command/implement.md](command/implement.md)).
- skill/: skill templates and utilities to scaffold and package agent skills (see [skill/skill-creator/SKILL.md](skill/skill-creator/SKILL.md)).
  - [skill/skills-script-paths.md](skill/skills-script-paths.md): Best practices for referencing scripts and assets from skills.
  - [skill/report/SKILL.md](skill/report/SKILL.md): the **report helper** — canonical end-of-session report format (Acceptance Criteria table, Meta-Data with ContextHub icons, Producer Actions, Notes, Conclusion) that every work-item skill renders as its final step.
- plugins/: local agent framework plugins used by this repository.
- docs/dev/: development and release process documentation ([release-process.md](docs/dev/release-process.md), [release-tests.md](docs/dev/release-tests.md)).
- Workflow.md: high-level workflow for using this repository.
- package.json: basic metadata used by tooling.

## Prerequisites

### Code Quality Linters (Optional)

The automated code quality review feature supports the following linters. Install them to enable quality scanning during audits:

| Language | Linter | Install command |
|----------|--------|-----------------|
| Python | [ruff](https://docs.astral.sh/ruff/) | `pip install ruff` |
| TypeScript/JavaScript | [ESLint](https://eslint.org/) | `npm install -g eslint` |
| Markdown | [markdownlint-cli](https://github.com/igmpaul/markdownlint-cli) | `npm install -g markdownlint-cli` |
| Shell | [ShellCheck](https://shellcheck.net/) | `apt install shellcheck` or `brew install shellcheck` |
| C# | [dotnet-format](https://github.com/dotnet/format) | Install [.NET SDK](https://dotnet.microsoft.com/download) |

If a linter is not available, the code quality check skips that language gracefully without errors.

## Release Process

Promoting changes from `dev` to `main` requires a human-reviewed merge.
See the full [release process documentation](docs/dev/release-process.md) for
the checklist and role definition.

Test verification during releases (and repeated audits/implement loops) is
routed through a per-repo cache so identical full-suite runs at the same git
state are not re-executed: use
`python3 skill/test/scripts/run_tests.py --json` (cached) and
`--summary` (read-only query). See [`skill/test_cache.py`](skill/test_cache.py)
and [`docs/dev/release-tests.md`](docs/dev/release-tests.md).

### For Release Managers

The canonical release script lives under the ship skill at
`skill/ship/scripts/release/merge-dev-to-main.sh` and is invoked via
`node skill/ship/scripts/run-release.js` (see [skill/ship/SKILL.md](skill/ship/SKILL.md)).

```sh
# Run a dry-run to preview the merge
node skill/ship/scripts/run-release.js --dry-run

# Execute the merge
node skill/ship/scripts/run-release.js
```

Before merging, the release process runs local gates (worklog refs, audit
readiness, critical items, producer review). Tests are run locally before
release; there is no CI pipeline. After the gates pass, the script merges
`dev` into `main`, pushes the result, and records an audit comment in the
worklog.

While a release runs, the ship skill sets a **Code Freeze marker** at
`.worklog/code-freeze.json` (contract WL-0MSBU4KMA004PKSR). The marker is
written before the gating checks and cleared on every exit path (success,
failure, abort, dry-run) via `try/finally` in `run-release.js` and an `EXIT`
trap in `merge-dev-to-main.sh`. While the marker is present, `implement.py`
refuses to start new implementation work (no `--force` bypass; fail-open on
missing/corrupt marker). A stale marker from a crashed release can be removed
manually by deleting `.worklog/code-freeze.json`.

## Getting started

1. Read the main workflow: [Workflow.md](Workflow.md).
2. Pick a folder to work in (e.g., `skill/` or `agent/`).
3. Follow the appropriate guide (see files inside each folder) to implement, test, and package your work.

## PR-based audit flow (Pi)

The audit helper supports PR mode in addition to work-item mode:

- Input can be a WL id (`SA-...`) or GitHub PR reference (`https://github.com/<owner>/<repo>/pull/<n>` or `<owner>/<repo>#<n>`).
- In PR mode, the helper resolves the related WL item from PR title/body (or uses explicit `--wl-id`, or optionally `--allow-create-wl`).
- The helper can prepare an ephemeral checkout, run autodetected build/tests, run audit via `pi -p --mode json "/audit <wl-id>"` (non-interactive, JSON-stream mode), and record audit text using `wl update --audit-text`.
- If build/tests and audit pass, it can present a merge offer and only merges when explicitly confirmed.

## Contributing

- Open an issue describing the change you'd like to make.
- Follow the relevant guide under `command/` for design and review steps.
- If adding a new skill, consider using the scripts in `skill/skill-creator/scripts` to scaffold and package it.

## Next steps / Suggestions

- Add example usage for each skill in `skill/` to make onboarding easier.

## License

See individual files for licenses. Some folders include a LICENSE.txt (for example: [skill/skill-creator/LICENSE.txt](skill/skill-creator/LICENSE.txt)).

---
If you'd like, I can commit this file, add a short changelog entry, or expand any section into more detailed docs.
