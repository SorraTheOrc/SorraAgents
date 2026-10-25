#!/usr/bin/env bash
# worklog-ref-gate.sh
#
# Release-gating helper: detect the presence of *dangerous* worklog refs under
# refs/worklog/ and abort so worklog data is never merged into main/dev.
#
# This gate is invoked by the canonical release merge script
# (release/merge-dev-to-main.sh) and implements exactly the same rule as the
# JavaScript gate (scripts/check-worklog-refs.js) used by the run-release.js
# wrapper — see SA-0MVO7XZAB0095R6F.
#
# The rule (deliberately NOT name-based):
#
#   1. Remote-tracking mirror refs (refs/worklog/remotes/**) are excluded — they
#      are harmless bookkeeping maintained by `wl sync` and are never merged by
#      `git merge`/`git pull`.
#   2. Any other refs/worklog/* ref whose commit contains the worklog data file
#      (.worklog/worklog-data.jsonl) is excluded: it is worklog DATA, never a
#      code branch. `wl sync` maintains several such bookkeeping refs
#      (refs/worklog/data, refs/worklog/fetched-check,
#      refs/worklog/tmp-reauthor — SA-0MVMD7NTC0086HWK); new bookkeeping refs
#      are covered automatically without touching this script.
#   3. Every remaining refs/worklog/* ref is a genuinely dangerous orphan (no
#      data file, no common ancestor with main/dev) and is reported. A ref whose
#      data-file presence cannot be determined (git cat-file fails for any
#      reason) is treated as dangerous — the gate fails closed.
#
# Usage:
#   worklog-ref-gate.sh [<repo-path>]
#
#   <repo-path>  Repository to inspect (default: the current directory).
#
# Exit codes:
#   0  No dangerous worklog refs (release may proceed).
#   1  Dangerous worklog ref(s) found; each is printed to stderr as "  - <ref>".
#   2  Usage error, or <repo-path> is not a git repository.
#
# A failure of `git for-each-ref` itself (e.g. the refs/worklog namespace does
# not exist) is not a gating signal: nothing can be merged from a ref that does
# not exist, so the helper warns and exits 0, matching check-worklog-refs.js.

set -uo pipefail

usage() {
  cat <<EOF
Usage: $0 [<repo-path>]

Inspect <repo-path> (default: current directory) for dangerous worklog refs
under refs/worklog/. A ref is dangerous when it is not a remote-tracking
mirror (refs/worklog/remotes/**) and does not carry the worklog data file
(.worklog/worklog-data.jsonl).

Exit codes: 0 = clean, 1 = dangerous ref(s) found, 2 = usage error.
EOF
}

case "${1:-}" in
  -h|--help)
    usage
    exit 0
    ;;
esac

if [[ $# -gt 1 ]]; then
  echo "Error: at most one argument (<repo-path>) is accepted." >&2
  usage >&2
  exit 2
fi

REPO_PATH="${1:-.}"

if ! TOPLEVEL="$(git -C "$REPO_PATH" rev-parse --show-toplevel 2>/dev/null)"; then
  echo "Error: '$REPO_PATH' is not a git repository; cannot check worklog refs." >&2
  exit 2
fi

# Enumerate the worklog ref namespace. A failure here means there is nothing in
# the namespace to be dangerous (or no git refs at all) — mirrors the JS gate's
# fail-open behaviour for this specific check.
if ! REF_OUTPUT="$(git -C "$TOPLEVEL" for-each-ref refs/worklog/ --format='%(refname)' 2>/dev/null)"; then
  echo "Warning: unable to enumerate refs/worklog/ in '$TOPLEVEL'; proceeding without gating." >&2
  exit 0
fi

DANGEROUS=0

while IFS= read -r ref; do
  [[ -n "$ref" ]] || continue

  # 1. Remote-tracking mirrors are never merged.
  case "$ref" in
    refs/worklog/remotes/*) continue ;;
  esac

  # 2. A ref carrying the worklog data file is legitimate sync bookkeeping.
  #    Fail closed: anything other than a definite "contains the data file"
  #    (cat-file exit 0) leaves the ref flagged.
  if git -C "$TOPLEVEL" cat-file -e "${ref}:.worklog/worklog-data.jsonl" 2>/dev/null; then
    continue
  fi

  # 3. Genuinely dangerous orphan ref.
  DANGEROUS=1
  echo "  - $ref" >&2
done <<< "$REF_OUTPUT"

if [[ "$DANGEROUS" -eq 1 ]]; then
  exit 1
fi

exit 0
