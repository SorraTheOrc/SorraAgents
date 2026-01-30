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
WORKTREE_DIR=".worklog/tmp-worktree-${AGENT_NAME}-${TIMESTAMP}"
BRANCH="feature/${WORK_ITEM_ID}-${SHORT}"

echo "Creating worktree '$WORKTREE_DIR' with branch '$BRANCH'"

# Create the worktree and new branch based on HEAD
git worktree add --checkout "$WORKTREE_DIR" -b "$BRANCH" HEAD

pushd "$WORKTREE_DIR" >/dev/null
ROOT_DIR=$(git rev-parse --show-toplevel)

# Ensure the new worktree has Worklog local state.
# If the parent repo has a .worklog directory, copy it. Otherwise initialize with `wl init`.
if [ ! -d ".worklog" ]; then
  if [ -d "${ROOT_DIR}/.worklog" ]; then
    echo "Copying parent .worklog into new worktree"
    cp -a "${ROOT_DIR}/.worklog" .worklog
  else
    echo "No parent .worklog found; initializing Worklog in new worktree"
    # Copy repository settings that should be used as defaults, if present
    if [ -f "${ROOT_DIR}/opencode.json" ]; then
      echo "Copying opencode.json defaults into new worktree"
      cp "${ROOT_DIR}/opencode.json" ./opencode.json
    fi
    # Initialize local Worklog state
    if ! wl init; then
      echo "wl init failed in worktree; aborting" >&2
      exit 1
    fi
  fi
fi

echo "agent: ${AGENT_NAME}" > agent-metadata.txt
echo "work-item: ${WORK_ITEM_ID}" >> agent-metadata.txt
echo "timestamp: $(date --iso-8601=seconds)" >> agent-metadata.txt

echo "Sample change from agent ${AGENT_NAME} for ${WORK_ITEM_ID}" > agent-sample.txt
git add agent-metadata.txt agent-sample.txt
git commit -m "chore(${WORK_ITEM_ID}): agent ${AGENT_NAME} sample commit"

echo "Running wl sync from worktree: $WORKTREE_DIR"
wl sync

COMMIT_HASH=$(git rev-parse HEAD)
echo "Committed ${COMMIT_HASH} on ${BRANCH} in ${WORKTREE_DIR}"

popd >/dev/null

echo "Skill run complete. Worktree: $WORKTREE_DIR Branch: $BRANCH Commit: $COMMIT_HASH"

exit 0
