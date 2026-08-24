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

Security note — scope: this restriction applies to **protected branches**
(`main`/`master`/`HEAD`) and to **creating PRs**: do not push to them or open
PRs automatically without explicit operator permission (no operator-approved
credential exists for those actions). Pushing the feature branch into `dev`
(Step 9) is **pre-authorized by this workflow** — the repo's pre-push hook
enforces the same policy, blocking `main`/`master`/`HEAD` only — and requires
no additional approval, provided the build passes and the test gate is green:
`implement.py finish` validates the worktree with **changed scope** (tests
affected by the change — fast iteration), then runs a **final `--scope full`
gate** before commit, and the **pre-push hook re-runs the full suite**
(`--scope full`) on the actual push to `dev`/`main` (SA-0MT6BYQHB008DOGC).
When in doubt, produce the exact
`git`/`gh`/`wl` commands for a human to run.

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

3. Stash hygiene gate (warn on orphaned stashes)

After the dirty-tree check passes, `implement.py start` inspects
`git stash list` on the main checkout. Stashes that reference an open work
item (e.g. `stash@{0}: On dev: WIP: partial SA-0XXXXXXX`) are matched and
not flagged. Stashes with no work-item reference, or where the referenced
work item is not in an open state, are reported as **orphaned**.

- Orphaned stashes trigger a **WARNING** (not a hard error) — the gate is
  fail-open. Run with `--allow-orphaned-stashes` to acknowledge and proceed.
- Each orphaned stash should be triaged: restore-and-commit via a proper work
  item if valuable, or delete if stale. See the recovery playbook below.
- The hygiene check is also available as a periodic script:
  `scripts/hygiene_check.sh` (run via cron, CI, or manually).

**Example warning:**

```
⚠  Orphaned stash(es) detected
============================================================
WARNING: 1 orphaned stash(es) detected on the main checkout.

Orphaned stashes:
  - stash@{1}: On dev: WIP: forgotten experiment

Triage these stashes (restore-and-commit via a proper work item, or delete if stale).
Proceed anyway with --allow-orphaned-stashes.
============================================================
```

4. Understand the work item

The item is already claimed from Step 1. Check the most recent worklog action,
comment, or audit entry (reuse a recent audit, else `/skill:audit
<work-item-id>`). Fetch `wl show <work-item-id> --json`; pay attention to
`description`, `acceptance criteria`, `comments`. Restate ACs/status; surface
blockers/dependencies/missing requirements; inspect linked PRDs/plans/docs;
confirm expected tests/validation.

4.1. Definition gate (must pass before implementation)

Verify: clear scope (in/out-of-scope); concrete, testable ACs; constraints and
compatibility expectations; unknowns captured as explicit questions.

If the gate fails: (1) `StatusLifecycle.update_status(<work-item-id>, "open")`;
(2) not well-defined → intake interview (`../intake/SKILL.md`); too large →
plan interview (`/skill:plan`); (3) inform the user and ask whether to restart.

4.2. Detect "already implemented" and close gaps (if applicable)

Before creating a worktree, check whether the work item has **already been
implemented** but is stuck in a wrong state/stage. Detection signals include:

- The implement skill has previously reported the work as completed with a
  commit hash (the skill's own output — not inferred from status alone).
- The item's status/stage is inconsistent (e.g. `in_progress` with a commit
  already on `dev`, or `completed` without `in_review`).
- A recent audit report indicates prior completion but unmet ACs or gaps.

If **any** detection signal applies:

1. Run the audit to get a current picture:
   `/skill:audit <work-item-id>` (reuse a recent audit if one exists from
   the same session; the most recent audit report may predate later fixes).
2. Review the audit report (`audit_report_<id>.md`) for gaps: unmet ACs,
   failing tests, missed requirements.
3. **If gaps exist:** fix them inline as part of the current item's
   implementation — write tests, code, or docs as needed. Do **NOT** create
   new work items for gaps (they are closed inline per the intake decision).
   Large gaps that exceed the scope of minimal remediation → record as a
   `discovered-from:<work-item-id>` work item instead.
4. **If the audit is clean** (no gaps): report the summary and skip further
   implementation — the item is done, just needs a status/stage update.
   Proceed directly to Step 9 (Commit, Push to dev and mark in_review).

If **no** detection signal applies (the item genuinely needs implementation):
proceed to Step 5.

5. Create a worktree from dev and branch inside it

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

> **Git submodules are auto-initialised:** `implement.py start` runs
> ``git submodule update --init --recursive`` inside the new worktree (SA-0MSN52GGN002B0AZ).
> Failures produce a ``WARNING`` log message but **do not abort** — the worktree
> remains usable (best-effort). Repos without ``.gitmodules`` are unaffected.

See [AGENTS_GLOBAL](../../AGENTS_GLOBAL.md#implement-the-work-item).

6. Implement

- Open/in_progress blockers or dependencies → implement them first (recursively via this procedure).

6.1. Parent recursion (epic/parent items only)

A parent invocation recurses into its children automatically. Run:

```bash
python3 $(skill_path implement)/scripts/implement.py parent <parent-id>
```

(`implement.py parent` — orchestrated by `phase_parent()`):

- **No children** → behaves like a leaf: use the standard `start`/`finish`
  workflow unchanged.
- **All children terminal** (`in_review`/`completed`/`done`) → the parent is
  advanced to `completed`/`in_review` (existing Step 6.1 advancement
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
- Once all ACs are met: **build** (no errors); **validate via the [test skill](../test/SKILL.md) — the loop is scope-aware (SA-0MT6BYQHB008DOGC)**: `implement.py finish` first runs a **changed-scope** validation (only the tests affected by this change — fast iteration), then a **final full-suite gate** (`--scope full`) before commit, and the **pre-push hook enforces the full suite again on the push to `dev`**. `run_tests.py` execution must go through the test-skill runner (`/skill:test`, `run_tests.py --scope full`): only the test-skill runner records runs in the per-repo test cache (keyed by git state + scope, 2h TTL) that the audit skill reads read-only via `query_cached()` to auto-verify execution-dependent ACs — an ad-hoc run (`npx vitest run`, `pytest`, …) is invisible to the audit, so the audit either auto-executes the suite itself on a cache miss (F3, SA-0MSTN5KRF0097TVP) or the operator attests with `--green-run HEAD` (it never hard-blocks — F4, SA-0MSTN8CWM003AAU9). Failures outside scope → triage helper (`python3 $(skill_path triage)/scripts/check_or_create.py '{"test_name":"<name>", "stdout_excerpt":"...", "stack_trace":"...", "parent_work_item_id":"<this-work-item-id>"}'`); implement returned critical issues, re-run until green. Update docs (except `CHANGELOG.md`); summarize changes.

7. Automated self-review

- Build and lint; fix any issues.
- Audit: `/skill:audit <work-item-id>` — if ACs unmet, inform the user and return to step 6. The item is `in_progress` during implementation, so pass `--force` (the audit's pre-flight affirmation guard refuses to audit an in-progress item without it — see [../audit/SKILL.md](../audit/SKILL.md)).
- Sequential passes: completeness, dependencies & safety, scope & regression, tests & acceptance, polish & handoff. Small, goal-aligned edits; intent changes → Open Question and stop.

8. Optional refactor step

Before final commit, an automated refactor step may detect/remediate code smells (files modified this session; linters for mechanical issues + LLM for design smells). **Session-introduced smells fixed immediately; pre-existing smells create Worklog items with REFACTOR comments.** Skip with ``--no-refactor``:

```bash
python3 $(skill_path refactor)/scripts/refactor.py <work-item-id>
```

See ``../refactor/SKILL.md``.

9. Commit, Push to dev and mark in_review

- Follow the mandatory build → test → commit order before committing.
- **Do NOT create a Pull Request to `main`** — work is integrated into `dev`; the `dev`→`main` promotion is handled by the release process.
- Push the feature branch into `dev` via the ship skill (`pushToDev()` from `$(skill_path ship)/scripts/ship.js`, preferred) or `git push origin HEAD:refs/heads/dev`. `dev` is **not** protected; only `main`, `master`, `HEAD` are blocked.

  > **Pushing from a worktree:** the repo's pre-push hook runs `wl sync`, which
  > refuses to run from a worktree (worktrees have no local `.worklog`; the
  > data lives in the main checkout). First run `wl sync` from the main
  > checkout, then push from the worktree with the hook's documented bypass:
  > `WORKLOG_SKIP_PRE_PUSH=1 git push origin HEAD:refs/heads/dev`. Nothing is
  > lost by skipping the sync at push time — the main checkout syncs the data
  > on its own pushes.
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

  > **Parent/epic items already advanced at Step 6.1:** skip the status update.
  > **Manual (leaf items, or parents not yet advanced):** mark `in_review` (do **NOT** close): `StatusLifecycle.update_status(<work-item-id>, "completed", stage="in_review")`

  > The item stays `in_review` until release promotes `dev` to `main` (see `../ship/SKILL.md`).

- **Final validation — belt-and-suspenders (post-push, post-`in_review`):** run one last
  audit at the committed state and reconcile its verdict BEFORE closing your
  response. This catches gaps the Step 7 self-review could not see (it ran
  pre-commit, with `HEAD` still at the base `dev` commit):

  ```bash
  python3 $(skill_path audit)/scripts/audit_runner.py issue <work-item-id> --green-run <full-commit-sha> --no-execute
  ```

  - `<full-commit-sha>` is the FULL 40-hex sha just pushed (from `git rev-parse
    HEAD`; short shas are rejected — the runner compares the value exactly
    against the audited HEAD). It attests the pre-push `/skill:test` green
    run against the **exact committed state**. The alias `--green-run HEAD`
    is accepted only when the checkout is at the pushed commit (immediately
    after `git pull origin dev`, with no intervening pushes; a mismatched
    attestation is reported and the run proceeds without it — never silently
    accepted).
  - `--no-execute` guarantees the audit never re-runs the test suite: it already
    passed pre-push, and execution-dependent ACs verify from the green-run
    attestation (see [../audit/SKILL.md](../audit/SKILL.md)).
  - Do **NOT** pass `--force`: the pre-flight guard passes at
    `completed`/`in_review`, and `--force` would bypass the freshness gate and
    child-verdict reuse.
  - `Ready to close: Yes` (or a freshness skip) → the item is genuinely ready;
    close your response below.
  - `Ready to close: No` → the final validation caught a real gap: inform the
    user, re-claim
    (`StatusLifecycle.update_status(<work-item-id>, "in_progress", stage="in_progress", assignee="<AGENT>")`),
    and return to Step 6 — do NOT leave the item in_review with unmet ACs.
  - **Parent/epic runs:** parent/epic items are validated per child at each
    child's Step 9; the parent itself is covered by its own audit when it
    advances at Step 6.1.

Pre-push blocking check
-----------------------

Run the full test suite via the [test skill](../test/SKILL.md) (`/skill:test`) and fix failures before pushing; outside scope → triage helper.

Final cleanup (belt-and-suspenders)
---------------------------------------

Before exiting at any point, `wl show <work-item-id> --json`; if `status: in_progress` and work is incomplete (not at Step 9), reset via `StatusLifecycle.update_status(work_item_id, "open")` to prevent orphaned `in_progress` items blocking other agents.

## Status Transition Matrix

| Phase | Mechanism | Status | Stage |
|-------|-----------|--------|-------|
| Claim (Step 1) | `update_status(id, "in_progress", stage="in_progress", assignee="<AGENT>")` / `phase_start()` | in_progress | in_progress |
| Epic/parent all children done (5.1) | `update_status(id, "completed", stage="in_review")` | completed | in_review |
| Final (Step 9) | `with StatusLifecycle(id, target_stage="in_review"):` / `phase_finish()` | completed | in_review |
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

## Dirty main-checkout recovery playbook

When `implement.py start` detects a dirty main checkout or orphaned stashes,
follow this decision tree to resolve the issue **without touching the operator's**
uncommitted changes without explicit permission.

### Decision tree: dirty working tree

```
Dirty main checkout detected?
│
├─ Only .worklog/ changes?
│  └─ YES → Safe to proceed. Carry forward.
│
└─ Other uncommitted changes?
   │
   ├─ Are they your own WIP from this session?
   │  ├─ YES → Commit them to a temporary branch or stash (with permission),
   │  │          then create a worktree. Never stash without asking.
   │  └─ NO (someone else's) → STOP. Ask the operator.
   │
   └─ Do you know whose changes these are?
      ├─ YES → Coordinate with the author: commit, revert, or reset.
      └─ NO (stale/unidentified) → Report to operator; DO NOT delete or
                                        stash without explicit permission.
```

### Decision tree: orphaned stashes

```
Orphaned stash detected (no matching open work item)?
│
├─ Stash message contains a work-item ID (SA-XXXXXXX)?
│  ├─ YES → Check if that work item is in_review/completed:
│  │         └─ YES (stale) → Delete: git stash drop stash@{N}
│  │         └─ NO (in-progress elsewhere) → Leave it (another agent owns it)
│  │
│  └─ NO → Stash has no work-item reference:
│         ├─ Can you reconstruct what's in it? (check reflog)
│         │  ├─ YES → Restore: git stash apply stash@{N}, then create a
│         │  │           work item and commit via a worktree.
│         │  └─ NO → Flag for operator review; delete only with permission.
│         └─ Is it clearly a forgotten experiment or test?
│            └─ YES → Safe to delete after documenting in comments.
│
└─ Multiple orphaned stashes?
   └─ Run: scripts/hygiene_check.sh --json for a structured report.
```

### Recovery examples

**Restore an orphaned stash:**
```bash
git stash apply stash@{0}
git stash drop stash@{0}
```

**Delete a confirmed-stale stash:**
```bash
git stash drop stash@{0}
```

**Periodic hygiene check:**
```bash
scripts/hygiene_check.sh
cron: 0 */4 * * * cd /path/to/repo && scripts/hygiene_check.sh >> /var/log/hygiene.log 2>&1
```

### Key rules (always apply)

1. **Never stash, commit, or revert** another user's changes without explicit
   permission — this is the foundational invariant that caused the original
   incident (SA-0MSALRZ3B006FPI5).
2. **Never delete stashes** that reference an open work item — they may be
   needed by another agent.
3. **Always document** stash dispositions in work-item comments for audit trail.
4. **Run `scripts/hygiene_check.sh`** periodically to catch issues early.


## Final step: standardized end-of-session report

Render the canonical end-of-session report (helper: [`../report/SKILL.md`](../report/SKILL.md)) as the **last step**, replacing any ad-hoc end-of-session summary:

```bash
python3 $(skill_path report)/scripts/render_report.py <work-item-id> \
  --skill-name <skill_name> \
  --headline "<1-3 sentence headline summary>" \
  --ac "<AC# description>|<verification metric>|met" \
  --ac "<...>|<...>|unmet" \
  [--producer-actions "<actions for the producer, or omit for 'None needed'>"] \
  [--notes "<freeform context/caveats/assumptions>"] \
  [--next-action <review|plan|implement|...>]
```

Then close with: `<work-item-id>: <one-line summary>`. Do NOT re-summarize the report in a different format — the report is the summary.
