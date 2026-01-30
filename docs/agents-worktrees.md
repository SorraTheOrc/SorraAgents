# Agent Worktrees — usage and examples

This document explains how to invoke the agent worktree skill, what outputs to expect, and examples for running locally and in CI.

## Purpose

Help developers and CI operators run the agent skill that creates ephemeral worktrees for isolated automation runs. This file explains invocation, common flags, expected outputs, and a short checklist for PR reviewers.

## How to invoke the agent skill

Local (developer):

1. Ensure you have a clean working tree or follow the runbook for conflicts (`docs/runbook-agent-conflicts.md`).
2. From the repository root run the agent helper (example):

```bash
# example: run the `agent` command that creates a worktree for `SA-0ML...`
agent create-worktree --work-item SA-0ML0507OP1JSRKEU --run-tests
```

CI (example):

- CI jobs should call the same `agent` helper or container image with flags tuned for non-interactive use. Example configuration in a CI job:

```yaml
steps:
  - name: Create agent worktree and run tests
    run: agent create-worktree --work-item $WORK_ITEM --ci --run-tests
```

## Expected outputs

- A new git worktree is created under `.worktrees/<work-item-id>` or similar location.
- The tool prints the created branch name, the worktree path, and the commands to reproduce locally.
- On success the agent returns with exit code `0`. On failure it prints troubleshooting hints and a pointer to the runbook.

## Examples

- Local example: `agent create-worktree --work-item SA-0ML0507OP1JSRKEU --open` opens an editor in the new worktree.
- CI example: `agent create-worktree --work-item $WORK_ITEM --ci --run-tests --output-json` writes a small JSON file with metadata for subsequent CI steps.

## Short checklist for PR reviewers (integration harness)

When reviewing PRs that touch agent automation, run this minimal checklist locally to verify integration:

1. Verify you are on `main` and up-to-date: `git fetch origin && git switch main && git pull`.
2. Run the agent creation flow for the work item referenced in the PR: `agent create-worktree --work-item <id> --run-tests`.
3. Confirm the created worktree contains the changes in the PR branch and tests run green.
4. If conflicts occur, follow the runbook (`docs/runbook-agent-conflicts.md`) to gather logs and resolve.

Notes: this checklist is intentionally short — the runbook contains full triage steps.
