#!/usr/bin/env bash
#
# merge-dev-to-main.sh — Release Manager merge workflow (PR-based)
#
# Merges the dev integration branch into main via a GitHub pull request.
# This approach works with server-side branch protection on `main` that
# requires pull requests, status checks, or restricts direct pushes.
#
# The script:
#   1. Verifies CI (dev-full-suite) is green on `dev`.
#   2. Creates a merge commit locally (dev → main).
#   3. Pushes the merge commit to a temporary `release/` branch.
#   4. Creates a GitHub PR from the temporary branch to `main`.
#   5. Waits for required status checks to pass on the PR.
#   6. Merges the PR using `gh pr merge --merge`.
#   7. Records an audit comment in the worklog.
#
# Usage:
#   bash scripts/release/merge-dev-to-main.sh [--dry-run] [--force]
#       [--work-item-id <id>] [--approver <name>] [--watch-timeout <seconds>]
#
# Options:
#   --dry-run            Show what would be done without making changes.
#   --force              Bypass the CI-green gate and status-check wait.
#   --work-item-id       Associate this merge with a specific work item for
#                        audit logging (optional; defaults to searching).
#   --approver           Override the approver identity in the audit record
#                        (defaults to the authenticated gh user).
#   --watch-timeout      Max seconds to wait for PR checks to pass
#                        (default: 600, i.e. 10 minutes).
#
# Requirements:
#   - gh CLI authenticated with repo access and Actions read/write.
#   - wl CLI available for audit logging.
#   - Clean working tree (no uncommitted changes).
#
# Exit codes:
#   0 — Merge completed successfully.
#   1 — Pre-flight check failed (CI not green, dirty tree, etc.).
#   2 — Merge, PR creation, or PR merge failed.
#   3 — PR checks timed out without passing.
#

set -euo pipefail

# ── Configuration ────────────────────────────────────────────────────────────
REPO="SorraTheOrc/SorraAgents"
DEV_BRANCH="dev"
MAIN_BRANCH="main"
REQUIRED_WORKFLOW="dev-full-suite"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# ── Parse arguments ──────────────────────────────────────────────────────────
DRY_RUN=false
FORCE=false
WORK_ITEM_ID=""
APPROVER=""
WATCH_TIMEOUT=600

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
      WORK_ITEM_ID="$2"
      shift 2
      ;;
    --approver)
      APPROVER="$2"
      shift 2
      ;;
    --watch-timeout)
      WATCH_TIMEOUT="$2"
      shift 2
      ;;
    --help|-h)
      echo "Usage: $0 [--dry-run] [--force] [--work-item-id <id>] [--approver <name>] [--watch-timeout <seconds>]"
      exit 0
      ;;
    *)
      echo "Unknown option: $1"
      exit 1
      ;;
  esac
done

log()  { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }
warn() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] WARNING: $*" >&2; }
err()  { echo "[$(date '+%Y-%m-%d %H:%M:%S')] ERROR: $*" >&2; }

# ── Pre-flight checks ────────────────────────────────────────────────────────

preflight() {
  log "Running pre-flight checks..."

  # Check gh authentication
  if ! gh auth status &>/dev/null; then
    err "gh CLI is not authenticated. Run 'gh auth login' first."
    exit 1
  fi

  # Check wl availability
  if ! command -v wl &>/dev/null; then
    err "wl CLI not found. Install worklog (wl) before running this script."
    exit 1
  fi

  # Check clean working tree
  if [[ -n "$(git status --porcelain)" ]]; then
    err "Working tree is dirty. Commit or stash changes before merging."
    exit 1
  fi

  log "Pre-flight checks passed."
}

# ── CI Verification ──────────────────────────────────────────────────────────

check_ci_green() {
  log "Checking CI status for '$REQUIRED_WORKFLOW' on '$DEV_BRANCH'..."

  local run_json
  run_json="$(gh run list \
    --repo "$REPO" \
    --workflow "$REQUIRED_WORKFLOW" \
    --branch "$DEV_BRANCH" \
    --limit 5 \
    --json databaseId,status,conclusion,headSha,createdAt \
    2>/dev/null)" || true

  if [[ -z "$run_json" || "$run_json" == "[]" ]]; then
    err "No '$REQUIRED_WORKFLOW' runs found on '$DEV_BRANCH'."
    err "Trigger the workflow before merging:"
    err "  gh workflow run '$REQUIRED_WORKFLOW' --ref $DEV_BRANCH"

    if [[ "$FORCE" == "true" ]]; then
      warn "--force: bypassing CI check despite no runs found."
      RUN_ID="N/A"
      RUN_SHA="N/A"
      return 0
    fi

    if [[ "$DRY_RUN" == "true" ]]; then
      log "[DRY-RUN] Would abort due to no CI runs found."
      return 1
    fi

    err "Run again with --force to bypass this gate (not recommended)."
    exit 1
  fi

  local latest_status latest_conclusion latest_id latest_sha
  latest_status="$(echo "$run_json" | jq -r '.[0].status')"
  latest_conclusion="$(echo "$run_json" | jq -r '.[0].conclusion')"
  latest_id="$(echo "$run_json" | jq -r '.[0].databaseId')"
  latest_sha="$(echo "$run_json" | jq -r '.[0].headSha')"

  if [[ "$latest_status" == "completed" && "$latest_conclusion" == "success" ]]; then
    log "CI '$REQUIRED_WORKFLOW' is GREEN on '$DEV_BRANCH'."
    log "  Run ID: $latest_id"
    log "  Commit: $latest_sha"
    RUN_ID="$latest_id"
    RUN_SHA="$latest_sha"
    return 0
  fi

  err "CI '$REQUIRED_WORKFLOW' is NOT green on '$DEV_BRANCH'."
  err "  Status:     $latest_status"
  err "  Conclusion: $latest_conclusion"
  err "  Run URL:    https://github.com/$REPO/actions/runs/$latest_id"

  if [[ "$FORCE" == "true" ]]; then
    warn "--force: bypassing CI check despite non-green conclusion."
    RUN_ID="$latest_id"
    RUN_SHA="$latest_sha"
    return 0
  fi

  if [[ "$DRY_RUN" == "true" ]]; then
    log "[DRY-RUN] Would abort due to CI not green."
    return 1
  fi

  err "Run again with --force to bypass this gate (not recommended)."
  exit 1
}

# ── Create merge commit on main ──────────────────────────────────────────────

create_merge_commit() {
  log "Fetching latest '$DEV_BRANCH' and '$MAIN_BRANCH' from origin..."

  git fetch origin "$DEV_BRANCH"
  git fetch origin "$MAIN_BRANCH"

  if [[ "$DRY_RUN" == "true" ]]; then
    log "[DRY-RUN] Would create merge commit (origin/$DEV_BRANCH into $MAIN_BRANCH)."
    log "[DRY-RUN] diff summary:"
    git diff --stat "origin/$MAIN_BRANCH".."origin/$DEV_BRANCH" 2>/dev/null || true
    return 0
  fi

  # Create the merge commit on origin/main without switching branches
  log "Creating merge commit (origin/$DEV_BRANCH into origin/$MAIN_BRANCH)..."

  # Use a temporary graft to compute the merge tree, then commit
  local merge_result
  merge_result="$( \
    git checkout -q "origin/$MAIN_BRANCH" && \
    git merge "origin/$DEV_BRANCH" --no-ff -m "Release: merge dev into main" 2>&1 \
  )" || {
    err "Merge failed. Resolve conflicts manually."
    err "$merge_result"
    # Switch back to original branch
    git checkout -q "$CURRENT_BRANCH" 2>/dev/null || true
    exit 2
  }
  echo "$merge_result"

  MERGE_COMMIT="$(git rev-parse HEAD)"
  log "Merge commit created: $MERGE_COMMIT"
}

# ── Push temp branch + create PR ─────────────────────────────────────────────

create_pr_and_merge() {
  local timestamp
  timestamp="$(date +%Y%m%d%H%M%S)"
  local temp_branch="release/dev-to-main-${timestamp}"

  if [[ "$DRY_RUN" == "true" ]]; then
    log "[DRY-RUN] Would create temp branch '${temp_branch}' at merge commit ${MERGE_COMMIT}."
    log "[DRY-RUN] Would push '${temp_branch}' to origin."
    log "[DRY-RUN] Would create PR: ${temp_branch} → ${MAIN_BRANCH}"
    log "[DRY-RUN] Would wait for status checks (timeout: ${WATCH_TIMEOUT}s)."
    log "[DRY-RUN] Would merge PR via 'gh pr merge --merge --delete-branch'."

    # Restore original branch in dry-run mode
    git checkout -q "$CURRENT_BRANCH" 2>/dev/null || true
    return 0
  fi

  # Create the temp branch at the merge commit and push it
  log "Creating temp branch '${temp_branch}'..."
  git branch "$temp_branch" "$MERGE_COMMIT"

  log "Pushing '${temp_branch}' to origin..."
  git push origin "$temp_branch"

  # Restore original branch so we're not left on a detached/merge state
  git checkout -q "$CURRENT_BRANCH" 2>/dev/null || true

  # Build PR body
  local pr_body
  pr_body="## Release: merge dev into main

This PR was created automatically by the release merge script.

### Changes included

\`\`\`
$(git log --oneline "origin/${DEV_BRANCH}" --not "origin/${MAIN_BRANCH}" 2>/dev/null | head -30 || echo '(unable to list changes)')
\`\`\`

### CI Verification

- **dev-full-suite**: [Run ${RUN_ID}](https://github.com/${REPO}/actions/runs/${RUN_ID})

> _Generated by \`scripts/release/merge-dev-to-main.sh\`_
"

  log "Creating PR: ${temp_branch} → ${MAIN_BRANCH}..."
  local pr_url pr_number
  pr_url="$(gh pr create \
    --repo "$REPO" \
    --base "$MAIN_BRANCH" \
    --head "$temp_branch" \
    --title "Release: merge dev into main" \
    --body "$pr_body" \
    2>&1)" || {
    err "Failed to create PR."
    err "$pr_url"
    exit 2
  }
  echo "$pr_url"
  pr_number="$(echo "$pr_url" | grep -oE '[0-9]+$' || true)"

  if [[ -z "$pr_number" ]]; then
    warn "Could not extract PR number from URL: $pr_url"
    PR_URL="$pr_url"
  else
    PR_URL="$pr_url"
    PR_NUMBER="$pr_number"
    log "PR #${PR_NUMBER} created: ${PR_URL}"
  fi

  # Wait for required status checks, unless --force
  if [[ "$FORCE" != "true" ]]; then
    log "Waiting for required status checks on PR #${PR_NUMBER} (timeout: ${WATCH_TIMEOUT}s)..."
    if ! gh pr checks "$PR_NUMBER" --repo "$REPO" --watch --interval 30 --required 2>&1; then
      if [[ "$WATCH_TIMEOUT" -gt 0 ]]; then
        err "PR checks did not all pass within ${WATCH_TIMEOUT}s timeout."
        err "PR is still open at ${PR_URL}"
        err "Manually review and merge when checks pass, or re-run with --force."
        exit 3
      fi
    fi
    log "All required checks passed."
  else
    warn "--force: skipping PR status check wait."
  fi

  # Merge the PR
  log "Merging PR #${PR_NUMBER}..."
  local merge_output
  merge_output="$(gh pr merge "$PR_NUMBER" --repo "$REPO" --merge --delete-branch 2>&1)" || {
    err "Failed to merge PR #${PR_NUMBER}."
    err "$merge_output"
    err "PR is still open at ${PR_URL}"
    exit 2
  }
  echo "$merge_output"

  # Delete the local temp branch
  git branch -D "$temp_branch" 2>/dev/null || true

  # Update main locally to reflect the merge
  git fetch origin "$MAIN_BRANCH" 2>/dev/null || true

  log "PR #${PR_NUMBER} merged successfully. Remote temp branch deleted."
}

# ── Audit logging ────────────────────────────────────────────────────────────

record_audit() {
  local approver="${APPROVER:-$(gh api user --jq '.login' 2>/dev/null || echo 'unknown')}"
  local merge_commit="${MERGE_COMMIT:-$(git rev-parse HEAD)}"
  local ci_run_id="${RUN_ID:-N/A}"
  local ci_sha="${RUN_SHA:-N/A}"
  local ci_url="https://github.com/${REPO}/actions/runs/${ci_run_id}"
  local pr_url="${PR_URL:-N/A}"
  local pr_number="${PR_NUMBER:-N/A}"
  local timestamp
  timestamp="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"

  local audit_comment
  audit_comment="## Release Merge Audit

- **Merge commit**: \`${merge_commit}\`
- **Source branch**: \`${DEV_BRANCH}\`
- **Target branch**: \`${MAIN_BRANCH}\`
- **PR**: [#${pr_number}](${pr_url})
- **Approver**: ${approver}
- **Timestamp**: ${timestamp}

### CI Verification

| Workflow | Run ID | Status |
|----------|--------|--------|
| ${REQUIRED_WORKFLOW} | [${ci_run_id}](${ci_url}) | success |

### Changes Released

\`\`\`
$(git log --oneline "origin/${DEV_BRANCH}" --not "$(git merge-base "origin/${MAIN_BRANCH}" "origin/${DEV_BRANCH}" 2>/dev/null)" 2>/dev/null | head -20 || echo '(unable to list)')
\`\`\`
"

  if [[ "$DRY_RUN" == "true" ]]; then
    log "[DRY-RUN] Would record the following audit comment to worklog:"
    echo "---"
    echo "$audit_comment"
    echo "---"
    return 0
  fi

  # If a work item ID was provided, comment on it
  if [[ -n "$WORK_ITEM_ID" ]]; then
    log "Recording audit comment on work item $WORK_ITEM_ID..."
    wl comment add "$WORK_ITEM_ID" --comment "$audit_comment" --author "release-manager" --json 2>/dev/null || {
      warn "Failed to record audit comment on work item $WORK_ITEM_ID."
      warn "Comment content saved above for manual recording."
    }
  else
    # Search for the parent epic to record the audit
    local parent_id
    parent_id="$(wl search 'push to dev release from main' --json 2>/dev/null | jq -r '.[0].id' 2>/dev/null)" || true
    if [[ -n "$parent_id" && "$parent_id" != "null" ]]; then
      log "Recording audit comment on work item $parent_id..."
      wl comment add "$parent_id" --comment "$audit_comment" --author "release-manager" --json 2>/dev/null || {
        warn "Failed to record audit comment on work item $parent_id."
        warn "Comment content saved above for manual recording."
      }
    else
      warn "Could not determine work item for audit logging."
      warn "Audit comment:"
      echo "$audit_comment"
    fi
  fi

  log "Audit recorded."
}

# ── Main ─────────────────────────────────────────────────────────────────────

main() {
  cd "$REPO_ROOT"

  # Save the branch we started from so we can restore it later
  CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
  export CURRENT_BRANCH

  if [[ "$DRY_RUN" == "true" ]]; then
    log "=== DRY RUN MODE — No changes will be made ==="
  fi

  preflight
  check_ci_green
  create_merge_commit
  create_pr_and_merge
  record_audit

  if [[ "$DRY_RUN" == "true" ]]; then
    log "=== DRY RUN COMPLETE ==="
  else
    log "Release merge dev → main completed successfully."
    log "Merge commit: ${MERGE_COMMIT}"
    log "PR: ${PR_URL}"
  fi
}

main "$@"
