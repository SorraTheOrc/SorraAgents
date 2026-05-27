# Pre-Merge Full-Suite Testing

## Overview

Before any merge from `dev` → `main` is performed, the **full test suite**
must pass. This ensures `main` always contains releasable, verified code.

## The `dev-full-suite` CI Job

A dedicated GitHub Actions workflow — `.github/workflows/dev-full-suite.yml` —
runs the project's complete test suite. This job is the gating step for
pre-merge validation.

### How the job is triggered

| Trigger | Description |
|---|---|
| **workflow_dispatch** | Manually via the GitHub Actions UI or `gh workflow run`. Use this for ad-hoc pre-merge checks. |
| **Release-candidate tag** | Pushing a tag matching `rc-*` (e.g. `rc-1.0.0`) triggers the job automatically. |
| **Pull request → main** | Any PR targeting `main` will run the full suite as a status check. |

### How to run it manually

**Via the GitHub Actions UI:**

1. Navigate to **Actions** in the repository.
2. Select the **dev-full-suite** workflow.
3. Click **Run workflow**.
4. Optionally provide a reason (e.g. "pre-merge gate for v1.2").
5. Click **Run workflow**.

**Via the GitHub CLI:**

```bash
gh workflow run dev-full-suite.yml --ref <branch-or-tag>
```

### How the merge is gated on success

1. The release manager creates a release candidate (e.g. pushes an `rc-*` tag
   or opens a PR from `dev` targeting `main`).
2. The `dev-full-suite` workflow runs automatically (or is triggered manually).
3. The release manager reviews the workflow run:
   - **Green** → proceed with the merge to `main`.
   - **Red** → investigate failures, fix them, and re-run the full suite.
4. The merge to `main` should **only** be performed when `dev-full-suite`
   reports success.

### Branch protection (recommended)

To enforce this gate automatically, configure branch protection on `main`:

1. Go to **Settings → Branches** in the repository.
2. Add or edit the rule for `main`.
3. Under **Require status checks to pass before merging**, search for and
   select `Full test suite` (the job name from `dev-full-suite.yml`).
4. Save the rule.

With this enabled, GitHub will block any merge to `main` until the
`dev-full-suite` job completes successfully.

### Re-running a failed job

If the full suite fails:

1. Review the failure logs in the workflow run.
2. Fix the underlying issues on the `dev` branch.
3. Push the fix and re-run the full suite (via any trigger method above).
4. Do **not** merge until the full suite passes.
