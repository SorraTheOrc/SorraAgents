# Worklog sync (`wl sync`) behavior notes

This document covers `wl sync` behavior that skill scripts and agents should
know, especially failure modes on unusual repositories.

## Worklog-dir resolution for skill scripts

Skill scripts (intake, find-related, effort-and-risk, audit, implement) shell
out to the `wl` CLI. The `wl` CLI resolves `.worklog` relative to the caller's
cwd, so shared skill helpers inject `--worklog-dir` with this precedence
(implemented in `skill/shared/status_lifecycle.py`):

1. **Explicit `--worklog-dir` value** (from a CLI flag / caller)
2. **Prefix-to-sibling scan** — the work-item id prefix (e.g. `OSL`) is matched
   against `SIBLING_SCAN_ROOT/*/.worklog/config.yaml`, so a non-SorraAgents
   item resolves to its own project's store even when the harness cwd is the
   framework repo
3. **cwd chain** — `<cwd>/.worklog`, git root, nearest initialized ancestor
4. **No flag** — `wl` resolves from cwd (failures surface real error detail)

This means skill scripts resolve the correct worklog store regardless of which
directory they are invoked from.

## `wl sync` on a repo with no commits (unborn HEAD)

`wl sync` pushes worklog data through a temporary git worktree created from
`HEAD`. A repository with **no commits yet** has an unborn `HEAD`, so git
cannot create the worktree and `wl sync` fails with a cryptic git error.

**Failure mode (documented behavior):** `wl sync` (with push) fails with an
actionable message naming the cause and remedy:

```
✗ Sync failed: Cannot sync: this repository has no commits yet, so git cannot
create a temporary worktree from HEAD. Create an initial commit first
(e.g. `git commit --allow-empty -m "chore: initial"`), or run
`wl sync --no-push` to keep worklog data local.
```

**Remedies:**

- Create an initial commit, e.g. `git commit --allow-empty -m "chore: initial"`,
  then run `wl sync` again.
- Or run `wl sync --no-push` to keep worklog data local (SQLite) without
  pushing to git. Work items remain persisted locally; sync to git can be
  done later once the repo has commits.

Normal sync behavior on repos with commits is unchanged.

### Implementation notes

- The guard lives in `withTempWorktree` in the worklog package
  (`src/sync.ts` / the compiled `dist/sync.js` runtime artifact), which
  translates the unborn-HEAD `git worktree add` failure into the actionable
  message above. It only fires when the worktree would be built from local
  `HEAD` (no remote worklog ref to build from).
- **Fragility on npm upgrades:** the patch is applied directly to the
  installed runtime artifact (`dist/sync.js`) with a parity edit to the
  source (`src/sync.ts`). A future `npm install -g worklog` upgrade will
  overwrite `dist/sync.js` and silently drop the guard. After upgrading the
  worklog package, re-apply the patch (or verify the source change is
  shipped upstream). Regression coverage: `tests/test_wl_sync_no_commits.py`.
