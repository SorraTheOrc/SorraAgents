#!/usr/bin/env python3
"""Generate a standup meeting report from the Herdr selection list.

Outputs a concise, human-readable standup report suitable for a 2-minute read.
Focuses on user stories rather than technical details.

The Herdr selection list is the SOLE ranking path (WL-0MTK1ILM2009QYB2):
  fetcher.ts:fetchNextItems → smart-selection.ts:selectWorkItems
  → grouping.ts:regroupWorkItems
Mirrored here via wl CLI (wl next -n N + mandatory wl list subsets)
so the report head matches downtime dispatcher's head by construction.

Default time window: 24 hours starting at 06:00 the previous day
(calendar day). Override with --startTime and --duration.

Usage:
    python3 generate_standup.py [--json] [--verbose] [--output-path <path>] [--count N]
                            [--startTime <ISO>] [--duration <hours>]
                            [--worklog-dir <path>]

Flags:
    --json           Output raw JSON data instead of the formatted report
    --verbose        Include extra detail (priority, status, stage) in output
    --output-path    Write report to a file instead of stdout
    --count N        Herdr browse window (default: from herdr config or 20)
    --startTime STR  Window start (ISO8601, e.g. 2026-09-03T06:00:00 or 2026-09-03 06:00)
    --duration H     Window duration in hours (default 24)
    --worklog-dir P  Explicit path to .worklog directory (e.g. /path/to/project/.worklog).
                     Bypasses cwd-based resolution. Also honored via WL_WORKLOG_DIR
                     env var; the value is forwarded to every wl invocation as
                     --worklog-dir <path>.
"""

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

DEFAULT_BROWSE_COUNT = 20
HERDR_CONFIG_PATH = Path.home() / ".config" / "herdr" / "worklog-plugin.json"
DEFAULT_WINDOW_HOURS = 24
DEFAULT_WINDOW_START_HOUR = 6  # 06:00 local
# Worklog directory: resolved from --worklog-dir flag or WL_WORKLOG_DIR env var.
# This ensures cwd-independent operation when launched from outside the project root.
# Evaluated at import time from env; may be overridden by --worklog-dir argv in main().
WORKLOG_DIR = os.environ.get("WL_WORKLOG_DIR", None)  # type: str | None


# ── CLI helpers ────────────────────────────────────────────────────────

def _inject_worklog_dir(cmd):
    """Prepend --worklog-dir <WORKLOG_DIR> to a wl command if WORKLOG_DIR is set.

    Handles both string and list commands. Only wl commands are decorated;
    git/read/bare shell commands are returned unchanged.
    Returns (decorated_cmd, use_shell).
    """
    if not WORKLOG_DIR:
        return cmd, isinstance(cmd, str)
    # Normalize to list for injection
    if isinstance(cmd, str):
        s = cmd.strip()
        is_wl = s.startswith("wl ") or s == "wl"
        if not is_wl:
            return cmd, True
        # wl invocation: inject flag after "wl"
        parts = s.split()
        parts.insert(1, "--worklog-dir")
        parts.insert(2, WORKLOG_DIR)
        return parts, False
    else:
        parts = list(cmd)
        if not parts or parts[0] != "wl":
            return cmd, False
        parts.insert(1, "--worklog-dir")
        parts.insert(2, WORKLOG_DIR)
        return parts, False


def run_cmd(cmd):
    """Run a shell command and return (success, stdout, stderr)."""
    try:
        decorated, use_shell = _inject_worklog_dir(cmd)
        result = subprocess.run(
            decorated,
            shell=use_shell,
            capture_output=True, text=True, timeout=30,
        )
        return result.returncode == 0, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        return False, "", "Command timed out"
    except Exception as e:
        return False, "", str(e)


def fetch_json(cmd):
    """Run a wl command and parse JSON output."""
    success, stdout, stderr = run_cmd(cmd)
    if not success:
        return None, stderr
    try:
        return json.loads(stdout), None
    except json.JSONDecodeError as e:
        return None, f"Failed to parse JSON: {e}"


# ── wl payload helpers ─────────────────────────────────────────────────

def normalize_item(raw):
    if not isinstance(raw, dict):
        return None
    iid = str(raw.get("id", ""))
    if not iid:
        return None
    return {
        "id": iid,
        "title": str(raw.get("title", "Untitled")),
        "status": str(raw.get("status", "unknown")),
        "priority": raw.get("priority"),
        "stage": raw.get("stage"),
        "parentId": raw.get("parentId"),
        "risk": raw.get("risk"),
        "effort": raw.get("effort"),
        "description": raw.get("description", ""),
        "group": raw.get("group"),
        "groupLabel": raw.get("groupLabel"),
        "needsProducerReview": raw.get("needsProducerReview"),
        "updatedAt": raw.get("updatedAt"),
    }


def extract_items(payload):
    """Mirror fetcher.ts extractItems for all known wl shapes."""
    if payload is None:
        return []
    if isinstance(payload, list):
        return [n for n in (normalize_item(x) for x in payload) if n]
    if isinstance(payload, dict):
        # results + workItem entries (wl next -n)
        if isinstance(payload.get("results"), list) and len(payload["results"]) > 0:
            out = []
            for entry in payload["results"]:
                if not isinstance(entry, dict):
                    continue
                wi = entry.get("workItem")
                if not wi:
                    continue
                if entry.get("group") is not None:
                    wi = dict(wi)
                    wi["group"] = entry["group"]
                if entry.get("groupLabel") is not None:
                    wi = dict(wi)
                    wi["groupLabel"] = entry["groupLabel"]
                n = normalize_item(wi)
                if n:
                    out.append(n)
            return out
        if isinstance(payload.get("workItems"), list) and len(payload["workItems"]) > 0:
            return [n for n in (normalize_item(x) for x in payload["workItems"]) if n]
        if isinstance(payload.get("workItem"), dict):
            n = normalize_item(payload["workItem"])
            return [n] if n else []
        if payload.get("id"):
            n = normalize_item(payload)
            return [n] if n else []
        if isinstance(payload.get("results"), list):
            return []
        if isinstance(payload.get("workItems"), list):
            return []
    return []


def get_browse_count(cli_count=None):
    if cli_count is not None:
        try:
            n = int(cli_count)
            return max(1, min(50, n))
        except:
            pass
    try:
        if HERDR_CONFIG_PATH.exists():
            data = json.loads(HERDR_CONFIG_PATH.read_text())
            n = int(data.get("browseItemCount", DEFAULT_BROWSE_COUNT))
            return max(1, min(50, n))
    except:
        pass
    return DEFAULT_BROWSE_COUNT


# ── Herdr pipeline (mirrors fetcher.ts + smart-selection + grouping) ───

def is_mandatory(item):
    return item.get("priority") == "critical" or (
        item.get("status") == "completed" and item.get("stage") == "in_review"
    )


def select_work_items(items, browse_count):
    """Mirror smart-selection.ts selectWorkItems."""
    root_only = [i for i in items if not i.get("parentId")]
    actionable = [i for i in root_only if i.get("stage") != "done"]
    criticals = [i for i in actionable if i.get("priority") == "critical"]
    reviews = [
        i for i in actionable
        if i.get("status") == "completed" and i.get("stage") == "in_review" and i.get("priority") != "critical"
    ]
    others = [i for i in actionable if not is_mandatory(i)]
    others_limit = max(0, browse_count - (len(criticals) + len(reviews)))
    return criticals + reviews + others[:others_limit]


def extract_file_paths(description: str):
    """Mirror grouping.ts extractFilePaths."""
    if not description or not description.strip():
        return []
    # Find "Key Files" header (case-insensitive, optional #/**, colon)
    m = re.search(r"^#{0,3}\s*\*{0,2}key files:?\*{0,2}\s*$", description, re.IGNORECASE | re.MULTILINE)
    if not m:
        return []
    after = description[m.end():]
    paths = []
    for line in after.split("\n"):
        trimmed = line.strip()
        if re.match(r"^#{1,3}\s", trimmed):
            break
        if re.match(r"^\*{1,2}\w.*:\*{0,2}\s*$", trimmed) and not re.match(r"^[-*]\s", trimmed):
            break
        if re.match(r"\*{0,2}key files:?\*{0,2}\s*$", trimmed, re.IGNORECASE) and not re.match(r"^[-*]\s", trimmed):
            break
        path_candidate = None
        bm = re.match(r"^[-*]\s+`([^`]+)`", trimmed)
        if bm:
            path_candidate = bm.group(1).strip()
        else:
            pm = re.match(r"^[-*]\s+([^\s]+)", trimmed)
            if pm:
                path_candidate = pm.group(1).strip()
        if not path_candidate:
            continue
        if re.match(r"^https?://", path_candidate, re.IGNORECASE):
            continue
        if "/" not in path_candidate:
            continue
        if not re.search(r"\.([a-zA-Z0-9]+)$", path_candidate):
            continue
        paths.append(path_candidate)
    return paths


def group_items_by_file_paths(items, max_groups=3):
    item_group = {}
    group_paths = {}
    restricted = set()
    next_group = 1
    for item in items:
        iid = item["id"]
        fps = item.get("filePaths", [])
        if not fps:
            item_group[iid] = next_group
            group_paths[next_group] = set()
            restricted.add(next_group)
            next_group += 1
            continue
        assigned = False
        groups_to_check = min(max_groups, next_group - 1)
        for g in range(1, groups_to_check + 1):
            if g not in group_paths:
                continue
            if g in restricted:
                continue
            if any(fp in group_paths[g] for fp in fps):
                continue
            # no conflict → assign
            for fp in fps:
                group_paths[g].add(fp)
            item_group[iid] = g
            assigned = True
            break
        if not assigned:
            ng = next_group
            group_paths[ng] = set(fps)
            item_group[iid] = ng
            next_group += 1
    return item_group


def assign_item_groups(items, max_groups=3):
    result = {}
    next_group = 1

    def remap(file_groups, start):
        uniq = sorted(set(file_groups.values()))
        m = {g: start + i for i, g in enumerate(uniq)}
        return m, len(uniq)

    def label_counter(file_groups):
        uniq = sorted(set(file_groups.values()))
        return {g: i + 1 for i, g in enumerate(uniq)}

    critical = [x for x in items if x.get("priority") == "critical"]
    if critical:
        fg = group_items_by_file_paths(critical, max_groups)
        mp, cnt = remap(fg, next_group)
        lc = label_counter(fg)
        for iid, g in fg.items():
            result[iid] = {"group": mp[g], "groupLabel": f"Critical Group {lc[g]}"}
        next_group += cnt

    group_n = [x for x in items if x.get("priority") != "critical" and x.get("stage") in ("in_progress", "plan_complete", "intake_complete")]
    if group_n:
        fg = group_items_by_file_paths(group_n, max_groups)
        mp, cnt = remap(fg, next_group)
        lc = label_counter(fg)
        for iid, g in fg.items():
            result[iid] = {"group": mp[g], "groupLabel": f"Group {lc[g]}"}
        next_group += cnt

    idea = [x for x in items if x.get("priority") != "critical" and x.get("stage") == "idea"]
    if idea:
        for it in idea:
            result[it["id"]] = {"group": next_group, "groupLabel": "Idea"}
        next_group += 1

    other = [x for x in items if x.get("priority") != "critical" and x.get("stage") not in ("in_review", "in_progress", "plan_complete", "intake_complete", "idea")]
    if other:
        for it in other:
            result[it["id"]] = {"group": next_group, "groupLabel": "Other"}
        next_group += 1

    in_review = [x for x in items if x.get("priority") != "critical" and x.get("stage") == "in_review"]
    if in_review:
        for it in in_review:
            result[it["id"]] = {"group": next_group, "groupLabel": "In Review"}
        next_group += 1

    return result


WITHIN_STAGE = {"in_progress": 0, "plan_complete": 1, "intake_complete": 2}
WITHIN_PRIO = {"high": 0, "medium": 1, "low": 2}
DEFAULT_PRIO = 1

def compare_items(a, b):
    sa = WITHIN_STAGE.get(a.get("stage") or "", 3)
    sb = WITHIN_STAGE.get(b.get("stage") or "", 3)
    if sa != sb:
        return sa - sb
    pa = WITHIN_PRIO.get((a.get("priority") or "").lower(), DEFAULT_PRIO)
    pb = WITHIN_PRIO.get((b.get("priority") or "").lower(), DEFAULT_PRIO)
    if pa != pb:
        return pa - pb
    return (a["id"] > b["id"]) - (a["id"] < b["id"])


def regroup_work_items(items, max_groups=3):
    groupable = [
        {"id": it["id"], "stage": it.get("stage"), "priority": it.get("priority"),
         "filePaths": extract_file_paths(it.get("description") or "")}
        for it in items
    ]
    gmap = assign_item_groups(groupable, max_groups)
    # sort by group then within-group order
    def sort_key(it):
        g = gmap.get(it["id"], {}).get("group", 9999)
        stage_ord = WITHIN_STAGE.get(it.get("stage") or "", 3)
        prio_ord = WITHIN_PRIO.get((it.get("priority") or "").lower(), DEFAULT_PRIO)
        return (g, stage_ord, prio_ord, it["id"])
    sorted_items = sorted(items, key=sort_key)
    for it in sorted_items:
        a = gmap.get(it["id"])
        if a:
            it["group"] = a["group"]
            it["groupLabel"] = a["groupLabel"]
    return sorted_items


def merge_unique_by_id(*arrays):
    seen = set()
    out = []
    for arr in arrays:
        for it in arr:
            if it["id"] not in seen:
                seen.add(it["id"])
                out.append(it)
    return out


def fetch_herdr_selection_list(browse_count=None):
    """Fetch the Herdr selection list (sole ranking path)."""
    count = get_browse_count(browse_count)
    # wl next -n N --include-in-progress
    nxt_data, nxt_err = fetch_json(f"wl next -n {count} --include-in-progress --json")
    wl_next_items = extract_items(nxt_data) if nxt_data else []

    # Mandatory subsets via wl list (fetchMandatorySubsets)
    crit_data, crit_err = fetch_json("wl list --priority critical --root-only --json")
    crit_items = extract_items(crit_data) if crit_data else []

    rev_data, rev_err = fetch_json("wl list --status completed --stage in_review --root-only --json")
    rev_items = extract_items(rev_data) if rev_data else []

    merged = merge_unique_by_id(wl_next_items, crit_items, rev_items)
    selected = select_work_items(merged, count)
    regrouped = regroup_work_items(selected)
    errors = {}
    if nxt_err:
        errors["wl_next"] = nxt_err
    if crit_err and not crit_data:
        errors["critical"] = crit_err
    if rev_err and not rev_data:
        errors["in_review"] = rev_err
    return regrouped, errors


def get_in_progress_items():
    """Fetch items currently in progress (for Yesterday calc fallback)."""
    data, err = fetch_json("wl in_progress --json")
    if data and data.get("success"):
        return [n for n in (normalize_item(x) for x in data.get("workItems", [])) if n], None
    # also try wl list --status in_progress
    if data is None:
        data2, err2 = fetch_json("wl list --status in_progress --json")
        if data2:
            return extract_items(data2), None
        return [], err or err2 or "No in_progress items"
    return [], err or "No in_progress items"


# ── TTS helpers ──────────────────────────────────────────────────────

def ordinal(n):
    """Return ordinal string: 1st, 2nd, 3rd, 4th, etc."""
    n = int(n)
    if 11 <= (n % 100) <= 13:
        return f"{n}th"
    suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def format_date_tts(dt):
    """Format a datetime as TTS-friendly text: '5th September 2026'"""
    months = [
        "January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December"
    ]
    return f"{ordinal(dt.day)} {months[dt.month - 1]} {dt.year}"


def format_time_tts(dt):
    """Format a time as TTS-friendly: '6:00 am' or '6:30 pm'"""
    hour = dt.hour
    minute = dt.minute
    period = "am" if hour < 12 else "pm"
    display_hour = hour % 12
    if display_hour == 0:
        display_hour = 12
    if minute == 0:
        return f"{display_hour}:00 {period}"
    return f"{display_hour}:{minute:02d} {period}"


def format_datetime_tts(dt):
    """Full TTS datetime: '5th September 2026 6:00 am'"""
    return f"{format_date_tts(dt)} {format_time_tts(dt)}"


# ── Time window ─────────────────────────────────────────────────────

def parse_start_time(s: str):
    """Parse --startTime into a naive local datetime. Supports ISO8601 and common variants."""
    s = s.strip()
    # allow 'Z' suffix
    s_norm = s.replace("Z", "")
    # try fromisoformat after normalizing space/T
    for fmt in (
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%Y/%m/%d",
        "%H:%M:%S",
        "%H:%M",
    ):
        try:
            dt = datetime.strptime(s_norm, fmt)
            # date-only → treat as midnight (caller decides hour)
            if fmt == "%Y-%m-%d" or fmt == "%Y/%m/%d":
                dt = dt.replace(hour=DEFAULT_WINDOW_START_HOUR)
            elif fmt in ("%H:%M", "%H:%M:%S"):
                today = datetime.now().replace(hour=dt.hour, minute=dt.minute, second=getattr(dt, 'second', 0), microsecond=0)
                # if time-only and today is in future, use yesterday
                if today > datetime.now():
                    today -= timedelta(days=1)
                return today
            return dt
        except ValueError:
            continue
    # fallback: fromisoformat
    try:
        iso = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is not None:
            dt = dt.astimezone().replace(tzinfo=None)
        return dt
    except Exception:
        pass
    raise ValueError(f"Cannot parse --startTime '{s}'. Use ISO8601 e.g. 2026-09-03T06:00:00")


def compute_window(start_time_str=None, duration_hours=None, now=None):
    """Return (window_start, window_end) as naive local datetimes."""
    if now is None:
        now = datetime.now()
    if start_time_str is not None:
        ws = parse_start_time(start_time_str)
        hours = float(duration_hours) if duration_hours is not None else DEFAULT_WINDOW_HOURS
        we = ws + timedelta(hours=hours)
        return ws, we
    # default: calendar previous day 06:00 -> today 06:00
    today6 = now.replace(hour=DEFAULT_WINDOW_START_HOUR, minute=0, second=0, microsecond=0)
    window_start = today6 - timedelta(days=1)
    hours = float(duration_hours) if duration_hours is not None else DEFAULT_WINDOW_HOURS
    window_end = window_start + timedelta(hours=hours)
    return window_start, window_end


def parse_updated_at(s):
    if not s:
        return None
    try:
        iso = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is not None:
            dt = dt.astimezone().replace(tzinfo=None)
        return dt
    except Exception:
        return None


def in_window(item, window_start, window_end):
    ua = parse_updated_at(item.get("updatedAt") or item.get("createdAt") or "")
    if ua is None:
        return False
    return window_start <= ua < window_end


def priority_sort_key(item):
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    p = (item.get("priority") or "").lower()
    return order.get(p, 4)


def extract_user_story(description):
    if not description:
        return ""
    for line in description.split("\n"):
        s = line.strip()
        if s.startswith("- As a") or s.startswith("**As a"):
            return s.lstrip("- ").lstrip("*").rstrip("*").strip()[:200]
        if s.startswith("As a"):
            return s[:200]
    return ""


def extract_problem(description):
    if not description:
        return ""
    lines = description.split("\n")
    for i, line in enumerate(lines):
        if "problem statement" in line.lower():
            for j in range(i + 1, min(i + 5, len(lines))):
                s = lines[j].strip()
                if s and not s.startswith("#") and not s.startswith("-"):
                    return s[:200]
    return ""


def format_item(item, verbose=False):
    title = item.get("title", "Untitled")
    item_id = item.get("id", "UNKNOWN")
    desc = item.get("description", "")
    status = item.get("status", "unknown")
    us = extract_user_story(desc)
    if us:
        summary = us
    else:
        prob = extract_problem(desc)
        if prob:
            summary = prob
        else:
            summary = ""
            for line in desc.split("\n"):
                s = line.strip()
                if s and not s.startswith("#") and len(s) > 20:
                    summary = s[:200]
                    break
    line = f"- **{title}** ({item_id})"
    if summary:
        line += f" — {summary}"
    if verbose:
        line += f" [{status}]"
    return line


def fetch_blockers(items):
    """Check wl dep list for each item; return blocker strings."""
    blockers = []
    for it in items:
        iid = it["id"]
        data, err = fetch_json(f"wl dep list {iid} --json")
        if not data:
            continue
        deps = []
        if isinstance(data, dict):
            if isinstance(data.get("dependencies"), list):
                deps = data["dependencies"]
            elif isinstance(data.get("workItems"), list):
                deps = data["workItems"]
            elif isinstance(data.get("blockedBy"), list):
                deps = data["blockedBy"]
        # blocked status also counts
        if it.get("status") == "blocked" and not deps:
            blockers.append(f"- **{it.get('title')}** ({iid}): status is blocked")
            continue
        for d in deps:
            if isinstance(d, dict):
                did = d.get("id", "?")
                dtitle = d.get("title", did)
                dstatus = d.get("status", "")
                # consider open/in_progress blockers as real blockers
                if dstatus in ("open", "in_progress", "blocked"):
                    blockers.append(f"- **{it.get('title')}** ({iid}) blocked by **{dtitle}** ({did})")
                    break
    return blockers


def fetch_all_completions_in_window(window_start, window_end):
    """Fetch ALL completed/in_review items whose updatedAt falls within window (not limited to Herdr list)."""
    data, err = fetch_json("wl list --status completed --stage in_review --json")
    if not data:
        return []
    items = extract_items(data)
    return [i for i in items if in_window(i, window_start, window_end)]


def _git_dir_flag():
    """Return git -C flag list for WORKLOG_DIR's project root, if set."""
    if WORKLOG_DIR:
        # WORKLOG_DIR is the .worklog dir; project root is its parent.
        root = str(Path(WORKLOG_DIR).resolve().parent)
        return ["-C", root]
    return []


def _run_git(args):
    """Run a git command (args without 'git') anchored to WORKLOG_DIR's root."""
    git_prefix = _git_dir_flag()
    cmd = ["git"] + git_prefix + args
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return result.returncode == 0, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        return False, "", "Command timed out"
    except Exception as e:
        return False, "", str(e)


def get_snapshot_before(window_start):
    """Find the latest full snapshot commit before window_start. Returns hash or None."""
    if window_start is None:
        return None
    try:
        date_str = window_start.strftime("%Y-%m-%dT%H:%M:%S")
        success, stdout, stderr = _run_git(["log", "--all", f'--before={date_str}', "--format=%H", "-n", "20", "--", ".worklog/worklog-data.jsonl"])
        if not success or not stdout.strip():
            return None
        hashes = [h.strip() for h in stdout.strip().splitlines() if h.strip()]
        for h in hashes:
            s2, out2, _ = _run_git(["show", f"{h}:.worklog/worklog-data.jsonl"])
            head_line = out2.splitlines()[0] if out2 else ""
            if s2 and head_line:
                if '"kind":"full"' in head_line or '"kind": "full"' in head_line:
                    return h
                if "__worklog_sync__" not in head_line:
                    return h
        return None
    except Exception:
        return None


def load_snapshot_map(snapshot_hash):
    """Load snapshot work items into id -> (status, stage) map."""
    success, stdout, stderr = _run_git(["show", f"{snapshot_hash}:.worklog/worklog-data.jsonl"])
    if not success or not stdout:
        return {}
    before = {}
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except:
            continue
        if rec.get("__worklog_sync__"):
            continue
        if rec.get("type") == "workitem" and rec.get("data"):
            d = rec["data"]
            iid = d.get("id")
            if iid:
                before[iid] = (d.get("status"), d.get("stage"))
        elif rec.get("id"):
            iid = rec.get("id")
            if iid:
                before[iid] = (rec.get("status"), rec.get("stage"))
    return before


def detect_regressions(window_start, window_end):
    """Items that were completed/in_review before window_start but moved back during window."""
    if window_start is None or window_end is None:
        return []
    snapshot_hash = get_snapshot_before(window_start)
    if not snapshot_hash:
        return []
    before = load_snapshot_map(snapshot_hash)
    if not before:
        return []
    data, err = fetch_json("wl list --json")
    if not data:
        return []
    current = extract_items(data)
    regs = []
    for item in current:
        iid = item["id"]
        prev = before.get(iid)
        if not prev:
            continue
        prev_status, prev_stage = prev
        if prev_status == "completed" and prev_stage == "in_review":
            cur_status = item.get("status")
            cur_stage = item.get("stage")
            is_regressed = not (cur_status == "completed" and cur_stage == "in_review")
            if is_regressed and in_window(item, window_start, window_end):
                # Flag if now in an actionable open state
                if cur_status in ("open", "in_progress", "in-progress", "blocked") or cur_stage in ("idea", "intake_complete", "plan_complete", "in_progress") or cur_status != "completed" or cur_stage != "in_review":
                    regs.append(item)
    seen = set()
    out = []
    for it in sorted(regs, key=lambda x: x["id"]):
        if it["id"] not in seen:
            seen.add(it["id"])
            out.append(it)
    return out


def _format_item_tts(item):
    """TTS-friendly single line: 'Title — user story / summary' without ID brackets."""
    title = (item.get("title") or "Untitled").strip()
    desc = item.get("description", "") or ""
    us = extract_user_story(desc)
    if us:
        summary = us
    else:
        prob = extract_problem(desc)
        if prob:
            summary = prob
        else:
            summary = ""
            for line in desc.split("\n"):
                s = line.strip()
                if s and not s.startswith("#") and len(s) > 20:
                    summary = s[:200]
                    break
    # Clean markdown emphasis for TTS
    summary = summary.lstrip("- *").strip()
    if summary:
        return f"  - {title} — {summary}"
    return f"  - {title}"


def _format_blocker_tts(blocker_raw):
    """Convert raw blocker string to TTS-friendly 'Blocked on X, because Y' if possible."""
    # blocker_raw already looks like '- **Title** (ID) blocked by **Other** (ID)' or '- **Title** (ID): status is blocked'
    # Produce a spoken form without IDs and markdown.
    import re as _re
    # Strip markdown and IDs for TTS
    s = _re.sub(r"\*\*", "", blocker_raw)
    s = _re.sub(r"\s*\([A-Z0-9\-]+\)", "", s)
    s = s.lstrip("- ").strip()
    if "blocked by" in s:
        parts = s.split("blocked by")
        return f"  - Blocked on {parts[0].strip()}, because blocked by {parts[1].strip()}"
    if "status is blocked" in s:
        title = s.replace("status is blocked", "").replace(":", "").strip()
        return f"  - Blocked on {title}, because its status is blocked"
    return f"  - {s}"


def generate_report(verbose=False, browse_count=None, window_start=None, window_end=None):
    herdr_items, fetch_errors = fetch_herdr_selection_list(browse_count)
    herdr_items_sorted = sorted(herdr_items, key=lambda x: (x.get("group", 999), priority_sort_key(x)))

    # Yesterday: ALL completed/in_review items whose updatedAt falls within window (not limited to Herdr list).
    if window_start is not None and window_end is not None:
        try:
            fetched = fetch_all_completions_in_window(window_start, window_end)
            yesterday_items = sorted(fetched, key=lambda x: (x.get("updatedAt") or "", x["id"]))
        except Exception:
            yesterday_all = [i for i in herdr_items_sorted if i.get("status") == "completed" and i.get("stage") == "in_review"]
            yesterday_items = [i for i in yesterday_all if in_window(i, window_start, window_end)]
    else:
        yesterday_items = [i for i in herdr_items_sorted if i.get("status") == "completed" and i.get("stage") == "in_review"]

    # Today: actionable open/in_progress/blocked items from Herdr list (the head)
    # status values use hyphen "in-progress" from wl (normalize preserves it)
    def _is_actionable(s):
        return s in ("open", "in_progress", "in-progress", "blocked")
    today_items = [i for i in herdr_items_sorted if _is_actionable(i.get("status")) and i.get("stage") != "done"]
    # If Herdr list is empty, today is empty (no fallback ranking per AC2)

    # Split today into critical (always included) and additional focus (fill to 5)
    today_critical_items = [i for i in today_items if (i.get("priority") or "").lower() == "critical"]
    today_non_critical = [i for i in today_items if (i.get("priority") or "").lower() != "critical"]
    if len(today_critical_items) >= 5:
        today_additional_items = []
    else:
        needed = 5 - len(today_critical_items)
        today_additional_items = today_non_critical[:needed]

    # Blockers: among today items, check deps (cap to avoid many wl calls)
    blocker_lines = fetch_blockers(today_items[:10])
    for it in today_items:
        if it.get("status") == "blocked" and not any(it["id"] in b for b in blocker_lines):
            blocker_lines.append(f"- **{it.get('title')}** ({it['id']}): status is blocked")

    # Regressions: items that were completed/in_review before window but moved back during window
    try:
        regressions = detect_regressions(window_start, window_end)
    except Exception:
        regressions = []

    # TTS-friendly formatted variants
    yesterday_tts = [_format_item_tts(i) for i in yesterday_items]
    critical_tts = [_format_item_tts(i) for i in today_critical_items]
    additional_tts = [_format_item_tts(i) for i in today_additional_items]
    blockers_tts = [_format_blocker_tts(b) for b in blocker_lines]
    regressions_tts = [_format_item_tts(i) for i in regressions]

    return {
        "herdr_count": len(herdr_items_sorted),
        "herdr": [format_item(i, verbose) for i in herdr_items_sorted],
        "yesterday": [format_item(i, verbose) for i in yesterday_items],
        "yesterday_count": len(yesterday_items),
        "yesterday_tts": yesterday_tts,
        "today": [format_item(i, verbose) for i in today_items],
        "today_count": len(today_items),
        "today_critical": [format_item(i, verbose) for i in today_critical_items],
        "today_critical_count": len(today_critical_items),
        "today_critical_tts": critical_tts,
        "today_additional": [format_item(i, verbose) for i in today_additional_items],
        "today_additional_count": len(today_additional_items),
        "today_additional_tts": additional_tts,
        "regressions": [format_item(i, verbose) for i in regressions],
        "regressions_count": len(regressions),
        "regressions_tts": regressions_tts,
        "blockers": blocker_lines,
        "blockers_tts": blockers_tts,
        "window": {
            "start": window_start.isoformat() if window_start else None,
            "end": window_end.isoformat() if window_end else None,
        },
        # compat keys for older callers/tests
        "queue": [format_item(i, verbose) for i in herdr_items_sorted],
        "queue_count": len(herdr_items_sorted),
        "in_progress": [],
        "in_progress_count": 0,
        "errors": fetch_errors,
    }


def format_report(data, browse_count=None):
    now = datetime.now()
    lines = [f"## Standup Report ({format_date_tts(now)})", ""]

    # Prefer TTS-friendly fields if present; fall back to legacy fields for back-compat
    yesterday_lines = data.get("yesterday_tts") if data.get("yesterday_tts") is not None else data.get("yesterday", [])
    critical_lines = data.get("today_critical_tts") if data.get("today_critical_tts") is not None else data.get("today_critical", [])
    additional_lines = data.get("today_additional_tts") if data.get("today_additional_tts") is not None else data.get("today_additional", [])
    blocker_tts_lines = data.get("blockers_tts") if data.get("blockers_tts") is not None else data.get("blockers", [])

    lines.append("### Yesterday I completed work on...")
    lines.append("")
    if yesterday_lines:
        lines.extend(yesterday_lines)
    else:
        lines.append("  - Nothing completed in this window.")
    lines.append("")

    lines.append("### Open critical items")
    lines.append("")
    if critical_lines:
        lines.extend(critical_lines)
    else:
        lines.append("  - No open critical items.")
    lines.append("")

    lines.append("### Additional focus items for today")
    lines.append("")
    if additional_lines:
        lines.extend(additional_lines)
    else:
        # If critical already consumed 5+, note that explicitly; otherwise no additional
        crit_count = data.get("today_critical_count", len(critical_lines))
        if crit_count >= 5:
            lines.append("  - No additional items — my focus today is entirely on critical work.")
        else:
            lines.append("  - No additional focus items scheduled.")
    lines.append("")

    # Regressions section (before Blockers)
    regressions_lines = data.get("regressions_tts") if data.get("regressions_tts") is not None else data.get("regressions", [])
    lines.append("### Regressions")
    lines.append("")
    if regressions_lines:
        lines.extend(regressions_lines)
    else:
        lines.append("  - No regressions — no items slipped back from completed.")
    lines.append("")

    lines.append("### Blockers")
    lines.append("")
    if blocker_tts_lines:
        lines.extend(blocker_tts_lines)
    else:
        lines.append("  - No immediate blockers.")
    # surface fetch errors quietly if any
    if data.get("errors"):
        for k, v in data["errors"].items():
            if v:
                lines.append("")
                lines.append(f"_Note: {k} fetch warning: {v}_")
                break
    return "\n".join(lines)


def main():
    global WORKLOG_DIR
    args = sys.argv[1:]

    # --worklog-dir: explicit project worklog (or WL_WORKLOG_DIR env). Apply
    # immediately so every subsequent wl call (including the first wl next)
    # targets the right database regardless of cwd.
    if "--worklog-dir" in args:
        _i = args.index("--worklog-dir")
        if _i + 1 < len(args) and not args[_i + 1].startswith("--"):
            WORKLOG_DIR = args[_i + 1]
            # normalize bare project-root path to <root>/.worklog
            try:
                _p = Path(WORKLOG_DIR)
                if _p.exists() and not _p.name == ".worklog":
                    _cand = _p / ".worklog"
                    if _cand.exists() and _cand.is_dir():
                        WORKLOG_DIR = str(_cand.resolve())
                else:
                    WORKLOG_DIR = str(_p.resolve()) if _p.exists() else WORKLOG_DIR
            except Exception:
                pass

    json_output = "--json" in args
    verbose = "--verbose" in args
    output_path = None
    browse_count = None
    start_time_str = None
    duration_hours = None

    if "--output-path" in args:
        idx = args.index("--output-path")
        if idx + 1 < len(args):
            output_path = args[idx + 1]
    if "--count" in args:
        idx = args.index("--count")
        if idx + 1 < len(args):
            try:
                browse_count = int(args[idx + 1])
            except:
                pass
    # also support -n
    if "-n" in args and browse_count is None:
        idx = args.index("-n")
        if idx + 1 < len(args):
            try:
                browse_count = int(args[idx + 1])
            except:
                pass
    if "--startTime" in args:
        idx = args.index("--startTime")
        if idx + 1 < len(args):
            start_time_str = args[idx + 1]
    if "--duration" in args:
        idx = args.index("--duration")
        if idx + 1 < len(args):
            duration_hours = args[idx + 1]

    # --help
    if "--help" in args or "-h" in args:
        print(__doc__)
        return 0

    try:
        window_start, window_end = compute_window(start_time_str, duration_hours)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    data = generate_report(verbose=verbose, browse_count=browse_count, window_start=window_start, window_end=window_end)

    if json_output:
        output = json.dumps(data, indent=2)
    else:
        output = format_report(data, browse_count)

    if output_path:
        with open(output_path, "w") as f:
            f.write(output)
        print(f"Report written to {output_path}", file=sys.stderr)
    else:
        print(output)

    return 0


if __name__ == "__main__":
    sys.exit(main())
