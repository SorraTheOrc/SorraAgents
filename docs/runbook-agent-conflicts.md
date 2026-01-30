# Agent runbook — triage and conflicts

This runbook captures common failure modes when the agent creates worktrees and how to collect logs for a GH issue.

## Symptoms

- Agent fails to create a worktree (non-zero exit, `git worktree add` error).
- Tests fail in the created worktree due to missing files or CI environment differences.
- Merge conflicts or index/state issues when creating a branch in the worktree.

## Immediate triage steps

1. Capture the agent command used and its full stdout/stderr.
2. Run `git status --porcelain=v1 -b` in both the invoking workspace and the new worktree.
3. Run `git worktree list --porcelain` and note paths.
4. If the failure was in CI, collect the job logs and attach them to the issue.

## Common fixes

- If `git worktree add` fails due to an existing path, remove the stale path or use a unique path.
- If tests fail due to environment differences, document failing tests and reproduce locally using the same environment (docker image or runner). 

## Logs to include in an issue

- The agent invocation command and flags.
- `git status --porcelain=v1 -b` from the caller workspace and worktree.
- `git log --oneline -n 20` from the worktree.
- The agent's stdout/stderr.
- The output of `git worktree list --porcelain`.

## When to open a GH issue

Open an issue when:

- The agent prints an unhandled error or stack trace.
- Repro steps show the agent failing in CI but not locally.

Attach the logs above, a minimal reproduction (or the CI job id), and the expected vs actual behaviour.
