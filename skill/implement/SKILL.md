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

- Deep implementation reference (relocated detail, tables, guard matrices):
  [docs/dev/implement-skill-reference.md](../../docs/dev/implement-skill-reference.md).
- Intake/interview helpers: `intake`, `plan`, `interview`.
- [Test writing guidelines](../shared/test-writing-guidelines.md),
  [background jobs](../shared/background-job-etiquette.md),
  [status lifecycle](../shared/status_lifecycle.py),
  [handling assets](../../docs/dev/implement-skill-reference.md#relocated-handling-assets),
  [timeouts](../../docs/dev/implement-skill-reference.md#relocated-test-timeout-configuration).

Security note — **protected branches** (`main`/`master`/`HEAD`) and **creating
PRs** require explicit operator permission: never push to them or open PRs
automatically. Pushing the feature branch into `dev` (Step 9) is
**pre-authorized by this workflow** — the pre-push hook blocks
`main`/`master`/`HEAD` only — and needs no further approval when the build
passes and the test gate is green. `implement.py finish` validates with
**changed scope only**; the **full suite first runs at the dev-push
integration gate** (`run_tests.py --scope full` in the pre-push hook), and the
ship release gate re-runs it on the merged dev→main tree
(SA-0MVECQUGF0064U81, SA-0MT6BYQHB008DOGC). `finish` resolves that verdict
before taking the main-checkout lock (SA-0MWFJS3820032YB4).

Privacy note: avoid secrets/tokens/PII in comments or PR bodies; mask/redact
sensitive values.

## StatusLifecycle Integration

All status transitions go through the shared `StatusLifecycle` context manager
([../shared/status_lifecycle.py](../shared/status_lifecycle.py)) — never
ad-hoc status writes (`wl update --status`): always `StatusLifecycle`.
`implement.py` uses it automatically (`phase_start` claims `in_progress`,
`phase_finish` lands `completed`/`in_review`, `phase_abort` resets to `open`);
manual: `StatusLifecycle.update_status(id, status, stage=…)`.

- **Actively worked ⇒ `in_progress`** — guard with
  `StatusLifecycle.require_claimed(<id>)`, resume with `ensure_claimed(<id>)`.
- **Assignee release on every terminal outcome** — `finish` clears it with the
  `completed`/`in_review` transition; every abort reset to `open` passes
  `assignee=""`
  ([assignee-release invariant](../../AGENTS_GLOBAL.md#assignee-release-invariant)).
- **Terminal status on both finish paths** — both persist it, then read back
  and fail loudly if lost (`tests/test_finish_no_diff_status.py`).

Deep detail:
[reference — StatusLifecycle](../../docs/dev/implement-skill-reference.md#relocated-statuslifecycle-integration).

## No-op finish: refusal by default, `--verification-only` opt-in

`finish` refuses a worktree with no commit **attributable to the item**: no
push, no terminal write, no completion comment, `success: false` with
`steps.commit.reason = "no_commit_no_signal"`, item released to `open`; the
ambient `dev` HEAD is never named as this item's commit.

- **Interrupted-push re-run.** A finish interrupted during its push leaves a
  clean worktree whose commit already exists; re-running `finish` attributes
  that **prior-run commit** (ahead of `origin/<parent_branch>`, message
  references the item) and finalises normally. A genuinely work-less worktree
  is still refused.
- **Produce work (default).** A clean, commit-less worktree is unfinished work:
  re-enter it, produce what the acceptance criteria require, re-run `finish`.
- **Verification-only (opt-in).** `start <id> --verification-only` records the
  signal in `.implement_state.json`; `finish <id> --verification-only` supplies
  it directly. Either pushes a no-op, writes the terminal status and reports
  "no changes / no commit".

`drive`/`phase_parent` report a work-less (`no-work`) child
`action: "not-finalisable"` — no `finish`, parent not advanced — while a
`verification-only` opt-in keeps it finalisable. Deep detail:
[reference — no-commit finalisation](../../docs/dev/implement-skill-reference.md#no-commit-finalisation-refusal-by-default---verification-only-opt-in).

## Project-local extensions

`.pi/skills_extensions/implement/` may carry `SKILL_PREFIX.md` (surfaced by
`start`), `SKILL_POSTFIX.md` (surfaced by `finish`) and `extension.json` with
`ignoreDirtyPaths` (expected-dirty prefixes that do not fail the dirty-tree
gate). Absence is a no-op; a malformed extension releases the item to `open`.
See [docs/dev/skill-extensions.md](../../docs/dev/skill-extensions.md).

## Best Practices

- Follow the steps in order; do not skip steps.
- **Testing is required — TDD preferred, not mandatory.** Write tests first
  whenever practical; test-after is permitted when TDD would complicate
  implementation. External constraints prevent complete tests → create
  harnesses/mocks and document the limitation. **Do NOT write placeholder
  tests** — track unimplemented features in a work item instead.
- No search tools (grep/ripgrep/code search) — rely on work-item context and
  linked docs; if insufficient, run intake interview. Keep changes focused on
  ACs; never edit `dist/`/`node_modules/`; CLI/API work always provides JSON.
  Document decisions in work-item comments; handle errors with actionable
  remediation.
- Follow AGENTS.md policies for branch naming, commit discipline, worktree
  workflow, and push-to-dev
  ([AGENTS_GLOBAL](../../AGENTS_GLOBAL.md#implement-the-work-item)); after
  `in_review`, use the cleanup skill to tidy local feature branches (not
  `dev`/`main`). Use `StatusLifecycle` for all status transitions.

## Background Job Etiquette

Follow the shared
[background-job etiquette](../shared/background-job-etiquette.md): launch with
a tailable log, then **one** completion-driven wait
(`../shared/wait_for_job.py`); never poll with `sleep N`.

## Gate failure diagnosis

A red finish gate reports a machine-readable `reason`; read it first:
[reference — reading a gate failure](../../docs/dev/implement-skill-reference.md#relocated-reading-a-gate-failure--reason-and-its-facts-sa-0mvtet8kq0031r6z).

## Status Safety & Abort Handling

**Every abort/failure path MUST reset status to `open`** (with `assignee=""`)
to release the lock — an orphaned `in_progress` item blocks work. Full
scenarios and reset semantics:
[reference — status safety](../../docs/dev/implement-skill-reference.md#relocated-status-safety--abort-handling).

## Steps

Execute the following steps in order. Do not skip steps. Record outputs in
work-item comments as you proceed.

1. Set status and safety gate

Claim with a **status-only** update:
`StatusLifecycle.update_status(<work-item-id>, "in_progress", assignee="<AGENT>")`
(or `implement.py start`). Do **not** pass `stage="in_progress"` — it is a
status, not a stage; `wl` applies both atomically and would reject the update.

> **Code Freeze gate:** `implement.py start <id>` checks the Code Freeze
> marker (`.worklog/code-freeze.json`, WL-0MSBU4KMA004PKSR) **before**
> claiming; a release in progress refuses the run without changing the item
> status. No `--force` bypass; fail-open when the marker is missing/corrupt.

2. Safety gate: handle dirty working tree

`implement.py start` performs this gate automatically (a **Key-Files-aware**
gate, SA-0MV0NSOGE000OSMR). Run `git rev-parse --is-inside-work-tree` and
`git status --porcelain=v1 -b`.

**CRITICAL: Never stash, commit, or revert the user's uncommitted changes
without explicit permission** — they may be user-authored work; stashing them
without asking can strand that work and is forbidden. When uncommitted changes
exist, STOP and ask the operator how to proceed (commit, stash, revert, or
abort); act only after the operator explicitly chooses.

- Dirty paths that do **not** overlap the description's Key Files section are
  left untouched in the main checkout and a clean worktree is created from
  `HEAD`; a dirty path that overlaps Key Files — or no Key Files section —
  asks the operator. Project-declared expected-dirty paths (`ignoreDirtyPaths`)
  do not stop the gate. The gate protects the **main checkout** only; inside a
  linked worktree it is skipped (those dirty files are the agent's own work).

3. Stash hygiene gate (warn on orphaned stashes)

`implement.py start` inspects `git stash list`; stashes that reference an open
work item are matched, others are reported **orphaned** as a **WARNING**
(fail-open; `--allow-orphaned-stashes` acknowledges). Triage each orphan:
restore-and-commit via a proper work item, or delete if stale — never delete a
stash without explicit permission. `scripts/hygiene_check.sh` runs the same
check periodically. Recovery playbook:
[reference — dirty main-checkout recovery](../../docs/dev/implement-skill-reference.md#relocated-dirty-main-checkout-recovery-playbook).

4. Understand the work item

Fetch `wl show <work-item-id> --json`; read `description`, acceptance criteria
and `comments`. Restate ACs/status; surface blockers/dependencies; inspect
linked PRDs/plans/docs.

- **Rejected audits:** fetch `wl audit-show <work-item-id> --json` and
  `wl comment list <work-item-id> --json`; act on the **most recent** rejection.
- **4.1 Definition gate:** clear scope, concrete/testable ACs, constraints and
  unknowns. Missing `risk`/`effort` is filled automatically — never a gate
  failure. On a definition failure: reset to `open`, run the intake/plan
  interview, ask whether to restart. Ill-defined → `wl reviewed <id> true` and
  STOP.
- **4.2 Already implemented:** if the skill previously reported completion with
  a commit, or status/stage is inconsistent, run `/skill:audit <id>`, fix gaps
  inline, and if clean proceed to Step 9.
- **4.3 Duplicate work items:** search (`wl search <keywords> --json`) for an
  overlapping completed item; a full duplicate is closed with a comment, a
  partial overlap rewrites this description/ACs to the gaps.

5. Create a worktree from dev

> **MANDATORY — worktree requirement:** all implementation work MUST happen in
> a git worktree created from `dev` — never edit, commit, or push from the main
> checkout. `implement.py finish` refuses changes outside the worktree;
> `implement.py start` creates it for you.
>
> **`cwd` is not sufficient.** Assert the worktree root before your first write
> and keep every write/edit path inside it:
>
> ```bash
> test "$(git rev-parse --show-toplevel)" = "$(pwd)" || exit 1
> test "$(git rev-parse --show-toplevel)" = "$IMPLEMENT_WORKTREE_PATH" || exit 1
> ```
>
> Never edit through an absolute path under the main checkout — `phase_parent`/
> `drive` fail closed on a driven child whose work landed there, and a
> **sibling worktree** (another item's linked worktree) is equally forbidden —
> refuse any write whose path resolves into one. Run `wl` worktree-safely with
> `wl --worklog-dir <main-checkout>/.worklog …`. See
> [docs/dev/worktree-isolation.md](../../docs/dev/worktree-isolation.md).
>
> **Driven red-gate rule:** a driven session (`IMPLEMENT_DRIVE_ACTIVE=1`)
> whose first `finish` gate is red for a failure it did not introduce triages
> (creates the work item, records the block) and **stops** — never
> `implement.py start` that item, never edit its (or any) worktree.

```bash
git worktree add --track -b wl-<WIP-id>-<short-slug> .worklog/worktrees/wl-<WIP-id>-<short-slug> dev
cd .worklog/worktrees/wl-<WIP-id>-<short-slug>
```

> **`node_modules` is a worktree-local link farm:** `implement.py start`
> materialises it as per-package symlinks (nested and workspace packages
> resolve to the worktree itself; supersedes the whole-directory symlink,
> SA-0MSGS763C006SM1B). **Do NOT run `npm install` inside a worktree** —
> writes corrupt the shared install. Deep detail:
> [reference — worktree creation](../../docs/dev/implement-skill-reference.md#relocated-step-5--create-a-worktree-from-dev-and-branch-inside-it).

> **Git submodules are auto-initialised:** `implement.py start` runs
> `git submodule update --init --recursive`. Failures produce a **WARNING**
> but do not abort (best-effort); repos without `.gitmodules` are unaffected.

`implement.py start` handles the rest: idempotent resume from either `cwd`,
and the stale-shared-resolution fail-closed gate.

6. Implement

- Open/in_progress blockers or dependencies → implement them first.
- **Write tests first** (TDD preferred) — at least one test file before
  editing implementation code; minimal, focused changes. External constraints
  prevent complete tests → harnesses/mocks/placeholders, documented.
- **A clean, commit-less worktree is unfinished work.** If the ACs need a
  change and the tree has nothing to commit, the empty diff is not done —
  `finish` refuses the terminal write. Produce the work the ACs require and
  re-run `finish`; reserve `--verification-only` for items whose sole
  deliverable is an operational run recorded as evidence.
- Discovered additional work →
  `wl create "<title>" --deps discovered-from:<work-item-id> --json`.
- Once all ACs are met: **build** (no errors), then validate via `/skill:test`
  ([test skill](../test/SKILL.md)). `implement.py finish` runs a **changed-scope**
  validation; the **full suite** is the dev-push gate's, resolved pre-lock.
  `run_tests.py` must go through the test-skill runner — only it records the
  per-repo cache the audit reads. Update docs (except `CHANGELOG.md`).

6.1. Parent recursion (epic/parent items only)

**Preferred: drive every child in one invocation.**

```bash
python3 $(skill_path implement)/scripts/implement.py drive <parent-id>
```

`phase_drive()` spawns a fresh Pi session per child in that child's own
worktree, waits, then advances the parent — one session and one worktree per
child, serial in dependency order. Pane-driving flags (`--pane-sessions`,
`--no-pane-sessions`, `--focus`/`--no-focus`,
`IMPLEMENT_DRIVE_PANE_SESSIONS=1`, `IMPLEMENT_DRIVE_ACTIVE=1`) are opt-in
(needs `HERDR_ENV=1` + herdr) and
fail-open to the headless `pi -p` spawner (default for the downtime worker,
audit dispatch and CI).

**Blocked children are re-validated (SA-0MVTUFL0U0000NV2).** A `blocked`
child is re-checked each run against its `wl dep` edges and explicit
`Blocked by …` line: a non-terminal or unresolvable blocker ⇒
`skip-blocked` with `blocking_ids`, never started; all blockers terminal ⇒
the block is stale and the child starts. Precedence: `skip-terminal` →
`skipped-held` → `skip-in-progress` → `skip-blocked` → `implement`. `drive`
never spawns a `skip-blocked` child. Vocabulary (`blocking_ids`,
`blocker_details`, `stale_blocks`, `unvalidated_blocks`,
`unresolved_blockers`, extended `blocked_children` entries) and parsing rule:
[reference](../../docs/dev/implement-skill-reference.md).

**Driven-session lock holder (SA-0MW7LMUUQ00701KJ).** The long-lived driver,
not the short-lived `start` subprocess, holds the child's per-child lock for
the session's whole lifetime (`IMPLEMENT_CHILD_LOCK_TOKEN` re-entry, released
on every spawner outcome); a killed driver is the residual window
([reference](../../docs/dev/implement-skill-reference.md#per-child-implementation-lock-sa-0mvq7vmrl00498yb)).
`--infra-retry-limit N` (5; 2 s doubling to a 30 s cap) bounds finish
retries. **Manual fallback:**
`python3 $(skill_path implement)/scripts/implement.py parent <parent-id>` —
no children = leaf; all-terminal children advance the parent to
`completed`/`in_review`; otherwise `phase_parent` finishes children whose
worktrees hold changes and starts the next one. A blocked invoked item starts
**nothing** (`parent_blocked_by`). Full contract:
[reference — parent recursion and drive](../../docs/dev/implement-skill-reference.md#relocated-step-61--parent-recursion-epicparent-items-only);
failure recovery:
[reference — drive failure recovery](../../docs/dev/implement-skill-reference.md#drive-failure-recovery-bounded-finish-attempts-reconciliation-and-reset).

7. Automated self-review

Build and lint; fix issues. Sequential passes: completeness, dependencies &
safety, scope & regression, tests & acceptance, polish & handoff. Small,
goal-aligned edits; intent changes → Open Question and stop.

8. Optional refactor step

`python3 $(skill_path refactor)/scripts/refactor.py <work-item-id>` —
session-introduced smells fixed immediately; pre-existing smells create
Worklog items with REFACTOR comments. Skip with `--no-refactor`. See
[../refactor/SKILL.md](../refactor/SKILL.md).

9. Commit, Push to dev and mark in_review

- **Plain leaf flow — run the finish with the deferral flag:**

  ```bash
  python3 $(skill_path implement)/scripts/implement.py finish <work-item-id> --keep-worktree
  ```

  `finish` builds, tests (changed scope), commits, pushes to `dev`, verifies
  post-push and marks the item `in_review`; the terminal write and assignee
  release are its **last lifecycle action** (SA-0MVXUSK02003ZMDN) — nothing
  lifecycle-relevant runs after it. A commit-less finish is refused unless the
  verification-only opt-in is present — see
  [No-op finish](#no-op-finish-refusal-by-default---verification-only-opt-in).
  `--keep-worktree` (or
  `IMPLEMENT_KEEP_WORKTREE=1`) defers worktree removal; driven children never
  pass it (`IMPLEMENT_DRIVE_ACTIVE=1` defers).

  **Retry taxonomy (SA-0MW22SEMV00230O5):** § Bounded push loop.
- Follow the mandatory **build → test → commit** order.
- **Add the `## Impact` sentence before finishing.** A newly completed item's
  description must carry an `## Impact` section: one sentence describing the user-experience impact — not a technical summary.
- **Do NOT create a Pull Request to `main`.** Push the feature branch into
  `dev` via the ship skill (`pushToDev()` from
  `$(skill_path ship)/scripts/ship.js`, preferred) or
  `git push origin HEAD:refs/heads/dev`. `dev` is not protected; only `main`,
  `master`, `HEAD` are blocked.

  > **Pushing from a worktree:** the pre-push `wl sync` refuses from a
  > worktree. Sync from the main checkout, then push with
  > `WORKLOG_SKIP_PRE_PUSH=1 git push origin HEAD:refs/heads/dev`.

- **Post-push local parent branch sync:** `finish` fast-forwards the main
  checkout's local `dev` to the pushed tip (safe-skip if dirty/diverged);
  `start` refreshes it before `git worktree add`.
- **Worktree removal ownership.** A plain leaf session removes its own tree
  with `implement.py cleanup <work-item-id>` as its **last** bash command; a
  driven child (`IMPLEMENT_DRIVE_ACTIVE=1`) — **do not remove your own
  worktree** — `implement.py drive` removes it after exit; a parent/parent-phase
  flow (`phase_parent`) removes each child's tree in its subprocess. Never
  remove a live cwd. See
  [reference — worktree removal ownership](../../docs/dev/implement-skill-reference.md#worktree-removal-ownership-sa-0mvte8xaj002iudr).

  **Why rebuild?** `dist/` is gitignored; `git pull` does not update it.
- **Close your response with:** `<work-item-id>: <concise-summary>` then
  `Work committed to dev`. Without `finish`, mark `in_review` via
  `StatusLifecycle.update_status(<work-item-id>, "completed", stage="in_review")`
  — never after `finish` returns.

Full finish contract (finish lock, verified sync, refusal recovery, push
timeout, gate reasons, terminal read-back):
[reference — finish and push](../../docs/dev/implement-skill-reference.md#relocated-finish-lock-verified-sync-and-refusal-recovery).

## Status Transition Matrix

Claim → `in_progress`; Step 9 → `completed`/`in_review` (assignee released);
every abort/failure → `open` with `assignee=""`.
[Full matrix → reference](../../docs/dev/implement-skill-reference.md#relocated-status-transition-matrix).

## Scripts (canonical runner & modules)

No single orchestrator — follow the steps above and invoke project-local
build/test and linters
([reference](../../docs/dev/implement-skill-reference.md)).

## Final cleanup (belt-and-suspenders)

See [relocated detail](../../docs/dev/implement-skill-reference.md#relocated-final-cleanup-belt-and-suspenders).

## Final step: standardized end-of-session report

Render the canonical report ([../report/SKILL.md](../report/SKILL.md)) as the
**last step**:

```bash
python3 $(skill_path report)/scripts/render_report.py <work-item-id> \
  --skill-name implement \
  --headline "<1-3 sentence summary>" \
  --ac "Worktree created | implement.py start | met" \
  --ac "Build and tests green | build/test output | met" \
  --ac "Commit pushed to dev | git log | met" \
  [--producer-actions "<...>"] [--notes "<...>"] [--next-action <review|plan|implement|...>]
```

> **Note:** `--ac` records this skill session's own success criteria, not the
> work item's feature ACs (shown in Use Cases; SA-0MTQY7YM5009J6QR).

Then paste it verbatim into your final response and close with
`<work-item-id>: <one-line summary>`. End with `</end_session>` on its own line
when the session ends with no open questions.

## Final cleanup step: remove the retained worktree (plain leaf sessions)

A plain leaf session that ran `implement.py finish <work-item-id>
--keep-worktree` (or exported `IMPLEMENT_KEEP_WORKTREE=1`) owns its tree's
removal. Run it after the end-of-session report, as the session's **very last**
bash command:

```bash
python3 $(skill_path implement)/scripts/implement.py cleanup <work-item-id> --json
```

The post-push verification and completion comment already ran inside `finish`,
before the terminal write. A `retained_dirty`/`retained_non_terminal` outcome is
a reported retention — never force it (no force switch). Driven child and
parent/parent-phase sessions do **not** run `cleanup` here. See
[reference — worktree removal ownership](../../docs/dev/implement-skill-reference.md#worktree-removal-ownership-sa-0mvte8xaj002iudr).
