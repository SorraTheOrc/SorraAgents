# Skills Script & Asset Path Best Practices

> **Audience:** Skill authors and maintainers working in this repository.
> **Purpose:** Establish consistent, robust conventions for referencing scripts,
> assets, and other files from within a skill's `SKILL.md` and related
> documentation.
> **Authoritative upstream reference:** [Pi Skills
> documentation](https://github.com/earendil-works/pi-coding-agent/blob/main/docs/skills.md)
> (see also the local copy in the installed pi package, e.g.
> `~/.nvm/versions/node/v24.11.1/lib/node_modules/@earendil-works/pi-coding-agent/docs/skills.md`).

---

## Table of Contents

- [Principle: Skill-Relative Paths](#principle-skill-relative-paths)
- [Recommended Patterns](#recommended-patterns)
  - [Referencing Scripts](#referencing-scripts)
  - [Referencing Assets and Documentation](#referencing-assets-and-documentation)
  - [Cross-Skill References](#cross-skill-references)
- [Defensive Existence Checks](#defensive-existence-checks)
- [Fallback Behaviour for Missing Scripts](#fallback-behaviour-for-missing-scripts)
- [Examples from This Repository](#examples-from-this-repository)
  - [Recommended (Skill-Relative)](#recommended-skill-relative)
  - [Discouraged (Repo-Root-Relative)](#discouraged-repo-root-relative)
  - [Cross-Skill References (Use with Caution)](#cross-skill-references-use-with-caution)
- [Summary Decision Table](#summary-decision-table)
- [Related Work](#related-work)

---

## Principle: Skill-Relative Paths

**Always use paths relative to the skill's own directory** (`./scripts/`,
`./references/`, `./assets/`) when referencing files that are bundled with the
skill. This is the pattern prescribed by the upstream Pi skills documentation:

> Use relative paths from the skill directory.
>
> *— Pi Skills documentation, "Skill Structure" section*

**Why skill-relative paths?**

| Benefit | Explanation |
|---------|-------------|
| **Portability** | The skill works regardless of which repository or directory it is installed in. A repo-root-absolute path like `skill/foo/scripts/bar.py` breaks when the skill is moved or symlinked elsewhere. |
| **Clarity** | `./scripts/foo.py` immediately signals "this is a script bundled with this skill." A path like `skill/foo/scripts/foo.py` could be mistaken for a reference to a project-level script. |
| **Discoverability** | An agent loading the skill sees `./scripts/` and knows the scripts directory is in the same folder as the `SKILL.md` being read, simplifying reasoning. |
| **Alignment with upstream** | The Pi skills specification explicitly recommends relative paths from the skill directory. Following this convention ensures consistency across all Pi ecosystem skills. |

---

## Recommended Patterns

### Referencing Scripts

When a skill needs to invoke one of its own scripts, use a skill-relative path
from the `SKILL.md`:

```bash
# ✅ Recommended — skill-relative path
python3 ./scripts/process.py <input>

# ✅ Also acceptable (identical)
node ./scripts/analyze.js <input>
```

**Discouraged — repo-root-relative path** (breaks when the skill is used
outside this repository):

```bash
# ❌ Discouraged — repo-root-relative
python3 skill/my-skill/scripts/process.py <input>
```

### Referencing Assets and Documentation

Reference documentation files, templates, and other assets using the same
skill-relative convention:

```markdown
<!-- ✅ Recommended — skill-relative path -->
See [the reference guide](references/REFERENCE.md) for details.

<!-- ✅ Recommended — asset path -->
See [template](assets/template.json) for an example.
```

```markdown
<!-- ❌ Discouraged — repo-root-relative -->
See [the reference guide](skill/my-skill/references/REFERENCE.md) for details.
```

### Cross-Skill References

When a skill needs to reference a script from another skill (e.g., shared
utility scripts), prefer skill-relative paths **from the consuming skill's
perspective** and document the external dependency explicitly.

```bash
# ✅ Acceptable — cross-skill reference with explicit dependency note
# NOTE: This script is provided by the 'ship' skill.
node ./node_modules/skill-ship/scripts/git-helpers.js
```

However, cross-skill references are inherently fragile and should be used
sparingly. When they are necessary, the consuming skill's SKILL.md should:

1. Document the external dependency in a clear "Dependencies" section.
2. Include a defensive existence check (see next section).
3. Provide a fallback for when the external skill is not installed.

---

## Defensive Existence Checks

**Always check that a script or file exists before attempting to execute it.**
This is especially important when:

- The script is repository-specific (not bundled with the skill).
- The script lives in another skill's directory (cross-skill reference).
- The script path is constructed dynamically.

### Python Pattern

```python
import os
import sys
import subprocess

script_path = os.path.join(os.path.dirname(__file__), "scripts", "process.py")

if not os.path.isfile(script_path):
    print(f"ERROR: Required script not found: {script_path}", file=sys.stderr)
    print("This skill requires the 'my-skill' package to be installed.", file=sys.stderr)
    print("See https://example.com/setup for installation instructions.", file=sys.stderr)
    sys.exit(1)

subprocess.run([sys.executable, script_path], check=True)
```

### JavaScript / Node.js Pattern

```javascript
const fs = require('fs');
const path = require('path');
const { execSync } = require('child_process');

const scriptPath = path.join(__dirname, 'scripts', 'process.js');

if (!fs.existsSync(scriptPath)) {
  console.error(`ERROR: Required script not found: ${scriptPath}`);
  console.error('This skill requires the \'my-skill\' package to be installed.');
  console.error('See https://example.com/setup for installation instructions.');
  process.exit(1);
}

execSync(`node "${scriptPath}"`, { stdio: 'inherit' });
```

### Inline Existence Check in SKILL.md

For simple cases, a shell-level existence check can be used directly in the
SKILL.md commands:

```bash
# ✅ Recommended — inline existence check with clear fallback
script="./scripts/process.sh"
if [ ! -f "$script" ]; then
  echo "ERROR: $script not found." >&2
  echo "Ensure the skill is installed correctly." >&2
  exit 1
fi
bash "$script"
```

For Python and Node.js invocations from SKILL.md, it is better to delegate the
existence check to the script itself (using the patterns above) rather than
relying on shell commands in the markdown.

---

## Fallback Behaviour for Missing Scripts

When a required script is missing, the skill MUST:

1. **Refuse automated execution.** Do not silently skip the step or use a
   degraded fallback that could produce incorrect results.
2. **Print a clear, human-readable error message** that includes:
   - The expected path of the missing file.
   - The skill or package that should provide the file.
   - A link to documentation or setup instructions.
3. **Exit with a non-zero status code** so calling agents or CI pipelines
   detect the failure.

### Example Error Message Template

```
ERROR: Required script not found: skill/my-skill/scripts/process.py
This script is part of the 'my-skill' skill package.
Installation instructions: https://example.com/my-skill#setup
```

### Example Good Message vs Poor Message

```text
# ✅ Good — actionable, informative
ERROR: Required script not found: skill/ship/scripts/git-helpers.js
This helper is provided by the Ship skill.
See skill/ship/SKILL.md for setup instructions.

# ❌ Poor — vague, unhelpful
File not found. Please check your installation.
```

---

## Examples from This Repository

The following examples are drawn from the actual skills in this repository to
illustrate the patterns described above.

### Recommended (Skill-Relative)

| Skill | Pattern | Example |
|-------|---------|---------|
| `cleanup` | `./scripts/` (when invoking prune script) | `python ./scripts/prune_local_branches.py --dry-run \` (from `skill/cleanup/SKILL.md`) |

The `cleanup` skill's SKILL.md includes an invocation using a skill-relative
path:

```bash
python ./scripts/prune_local_branches.py --dry-run \
  --branches feature-branch-1 feature-branch-2
```

### Discouraged (Repo-Root-Relative)

Many skills in this repository currently use repo-root-relative paths. These
work in the context of this specific repository but would break if the skill
were used elsewhere. They are listed here as **discouraged patterns** to avoid
in future skill development.

| Skill | Discouraged Pattern | Would Break If... |
|-------|--------------------|-------------------|
| `ship` | `node skill/ship/scripts/ship.js` | Skill is moved to another repo or path |
| `ship` | `node skill/ship/scripts/git-helpers.js` | Same as above |
| `ship` | `node skill/ship/scripts/check-unmerged-branches.js` | Same as above |
| `ship` | `node skill/ship/scripts/run-release.js` | Same as above |
| `cleanup` | `python skill/cleanup/scripts/inspect_current_branch.py` | Same as above |
| `cleanup` | `python skill/cleanup/scripts/switch_to_default_and_update.py` | Same as above |
| `cleanup` | `python skill/cleanup/scripts/summarize_branches.py` | Same as above |
| `cleanup` | `python skill/cleanup/scripts/prune_local_branches.py` | Same as above |
| `cleanup` | `python skill/cleanup/scripts/delete_remote_branches.py` | Same as above |
| `triage` | `python3 skill/triage/scripts/check_or_create.py` | Same as above |
| `audit` | `python3 skill/audit/scripts/audit_runner.py` | Same as above |
| `audit` | `python3 skill/audit/scripts/persist_audit.py` | Same as above |

**Important:** This document is guidance-only. Remediation of existing skills
to use skill-relative paths is completed under the parent epic
[SA-0MPVIZEVE0002CIA](#related-work).

### Cross-Skill References (Use with Caution)

The `git-management` skill references scripts from the `ship` and `cleanup`
skills. These are cross-skill dependencies that should be clearly documented.

| Consuming Skill | Reference | External Provider |
|----------------|-----------|-------------------|
| `git-management` | `skill/ship/scripts/git-helpers.js` | `ship` skill |
| `git-management` | `skill/ship/scripts/ship.js` | `ship` skill |
| `git-management` | `skill/cleanup/scripts/` | `cleanup` skill |

The `audit` skill also references a script from the `code-review` skill:

| Consuming Skill | Reference | External Provider |
|----------------|-----------|-------------------|
| `audit` | `skill/code_review/scripts/code_quality.py` | `code-review` skill |

When making cross-skill references, always include a defensive existence check
and document the external dependency in the consuming skill's SKILL.md.

---

## Summary Decision Table

| Scenario | Recommended Path Style | Existence Check Required? |
|----------|----------------------|--------------------------|
| Script bundled with the same skill | `./scripts/foo.py` | Optional (recommended) |
| Asset bundled with the same skill | `./assets/template.json` | No |
| Documentation bundled with the same skill | `./references/REFERENCE.md` | No |
| Script from another skill in the same repo | `./scripts/foo.py` (via symlink or shared location) | **Yes** |
| Repository-specific script (not part of any skill) | Document path in SKILL.md preamble | **Yes** |
| External tool (system-wide, e.g. `git`, `node`) | Use bare name (no path prefix) | No |

---

## Related Work

- **Parent epic:**
  [SA-0MPVIZEVE0002CIA](https://github.com/earendil-works/SorraAgents/issues/1)
  — "Audit and remediate skills for robust script references (Ship subagent
  failures)" — this document is acceptance criterion #1 of that epic.
- **Pi Skills documentation:** Authoritative reference for skill structure and
  path conventions (installed at
  `~/.nvm/versions/node/v24.11.1/lib/node_modules/@earendil-works/pi-coding-agent/docs/skills.md`;
  also available at
  [GitHub](https://github.com/earendil-works/pi-coding-agent/blob/main/docs/skills.md)).
- **Agent Skills specification:** [https://agentskills.io/specification](https://agentskills.io/specification)
- **Skills in this repository:** `skill/ship/`, `skill/cleanup/`,
  `skill/triage/`, `skill/audit/`, `skill/git-management/`
