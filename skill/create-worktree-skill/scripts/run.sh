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
# derive suffix from work item id if it contains a final '-suffix'
if [[ "$WORK_ITEM_ID" == *-* ]]; then
  SHORT=${WORK_ITEM_ID##*-}
else
  # sensible default when not present
  SHORT=it
fi

command -v git >/dev/null 2>&1 || { echo "git is required"; exit 1; }
command -v wl >/dev/null 2>&1 || { echo "wl CLI is required"; exit 1; }

# Prevent repository post-pull hook from running wl sync before we initialize the new worktree
export WORKLOG_SKIP_POST_PULL=1

TIMESTAMP=$(date +"%d-%m-%y-%H-%M")
REPO_ROOT=$(git rev-parse --show-toplevel)

mkdir -p "${REPO_ROOT}/.worktrees"
# create a unique worktree dir inside the repo .worktrees using mktemp
WORKTREE_DIR=$(mktemp -d "${REPO_ROOT}/.worktrees/tmp-worktree-${AGENT_NAME}-XXXXXXXX")
if [ -z "$WORKTREE_DIR" ] || [ ! -d "$WORKTREE_DIR" ]; then
  echo "Failed to create unique worktree dir with mktemp" >&2
  exit 1
fi
# relative path used with git worktree add
WORKTREE_DIR_REL=${WORKTREE_DIR#${REPO_ROOT}/}

BRANCH_BASE="feature/${WORK_ITEM_ID}-${SHORT}"
BRANCH="$BRANCH_BASE"

echo "Creating worktree '$WORKTREE_DIR_REL' with branch '$BRANCH'"

echo "Ensuring repository Worklog state is up-to-date (running wl sync in repo root)"
pushd "$REPO_ROOT" >/dev/null
if wl sync; then
  echo "Repository wl sync succeeded"
else
  echo "Warning: repository wl sync failed or reported uninitialized; continuing but new worktree sync may fail" >&2
fi
popd >/dev/null

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

echo "agent: ${AGENT_NAME}" > agent-metadata.txt
echo "work-item: ${WORK_ITEM_ID}" >> agent-metadata.txt
echo "timestamp: $(date --iso-8601=seconds)" >> agent-metadata.txt

echo "Sample change from agent ${AGENT_NAME} for ${WORK_ITEM_ID}" > agent-sample.txt
git add agent-metadata.txt agent-sample.txt
git commit -m "chore(${WORK_ITEM_ID}): agent ${AGENT_NAME} sample commit"

echo "Running wl sync from worktree: $WORKTREE_DIR"
WL_SYNC_OUT=$(mktemp)
WL_SYNC_ERR=$(mktemp)
if wl sync >"$WL_SYNC_OUT" 2>"$WL_SYNC_ERR"; then
  echo "wl sync succeeded"
else
  SYNC_ERR_CONTENT=$(cat "$WL_SYNC_ERR" | tr -d '\r')
  echo "wl sync failed: $SYNC_ERR_CONTENT"
  if echo "$SYNC_ERR_CONTENT" | grep -qi "not initialized"; then
    echo "Detected uninitialized Worklog in worktree; attempting 'wl init' and retry"
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
    if wl init --json "${WL_INIT_ARGS[@]}" > /tmp/wl_init_out 2>/tmp/wl_init_err; then
      echo "wl init succeeded; output:"; sed -n '1,200p' /tmp/wl_init_out || true
      sleep 1
      echo ".worklog after init:"; ls -la .worklog || true
      echo "wl init succeeded; retrying wl sync"
      if ! wl sync >"$WL_SYNC_OUT" 2>"$WL_SYNC_ERR"; then
        echo "wl sync still failing after wl init:" >&2
        cat "$WL_SYNC_ERR" >&2
        echo "Listing .worklog for debug:" >&2
        ls -la .worklog || true
        echo "Printing .worklog/initialized if present:" >&2
        [ -f .worklog/initialized ] && cat .worklog/initialized || true
        echo "wl sync still failing after wl init and no bootstrap performed:" >&2
        cat "$WL_SYNC_ERR" >&2 || true
        echo "Listing .worklog for debug:" >&2
        ls -la .worklog || true
        exit 1
      fi
    else
      echo "wl init failed; aborting" >&2
      cat "$WL_SYNC_ERR" >&2 || true
      exit 1
    fi
  else
    echo "wl sync failed with unexpected error:" >&2
    cat "$WL_SYNC_ERR" >&2 || true
    exit 1
  fi
fi

COMMIT_HASH=$(git rev-parse HEAD)
echo "Committed ${COMMIT_HASH} on ${BRANCH} in ${WORKTREE_DIR}"

ROOT_DIR="$REPO_ROOT"

popd >/dev/null

echo "Skill run complete. Worktree: $WORKTREE_DIR Branch: $BRANCH Commit: $COMMIT_HASH"

exit 0
