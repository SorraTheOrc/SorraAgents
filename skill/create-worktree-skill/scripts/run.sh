#!/usr/bin/env bash
set -euo pipefail

# Create Worktree Skill script (moved into scripts/)
# Usage: ./scripts/run.sh <work-item-id> <agent-name>
# The script derives a short suffix from the work-item id (final '-' segment) or uses 'it' when not present.

if [ "$#" -ne 2 ]; then
  echo "Usage: $0 <work-item-id> <agent-name>"
  exit 2
fi

WORK_ITEM_ID="$1"
AGENT_NAME="$2"

command -v git >/dev/null 2>&1 || { echo "git is required"; exit 1; }
command -v wl >/dev/null 2>&1 || { echo "wl CLI is required"; exit 1; }

# Prevent repository post-pull hook from running wl sync before we initialize the new worktree
export WORKLOG_SKIP_POST_PULL=1

TIMESTAMP=$(date +"%d-%m-%y-%H-%M")
REPO_ROOT=$(git rev-parse --show-toplevel)

mkdir -p "${REPO_ROOT}/.worktrees"
# deterministic worktree name: <agent>-<work-item-id>; append epoch suffix if it already exists
WORKTREE_DIR="${REPO_ROOT}/.worktrees/${AGENT_NAME}-${WORK_ITEM_ID}"
if [ -e "$WORKTREE_DIR" ]; then
  WORKTREE_DIR="${WORKTREE_DIR}-$(date +%s)"
fi
# relative path used with git worktree add
WORKTREE_DIR_REL=${WORKTREE_DIR#${REPO_ROOT}/}

# Branch name: feature/<work-item-id>
BRANCH_BASE="feature/${WORK_ITEM_ID}"
BRANCH="$BRANCH_BASE"

echo "Creating worktree '$WORKTREE_DIR_REL' with branch '$BRANCH'"

# Do not run wl sync in repo root here. Worklog initialization and sync will be
# performed inside the new worktree after it's created and initialized.

# If the branch already exists, check it out into the new worktree; otherwise create it from HEAD
if git show-ref --verify --quiet "refs/heads/${BRANCH}"; then
  echo "Branch ${BRANCH} already exists; attempting to add worktree for existing branch"
  if git worktree list --porcelain | grep -q "refs/heads/${BRANCH}"; then
    UNIQUE_SUFFIX=$(date +"%s")
    BRANCH="${BRANCH_BASE}-${UNIQUE_SUFFIX}"
    echo "Branch is checked out elsewhere; creating a unique branch ${BRANCH} from HEAD"
    WORKLOG_SKIP_POST_PULL=1 git worktree add --checkout "$WORKTREE_DIR_REL" -b "$BRANCH" HEAD
  else
    WORKLOG_SKIP_POST_PULL=1 git worktree add --checkout "$WORKTREE_DIR_REL" "$BRANCH"
  fi
else
  WORKLOG_SKIP_POST_PULL=1 git worktree add --checkout "$WORKTREE_DIR_REL" -b "$BRANCH" HEAD
fi

echo "Using branch: ${BRANCH}"

pushd "$WORKTREE_DIR" >/dev/null
ROOT_DIR=$(git rev-parse --show-toplevel)

# Initialize Worklog in the new worktree when necessary; do not copy runtime DB files
if [ ! -f ".worklog/initialized" ]; then
  echo "Worklog not initialized in new worktree (missing .worklog/initialized) — initializing (init-only)"
  WL_INIT_ARGS=()
  if [ -f "${REPO_ROOT}/.worklog/config.yaml" ]; then
    PROJECT_NAME=$(sed -n 's/^projectName:[[:space:]]*\(.*\)$/\1/p' "${REPO_ROOT}/.worklog/config.yaml" | sed 's/^ *//;s/ *$//') || true
    PREFIX=$(sed -n 's/^prefix:[[:space:]]*\(.*\)$/\1/p' "${REPO_ROOT}/.worklog/config.yaml" | sed 's/^ *//;s/ *$//') || true
    if [ -n "$PROJECT_NAME" ]; then
      WL_INIT_ARGS+=(--project-name "$PROJECT_NAME")
    fi
    if [ -n "$PREFIX" ]; then
      WL_INIT_ARGS+=(--prefix "$PREFIX")
    fi
  fi

  if ! wl init --json "${WL_INIT_ARGS[@]}" > /tmp/wl_init_out 2>/tmp/wl_init_err; then
    echo "wl init failed in worktree; aborting" >&2
    echo "--- wl init stdout ---"; sed -n '1,200p' /tmp/wl_init_out || true
    echo "--- wl init stderr ---"; sed -n '1,200p' /tmp/wl_init_err || true
    ls -la .worklog || true
    exit 1
  else
    echo "wl init succeeded; output:"; sed -n '1,200p' /tmp/wl_init_out || true
    sleep 1
    echo ".worklog after init:"; ls -la .worklog || true
  fi
fi

# Add a comment to the work-item indicating the worktree and branch were created
COMMENT="Agent ${AGENT_NAME} created worktree '${WORKTREE_DIR_REL}' and branch '${BRANCH}'"
echo "Adding worklog comment to ${WORK_ITEM_ID}: ${COMMENT}"
if ! wl comment add "${WORK_ITEM_ID}" --comment "${COMMENT}" --author "${AGENT_NAME}" --json >/tmp/wl_comment_out 2>/tmp/wl_comment_err; then
  echo "Warning: failed to add worklog comment for ${WORK_ITEM_ID}. See /tmp/wl_comment_err" >&2
  sed -n '1,200p' /tmp/wl_comment_err || true
else
  sed -n '1,200p' /tmp/wl_comment_out || true
fi

echo "Running wl sync from worktree: $WORKTREE_DIR"
WL_SYNC_OUT=$(mktemp)
WL_SYNC_ERR=$(mktemp)
if wl sync >"$WL_SYNC_OUT" 2>"$WL_SYNC_ERR"; then
  echo "wl sync succeeded"
else
  echo "wl sync failed:" >&2
  sed -n '1,200p' "$WL_SYNC_ERR" >&2 || true
  echo "Listing .worklog for debug:" >&2
  ls -la .worklog || true
  [ -f .worklog/initialized ] && echo "initialized:" && cat .worklog/initialized || true
  exit 1
fi

ROOT_DIR="$REPO_ROOT"

popd >/dev/null

echo "Skill run complete. Worktree: $WORKTREE_DIR Branch: $BRANCH"

exit 0
