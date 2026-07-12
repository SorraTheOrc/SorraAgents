# Release Process: dev → main

This document describes the release checklist and merge workflow for promoting
tested changes from the `dev` integration branch to `main`.

## Roles

### Release Manager

The **Release Manager** is the person or role authorised to perform the
`dev` → `main` merge. The Release Manager:

- Reviews CI results and confirms all required checks are green.
- Executes the merge script or performs the merge manually.
- Records the merge in the worklog with approval metadata.

Currently, the Release Manager role is held by the **project maintainer /
Producer**. Delegation of this role must be approved by the Producer and
recorded in this document.

## Branch Model

| Branch / Worktree | Purpose                                                                  |
|-------------------|--------------------------------------------------------------------------|
| `main`            | Always releasable; production-ready code.                                |
| `dev`             | Integration branch; agents push completed work here (from worktrees).    |
| `wl-<id>-<desc>`  | Feature branches (one per work item) inside a worktree.                  |

Agents work in worktrees with feature branches (see the [[concepts/git-worktree-best-practices-for-agent-workflows]] wiki page for the canonical workflow), push to `dev`, and the Release Manager promotes `dev` → `main` after review.

The release/ship process operates from the **main checkout** (not a worktree).
Implementation agents create worktrees for feature work; after pushing to dev,
they clean up the worktree and return to the main checkout.

## Overview

- Agents work in worktrees with feature branches and push completed work to `dev` as the integration step. See the [[concepts/git-worktree-best-practices-for-agent-workflows]] wiki page for the canonical worktree workflow.
- CI validates `dev` on every change.
- A human reviewer inspects CI results and triggers the merge from `dev` → `main`.
- `main` must always be releasable.

## Pre-merge Checklist

Before merging `dev` into `main`, the Release Manager **must** verify:

1. **CI — `dev-full-suite` is green**
   - The `dev-full-suite` GitHub Actions workflow must have completed
     successfully on the current `dev` HEAD.
   - Check the [Actions tab](https://github.com/SorraAgents/actions) for the
     latest `dev-full-suite` run.
   - Confirm the `full-suite` job shows a green checkmark.

2. **CI — `dev-smoke` is green**
   - The `dev-smoke` workflow (smoke + critical tests) must also
     be green on `dev`.

3. **Test suite results**
   - Smoke tests have passed.
   - Critical tests have passed.
   - The full test suite has passed (run locally or via CI if not already run on `dev`).

4. **No open merge conflicts**
   - Ensure `dev` has no unresolved conflicts with `main`.
   - Run `git diff main...dev --name-only` to inspect divergent files.

5. **Review outstanding worklog items**
   - Run `wl list --status open --priority high --json` to check for any
     critical or high-priority items that may block the release.

6. **No open blockers**
   - All blocking work-items related to the release are closed.
   - No unresolved merge conflicts exist on `dev`.

7. **Audit readiness gate — all `in_review` and `completed` items have passing audits**
   - The automated release script (`run-release.js`) enforces this gate at exit code 6.
   - Run the gate manually to check:
     ```bash
     node skill/ship/scripts/run-release.js --dry-run
     ```
   - If the gate fails, run `wl audit-show <blocking-item-id> --json` to inspect
     the audit status, then re-run the audit with:
     ```bash
     python3 skill/audit/scripts/audit_runner.py issue <blocking-item-id>
     ```
   - Use `--skip-checks` to bypass the gate in exceptional circumstances.

8. **Verify CHANGELOG.md is up to date**
   - The release script now generates `CHANGELOG.md` automatically from
     worklog items (completed / in_review) during the release flow.
   - Verify that the generated `CHANGELOG.md` section reflects the correct
     release version and contains all expected entries.

## CI Jobs

The CI pipeline for `dev` is expected to run:

- **Smoke tests**: Quick sanity checks that core functionality works.
- **Critical tests**: Tests for high-priority features and known failure points.
- **Full test suite**: Run before the `dev` → `main` merge to catch regressions.

See [Release Tests](./release-tests.md) for commands to run these locally.

## Merge Procedure

### Option A — Automated merge script (recommended)

Run the merge script from the repository root:

```bash
bash scripts/release/merge-dev-to-main.sh
```

The script will:

1. Verify the `dev-full-suite` CI job is green (via GitHub Actions API). This
   is a **hard gate** — the script will abort if CI is not green. Use
   `--force` to bypass (only in exceptional circumstances).
2. Fetch the latest `dev` and `main` from origin.
3. **Automatically increment** the version in `package.json` (default: patch
   bump; use `--bump minor` or `--bump major` to override) and commit it.
4. **Generate `CHANGELOG.md`** by querying worklog for completed / in_review
   work items, categorising by issue_type, and prepending a new release
   section. The updated file is committed.
5. Merge `dev` into the release branch (`dev` → `main`).
6. Create an **annotated git tag** `v<new-version>` on the merge commit.
7. Push the merge branch and the tag to `origin`.
8. Create a **GitHub Pull Request** from the temp branch to `main`.
9. Wait for required status checks to pass on the PR.
10. Merge the PR using `gh pr merge --merge --delete-branch`.
11. Record an audit comment in the worklog with the merge commit hash,
    CI run IDs, PR number, and approver identity.

Example with custom bump:

```bash
bash scripts/release/merge-dev-to-main.sh --bump minor
```

The PR-based approach works with **server-side branch protection** on `main`
that requires pull requests or status checks. The script uses the `gh` CLI to
create and merge the PR, so branch protection rules are satisfied.

### Option B — Manual merge (without branch protection)

If `main` does not have branch protection and you prefer a direct merge:

```bash
# Fetch latest
git fetch origin

# Switch to main
git checkout main
git pull origin main

# Merge dev
git merge origin/dev --no-ff -m "Release: merge dev into main (manual)"

# Push main
git push origin main
```

Then manually record the audit in the worklog with the merge commit hash
and CI run details.

### Option C — Manual PR (for review)

If you want human review of the merge before it lands:

```bash
# Create a temp branch with the merge result
git fetch origin
git checkout origin/main -b release/dev-to-manual-$(date +%Y%m%d%H%M%S)
git merge origin/dev --no-ff -m "Release: merge dev into main"
git push origin HEAD

# Create the PR manually
gh pr create --base main --head "$(git rev-parse --abbrev-ref HEAD)" --title "Release: merge dev into main"
```

## Post-merge Steps

1. Verify `main` is green — confirm the `ci` workflow passes on the merge
   commit.
2. **Work items are automatically closed** — After a successful release via
   `run-release.js`, all work items that passed the audit readiness gate
   (items in `in_review` stage or `completed` status, excluding `stage: done`)
   are closed with the reason `"Shipped in v<version>"`. This is a
   non-blocking step — individual close failures are logged as warnings but
   do not affect the release outcome.
3. Version numbering, tagging, and tag pushing are **now automated** as part
   of the merge script (`merge-dev-to-main.sh`). Before merging, the script:
   - Increments the version in `package.json` (default: patch bump).
   - Commits the version change.
   - Creates an annotated git tag `v<new-version>` on the merge commit.
   - Pushes the tag to `origin`.
4. Customize the bump type with the `--bump` flag:

   ```bash
   bash scripts/release/merge-dev-to-main.sh --bump minor
   ```

5. Update any downstream consumers or deployment targets.

> **Note:** If you need to see the current version, run:
>
> ```bash
> node -p "require('./package.json').version"
> ```
>
> The initial version is set to `0.1.0`.

### Version Bump Types

| Flag | Effect | Example |
|------|--------|---------|
| `--bump patch` (default) | Increments patch version | `0.1.0` → `0.1.1` |
| `--bump minor` | Increments minor version, resets patch | `0.1.0` → `0.2.0` |
| `--bump major` | Increments major version, resets minor and patch | `0.1.0` → `1.0.0` |

### Tag Collision Handling

If a git tag already exists for the computed version (e.g., from a prior
manual tag), the script will fail when attempting to create the tag. In that
case:

1. Check existing tags: `git tag -l 'v*'`
2. Decide whether to bump the version manually (e.g., `--bump minor` instead
   of patch) or delete the conflicting tag.
3. Re-run the release script.

## Rollback

If a release introduces a critical issue:

1. Revert the merge commit on `main`:

   ```sh
   git checkout main
   git revert -m 1 <merge-commit-hash>
   git push origin main
   ```

2. Create a bug work-item documenting the issue.
3. Fix the issue on a feature branch, push to `dev`, and follow the release process again.

## Troubleshooting

### `dev-full-suite` is red

- The merge script enforces a **hard gate** and will abort if CI is not green.
- Identify the failing tests from the CI artifacts.
- Create a work item for the failure using the triage skill.
- Notify the operator / Producer.
- If you must proceed despite red CI (exceptional circumstances), use the
  `--force` flag. This bypasses the gate and records the override in the
  audit log with a warning.

### Merge conflicts between `dev` and `main`

- Resolve conflicts manually on the feature branch.
- Record conflict details and resolution steps in a comment on the owning work item.
- Push the resolved branch to `dev` after review.
- Re-run the pre-merge checklist.

### Script fails with authentication errors

- Ensure the `GH_TOKEN` environment variable is set with appropriate
  permissions (repo access, Actions read).
- Or log in via `gh auth login` before running the script.

## Audit Trail

Every merge must be recorded in the worklog with:

- The merge commit hash.
- The CI run IDs for `dev-full-suite` and `dev-smoke`.
- The identity of the Release Manager who approved the merge.
- A brief summary of what was released.

The merge script automatically records this information. For manual merges,
the Release Manager is responsible for adding the audit comment.

### Override Auditing

When the `--force` flag is used to bypass the CI gate, the script emits a
warning in the audit log indicating that the gate was bypassed. This provides
a clear audit trail for any merges that did not have green CI.
