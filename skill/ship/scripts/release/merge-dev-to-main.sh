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
#
# Derived version files:
#   After the dev merge, when package.json declares a `build` script, the
#   script runs the build and commits regenerated tracked files (e.g.
#   ContextHub src/version.ts) so they match the bumped version (non-fatal).

# Resolve the directory where this script resides (used to find sibling scripts)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  cat <<EOF
Usage: $0 [--dry-run] [--force] [--work-item-id <id>] [--bump patch|minor|major]

Options:
  --dry-run       Do not push or create/merge the PR; just show planned actions
  --force         Merge the PR immediately without waiting for status checks
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

# ── Code Freeze marker (contract: WL-0MSBU4KMA004PKSR) ──────────────────────
# Set a Code Freeze marker for the duration of the release so implement skills
# refuse to start new implementation work. Cleared on every exit path (success,
# failure, abort, dry-run) via the EXIT trap. This is a fallback for when the
# run-release.js wrapper is bypassed; the wrapper sets/clears the same marker.
REPO_ROOT="$(git rev-parse --show-toplevel)"
FREEZE_MARKER="$REPO_ROOT/.worklog/code-freeze.json"
write_code_freeze_marker() {
  mkdir -p "$REPO_ROOT/.worklog"
  printf '{"active": true, "reason": "ship release in progress", "startedAt": "%s", "pid": %s}\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$$" > "$FREEZE_MARKER"
}
clear_code_freeze_marker() {
  rm -f "$FREEZE_MARKER"
}
write_code_freeze_marker
trap clear_code_freeze_marker EXIT

# Ensure clean workspace unless dry-run
if [[ "$DRY_RUN" != "true" ]]; then
  if [[ -n "$(git status --porcelain)" ]]; then
    echo "Working tree is not clean; aborting." >&2
    exit 1
  fi
fi

# ── Worklog-ref gating ─────────────────────────────────────────────────--------
# Detect *dangerous* worklog refs and abort, so worklog data is never merged
# into main. Genuine orphans (no common ancestor with main/dev) would corrupt
# the worklog database for all users.
#
# The rule lives in exactly one place — release/worklog-ref-gate.sh — and is
# identical to the JS gate (check-worklog-refs.js) applied by the run-release.js
# wrapper moments earlier (SA-0MVO7XZAB0095R6F). Both exclude:
#   * remote-tracking mirror refs (refs/worklog/remotes/**), and
#   * any refs/worklog/* ref carrying the worklog data file
#     (.worklog/worklog-data.jsonl) — `wl sync` bookkeeping such as
#     refs/worklog/data, refs/worklog/fetched-check and
#     refs/worklog/tmp-reauthor,
# while still flagging orphan refs. Do NOT re-inline a name-based check here:
# the previous inline version only special-cased refs/worklog/data by name and
# aborted every release once `wl sync` created the other bookkeeping refs.
WORKLOG_REF_GATE="$SCRIPT_DIR/worklog-ref-gate.sh"
if [[ ! -f "$WORKLOG_REF_GATE" ]]; then
  # Fail closed: without the gate the release cannot verify that worklog data
  # refs are safe to ignore. The ship skill ships both scripts side by side, so
  # a missing sibling means a partial install/copy.
  echo "Error: worklog-ref gate not found at '$WORKLOG_REF_GATE'." >&2
  echo "release/worklog-ref-gate.sh ships alongside this script; install (or copy) both." >&2
  exit 1
fi
if ! bash "$WORKLOG_REF_GATE" "$REPO_ROOT"; then  echo "Error: Dangerous worklog refs detected under refs/worklog/. Aborting release to prevent accidental merge of worklog data into main." >&2
  echo "The worklog refs listed above are separate from regular branches and must not be merged into main." >&2
  exit 1
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

# ── Generate CHANGELOG.md (only on actual release) ─────────────────
if [[ "$DRY_RUN" != "true" && -n "${NEW_VERSION:-}" ]]; then
  CHANGELOG_SCRIPT="${SCRIPT_DIR}/generate-changelog.js"
  if [[ -f "$CHANGELOG_SCRIPT" ]]; then
    node "$CHANGELOG_SCRIPT" "$NEW_VERSION" 2>&1 || {
      echo "Warning: CHANGELOG.md generation failed (continuing with release)" >&2
    }
    if [[ -f "$(git rev-parse --show-toplevel)/CHANGELOG.md" ]]; then
      git add "$(git rev-parse --show-toplevel)/CHANGELOG.md"
      git commit -m "Update CHANGELOG.md for v${NEW_VERSION}"
      echo "CHANGELOG.md updated for v${NEW_VERSION}"
    fi
  fi
elif [[ "$DRY_RUN" == "true" ]]; then
  echo "Dry-run: CHANGELOG.md would be generated for v<version>"
fi

# Merge origin/dev into the release branch
if git merge --no-ff origin/dev -m "Merge origin/dev into main (automated)"; then
  echo "Created merge commit on $BRANCH"
else
  # Auto-resolve CHANGELOG.md conflicts: dev's CHANGELOG carries both the
  # previously released history and the unreleased entries for this release
  # (generate-changelog.js rewrites it for the new version afterwards).
  if git ls-files -u | grep -q 'CHANGELOG.md'; then
    echo "Resolving CHANGELOG.md merge conflict (taking dev's version)" >&2
    git checkout --theirs CHANGELOG.md
    git add CHANGELOG.md
  fi
  # Auto-resolve package.json conflicts: keep the bumped release version
  # (the release branch bumped it before the merge; dev may be behind).
  if git ls-files -u | grep -q 'package.json'; then
    echo "Resolving package.json merge conflict (keeping bumped version)" >&2
    git checkout --ours package.json
    git add package.json
  fi
  if git ls-files -u | grep -q .; then
    echo "Merge failed; please resolve conflicts manually." >&2
    git status --short >&2
    exit 1
  fi
  if git merge --continue --no-edit 2>/dev/null || git commit -m "Merge origin/dev into main (automated, CHANGELOG resolved)"; then
    echo "Created merge commit on $BRANCH (with CHANGELOG resolution)"
  else
    echo "Merge failed; please resolve conflicts manually." >&2
    exit 1
  fi
fi

# ── Regenerate derived version files (only on actual release) ───────
# After the dev merge (final tree) and before tagging, run the repo's build
# so auto-generated derived version files (e.g. ContextHub src/version.ts)
# match the bumped package.json; then commit the regenerated tracked files
# (SA-0MV7WAUHZ009UWY2). Non-fatal: a failed build logs a warning and the
# release continues, mirroring the CHANGELOG step.
if [[ "$DRY_RUN" != "true" && -n "${NEW_VERSION:-}" ]]; then
  REGEN_SCRIPT="${SCRIPT_DIR}/regenerate-version.js"
  if [[ -f "$REGEN_SCRIPT" ]]; then
    node "$REGEN_SCRIPT" "$NEW_VERSION" || {
      echo "Warning: derived version file regeneration failed (continuing with release)" >&2
    }
  fi
elif [[ "$DRY_RUN" == "true" ]]; then
  echo "Dry-run: derived version files would be regenerated for v${NEW_VERSION:-<version>}"
fi

# ── Full-suite gate on the merged dev→main tree (SA-0MVECQUGF0064U81) ──
# The dev→main merge is the integration point: assert the merged tree is
# full green BEFORE the release is made (tag created, branch pushed, PR
# opened/merged). The release branch push is a feature-branch push, so the
# pre-push hook skips its test gate — this gate is the only full-suite
# assertion for the release. It runs AFTER the tree-modifying steps above
# (version bump, CHANGELOG, derived files) so the asserted tree is exactly
# the tree that lands on main. Mirrors the pre-push hook's invocation
# (`--scope full --strict-git-env`); the cached runner makes repeat
# verification cheap. SHIP_TEST_SKIP=1 bypasses (not recommended).
if [[ "$DRY_RUN" != "true" && "${SHIP_TEST_SKIP:-}" != "1" ]]; then
  TEST_RUNNER=""
  if [[ -f "$REPO_ROOT/skill/test/scripts/run_tests.py" ]]; then
    TEST_RUNNER="$REPO_ROOT/skill/test/scripts/run_tests.py"
  elif [[ -f "$HOME/.pi/agent/skills/test/scripts/run_tests.py" ]]; then
    TEST_RUNNER="$HOME/.pi/agent/skills/test/scripts/run_tests.py"
  fi
  if [[ -z "$TEST_RUNNER" ]]; then
    echo "ERROR: full-suite gate cannot run — run_tests.py not found." >&2
    echo "  Refusing to release without a full-suite assertion on the merged tree." >&2
    echo "  Set SHIP_TEST_SKIP=1 to bypass (not recommended)." >&2
    exit 1
  fi
  echo "Release gate: running the full suite on the merged dev→main tree (--scope full --strict-git-env)..." >&2
  if python3 "$TEST_RUNNER" --scope full --strict-git-env 2>&1; then
    echo "Release gate: full suite green on the merged tree." >&2
  else
    echo "ERROR: Full test suite failed on the merged dev→main tree — the release is blocked." >&2
    echo "  Fix the failures on dev and re-run the release. SHIP_TEST_SKIP=1 bypasses (not recommended)." >&2
    exit 1
  fi
elif [[ "$DRY_RUN" == "true" ]]; then
  echo "Dry-run: full-suite gate would run on the merged tree (--scope full --strict-git-env)"
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
PR_BODY="Automated release created by ship skill.\n\nIncludes CHANGELOG.md with work-item summaries from this release."
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
  # ── Post-merge verification (defense in depth, SA-0MSJ2XMQL006CVQS) ──
  # Confirm the merge actually landed on origin/main before signalling
  # success. Without this, a silently-skipped/failed merge could still let
  # the run-release.js close step run and spuriously close work items with
  # a "Shipped in v<version>" reason even though the release never landed.
  git fetch origin main:refs/remotes/origin/main || {
    echo "Failed to fetch origin/main after PR merge" >&2
    exit 1
  }
  if git merge-base --is-ancestor "$BRANCH" origin/main 2>/dev/null; then
    echo "Merge verified: release branch $BRANCH is an ancestor of origin/main"
  else
    echo "ERROR: release branch $BRANCH is NOT on origin/main after merge — the release did not land." >&2
    exit 1
  fi
else
  echo "Release prepared. The PR $PR_URL should be merged once status checks pass (if any are configured) and reviewers approve."
fi

# Audit logging could be added here (e.g., call wl comment add ...)

exit 0
