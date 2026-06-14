#!/usr/bin/env python3
"""
json_to_human.py

Convert the final JSON object into a human-readable bulleted narrative.

Accepts stdin JSON (the same shape as the skill final JSON) and prints an expanded
narrative that includes a 5-12 item WBS and top risk drivers with mitigations.

Input fields:
  effort: { unit, tshirt, o, m, p, expected, recommended, range }
  risk: { probability, impact, score, level, top_drivers, mitigations }
  confidence_percent: int
  assumptions: list[str]
  unknowns: list[str]
  wbs_items: optional list of { id, title, o, m, p } — per-work-item WBS estimates
  wbs_children: optional list of { id, title } — child work items
"""

import sys
import json


def _tshirt_display(tshirt: str) -> str:
    """Normalize tshirt code to full-text label."""
    tshirt_map = {
        "XS": "Extra Small",
        "S": "Small",
        "M": "Medium",
        "L": "Large",
        "XL": "Extra Large",
    }
    # Accept either codes or full-text
    if tshirt in tshirt_map:
        return tshirt_map[tshirt]
    if tshirt in tshirt_map.values():
        return tshirt
    return tshirt or "N/A"


def _pert_expected(o: float, m: float, p: float) -> float:
    """Compute PERT expected value: (O + 4*M + P) / 6."""
    return (o + 4 * m + p) / 6.0


def _render_wbs_table(items: list[dict]) -> str:
    """Render a WBS table from a list of items.

    Each item has: id, title, o, m, p.
    Returns a markdown table string.
    """
    if not items:
        return ""

    lines = ["### Work Breakdown Structure (WBS)\n", "| # | Item | O (h) | M (h) | P (h) | Expected (h) |"]
    lines.append("|---|------|-------|-------|-------|-------------|")
    for i, item in enumerate(items, start=1):
        o = float(item.get("o", 0))
        m = float(item.get("m", 0))
        p = float(item.get("p", 0))
        exp = _pert_expected(o, m, p)
        title = item.get("title", item.get("id", f"Item {i}"))
        lines.append(f"| {i} | {title} | {o:.2f} | {m:.2f} | {p:.2f} | {exp:.2f} |")

    return "\n".join(lines) + "\n"


def _render_children_list(children: list[dict]) -> str:
    """Render children as a bulleted list (when no WBS items with estimates exist)."""
    if not children:
        return ""

    lines = ["### Work Breakdown Structure (WBS)", ""]
    for c in children:
        title = c.get("title", c.get("id", "Unknown"))
        lines.append(f"- {title}")
    return "\n".join(lines) + "\n"


def _render_risk_drivers(top_drivers: list[str], mitigations: list[str]) -> str:
    """Render top risk drivers with mitigations as bulleted narrative."""
    if not top_drivers:
        return ""

    # Pair drivers with mitigations (by index, pad if needed)
    paired = []
    for i, driver in enumerate(top_drivers[:3]):
        mitigation = mitigations[i] if i < len(mitigations) else "No mitigation specified"
        paired.append((driver, mitigation))

    lines = ["### Top Risk Drivers & Mitigations\n"]
    for i, (driver, mitigation) in enumerate(paired, start=1):
        lines.append(f"{i}. **{driver}** — {mitigation}")

    return "\n".join(lines) + "\n"


def main():
    data = json.load(sys.stdin)
    effort = data.get("effort", {})
    risk = data.get("risk", {})
    confidence = data.get("confidence_percent", 0)
    assumptions = data.get("assumptions", [])
    unknowns = data.get("unknowns", [])
    wbs_items = data.get("wbs_items")
    wbs_children = data.get("wbs_children", data.get("children"))

    tshirt = _tshirt_display(effort.get("tshirt", ""))
    expected = effort.get("expected", 0)
    o_val = effort.get("o", 0)
    m_val = effort.get("m", 0)
    p_val = effort.get("p", 0)
    recommended = effort.get("recommended", 0)
    effort_range = effort.get("range", [0, 0])
    unit = effort.get("unit", "hours")

    score = risk.get("score", 0)
    level = risk.get("level", "")
    probability = risk.get("probability", 0)
    impact = risk.get("impact", 0)
    top_drivers = risk.get("top_drivers", [])
    mitigations = risk.get("mitigations", [])

    unknowns_str = "; ".join(unknowns) if unknowns else "none"
    assumptions_str = "; ".join(assumptions) if assumptions else "none"

    # Build the report
    lines = []
    lines.append("# Effort and Risk Report")
    lines.append("")

    # === Effort Section ===
    lines.append("## Effort Estimate")
    lines.append("")
    lines.append(f"- **T-shirt size:** {tshirt}")
    lines.append(f"- **Three-point (PERT):** O={o_val:.2f}h, M={m_val:.2f}h, P={p_val:.2f}h")
    lines.append(f"- **Expected (E=(O+4M+P)/6):** {expected:.2f}h")
    lines.append(f"- **Recommended (with overheads):** {recommended:.2f}h")
    lines.append(f"- **Range:** [{effort_range[0]:.2f}h — {effort_range[1]:.2f}h]")
    lines.append(f"- **Unit:** {unit}")
    lines.append("")

    # === WBS Section ===
    if wbs_items and len(wbs_items) >= 2:
        lines.append(_render_wbs_table(wbs_items))
        lines.append("")
    elif wbs_children and len(wbs_children) >= 2:
        lines.append(_render_children_list(wbs_children))
        lines.append("")
    else:
        # Fallback — generic breakdown suggestion
        lines.append("### Work Breakdown (suggested)")
        lines.append("")
        lines.append("_No detailed WBS items provided. Recommended breakdown based on estimate:_")
        lines.append("")
        if recommended > 0:
            # Generate generic WBS items based on estimate
            generic_items = [
                ("Design & Planning", 0.15),
                ("Implementation — Core Logic", 0.30),
                ("Implementation — Edge Cases", 0.15),
                ("Testing & QA", 0.15),
                ("Documentation & Rollout", 0.10),
                ("Coordination & Review", 0.10),
                ("Risk Buffer", 0.05),
            ]
            lines.append("| Component | % of Effort | Estimated Hours |")
            lines.append("|-----------|-------------|-----------------|")
            for component, pct in generic_items:
                hours = recommended * pct
                lines.append(f"| {component} | {pct*100:.0f}% | {hours:.2f}h |")
        lines.append("")

    # === Risk Section ===
    lines.append("## Risk Assessment")
    lines.append("")
    lines.append(f"- **Risk Score:** {score}/25 — **{level}**")
    lines.append(f"- **Probability:** {probability}/5 | **Impact:** {impact}/5")
    lines.append("")

    risk_drivers_text = _render_risk_drivers(top_drivers, mitigations)
    if risk_drivers_text.strip():
        lines.append(risk_drivers_text)
        lines.append("")

    # === Confidence Section ===
    lines.append("## Confidence")
    lines.append("")
    lines.append(f"- **Confidence:** {confidence}%")
    lines.append(f"- **Unknowns:** {unknowns_str}")
    lines.append(f"- **Assumptions:** {assumptions_str}")

    # Print the report
    print("\n".join(lines))


if __name__ == "__main__":
    main()
