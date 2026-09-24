# Local Skill Extensions

> **Audience:** Skill authors and project maintainers.
> **Purpose:** Define the convention that lets an invoking project augment a
> global skill with project-local prose hooks and machine-readable data,
> without forking or editing the global skill.
> **Contract owner:** SA-0MSQ7MQEJ0064ZB0. **Reference consumer:**
> SA-0MTJQB2MA008HMO6 (test skill `--type`).

---

## Overview

Global skills shipped by this repository are static and shared by every
project. Project-specific behaviour (additional test types, extra validation,
local gates) must be supplied by the consuming project, not hardcoded here.
The convention below provides a single, repo-root-scoped discovery path for
that augmentation.

When **no extension exists, global skill behaviour is byte-for-byte
unchanged** — absence is a no-op, never an error.

## Convention and discovery

A project extends a skill by creating a directory under its own repo root:

```
<project_root>/.pi/skills_extensions/<skill-name>/
├── SKILL_PREFIX.md     # optional prose, injected before the first step
├── SKILL_POSTFIX.md    # optional prose, injected after the final step
└── extension.json      # optional machine-readable data
```

- **`<project_root>`** is the invoking git root
  (`git rev-parse --show-toplevel`), falling back to the invoking cwd when
  git is unavailable. Resolution is handled by
  `skill/shared/skill_extensions.py::resolve_project_root()`. This mirrors
  the existing `detect_project_root()` used by the test skill, so an
  extension is found from any cwd, including inside a git worktree.
- **`<skill-name>`** is the global skill's directory name (e.g. `test`,
  `audit`, `implement`). It must be a single path component; traversal
  (`..`, `/`) is rejected.
- This is the **only** extension discovery path. It does not replace or
  compete with `.pi/test-config.json` (the test skill's full-suite override);
  see [Precedence](#precedence-and-interaction-with-pi-test-configjson).

## Prose hooks

`SKILL_PREFIX.md` and `SKILL_POSTFIX.md` are optional Markdown fragments
surfaced to the agent running the skill:

| File | When surfaced |
|------|---------------|
| `SKILL_PREFIX.md` | After the skill's gating steps, before its first actionable step. |
| `SKILL_POSTFIX.md` | After the skill's final step completes. |

They are **strictly additive**: they may add guidance, but they cannot
disable, reorder or weaken a skill's safety or gating steps (plan approval,
audit gates, build → test → commit order). A missing file is simply absent.

## Machine-readable data (`extension.json`)

`extension.json` must parse to a **JSON object** at the top level. The schema
of that object is owned by the *consuming skill*; the shared loader
guarantees only that it parses.

```json
{
  "types": {
    "unit": ["pytest tests/unit -q"],
    "smoke": ["pytest tests/smoke -q"],
    "dev": ["pytest tests/unit tests/integration -q"]
  }
}
```

Guidance:

- Prefer command **lists** (arrays of strings) so a type can map to more than
  one command; the consuming skill should also accept a single string.
- Keep project-specific commands out of the global skill — this file is
  exactly where they belong.
- Unknown top-level keys are preserved by the loader (forward compatibility):
  a skill ignores keys it does not know, and future skills may define new
  ones.
- **Malformed input is loud.** Unparseable JSON, an unreadable file, or a
  non-object top level raises `SkillExtensionError` naming the file. The
  loader never silently substitutes a default.

### Loader API (`skill/shared/skill_extensions.py`)

```python
from shared.skill_extensions import load_extension

ext = load_extension("test")           # project root resolved from cwd
if ext.present:
    types = ext.data.get("types", {})
```

- `load_extension(skill_name, project_root=None) -> SkillExtension`
- `SkillExtension` fields: `skill_name`, `project_root`, `directory`,
  `prefix`, `postfix`, `data`, `prefix_path`, `postfix_path`, `data_path`,
  `present`.
- `extension_dir(skill_name, project_root=None) -> Path`
- `resolve_project_root(start=None) -> Path`
- `SkillExtensionError` — raised for malformed/unreadable present files.

## Precedence and interaction with `.pi/test-config.json`

These are complementary, non-overlapping extension points:

1. **`.pi/skills_extensions/<skill>/extension.json`** — general per-skill
   project extension (this convention), consumed by skill scripts that opt in.
2. **`.pi/test-config.json`** — the test skill's full-suite override
   (`suiteCommands`, `timeoutPerCommand`), unchanged by this convention.

For the test skill, the type→command map lives in the extension file. When a
type is not defined locally, the skill's own fallbacks apply (see
`docs/dev/test-skill-reference.md`). Presence of an extension directory does
not change how `.pi/test-config.json` is read, and vice versa.

## Trust and security

Extension fragments are instructions and commands supplied by the consuming
project. They carry **the same trust level as project skills** — treat an
untrusted project's extension directory with the same caution as its source
code. Extensions are additive only and cannot bypass a skill's safety gates
(plan approval, audit gates, build → test → commit ordering).

## Example: adding test types from a project

A project that wants a fast `unit` profile and a custom `e2e` profile writes
`<project_root>/.pi/skills_extensions/test/extension.json`:

```json
{
  "types": {
    "unit": ["npx vitest run --project unit"],
    "e2e": ["npx playwright test"]
  }
}
```

The global test skill then dispatches `--type unit` / `--type e2e` to those
commands without any change to the installed skill.

## Verification

- Loader unit tests: `skill/shared/tests/test_skill_extensions.py`.
- Consumer tests: `skill/test/tests/test_run_tests_type.py`.

## Related work

- **SA-0MSQ7MQEJ0064ZB0** — Local skill extensions for global skills (this
  convention and loader).
- **SA-0MTJQB2MA008HMO6** — Test skill `--type` parameter (reference
  consumer of the machine-readable map).
- `docs/dev/skills-script-paths.md` — path-resolution conventions for skills.
- `docs/dev/test-skill-reference.md` — the test skill's type/scope/cache
  model.
