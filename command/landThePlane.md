---
description: Finish a work session safely ("land the plane")
tags:
  - workflow
  - completion
  - git
agent: ship
permission:
  bash:
    "git*": allow
    "bd*": allow
---

You are guiding the end-of-session wrap-up so that work is fully synced, issues are updated, and no changes are stranded locally.

## Argument parsing

- Pattern: If the raw input begins with a slash-command token (a leading token that starts with `/`, e.g., `/landThePlane`), strip that token first.
- This command expects no arguments; `$ARGUMENTS` may be empty. If the user provides unexpected arguments, ask whether they intended to pass a specific bead id or option, and suggest running the command without arguments if none are required.

## Preconditions

- You know the active branch and whether an origin remote is configured.
- If there are uncommitted changes, decide whether to commit, stash, or finish before proceeding.

## Mandatory workflow

1. **File issues for remaining work** — create beads for any follow-ups or known gaps.
2. **Run quality gates** — execute relevant tests/linters/builds if code changed.
3. **Update issue status** — close finished items; refresh in-progress beads.
4. **Push to remote** — required once a remote exists:
   ```bash
   git pull --rebase
   bd sync
   git push
   git status  # MUST show "up to date with origin"
   ```
5. **Clean up** — clear stashes and prune remote branches if used.
6. **Verify** — ensure all changes are committed and pushed.
7. **Hand off** — leave concise context for the next session.

## Critical rules

- Work is NOT complete until changes are pushed to the canonical remote.
- If no remote is configured, configure it first, then push.
- Do not stop before pushing; retry until `git push` succeeds.
- Do not say "ready to push when you are" — you must push.
- If push fails, resolve and retry until it succeeds.

## Suggested command usage

Run `/landThePlane` when wrapping up a session to ensure these steps are executed without omission.
