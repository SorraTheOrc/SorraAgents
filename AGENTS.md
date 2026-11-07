## Global agent guidance

Read the global agent instructions at `~/.pi/agent/AGENTS.md` — they define the core principles, the Worklog (wl) work-item workflow, and the coding disciplines that apply to every project. That file is installed from this repository's `AGENTS_GLOBAL.md` by `scripts/install_pi.sh`, which symlinks it into place.

## Worktree hygiene

- Run `scripts/hygiene_check.sh` periodically to detect orphaned stashes and dirty main checkouts before they block automated agents.
- When `implement.py start` warns about orphaned stashes, triage them using the [recovery playbook](skill/implement/SKILL.md#dirty-main-checkout-recovery-playbook). Never stash or delete stashes without explicit operator permission.

## Compaction Configuration

Pi client-side auto-compaction is **disabled** (`.pi/settings.json` → `compaction.enabled: false`) for the Local Proxy/plan fallback model path. The Local Proxy at `192.168.0.199:8000` handles server-side compaction at ~88k/120k tokens before reaching the Qwen context limit. Pi's client-side compaction double-fires and miscalculates context size because the proxy has no `contextWindow` configured, causing Pi to assume a default 128k window and use stale `totalTokens` values for threshold estimation. This configuration change prevents compaction artefacts (stale token counts, double-fired compaction cycles) in long sessions using the Local Proxy fallback path. See **SA-0MTT7EUH30054I41** for full details.

## Project-specific guidance

- This repository is the **canonical source** for the pi agent infrastructure: `skill/` (skills), `command/` (prompts/commands), and `AGENTS_GLOBAL.md` (global agent guidance).
- Run `scripts/install_pi.sh` from this repo to (re)install the global symlinks: `~/.pi/agent/skills`, `~/.pi/agent/prompts`, and `~/.pi/agent/AGENTS.md`.
- Global pi configuration for the agent is tracked in `.pi-config/agent/` (`settings.json`, `models.json`) and installed/exported by the same script. Never store real credentials (`auth.json`) in the repository — run `pi login` locally instead.
- Changes to `skill/`, `command/`, or `AGENTS_GLOBAL.md` affect every project once installed, so they must be tracked here with work items, built, tested, and pushed to `dev` per the global workflow before the install script is re-run.
- When adding or modifying skills/commands, follow the conventions of the existing files and keep tests in the corresponding `tests/` directories.

### Game Scenes and Gym Scenes Parity

Game scenes and Gym Scenes must implement identical functionality. Gym Scenes
provide a deterministic, headless environment for exercising the same game
logic — not a separate gameplay variant — so any functional divergence between
a game scene and its corresponding Gym Scene is a bug in one or both
implementations. If divergence appears, extract the shared logic into a common
helper rather than maintaining two copies. When testing, avoid the
[`test-writing-guidelines.md`](skill/shared/test-writing-guidelines.md)
anti-patterns **Self-Referential Simulations** (a Gym Scene test re-implementing
game logic instead of exercising production code) and **Duplicates of Existing
Core Coverage** (a Gym Scene test re-testing behaviour already covered by a
core game test).

## Work-item completion conventions

- **`## Impact` is required on newly completed items.** Before advancing a work item to `in_review`, add an `## Impact` section to its description: **one sentence describing the user-experience impact** — what a user can now do, see, or stop worrying about — not a technical summary of what was built. Standup reports quote it verbatim.
- **Existing completed items are deliberately not retrofitted.** The convention applies to items completed from now on.

The canonical wording lives in `AGENTS_GLOBAL.md` (installed as `~/.pi/agent/AGENTS.md`).
