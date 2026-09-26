#!/usr/bin/env python3
"""
render_report.py — Canonical end-of-session report renderer.

Produces a standardized, scannable markdown report at the end of a skill
session.  The report is produced by calling the public functions defined
below; the CLI entry-point (``--json`` mode) reads ``wl show <id> --json``
output when invoked directly.

Usage
-----
    # As a library (preferred):
    from render_report import render_report, render_report_from_workitem
    report = render_report(
        skill_name="implement",
        work_item_id="SA-0MSJ082OY003IQ8S",
        title="Standardize skill session end-of-session reporting",
        headline="Implemented the report helper and wired it into all skills.",
        acceptance_criteria=[
            ("1", "Report helper exists", "file present", "met"),
            ("2", "All skills wired", "grep verified", "met"),
        ],
        metadata={
            "Type": "🔷 feature",
            "Priority": "⭐ high",
            "Status": "🔄 in-progress",
            "Stage": "🛠️ in_progress",
            "Risk": "⚠️ medium",
            "Effort": "🐕 M",
            "Children": "👥 4",
            "Audit": "✅ passed",
        },
        producer_actions="Review the wiring grep output.",
        notes="Icons sourced from ContextHub.",
        next_action="ship",
    )
    print(report)

    # CLI mode: read a work-item JSON blob from stdin or a file, render
    # a minimal report from the work-item fields.
    $ echo '{"id":"SA-0XXX","title":"Foo"}' | python3 render_report.py --json

ContextHub Icon Mappings
-------------------------
Icon values are sourced from the ContextHub canonical set
(../ContextHub/src/icons.ts + docs/icons-design.md).  See the
``render_metadata()`` function for the mapping table.

Public API
----------
"""

import json
import re
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

# Ensure ``skill/`` is on sys.path so shared modules are importable
# regardless of whether this script is run from cwd, worktree, or as module.
_SKILLS_ROOT = Path(__file__).resolve().parents[2]
if str(_SKILLS_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILLS_ROOT))

try:
    from shared.status_lifecycle import (
        worklog_dir_flag,  # type: ignore[import-not-found]
    )
except ImportError:
    def worklog_dir_flag(*_a, **_kw):  # type: ignore[no-redef]
        return []

# ─── Icon mappings (sourced from ContextHub) ───────────────────────────

# See: ../ContextHub/src/icons.ts
PRIORITY_ICONS = {
    "critical": "\U0001F6A8",   # 🚨
    "high":     "\U00002B50",    # ⭐
    "medium":   "\U0001F4CB",   # 📋
    "low":      "\U0001F422",   # 🐢
}
PRIORITY_FALLBACK = {
    "critical": "[CRIT]",
    "high":     "[HIGH]",
    "medium":   "[MED]",
    "low":      "[LOW]",
}

STATUS_ICONS = {
    "open":          "\U0001F513",   # 🔓
    "in-progress":   "\U0001F504",   # 🔄
    "completed":     "\u2714\ufe0f",  # ✔️
    "blocked":       "\u26d4",       # ⛔
    "deleted":       "\U0001f5d1\ufe0f",  # 🗑️
    "input_needed":  "\U0001f4ac",   # 💬
}
STATUS_FALLBACK = {
    "open":          "[OPEN]",
    "in-progress":   "[INPR]",
    "completed":     "[DONE]",
    "blocked":       "[BLKD]",
    "deleted":       "[DEL]",
    "input_needed":  "[HELP]",
}

STAGE_ICONS = {
    "idea":            "\U0001f4a1",          # 💡
    "intake_complete": "\U0001f4e5",          # 📥
    "plan_complete":   "\U0001f4cb",          # 📋
    "in_progress":     "\U0001f6e0\ufe0f",    # 🛠️
    "in_review":       "\U0001f50d",          # 🔍
    "done":            "\U0001f3c1",          # 🏁
}
STAGE_FALLBACK = {
    "idea":            "[IDEA]",
    "intake_complete": "[INTAKE]",
    "plan_complete":   "[PLAN]",
    "in_progress":     "[PROG]",
    "in_review":       "[REVIEW]",
    "done":            "[DONE]",
}

RISK_ICONS = {
    "low":    "\U0001f331",  # 🌱
    "medium": "\u26a0\ufe0f", # ⚠️
    "high":   "\U0001f525",  # 🔥
    "severe": "\U0001f6a8",  # 🚨
}
RISK_FALLBACK = {
    "low":    "[LOW]",
    "medium": "[MED]",
    "high":   "[HIGH]",
    "severe": "[SEV]",
}

EFFORT_ICONS = {
    "xs":        "\U0001f41c",  # 🐜
    "s":         "\U0001f407",  # 🐇
    "m":         "\U0001f415",  # 🐕
    "l":         "\U0001f418",  # 🐘
    "xl":        "\U0001f40b",  # 🐋
    "extra small": "\U0001f41c",
    "small":       "\U0001f407",
    "medium":      "\U0001f415",
    "large":       "\U0001f418",
    "extra large": "\U0001f40b",
    "xlarge":      "\U0001f40b",
}
EFFORT_FALLBACK = {
    "xs":        "[XS]",
    "s":         "[S]",
    "m":         "[M]",
    "l":         "[L]",
    "xl":        "[XL]",
    "extra small": "[XS]",
    "small":       "[S]",
    "medium":      "[M]",
    "large":       "[L]",
    "extra large": "[XL]",
    "xlarge":      "[XL]",
}

AUDIT_ICONS = {
    "yes":     "\u2705",   # ✅
    "no":      "\u274c",   # ❌
    "unknown": "\u2754",   # ❔
}
AUDIT_FALLBACK = {
    "yes":     "[YES]",
    "no":      "[NO]",
    "unknown": "[UNKN]",
}

# ─── Icon lookup helpers ───────────────────────────────────────────────

def _icon(lookup_table, fallback_table, key, default_emoji=""):
    """Return (emoji, label) for a given key."""
    k = (key or "").lower().strip()
    icon = lookup_table.get(k, default_emoji)
    if icon:
        return icon, k
    return default_emoji, k or ""


def _fallback(lookup_table, key, default="[?]", label=""):
    """Return the bracketed-text fallback."""
    k = (key or "").lower().strip()
    return lookup_table.get(k, default), k or label or default


# ─── Metadata rendering ────────────────────────────────────────────────

#: First line of a legacy ``workItem.audit.text`` that states the verdict.
_READY_TO_CLOSE_RE = re.compile(
    r"^\s*Ready to close:\s*(yes|no)\b", re.IGNORECASE,
)
#: Legacy ``workItem.audit.status`` values that imply a passing verdict.
_LEGACY_PASS_STATUSES = {"complete", "completed", "passed", "pass"}
#: Legacy ``workItem.audit.status`` values that imply a failing verdict.
_LEGACY_FAIL_STATUSES = {"failed", "fail"}


def _coerce_bool(value) -> bool:
    """Coerce a ``readyToClose`` value to a bool (tolerates string forms)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "1"}
    if isinstance(value, (int, float)):
        return value != 0
    return bool(value)


def _legacy_audit_verdict(audit: Mapping) -> str | None:
    """Derive a verdict from the legacy ``workItem.audit`` mirror.

    The explicit ``Ready to close: Yes|No`` line in ``audit.text`` (when
    present) is authoritative; otherwise a ``status`` of ``Complete``
    (or similar) is treated as a pass. Returns ``None`` when undecidable.
    """
    text = audit.get("text")
    if isinstance(text, str):
        stripped = text.strip()
        if stripped:
            first_line = stripped.splitlines()[0]
            match = _READY_TO_CLOSE_RE.match(first_line)
            if match:
                return "yes" if match.group(1).lower() == "yes" else "no"
    status = audit.get("status")
    if isinstance(status, str):
        normalised = status.strip().lower()
        if normalised in _LEGACY_PASS_STATUSES:
            return "yes"
        if normalised in _LEGACY_FAIL_STATUSES:
            return "no"
    return None


def _audit_verdict(work_item: dict, audit_result: Mapping | None) -> str:
    """Normalise the audit verdict to ``yes`` / ``no`` / ``unknown``.

    Precedence:
      1. the structured top-level ``auditResult.readyToClose`` (authoritative);
      2. a legacy boolean ``workItem.auditResult`` (simple dict callers);
      3. the legacy ``workItem.audit`` mirror (``status`` / ``Ready to close:``
         line);
      4. ``unknown`` (rendered as ``not run``).
    """
    if isinstance(audit_result, Mapping) and "readyToClose" in audit_result:
        return "yes" if _coerce_bool(audit_result.get("readyToClose")) else "no"
    legacy_boolean = work_item.get("auditResult")
    if isinstance(legacy_boolean, bool):
        return "yes" if legacy_boolean else "no"
    audit = work_item.get("audit")
    if isinstance(audit, Mapping):
        verdict = _legacy_audit_verdict(audit)
        if verdict is not None:
            return verdict
    return "unknown"


def render_metadata(
    work_item: dict,
    children: list | None = None,
    audit_result: Mapping | None = None,
) -> dict:
    """Populate the Meta-Data block from a ``wl show --json`` dict.

    Args:
        work_item: The ``workItem`` object from the ``wl show --json`` payload.
        children: The payload's ``children`` array (for the child count).
        audit_result: The payload's **top-level** ``auditResult`` object
            (``readyToClose``); ``None`` when absent or unaudited. Legacy
            callers that pass only *work_item* keep working via the
            ``workItem.auditResult`` / ``workItem.audit`` fallbacks.

    Returns a dict keyed by field name with ``<icon> <value>`` strings.
    """
    metadata = {}

    # Type
    issue_type = work_item.get("issueType", "task")
    type_icon = "\U0001f537"  # 🔷 blue diamond (generic)
    metadata["Type"] = f"{type_icon} {issue_type}"

    # Priority
    p_icon, p_label = _icon(PRIORITY_ICONS, PRIORITY_FALLBACK,
                             work_item.get("priority"))
    metadata["Priority"] = f"{p_icon} {p_label}"

    # Status
    s_icon, s_label = _icon(STATUS_ICONS, STATUS_FALLBACK,
                             work_item.get("status"))
    metadata["Status"] = f"{s_icon} {s_label}"

    # Stage
    st_icon, st_label = _icon(STAGE_ICONS, STAGE_FALLBACK,
                               work_item.get("stage"))
    metadata["Stage"] = f"{st_icon} {st_label}"

    # Risk
    r_icon, r_label = _icon(RISK_ICONS, RISK_FALLBACK,
                             work_item.get("risk"))
    metadata["Risk"] = f"{r_icon} {r_label}" if r_icon else f"❓ {r_label or 'unknown'}"

    # Effort
    e_icon, e_label = _icon(EFFORT_ICONS, EFFORT_FALLBACK,
                             work_item.get("effort"))
    metadata["Effort"] = f"{e_icon} {e_label}" if e_icon else f"❓ {e_label or 'unknown'}"

    # Children
    child_count = len(children) if children else 0
    children_icon = "\U0001f465"  # 👥
    metadata["Children"] = f"{children_icon} {child_count}"

    # Audit
    a_key = _audit_verdict(work_item, audit_result)
    a_icon = AUDIT_ICONS.get(a_key, "")
    a_label = {"yes": "passed", "no": "failed", "unknown": "not run"}[a_key]
    metadata["Audit"] = f"{a_icon} {a_label}"

    return metadata


# ─── Report rendering ──────────────────────────────────────────────────

def render_report(
    skill_name: str,
    work_item_id: str,
    title: str,
    headline: str,
    acceptance_criteria: list,
    metadata: dict,
    producer_actions=None,
    notes=None,
    next_action: str = "review",
) -> str:
    """Render the full canonical end-of-session report.

    Parameters
    ----------
    skill_name : str
        Name of the skill that produced the report (e.g. "implement").
    work_item_id : str
        The Worklog work-item ID (e.g. "SA-0MSJ082OY003IQ8S").
    title : str
        Work-item title.
    headline : str
        1–3 sentence summary of what was done.
    acceptance_criteria : list of (str, str, str, str)
        Tuples of (ac#, description, metric, verdict).
    metadata : dict
        Pre-computed metadata mapping (from ``render_metadata()``).
    producer_actions : str or None
        Actions for the producer. Defaults to "None needed".
    notes : str or None
        Freeform agent commentary.
    next_action : str
        The next action (e.g. "review", "plan", "ship").

    Returns
    -------
    str
        The rendered markdown report.
    """
    lines = []

    # Header
    lines.append(f"# Completed {skill_name}")
    lines.append("")
    lines.append(f"**{title}** ({work_item_id})")
    lines.append("")

    # Headline
    if headline:
        lines.append(headline)
        lines.append("")

    # Acceptance Criteria
    lines.append("## Acceptance Criteria")
    lines.append("")
    lines.append("| AC# | Description | Metric | Verdict |")
    lines.append("|-----|-------------|--------|---------|")
    if not acceptance_criteria:
        lines.append("| \u2014 | No acceptance criteria supplied | \u2014 | \u2014 |")
    else:
        for ac_num, desc, metric, verdict in acceptance_criteria:
            lines.append(f"|{ac_num}|{desc}|{metric}|{verdict}|")
    lines.append("")

    # Meta-Data
    lines.append("## Meta-Data")
    lines.append("")
    for key in ["Type", "Priority", "Status", "Stage", "Risk", "Effort",
                "Children", "Audit"]:
        value = metadata.get(key, f"❓ {key.lower()}")
        lines.append(f"- **{key}:** {value}")
    lines.append("")

    # Producer Actions
    lines.append("## Producer Actions")
    lines.append("")
    if not producer_actions or not producer_actions.strip():
        lines.append("None needed")
    else:
        lines.append(producer_actions)
    lines.append("")

    # Notes
    lines.append("## Notes")
    lines.append("")
    if notes and notes.strip():
        lines.append(notes)
    else:
        lines.append("None")
    lines.append("")

    # Conclusion
    lines.append("## Conclusion")
    lines.append("")
    lines.append(
        f"This completes the {skill_name} process for "
        f"{work_item_id} ({title}). Ready for {next_action}."
    )
    lines.append("")
    lines.append("</end_session>")

    return "\n".join(lines)


def render_report_from_workitem(
    work_item: dict,
    skill_name: str,
    headline: str,
    acceptance_criteria: list,
    producer_actions=None,
    notes=None,
    next_action: str = "review",
    children: list | None = None,
    audit_result: Mapping | None = None,
) -> str:
    """Convenience wrapper: read metadata from a work-item dict and render.

    ``audit_result`` is the structured top-level ``auditResult`` from the
    ``wl show --json`` envelope; it takes precedence over legacy fields.
    """
    metadata = render_metadata(
        work_item, children=children, audit_result=audit_result,
    )
    return render_report(
        skill_name=skill_name,
        work_item_id=work_item.get("id", "unknown"),
        title=work_item.get("title", "Untitled"),
        headline=headline,
        acceptance_criteria=acceptance_criteria,
        metadata=metadata,
        producer_actions=producer_actions,
        notes=notes,
        next_action=next_action,
    )


def render_from_wl(
    skill_name: str,
    work_item_id: str,
    headline: str,
    acceptance_criteria: list,
    producer_actions=None,
    notes=None,
    next_action: str = "review",
) -> str:
    """Fetch work-item data via ``wl show`` and render a report."""
    cmd = ["wl", "show", work_item_id, "--json", "--children"]
    try:
        wl_flags = worklog_dir_flag()
    except (RuntimeError, OSError):
        wl_flags = []
    if wl_flags:
        cmd[1:1] = wl_flags
    result = subprocess.run(
        cmd,
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        print(f"ERROR: wl show failed: {result.stderr}", file=sys.stderr)
        sys.exit(1)
    data = json.loads(result.stdout)
    work_item = data.get("workItem", {})
    children = data.get("children", [])
    return render_report_from_workitem(
        work_item=work_item,
        skill_name=skill_name,
        headline=headline,
        acceptance_criteria=acceptance_criteria,
        producer_actions=producer_actions,
        notes=notes,
        next_action=next_action,
        children=children,
        audit_result=data.get("auditResult"),
    )


# ─── CLI entry-point ───────────────────────────────────────────────────

def _parse_ac_args(ac_values: list[str] | None) -> list[tuple[str, str, str, str]]:
    """Parse repeated ``--ac`` values into ``(ac#, desc, metric, verdict)`` tuples.

    Each ``--ac`` is ``"<description>|<metric>|met"``; AC numbers are 1..N in
    order supplied. ``verdict`` is normalised to lower-case.
    """
    rows: list[tuple[str, str, str, str]] = []
    for idx, raw in enumerate(ac_values or [], start=1):
        parts = raw.split("|", 2)
        if len(parts) != 3:
            # Gracefully degrade: treat the whole string as description.
            desc = raw.strip()
            rows.append((str(idx), desc, "\u2014", "unmet"))
            continue
        desc, metric, verdict = (p.strip() for p in parts)
        verdict = verdict.lower().strip()
        if verdict not in ("met", "unmet"):
            verdict = "unmet"
        rows.append((str(idx), desc, metric, verdict))
    return rows


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Render a canonical end-of-session report.",
    )
    parser.add_argument(
        "work_item_id_pos",
        nargs="?",
        help="Work-item ID (positional, preferred).",
    )
    parser.add_argument(
        "--json",
        help="Read work-item JSON from stdin and render a minimal report.",
        action="store_true",
    )
    parser.add_argument(
        "--work-item-id",
        dest="work_item_id",
        help="Work-item ID (flag form, alias for positional).",
    )
    parser.add_argument(
        "--skill-name",
        default="skill",
        help="Name of the calling skill.",
    )
    parser.add_argument(
        "--headline",
        default="",
        help="1\u20133 sentence headline summary.",
    )
    parser.add_argument(
        "--ac",
        action="append",
        dest="ac",
        default=None,
        help="AC row as \"<description>|<metric>|met|unmet\" (repeatable).",
    )
    parser.add_argument(
        "--producer-actions",
        default=None,
        help="Actions for the producer (or omit for 'None needed').",
    )
    parser.add_argument(
        "--notes",
        default=None,
        help="Freeform context/caveats/assumptions.",
    )
    parser.add_argument(
        "--next-action",
        default="review",
        help="Next action (review, plan, ship, etc.).",
    )
    parser.add_argument(
        "--no-icons",
        action="store_true",
        help="Use bracketed-text fallbacks instead of emoji icons.",
    )
    args = parser.parse_args()

    if args.json:
        data = json.load(sys.stdin)
        # ``data`` may be a ``{workItem: {...}, auditResult: {...}}`` envelope
        # or the bare work item itself.
        if isinstance(data, dict) and "workItem" in data:
            work_item = data.get("workItem") or {}
            audit_result = data.get("auditResult")
        elif isinstance(data, dict):
            work_item = data
            audit_result = None
        else:
            work_item = {}
            audit_result = None
        ac_rows = _parse_ac_args(args.ac)
        report = render_report_from_workitem(
            work_item=work_item if isinstance(work_item, dict) else {},
            skill_name=args.skill_name,
            headline=args.headline,
            acceptance_criteria=ac_rows,
            producer_actions=args.producer_actions,
            notes=args.notes,
            next_action=args.next_action,
            audit_result=audit_result,
        )
        print(report)
        return

    work_item_id = args.work_item_id or args.work_item_id_pos
    if not work_item_id:
        parser.error("work-item ID is required (positional <id> or --work-item-id)")
    ac_rows = _parse_ac_args(args.ac)
    report = render_from_wl(
        skill_name=args.skill_name,
        work_item_id=work_item_id,
        headline=args.headline,
        acceptance_criteria=ac_rows,
        producer_actions=args.producer_actions,
        notes=args.notes,
        next_action=args.next_action,
    )
    print(report)


if __name__ == "__main__":
    main()
