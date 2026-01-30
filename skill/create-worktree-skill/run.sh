#!/usr/bin/env bash
set -euo pipefail

# Agent skill: create a named worktree, create/check-out a branch, commit a sample file, and run 'wl sync'
# Location: ./skill/create-worktree-skill/
# Usage: ./run.sh <work-item-id> <agent-name> <short-suffix>

if [ "$#" -lt 3 ]; then
  echo "Usage: $0 <work-item-id> <agent-name> <short-suffix>"
  exit 2
fi

WORK_ITEM_ID="$1"
AGENT_NAME="$2"
SHORT="$3"

command -v git >/dev/null 2>&1 || { echo "git is required"; exit 1; }
command -v wl >/dev/null 2>&1 || { echo "wl CLI is required"; exit 1; }

TIMESTAMP=$(date +"%d-%m-%y-%H-%M")
# record repo root before creating the worktree so we can copy .worklog from the main worktree
REPO_ROOT=$(git rev-parse --show-toplevel)

WORKTREE_DIR=".worktrees/tmp-worktree-${AGENT_NAME}-${TIMESTAMP}"
# Ensure WORKTREE_DIR is unique if it already exists (leftover from failed runs)
if [ -e "$WORKTREE_DIR" ]; then
  UNIQUE_POSTFIX=$(date +"%s%N")
  WORKTREE_DIR="${WORKTREE_DIR}-${UNIQUE_POSTFIX}"
  echo "Worktree path already existed; using unique path $WORKTREE_DIR"
fi
BRANCH_BASE="feature/${WORK_ITEM_ID}-${SHORT}"
BRANCH="$BRANCH_BASE"

echo "Creating worktree '$WORKTREE_DIR' with branch '$BRANCH'"

# Ensure repository Worklog state is published so new worktree can sync after wl init
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
  # Branch exists. Try to add worktree for it; if it's already checked out elsewhere,
  # create a new unique branch based on timestamp to avoid conflicts.
  echo "Branch ${BRANCH} already exists; attempting to add worktree for existing branch"
  if git worktree list --porcelain | grep -q "refs/heads/${BRANCH}"; then
    # branch is checked out in another worktree; create a unique branch instead
    UNIQUE_SUFFIX=$(date +"%s")
    BRANCH="${BRANCH_BASE}-${UNIQUE_SUFFIX}"
    echo "Branch is checked out elsewhere; creating a unique branch ${BRANCH} from HEAD"
    git worktree add --checkout "$WORKTREE_DIR" -b "$BRANCH" HEAD
  else
    git worktree add --checkout "$WORKTREE_DIR" "$BRANCH"
  fi
else
  git worktree add --checkout "$WORKTREE_DIR" -b "$BRANCH" HEAD
fi

echo "Using branch: ${BRANCH}"

pushd "$WORKTREE_DIR" >/dev/null
ROOT_DIR=$(git rev-parse --show-toplevel)

# Initialize Worklog in the new worktree using the parent repo settings (init-only strategy).
# Do NOT copy the parent's runtime DB files; instead, copy configuration defaults and run `wl init`.
if [ ! -d ".worklog" ]; then
  echo "Initializing Worklog in new worktree using parent repo settings (init-only)"
  mkdir -p .worklog
  # Copy opencode.json if present in repo root to provide provider/settings defaults
  if [ -f "${REPO_ROOT}/opencode.json" ]; then
    echo "Copying ${REPO_ROOT}/opencode.json into worktree"
    cp "${REPO_ROOT}/opencode.json" ./opencode.json
  fi
  # Copy parent .worklog/config.yaml if present to preserve Worklog config defaults
  if [ -f "${REPO_ROOT}/.worklog/config.yaml" ]; then
    echo "Copying ${REPO_ROOT}/.worklog/config.yaml into new worktree .worklog/config.yaml"
    mkdir -p .worklog
    cp "${REPO_ROOT}/.worklog/config.yaml" .worklog/config.yaml
  fi
  # Initialize local Worklog state using parent .worklog config defaults when present
  WL_INIT_ARGS=()
  if [ -f "${REPO_ROOT}/.worklog/config.yaml" ]; then
    # parse simple yaml entries (projectName, autoSync, autoExport)
    PROJECT_NAME=$(sed -n 's/^projectName:[[:space:]]*\(.*\)$/\1/p' "${REPO_ROOT}/.worklog/config.yaml" | sed 's/^ *//;s/ *$//') || true
    AUTO_SYNC=$(sed -n 's/^autoSync:[[:space:]]*\(.*\)$/\1/p' "${REPO_ROOT}/.worklog/config.yaml" | sed 's/^ *//;s/ *$//') || true
    AUTO_EXPORT=$(sed -n 's/^autoExport:[[:space:]]*\(.*\)$/\1/p' "${REPO_ROOT}/.worklog/config.yaml" | sed 's/^ *//;s/ *$//') || true
    if [ -n "$PROJECT_NAME" ]; then
      WL_INIT_ARGS+=(--project-name "$PROJECT_NAME")
    fi
    if [ -n "$AUTO_SYNC" ]; then
      # map true/false to yes/no
      if [ "$AUTO_SYNC" = "true" ] || [ "$AUTO_SYNC" = "True" ]; then
        WL_INIT_ARGS+=(--auto-sync yes)
      else
        WL_INIT_ARGS+=(--auto-sync no)
      fi
    fi
    if [ -n "$AUTO_EXPORT" ]; then
      if [ "$AUTO_EXPORT" = "true" ] || [ "$AUTO_EXPORT" = "True" ]; then
        WL_INIT_ARGS+=(--auto-export yes)
      else
        WL_INIT_ARGS+=(--auto-export no)
      fi
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
    # small pause to let init write files to disk
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
    if [ -f ./opencode.json ]; then
      echo "Using opencode.json in worktree for init defaults"
    fi
    # Retry initialization with defaults from parent config if available
    WL_INIT_ARGS=()
    if [ -f "./.worklog/config.yaml" ]; then
      PROJECT_NAME=$(sed -n 's/^projectName:[[:space:]]*\(.*\)$/\1/p' ./.worklog/config.yaml | sed 's/^ *//;s/ *$//') || true
      if [ -n "$PROJECT_NAME" ]; then
        WL_INIT_ARGS+=(--project-name "$PROJECT_NAME")
      fi
    fi
    # Retry initialization with defaults from parent config if available
    WL_INIT_ARGS=()
    if [ -f "./.worklog/config.yaml" ]; then
      PROJECT_NAME=$(sed -n 's/^projectName:[[:space:]]*\(.*\)$/\1/p' ./.worklog/config.yaml | sed 's/^ *//;s/ *$//') || true
      if [ -n "$PROJECT_NAME" ]; then
        WL_INIT_ARGS+=(--project-name "$PROJECT_NAME")
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

  # use REPO_ROOT (main worktree) when copying .worklog
  ROOT_DIR="$REPO_ROOT"

  popd >/dev/null

  echo "Skill run complete. Worktree: $WORKTREE_DIR Branch: $BRANCH Commit: $COMMIT_HASH"

exit 0
