# Release Process

This document describes the gated release process for promoting changes from `dev` to `main`.

## Overview

- Agents work in feature branches and push completed work to `dev` as the integration step.
- CI validates `dev` on every change.
- A human reviewer inspects CI results and triggers the merge from `dev` → `main`.
- `main` must always be releasable.

## Release Checklist

Before triggering a `dev` → `main` merge, the reviewer must verify:

1. **CI is green on `dev`**
   - All CI jobs for the `dev` branch have passed.
   - No flaky or intermittent failures are present.
   - If a failure exists, investigate before proceeding (see [Release Tests](./release-tests.md)).

2. **Test suite results**
   - Smoke tests have passed.
   - Critical tests have passed.
   - The full test suite has passed (run locally or via CI if not already run on `dev`).

3. **No open blockers**
   - All blocking work-items related to the release are closed.
   - No unresolved merge conflicts exist on `dev`.

4. **Review completed PRs**
   - All PRs merged into `dev` since the last release have been reviewed.
   - Any post-merge review comments on PRs have been addressed.

5. **Version bump**
   - If applicable, update the version number in `package.json` and commit to `dev` before triggering the release.

## Triggering the Merge

Once the checklist above is satisfied:

1. Ensure you are on `main` and it is up to date:
   ```sh
   git checkout main
   git pull origin main
   ```

2. Merge `dev` into `main`:
   ```sh
   git merge dev
   ```

3. Resolve any conflicts (should be rare if `dev` is well-maintained).

4. Push `main`:
   ```sh
   git push origin main
   ```

5. Tag the release (if applicable):
   ```sh
   git tag -a v1.2.3 -m "Release v1.2.3"
   git push origin v1.2.3
   ```

## CI Jobs

The CI pipeline for `dev` is expected to run:

- **Build**: Verify the project builds without errors.
- **Smoke tests**: Quick sanity checks that core functionality works.
- **Critical tests**: Tests for high-priority features and known failure points.
- **Full test suite**: Run before the `dev` → `main` merge to catch regressions.

See [Release Tests](./release-tests.md) for commands to run these locally.

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
