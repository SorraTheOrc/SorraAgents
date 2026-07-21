---
name: implement
description: |
  Write tests, docs and code for a Worklog work item by following a
  deterministic, script-assisted workflow. Ensure implementation meets
  defined acceptance criteria. Trigger on user queries such as:
  'Implement <work-item-id>', 'Complete <work-item-id>',
  'Work on <work-item-id>'.
---

## Purpose

Provide a deterministic, script-assisted implementation workflow for completing
a Worklog work item. The Python orchestration script (`scripts/implement.py`)
manages all deterministic lifecycle steps (claim, worktree creation, refactor,
build/test/commit cycle, cleanup, stage advancement), while this guide covers
code/test authoring, review, and best practices.

## Inputs

- work-item id: required. Validate id format `<prefix>-<hash>` and prompt if
  missing.
- Optional freeform guidance in the arguments string may be used to shape the
  implementation approach.

## Outputs

- Tests and implementation code meeting acceptance criteria (committed to a
  branch and pushed to `dev`).
- Work item updated to `in_review` stage (work item is NOT closed; it stays
  open until the release process promotes the changes to `main`).

## References to Bundled Resources

- Script runner: `./implement` (wrapper) or `python3 scripts/implement.py`
- Intake/interview helpers: `intake`, `plan`.
- Refactor skill: `../refactor/SKILL.md` (invoked automatically during finish phase)

Security note: Do not push or create PRs automatically unless the invoking
agent has explicit permission to push to the repository and open pull
requests. Require explicit confirmation before performing remote actions
(push/pr creation) when operating without an operator-approved credential.
When in doubt, produce the exact `git`/`gh`/`wl` commands for a human to run.

Privacy note: Avoid including secrets, tokens, or personally-identifiable data
in work item comments or PR bodies. If such data must be referenced, reference
it by work-item id or document path instead of pasting values. Mask or redact
any sensitive values before writing them to logs or comments.

## Best Practices

- Follow the steps in order and do not skip steps.
- **Write tests before implementation code** (test-driven development). Always create at least one test file before editing implementation code. Tests may fail on first run; write implementation code to make them pass. When external constraints prevent complete tests, create harnesses/mocks and document the limitation as a temporary placeholder.
- Do not use search tools (grep, ripgrep, code search). Rely on work-item context and linked docs. If insufficient context, run intake interview.
- Keep implementation focused on meeting acceptance criteria with minimal changes.
- Never edit code outside `src/`, `tests/`, `docs/` unless essential configuration files.
- Never edit bundled libraries (`dist/`, `node_modules/`).
- When implementing a CLI or API, always provide JSON formatted output.
- Use work item comments to document process, decisions, and next steps.
- Handle errors gracefully with actionable remediation messages.
- If the work item is not well-defined, run intake interview before proceeding.
- If blockers or dependencies exist, implement those first.
- Follow AGENTS.md policies for branch naming, commit discipline, worktree workflow, and push-to-dev integration. See [AGENTS.md](../../AGENTS.md#implement-the-work-item).
- After implementation is `in_review`, use the cleanup skill to tidy up local feature branches (do not clean up `dev` or `main`).

## Status Safety & Abort Handling

### Critical Rule: Always Reset Status on Abort

When an implementation is aborted, interrupted, or fails before reaching the
final commit/push step, the work item can remain stuck at `status: in_progress`,
blocking other agents from claiming or processing it. **Every abort/failure path
MUST reset status to `open`** to release the work item lock.

### Mandatory Abort Pattern

Use the script's `abort` subcommand to cleanly abort and reset status:

```bash
python3 scripts/implement.py abort <work-item-id>
```

The abort command:
1. Resets work-item status to `open`
2. Cleans up worktree processes (via `wl cleanup-worktree` or fallback)
3. Removes the worktree directory
4. Restores the repo to the `dev` branch
5. Adds an abort comment to the work item

> **Why this matters:** Work items in `in_progress` status are filtered by `wl next`
> and are invisible to other agents. An orphaned `in_progress` item blocks all
> downstream work on that item until a human intervenes. Always resetting to `open`
> on abort ensures the work item is visible and claimable by the next agent.

### Abort Scenarios

The implement script handles **five** abort/failure scenarios:

| # | Scenario | Script handling |
|---|----------|-----------------|
| 1 | Dirty work tree abort | Start phase detects and reports dirty tree; does not create worktree |
| 2 | Definition gate failure | Agent detects during understanding; runs `implement.py abort <id>` |
| 3 | User-initiated abort | Run `implement.py abort <id>` at any time |
| 4 | Error/exception during implementation | Script catches errors and resets status to open |
| 5 | SIGTERM/SIGINT | Signal handler runs deterministic cleanup (kill processes, remove worktree, reset status, restore repo) |

## Handling Assets

- **Graphics/audio:** Create in `assets/images/` or `assets/audio/` with a `placeholder_` prefix. Reference in work item comments and commit. Optimize for size/performance. Use only assets you have rights to distribute; provide attribution where required.
- **Documentation:** Update relevant markdown files in `docs/`. Ensure changes are clear and accurate.
- **Exception:** `CHANGELOG.md` is excluded — managed automatically by the ship skill's release pipeline.

## Workflow

Execute the following steps in order. Do not skip steps. The orchestration
script handles deterministic lifecycle operations; the agent handles code and
test authoring.

### Step 1 — Start the implementation environment

Run the script's `start` subcommand to claim the work item, check for a clean
working tree, fetch work-item details, and create an isolated worktree:

```bash
python3 scripts/implement.py start <work-item-id>
```

Or via the wrapper:

```bash
./implement start <work-item-id>
```

The script will:
1. Validate the work item ID format
2. Claim the item (`wl update --status in_progress`)
3. Check for uncommitted changes (safety gate)
4. Fetch and audit the work item
5. Create a git worktree from `dev` with an auto-named branch (`wl-<id>-<slug>`)
6. Register SIGTERM/SIGINT handlers for deterministic cleanup
7. Write persistent state into the worktree
8. Print the worktree path and next steps

Output (non-JSON mode):
```
============================================================
  Implement: <title> (<id>)
============================================================
  Worktree:  /path/to/worktree
  Branch:    wl-<id>-<slug>
  Parent:    dev

  Next steps:
  1. cd /path/to/worktree
  2. Write tests and implementation code
  3. Run: python3 scripts/implement.py finish <id>

  To abort:
  python3 scripts/implement.py abort <id>
```

#### Safety gate: dirty working tree

If the start phase detects uncommitted changes outside `.worklog/`, it will
report them and refuse to proceed. Resolve the changes (stash, commit, or
revert) and re-run start.

#### Definition gate

Before implementing, verify:
- Clear scope (in/out-of-scope).
- Concrete, testable ACs.
- Constraints and compatibility expectations.
- Unknowns captured as explicit questions.

If the gate fails:
1. Run `python3 scripts/implement.py abort <work-item-id>` to release the item.
2. If not well-defined → run intake interview (see `command/intake.md`).
3. If too large → run plan interview (`/skill:plan`) to decompose.
4. Inform the user and ask if they want to restart implementation review.

### Step 2 — Switch to the worktree

```bash
cd <worktree-path>
```

All implementation work happens inside this isolated worktree.

### Step 3 — Implement

- **Write tests first** (TDD): create at least one test file before editing
  implementation code. Tests may fail initially; implement code to make them
  pass. If external constraints prevent complete tests, use harnesses/mocks
  and document the limitation.
- Make minimal, focused changes to meet the acceptance criteria.
- Follow project style and conventions.
- Comment on significant design decisions in the work item.
- If additional work is discovered, create linked work items:
  ```bash
  wl create "<title>" --deps discovered-from:<work-item-id> --json
  ```
- Once all ACs are met, proceed to Step 4.

> **Parent-advancement check (epic/parent items only):**
> After implementing, check whether this item has children:
> ```bash
> wl show <work-item-id> --children --json
> ```
> If all children are in a terminal stage (`in_review`/`completed`/`done`):
> ```bash
> wl update <work-item-id> --status completed --stage in_review --json
> ```
> If any children are NOT in a terminal stage:
> ```bash
> wl update <work-item-id> --status open --json
> wl comment add <work-item-id> --comment "Not all children are in a terminal stage. Needs producer review." --author "<AGENT>" --json
> ```
> **Under Ralph:** parent advancement is handled automatically. Skip manual advancement.

#### Audit self-check

Before running the finish phase, check for a recent audit record. If a recent
audit exists, reuse it to establish what work remains. If no recent audit
exists, run `/skill:audit <work-item-id>` for a full audit to verify ACs and
scope. The audit result feeds into the implementation workflow: once ACs are
clear, proceed to the finish phase.

```bash
/skill:audit <work-item-id>
```

If ACs are unmet after the audit, continue implementing (return to Step 3).
Otherwise proceed.

### Step 4 — Run the finish phase

Once implementation is complete, run the `finish` subcommand to automate the
remaining lifecycle: refactor, build, test, commit, cleanup, push, and stage
advancement.

```bash
python3 scripts/implement.py finish <work-item-id>
```

Or:

```bash
./implement finish <work-item-id>
```

The script will:
1. **Refactor step**: Invoke `python3 ../refactor/scripts/refactor.py <id>` to
   detect and remediate code smells. Session-introduced smells are fixed
   automatically; pre-existing smells create Worklog items. Skip with
   `--no-refactor`.
2. **Build**: Run `npm run build` (or equivalent). Fails fast if build errors.
3. **Test**: Run the full test suite (pytest or npm test).
4. **Test-fix loop**: If tests fail, the script reports failures and prompts
   for fixes (interactive) or returns a structured failure report (JSON mode).
   Up to `--max-retry N` attempts (default: 3).
5. **Commit**: Stage all changes and commit with a descriptive message.
6. **Process cleanup**: Call `wl cleanup-worktree <path>` to terminate tracked
   processes. Falls back to pgrep-based scanning if unavailable.
7. **Remove worktree**: Run `git worktree remove --force` and `git worktree prune`.
8. **Restore repo**: Checkout `dev` branch, pull latest.
9. **Push to dev**: Push the feature branch into `dev` on origin.
10. **Mark in_review**: Set work item status to `completed` and stage to `in_review`.

Output (non-JSON mode):
```
============================================================
  ✅ Implementation complete for <id>
============================================================
  Commit: <hash>
  Branch: wl-<id>-<slug>
  Status: in_review
```

#### Test-fix loop (interactive mode)

When tests fail in interactive mode, the script will:
```
  ⚠  Test run 1/3 failed
  ============================================================
  Failures:
    • tests/test_foo.py::test_bar - AssertionError

  Fix the failures and press Enter to re-run tests.
  Type 'abort' to abort the finish phase, or 'skip' to skip tests.
```

Your options:
- **Fix the code**, then press Enter to re-run tests.
- Type `skip` to skip further testing and proceed to commit.
- Type `abort` to abort the finish phase (status reset to open).

#### Test-fix loop (JSON mode)

In JSON mode (`--json`), test failures return a structured report and exit
non-zero. The agent fixes the code and re-invokes `finish`:

```bash
# Run finish
output=$(python3 scripts/implement.py finish <id> --json 2>&1)
exit_code=$?
# Parse output for test failures
if echo "$output" | python3 -c "import sys,json; d=json.load(sys.stdin); sys.exit(0 if d.get('steps',{}).get('tests',[{}])[-1].get('success') else 1)" 2>/dev/null; then
  : # tests passed
else
  # Fix code and re-run
  python3 scripts/implement.py finish <id> --json
fi
```

### Step 5 — Self-review (agent responsibility)

After the finish phase completes, perform a self-review:

1. **Build and lint**: Verify no build or lint errors remain.
2. **Run tests**: Confirm all tests pass.
3. **Audit the work item**: `/skill:audit <work-item-id>`. If ACs are unmet,
   fix and repeat.
4. **Self-review passes**:
   - Completeness: All ACs met?
   - Dependencies & safety: Any regressions?
   - Scope & regression: Only intended files changed?
   - Tests & acceptance: Tests cover ACs?
   - Polish & handoff: Ready for review?

For each pass, make small, goal-aligned edits. If intent changes are
discovered, create an Open Question and stop.

## Status Transition Matrix

The following table documents the expected status and stage transitions at each workflow phase for the `implement` skill.

| Phase | Action | Status | Stage |
|-------|--------|--------|-------|
| Start (Step 1) | `implement.py start <id>` | in_progress | in_progress |
| Finish (Step 4) | `implement.py finish <id>` | completed | in_review |
| Abort | `implement.py abort <id>` | open | (unchanged) |
| SIGTERM/SIGINT | Signal handler | open | (unchanged) |

> All abort/failure paths use `--status open` while keeping the stage unchanged.

## Scripts

### Orchestrator (canonical runner)

```bash
python3 scripts/implement.py start <id>        # Setup phase
python3 scripts/implement.py finish <id>        # Completion phase
python3 scripts/implement.py abort <id>         # Abort and cleanup
```

### Wrapper

```bash
./implement start <id>
./implement finish <id>
./implement abort <id>
```

### Options

| Flag | Description |
|------|-------------|
| `--json` | Output results in JSON format |
| `--no-refactor` | Skip the refactor step |
| `--max-retry N` | Max test-fix loop retries (default: 3) |
| `--commit-msg <msg>` | Override commit message |
| `--parent-branch <branch>` | Parent branch for worktree (default: dev) |
| `--worktree-path <path>` | Override worktree path |
| `-v` / `--verbose` | Enable verbose logging |

### Examples

```bash
# Start implementation
python3 scripts/implement.py start SA-0MPYMFZXO0004ZU4

# Start with JSON output
python3 scripts/implement.py start SA-0MPYMFZXO0004ZU4 --json

# Finish (interactive)
python3 scripts/implement.py finish SA-0MPYMFZXO0004ZU4

# Finish with custom commit message and no refactor
python3 scripts/implement.py finish SA-0MPYMFZXO0004ZU4 \
  --commit-msg "SA-0MPYMFZXO0004ZU4: Add input validation" \
  --no-refactor

# Finish with max 5 test-fix retries
python3 scripts/implement.py finish SA-0MPYMFZXO0004ZU4 --max-retry 5

# Abort
python3 scripts/implement.py abort SA-0MPYMFZXO0004ZU4
```

## Lifecycle Summary

```
┌─────────────────────────────────────────────────────────────┐
│ 1. implement.py start <id>                                  │
│    - Claim work item (in_progress)                          │
│    - Safety gate (dirty check)                              │
│    - Create worktree from dev                               │
│    - Register signal handlers                               │
│    - Print worktree path                                    │
└───────────────────────┬─────────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────────────┐
│ 2. Agent writes tests and code inside worktree              │
│    - TDD: tests first                                       │
│    - Meet acceptance criteria                               │
└───────────────────────┬─────────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────────────┐
│ 3. implement.py finish <id>                                 │
│    - Refactor (auto-fix smells)                             │
│    - Build                                                  │
│    - Test (fix-and-re-run loop, max N retries)              │
│    - Commit                                                 │
│    - Clean up processes (wl cleanup-worktree)               │
│    - Remove worktree                                        │
│    - Push to dev                                            │
│    - Mark in_review                                         │
└─────────────────────────────────────────────────────────────┘
```

After committing and pushing changes, close your response to the operator with:

```
<work-item-id>: <concise-summary>

Work committed to dev
```

End.
