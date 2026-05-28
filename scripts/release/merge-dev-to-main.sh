#!/usr/bin/env bash
#
# merge-dev-to-main.sh — Release Manager merge workflow
#
# Merges the dev integration branch into main after verifying that required
# CI jobs (dev-full-suite) are green. Records an audit comment in the
# worklog with the merge commit hash, CI run IDs, and approver identity.
#
# Usage:
#   bash scripts/release/merge-dev-to-main.sh [--dry-run] [--force] [--work-item-id <id>] [--approver <name>]
#
# Options:
#   --dry-run         Show what would be done without making changes.
#   --force           Bypass the CI-green gate. The release manager must
#                     explicitly accept responsibility for merging without
#                     green CI.
#   --work-item-id    Associate this merge with a specific work item for
#                     audit logging (optional; defaults to searching recent).
#   --approver        Override the approver identity in the audit record
#                     (defaults to the authenticated gh user).
#
# Requirements:
#   - gh CLI authenticated with repo access and Actions read permissions.
#   - wl CLI available for audit logging.
#   - Clean working tree (no uncommitted changes).
#
# Exit codes:
#   0 — Merge completed successfully.
#   1 — Pre-flight check failed (CI not green, dirty tree, etc.).
#   2 — Merge or push failed.
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
    --help|-h)
      echo "Usage: $0 [--dry-run] [--force] [--work-item-id <id>] [--approver <name>]"
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

  # Verify we are on main branch
  local current_branch
  current_branch="$(git rev-parse --abbrev-ref HEAD)"
  if [[ "$current_branch" != "$MAIN_BRANCH" ]]; then
    err "Must be on '$MAIN_BRANCH' branch to merge. Currently on '$current_branch'."
    exit 1
  fi

  log "Pre-flight checks passed."
}

# ── CI Verification ──────────────────────────────────────────────────────────

check_ci_green() {
  log "Checking CI status for '$REQUIRED_WORKFLOW' on '$DEV_BRANCH'..."

  # Fetch recent workflow runs for dev-full-suite on the dev branch
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

  # Check the most recent run
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

# ── Merge ────────────────────────────────────────────────────────────────────

do_merge() {
  log "Fetching latest '$DEV_BRANCH' and '$MAIN_BRANCH' from origin..."

  git fetch origin "$DEV_BRANCH"
  git fetch origin "$MAIN_BRANCH"

  if [[ "$DRY_RUN" == "true" ]]; then
    log "[DRY-RUN] Would merge origin/$DEV_BRANCH into $MAIN_BRANCH."
    log "[DRY-RUN] diff summary:"
    git diff --stat "$MAIN_BRANCH".."origin/$DEV_BRANCH" 2>/dev/null || true
    return 0
  fi

  log "Merging origin/$DEV_BRANCH into $MAIN_BRANCH..."

  local merge_result
  merge_result="$(git merge "origin/$DEV_BRANCH" --no-ff -m "Release: merge dev into main" 2>&1)" || {
    err "Merge failed. Resolve conflicts manually and re-run."
    err "$merge_result"
    exit 2
  }
  echo "$merge_result"

  # Get the merge commit hash
  MERGE_COMMIT="$(git rev-parse HEAD)"
  log "Merge commit: $MERGE_COMMIT"
}

# ── Push ─────────────────────────────────────────────────────────────────────

do_push() {
  if [[ "$DRY_RUN" == "true" ]]; then
    log "[DRY-RUN] Would push $MAIN_BRANCH to origin."
    return 0
  fi

  log "Pushing $MAIN_BRANCH to origin..."

  if ! git push origin "$MAIN_BRANCH"; then
    err "Push to origin/$MAIN_BRANCH failed."
    exit 2
  fi

  log "Push successful."
}

# ── Audit logging ────────────────────────────────────────────────────────────

record_audit() {
  local approver="${APPROVER:-$(gh api user --jq '.login' 2>/dev/null || echo 'unknown')}"
  local merge_commit="${MERGE_COMMIT:-$(git rev-parse HEAD)}"
  local ci_run_id="${RUN_ID:-N/A}"
  local ci_sha="${RUN_SHA:-N/A}"
  local ci_url="https://github.com/${REPO}/actions/runs/${ci_run_id}"
  local timestamp
  timestamp="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"

  local audit_comment
  audit_comment="## Release Merge Audit

- **Merge commit**: \`${merge_commit}\`
- **Source branch**: \`${DEV_BRANCH}\`
- **Target branch**: \`${MAIN_BRANCH}\`
- **Approver**: ${approver}
- **Timestamp**: ${timestamp}

### CI Verification

| Workflow | Run ID | Status |
|----------|--------|--------|
| ${REQUIRED_WORKFLOW} | [${ci_run_id}](${ci_url}) | success |

### Changes Released

\`\`\`
$(git log --oneline "origin/${DEV_BRANCH}" --not "$(git merge-base HEAD~1 HEAD)" 2>/dev/null | head -20 || echo '(unable to list)')
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

  if [[ "$DRY_RUN" == "true" ]]; then
    log "=== DRY RUN MODE — No changes will be made ==="
  fi

  preflight
  check_ci_green
  do_merge
  do_push
  record_audit

  if [[ "$DRY_RUN" == "true" ]]; then
    log "=== DRY RUN COMPLETE ==="
  else
    log "Release merge dev → main completed successfully."
    log "Merge commit: ${MERGE_COMMIT}"
  fi
}

main "$@"
