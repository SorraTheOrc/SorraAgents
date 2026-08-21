#!/usr/bin/env python3
"""Render the canonical end-of-session report for a work item (report skill).

Canonical template (spec: parent item SA-0MSJ082OY003IQ8S):

    # Completed <skill_name>

    **<title>** (<id>)

    <headline summary>

    ## Acceptance Criteria

    | AC# | Description | Metric | Verdict |
    |-----|-------------|--------|---------|
    | 1 | <Description> | <Metric> | met |
    | 2 | <Description> | <Metric> | unmet |

    ## Meta-Data

    - Type: <icon> <text>
    - Priority: <icon> <text>
    - Status: <icon> <text>
    - Stage: <icon> <text>
    - Risk: <icon> <text>
    - Effort: <icon> <text>
    - Children: <count>
    - Audit: <icon> <text>

    ## Producer Actions

    None needed

    ## Notes

    <freeform>

    ## Conclusion

    This completes the <skill_name> process for <id> (<title>). Ready for <next_action>.

Meta-Data values are populated deterministically from ``wl show <id> --children
--json`` (``workItem``, ``auditResult``, ``children``). Icons come from the
ContextHub canonical set (``../ContextHub/docs/icons-design.md`` /
``src/icons.ts``): priority, status, stage, risk, effort, audit, epic. Each
entry maps a value to an emoji and a bracketed-text fallback; the fallback is
used when ``no_icons`` is requested (``--no-icons`` / ``WL_NO_ICONS=1``).
Missing/unset values render the neutral marker ``— N/A``; known values with no
ContextHub icon (e.g. non-epic issue types) render as plain text.
"""  # noqa: EXE001
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

# Add repo root to sys.path for shared utility access.
_SKILLS_ROOT = Path(__file__).resolve().parents[2]
_SKILLS_ROOT_STR = str(_SKILLS_ROOT)
if _SKILLS_ROOT_STR in sys.path:
    sys.path.remove(_SKILLS_ROOT_STR)
sys.path.insert(0, _SKILLS_ROOT_STR)

from import_guard import guard_shared_import

try:
    from shared.status_lifecycle import resolve_worklog_flags
except ModuleNotFoundError as _missing_shared:
    guard_shared_import(_missing_shared.name)

# ── Icon mappings: ContextHub canonical set ────────────────────────────────
# Source: ../ContextHub/docs/icons-design.md (spec) and ../ContextHub/src/icons.ts
# (implementation consumed by the wl CLI). Value → (emoji, bracketed fallback).
# NOTE: src/icons.ts pads some CLI fallbacks for column alignment (e.g. '[MED ]',
# '[LOW ]', '[DEL ]'); the report uses the unpadded documented values ('[MED]',
# '[LOW]', '[DEL]') so the bracketed text stays clean in markdown.

PRIORITY_ICONS = {
    "critical": ("🚨", "[CRIT]"),
    "high": ("⭐", "[HIGH]"),
    "medium": ("📋", "[MED]"),
    "low": ("🐢", "[LOW]"),
}

STATUS_ICONS = {
    "open": ("🔓", "[OPEN]"),
    "in-progress": ("🔄", "[INPR]"),
    "completed": ("✔️", "[DONE]"),
    "blocked": ("⛔", "[BLKD]"),
    "deleted": ("🗑️", "[DEL]"),
    "input_needed": ("💬", "[HELP]"),
}

STAGE_ICONS = {
    "idea": ("💡", "[IDEA]"),
    "intake_complete": ("📥", "[INTAKE]"),
    "plan_complete": ("📋", "[PLAN]"),
    "in_progress": ("🛠️", "[PROG]"),
    "in_review": ("🔍", "[REVIEW]"),
    "done": ("🏁", "[DONE]"),
}

RISK_ICONS = {
    "low": ("🌱", "[LOW]"),
    "medium": ("⚠️", "[MED]"),
    "high": ("🔥", "[HIGH]"),
    "severe": ("🚨", "[SEV]"),
}

EFFORT_ICONS = {
    "xs": ("🐜", "[XS]"),
    "s": ("🐇", "[S]"),
    "m": ("🐕", "[M]"),
    "l": ("🐘", "[L]"),
    "xl": ("🐋", "[XL]"),
    # Full-text aliases used by the Worklog CLI and effort-and-risk skill.
    "extra small": ("🐜", "[XS]"),
    "small": ("🐇", "[S]"),
    "medium": ("🐕", "[M]"),
    "large": ("🐘", "[L]"),
    "extra large": ("🐋", "[XL]"),
    "xlarge": ("🐋", "[XL]"),
}

EPIC_ICONS = {
    "epic": ("🏰", "[EPIC]"),
}

AUDIT_ICONS = {
    "yes": ("✅", "[YES]"),
    "no": ("❌", "[NO]"),
    "unknown": ("❔", "[UNKN]"),
}

NEUTRAL_MARKER = "—"
NEUTRAL_TEXT = "N/A"

# ── Icon helpers ───────────────────────────────────────────────────────────

def _pair(mapping: dict, value) -> tuple | None:
    """Return ``(emoji, fallback)`` for a value, or ``None`` when unmapped."""
    if not value:
        return None
    return mapping.get(str(value).strip().lower())


def _icon(mapping: dict, value, no_icons: bool = False) -> str | None:
    """Return the emoji (or bracketed fallback) for a mapped value, else ``None``."""
    pair = _pair(mapping, value)
    if pair is None:
        return None
    emoji, fallback = pair
    return fallback if no_icons else emoji


def _meta_line(label: str, mapping: dict, value, no_icons: bool = False) -> str:
    """Render one ``- Label: <icon> <text>`` line for a Meta-Data field.

    Kept as a small public-ish helper so tests and other skills can render a
    single field line without building a full payload.
    """
    if not value:
        return f"- {label}: {NEUTRAL_MARKER} {NEUTRAL_TEXT}"
    icon = _icon(mapping, value, no_icons)
    if icon is None:
        return f"- {label}: {value}"
    return f"- {label}: {icon} {value}"


def _audit_state(audit_result) -> tuple[str, str]:
    """Map ``auditResult`` JSON to ``(text, icon_key)``: passed/failed/not run."""
    if audit_result is None:
        return "not run", "unknown"
    if audit_result.get("readyToClose") is True:
        return "passed", "yes"
    return "failed", "no"


# ── Metadata extraction (deterministic from wl show --json) ────────────────

def extract_metadata(data: dict, no_icons: bool = False) -> dict:
    """Extract the Meta-Data fields from a ``wl show --children --json`` payload.

    Returns an ordered dict mapping field label → (icon_text, raw_text) where
    icon_text is the emoji/bracketed fallback plus trailing space (or the
    neutral marker ``—``, or empty for fields with no ContextHub icon such as
    Children) and raw_text is the worklog value (``N/A`` when unset).
    """
    work_item = data.get("workItem", {})
    audit_result = data.get("auditResult")
    children = data.get("children") or []
    audit_text, audit_key = _audit_state(audit_result)

    fields: dict[str, tuple[str, str]] = {}
    for label, mapping, key in (
        ("Type", EPIC_ICONS, "issueType"),
        ("Priority", PRIORITY_ICONS, "priority"),
        ("Status", STATUS_ICONS, "status"),
        ("Stage", STAGE_ICONS, "stage"),
        ("Risk", RISK_ICONS, "risk"),
        ("Effort", EFFORT_ICONS, "effort"),
    ):
        value = work_item.get(key, "")
        if not value:
            fields[label] = (f"{NEUTRAL_MARKER} ", NEUTRAL_TEXT)
            continue
        icon = _icon(mapping, value, no_icons=no_icons)
        fields[label] = ((f"{icon} " if icon else ""), value)

    audit_icon = _icon(AUDIT_ICONS, audit_key, no_icons=no_icons)
    fields["Children"] = ("", str(len(children)))
    fields["Audit"] = ((f"{audit_icon} " if audit_icon else ""), audit_text)
    return fields


# ── Section renderers ──────────────────────────────────────────────────────

def render_ac_table(ac_rows: list[dict]) -> str:
    """Render the ``## Acceptance Criteria`` table body.

    Each row: ``{ac#} | {description} | {metric} | met|unmet``.

    Accepts rows with either a boolean ``met`` field (backward compat)
    or a string ``verdict`` field (audit-runner format: met/unmet/adjusted/partial).
    """
    lines = ["| AC# | Description | Metric | Verdict |", "|---|---|---|---|"]
    if not ac_rows:
        lines.append("| — | No acceptance criteria supplied | — | — |")
    else:
        for i, row in enumerate(ac_rows, start=1):
            # Prefer an explicit string verdict (audit-runner format or
            # adjusted/partial passed via --ac); fall back to the met
            # boolean for backward compat.
            v = row.get("verdict")
            if v is None:
                met = row.get("met")
                if isinstance(met, str):
                    v = met
                else:
                    v = "met" if met else "unmet"
            lines.append(f"| {i} | {row['description']} | {row['metric']} | {v} |")
    return "\n".join(lines)


def render_metadata(data: dict, no_icons: bool = False) -> str:
    """Render the ``## Meta-Data`` bullet block (icons or bracketed fallbacks)."""
    fields = extract_metadata(data, no_icons=no_icons)
    return "\n".join(f"- {label}: {icon}{text}" for label, (icon, text) in fields.items())


def render_report(
    data: dict,
    *,
    skill_name: str,
    headline: str,
    ac_rows: list[dict],
    producer_actions: str | None = None,
    notes: str = "",
    next_action: str = "review",
    no_icons: bool = False,
) -> str:
    """Render the full canonical end-of-session report for a work item.

    Args:
        data: parsed ``wl show <id> --children --json`` payload.
        skill_name: name of the calling skill (e.g. ``plan``).
        headline: one-paragraph headline summary supplied by the calling skill.
        ac_rows: list of ``{"description", "metric", "met": bool}`` dicts with
            the calling skill's own per-criterion verdicts.
        producer_actions: actions for the producer; ``None``/empty renders
            ``None needed``.
        notes: freeform agent commentary (between Producer Actions and
            Conclusion).
        next_action: where the work item goes next (e.g. ``review``).
        no_icons: render bracketed-text fallbacks instead of emoji.
    """
    work_item = data.get("workItem", {})
    title = work_item.get("title", "")
    item_id = work_item.get("id", "")

    producer_actions = producer_actions if producer_actions else "None needed"

    sections = [
        f"# Completed {skill_name}",
        "",
        f"**{title}** ({item_id})",
        "",
        headline,
        "",
        "## Acceptance Criteria",
        "",
        render_ac_table(ac_rows),
        "",
        "## Meta-Data",
        "",
        render_metadata(data, no_icons=no_icons),
        "",
        "## Producer Actions",
        "",
        producer_actions,
        "",
        "## Notes",
        "",
        notes,
        "",
        "## Conclusion",
        "",
        f"This completes the {skill_name} process for {item_id} ({title}). Ready for {next_action}.",
    ]
    return "\n".join(sections)


# ── CLI ────────────────────────────────────────────────────────────────────

def _load_payload(work_item_id: str) -> dict:
    """Fetch ``wl show <id> --children --json`` output for a work item.

    ``--worklog-dir`` flags are resolved via the shared prefix-to-sibling
    scan so the item is fetched from its own worklog store regardless of cwd
    (including git worktrees).
    """
    cmd = ["wl", "show", work_item_id, "--children", "--json"]
    worklog_flags = resolve_worklog_flags(cmd)
    if worklog_flags:
        cmd[1:1] = worklog_flags
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def _parse_ac(spec: str) -> dict:
    """Parse an ``--ac`` argument: ``description|metric|verdict``.

    Exactly three pipe-separated fields; ``verdict`` is one of ``met``,
    ``unmet``, ``adjusted`` or ``partial`` (case-insensitive;
    ``yes``/``true``/``1`` accepted as met, ``no``/``false``/``0`` as unmet).
    """
    parts = spec.split("|")
    if len(parts) != 3:
        raise SystemExit(
            "--ac expects 'description|metric|verdict' "
            "(VERDICT: met|unmet|adjusted|partial), got: {spec!r}"
        )
    description, metric, verdict = (p.strip() for p in parts)
    v = verdict.lower()
    if v in {"met", "yes", "true", "1"}:
        met: bool | str = True
    elif v in {"unmet", "no", "false", "0"}:
        met = False
    else:
        # Pass through adjusted/partial as strings; render_ac_table treats
        # a non-boolean met as an explicit verdict string.
        met = v
    return {"description": description, "metric": metric, "met": met}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Render the canonical end-of-session report for a work item."
    )
    parser.add_argument("work_item_id", help="Work item ID (fetched via `wl show --children --json`)")
    parser.add_argument("--skill-name", required=True, help="Calling skill name, e.g. plan")
    parser.add_argument("--headline", required=True, help="One-paragraph headline summary")
    parser.add_argument(
        "--ac",
        action="append",
        default=[],
        metavar="DESCRIPTION|METRIC|VERDICT",
        help="Acceptance criterion row; repeat for each AC (VERDICT: met or unmet)",
    )
    parser.add_argument("--producer-actions", default=None, help="Producer actions (default: None needed)")
    parser.add_argument("--notes", default="", help="Freeform notes section")
    parser.add_argument("--next-action", default="review", help="Next action for the conclusion")
    parser.add_argument("--no-icons", action="store_true", help="Use bracketed-text fallbacks instead of emoji")
    args = parser.parse_args(argv)

    if os.environ.get("WL_NO_ICONS") == "1":
        args.no_icons = True

    data = _load_payload(args.work_item_id)
    ac_rows = [_parse_ac(spec) for spec in args.ac]
    report = render_report(
        data,
        skill_name=args.skill_name,
        headline=args.headline,
        ac_rows=ac_rows,
        producer_actions=args.producer_actions,
        notes=args.notes,
        next_action=args.next_action,
        no_icons=args.no_icons,
    )
    print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
