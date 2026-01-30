#!/usr/bin/env bash
set -euo pipefail

# Create Worktree Skill script (moved into scripts/)
# Usage: ./scripts/run.sh <work-item-id> <agent-name>
# The script derives a short suffix from the work-item id (final '-' segment) or uses 'it' when not present.

if [ "$#" -lt 1 ] || [ "$#" -gt 2 ]; then
  echo "Usage: $0 <work-item-id> [agent-name]"
  exit 2
fi

WORK_ITEM_ID="$1"
# Agent name may be provided as an optional second arg; otherwise derive it from env/git/whoami
if [ "$#" -ge 2 ]; then
  AGENT_NAME="$2"
else
  AGENT_NAME="${AGENT_NAME:-}"
  if [ -z "$AGENT_NAME" ]; then
    # prefer git user.name, then git user.email, then whoami, then hostname
    GIT_NAME=$(git config user.name 2>/dev/null || true)
    if [ -z "$GIT_NAME" ]; then
      GIT_NAME=$(git config user.email 2>/dev/null || true)
    fi
    if [ -z "$GIT_NAME" ]; then
      GIT_NAME=$(whoami 2>/dev/null || true)
    fi
    if [ -z "$GIT_NAME" ]; then
      GIT_NAME=$(hostname 2>/dev/null || true)
    fi
    AGENT_NAME="${GIT_NAME:-agent}"
    # sanitize: lowercase, replace non-alnum with '-', trim '-' edges
    AGENT_NAME=$(echo "$AGENT_NAME" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9._-]/-/g' | sed 's/^-*//;s/-*$//')
  fi
fi

command -v git >/dev/null 2>&1 || { echo "git is required"; exit 1; }
command -v wl >/dev/null 2>&1 || { echo "wl CLI is required"; exit 1; }

# Prevent repository post-pull hook from running wl sync before we initialize the new worktree
export WORKLOG_SKIP_POST_PULL=1

TIMESTAMP=$(date +"%d-%m-%y-%H-%M")
REPO_ROOT=$(git rev-parse --show-toplevel)

mkdir -p "${REPO_ROOT}/.worktrees"
# refuse to create if any worktree exists for this work-item id
shopt -s nullglob
existing=("${REPO_ROOT}/.worktrees/"*"-${WORK_ITEM_ID}"*)
shopt -u nullglob
if [ "${#existing[@]}" -gt 0 ]; then
  echo "Refusing to create worktree: found existing worktree(s) matching '*-${WORK_ITEM_ID}*':"
  for p in "${existing[@]}"; do
    echo "  - $p"
  done
  echo "Please confirm those worktree(s) are inactive and remove them (eg. 'git worktree remove <path>' or 'rm -rf <path>') before retrying."
  exit 2
fi

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

# Ensure repository Worklog state is up-to-date (running wl sync in repo root)
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

  # Provide non-interactive defaults and repo config when available
  if [ -f "${REPO_ROOT}/opencode.json" ]; then
    cp "${REPO_ROOT}/opencode.json" ./opencode.json || true
  fi
  if [ -f "${REPO_ROOT}/.worklog/config.yaml" ]; then
    mkdir -p .worklog
    cp "${REPO_ROOT}/.worklog/config.yaml" .worklog/config.yaml || true
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
