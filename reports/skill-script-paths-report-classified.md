# Classified skill script/path references report

## __pycache__

- path: skill/__pycache__
- status: PASS

## audit

- path: skill/audit
- status: PASS

### Safe matches (informational)

- skill/audit/SKILL.md:159 — - Path: `skill/audit/scripts/persist_audit.py`
- skill/audit/SKILL.md:161 — - Persist from stdin: `cat report.md | python3 skill/audit/scripts/persist_audit.py --issue-id SA-123`
- skill/audit/SKILL.md:162 — - Persist from a file: `python3 skill/audit/scripts/persist_audit.py --issue-id SA-123 --file report.md`
- skill/audit/SKILL.md:163 — - Persist from a CLI string: `python3 skill/audit/scripts/persist_audit.py --issue-id SA-123 --report "Ready to close: Yes\n..."`

## author-command

- path: skill/author-command
- status: PASS

## changelog-generator

- path: skill/changelog-generator
- status: PASS

## cleanup

- path: skill/cleanup
- status: PASS

### Safe matches (informational)

- skill/cleanup/SKILL.md:26 — - The skill ships a set of deterministic scripts under `./scripts/` that implement the non-interactive behaviour described below. Each script supports `--dry-run`, `--yes`, `--report <path>`, `--quiet`, and `--verbose`.
- skill/cleanup/SKILL.md:26 — - The skill ships a set of deterministic scripts under `./scripts/` that implement the non-interactive behaviour described below. Each script supports `--dry-run`, `--yes`, `--report <path>`, `--quiet`, and `--verbose`.
- skill/cleanup/SKILL.md:47 — Use `skill/cleanup/scripts/inspect_current_branch.py` to inspect the current branch, detect the default branch, fetch `origin --prune` when needed, determine merge status, last commit, unpushed commits, and parse work item token. The agent MUST run this script by default and only perform inline git inspections if an edge case (see "Preferred execution behaviour") applies and the operator approves.
- skill/cleanup/SKILL.md:74 — python skill/cleanup/scripts/inspect_current_branch.py --report /tmp/cleanup/inspect_current.json
- skill/cleanup/SKILL.md:74 — python skill/cleanup/scripts/inspect_current_branch.py --report /tmp/cleanup/inspect_current.json
- skill/cleanup/SKILL.md:89 — Run `skill/cleanup/scripts/switch_to_default_and_update.py` to fetch, check out the default branch, and perform a fast-forward pull. The agent MUST run this script by default (see Preferred execution behaviour) and only attempt manual git switch/pull sequences when explicitly instructed by the human in an allowed edge case.
- skill/cleanup/SKILL.md:96 — python skill/cleanup/scripts/switch_to_default_and_update.py --report /tmp/cleanup/switch_default.json
- skill/cleanup/SKILL.md:96 — python skill/cleanup/scripts/switch_to_default_and_update.py --report /tmp/cleanup/switch_default.json
- skill/cleanup/SKILL.md:101 — Run `skill/cleanup/scripts/summarize_branches.py` to list local branches and include any open PRs targeting the default branch. The agent MUST run this script by default and present the script-generated report, in markdown format, for any deletion decisions.
- skill/cleanup/SKILL.md:110 — python skill/cleanup/scripts/summarize_branches.py --report /tmp/cleanup/branches.json
- ... and 11 more

## code-review

- path: skill/code-review
- status: PASS

## effort-and-risk

- path: skill/effort-and-risk
- status: NEEDS_REVIEW

### Needs attention

- skill/effort-and-risk/SKILL.md:73 — python3 scripts/run_skill.py --issue <issue-id> <<'JSON' > final-<issue-id>.json — classification: should_use_skill_relative
- skill/effort-and-risk/SKILL.md:93 — - scripts/calc_effort.py — classification: should_use_skill_relative
- skill/effort-and-risk/SKILL.md:94 — - scripts/calc_risk.py — classification: should_use_skill_relative
- skill/effort-and-risk/SKILL.md:95 — - scripts/calc_effort_with_risk.py — classification: should_use_skill_relative
- skill/effort-and-risk/SKILL.md:96 — - scripts/assemble_json.py — classification: should_use_skill_relative
- skill/effort-and-risk/SKILL.md:97 — - scripts/json_to_human.py — classification: should_use_skill_relative
- skill/effort-and-risk/SKILL.md:98 — - scripts/orchestrate_estimate.py — classification: should_use_skill_relative

## find-related

- path: skill/find-related
- status: PASS

## implement

- path: skill/implement
- status: NEEDS_REVIEW

### Needs attention

- skill/implement/SKILL.md:173 — > See `skill/ship/SKILL.md` for the push-to-dev workflow and `skill/ship/scripts/run-release.js` (safe wrapper) for the release process. The wrapper detects when a repository lacks `scripts/release/merge-dev-to-main.sh` and prints a clear human fallback message. — classification: repo_script

### Safe matches (informational)

- skill/implement/SKILL.md:139 — - Example: `python3 skill/triage/scripts/check_or_create.py '{"test_name":"<name>", "stdout_excerpt":"...", "stack_trace":"..."}'`
- skill/implement/SKILL.md:163 — - Using the ship skill: `pushToDev()` from `skill/ship/scripts/ship.js` (preferred)

## implement-single

- path: skill/implement-single
- status: PASS

## owner-inference

- path: skill/owner-inference
- status: NEEDS_REVIEW

### Needs attention

- skill/owner-inference/SKILL.md:40 — - `scripts/infer_owner.py` — CLI entrypoint and library functions. — classification: should_use_skill_relative

## owner_inference

- path: skill/owner_inference
- status: PASS

### Safe matches (informational)

- skill/owner_inference/scripts/infer_owner.py:1 — # Compatibility shim: delegate to the implementation in skill/owner-inference/scripts/infer_owner.py

## ralph

- path: skill/ralph
- status: NEEDS_REVIEW

### Needs attention

- skill/ralph/SKILL.md:82 — # python3 /path/to/skills/ralph/scripts/ralph_loop.py <work-item-id> --json — classification: should_use_skill_relative

### Safe matches (informational)

- skill/ralph/ralph:8 — exec python3 "$BASEDIR/scripts/ralph_control.py" "$@"
- skill/ralph/ralph:10 — exec python3 "$BASEDIR/scripts/ralph_control.py" launch "$@"
- skill/ralph/SKILL.md:74 — # python3 skill/ralph/scripts/ralph_loop.py <work-item-id> --json
- skill/ralph/SKILL.md:77 — # python3 skill/ralph/scripts/ralph_loop.py <parent-id> --child <child-id> --json
- skill/ralph/tests/test_structured_response.py:39 — {"command": "edit", "args": ["skill/ralph/scripts/ralph_loop.py"]},
- skill/ralph/tests/test_structured_response.py:47 — assert parsed.text == "edit skill/ralph/scripts/ralph_loop.py"
- skill/ralph/tests/test_structured_response.py:48 — assert parsed.summary == "edit skill/ralph/scripts/ralph_loop.py"
- skill/ralph/tests/test_structured_response.py:49 — assert parsed.actions[0].render() == "edit skill/ralph/scripts/ralph_loop.py"

## resolve-pr-comments

- path: skill/resolve-pr-comments
- status: PASS

## ship

- path: skill/ship
- status: NEEDS_REVIEW

### Needs attention

- skill/ship/SKILL.md:62 — - `scripts/ship.js` — Push-to-dev behaviour module (exports: `pushToDev`, `pushToBranch`, `validatePushTarget`, `validateForcePush`, `DEV_BRANCH`, `PROTECTED_BRANCHES`, and re-exports from `git-helpers.js`) — classification: should_use_skill_relative
- skill/ship/SKILL.md:63 — - `scripts/git-helpers.js` — Branch naming and policy helpers (exports: `makeBranchName`, `validateBranchName`, `isBranchBlocked`, `BLOCKED_BRANCHS`, `BRANCH_NAME_PATTERN`) — classification: should_use_skill_relative
- skill/ship/SKILL.md:64 — - `scripts/release/merge-dev-to-main.sh` — the canonical release merge script — classification: repo_script
- skill/ship/SKILL.md:94 — 1. __Create a feature branch__ using `makeBranchName(workItemId, shortDesc)` from `scripts/git-helpers.js`. — classification: should_use_skill_relative
- skill/ship/SKILL.md:97 — 4. __Push to dev__ using `pushToDev()` from `scripts/ship.js`. This: — classification: should_use_skill_relative
- skill/ship/SKILL.md:166 — # bash scripts/release/merge-dev-to-main.sh — classification: repo_script
- skill/ship/SKILL.md:166 — # bash scripts/release/merge-dev-to-main.sh — classification: repo_script
- skill/ship/SKILL.md:200 — | __Automated script__ | Run `bash scripts/release/merge-dev-to-main.sh` manually | Ship subagent unavailable but script is available | — classification: repo_script
- skill/ship/SKILL.md:200 — | __Automated script__ | Run `bash scripts/release/merge-dev-to-main.sh` manually | Ship subagent unavailable but script is available | — classification: repo_script
- skill/ship/SKILL.md:221 — executor. The agent should invoke `scripts/release/merge-dev-to-main.sh` — classification: repo_script
- skill/ship/scripts/run-release.js:11 — const RELEASE_SCRIPT = 'scripts/release/merge-dev-to-main.sh'; — classification: repo_script
- skill/ship/scripts/run-release.js:26 — "If you want the agent to run an automated release, add the canonical script at 'scripts/release/merge-dev-to-main.sh' in the repository, or use the Ship subagent configured for this repo.", — classification: repo_script

### Safe matches (informational)

- skill/ship/SKILL.md:70 — import { pushToDev } from './scripts/ship.js';
- skill/ship/SKILL.md:70 — import { pushToDev } from './scripts/ship.js';
- skill/ship/SKILL.md:70 — import { pushToDev } from './scripts/ship.js';
- skill/ship/SKILL.md:78 — import { makeBranchName, validateBranchName, isBranchBlocked } from './scripts/git-helpers.js';
- skill/ship/SKILL.md:78 — import { makeBranchName, validateBranchName, isBranchBlocked } from './scripts/git-helpers.js';
- skill/ship/SKILL.md:78 — import { makeBranchName, validateBranchName, isBranchBlocked } from './scripts/git-helpers.js';
- skill/ship/SKILL.md:163 — node skill/ship/scripts/run-release.js

## triage

- path: skill/triage
- status: NEEDS_REVIEW

### Needs attention

- skill/triage/SKILL.md:37 — - `scripts/check_or_create.py` — implementation using `wl` CLI. — classification: should_use_skill_relative
