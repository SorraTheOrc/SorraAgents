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

| Branch          | Purpose                                          |
|-----------------|--------------------------------------------------|
| `main`          | Always releasable; production-ready code.        |
| `dev`           | Integration branch; agents push completed work here. |
| `wl-<id>-<desc>`| Feature branches; one per work item.             |

Agents work in feature branches, push to `dev`, and the Release Manager
promotes `dev` → `main` after review.

## Pre-merge Checklist

Before merging `dev` into `main`, the Release Manager **must** verify:

1. **CI — `dev-full-suite` is green**
   - The `dev-full-suite` GitHub Actions workflow must have completed
     successfully on the current `dev` HEAD.
   - Check the [Actions tab](https://github.com/SorraAgents/actions) for the
     latest `dev-full-suite` run.
   - Confirm the `full-suite` job shows a green checkmark.

2. **CI — `ci` workflow is green**
   - The standard `ci` workflow (unit tests + integration tests) must also
     be green on `dev`.

3. **No open merge conflicts**
   - Ensure `dev` has no unresolved conflicts with `main`.
   - Run `git diff main...dev --name-only` to inspect divergent files.

4. **Review outstanding worklog items**
   - Run `wl list --status open --priority high --json` to check for any
     critical or high-priority items that may block the release.

5. **Verify changelog / release notes**
   - Confirm that any user-facing changes have been documented.
   - Use the changelog generator skill if needed.

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
3. Perform a merge commit `dev` → `main` (or fast-forward if possible).
4. Push `main` to origin.
5. Record an audit comment in the worklog with the merge commit hash,
   CI run IDs, and approver identity.

### Option B — Manual merge

If the script is unavailable or fails:

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

## Post-merge Steps

1. Verify `main` is green — confirm the `ci` workflow passes on the merge
   commit.
2. Optionally tag the release: `git tag -a v<version> -m "Release v<version>"`
3. Push the tag: `git push origin v<version>`
4. Update any downstream consumers or deployment targets.

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

- Create a merge-conflict work item via `wl create`.
- Resolve conflicts on a temporary branch.
- Push the resolved branch to `dev` after review.
- Re-run the pre-merge checklist.

### Script fails with authentication errors

- Ensure the `GH_TOKEN` environment variable is set with appropriate
  permissions (repo access, Actions read).
- Or log in via `gh auth login` before running the script.

## Audit Trail

Every merge must be recorded in the worklog with:

- The merge commit hash.
- The CI run IDs for `dev-full-suite` and `ci`.
- The identity of the Release Manager who approved the merge.
- A brief summary of what was released.

The merge script automatically records this information. For manual merges,
the Release Manager is responsible for adding the audit comment.

### Override Auditing

When the `--force` flag is used to bypass the CI gate, the script emits a
warning in the audit log indicating that the gate was bypassed. This provides
a clear audit trail for any merges that did not have green CI.
