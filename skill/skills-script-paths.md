# Skill script & asset path best practices

This document summarises recommended practices for Pi skill authors in this repository when referencing scripts and other on-disk assets. Follow these rules to avoid runtime failures when a skill is invoked from a repository that does not contain the expected files.

Principles
----------
- Skills are portable: they ship with their own `scripts/` and `assets/` directories under the skill directory. Use skill-relative references when invoking your bundled helpers.
- Do not assume repository-local scripts exist. When a skill documents or expects a repository-level script (for example `scripts/release/merge-dev-to-main.sh`) treat that script as optional and implement defensiveness.
- Always check for the existence (and executable bit where appropriate) of any external script before invoking it.
- Prefer a small, skill-shipped wrapper that performs the safety checks and presents a clear, actionable message to humans when the repository lacks the script.
- Write tests that simulate repository states (script present / script missing) and assert that the skill behaves safely and prints helpful instructions.

Quick checklist for skill authors
--------------------------------
1. Prefer skill-relative helpers
   - If the skill needs helper code, keep it under `skill/<skill-name>/scripts/` and invoke it with skill-relative paths from the documentation and from other skill modules.
   - Example: `node skill/ship/scripts/run-release.js` (skill-shipped wrapper) rather than calling a repository-local script directly.

2. Defensive invocation of repository scripts
   - Always check for the file before running it:
     - POSIX shell: `if [ -x scripts/release/merge-dev-to-main.sh ]; then bash scripts/release/merge-dev-to-main.sh; else echo "Missing script"; fi`
     - Node.js: `import { existsSync } from 'fs'; if (!existsSync(path)) { /* print helpful message and exit non-zero */ }`
   - Provide a clear human-friendly error explaining the missing file and listing the manual fallback steps (link to docs when available).

3. Provide a safe wrapper
   - Ship a small wrapper inside the skill that performs the check and forwards arguments to the repository script if present.
   - This wrapper gives a single stable entrypoint that agents and subagents can call across repositories.
   - Example: `skill/ship/scripts/run-release.js` — checks for `scripts/release/merge-dev-to-main.sh`, prints fallback guidance if missing, or executes the script if present.

4. Document the wrapper in SKILL.md
   - Update the skill's SKILL.md to recommend the wrapper as the preferred invocation and preserve the older direct commands as legacy notes.

5. Tests and CI
   - Add unit tests that assert the wrapper reports the missing-script condition and returns a non-zero exit code.
   - Where practical, add integration or manual tests that exercise the present-script path.

6. When referencing absolute paths
   - Avoid absolute paths or hard-coded local user paths in SKILL.md or scripts. Prefer relative or skill-resolved paths.

Examples
--------
- Good: `node skill/ship/scripts/run-release.js` — wrapper detects missing repo script and prints `Ship automated release unavailable: repository is missing the canonical release script 'scripts/release/merge-dev-to-main.sh'. See docs/dev/release-process.md for manual steps.`
- Bad: `bash scripts/release/merge-dev-to-main.sh` — direct invocation will fail in repositories that lack the script and produce unhelpful errors unless the caller checked first.

References
----------
- Pi skills docs: `~/.pi/agent/skills` and the Pi documentation `docs/skills.md` shipped with Pi. Important guidance: use relative paths from the skill directory and check that assets exist before executing repository-local scripts.

Repository-specific notes
-------------------------
- This repository ships `skill/ship/scripts/run-release.js` as the recommended safe wrapper for the release action. Use it when implementing or invoking the Ship skill's release behaviour.

Contact
-------
If you're unsure about a particular skill's invocation semantics or need help writing a safe wrapper, open a child work item under the parent `Audit and remediate skills for robust script references (SA-0MPVIZEVE0002CIA)` and assign it to the agent for remediation.
