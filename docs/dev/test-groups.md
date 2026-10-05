# Test Groups

> **Work item:** SA-0MUJKF2HP001BR51 — Provide targeted test groups for
> stage-aware testing (avoid full-suite Phase 2 timeouts).

## Overview

Named test groups let agents and CI run a *targeted subset* of the full test
suite, selected either explicitly via `--group <name>` or implicitly from
changed files.  Groups are defined per-project in
`.pi/test-config.json`.

## Schema

```jsonc
{
  "timeoutPerCommand": 1500,
  "groups": {
    "<group-name>": {
      "description": "Human-readable description.",
      "commands": [
        "pytest -q -r a --disable-warnings <path>"
      ],
      "paths": [
        "skill/audit/**"
      ]
    }
  }
}
```

| Field        | Type     | Required | Description                                                    |
|------------- |----------|----------|----------------------------------------------------------------|
| `description` | string | No       | Human-readable group description (informational only).         |
| `commands`    | string[] | Yes      | Non-empty list of test commands to run for this group.         |
| `paths`       | string[] | Yes      | Glob patterns that, when matched by changed files, trigger this group's selection. |

### Special groups

- **`full`** — the fallback group.  Its commands are resolved at runtime from
  the existing `full_suite_commands` resolution (extension → pytest → node →
  npm).  If not defined in config, it is injected automatically with
  `paths: ["**"]`.

### Path patterns

Path patterns use Python's `fnmatch.fnmatchcase`:

| Pattern         | Matches                           |
|-----------------|-----------------------------------|
| `skill/audit/**` | Anything under `skill/audit/`     |
| `skill/test_cache.py` | Exact file match             |
| `**`              | Everything (used by `full`)       |

## CLI

### `--group <name>`

Run a named test group:

```bash
python3 run_tests.py --group audit
```

The group's commands override suite-based resolution.  The group name is
recorded in the cache key as `type: group:<name>` so cached entries for
different groups never collide.

### `--list-groups`

Print available group names (one per line) and exit:

```bash
python3 run_tests.py --list-groups
# Output:
# audit
# full
# test
```

When no groups are defined, prints a message and exits 0.

## Changed-file → group selector

When `run_tests.py` is invoked with `--scope changed` (or when
`implement.py finish` runs in iteration mode), the selection follows this
order:

1. **Named group selection** — changed files are matched against each group's
   `paths` spec.  The **smallest** (fewest commands) matching group is
   selected.  The `full` group is excluded from selection — it is the
   implicit fallback.
2. **Changed-file → test-file selector** — the existing convention mapping
   (source file → test file) is tried.
3. **Full suite fallback** — when neither group nor file-level selection
   produces a subset, the full suite runs.

### Examples

| Changed files                          | Selected group | Rationale                           |
|---------------------------------------|---------------|-------------------------------------|
| `skill/audit/scripts/audit_runner.py` | `audit`       | Matches `skill/audit/**`            |
| `skill/test/tests/test_foo.py`        | `test`        | Matches `skill/test/**`             |
| `docs/guide.md`                        | *full*        | No group matches → fallback         |
| `package.json`                         | *full*        | No group matches → fallback         |

## Integration with `implement.py finish`

During the build → test → commit loop in `implement.py finish`, the `run_tests`
function (with `scope="changed"`) now tries group selection *before* the
existing changed-file → test-file selector.  This means an audit-skill change
runs only the `audit` group instead of every test in the repo, dramatically
reducing iteration time.

The full group remains the **gate** before pushing to `dev`/`main` —
`implement.py finish` always runs the full suite as a final gate regardless of
which group was used during iteration.

## Cache key compatibility

Group-scoped runs are keyed independently: the cache key includes
`type: group:<name>` (e.g. `type: group:audit`), so cached entries for
different groups never collide, and a group run never overwrites a full-suite
cache entry.

## Testing

Tests live in `skill/test/tests/test_run_tests_groups.py` and cover:

- Group config reading and schema validation.
- CLI `--group` and `--list-groups` argument handling.
- Changed-file → group selector logic (matching, specificity, fallback).
- `implement.py` import-level integration checks.
- `group_commands` and `group_scope_commands` resolution.
