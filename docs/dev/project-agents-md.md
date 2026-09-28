# Project AGENTS.md Installer

How a new project gets its project-local `AGENTS.md`, and how that file relates
to the global agent guidance installed by `scripts/install_pi.sh`.

## Model: single reference, never a copy

Agent guidance has two layers:

| Layer | File | Installed by | Content |
|-------|------|--------------|---------|
| **Global** | `~/.pi/agent/AGENTS.md` (symlink to this repo's `AGENTS_GLOBAL.md`) | [`scripts/install_pi.sh`](../../scripts/install_pi.sh) | The full agent workflow: core principles, Worklog (wl) lifecycle, coding disciplines, push policy, triage policy |
| **Project** | `<project>/AGENTS.md` | [`scripts/init_project_agents.py`](../../scripts/init_project_agents.py) | The canonical reference structure only: a single reference to the global file plus a `## Project-specific guidance` placeholder |

The project file **must not** contain a copy of the global instruction set —
that duplicated ~20 KB per session (see SA-0MSITKHPW002XG4G) and drifted from
the global file whenever the global rules changed. The canonical project file
is exactly:

```markdown
## Global agent guidance

Read the global agent instructions at `~/.pi/agent/AGENTS.md` — they define the core principles, the Worklog (wl) work-item workflow, and the coding disciplines that apply to every project.

## Project-specific guidance

(project-specific rules are added by the project owner here, never by copying the global file)
```

The canonical template lives at
[`templates/project-AGENTS.md`](../../templates/project-AGENTS.md).

## Installing the project file

```bash
# Emit the canonical reference into the current project (creates AGENTS.md).
python3 scripts/init_project_agents.py

# Target another project directory and print machine-readable output.
python3 scripts/init_project_agents.py --target /path/to/project --json

# Preview without writing.
python3 scripts/init_project_agents.py --target /path/to/project --dry-run
```

### Behaviour

| Situation | Result |
|-----------|--------|
| No `AGENTS.md` | Created with the canonical structure |
| `AGENTS.md` already references the global file | No-op (idempotent) |
| `AGENTS.md` has custom rules, no reference (default `--mode append`) | Reference prepended; custom rules preserved below it |
| `AGENTS.md` has custom rules, `--mode overwrite` | Replaced with the canonical structure |
| `AGENTS.md` has custom rules, `--mode skip` | Left unchanged |

Re-running the installer never duplicates the reference: files that already
contain the `## Global agent guidance` heading (or the legacy pointer line
`Follow the global AGENTS.md …`) are treated as already installed.

### Explicit opt-in: copy the global file

For offline or standalone use, copying the full global file is still possible —
but only as an explicit opt-in, never the default:

```bash
python3 scripts/init_project_agents.py --copy-global --global-source ~/path/to/AGENTS_GLOBAL.md
```

`--copy-global` duplicates the global instruction set into the project (the
legacy behaviour). It accepts `--global-source PATH`; when omitted it resolves
this repo's `AGENTS_GLOBAL.md`, then `~/.pi/agent/AGENTS.md`.

## Relationship to `wl init`

ContextHub's `wl init` emits the same canonical reference structure for
projects that use the Worklog CLI, and delegates workflow setup to the global
SorraAgents install when it is detected (ContextHub
WL-0MSIXMKOX0052514, commit `dd048f49`). `scripts/init_project_agents.py` is
the SorraAgents-side installer for projects that do not use `wl init`; both
produce the canonical structure so the two paths cannot drift.

## Related

- [`docs/dev/skills-script-paths.md`](skills-script-paths.md#initializing-a-new-project-global-install) — installing the global skills/guidance for a new project.
- [`templates/project-AGENTS.md`](../../templates/project-AGENTS.md) — the canonical template.
- [`tests/test_init_project_agents.py`](../../tests/test_init_project_agents.py) — behaviour tests.
