# Worklog Cross-Project Protection

This document is the SorraAgents-side operational guide for protecting this
project's worklog from cross-project contamination (foreign `WL-`, `OB-`, etc.
work items). The canonical engineering documentation lives in the ContextHub
repo (`wl` CLI source):

- `docs/CROSS_PROJECT_POLLUTION_CLEANUP.md` — cleanup tooling
  (`wl doctor foreign-items`), the sync prefix filter, and the daemon restart
  procedure.

## What happened (2026-08-01 / 2026-08-02)

1. **2026-08-01** — the SorraAgents worklog was polluted with 2132 foreign
   items (2131 `WL-` + 1 `OB-`). Cleaned with
   `wl doctor foreign-items --apply --push` (WL-0MSAH2A71000MUA3).
2. **2026-08-02** — contamination returned (1607 `WL-` local, 2131 `WL-` + 1
   `OB-` in the remote ref) despite the repo-context guard
   (`assertDataFileInCwdRepo`, WL-0MSAH26DD001XXST) being deployed. Root cause:
   a long-running process loaded **pre-fix code** (orphaned test-harness
   processes running a deleted worktree's `src/cli.ts`, and herdr daemon/panes
   started before the guard rebuild) and pushed a stale pre-cleanup snapshot.

## Guard rails

- **Repo-context guard** (`assertDataFileInCwdRepo`) — `wl sync
  --worklog-dir <this>/.worklog` run from inside a different git repo fails
  loudly before any merge/push.
- **Sync prefix filter** (SA-0MSC0BM1V0032UYT) — `wl sync` never imports work
  items whose ID prefix does not match this project's prefix (`SA`), dropping
  their comments/edges/audits too. Defense-in-depth against stale/daemon
  processes that bypass the repo-context guard.
- **`wl doctor foreign-items --apply --push`** — removes foreign items from
  the local DB and rewrites the remote `refs/worklog/data` with own items only.

## Re-cleanup procedure (if contamination recurs)

```bash
# From the repo root of each project (ContextHub, SorraAgents,
# Tableau-Card-Engine, open_source_llm):
wl doctor foreign-items --dry-run        # confirm foreign count
wl doctor foreign-items --apply --push   # clean DB + rewrite remote ref
# Verify:
wl doctor foreign-items --dry-run        # expect 0 foreign
wl sync --dry-run --no-push --json       # expect itemsAdded=0
git ls-remote origin refs/worklog/data   # ref contains only own-prefix items
```

## Daemon / long-running process restart

The installed `wl` is a symlink to the live ContextHub repo
(`/usr/local/lib/node_modules/worklog` → `~/projects/ContextHub`), so
rebuilding ContextHub's `dist/` changes the installed CLI — but **only for new
processes**. After a `wl` upgrade, restart or terminate long-running
`wl`-spawning processes:

1. Orphaned test-harness processes (`tsx <worktree>/src/cli.ts`, PPID 1) —
   terminate them; they never exit on their own and can run pre-fix code.
2. `herdr server` (+ its panes) — restart so panes load current
   `worklog-selection-list` plugin code. ⚠ Restarting the server terminates
   every pane it hosts, including active pi/agent sessions — coordinate with
   operators first.
3. pi TUI Worklog extension — no restart needed for CLI-side fixes (it spawns
   the installed `wl` per invocation).
