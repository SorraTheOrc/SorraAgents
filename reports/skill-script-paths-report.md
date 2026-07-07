# Skill script/path references report

## __pycache__

- path: skill/__pycache__
- status: PASS

## audit

- path: skill/audit
- status: FAIL

### Matches

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
- status: FAIL

### Matches

- skill/cleanup/SKILL.md:26 — - The skill ships a set of deterministic scripts under `./scripts/` that implement the non-interactive behaviour described below. Each script supports `--dry-run`, `--yes`, `--report <path>`, `--quiet`, and `--verbose`.
- skill/cleanup/SKILL.md:47 — Use `skill/cleanup/scripts/inspect_current_branch.py` to inspect the current branch, detect the default branch, fetch `origin --prune` when needed, determine merge status, last commit, unpushed commits, and parse work item token. The agent MUST run this script by default and only perform inline git inspections if an edge case (see "Preferred execution behaviour") applies and the operator approves.
- skill/cleanup/SKILL.md:74 — python skill/cleanup/scripts/inspect_current_branch.py --report /tmp/cleanup/inspect_current.json
- skill/cleanup/SKILL.md:74 — python skill/cleanup/scripts/inspect_current_branch.py --report /tmp/cleanup/inspect_current.json
- skill/cleanup/SKILL.md:89 — Run `skill/cleanup/scripts/switch_to_default_and_update.py` to fetch, check out the default branch, and perform a fast-forward pull. The agent MUST run this script by default (see Preferred execution behaviour) and only attempt manual git switch/pull sequences when explicitly instructed by the human in an allowed edge case.
- skill/cleanup/SKILL.md:96 — python skill/cleanup/scripts/switch_to_default_and_update.py --report /tmp/cleanup/switch_default.json
- skill/cleanup/SKILL.md:96 — python skill/cleanup/scripts/switch_to_default_and_update.py --report /tmp/cleanup/switch_default.json
- skill/cleanup/SKILL.md:101 — Run `skill/cleanup/scripts/summarize_branches.py` to list local branches and include any open PRs targeting the default branch. The agent MUST run this script by default and present the script-generated report, in markdown format, for any deletion decisions.
- skill/cleanup/SKILL.md:110 — python skill/cleanup/scripts/summarize_branches.py --report /tmp/cleanup/branches.json
- skill/cleanup/SKILL.md:110 — python skill/cleanup/scripts/summarize_branches.py --report /tmp/cleanup/branches.json
- skill/cleanup/SKILL.md:115 — Use `skill/cleanup/scripts/prune_local_branches.py` with an explicit branch list derived from the summarize report and user input. The summarize report and user choice are the authoritative source; the prune script only deletes branches you pass in. The agent MUST NOT delete branches outside of the explicit branch list produced by the script and approved by the human.
- skill/cleanup/SKILL.md:121 — python skill/cleanup/scripts/prune_local_branches.py \
- skill/cleanup/SKILL.md:121 — python skill/cleanup/scripts/prune_local_branches.py \
- skill/cleanup/SKILL.md:126 — python ./scripts/prune_local_branches.py --dry-run \
- skill/cleanup/SKILL.md:133 — Run `skill/cleanup/scripts/delete_remote_branches.py` — deletes remote branches that are merged into default and older than a threshold (default 14 days). Report on branches deleted, skipped (e.g., due to open PRs), and any errors.
- skill/cleanup/SKILL.md:139 — python skill/cleanup/scripts/delete_remote_branches.py --days 14 --report /tmp/cleanup/delete_remote.json
- skill/cleanup/SKILL.md:139 — python skill/cleanup/scripts/delete_remote_branches.py --days 14 --report /tmp/cleanup/delete_remote.json
- skill/cleanup/SKILL.md:142 — python skill/cleanup/scripts/delete_remote_branches.py --days 14 --dry-run --report /tmp/cleanup/delete_remote.json
- skill/cleanup/SKILL.md:142 — python skill/cleanup/scripts/delete_remote_branches.py --days 14 --dry-run --report /tmp/cleanup/delete_remote.json

## code-review

- path: skill/code-review
- status: PASS

## effort-and-risk

- path: skill/effort-and-risk
- status: FAIL

### Matches

- skill/effort-and-risk/SKILL.md:73 — python3 scripts/run_skill.py --issue <issue-id> <<'JSON' > final-<issue-id>.json
- skill/effort-and-risk/SKILL.md:93 — - scripts/calc_effort.py
- skill/effort-and-risk/SKILL.md:94 — - scripts/calc_risk.py
- skill/effort-and-risk/SKILL.md:95 — - scripts/calc_effort_with_risk.py
- skill/effort-and-risk/SKILL.md:96 — - scripts/assemble_json.py
- skill/effort-and-risk/SKILL.md:97 — - scripts/json_to_human.py
- skill/effort-and-risk/SKILL.md:98 — - scripts/orchestrate_estimate.py

## find-related

- path: skill/find-related
- status: PASS

## implement

- path: skill/implement
- status: FAIL

### Matches

- skill/implement/SKILL.md:139 — - Example: `python3 skill/triage/scripts/check_or_create.py '{"test_name":"<name>", "stdout_excerpt":"...", "stack_trace":"..."}'`
- skill/implement/SKILL.md:163 — - Using the ship skill: `pushToDev()` from `skill/ship/scripts/ship.js` (preferred)
- skill/implement/SKILL.md:173 — > See `skill/ship/SKILL.md` for the push-to-dev workflow and `scripts/release/merge-dev-to-main.sh` for the release process.

## implement-single

- path: skill/implement-single
- status: PASS

## owner-inference

- path: skill/owner-inference
- status: FAIL

### Matches

- skill/owner-inference/SKILL.md:40 — - `scripts/infer_owner.py` — CLI entrypoint and library functions.

## owner_inference

- path: skill/owner_inference
- status: FAIL

### Matches

- skill/owner_inference/scripts/infer_owner.py:1 — # Compatibility shim: delegate to the implementation in skill/owner-inference/scripts/infer_owner.py

## ralph

- path: skill/ralph
- status: FAIL

### Matches

- skill/ralph/ralph:8 — exec python3 "$BASEDIR/scripts/ralph_control.py" "$@"
- skill/ralph/ralph:10 — exec python3 "$BASEDIR/scripts/ralph_control.py" launch "$@"
- skill/ralph/SKILL.md:73 — # python3 /home/rgardler/.pi/agent/skills/ralph/scripts/ralph_loop.py <work-item-id> --json
- skill/ralph/SKILL.md:76 — # python3 /home/rgardler/.pi/agent/skills/ralph/scripts/ralph_loop.py <parent-id> --child <child-id> --json
- skill/ralph/SKILL.md:81 — # python3 /path/to/skills/ralph/scripts/ralph_loop.py <work-item-id> --json
- skill/ralph/tests/test_structured_response.py:39 — {"command": "edit", "args": ["skill/ralph/scripts/ralph_loop.py"]},
- skill/ralph/tests/test_structured_response.py:47 — assert parsed.text == "edit skill/ralph/scripts/ralph_loop.py"
- skill/ralph/tests/test_structured_response.py:48 — assert parsed.summary == "edit skill/ralph/scripts/ralph_loop.py"
- skill/ralph/tests/test_structured_response.py:49 — assert parsed.actions[0].render() == "edit skill/ralph/scripts/ralph_loop.py"

## resolve-pr-comments

- path: skill/resolve-pr-comments
- status: PASS

## ship

- path: skill/ship
- status: FAIL

### Matches

- skill/ship/SKILL.md:62 — - `scripts/ship.js` — Push-to-dev behaviour module (exports: `pushToDev`, `pushToBranch`, `validatePushTarget`, `validateForcePush`, `DEV_BRANCH`, `PROTECTED_BRANCHES`, and re-exports from `git-helpers.js`)
- skill/ship/SKILL.md:63 — - `scripts/git-helpers.js` — Branch naming and policy helpers (exports: `makeBranchName`, `validateBranchName`, `isBranchBlocked`, `BLOCKED_BRANCHS`, `BRANCH_NAME_PATTERN`)
- skill/ship/SKILL.md:64 — - `scripts/release/merge-dev-to-main.sh` — the canonical release merge script
- skill/ship/SKILL.md:70 — import { pushToDev } from './scripts/ship.js';
- skill/ship/SKILL.md:70 — import { pushToDev } from './scripts/ship.js';
- skill/ship/SKILL.md:78 — import { makeBranchName, validateBranchName, isBranchBlocked } from './scripts/git-helpers.js';
- skill/ship/SKILL.md:78 — import { makeBranchName, validateBranchName, isBranchBlocked } from './scripts/git-helpers.js';
- skill/ship/SKILL.md:94 — 1. __Create a feature branch__ using `makeBranchName(workItemId, shortDesc)` from `scripts/git-helpers.js`.
- skill/ship/SKILL.md:97 — 4. __Push to dev__ using `pushToDev()` from `scripts/ship.js`. This:
- skill/ship/SKILL.md:162 — bash scripts/release/merge-dev-to-main.sh
- skill/ship/SKILL.md:162 — bash scripts/release/merge-dev-to-main.sh
- skill/ship/SKILL.md:196 — | __Automated script__ | Run `bash scripts/release/merge-dev-to-main.sh` manually | Ship subagent unavailable but script is available |
- skill/ship/SKILL.md:196 — | __Automated script__ | Run `bash scripts/release/merge-dev-to-main.sh` manually | Ship subagent unavailable but script is available |
- skill/ship/SKILL.md:217 — executor. The agent should invoke `scripts/release/merge-dev-to-main.sh`

## triage

- path: skill/triage
- status: FAIL

### Matches

- skill/triage/SKILL.md:37 — - `scripts/check_or_create.py` — implementation using `wl` CLI.
