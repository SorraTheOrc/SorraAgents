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

# ── Check 4: Main-checkout finish lock (WL-0MVPADZ1Z000FOSG AC5) ────────

check_main_checkout_lock() {
    local lock_file="${REPO_ROOT}/.worklog/locks/main-checkout.lock"
    if [[ ! -f "$lock_file" ]]; then
        if [[ "$JSON_OUTPUT" == "true" ]]; then
            echo '{"check":"main_checkout_lock","status":"ok"}'
        else
            echo "✓ No finish holds the main-checkout lock."
        fi
        return 0
    fi

    # Probe: a non-blocking flock on a fresh fd succeeds only when nobody
    # holds the lock. The subshell's fd closes on exit, releasing the probe.
    if ( exec 9<>"$lock_file" && flock -n 9 ) 2>/dev/null; then
        if [[ "$JSON_OUTPUT" == "true" ]]; then
            echo '{"check":"main_checkout_lock","status":"ok"}'
        else
            echo "✓ Main-checkout lock file exists but is free."
        fi
        return 0
    fi

    local holder="unknown (lock file present but no holder metadata)"
    holder="$(python3 - "$lock_file" <<'PY' 2>/dev/null || echo "$holder"
import json, sys
path = sys.argv[1]
try:
    data = json.load(open(path, encoding='utf-8'))
except Exception:
    print('unknown (lock file present but no holder metadata)')
    raise SystemExit(0)
parts = []
wid = data.get('work_item_id')
parts.append(f'work item {wid}' if wid else 'unknown work item')
if data.get('pid'):
    parts.append(f"pid {data['pid']}")
if data.get('started_at'):
    parts.append(f"started {data['started_at']}")
if data.get('repo_root'):
    parts.append(f"repo {data['repo_root']}")
if data.get('host'):
    parts.append(f"host {data['host']}")
print(', '.join(parts))
PY
)"

    if [[ "$JSON_OUTPUT" == "true" ]]; then
        echo "{\"check\":\"main_checkout_lock\",\"status\":\"warning\",\"lock_path\":\"$lock_file\",\"holder\":\"$(echo "$holder" | sed 's/["\\]/\\&/g')\"}"
    else
        echo "⚠ Main-checkout lock is HELD: $holder"
        echo "  Lock file: $lock_file"
        echo "  Only one implement.py finish may operate on a shared main checkout."
    fi
    return 1
}

# ── Check 5: Tracked-file deletions in the main checkout (AC6) ────────────

check_tracked_deletions() {
    local status_output
    status_output="$(git status --porcelain=v1 2>/dev/null || echo "")"

    local deletions=()
    while IFS= read -r line; do
        [[ -z "$line" || "$line" == "##"* ]] && continue
        local code="${line:0:2}"
        local file_path="${line:3}"
        file_path="$(echo "$file_path" | sed 's/^ *//') "
        file_path="$(echo "$file_path" | sed 's/ -> .*//' | sed 's/ *$//')"
        [[ -z "$file_path" || "$file_path" == .worklog/* ]] && continue
        if [[ "$code" == *D* ]]; then
            deletions+=("$file_path")
        fi
    done <<< "$status_output"

    if [[ ${#deletions[@]} -gt 0 ]]; then
        if [[ "$JSON_OUTPUT" == "true" ]]; then
            local deletions_json="["
            local first=true
            for f in "${deletions[@]}"; do
                if [[ "$first" == "true" ]]; then
                    first=false
                else
                    deletions_json+=","
                fi
                deletions_json+="\"$(echo "$f" | sed 's/"/\\"/g')\""
            done
            deletions_json+="]"
            echo "{\"check\":\"tracked_deletions\",\"status\":\"warning\",\"deletions\":${deletions_json}}"
        else
            echo "⚠ Tracked-file deletions in the main checkout (${#deletions[@]}):"
            for f in "${deletions[@]}"; do
                echo "  - $f"
            done
            echo "  Recovery: git checkout -- <path> (or git restore <path>) in the main checkout."
        fi
        return 1
    else
        if [[ "$JSON_OUTPUT" == "true" ]]; then
            echo '{"check":"tracked_deletions","status":"ok"}'
        else
            echo "✓ No tracked-file deletions in the main checkout."
        fi
        return 0
    fi
}

# ── Check 6: Self-referential branch upstreams ─────────────────────────
# `branch.<name>.remote = "."` on a long-lived branch (dev/main) makes a
# plain `git pull` a silent no-op: it fetches from the same repository and
# changes nothing, leaving the machine on stale skills and stale worklog data
# (SA-0MVX1M6HH004YPX2). The detection module is consumed from this
# framework's own skill tree (not the checked repo's), so `--repo-root` can
# point at any project. FAIL-OPEN by design (AC3): a missing module, a
# non-repo target or a git failure is reported as `skipped`, never as an
# error, and nothing is ever rewritten — repair stays an explicit operator
# action (`--fix`).

check_self_referential_upstream() {
    local detection
    detection="$(cd "$SCRIPT_DIR/.." 2>/dev/null && pwd)/skill/shared/self_referential_upstream.py"
    if [[ ! -f "$detection" ]]; then
        if [[ "$JSON_OUTPUT" == "true" ]]; then
            echo '{"check":"self_referential_upstream","status":"skipped","reason":"detection module not present"}'
        else
            echo "– Self-referential-upstream check skipped (detection module not present)."
        fi
        return 0
    fi

    local json_flag=false
    [[ "$JSON_OUTPUT" == "true" ]] && json_flag=true

    # The inline python both detects and formats the finding, so the JSON
    # shape lives in one place. Exit 0 = no defect / skipped, 1 = defect.
    if python3 - "$detection" "$REPO_ROOT" "$json_flag" <<'PY'
import json
import sys
from pathlib import Path

detection_path, repo_root, json_flag = (
    sys.argv[1],
    sys.argv[2],
    sys.argv[3] == "true",
)
CHECK = "self_referential_upstream"


def report(status, **fields):
    if json_flag:
        payload = {"check": CHECK, "status": status, **fields}
        print(json.dumps(payload))
    elif status == "skipped":
        print(f"– {CHECK.replace('_', ' ')}: skipped ({fields.get('reason', '')})")
    elif status == "ok":
        print("✓ No self-referential upstreams on long-lived branches.")
    else:
        defects = fields.get("defects", [])
        print(
            f"⚠ {len(defects)} long-lived branch(es) track themselves — "
            "`git pull` is a silent no-op:"
        )
        for finding in defects:
            print(f"  - {finding['branch']}: {finding['message']}")
        print(
            "  Repair (explicit, never automatic): "
            "python3 skill/shared/self_referential_upstream.py --fix"
        )


sys.path.insert(0, str(Path(detection_path).resolve().parents[1]))
try:
    from shared.self_referential_upstream import (
        detect_self_referential_upstreams as _detect,
    )
except Exception as exc:  # fail-open: an unavailable checker is a skip
    report("skipped", reason=f"detection unavailable: {exc}")
    raise SystemExit(0)

try:
    # Worktree branches are reported as ``expected``; only ``defect`` findings
    # are the machine-wide breakage this check warns about.
    found = [
        finding
        for finding in _detect(repo_root)
        if finding.get("type") == "defect"
    ]
except Exception as exc:  # fail-open: git/repo errors never fail hygiene
    report("skipped", reason=f"detection unavailable: {exc}")
    raise SystemExit(0)

if found:
    report("warning", defect_count=len(found), defects=found)
    raise SystemExit(1)
report("ok")
raise SystemExit(0)
PY
    then
        return 0
    fi
    return 1
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
check_main_checkout_lock || overall_exit=1
check_tracked_deletions || overall_exit=1
check_self_referential_upstream || overall_exit=1

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
