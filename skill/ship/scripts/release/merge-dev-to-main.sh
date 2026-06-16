#!/usr/bin/env bash
set -euo pipefail

# merge-dev-to-main.sh
# Canonical release merge script. Intended to be installed under the ship skill
# at <skill-dir>/scripts/release/merge-dev-to-main.sh. The wrapper
# skill/ship/scripts/run-release.js will invoke this script.
#
# Version numbering:
#   Before merging dev into main, this script automatically increments the
#   version in package.json, commits the change, and creates an annotated git
#   tag (v<semver>) on the merge commit. The tag is pushed to origin.

# Resolve the directory where this script resides (used to find sibling scripts)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  cat <<EOF
Usage: $0 [--dry-run] [--force] [--work-item-id <id>] [--bump patch|minor|major]

Options:
  --dry-run       Do not push or create/merge the PR; just show planned actions
  --force         Proceed even if CI checks are not green or other hard gates
  --work-item-id  Worklog item id to record in logs (optional)
  --bump <type>   Version bump type: patch, minor, or major (default: patch).
                  The version in package.json is incremented before the merge.
  -h, --help      Show this help message
EOF
}

DRY_RUN=false
FORCE=false
WORK_ITEM=""
BUMP="patch"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)
      DRY_RUN=true
      shift
      ;;
    --force)
      FORCE=true
      shift
      ;;
    --work-item-id)
      WORK_ITEM="$2"
      shift 2
      ;;
    --bump)
      BUMP="$2"
      if [[ ! "$BUMP" =~ ^(patch|minor|major)$ ]]; then
        echo "Error: --bump must be one of: patch, minor, major (got: $BUMP)" >&2
        exit 2
      fi
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown arg: $1" >&2
      usage
      exit 2
      ;;
  esac
done

command -v git >/dev/null 2>&1 || { echo "git not found in PATH" >&2; exit 1; }
command -v gh >/dev/null 2>&1 || { echo "gh (GitHub CLI) not found in PATH" >&2; exit 1; }

# Ensure we are in a git repository
if ! git rev-parse --show-toplevel >/dev/null 2>&1; then
  echo "Not in a git repository; this script must be run from a repository root or a worktree." >&2
  exit 1
fi

# Ensure clean workspace unless dry-run
if [[ "$DRY_RUN" != "true" ]]; then
  if [[ -n "$(git status --porcelain)" ]]; then
    echo "Working tree is not clean; aborting." >&2
    exit 1
  fi
fi

# Fetch latest
git fetch origin --prune

# Create a temporary release branch
TIMESTAMP=$(date -u +%Y%m%d%H%M%S)
BRANCH="release/dev-to-main-$TIMESTAMP"

# Create the branch from main (ensure we have latest main)
git fetch origin main:refs/remotes/origin/main || true

# Checkout a new branch from origin/main
git checkout -b "$BRANCH" origin/main

# ── Version bump (only on actual release, not dry-run) ──────────────
if [[ "$DRY_RUN" != "true" ]]; then
  BUMPS_SCRIPT="${SCRIPT_DIR}/bump-version.js"
  if [[ -f "$BUMPS_SCRIPT" ]]; then
    NEW_VERSION=$(node "$BUMPS_SCRIPT" --bump "$BUMP" 2>/dev/null) || {
      echo "Version bump failed. Check that package.json has a valid version field and node is available." >&2
      exit 1
    }
    git add package.json
    git commit -m "Bump version to v${NEW_VERSION}"
    echo "Version bumped to v${NEW_VERSION}"
  else
    echo "Warning: bump-version.js not found at $BUMPS_SCRIPT; skipping version bump." >&2
  fi
elif [[ "$DRY_RUN" == "true" ]]; then
  echo "Dry-run: version bump would be applied with --bump $BUMP"
fi

# Merge origin/dev into the release branch
if git merge --no-ff origin/dev -m "Merge origin/dev into main (automated)"; then
  echo "Created merge commit on $BRANCH"
else
  echo "Merge failed; please resolve conflicts manually." >&2
  exit 1
fi

# ── Create git tag (only on actual release) ───────────────────────
if [[ "$DRY_RUN" != "true" && -n "${NEW_VERSION:-}" ]]; then
  git tag -a "v${NEW_VERSION}" -m "Release v${NEW_VERSION}"
  echo "Created annotated tag v${NEW_VERSION} on merge commit"
elif [[ "$DRY_RUN" == "true" ]]; then
  echo "Dry-run: tag v<version> would be created on the merge commit"
fi

if [[ "$DRY_RUN" == "true" ]]; then
  echo "Dry-run: created merge commit on branch $BRANCH. No push or PR will be created."
  exit 0
fi

# Push the release branch and tag
git push origin HEAD
if [[ -n "${NEW_VERSION:-}" ]]; then
  git push origin "v${NEW_VERSION}"
fi

# Create a PR
PR_TITLE="Merge dev → main (automated)"
PR_BODY="Automated release created by ship skill."
PR_URL=""

# Create PR and capture output
PR_OUT=$(gh pr create --title "$PR_TITLE" --body "$PR_BODY" --base main --head "$BRANCH" 2>&1) || {
  echo "Failed to create PR: $PR_OUT" >&2
  exit 1
}

# Extract PR URL from gh output
PR_URL=$(printf '%s' "$PR_OUT" | grep -Eo 'https?://[^[:space:]]+/pull/[0-9]+' | head -n1)
if [[ -z "$PR_URL" ]]; then
  PR_URL=$(printf '%s' "$PR_OUT" | grep -Eo 'https?://[^[:space:]]+' | head -n1)
fi

if [[ -z "$PR_URL" ]]; then
  echo "Failed to obtain PR URL (gh output below):" >&2
  printf '%s
' "$PR_OUT" >&2
  exit 1
fi

echo "PR created: $PR_URL"

# Optionally wait for status checks or merge immediately
# Attempt to merge (this will obey GitHub protections and fail if checks are required)
if [[ "$FORCE" == "true" ]]; then
  echo "Force flag provided: attempting to merge PR immediately"
  gh pr merge "$PR_URL" --merge --delete-branch || {
    echo "Failed to merge PR" >&2
    exit 1
  }
  echo "PR merged: $PR_URL"
else
  echo "Release prepared. The PR $PR_URL should be merged once CI checks pass and reviewers approve."
fi

# Audit logging could be added here (e.g., call wl comment add ...)

exit 0
