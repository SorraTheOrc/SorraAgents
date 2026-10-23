# Worklog sync (`wl sync`) behavior notes

This document covers `wl sync` behavior that skill scripts and agents should
know, especially failure modes on unusual repositories.

## Author-identity gate

`wl sync` refuses to merge incoming worklog-data commits unless every author
on the remote tracking ref (`refs/worklog/remotes/<remote>/worklog/data`,
scanned since `.worklog/last-synced-ref`) passes the author-identity gate. The
gate keeps foreign and incident-era history out of the project store
(SA-0MUJ2VMF7000UHCX).

### What the gate accepts

A commit author is accepted when its email:

- matches the store's configured `user.email` (trimmed, case-insensitive), or
- appears verbatim in the project's `syncAllowedAuthors` allow-list in
  `.worklog/config.yaml`.

An **empty author email is always refused**, even with
`--allow-foreign-author`. The allow-list is deliberately **exact-email** only:
a YAML list of non-empty strings, with no wildcards and no allow-all entry.
Incident-era sentinel identities (`t@t.com` and the other emails in
`scripts/check_git_identity.py::INCIDENT_ERA_EMAILS`) must never be added to
it — regression coverage pins this in `tests/test_author_gate_allowlist.py`.

`pi@local` is allow-listed for this project (SA-0MVGSB63F00803HK): it is the
framework identity used by agent checkouts, so their worklog-data commits
merge instead of stranding every local Worklog mutation (incident
SA-0MVB57M02008QPJS).

### When the gate refuses

The refusal names the remote tracking ref and each offending commit hash and
author email, for example:

```
Refusing to merge worklog data from refs/worklog/remotes/origin/worklog/data: 1 incoming commit(s) fail the author-identity gate.
- 4617ae25a: author email 'pi@local' does not match configured user.email 'you@example.com'
```

Data already merged is not re-gated: the scan starts at
`.worklog/last-synced-ref`, so an offending commit that is an ancestor of that
ref has converged and no longer blocks (as with `4617ae25a` above).

Remediation, in order of preference:

1. **Fix the checkout identity** (ongoing fix). Set the canonical identity the
   store expects, verify it, then re-run `wl sync`:

   ```bash
   git config --local user.email "you@example.com"
   git config --local user.name  "Your Name"
   git config user.email          # verify
   python3 scripts/check_git_identity.py --repo-root .   # pre-flight guard
   wl sync
   ```

2. **Allow-list a legitimate identity** (project-scoped, reviewable fix). If a
   known identity (for example the agent framework's `pi@local`) legitimately
   authors worklog-data commits for this project, add it to
   `syncAllowedAuthors` in `.worklog/config.yaml`, keeping the list
   exact-email. Regression coverage: `tests/test_author_gate_allowlist.py`.

3. **One-time escape hatch** (incident remediation only). Merge a single
   inbound batch authored by an identity that is not — and should not be —
   allow-listed:

   ```bash
   wl sync --allow-foreign-author
   ```

   This bypasses the foreign-email comparison for that run only. It never
   bypasses the empty-email gate and it does not modify the allow-list.

`scripts/check_git_identity.py` runs before `wl sync` in the pre-push hook and
from `scripts/install_pi.sh`, so a mismatched local identity surfaces as an
actionable error instead of a cryptic refusal (SA-0MUJ2VMF7000UHCX). For
install/sync setup see [`scripts/install_pi.sh`](../../scripts/install_pi.sh)
and §9.4 step 4 of [`docs/dev/test-isolation.md`](test-isolation.md).

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

## Post-pull sync hooks (central install)

Every framework project commits three git hooks — `.githooks/post-checkout`,
`post-merge`, `post-rewrite` (all `worklog:post-pull-hook:v1`) — that delegate
`wl sync` to one **per-machine** script:

```sh
exec "$HOME/.config/pi/.githooks/worklog-post-pull" "$@"
```

`scripts/install_pi.sh` installs that central script (an executable symlink to
the repo's committed `.githooks/worklog-post-pull`) as an idempotent,
`PI_SKIP_HOOK_INSTALL=1`-gated step, so a fresh machine's checkouts/merges do
not error with `exec: ... not found` and the post-pull `wl sync` runs
(SA-0MU8CRX1X008IORP). The central script skips sync when
`WORKLOG_SKIP_POST_PULL=1` and is fail-open when `wl` is not installed.

> **Portability caveat (SA-0MVK1Q30O0067NXX):** the committed wrappers
> hard-code the delegating path. `install_pi.sh` warns (fail-open) when the
> current project's `.githooks/post-checkout` does not reference the central
> path it just installed — on machines whose `$HOME` differs, the wrappers
> must be made `$HOME`-relative before checkout/merge stops erroring.

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

## Held claims survive background syncs

Background syncs (`wl sync --if-idle` from herdr's auto-sync timer, auto-sync
spawns, `skill/shared/worklog_sync.py`) must never undo a claim. The invariant
is *actively worked ⇒ `status: in-progress`* (SA-0MTFTFUIH000UWM9); before
SA-0MVE34D9B005TUYJ a sync could write a pre-claim snapshot back over a held
claim — four times in one implement run (SA-0MVDQF3VW002PKCA) — forcing the
agent to re-claim between tool calls.

**What a sync now guarantees** (ContextHub `dev`, `src/claim-hold.ts`):

- An item that is `in-progress` — or carries a non-empty `assignee` while not
  `completed`/`deleted` — and was active within `syncClaimHoldTtlHours`
  (default 24 h; `0` disables) keeps its claimed `status`/`stage` pair when a
  sync pulls a stale row for it.
- The claim's `updatedAt`/`activityAt` are preserved: the losing stale row is
  not re-stamped as the newest row by `import()` / `upsertItems()` (that
  re-stamp is what made a reverted claim stick).
- Newer, *more active* transitions still win (`→ completed`, `→ in_review`, a
  close, an audit demotion of a completed item, a reassignment), attributed
  deletes still propagate, and a claim older than the TTL is released.
- Every refusal is recorded in the sync log (`~/.worklog/logs/sync.log`) and in
  `wl --json sync` output as `sync.claimHolds[]`.

**What agents should do:** nothing beyond the existing claim discipline —
`wl update <id> --status in_progress --assignee <agent>` when starting work.
If a claim *does* appear to revert, `wl --json sync` now names the store it
operated on (`sync.store`) and every refused downgrade, so the next recurrence
is diagnosable without source archaeology.

## One store per repository (worktrees)

`.worklog/config.yaml` is tracked, so every implement/audit worktree under
`.worklog/worktrees/<name>` checks out a `.worklog/` directory that holds
configuration but **no store**. Two stores for one project prefix do not
serialise — the mutation lock (`worklog-data.jsonl.lock`) and the
`last-synced-ref` watermark are both derived from the data path — so they hold
different locks and watermarks while pushing to the same worklog ref.

`wl` therefore binds a command run inside a managed worktree to the **main
checkout's** store:

- a worktree `.worklog/` is used only when it is a genuine store (`wl init`
  wrote an `initialized` marker and/or `worklog.db`);
- otherwise the command binds to the main checkout's `.worklog/`;
- if neither exists the command fails with an actionable error naming
  `--worklog-dir` instead of silently creating a second store.

`wl --json sync` reports `sync.store.secondStore: true` (plus a warning in the
sync log) when it detects a second store, so the condition is visible rather
than silent.

**Agent rule (unchanged, now enforced):** when running `wl` from inside a
worktree, pass `--worklog-dir <main-checkout>/.worklog`
(`skill/shared/status_lifecycle.py` already resolves and injects it). See
[docs/dev/worktree-isolation.md](worktree-isolation.md).
