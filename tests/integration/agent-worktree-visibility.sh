#!/usr/bin/env bash
set -euo pipefail

# Integration test: ensure agent-created worktree/branch is visible from another worktree
# Usage: ./tests/integration/agent-worktree-visibility.sh

ROOT_DIR=$(pwd)
SKILL=./skill/create-worktree-skill/run.sh
WORK_ITEM_ID=${1:-SA-0ML0502B21WHXDYA}
AGENT_A=testA
SHORT_A=it

TMP_A_DIR=".worklog/tmp-worktree-${AGENT_A}-test"
TMP_B_DIR=".worklog/tmp-worktree-testB"

cleanup() {
  echo "Cleaning up..."
  set +e
  # remove worktrees if present
  if [ -d "$TMP_A_DIR" ]; then
    git worktree remove "$TMP_A_DIR" || true
    rm -rf "$TMP_A_DIR" || true
  fi
  if [ -d "$TMP_B_DIR" ]; then
    git worktree remove "$TMP_B_DIR" || true
    rm -rf "$TMP_B_DIR" || true
  fi
}

trap cleanup EXIT

echo "Running skill to create worktree and branch (Agent A)"
"$SKILL" "$WORK_ITEM_ID" "$AGENT_A" "$SHORT_A"

# Determine branch name and commit hash
BRANCH="feature/${WORK_ITEM_ID}-${SHORT_A}"
if ! git show-ref --verify --quiet refs/heads/${BRANCH}; then
  echo "Branch ${BRANCH} not found in repo refs" >&2
  exit 3
fi
COMMIT_A=$(git rev-parse ${BRANCH})
echo "Agent A created branch ${BRANCH} commit ${COMMIT_A}"

echo "Creating Agent B worktree"
git worktree add --checkout "$TMP_B_DIR" HEAD

pushd "$TMP_B_DIR" >/dev/null
echo "Agent B running wl sync"
wl sync
popd >/dev/null

echo "Verifying branch visibility from Agent B (repo-level)"
if ! git show-ref --verify --quiet refs/heads/${BRANCH}; then
  echo "Branch ${BRANCH} not visible after wl sync" >&2
  exit 4
fi
COMMIT_B=$(git rev-parse ${BRANCH})

echo "Compare commits: A=${COMMIT_A} B=${COMMIT_B}"
if [ "$COMMIT_A" != "$COMMIT_B" ]; then
  echo "Commit mismatch between worktrees" >&2
  exit 5
fi

echo "Integration test succeeded: branch ${BRANCH} is visible with matching commit ${COMMIT_A}"

exit 0
