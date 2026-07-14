#!/usr/bin/env python3
"""
orchestrate_estimate.py

Take inputs (o, m, p, overheads, parent risk, children risks, certainty, assumptions, unknowns)
and produce the final JSON output used by the skill. Also prints the JSON to stdout.

Input: JSON via stdin with keys:
  o, m, p (numbers, hours)
  items: optional list of per-work-item estimates (each item: {id,title,o,m,p})
  overheads: { coordination: n, review: n, testing: n, risk_buffer: n }
  parent: { probability, impact }
  children: [ { id, title, probability, impact } ]
  certainty: 0-100
  confidence_percent (optional override)
  assumptions (list)
  unknowns (list)

Output: final JSON block written to stdout
"""

import sys
import json
import traceback
from pathlib import Path

# Add repo root to sys.path for shared utility access
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from skill.scripts.failure_notice import FailureNotice  # noqa: E402

from _shared import compute_omp, level_from_score, pick_tshirt, TSHIRT_MAP, DEFAULT_THRESHOLDS


# ---------------------------------------------------------------------------
# Extracted helper functions
# ---------------------------------------------------------------------------


def _load_thresholds() -> dict:
    """Load t-shirt thresholds from references/t-shirt_sizes.json.

    Falls back to DEFAULT_THRESHOLDS if the file is missing or unreadable.

    Returns:
        A dict mapping size codes to {"min": float, "max": float | None}.
    """
    try:
        with open("references/t-shirt_sizes.json", "r") as f:
            tshirt_cfg = json.load(f)
            return tshirt_cfg.get("thresholds", {})
    except Exception:
        return DEFAULT_THRESHOLDS


def _fetch_issue_stage(issue_id: str) -> str:
    """Run ``wl show <issue_id> --json`` and return the issue stage.

    Args:
        issue_id: The work-item identifier to inspect.

    Returns:
        The stage string (expected to be ``"plan_complete"`` or ``"intake_complete"``).

    Raises:
        The function calls ``sys.exit()`` with the following codes on failure:
            3 – ``wl show`` subprocess returned a non-zero exit code.
            4 – The issue stage is not ``plan_complete`` or ``intake_complete``.
            5 – An unexpected exception occurred (e.g., JSON decode failure).
    """
    try:
        import subprocess

        show_proc = subprocess.run(
            ["wl", "show", issue_id, "--json"], capture_output=True, text=True
        )
        if show_proc.returncode != 0:
            print(json.dumps({
                "error": "wl show failed",
                "stdout": show_proc.stdout,
                "stderr": show_proc.stderr,
            }))
            sys.exit(3)
        show_json = json.loads(show_proc.stdout)
        stage = show_json.get("workItem", {}).get("stage", "")
        if stage not in ("plan_complete", "intake_complete"):
            print(
                f"The issue does not have a sufficiently detailed plan, to proceed it must be in the stage of `intake_complete` or `plan_complete`. Run the intake command with `/intake {issue_id}` or the plan command with `/skill:plan {issue_id}`."
            )
            sys.exit(4)
        return stage
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(5)


def _compute_tshirt(recommended: float, thresholds: dict) -> str:
    """Compute the full-text t-shirt size label for the given recommended hours.

    Args:
        recommended: The recommended hours (expected + overheads total).
        thresholds: A dict mapping size codes to {"min": float, "max": float | None}.

    Returns:
        A human-readable t-shirt size label (e.g. "Small", "Extra Large").
    """
    tshirt_code = pick_tshirt(recommended, thresholds)
    return TSHIRT_MAP.get(tshirt_code, tshirt_code)


def _compute_risk(data: dict, certainty: float) -> dict:
    """Aggregate risk from parent/children data with the given certainty.

    Args:
        data: The full input data dict containing ``parent``, ``children`` keys.
        certainty: The (potentially stage-adjusted) certainty percentage.

    Returns:
        A risk dict with keys: probability, impact, score, level,
        top_drivers, mitigations.
    """
    parent = data.get("parent", {})
    children = data.get("children", [])

    probs = [parent.get("probability", 0)] + [c.get("probability", 0) for c in children]
    imps = [parent.get("impact", 0)] + [c.get("impact", 0) for c in children]
    certainty_factor = 1.0 + max(0, (100 - certainty) / 100.0) * 0.1
    agg_prob = min(5, max(probs) * certainty_factor)
    agg_imp = min(5, max(imps) * certainty_factor)
    score = int(round(agg_prob * agg_imp))
    level = level_from_score(score)

    drivers = []
    for c in children:
        drivers.append((
            c.get("id", ""),
            c.get("probability", 0) * c.get("impact", 0),
            c.get("title", ""),
        ))
    drivers.sort(key=lambda x: x[1], reverse=True)
    top = [d[2] or d[0] for d in drivers[:3]]

    mitigations = [
        "Add targeted tests and integration checks",
        "Lock dependencies and add compatibility tests",
        "Schedule extra review for risky components",
    ]

    return {
        "probability": round(agg_prob, 2),
        "impact": round(agg_imp, 2),
        "score": score,
        "level": level,
        "top_drivers": top,
        "mitigations": mitigations,
    }


def _update_work_item(issue_id: str, wl_effort: str, wl_risk: str) -> dict:
    """Run ``wl update`` to set effort and risk fields on the work item.

    Args:
        issue_id: The work-item identifier.
        wl_effort: The effort label string (e.g. "Small").
        wl_risk: The risk label string (e.g. "Medium", "Severe").

    Returns:
        A result dict with keys ``success``, ``returncode``, ``stdout``,
        ``stderr`` on success, or ``success`` and ``error`` on exception.
    """
    try:
        import subprocess

        update_cmd = [
            "wl",
            "update",
            issue_id,
            "--effort",
            str(wl_effort),
            "--risk",
            str(wl_risk),
            "--json",
        ]
        update_proc = subprocess.run(update_cmd, capture_output=True, text=True)
        return {
            "success": update_proc.returncode == 0,
            "returncode": update_proc.returncode,
            "stdout": update_proc.stdout,
            "stderr": update_proc.stderr,
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


def _render_human_text(data: dict, final: dict) -> str:
    """Generate human-readable narrative using ``json_to_human.py`` subprocess.

    Side effects: sets ``final["human_text"]``, ``final["human_render_rc"]``,
    and ``final["human_render_stderr"]``.

    Args:
        data: The original input data dict (for wbs_items/wbs_children).
        final: The output dict being built (for effort, risk, etc.).

    Returns:
        The rendered human text string (may be empty on failure).
    """
    try:
        import subprocess
        import os

        # Build a sanitized object for rendering to avoid contaminating the
        # human text. Include WBS data (items and children) for narrative generation.
        wbs_items = data.get("items", [])
        wbs_children = data.get("children", [])
        sanitized = {
            "effort": final.get("effort"),
            "risk": final.get("risk"),
            "confidence_percent": final.get("confidence_percent"),
            "assumptions": final.get("assumptions"),
            "unknowns": final.get("unknowns"),
            "wbs_items": wbs_items,
            "wbs_children": wbs_children,
        }

        script_dir = os.path.dirname(__file__)
        json_to_human_path = os.path.join(script_dir, "json_to_human.py")
        sj = json.dumps(sanitized)
        p = subprocess.run(
            ["python3", json_to_human_path], input=sj, text=True, capture_output=True
        )
        human_text = p.stdout or ""
        final["human_text"] = human_text
        final["human_render_rc"] = p.returncode
        final["human_render_stderr"] = p.stderr

        return human_text
    except Exception as e:
        final["human_text"] = ""
        final["human_render_rc"] = -1
        final["human_render_stderr"] = str(e)
        return ""


def _post_comment(issue_id: str, combined_text: str) -> dict:
    """Post a rendered comment to the work item via ``wl comment add``.

    Args:
        issue_id: The work-item identifier.
        combined_text: The full comment body (human text + JSON block).

    Returns:
        A result dict with keys ``returncode``, ``stdout``, ``stderr``,
        ``success`` on success, or ``success`` and ``error`` on exception.
    """
    try:
        import subprocess

        comment_cmd = [
            "wl",
            "comment",
            "add",
            issue_id,
            "--author",
            "effort_and_risk_skill",
            "--comment",
            combined_text,
            "--json",
        ]
        comment_proc = subprocess.run(comment_cmd, capture_output=True, text=True)
        return {
            "returncode": comment_proc.returncode,
            "stdout": comment_proc.stdout,
            "stderr": comment_proc.stderr,
            "success": comment_proc.returncode == 0,
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------


def main():
    data = json.load(sys.stdin)

    # 1. Compute O/M/P estimates and basic effort metrics
    o, m, p = compute_omp(data)
    overheads = data.get("overheads", {})

    expected = (o + 4 * m + p) / 6.0
    overheads_total = sum(float(v) for v in overheads.values()) if overheads else 0.0
    recommended = expected + overheads_total
    range_min = o + overheads_total
    range_max = p + overheads_total

    # 2. Validate issue_id early
    issue_id = data.get("issue_id")
    if not issue_id:
        print(json.dumps({"error": "missing required field: issue_id"}))
        sys.exit(2)

    # 3. Fetch the issue stage (exits with codes 3-5 on failure)
    input_stage = _fetch_issue_stage(issue_id)

    # 4. Load t-shirt thresholds and compute the t-shirt size label
    thresholds = _load_thresholds()
    tshirt = _compute_tshirt(recommended, thresholds)

    # 5. Compute certainty (stage-adjusted) and build the risk dict
    certainty = float(data.get("certainty", 100))
    original_certainty = certainty
    if input_stage == "intake_complete":
        certainty = certainty * 0.6

    risk = _compute_risk(data, certainty)

    # 6. Build the final output dict
    final = {
        "effort": {
            "unit": "hours",
            "tshirt": tshirt,
            "o": o,
            "m": m,
            "p": p,
            "expected": round(expected, 2),
            "recommended": round(recommended, 2),
            "range": [round(range_min, 2), round(range_max, 2)],
        },
        "risk": risk,
        "confidence_percent": int(
            data.get("confidence_percent", round(100 - (100 - certainty) / 2))
        ),
        "assumptions": data.get("assumptions", []),
        "unknowns": data.get("unknowns", []),
    }

    # Attach audit fields describing the input stage and any certainty adjustment
    final["input_stage"] = input_stage
    final["original_certainty"] = original_certainty
    final["adjusted_certainty"] = certainty

    # 7. Map risk.level to wl's risk label (Critical -> Severe)
    risk_level = risk.get("level", "")
    wl_risk_map = {
        "Low": "Low",
        "Medium": "Medium",
        "High": "High",
        "Critical": "Severe",
        "Severe": "Severe",
    }
    wl_risk = wl_risk_map.get(risk_level, "Medium")
    wl_effort = tshirt

    # 8. Update the work-item's effort and risk fields via wl update
    final["update_result"] = _update_work_item(issue_id, wl_effort, wl_risk)

    # 9. Render human-readable narrative
    human_text = _render_human_text(data, final)

    # 10. Post the combined narrative + JSON as a comment
    if human_text.strip():
        skill_json = {
            "effort": final.get("effort"),
            "risk": final.get("risk"),
            "confidence_percent": final.get("confidence_percent"),
            "assumptions": final.get("assumptions"),
            "unknowns": final.get("unknowns"),
        }
        skill_json_str = json.dumps(skill_json, indent=2)
        combined_text = human_text + "\n\n```json\n" + skill_json_str + "\n```"
        final["comment_result"] = _post_comment(issue_id, combined_text)
    else:
        final["comment_result"] = {
            "success": False,
            "error": "empty rendered human text",
            "human_render_stderr": final.get("human_render_stderr", ""),
        }

    print(json.dumps(final))


def _run() -> None:
    """Entry point with failure notice wrapping."""
    try:
        main()
    except Exception as exc:
        notice = FailureNotice(
            script_name="orchestrate_estimate.py",
            reason=f"Unhandled exception: {exc}",
            stderr_context=traceback.format_exc(),
        )
        print(notice.wrap(
            json.dumps({"error": str(exc)})
        ))
        sys.exit(1)


if __name__ == "__main__":
    _run()
