---
name: implement
description: "Write tests, docs, and code for a Worklog item via a deterministic workflow. Use when: 'Implement <id>'."
---

## Purpose

Deterministic, step-by-step workflow for completing a Worklog work item
through code, tests, and docs.

## Inputs

- work-item id: required; validate `<prefix>-<hash>`, prompt if missing.
- Optional freeform guidance in the arguments may shape the approach.

## Outputs

- Tests and implementation code meeting ACs, committed and pushed to `dev`.
- Work item updated to `in_review` (NOT closed; stays open until release).

## References to Bundled Resources

- Intake/interview helpers: `intake`, `plan`.

Security note: Do not push or create PRs automatically unless the invoking
agent has explicit permission to push and open pull requests; require explicit
confirmation before remote actions (push/PR) without an operator-approved
credential. When in doubt, produce the exact `git`/`gh`/`wl` commands for a
human to run.

Privacy note: Avoid secrets/tokens/PII in comments or PR bodies — reference by
work-item id or document path; mask/redact sensitive values before writing to
logs or comments.

## StatusLifecycle Integration

Status transitions are managed by the shared `StatusLifecycle` context manager
from `../shared/status_lifecycle.py`. The orchestration script (`implement.py`)
uses it automatically: `phase_start()` claims (`in_progress`); `phase_finish()`
wraps build/test/commit/push in `with StatusLifecycle(..., target_stage="in_review"):`
(success → `completed`/`in_review`; error → original status restored);
`phase_abort()` resets to `open` (also `implement.py abort <WIP-id>`). Manual
use: `StatusLifecycle.update_status()` or the context-manager pattern in
`../shared/status_lifecycle.py`.

## Test Anti-Patterns

Review the shared [Test Writing Guidelines](../shared/test-writing-guidelines.md)
before writing tests (six anti-patterns from a full audit of the
Tableau-Card-Engine suite; 32 low-value files removed). Never write tests
that: (1) grep source instead of asserting behaviour, (2) contain
`expect(true).toBe(true)` or zero assertions, (3) re-implement production
logic, (4) duplicate an existing core test, (5) assert type-level
satisfaction the compiler checks, (6) boot a browser/scene without asserting
anything. Every test must assert observable behaviour via the public API.

## Best Practices

- Follow the steps in order; do not skip steps.
- **Testing is required — TDD preferred, not mandatory.** Write tests first
  whenever practical; test-after is permitted when TDD would complicate
  implementation. When external constraints prevent complete tests, create
  harnesses/mocks and document the limitation. **Do NOT write placeholder
  tests** — track unimplemented features in a work item instead
  ([Test Writing Guidelines](../shared/test-writing-guidelines.md)).
- No search tools (grep/ripgrep/code search) — rely on work-item context and
  linked docs; if insufficient, run intake interview.
- Keep implementation focused on meeting ACs with minimal changes; never edit
  code outside `src/`/`tests/`/`docs/` unless essential config; never edit
  bundled libraries (`dist/`, `node_modules/`).
- CLI/API work always provides JSON formatted output.
- Document process/decisions/next steps in work item comments; handle errors
  gracefully with actionable remediation.
- Not well-defined → intake interview; implement blockers/dependencies first.
- Follow AGENTS.md policies for branch naming, commit discipline, worktree workflow, and push-to-dev ([AGENTS_GLOBAL](../../AGENTS_GLOBAL.md#implement-the-work-item)); after `in_review`, use the cleanup skill to tidy local feature branches (not `dev`/`main`).
- Use `StatusLifecycle` for all status transitions — never ad-hoc `wl update --status` commands.

## Status Safety & Abort Handling

### Critical Rule: Always Reset Status on Abort

When an implementation is aborted, interrupted, or fails before the final
commit/push step, the item can remain stuck at `in_progress`, blocking other
agents. **Every abort/failure path MUST reset status to `open`** to release
the lock.

### Mandatory Abort Pattern

1. **Reset to open:** `StatusLifecycle.update_status(<work-item-id>, "open")`
2. **Stop execution:** Return control to the operator with a clear explanation

> `in_progress` items are filtered by `wl next`; an orphaned one blocks work.

### Abort Scenarios

The implement skill covers **five** abort/failure scenarios: (1) dirty work
tree abort, (2) definition gate failure (unclear scope, untestable ACs), (3)
user-initiated abort, (4) error/exception during implementation, (5)
unexpected termination (covered by the Final cleanup step).

> **Status reset is conditional, not blind:** `_safety_reset_if_in_progress()`
> resets to `open` **only if** currently `in-progress`; SIGINT/SIGTERM during
> any phase releases the item via `os._exit`.

### Error/exception handling (abort on unexpected errors)

On unexpected error (API/network failure, exception): (1) reset to open
(`StatusLifecycle.update_status(<work-item-id>, "open")`); (2) log it
(`wl comment add <work-item-id> --comment "Error: <description>" --author "<AGENT>" --json`);
(3) return control with details; (4) operator may retry if transient.

### User-initiated abort

If the operator cancels after Step 2: reset to `open`
(`StatusLifecycle.update_status(<work-item-id>, "open")`), return control, and
document: `wl comment add <work-item-id> --comment "Aborted by operator" --author "<AGENT>" --json`.

## Handling Assets

- **Graphics/audio:** create in `assets/images/` or `assets/audio/` with a `placeholder_` prefix; reference in comments and commit; optimize size/performance; only use assets you have rights to distribute (attribute where required).
- **Documentation:** update relevant markdown in `docs/`; keep changes clear and accurate.
- **Exception:** `CHANGELOG.md` excluded — managed by the ship skill's release pipeline.

## Steps

Execute the following steps in order. Do not skip steps. Use the live commands where applicable and record outputs in the work-item comments as you proceed.

1. Set status and safety gate

- **Before any other step**, claim the work item:
  `StatusLifecycle.update_status(<work-item-id>, "in_progress", stage="in_progress", assignee="<AGENT>")` (or `implement.py start`)

> **Code Freeze gate:** `implement.py start <id>` checks the Code Freeze marker
> (`.worklog/code-freeze.json`, contract WL-0MSBU4KMA004PKSR) **before**
> claiming; if a release is in progress it refuses ("Project is in Code Freeze —
> implementation blocked until the release completes"), exits non-zero, and
> does **not** change the item status. No `--force` bypass; fail-open: a
> missing/corrupt marker never blocks.

2. Safety gate: handle dirty working tree

- Run `git rev-parse --is-inside-work-tree` (worktree?) and
  `git status --porcelain=v1 -b` (uncommitted changes?).

**CRITICAL: Never stash, commit, or revert the user's uncommitted changes
without explicit permission** — they may be user-authored work; stashing them
without asking can strand that work and is forbidden. When uncommitted changes
exist, STOP and ask the operator how to proceed (commit, stash, revert, or
abort); act only after the operator explicitly chooses.

- **Inside a worktree:** `.worklog/`-only → carry forward; otherwise stop and
  ask the operator; never stash/commit/revert unilaterally.
- **Main checkout:** `.worklog/`-only → carry forward; otherwise report the
  dirty files (may be stale) and create a worktree for isolation without
  touching the user's changes; if dirty files prevent worktree creation, stop
  and ask the operator (act only on their explicit choice).

On abort: `StatusLifecycle.update_status(<work-item-id>, "open")`

3. Understand the work item

The item is already claimed from Step 1. Check the most recent worklog action,
comment, or audit entry (reuse a recent audit, else `/skill:audit
<work-item-id>`). Fetch `wl show <work-item-id> --json`; pay attention to
`description`, `acceptance criteria`, `comments`. Restate ACs/status; surface
blockers/dependencies/missing requirements; inspect linked PRDs/plans/docs;
confirm expected tests/validation.

3.1. Definition gate (must pass before implementation)

Verify: clear scope (in/out-of-scope); concrete, testable ACs; constraints and
compatibility expectations; unknowns captured as explicit questions.

If the gate fails: (1) `StatusLifecycle.update_status(<work-item-id>, "open")`;
(2) not well-defined → intake interview (`../intake/SKILL.md`); too large →
plan interview (`/skill:plan`); (3) inform the user and ask whether to restart.

4. Create a worktree from dev and branch inside it

> **MANDATORY — worktree requirement:** All implementation work MUST be done
> in a git worktree created from `dev` — never edit, commit, or push from the
> main checkout. `implement.py finish` refuses if it detects changes outside
> the worktree; `implement.py start` creates it for you — `cd` into it and do
> all work there.

```bash
git worktree add --track -b wl-<WIP-id>-<short-slug> .worklog/worktrees/wl-<WIP-id>-<short-slug> dev
cd .worklog/worktrees/wl-<WIP-id>-<short-slug>
```

> **`node_modules` is auto-symlinked:** `implement.py start` creates
> `<worktree>/node_modules -> <repo-root>/node_modules` when the main checkout
> has one (SA-0MSGS763C006SM1B). **Do NOT run `npm install` inside a worktree** — writes pass through the symlink, corrupting the shared tree.

See [AGENTS_GLOBAL](../../AGENTS_GLOBAL.md#implement-the-work-item).

5. Implement

- Open/in_progress blockers or dependencies → implement them first (recursively via this procedure).

5.1. Parent recursion (epic/parent items only)

A parent invocation recurses into its children automatically. Run:

```bash
python3 scripts/implement.py parent <parent-id>
```

(`implement.py parent` — orchestrated by `phase_parent()`):

- **No children** → behaves like a leaf: use the standard `start`/`finish`
  workflow unchanged.
- **All children terminal** (`in_review`/`completed`/`done`) → the parent is
  advanced to `completed`/`in_review` (existing Step 5.1 advancement
  retained) and a per-child summary (ids, statuses) is commented.
- **Children remain** → the next child is claimed (`in_progress`), its own
  worktree is created from `dev`, and the worktree path is reported.

Then implement that child by recursing into this procedure (steps 1–8),
run `implement.py finish <child-id>`, and re-run
`implement.py parent <parent-id>` for the next child. Repeat until the
parent reports all children terminal. Each child is implemented in its own
worktree (never the main checkout); sequential children reuse/rotate the
`.worklog/worktrees` machinery.

Guards (deterministic, in `phase_parent`):

- **Dependency order** — a child `blocked` by another item is implemented
  only after its blockers; the chain is resolved dependency-order correct.
- **Terminal children are never re-implemented** (skipped, reported).
- **In-progress by another agent** → skipped and reported, never clobbered.
- **Cycles fail fast** with a clear error (no infinite recursion).
- **Abort/failure** in a child resets THAT child to `open` (StatusLifecycle
  abort semantics) and stops the chain with a report of what completed and
  what failed; already-completed siblings are not regressed.
- **No orphaned `in_progress`** — a child start failure or abort leaves no
  in-progress state behind; re-run the parent phase after resolving the
  blocker to continue the chain.
- A parent with no children or all-terminal children behaves as today.

Worktree isolation per child is preserved: every child is implemented in
its own worktree created by `phase_start` (never the main checkout);
sequential children reuse/rotate the `.worklog/worktrees` machinery. The
parent itself gets no worktree.

- Write tests and code to meet ACs:
  - **Write tests first** (TDD preferred) — at least one test file before
    editing implementation code; minimal, focused changes.
  - External constraints prevent complete tests → harnesses/mocks/placeholders,
    documented; follow project style; comment on significant decisions.
  - Discovered additional work → `wl create "<title>" --deps discovered-from:<work-item-id> --json`
- Once all ACs are met: **build** (no errors); **run the full test suite via the [test skill](../test/SKILL.md) (`/skill:test`)** — run → triage → evaluate → loop until green; report; fix failures. **This MUST be `/skill:test` (`./scripts/run_tests.py`), never an ad-hoc equivalent** (`npx vitest run`, `pytest`, …): only the test-skill runner records the run in the per-repo test cache (keyed by git state, 2h TTL) that the audit skill reads read-only via `query_cached()` to auto-verify execution-dependent ACs — an ad-hoc run at the same commit is invisible to the audit, so the audit either auto-executes the suite itself on a cache miss (F3, SA-0MSTN5KRF0097TVP) or the operator attests with `--green-run HEAD` (it never hard-blocks — F4, SA-0MSTN8CWM003AAU9). Failures outside scope → triage helper (`python3 ../triage/scripts/check_or_create.py '{"test_name":"<name>", "stdout_excerpt":"...", "stack_trace":"...", "parent_work_item_id":"<this-work-item-id>"}'`); implement returned critical issues, re-run until green. Update docs (except `CHANGELOG.md`); summarize changes.

6. Automated self-review

- Build and lint; fix any issues.
- Audit: `/skill:audit <work-item-id>` — if ACs unmet, inform the user and return to step 5. The item is `in_progress` during implementation, so pass `--force` (the audit's pre-flight affirmation guard refuses to audit an in-progress item without it — see [../audit/SKILL.md](../audit/SKILL.md)).
- Sequential passes: completeness, dependencies & safety, scope & regression, tests & acceptance, polish & handoff. Small, goal-aligned edits; intent changes → Open Question and stop.

7. Optional refactor step

Before final commit, an automated refactor step may detect/remediate code smells (files modified this session; linters for mechanical issues + LLM for design smells). **Session-introduced smells fixed immediately; pre-existing smells create Worklog items with REFACTOR comments.** Skip with ``--no-refactor``:

```bash
python3 ../refactor/scripts/refactor.py <work-item-id>
```

See ``../refactor/SKILL.md``.

8. Commit, Push to dev and mark in_review

- Follow the mandatory build → test → commit order before committing.
- **Do NOT create a Pull Request to `main`** — work is integrated into `dev`; the `dev`→`main` promotion is handled by the release process.
- Push the feature branch into `dev` via the ship skill (`pushToDev()` from `../ship/scripts/ship.js`, preferred) or `git push origin HEAD:refs/heads/dev`. `dev` is **not** protected; only `main`, `master`, `HEAD` are blocked.
- After pushing, clean up the worktree:

  ```bash
  git worktree remove .worklog/worktrees/wl-<WIP-id>-<short-slug>
  git worktree prune
  git checkout dev && git pull origin dev
  npm run build 2>/dev/null || echo "No build script, skipping rebuild"
  ```

  > **Why rebuild?** `dist/` is gitignored; `git pull` does not update it. See [[concepts/git-worktree-best-practices-for-agent-workflows]].
- Add a work-item comment with the commit hash: `wl comment add <work-item-id> --comment "Completed work pushed to dev, see commit <hash>." --author "<AGENT>" --json`
- Close your response with: `<work-item-id>: <concise-summary>\n\nWork committed to dev`

  > **Parent/epic items already advanced at Step 5.1:** skip the status update.
  > **Manual (leaf items, or parents not yet advanced):** mark `in_review` (do **NOT** close): `StatusLifecycle.update_status(<work-item-id>, "completed", stage="in_review")`

  > The item stays `in_review` until release promotes `dev` to `main` (see `../ship/SKILL.md`).

Pre-push blocking check
-----------------------

Run the full test suite via the [test skill](../test/SKILL.md) (`/skill:test`) and fix failures before pushing; outside scope → triage helper.

Final cleanup (belt-and-suspenders)
---------------------------------------

Before exiting at any point, `wl show <work-item-id> --json`; if `status: in_progress` and work is incomplete (not at Step 8), reset via `StatusLifecycle.update_status(work_item_id, "open")` to prevent orphaned `in_progress` items blocking other agents.

## Status Transition Matrix

| Phase | Mechanism | Status | Stage |
|-------|-----------|--------|-------|
| Claim (Step 1) | `update_status(id, "in_progress", stage="in_progress", assignee="<AGENT>")` / `phase_start()` | in_progress | in_progress |
| Epic/parent all children done (5.1) | `update_status(id, "completed", stage="in_review")` | completed | in_review |
| Final (Step 8) | `with StatusLifecycle(id, target_stage="in_review"):` / `phase_finish()` | completed | in_review |
| Abort (dirty/gate/user/error/termination) | `update_status(id, "open")` via `phase_abort()` (error: context manager restores original; termination: final cleanup resets) | open | unchanged |

> **All abort/failure transitions reset to `open`.** Never leave a work item in `in_progress` unless actively implementing.

## Scripts (canonical runner & modules)

This skill does not ship a single orchestrator script — implementation follows
the steps above and invokes project-local build/test and linters. When a
repository provides an "implement" helper script, prefer it for deterministic
behavior.

Build/test steps for repos without build/test tooling:
[docs/dev/implement-skill-reference.md](../../docs/dev/implement-skill-reference.md).

Example commands (documentation example, SA-0MPYMFZXO0004ZU4):

```bash
wl show SA-0MPYMFZXO0004ZU4 --json
git push origin HEAD:refs/heads/dev   # ship.js pushToDev preferred
python3 -c "from skill.shared.status_lifecycle import StatusLifecycle; StatusLifecycle.update_status('SA-0MPYMFZXO0004ZU4', 'completed', stage='in_review')"
```

End.


## Final step: standardized end-of-session report

Render the canonical end-of-session report (helper: [`../report/SKILL.md`](../report/SKILL.md)) as the **last step**, replacing any ad-hoc end-of-session summary:

```bash
python3 ../report/scripts/render_report.py <work-item-id> \
  --skill-name <skill_name> \
  --headline "<1-3 sentence headline summary>" \
  --ac "<AC# description>|<verification metric>|met|unmet" \
  --ac "<...>|<...>|met|unmet" \
  [--producer-actions "<actions for the producer, or omit for 'None needed'>"] \
  [--notes "<freeform context/caveats/assumptions>"] \
  [--next-action <review|plan|implement|...>]
```

Then close with: `<work-item-id>: <one-line summary>`. Do NOT re-summarize the report in a different format — the report is the summary.
