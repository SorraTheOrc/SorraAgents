#!/usr/bin/env bash
# hygiene_check.sh — Periodic stash and worktree hygiene check.
#
# Reports orphaned stashes (no matching open work item) and dirty main
# checkouts. Designed to run periodically (cron, CI, or manually).
#
# Usage:
#   scripts/hygiene_check.sh [--json] [--repo-root PATH]
#
# Exit codes:
#   0  — OK (no issues or only matched stashes)
#   1  — Warnings (orphaned stashes or dirty checkouts found)
#   2  — Error (script failure)
#
# Related work item: SA-0MT4DFE8Y004J8SP

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Default repo root: the main checkout (which may differ from the script
# location when the script lives inside a git worktree). Try to resolve
# the main repo root from the current directory; fall back to the script
# directory.
DEFAULT_REPO_ROOT="${PWD}"
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    # Resolve the MAIN checkout root from the common git dir
    COMMON_DIR="$(git rev-parse --git-common-dir 2>/dev/null)"
    if [[ -n "$COMMON_DIR" ]]; then
        if [[ "$COMMON_DIR" == /* ]]; then
            MAIN_ROOT="$(dirname "$COMMON_DIR")"
        else
            TOPLEVEL="$(git rev-parse --show-toplevel 2>/dev/null)"
            COMMON_ABS="${TOPLEVEL}/${COMMON_DIR}"
            MAIN_ROOT="$(cd "${COMMON_ABS}" && cd .. && pwd)"
        fi
        if [[ -d "$MAIN_ROOT/.git" || -f "$MAIN_ROOT/.git" ]]; then
            DEFAULT_REPO_ROOT="$MAIN_ROOT"
        fi
    fi
fi
REPO_ROOT="${DEFAULT_REPO_ROOT}"

# Parse arguments
JSON_OUTPUT=false
FORCE=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --json)
            JSON_OUTPUT=true
            shift
            ;;
        --repo-root)
            REPO_ROOT="$2"
            shift 2
            ;;
        --help)
            cat <<EOF
Usage: $(basename "$0") [--json] [--repo-root PATH]

Periodic hygiene check for git stash and worktree state.

Options:
  --json         Output results in JSON format
  --repo-root    Path to the repository root (default: script location)
  --help         Show this help message

Exit codes:
  0  — OK (no issues)
  1  — Warnings (orphaned stashes or dirty checkouts)
  2  — Error
EOF
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            exit 2
            ;;
    esac
done

# ── Helper functions ───────────────────────────────────────────────

warn_json() {
    # Emit a warning entry in JSON format
    echo "{\"type\":\"warning\",\"message\":\"$1\"}"
}

error_json() {
    echo "{\"type\":\"error\",\"message\":\"$1\"}"
}

# ── Check 1: Orphaned stashes ─────────────────────────────────────

check_orphaned_stashes() {
    local stash_list
    stash_list="$(git stash list 2>/dev/null || echo "")"
    
    if [[ -z "$stash_list" ]]; then
        if [[ "$JSON_OUTPUT" == "true" ]]; then
            echo "{\"check\":\"orphaned_stashes\",\"status\":\"ok\",\"count\":0}"
        else
            echo "✓ No stashes found."
        fi
        return 0
    fi

    local total_stashes=0
    local orphaned_count=0
    local orphaned_entries=""
    
    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        total_stashes=$((total_stashes + 1))
        
        # Extract work-item IDs from stash message
        local matched_ids
        matched_ids=$(echo "$line" | grep -oP '[A-Z]+-[A-Za-z0-9]+' || true)
        
        if [[ -z "$matched_ids" ]]; then
            # No work-item IDs — orphaned
            orphaned_count=$((orphaned_count + 1))
            orphaned_entries="${orphaned_entries}  - ${line}\n"
        else
            # Check if any matched ID is an open work item
            local is_open=false
            for wid in $matched_ids; do
                local status
                status=$(wl show "$wid" --json 2>/dev/null | python3 -c "
                    import sys, json
                    try:
                        data = json.load(sys.stdin)
                        print(data.get('workItem', {}).get('status', ''))
                    except:
                        print('')
                " || echo "")
                
                if [[ "$status" == "open" || "$status" == "in-progress" || "$status" == "in_progress" || "$status" == "blocked" ]]; then
                    is_open=true
                    break
                fi
            done
            
            if [[ "$is_open" == "false" ]]; then
                orphaned_count=$((orphaned_count + 1))
                orphaned_entries="${orphaned_entries}  - ${line}\n"
            fi
        fi
    done <<< "$stash_list"
    
    if [[ "$orphaned_count" -gt 0 ]]; then
        if [[ "$JSON_OUTPUT" == "true" ]]; then
            # Build a proper JSON array of entry lines via python (safe encoding)
            local entries_json
            entries_json="$(printf '%b' "$orphaned_entries" | python3 -c '
import sys, json
lines = [l.strip() for l in sys.stdin if l.strip()]
print(json.dumps(lines))
')"
            echo "{\"check\":\"orphaned_stashes\",\"status\":\"warning\",\"total\":${total_stashes},\"orphaned\":${orphaned_count},\"entries\":${entries_json}}"
        else
            echo "⚠ ${orphaned_count} orphaned stash(es) of ${total_stashes} total:"
            echo -e "$orphaned_entries"
        fi
        return 1
    else
        if [[ "$JSON_OUTPUT" == "true" ]]; then
            echo "{\"check\":\"orphaned_stashes\",\"status\":\"ok\",\"total\":${total_stashes},\"orphaned\":0}"
        else
            echo "✓ All ${total_stashes} stash(es) matched to open work items."
        fi
        return 0
    fi
}

# ── Check 2: Dirty main checkout ──────────────────────────────────

check_dirty_checkout() {
    local status_output
    status_output="$(git status --porcelain=v1 -b 2>/dev/null || echo "")"
    
    local dirty_files=()
    while IFS= read -r line; do
        # Skip branch info
        [[ "$line" == "##"* ]] && continue
        # Skip .worklog/ changes
        local file_path="${line:3}"
        file_path="$(echo "$file_path" | sed 's/^ *//')"
        [[ "$file_path" == .worklog/* ]] && continue
        [[ -n "$file_path" ]] && dirty_files+=("$file_path")
    done <<< "$status_output"
    
    if [[ ${#dirty_files[@]} -gt 0 ]]; then
        if [[ "$JSON_OUTPUT" == "true" ]]; then
            local files_json="["
            local first=true
            for f in "${dirty_files[@]}"; do
                if [[ "$first" == "true" ]]; then
                    first=false
                else
                    files_json+=","
                fi
                files_json+="\"$(echo "$f" | sed 's/"/\\"/g')\""
            done
            files_json+="]"
            echo "{\"check\":\"dirty_checkout\",\"status\":\"warning\",\"dirty_files\":${files_json}}"
        else
            echo "⚠ Dirty main checkout (${#dirty_files[@]} file(s) outside .worklog/):"
            for f in "${dirty_files[@]}"; do
                echo "  - $f"
            done
            echo "  Note: This blocks worktree creation but may be stale."
        fi
        return 1
    else
        if [[ "$JSON_OUTPUT" == "true" ]]; then
            echo "{\"check\":\"dirty_checkout\",\"status\":\"ok\"}"
        else
            echo "✓ Main checkout is clean (no uncommitted changes outside .worklog/)."
        fi
        return 0
    fi
}

# ── Check 3: Orphaned worktrees ───────────────────────────────────

check_orphaned_worktrees() {
    local worktrees
    worktrees="$(git worktree list 2>/dev/null || echo "")"
    
    # Check for worktrees in .worklog/worktrees that reference deleted branches
    local orphaned_wt=""
    local wt_count=0
    
    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        wt_count=$((wt_count + 1))
        
        # Extract worktree path
        local wt_path
        wt_path="$(echo "$line" | awk '{print $1}')"
        
        # The branch is in the third column, in brackets: [branch]
        # Format: <path> <HEAD> [<branch>]
        local branch
        branch="$(echo "$line" | grep -o '\[[^]]*\]' | tr -d '[]' || true)"
        
        if [[ -n "$branch" ]]; then
            if ! git show-ref --verify --quiet "refs/heads/$branch" 2>/dev/null; then
                orphaned_wt="${orphaned_wt}  - ${wt_path} (branch ${branch} deleted)\n"
            fi
        fi
    done <<< "$worktrees"
    
    if [[ -n "$orphaned_wt" ]]; then
        if [[ "$JSON_OUTPUT" == "true" ]]; then
            echo "{\"check\":\"orphaned_worktrees\",\"status\":\"warning\",\"count\":${wt_count},\"orphaned\":[${orphaned_wt}]}"
        else
            echo "⚠ ${wt_count} worktree(s) found, some with deleted branches:"
            echo -e "$orphaned_wt"
        fi
        return 1
    else
        if [[ "$JSON_OUTPUT" == "true" ]]; then
            echo "{\"check\":\"orphaned_worktrees\",\"status\":\"ok\",\"count\":${wt_count}}"
        else
            echo "✓ All ${wt_count} worktree(s) have valid branches."
        fi
        return 0
    fi
}

# ── Main ──────────────────────────────────────────────────────────

cd "$REPO_ROOT"

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
    error_json "Not a git repository at ${REPO_ROOT}"
    exit 2
fi

overall_exit=0

# Run checks
check_orphaned_stashes || overall_exit=1
check_dirty_checkout || overall_exit=1
check_orphaned_worktrees || overall_exit=1

if [[ "$overall_exit" -ne 0 ]]; then
    if [[ "$JSON_OUTPUT" == "true" ]]; then
        # Emit a final summary line; the individual check JSON lines above
        # carry the detail.
        echo '{"check":"summary","status":"warning","message":"Hygiene check completed with warnings"}'
    else
        echo ""
        echo "Hygiene check completed with warnings."
        echo "Run 'wl list orphan' to find related work items."
        echo "Orphaned stashes should be triaged (restore-and-commit via work item, or delete if stale)."
    fi
fi

exit $overall_exit
