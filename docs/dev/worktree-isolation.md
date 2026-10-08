# Worktree isolation for implementation work

All implementation work MUST happen inside the git worktree created by
`implement.py start <WIP-id>`. This document explains **why**, the one
non-obvious split agents hit (**`wl` needs the main checkout, edits need the
worktree**), and how to prove you are really inside the worktree — because
`cwd` alone does not confine an agent.

Related: [implement skill](../../skill/implement/SKILL.md),
[implement skill reference](implement-skill-reference.md),
[test isolation](test-isolation.md). Origin: incident in repo
`tce-main-street` (epic `MS-0MUXW90YE009L4DQ`, child
`MS-0MUXWGT7G009THBO`) and work item `SA-0MUY9PSRS003V5CM`.

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

## The placement guard

`phase_parent`/`phase_drive` fail closed when a **non-terminal** child's
worktree is clean at the parent-HEAD (`dev`) while the main checkout holds
uncommitted changes outside `.worklog/` — evidence the child wrote to the main
checkout instead of its worktree. The failure names the offending
main-checkout paths and does **not** advance the parent.

The guard is deliberately precise: a legitimate child whose work is already in
its worktree, a clean main checkout, or `.worklog/`-only dirt never trigger
it.

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
