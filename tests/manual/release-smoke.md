# Release Smoke Test — Manual Demo Script

This document provides a step-by-step demo script for reviewers to validate
the release merge workflow before approving a `dev` → `main` promotion.

## Purpose

Verify that the release process documentation and merge script work correctly
end-to-end. This is a **dry-run** validation — no actual merge to `main` is
performed during this smoke test.

## Prerequisites

- `gh` CLI installed and authenticated: `gh auth status`
- `wl` CLI installed and configured
- Local repository with `main` and `dev` branches fetched
- Bash shell

## Smoke Test Steps

### Step 1 — Validate the release process document exists

```bash
test -f docs/dev/release-process.md && echo "PASS: release-process.md exists" || echo "FAIL: release-process.md missing"
```

Verify the document contains:
- [ ] Role definition (Release Manager)
- [ ] Pre-merge checklist
- [ ] Merge procedure (automated and manual)
- [ ] Post-merge steps
- [ ] Troubleshooting section
- [ ] Audit trail requirements

### Step 2 — Validate the merge script exists and is executable

```bash
test -x scripts/release/merge-dev-to-main.sh && echo "PASS: merge script exists and is executable" || echo "FAIL: merge script missing or not executable"
```

### Step 3 — Validate the merge script help

```bash
bash scripts/release/merge-dev-to-main.sh --help
```

Expected output: usage information with `--dry-run`, `--force`,
`--work-item-id`, and `--approver` options.

### Step 4 — Run a dry-run merge

From the repository root, on the `main` branch:

```bash
git checkout main
git pull origin main
bash scripts/release/merge-dev-to-main.sh --dry-run
```

Expected behaviour:
- The script runs pre-flight checks (gh auth, wl availability, clean tree).
- It checks CI status for `dev-full-suite` on `dev`.
  - If CI is not green, the script will **abort** (hard gate). In dry-run
    mode it reports what it would have done.
- It shows what the merge diff would look like.
- It prints the audit comment that would be recorded.
- **No changes are made** to any branch.

### Step 5 — Verify CI workflow files

```bash
# Check dev-full-suite workflow exists
test -f .github/workflows/dev-full-suite.yml && echo "PASS: dev-full-suite.yml exists" || echo "FAIL: dev-full-suite.yml missing"

# Check standard CI workflow exists
test -f .github/workflows/ci.yml && echo "PASS: ci.yml exists" || echo "FAIL: ci.yml missing"
```

Verify `dev-full-suite.yml` contains:
- [ ] `workflow_dispatch` trigger (allows manual pre-merge runs)
- [ ] `full-suite` job that runs `pytest`
- [ ] Test results uploaded as artifacts

### Step 6 — Verify branch protection awareness

Review that the merge script **does not** allow pushing directly to `main`
from a feature branch (only the script on the `main` branch itself performs
the merge). Confirm:

```bash
grep -c "protected" agent/ship.md || echo "WARNING: protected branch policy not found in ship.md"
grep -c "main" agent/ship.md | xargs -I{} echo "Found {} references to main in ship.md"
```

### Step 7 — Verify audit trail capability

Run a dry-run with an explicit work item ID to confirm audit logging works:

```bash
bash scripts/release/merge-dev-to-main.sh --dry-run --work-item-id SA-0MPDZE6LZ008WKR3
```

The output should include a structured audit comment template.

## Pass Criteria

All of the following must pass for the release process to be considered
ready for use:

1. `docs/dev/release-process.md` exists and contains all required sections.
2. `scripts/release/merge-dev-to-main.sh` exists, is executable, and runs
   without errors in `--dry-run` mode.
3. `.github/workflows/dev-full-suite.yml` exists and is properly configured.
4. The dry-run merge produces a valid diff summary and audit comment.
5. The script correctly rejects runs from non-`main` branches.

## Reporting Results

Document pass/fail results for each step above and share with the reviewer
or Release Manager before approving the release process for production use.
