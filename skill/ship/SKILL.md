---
name: ship
description: "Automated dev-to-main release execution via the Ship subagent. Trigger on queries like: 'release', 'merge dev to main', 'ship release', 'promote dev'."
---

# Ship Skill — Automated Release Execution

Triggers

- "release"
- "merge dev to main"
- "ship it"
- "promote dev"
- "release the changes"

## Purpose

The Ship skill encapsulates the automated dev-to-main release process. When
invoked, the **Ship subagent** (defined in [`agent/ship.md`](../agent/ship.md))
acts as the primary release executor: it runs the release merge script,
verifies CI status, creates the merge PR, and records audit metadata.

For repositories where the Ship subagent is not configured, the **human
Release Manager** serves as the fallback, following the manual procedures
documented in [`docs/dev/release-process.md`](../docs/dev/release-process.md).

## Required tools

- `gh` (GitHub CLI) — required for PR creation and CI status checks
- `wl` (Worklog CLI) — required for audit logging
- `git` — required for merge operations
- `jq` — required for JSON parsing in the release script

Scripts

- `scripts/release/merge-dev-to-main.sh` — the canonical release merge
  script that automates the dev → main promotion.
- `docs/dev/release-process.md` — the human-readable release checklist
  and merge procedure documentation.

## Subagent configuration

The Ship subagent that executes this skill is configured in
[`agent/ship.md`](../agent/ship.md). Before invoking this skill, ensure the
Ship subagent is properly configured:

- The agent YAML frontmatter sets `mode: subagent` and grants necessary
  tool permissions (`bash`, `write`, `edit`).
- The subagent has access to the `scripts/release/merge-dev-to-main.sh`
  script and the `gh`, `wl`, and `jq` CLIs.

## Release Process

### Primary executor: Ship subagent

The Ship subagent executes the release using the canonical merge script:

```bash
bash scripts/release/merge-dev-to-main.sh
```

The script performs the following steps:

1. **Pre-flight checks**: Verifies `gh` authentication, `wl` availability,
   and a clean working tree.
2. **CI verification**: Checks that the `dev-full-suite` workflow is green
   on the `dev` branch via the GitHub Actions API. This is a **hard gate**
   — the script aborts if CI is not green (use `--force` to bypass in
   exceptional circumstances).
3. **Merge commit creation**: Fetches latest `dev` and `main`, creates a
   merge commit locally (`dev` → `main` with `--no-ff`).
4. **PR creation**: Pushes the merge commit to a temporary
   `release/dev-to-main-<timestamp>` branch and creates a GitHub Pull
   Request targeting `main`.
5. **Status check wait**: Waits for required status checks to pass on the PR
   (configurable timeout, default 10 minutes).
6. **PR merge**: Merges the PR using `gh pr merge --merge --delete-branch`,
   which satisfies server-side branch protection on `main`.
7. **Audit logging**: Records the merge commit hash, CI run IDs, PR URL,
   and approver identity in the worklog.

### Fallback: Human Release Manager

For repositories where the Ship subagent is not configured or the automated
merge is not suitable, a **human Release Manager** can perform the release
manually. The Release Manager must follow the checklist and procedures in
[`docs/dev/release-process.md`](../docs/dev/release-process.md).

The human fallback supports three approaches:

| Approach | Description | When to use |
|----------|-------------|-------------|
| **Automated script** | Run `bash scripts/release/merge-dev-to-main.sh` manually | Ship subagent unavailable but script is available |
| **Direct merge** | `git checkout main && git merge origin/dev --no-ff` | No branch protection on main |
| **Manual PR** | Create a temp branch with merge result and open a PR | Want human review before merge |

### Pre-merge checklist

Whether using the Ship subagent or the human fallback, the following must be
verified before merging `dev` into `main`:

1. **CI — `dev-full-suite` is green** on the current `dev` HEAD.
2. **CI — `dev-smoke` is green** on the current `dev` HEAD.
3. **No open merge conflicts** between `dev` and `main`.
4. **No open critical work items** that would block the release.
5. **Changelog / release notes** are updated for user-facing changes.

See [`docs/dev/release-tests.md`](../docs/dev/release-tests.md) for the
test commands to run locally.

## Preferred execution behaviour (policy)

- When the Ship subagent is available, it MUST serve as the primary release
  executor. The agent should invoke `scripts/release/merge-dev-to-main.sh`
  to perform the dev → main merge.
- The agent MUST NOT perform the merge by substituting its own ad-hoc git
  commands for the canonical script during normal operation.
- The agent may fall back to manual git commands ONLY in narrowly defined
  edge cases:
  - the merge script is missing or not executable,
  - the script fails with an unexpected error and the operator explicitly
    instructs the agent to fall back,
  - the Ship subagent is not configured and a human has explicitly requested
    manual steps.
- If the Ship subagent is not configured, the agent MUST refuse to perform
  the release automatically and instead direct the operator to the human
  Release Manager fallback procedures in `docs/dev/release-process.md`.

## Preconditions & safety

- Never force-push or rewrite history on `main` or `dev`.
- Never bypass the CI-green gate unless explicitly instructed with `--force`.
- Always log the merge audit to the worklog using `wl comment add`.
- The `main` branch is protected: agents must never push directly to `main`.
- All merges must go through a GitHub Pull Request that satisfies branch
  protection rules, or follow the documented manual fallback exactly.

## High-level Steps

1. **Verify Ship subagent availability**
   - Check that `agent/ship.md` is configured with `mode: subagent` and
     the required tool permissions.
   - If the Ship subagent is available, proceed to step 2.
   - If the Ship subagent is not available, direct the operator to the
     human Release Manager fallback (see `docs/dev/release-process.md`).

2. **Run the release merge script**
   - Execute `bash scripts/release/merge-dev-to-main.sh` from the
     repository root.
   - If `--dry-run` mode is requested, pass `--dry-run` to preview the
     actions without making changes.

3. **Monitor and verify**
   - The script will wait for CI status checks to pass on the PR.
   - If checks pass and the PR merges, the release is complete.
   - If checks fail or timeout, the agent reports the failure and leaves
     the PR open for manual review.

4. **Record audit**
   - The merge script automatically records an audit comment in the worklog.
   - Verify the audit was recorded by checking `wl comment list` on the
     associated work item.

5. **Post-release verification**
   - Confirm that `main` is green (the CI workflow passes on the merge
     commit).
   - Optionally tag the release: `git tag -a v<version> -m "Release v<version>"`
     and push the tag.

## Outputs

- A GitHub Pull Request from `release/dev-to-main-<timestamp>` to `main`
  (created and merged by the script).
- An audit comment in the worklog with merge commit hash, CI run IDs,
  PR URL, and approver identity.
- A notification to the operator summarising the merge result.
