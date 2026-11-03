# Worktree isolation for implementation work

All implementation work MUST happen inside the git worktree created by
`implement.py start <WIP-id>`. This document explains **why**, the one
non-obvious split agents hit (**`wl` needs the main checkout, edits need the
worktree**), and how to prove you are really inside the worktree — because
`cwd` alone does not confine an agent.

Related: [implement skill](../../skill/implement/SKILL.md),
[implement skill reference](implement-skill-reference.md),
[main-checkout wipe RCA](rca-main-checkout-wipe.md),
[test isolation](test-isolation.md). Origin: incident in repo
`tce-main-street` (epic `MS-0MUXW90YE009L4DQ`, child
`MS-0MUXWGT7G009THBO`), the 2026-10-26 ContextHub wipe
(`WL-0MVPADZ1Z000FOSG`, see
[the RCA](rca-main-checkout-wipe.md)) and work item `SA-0MUY9PSRS003V5CM`.

> **Normative invariant (WL-0MVPADZ1Z000FOSG).** A worktree-scoped agent run
> (build, test, finish, refactor) MUST NOT mutate the **main checkout** beyond
> the narrow allow-list in
> [The main checkout is a guarded surface](#the-main-checkout-is-a-guarded-surface).
> Every deviation is either refused before it happens or aborted loudly with
> the offending paths named — never carried silently into a commit or push.

## Repo-specific pages

This page is the framework-wide copy: `AGENTS_GLOBAL.md` links it by absolute
path and the implement skill points at it. ContextHub carries its own
[worktree-isolation page](/home/rgardler/projects/ContextHub/docs/dev/worktree-isolation.md)
with the guards that only apply inside that repository:

- **main-checkout deletions** — detecting, refusing and recovering from a run
  that removed files from the shared checkout;
- **repo-wide filesystem walks** racing concurrent worktree churn;
- **module resolution in a worktree** — `@worklog/shared` resolving into the
  main checkout's `packages/shared/dist`, and the stale-`dist` false red that
  follows from editing `packages/shared` in a worktree.

The rule in this page applies everywhere; read ContextHub's page as well when
the work item lives there.

## Why worktrees

`implement.py start` creates
`.worklog/worktrees/wl-<WIP-id>-<slug>` on a fresh branch forked from `dev`.
The worktree keeps each work item's edits off the shared checkout so that:

- concurrent agents never fight over the same working tree;
- `implement.py finish` can build, test, commit and push exactly the changes
  for one work item;
- a failed or abandoned run can be discarded without touching `dev`.

`implement.py finish` refuses (fails closed) when it detects changes outside
the worktree, and `phase_parent`/`drive` detect a driven child whose work
landed in the main checkout — see **The placement guard** below.

## The split: `wl` reads the main checkout, edits live in the worktree

`wl` stores work-item data in `<main-checkout>/.worklog/` — **not** in the
worktree. A linked worktree has no local `.worklog`, so:

- running `wl sync` (or any `wl` command) from inside a worktree fails or
  operates on the wrong store;
- the temptation is to stop prefixing commands with the worktree path and
  instead `cd <main-checkout> && ...` — **this is the incident**. Once you are
  in the main checkout it is very easy to write files there too.

The rule is simple: **edits stay in the worktree; `wl` targets the main
checkout's `.worklog` explicitly.** You never need to leave the worktree to
run `wl`.

### Correct: run `wl` from inside the worktree

Pass the main checkout's worklog directory explicitly with `--worklog-dir`:

```bash
# From inside .worklog/worktrees/wl-<WIP-id>-<slug>:
wl --worklog-dir /abs/path/to/main-checkout/.worklog \
   comment add <WIP-id> --comment "..." --author "<agent>" --json

wl --worklog-dir /abs/path/to/main-checkout/.worklog \
   show <WIP-id> --json
```

`StatusLifecycle` resolves the same directory automatically (its
`--worklog-dir` precedence: explicit > prefix-to-sibling scan > cwd chain), so
workflow scripts that use it keep working from a worktree.

### Wrong: leave the worktree to run `wl`, then edit there

```bash
# DO NOT DO THIS — it is how work lands in the main checkout:
cd /abs/path/to/main-checkout && wl show <WIP-id> --json   # forgets to return
# ...now every subsequent edit path is under the main checkout...
```

## Verify the worktree root before your first edit

`cwd` is only a hint — an agent can override it per command by prefixing
`cd <main-checkout> && ...`. Before the first write, assert the root and keep
every write/edit path inside it.

```bash
# 1. From inside the worktree, confirm the git root is the worktree:
expected="/abs/path/to/main-checkout/.worklog/worktrees/wl-<WIP-id>-<slug>"
test "$(git rev-parse --show-toplevel)" = "$expected" \
  || { echo "REFUSING: not in $expected"; exit 1; }
```

A driven child session is additionally given the root in the environment, so
it can assert without hard-coding a path:

```bash
test "$(git rev-parse --show-toplevel)" = "$IMPLEMENT_WORKTREE_PATH" \
  || { echo "REFUSING: outside the driven worktree"; exit 1; }
```

Rules:

- Never edit through an absolute path under the main checkout (for example
  `/abs/path/to/main-checkout/src/...`) — that writes outside the worktree
  even when the shell `cwd` looks correct.
- Resolve every `write`/`edit` target inside the worktree root.
- If you `cd` anywhere, `cd` back before editing; prefer a single explicit
  `--worklog-dir` invocation over changing directory at all.

## `IMPLEMENT_WORKTREE_PATH`

`phase_drive` exports `IMPLEMENT_WORKTREE_PATH` (the absolute child worktree
path) into each driven child session's environment, alongside the
`IMPLEMENT_DRIVE_ACTIVE=1` recursion guard. It is the machine-checkable
counterpart to the human instruction above.

## The state file: `repo_root` is the main checkout

`<worktree>/.implement_state.json` records the run's metadata. Its
`repo_root` field has exactly one meaning: **the main checkout root** — never
the worktree, never a path inside it.

This matters because every consumer of that field acts on the main checkout:

- `finish` fast-forwards `dev` in the main checkout (hygiene only) and posts
  the parent-branch sync there;
- the **dev push runs from the worktree** that holds the pushed commit — not
  from `repo_root` — so `.githooks/pre-push` resolves its project root to the
  pushed tree and validates the pushed commit (SA-0MVTFMB7K002Y1F3); a
  pre-push subject guard refuses the push when the worktree does not hold that
  commit;
- the pre-push gate's **subject is the pushed commit**
  (SA-0MVTRBT50005DUEH): the hook forwards `<local_sha>` as
  `run_tests.py --gate-commit`, which measures (and cache-keys against) a
  checkout of that commit with no dirty paths — the pusher's worktree on the
  normal finish path, else another clean `git worktree list` checkout — and
  refuses the push when none exists. A push from a **dirty** linked worktree
  therefore cannot leave a green verdict behind: either another clean worktree
  of the commit is gated, or the push is refused;
- `_remove_worktree(..., repo_root=<value>)` refuses to remove a path that
  resolves to `repo_root`, so a worktree recorded as the root makes finish
  raise `REFUSE to remove the repository root` *after* a successful push;
- the signal handler restores (`git checkout` / `git pull`) in `repo_root`.

**Resolution rule.** `phase_start` resolves it with `_get_repo_root()`, which
returns the main checkout even when invoked from **inside** a linked worktree
(`git rev-parse --git-common-dir`'s parent). Resuming `start` from inside an
existing worktree — the normal driven-child pattern — therefore also records
the main checkout. Never derive it from `Path.cwd()`: when `cwd` is the
worktree that writes the worktree into the state file.

**Stale state is repaired, not trusted.** A state file written by an older
build may already carry the worktree (or a path inside it) as `repo_root`.
`finish` and `abort` detect that (`_resolve_main_repo_root`), log the rejected
and the derived value at WARNING, and use the main checkout root anyway — so a
stranded worktree finishes cleanly without hand-edited JSON.
`_remove_worktree`'s safety gates are unchanged: the corrected root is
supplied to them, the gates are never relaxed.

Regression coverage (real temp git repo plus linked worktree, no
`_get_repo_root` mocking): `skill/implement/tests/test_repo_root_resolution.py`.

## Worktree removal ownership

A worktree must never be deleted out from under the session that owns it as
its cwd: doing so makes every later `bash` call fail with `Working directory
does not exist` (absolute-path file tools keep working), exactly when the
session still has to write its completion comment, render its report and
verify the pushed commit (SA-0MVTE8XAJ002IUDR). Exactly one owner removes a
worktree, decided by context:

| Flow | Owner | When |
|---|---|---|
| Driven child session (`IMPLEMENT_DRIVE_ACTIVE=1`) | `implement.py drive` | After the child's session exits; terminal child, clean tree only |
| Opted-in plain leaf (`finish --keep-worktree` / `IMPLEMENT_KEEP_WORKTREE=1`) | The session itself (or an operator) | The session's **last** bash command, after the comment, report and post-push verification |
| Plain leaf (default) | `finish` | Immediately after a successful dev push |
| Parent/parent-phase flow | `phase_parent` (its own `finish` subprocess per child) | Each child is finished in its own subprocess and its worktree removed there — no session-level cleanup instruction applies |
| Aborted item | `abort` | Unconditionally |

`_should_defer_worktree_removal()` is the single deferral predicate
(`IMPLEMENT_DRIVE_ACTIVE`, `--keep-worktree`, `IMPLEMENT_KEEP_WORKTREE`); it
gates `finish`'s Step 6b/7 so the tree, its branch and
`.implement_state.json` survive a deferred finish, and it is honoured by the
signal handler (`SIGTERM`/`SIGINT` still releases the item, never the tree).
Removal itself stays `cleanup_worktree_processes` + `_remove_worktree` (plus
`remove_state`) — no second deletion mechanism.

**Interruption safety.** A deferred finish leaves the tree present and
reported, never half-removed: `phase_start` resumes onto the retained
worktree instead of failing on `git worktree add`, and the driver's
start-of-run sweep (terminal children only) plus its `retained_worktrees`
report (each with a `git worktree remove … && git worktree prune` remedy) keep
a deferred tree from leaking. A plain leaf session that deferred its own
removal finishes the job with
`python3 skill/implement/scripts/implement.py cleanup <work-item-id>` as its
**last** bash command — the command `finish` names in its completion comment,
human output and JSON report (`steps.worktree_cleanup.command`). The action
shares the driver's single removal path, refuses a non-terminal item and a
dirty tree, and is an idempotent reported no-op for an already-absent tree.
Full contract:
[implement-skill-reference.md](implement-skill-reference.md#worktree-removal-ownership-sa-0mvte8xaj002iudr).

**The session-side removal is `implement.py cleanup <id>`**, not a raw `git
worktree remove` (SA-0MVTHIZE0007EAKZ): it routes through the same
`cleanup_worktree_processes` → `_remove_worktree` → `remove_state` path, so it
inherits the dirty-tree, repo-root and not-a-worktree refusals, and it refuses
to touch a non-terminal item. It must be the session's **last** bash command,
and re-running it is a reported no-op (`already_absent`) — see
[session-side removal
action](implement-skill-reference.md#the-session-side-removal-action-implementpy-cleanup-id).

## A branch upstream that points at itself breaks `git pull`

A linked worktree and the main checkout **share** `.git/config`, so the
branch-level tracking keys are **one per branch name**, not one per checkout:
`branch.<name>.remote` and `branch.<name>.merge` written from any worktree
are the same keys every other checkout (and every future `git pull`) reads.

`.` is the legitimate **worktree-ownership signal**. `git worktree add --track
-b wl-<id>-<slug> <path> dev` records `branch.wl-<id>-<slug>.remote = "."`, and
that value deliberately **survives `git worktree remove`** — sandbox cleanup
relies on it. For a `wl-*` branch it is correct and expected.

On a **long-lived** branch (`dev`, `main`, and any branch named in the
repository's release config) the identical value is a defect: `git pull`
resolves the upstream to the *same repository*, prints `From .` /
`Already up to date`, exits 0, and no remote data ever arrives. The failure is
silent, so agents keep running stale skills and a stale worklog view while
believing they are up to date (SA-0MVX1M6HH004YPX2).

Two supported operations write the key:

```bash
# 1. Worktree registration tracking a LOCAL base branch (implement.py start)
#    — correct: only the NEW wl-* branch gets "."; dev/main keep origin.
git worktree add --track -b wl-<id>-<slug> <path> dev

# 2. A push whose remote is the local path form — DEFECT on a long-lived branch:
git push -u . dev:dev      # → branch.dev.remote = "."
```

### Detect

| Surface | Behaviour |
|---|---|
| `scripts/hygiene_check.sh` (check 6) | reports every self-referential long-lived branch as a **warning** (exit 1); `--json` line `{"check":"self_referential_upstream","status":"warning","defects":[…]}` |
| `implement.py start` | emits the same finding as a **warning** at the start of every run (`steps.self_referential_upstream` in the JSON report) |

The detection module is `skill/shared/self_referential_upstream.py`
(`detect_self_referential_upstreams(repo)`; findings carry `branch`, `type`
(`defect`/`expected`), `remote`, `merge`, `message`). `wl-*` branches are
classified `expected` and are never defects. Directly:

```bash
python3 skill/shared/self_referential_upstream.py --json   # exit 1 if defects
```

Both integrations are **fail-open**: they never block a work item, never
rewrite configuration, and degrade to a skip when the module or the
repository is unavailable.

### Repair

Repair is an explicit operator action — nothing rewrites the config
implicitly:

```bash
# Preview first (writes nothing):
python3 skill/shared/self_referential_upstream.py --fix --dry-run --json
# Repair: only when "origin" is a configured remote AND it has the branch
python3 skill/shared/self_referential_upstream.py --fix --json
# Equivalent manual repair for one branch:
git branch --set-upstream-to=origin/dev dev
```

`--fix` writes only `branch.<name>.remote = origin` and
`branch.<name>.merge = refs/heads/<name>`; it refuses when `origin` is missing
or does not advertise the branch, never touches a `wl-*` branch, and never
checks out or modifies a working tree. Exit codes: `0` no defects left, `1`
defects remain, `2` environment error (no repository / no `origin` remote).

### Verify

```bash
git config --get branch.dev.remote        # must print: origin (never ".")
git pull                                  # must show "From github.com:…", not "From ."
git log origin/dev..dev                   # must be empty after the pull
```

### Prevention

No framework code path can produce the defect any more
(SA-0MVX35ABT000I1PD): `implement.py start` keeps `--track` for the `wl-*`
branch (the ownership signal stays) but **asserts** — after registering the
worktree, and again on every run — that the **parent** branch's tracking
config is untouched, failing loudly if a registration ever writes `remote=.
` onto the parent branch name; and every framework `git push` uses the
configured remote **name** (`origin`), never a path or `.` form, so `-u` can
never record a self-upstream.

## The placement guard

`phase_parent`/`phase_drive` fail closed when a **non-terminal** child's
worktree holds no work — no dirty files and **no commits ahead of** the parent
HEAD (`dev`), however far *behind* it is — while the main checkout holds
uncommitted changes outside `.worklog/`: evidence the child wrote to the main
checkout instead of its worktree. The failure names the offending
main-checkout paths and does **not** advance the parent.

The guard is deliberately precise: a legitimate child whose work is already in
its worktree, a clean main checkout, or `.worklog/`-only dirt never trigger
it. The ahead-count (rather than a HEAD comparison) is what keeps a
behind-only worktree — a long-running drive whose branch fell behind after
unrelated commits landed on `dev` — on the "no work" side of the guard and on
the "implement next" side of `phase_parent` (SA-0MVRN5Q67005H3M5).

## The main checkout is a guarded surface

A linked worktree shares one object store — and, in agent workflows, one
symlinked `node_modules` chain
(`<worktree>/node_modules -> <main>/node_modules`, with
`node_modules/@worklog/shared -> ../../packages/shared`) — with the **main
checkout**. Implementation work therefore happens *inside* the worktree, but
`implement.py finish` necessarily operates on the main checkout: it
fast-forwards `dev`, pushes from it, and removes the worktree afterwards. On
a shared host several agents can be finishing at the same time.

The 2026-10-26 incident showed what that costs when nothing watches the main
checkout: 25 tracked files under `packages/shared/` disappeared from the main
working tree while an unrelated session ran in it, and `wl` itself stopped
working ([RCA](rca-main-checkout-wipe.md)).

### Legitimate mutations (allow-list)

Only these main-checkout mutations are legitimate during a worktree-scoped run:

| Mutation | Actor | Why it is legitimate |
|---|---|---|
| Fast-forward of the main checkout's `dev` to the finished commit | `implement.py finish` Step 5 | Keeps the push baseline current; its own working-tree churn is attributed to finish (`own_churn`) and never aborts. `--ff-only` only, and restored to `origin/dev` if the push fails |
| Writes under `<main-checkout>/.worklog/` | `wl` (comments, status, cache), the finish lock file | Worklog bookkeeping is the store's own surface, never a source change |
| Project-declared expected-dirty paths | the project (`.pi/skills_extensions/implement/extension.json` → `ignoreDirtyPaths`) | Read-only background state the project explicitly declares |
| Nothing else | — | Any other tracked-file deletion or modification is foreign |

### Refused or aborted mutations

| Defect class | Enforcement point | Evidence |
|---|---|---|
| A test run mutates the guarded checkout, or the main checkout of a worktree-scoped run | outer `guarded_run_all` guard and the inner `live_repo_guard` plugin (both snapshot **every reachable checkout**) | `skill/shared/tests/test_git_sandbox.py` (`TestGuardedRoots`), `skill/test/tests/test_guarded_run_all.py`, `tests/test_live_repo_mutation_guard.py` |
| A foreign mutation of the main checkout is observed by `finish` (deletion **or** modification) | finish main-checkout guard (`_snapshot_main_checkout` → `_verify_main_checkout` at `pre_push` and `pre_worktree_removal`): aborts loudly, names every offending path, the concurrent-actor evidence and `git checkout -- <path>`; the item stays `open` | `skill/implement/tests/test_finish_main_checkout_guard.py` |
| Two finishes operate on the same main checkout concurrently | per-checkout exclusive `flock` on `<main-checkout>/.worklog/locks/main-checkout.lock` (bounded wait, kernel-released, stale lock files never block): the second finish exits non-zero naming the holder, without touching the checkout or the work item | `skill/implement/tests/test_finish_main_checkout_lock.py` |
| A finish commit never reaches `origin/dev` (including the rebased replacement hash produced by non-fast-forward recovery) | post-push ancestry assertion (`git merge-base --is-ancestor <commit> refs/remotes/origin/dev`) fails closed: worktree + branch retained, item `open`, refusal comment posted | `tests/test_implement_finish_local_dev_verified_sync.py` |
| A failed push leaves local `dev` pointing at an unpushed commit (the 2026-10-25 orphan shape) | push-failure restore returns local `dev` to `refs/remotes/origin/dev` (`reset --merge` when checked out, never `--hard`; compare-and-swap `update-ref` otherwise) | `tests/test_implement_finish_local_dev_verified_sync.py` |
| A driven child writes to the main checkout instead of its worktree | placement guard in `phase_parent`/`phase_drive`: fails closed, names the offending paths, does not advance the parent | `skill/implement/tests/test_implement_parent_safety.py` |
| A periodic/operator check of main-checkout hygiene | `scripts/hygiene_check.sh`: `main_checkout_lock` and `tracked_deletions` checks (human and `--json`) | `tests/test_implement_stash_hygiene.py` |
| The shared workspace build output is missing, breaking `wl` | `wl` preflight (`src/cli.ts` in ContextHub): one actionable diagnostic and a non-zero exit instead of `ERR_MODULE_NOT_FOUND` | `tests/cli/missing-shared-dist.test.ts` (ContextHub), [dev note](https://github.com/SorraTheOrc/ContextHub/blob/dev/docs/dev/wl-shared-dist-diagnostic.md) |

The finish-side details (report keys `report["steps"]["main_checkout_guard"]`,
`report["steps"]["main_checkout_lock"]`, `["local_dev_fast_forward"]`,
`["local_dev_restore"]` and `["ancestry_assertion"]`, the `own_churn`
allow-list, the per-checkout lock contract and the refusal-recovery path) are
documented in the
[implement skill reference](implement-skill-reference.md#finish-main-checkout-guard-wl-0mvpadz1z000fosg-ac3)
and its
[verified sync / ancestry assertion section](implement-skill-reference.md#verified-reversible-local-dev-sync-and-post-push-ancestry-assertion-sa-0mvpzg7a0000n66u).

### Residual risk

The RCA proved the *silence* mechanism (a worktree-scoped run could mutate the
main checkout without any guard noticing) and named the surviving mechanism
class — a worktree-scoped `npm run build` whose postbuild re-anchors onto the
main checkout (`scripts/install-herdr.sh`) combined with the nested
`node_modules` symlinks — but **no single command could be reproduced** from
the surviving evidence. The mitigation is therefore detection-first: the
guards above refuse or abort the *effect* (a mutated main checkout) whatever
the trigger was. Residual risk: a mutation performed while no finish/guard is
running stays undetected until the next hygiene check
(`scripts/hygiene_check.sh`) or finish run notices it.

## Recovery: the main checkout was mutated

This mirrors exactly what the incident session did, in order:

1. **Inspect before touching anything** — confirm the change is pure:

   ```bash
   cd /abs/path/to/main-checkout
   git status --porcelain=v1          # deletions show as " D <path>"
   git diff --stat                    # verify no local edits you need
   ```

2. **Restore tracked deletions from HEAD** (never stash or reset — that can
   strand another agent's work):

   ```bash
   git checkout -- packages/shared   # or an individual `git checkout -- <path>`
   ```

3. **Rebuild the gitignored shared build output** (`wl` resolves
   `@worklog/shared/*` from it):

   ```bash
   npm run build:shared
   wl show <any-id> --json           # proves wl resolves again
   ```

4. **Confirm the checkout is hygienic** (no held finish lock, no tracked
   deletions, no orphaned stashes):

   ```bash
   scripts/hygiene_check.sh          # add --json for machine-readable output
   ```

5. **Re-run the interrupted step** — `implement.py finish <id>` from the
   worktree, or the original `wl`/plan command. A `finish` that observed the
   mutation has already aborted and left the work item `open`.

## Recovery when work landed in the main checkout

1. Read the failure message — it names the offending paths.
2. Move those changes into the child's worktree:

   ```bash
   cd /abs/path/to/main-checkout/.worklog/worktrees/wl-<WIP-id>-<slug>
   # copy the named paths in, then restore the main checkout:
   git -C /abs/path/to/main-checkout checkout -- <path>      # if tracked
   rm /abs/path/to/main-checkout/<untracked-path>            # if untracked
   ```

3. Re-run the child implementation from inside the worktree.
