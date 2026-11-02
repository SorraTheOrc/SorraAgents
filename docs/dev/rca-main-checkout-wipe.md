# RCA: the 2026-10-26 main-checkout wipe (`packages/shared`, ContextHub)

**Work item:** WL-0MVPADZ1Z000FOSG (child WL-0MVPE2A1H006HFX1)
**Incident date:** 2026-10-26, 13:16 UTC
**Status of this document:** root-cause class identified and proven; the exact
trigger command could not be reproduced from the surviving evidence — the
ruled-out set and the residual hypothesis are recorded below.

## 1. Incident summary

While `/skill:plan WL-0MVGKGMU400631VZ` ran in the ContextHub main checkout
(`/home/rgardler/projects/ContextHub`, branch `dev`, HEAD `be2d7771`), 25
tracked files under `packages/shared/` were deleted from the working tree, the
gitignored `packages/shared/dist/` build output was removed, and `wl` broke
with `ERR_MODULE_NOT_FOUND: .../node_modules/@worklog/shared/worklog-paths`.
A concurrent `implement.py finish WL-0MVOBOX1U0013HNF` was running in the
worktree `.worklog/worktrees/wl-WL-0MVOBOX1U0013HNF-...` (PID 2640027) in the
same window.

Recovery (performed by the planning session, non-destructive): `git checkout --
packages/shared` then `npm run build:shared`.

## 2. Timeline

| Time (UTC) | Event |
|---|---|
| 13:16:09 | Concurrent session starts `/skill:implement finish WL-0MVOBOX1U0013HNF` in its worktree |
| 13:16:40 | The planning session runs `wl sync` in the main checkout (succeeds) |
| 13:16:49 | `packages/shared/` directory mtime moves; all 25 tracked files gone, `dist/` gone |
| ~13:19:50 | Manual recovery (`git checkout -- packages/shared`, `npm run build:shared`) — recovery rewrites the directory mtime again |

Surviving evidence at investigation time (2026-10-26, this session):

- `git status --porcelain` at the time of discovery reported exactly 25 ` D`
  entries, all under `packages/shared/`, with HEAD unchanged. ` D` (worktree
  deletion, index still holding the blob) is the signature that matters: the
  files were **unlinked from the working tree**, not removed from `HEAD`.
- The parent directory `packages/` mtime was untouched, so the deletions were
  per-file unlinks inside an existing directory rather than a removal of
  `packages/shared` itself. `packages/shared/dist/` (gitignored) went too, so
  the unlink covered the directory's **entire** content, tracked and ignored —
  the shape of a recursive directory removal, not of a targeted file edit.
- `/tmp/guard-finish.log` records that the concurrent finish escalated to a
  **full-scope** test run (`IMPLEMENT_TEST_COMMAND` override is not
  subsettable), i.e. repository-wide command execution started from a
  worktree-scoped run.

### 2.1 Second-level timeline (reconstructed from process logs)

`/tmp/guard-finish.log` line `test slot acquired — queued_at=1793020609.520
wait_seconds=309.068` pins two instants exactly: the finishing run began
waiting for a test-concurrency slot at **13:16:49.520 UTC** and started
executing its suite at **13:21:58.588 UTC** (`epoch + wait`).

| Time (UTC) | Event |
|---|---|
| 13:14:17 | A **sibling worktree** (`wl-WL-0MVOA0U84003EXVK-…`) starts a full `vitest run tests` (PID 2613991) — a second worktree-scoped repository-wide run |
| 13:16:09.998 | `/skill:implement finish WL-0MVOBOX1U0013HNF` starts in its worktree (PID 2640027); build step runs |
| 13:16:45.6 | The planning session launches its main-checkout `wl sync` (completes 13:17:05) |
| **13:16:49.34** | **`packages/shared/` mtime — the wipe** |
| 13:16:49.520 | The finishing run's test step *begins queueing* for a slot (so its own suite had **not** started) |
| 13:21:58 | The finishing run's suite finally starts |
| 13:33:43 | `dev` in the main checkout fast-forwards to the finish's commit `e2de9eb9` (`git reflog show dev`) — **17 minutes after** the wipe |

Two consequences, both used below: the finish's own **test run cannot be the
actor** (it had not started), and `implement.py`'s own Step-5 fast-forward in
the main checkout **cannot be the actor** either (it happened 17 minutes
later). The wipe lands inside the finish's **build/postbuild** window and inside
the planning session's `wl sync` — while a *second* worktree-scoped suite was
also running in the same repository.

## 3. Root cause of the *silence* (proven, deterministic)

The defect class is: **a worktree-scoped run mutated the main checkout and no
guard could see it.** Both live-repo guards snapshotted only the guarded root
(state before the fix; the fix is WL-0MVPE2BY9001CR9O):

- outer guard — `guarded_run_all()` in `skill/test/scripts/run_tests.py` took
  `snapshot_repo_state(project_root)` and verified that one snapshot in
  `_detect_live_repo_mutation()`;
- inner pytest plugin — `LiveRepoMutationGuard.__init__` in
  `skill/shared/live_repo_guard.py` took `snapshot_repo_state(self.root)` and
  compared it in `pytest_runtest_teardown` / `pytest_sessionfinish` for
  `self.root` only.

Both now use `snapshot_guarded_roots` / `diff_guarded_roots` /
`describe_guarded_diff`, which cover the guarded root **and** its main checkout
(see [implement-skill-reference.md](implement-skill-reference.md#live-repo-guard-ownership-and-main-checkout-coverage-wl-0mvpe2by9001cr9o)).

When `project_root` is a linked worktree (`<main>/.worklog/worktrees/<wl-…>`),
neither guard ever observes the main checkout, so a mutation there passes a
"clean run". Reproduced deterministically (pre-fix run this session, scratch repo with
a real linked worktree, `skill/shared/git_sandbox.py` helpers; the same probe
now reports the mutation):

```
worktree toplevel: /tmp/rca-guard-izrbk0qz/main/.worklog/worktrees/wl-TEST
main_checkout_root(worktree): /tmp/rca-guard-izrbk0qz/main
# then: rm main/pkg/keep.txt   (a tracked file, from the "worktree-scoped" run)
guard diff changed = False | describe: (no differences)
```

That single line is why a whole workspace package could disappear without a
single warning from the framework: the worktree-scoped snapshot pair is
structurally blind to `<main_checkout>`.

## 4. Hypotheses: ruled out, with evidence

Each was checked against the live code and, where cheap, executed in a
throwaway directory (never against a live checkout).

1. **`git clean` / `git stash` / `git reset --hard` in the main checkout.**
   *Ruled out.* None of these can produce `git status` ` D` entries with an
   unchanged `HEAD`: `git clean` cannot touch tracked paths, `git stash`
   restores tracked files to their `HEAD` content, `git reset --hard` rewrites
   the index and removes the `D` entries.
2. **`implement.py`'s own worktree removal.**
   *Ruled out.* `_remove_worktree` (`skill/implement/scripts/implement.py`
   `_remove_worktree`, safety gates 1 and 2) refuses to run when the target
   resolves to the repository root and refuses manual (`shutil.rmtree`)
   removal when the target contains a `.git/` directory, precisely so a main
   working tree can never be removed. It also falls back to
   `git worktree remove --force`, which git itself refuses for a main working
   tree.
3. **`wl cleanup-worktree`.** *Ruled out.* `src/commands/cleanup-worktree.ts`
   resolves PIDs from the worktree-process registry and sends `SIGTERM` /
   `SIGKILL`; it performs no filesystem mutation.
4. **A recursive delete following the `@worklog/shared` workspace symlink.**
   *Ruled out empirically.* Both `node_modules/@worklog/shared` (repo root) and
   `packages/herdr/node_modules/@worklog/shared` are symlinks to
   `packages/shared`; `packages/herdr/scripts/link-shared.mjs:76` runs
   `fs.rmSync(linkPath, {recursive: true, force: true})`. Measured on the host
   (`node v22.22.1`): `fs.rmSync(<symlink-to-dir>, {recursive:true,
   force:true})` unlinks the symlink and leaves every target file intact, and
   `rm -rf <symlink>/` (GNU rm 9) removes nothing. This path cannot delete
   `packages/shared` contents.
5. **`--no-refactor` / `commit -a` / concurrent `git worktree` bookkeeping.**
   *Ruled out as deletion paths.* `--no-refactor` only skips the refactor
   phase; `commit -a` commits, it never unlinks; `git worktree remove/prune`
   only act on registered worktrees and refuse the main working tree.
6. **The full-scope `run_tests.py` escalation itself.**
   *Ruled out as a direct delete path* (it only spawns the project's test
   commands) but **kept as the enabling condition**: it is what turned a
   worktree-scoped `finish` into repo-wide command execution in the first
   place.
7. **`implement.py`'s own Step-5 fast-forward in the main checkout.**
   *Ruled out by timestamp.* `git reflog show dev` dates the fast-forward to
   `2026-10-26 13:33:43` — 17 minutes after the wipe at 13:16:49 — and a
   fast-forward cannot produce ` D` entries anyway (it updates the index and
   the working tree together).

## 5. Residual hypothesis (the named mechanism class)

The only identified code path that performs a recursive `rm` inside the
repository from a worktree-scoped build is the herdr install step, which
deliberately re-anchors itself onto the **main checkout**:

- `scripts/install-herdr.sh:44-51` — resolves `MAIN_CHECKOUT` from
  `git worktree list --porcelain` and rewrites `REPO_ROOT` to it "even when the
  build runs inside a linked git worktree";
- `scripts/install-herdr.sh` step 0 — `npm ci --prefix "${HERDR_DIR}"` /
  `npm install --prefix "${HERDR_DIR}"` where `HERDR_DIR =
  <main>/packages/herdr`: npm's `ci` deletes and recreates
  `packages/herdr/node_modules`;
- step 0b — `node <main>/packages/herdr/scripts/link-shared.mjs`: a recursive
  `fs.rmSync` inside `<main>/packages/herdr/node_modules`;
- `implement.py start` symlinks **nested** `node_modules` for every package
  (`<worktree>/<pkg>/node_modules -> <main>/<pkg>/node_modules`), so a
  worktree-scoped `npm run build` writes through that symlink straight into
  the main checkout.

That combination is the residual hypothesis: **a worktree-scoped `npm run
build` (postbuild → `install-herdr.sh`, run in the finish's build step at
~13:16:4x) mutating the shared main checkout**, executed under a guard that
cannot see the main checkout. A second, equally plausible actor exists in the
timeline: the **sibling worktree's full `vitest run tests`** (PID 2613991,
13:14:17 onward), whose own `node_modules` chain resolves `@worklog/shared`
through the main checkout. Neither can be pinned without a command log that
names the 25 paths, and none survived. It is not proven for
the exact 25 `packages/shared/*` paths — no command log survived that ties
`npm`'s output to those paths — so this document stops at the mechanism class
rather than naming a single line to "fix".

Recommendation: **detection-first.** Make the guard watch the main checkout
(child WL-0MVPE2BY9001CR9O), make `finish` abort loudly on foreign deletions
(WL-0MVPE2CMW006K25Q) and serialise finishes per checkout
(WL-0MVPE2DA7007WF84). Any of the surviving mechanisms above is then caught,
named and attributed, which is strictly more robust than removing one of them.

## 6. The orphaned worktree shells and the missing `.git/worktrees`

Observed 2026-10-26 (planning session and re-verified this session):

- `ContextHub/.worklog/worktrees/` holds 26 directories; 24 have **no `.git`
  file**, so they are no longer registered worktrees. Two carry marker files
  instead of content: `.keep` (mtime `2026-10-26 13:36:25`, i.e. *after* the
  incident) and `.placeholder-removed-worktree` (mtime `2026-10-24`).
- `.git/worktrees/` does exist now, containing only two entries created at
  `16:22` and `16:28` — after the incident. At planning time the directory was
  absent.

Interpretation: the worktree registry was removed during the incident window
and later recreated by ordinary `git worktree add` calls. Removing the registry
directory without removing the worktree directories (and leaving `.keep` /
`.placeholder-removed-worktree` markers behind) matches an **interrupted or
repeatedly retried `git worktree` operation**, not a normal
`git worktree remove` (which deletes the directory). No code path in
ContextHub or SorraAgents writes `.keep`, `.placeholder-removed-worktree`, or
`rm -rf .git/worktrees` — both marker names are unknown to both repositories,
so the writer was an ad-hoc shell command or an earlier (now-removed) cleanup
script.

This remains **unexplained beyond the mechanism class** (a recursive removal
anchored on the wrong root, or a registry delete racing a worktree add) and is
recorded as an open residual. It is harmless: `git worktree prune` (already
run by `implement.py` after every removal) drops stale registrations, and the
empty directories are cleaned by `scripts/hygiene_check.sh` reporting plus a
manual `rmdir`. No child of WL-0MVPADZ1Z000FOSG depends on resolving it.

## 7. Fix targets

| Finding | Child |
|---|---|
| Guard blind to the main checkout (root cause of silence) | WL-0MVPE2BY9001CR9O (guard) + WL-0MVPE2B8E005O0OS (regression tests) |
| No loud abort when the main checkout loses tracked files | WL-0MVPE2CMW006K25Q |
| Two finishes can operate on one checkout concurrently | WL-0MVPE2DA7007WF84 |
| `wl` fails with a stack trace when `packages/shared/dist` is missing | WL-0MVPE2DXB00978T5 |
| Invariant + recovery procedure documentation | WL-0MVPE2EKQ001S863 |

## 8. Reproduction assets

The blind-spot reproduction in §3 needs no fixture: it is 20 lines against
`git_sandbox` and is promoted into the permanent regression suite by
WL-0MVPE2B8E005O0OS (`skill/shared/tests/test_git_sandbox.py`,
`skill/test/tests/test_guarded_run_all.py`). The symlink experiments in §4.4
are recorded here because they were run once, by hand, and are not worth a
permanent test.
