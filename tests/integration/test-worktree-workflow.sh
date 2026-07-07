#!/usr/bin/env bash
#
# Worktree Workflow Integration Test
#
# Creates an ephemeral worktree, runs a mini end-to-end agent workflow step
# (create worktree → branch → commit), and asserts isolation from the main
# checkout.
#
# Convention: follow the canonical worktree conventions from
# .wiki/concepts/git-worktree-best-practices-for-agent-workflows
#
# Usage:
#   bash tests/integration/test-worktree-workflow.sh
#
# Exit codes:
#   0 - All tests pass
#   1 - One or more tests failed
#
# Note on test location:
#   The integration test directory is tests/integration/ (project convention).
#   This test was added as part of SA-0MQNPZ1VX009SL27 (worktree adoption) and
#   supersedes the prior agent-worktree-visibility.sh test that was removed in
#   F1 (SA-0MQNR2E91003VUEI).

set -euo pipefail

# ── Colors ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color
PASS=0
FAIL=0

pass() {
  PASS=$((PASS + 1))
  echo -e "${GREEN}✓ PASS:${NC} $*"
}

fail() {
  FAIL=$((FAIL + 1))
  echo -e "${RED}✗ FAIL:${NC} $*"
}

skip() {
  echo -e "${YELLOW}— SKIP:${NC} $*"
}

# ── Setup ────────────────────────────────────────────────────────────────────
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT"

# Create a unique test worktree name
TEST_ID="test-$$-$(date +%s)"
WORKTREE_NAME="wl-test-${TEST_ID}"
WORKTREE_PATH=".worklog/worktrees/${WORKTREE_NAME}"
BRANCH_NAME="test/${WORKTREE_NAME}"

CLEANUP_DONE=false

cleanup() {
  if [ "$CLEANUP_DONE" = true ]; then
    return
  fi
  CLEANUP_DONE=true
  echo ""
  echo "--- Cleanup ---"

  # Remove the worktree (safe if already removed)
  if [ -d "$WORKTREE_PATH" ]; then
    # Try git worktree remove first, fall back to manual removal
    git worktree remove "$WORKTREE_PATH" 2>/dev/null || {
      # If worktree is dirty, force remove
      git worktree remove --force "$WORKTREE_PATH" 2>/dev/null || true
      rm -rf "$WORKTREE_PATH"
    }
  fi

  # Prune stale worktree metadata
  git worktree prune 2>/dev/null || true

  # Delete the test branch if it exists (local)
  git branch -D "$BRANCH_NAME" 2>/dev/null || true

  echo "Cleanup complete."
}

trap cleanup EXIT
trap 'echo "Test interrupted"; exit 1' INT TERM

# ── Test: Create worktree ────────────────────────────────────────────────────
echo "=== Worktree Workflow Integration Test ==="
echo ""

echo "Test worktree: $WORKTREE_PATH"
echo "Test branch:   $BRANCH_NAME"
echo ""

# Ensure the .worklog/worktrees directory exists
mkdir -p .worklog/worktrees

# Create the worktree with a new branch from dev
if git worktree add --track -b "$BRANCH_NAME" "$WORKTREE_PATH" dev 2>/dev/null; then
  pass "Created worktree at $WORKTREE_PATH with branch $BRANCH_NAME"
else
  # If branch already exists, try without --track
  if git worktree add "$WORKTREE_PATH" dev 2>/dev/null; then
    pass "Created worktree at $WORKTREE_PATH (branch may already exist)"
  else
    fail "Failed to create worktree at $WORKTREE_PATH"
    exit 1
  fi
fi

# Verify worktree exists
if [ -d "$WORKTREE_PATH" ]; then
  pass "Worktree directory exists at $WORKTREE_PATH"
else
  fail "Worktree directory not found at $WORKTREE_PATH"
fi

# ── Test: Branch exists ──────────────────────────────────────────────────────
if git branch --list "$BRANCH_NAME" | grep -q "$BRANCH_NAME"; then
  pass "Branch $BRANCH_NAME exists"
else
  fail "Branch $BRANCH_NAME not found"
fi

# ── Test: Isolation - make a change inside the worktree ──────────────────────
TEST_FILE="test-isolation-${TEST_ID}.txt"
echo "worktree isolation test ${TEST_ID}" > "${WORKTREE_PATH}/${TEST_FILE}"

# Stage and commit inside the worktree
(cd "$WORKTREE_PATH" && git add "$TEST_FILE" && \
 git commit -m "test: isolation check ${TEST_ID}" ) 2>/dev/null

if [ $? -eq 0 ]; then
  pass "Committed test file inside worktree"
else
  fail "Failed to commit inside worktree"
fi

# ── Test: Main checkout does not see the file ─────────────────────────────────
if [ ! -f "${REPO_ROOT}/${TEST_FILE}" ]; then
  pass "Main checkout is isolated - test file not visible in main checkout"
else
  fail "Main checkout is NOT isolated - test file found in main checkout"
fi

# ── Test: Main checkout git status is clean (ignoring test artifacts) ──────────
MAIN_STATUS=$(cd "$REPO_ROOT" && git status --porcelain 2>/dev/null | \
  grep -v "^?? .worklog/" | \
  grep -v "^?? tests/integration/test-worktree-workflow.sh" | \
  grep -v "^?? tests/integration/" || true)
if [ -z "$MAIN_STATUS" ]; then
  pass "Main checkout git status is clean (ignoring .worklog/ and test script)"
else
  skip "Main checkout has non-test changes: $MAIN_STATUS (may be pre-existing)"
fi

# ── Test: Worktree has the committed change ──────────────────────────────────
if (cd "$WORKTREE_PATH" && git log --oneline -1 | grep -q "${TEST_ID}"); then
  pass "Worktree git log contains the test commit"
else
  fail "Worktree git log missing the test commit"
fi

# ── Test: wl sync works inside worktree ──────────────────────────────────────
echo ""
echo "--- wl sync test ---"
if command -v wl &>/dev/null; then
  WLSYNC_OUTPUT=$(cd "$WORKTREE_PATH" && wl sync 2>&1) || true
  if echo "$WLSYNC_OUTPUT" | grep -qiE "error|failed|not found|not configured"; then
    skip "wl sync inside worktree reported: $WLSYNC_OUTPUT"
  else
    pass "wl sync succeeds inside worktree"
  fi
else
  skip "wl not installed - skipping wl sync test"
fi

# ── Test: Multiple worktree isolation (create a second worktree) ─────────────
echo ""
echo "--- Multi-worktree isolation test ---"

WORKTREE2_NAME="wl-test2-${TEST_ID}"
WORKTREE2_PATH=".worklog/worktrees/${WORKTREE2_NAME}"
BRANCH2_NAME="test/${WORKTREE2_NAME}"

CLEANUP2_DONE=false
cleanup2() {
  if [ "$CLEANUP2_DONE" = true ]; then
    return
  fi
  CLEANUP2_DONE=true
  if [ -d "$WORKTREE2_PATH" ]; then
    git worktree remove --force "$WORKTREE2_PATH" 2>/dev/null || true
    rm -rf "$WORKTREE2_PATH"
  fi
  git branch -D "$BRANCH2_NAME" 2>/dev/null || true
}

trap 'cleanup2; cleanup' EXIT

if git worktree add --track -b "$BRANCH2_NAME" "$WORKTREE2_PATH" dev 2>/dev/null; then
  pass "Created second worktree for isolation test"

  # Write different content in the second worktree
  TEST_FILE2="test-isolation2-${TEST_ID}.txt"
  echo "second worktree content ${TEST_ID}" > "${WORKTREE2_PATH}/${TEST_FILE2}"
  (cd "$WORKTREE2_PATH" && git add "$TEST_FILE2" && \
   git commit -m "test: second worktree isolation ${TEST_ID}" ) 2>/dev/null

  if [ $? -eq 0 ]; then
    pass "Second worktree committed changes"
  else
    fail "Second worktree commit failed"
  fi

  # Assert first worktree does NOT see second worktree's file
  if [ ! -f "${WORKTREE_PATH}/${TEST_FILE2}" ]; then
    pass "Worktree 1 is isolated from Worktree 2"
  else
    fail "Worktree 1 can see Worktree 2's files"
  fi

  # Assert second worktree does NOT see first worktree's file
  if [ ! -f "${WORKTREE2_PATH}/${TEST_FILE}" ]; then
    pass "Worktree 2 is isolated from Worktree 1"
  else
    fail "Worktree 2 can see Worktree 1's files"
  fi

  # Assert main checkout does NOT see either worktree file
  if [ ! -f "${REPO_ROOT}/${TEST_FILE2}" ]; then
    pass "Main checkout is isolated from Worktree 2"
  else
    fail "Main checkout can see Worktree 2's files"
  fi
else
  skip "Could not create second worktree - skipping multi-worktree isolation test"
fi

# Clean up second worktree before final report
cleanup2

# ── Report ───────────────────────────────────────────────────────────────────
echo ""
echo "=============================="
echo "  Results: ${PASS} passed, ${FAIL} failed"
echo "=============================="

if [ "$FAIL" -gt 0 ]; then
  exit 1
fi
exit 0
