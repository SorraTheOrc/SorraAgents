# Intake skill — implementation reference

Deep implementation-reference detail relocated from `skill/intake/SKILL.md`
(relocation tracked by SA-0MSLK7SAE0032V9K). The SKILL.md is the agent-facing
operational brief; this document preserves the full implementation reference
for maintainers. Workflow semantics are unchanged.

## Worklog resolution

`intake.py` routes every `wl` call through the shared `run_wl` helper
(`../shared/status_lifecycle.py`), which injects `--worklog-dir` with this
precedence:

1. **Explicit `--worklog-dir` value** (from a CLI flag / caller)
2. **Prefix-to-sibling scan** — the work-item id prefix (e.g. `OSL`) is matched
   against sibling projects' `config.yaml` so a non-SorraAgents item resolves
   to its own worklog store even when the harness cwd is the framework repo
3. **cwd chain** — `<cwd>/.worklog`, git root, nearest initialized ancestor
4. **No flag** — `wl` resolves from cwd (failures surface real error detail)

The script resolves the correct worklog store regardless of the directory it
is invoked from. See `docs/dev/worklog-sync.md` for the shared resolution
order and `wl sync` failure modes.

## Issue type decision guide (full)

**Issue type decision guide** — use these rules to assign the correct `issueType`:

| Type     | Use when… | Do NOT use when… | Examples |
|----------|-----------|-------------------|----------|
| `bug`    | Something is currently **incorrect or broken** and needs fixing. The change corrects existing wrong behavior. | The work adds new behavior or capability. | Fixing a crash, correcting a wrong calculation, patching a security vulnerability, handling an edge case that causes incorrect output. |
| `feature` | The work **adds new capability or functionality** that did not exist before. It introduces net-new behavior. | The work only fixes something that is already broken. | New API endpoint, new UI component, new integration, new command/flag. |
| `chore`  | The work **does not change code behavior** — it is maintenance or housekeeping. This includes changes to configuration, CI, documentation, dependencies, or formatting. | The work changes how the application behaves. | Dependency updates, CI configuration changes, documentation updates, code formatting, license files, build script tweaks. |
| `task`   | The work is general-purpose and does not fit cleanly into the other categories. | The work clearly fixes a bug, adds a feature, or is pure maintenance. | Writing tests, refactoring, performance profiling, investigation, benchmarking. |
| `epic`   | The work is **large in scope** and must be decomposed into multiple subtasks. Typically an epic is itself a feature or bug fix. | The work is small enough to complete in a single iteration. | Large feature spanning multiple services, major refactor across the codebase, migration from one technology to another. |

**Decision procedure** — when uncertain, ask:
1. *Is something currently broken or incorrect?* → `bug`
2. *Does this add net-new behavior/capability?* → `feature`
3. *Does this change NO code behavior (docs, CI, deps, formatting)?* → `chore`
4. *Is this general-purpose work (tests, refactoring, investigation)?* → `task`
5. *Is this large enough to need subtasks?* → `epic` (with children)

## Finishing output template (full)

# Objective

  Headline summary of the issue

# Acceptance Criteria

  Complete list of measurable acceptance criteria. If any are not measurable, add a clarifying question to the Appendix and mark as "TBD pending clarification".

  Always include:

- At least one criterion related to testing and validation.
- "All related documentation is updated to reflect the changes, including code comments, README, and any relevant wiki or docs site entries."
- "Full project test suite must pass with the new changes."

  > **Note:** CHANGELOG.md is **excluded** from this list. It is managed automatically by the ship skill's release pipeline (`../ship/scripts/release/generate-changelog.js`). Implementing agents should not manually update CHANGELOG.md.

  Do not include CI/CD pipeline tests.

# Effort and Risk

  T-shirt sizing and one-line description of the biggest risks
```

- Finish with "This completes the Intake process for <work-item-id> <work-item-title>"

