## Global agent guidance

Read the global agent instructions at `~/.pi/agent/AGENTS.md` — they define the core principles, the Worklog (wl) work-item workflow, and the coding disciplines that apply to every project. That file is installed from this repository's `AGENTS_GLOBAL.md` by `scripts/install_pi.sh`, which symlinks it into place.

## Worktree hygiene

- Run `scripts/hygiene_check.sh` periodically to detect orphaned stashes and dirty main checkouts before they block automated agents.
- When `implement.py start` warns about orphaned stashes, triage them using the [recovery playbook](skill/implement/SKILL.md#dirty-main-checkout-recovery-playbook). Never stash or delete stashes without explicit operator permission.

## Project-specific guidance

- This repository is the **canonical source** for the pi agent infrastructure: `skill/` (skills), `command/` (prompts/commands), and `AGENTS_GLOBAL.md` (global agent guidance).
- Run `scripts/install_pi.sh` from this repo to (re)install the global symlinks: `~/.pi/agent/skills`, `~/.pi/agent/prompts`, and `~/.pi/agent/AGENTS.md`.
- Global pi configuration for the agent is tracked in `.pi-config/agent/` (`settings.json`, `models.json`) and installed/exported by the same script. Never store real credentials (`auth.json`) in the repository — run `pi login` locally instead.
- Changes to `skill/`, `command/`, or `AGENTS_GLOBAL.md` affect every project once installed, so they must be tracked here with work items, built, tested, and pushed to `dev` per the global workflow before the install script is re-run.
- When adding or modifying skills/commands, follow the conventions of the existing files and keep tests in the corresponding `tests/` directories.
