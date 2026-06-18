#!/usr/bin/env python3
"""Shared autoplan decision logic for Ralph and the /plan command.

Centralizes the effort/risk threshold decision logic that was previously
duplicated across RalphLoop (skill/ralph/scripts/ralph_loop.py) and the
/plan command prompt.

Both Ralph and PlanAll invoke this module to decide whether a work item
should be planned or can skip directly to implementation.

The module provides:
  - Pure functions for decision logic (testable without mocking)
  - A high-level ``make_autoplan_decision()`` orchestrator
  - ``run_effort_and_risk()`` wrapper for the effort-and-risk skill
  - ``append_autoplan_decision_comment()`` for idempotent comment posting
  - CLI entry points ``plan-if-needed`` and ``check-effort-risk``
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import subprocess
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("plan_helpers")

# Default thresholds for auto-plan decision
# If effort t-shirt is in this set AND risk level is in the risk set,
# skip /plan and proceed directly to implement.
DEFAULT_AUTOPLAN_EFFORT_SKIP: frozenset[str] = frozenset({"Extra Small", "Small"})
DEFAULT_AUTOPLAN_RISK_SKIP: frozenset[str] = frozenset({"Low"})


# ---------------------------------------------------------------------------
# Subprocess execution helper (supports custom runners for test injection)
# ---------------------------------------------------------------------------


def _execute_subprocess(
    cmd: list[str],
    input_data: str | None = None,
    runner: Callable[..., Any] | None = None,
) -> Any:
    """Execute a subprocess, supporting custom runners for test injection.

    When ``runner`` is provided, the payload is appended as a trailing
    command-line argument (the convention used by Ralph's FakeRunner).
    When ``runner`` is None, the payload is supplied via stdin (the
    convention used by the CLI and production subprocess calls).

    Returns an object with ``returncode``, ``stdout``, and ``stderr``
    attributes (compatible with both ``subprocess.CompletedProcess``
    and Ralph's ``Result`` dataclass).
    """
    if runner is not None:
        if input_data is not None:
            return runner(list(cmd) + [input_data])
        return runner(list(cmd))
    return subprocess.run(cmd, input=input_data, capture_output=True, text=True)


# ---------------------------------------------------------------------------
# Helpers: calling wl via subprocess
# ---------------------------------------------------------------------------


def _wl_comment_list(
    work_item_id: str,
    runner: Callable[..., Any] | None = None,
) -> list[dict]:
    """Call ``wl comment list <id> --json`` and return the comment list."""
    cmd = ["wl", "comment", "list", work_item_id, "--json"]
    proc = _execute_subprocess(cmd, runner=runner)
    if proc.returncode != 0:
        logger.warning("wl comment list failed target=%s stderr=%s", work_item_id, proc.stderr)
        return []
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        logger.warning("wl comment list invalid JSON target=%s", work_item_id)
        return []
    if isinstance(data, dict) and data.get("success") is False:
        logger.warning("wl comment list returned error target=%s", work_item_id)
        return []
    return data.get("comments", []) if isinstance(data, dict) else []


def _wl_comment_add(
    work_item_id: str,
    comment: str,
    author: str = "ralph",
    runner: Callable[..., Any] | None = None,
) -> dict:
    """Call ``wl comment add <id> --author <a> --comment <c> --json``."""
    cmd = [
        "wl",
        "comment",
        "add",
        work_item_id,
        "--author",
        author,
        "--comment",
        comment,
        "--json",
    ]
    proc = _execute_subprocess(cmd, runner=runner)
    if proc.returncode != 0:
        logger.warning(
            "wl comment add failed target=%s rc=%s stderr=%s",
            work_item_id, proc.returncode, proc.stderr,
        )
        return {}
    try:
        return json.loads(proc.stdout) or {}
    except json.JSONDecodeError:
        return {}


# ---------------------------------------------------------------------------
# Complexity tier resolution
# ---------------------------------------------------------------------------


def resolve_complexity_tier(item: dict, config: dict) -> str:
    """Resolve the complexity tier (low, medium, high) for a work item.

    Mapping:
    - Low: Effort XS or S AND risk Low
    - Medium: Effort M OR risk Medium
    - High: Effort L or XL OR risk High

    Thresholds are configurable via config["complexity_tier"].
    Defaults to 'low' when both effort and risk are absent, otherwise
    defaults to 'medium' if mapping cannot be determined.
    """
    effort = item.get("effort")
    risk = item.get("risk")

    # When both effort and risk are absent, default to low tier
    if not effort and not risk:
        return "low"

    # Load thresholds from config
    tier_cfg = config.get("complexity_tier", {})
    low_cfg = tier_cfg.get("low", {})
    high_cfg = tier_cfg.get("high", {})

    low_max_effort = low_cfg.get("max_effort", "Small")
    low_max_risk = low_cfg.get("max_risk", "Low")
    high_min_effort = high_cfg.get("min_effort", "Large")
    high_min_risk = high_cfg.get("min_risk", "High")

    # T-shirt size order for comparison
    effort_order = {"Extra Small": 0, "Small": 1, "Medium": 2, "Large": 3, "Extra Large": 4}
    risk_order = {"Low": 0, "Medium": 1, "High": 2}

    item_effort_val = effort_order.get(effort)
    item_risk_val = risk_order.get(risk)

    # Fallback for missing values: treat as Medium (safe middle ground)
    if item_effort_val is None:
        item_effort_val = effort_order["Medium"]
    if item_risk_val is None:
        item_risk_val = risk_order["Medium"]

    # High tier check (OR)
    if item_effort_val >= effort_order.get(high_min_effort, 3) or \
       item_risk_val >= risk_order.get(high_min_risk, 2):
        return "high"

    # Low tier check (AND)
    if item_effort_val <= effort_order.get(low_max_effort, 1) and \
       item_risk_val <= risk_order.get(low_max_risk, 0):
        return "low"

    return "medium"


# ---------------------------------------------------------------------------
# Idempotence check
# ---------------------------------------------------------------------------


def is_effort_risk_computed(work_item: dict, comments: list[dict]) -> bool:
    """Check whether effort and risk have already been computed for a work item.

    Returns True if:
    - Both effort and risk fields are non-empty, OR
    - Any comment contains the ``autoplan-decision-hash:`` marker.

    Arguments:
        work_item: The work item dict (from ``wl show --json``).
        comments: The comment list (from ``wl comment list --json``).
    """
    effort = (work_item.get("effort") or "").strip()
    risk = (work_item.get("risk") or "").strip()
    if effort and risk:
        return True
    for comment in comments:
        comment_text = comment.get("comment") or ""
        if "autoplan-decision-hash:" in comment_text:
            return True
    return False


# ---------------------------------------------------------------------------
# Effort-and-risk skill invocation
# ---------------------------------------------------------------------------


def run_effort_and_risk(
    target_id: str,
    runner: Callable[..., Any] | None = None,
) -> dict | None:
    """Run the effort-and-risk orchestration skill for a work item.

    Invokes ``skill/effort-and-risk/scripts/orchestrate_estimate.py`` via
    subprocess (or via the provided runner) and returns the parsed JSON
    result.

    When ``runner`` is provided, the payload is appended as a trailing CLI
    argument (the convention used by Ralph's FakeRunner in tests). When
    ``runner`` is None, the payload is supplied via stdin (production use).

    Returns None on failure (non-zero exit, invalid JSON, or error key in
    result).
    """
    skill_root = Path(__file__).resolve().parents[1]
    orchestrate_script = skill_root / "skill" / "effort-and-risk" / "scripts" / "orchestrate_estimate.py"
    payload = json.dumps({
        "issue_id": target_id,
        "o": 0, "m": 0, "p": 0,
        "certainty": 100,
        "assumptions": ["Auto-generated by autoplan decision"],
        "unknowns": [],
    })

    cmd = ["python3", str(orchestrate_script)]
    logger.info("plan_helpers.effort_risk.start target=%s", target_id)

    try:
        proc = _execute_subprocess(cmd, input_data=payload, runner=runner)
    except OSError as exc:
        logger.warning("plan_helpers.effort_risk.os_error target=%s exc=%s", target_id, exc)
        return None

    if proc.returncode != 0:
        logger.warning(
            "plan_helpers.effort_risk.failed target=%s rc=%s stderr=%s",
            target_id, proc.returncode, proc.stderr[:500],
        )
        return None

    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        logger.warning("plan_helpers.effort_risk.parse_error target=%s exc=%s", target_id, exc)
        return None

    if not isinstance(result, dict):
        logger.warning("plan_helpers.effort_risk.unexpected_type target=%s type=%s", target_id, type(result).__name__)
        return None

    if "error" in result:
        logger.warning("plan_helpers.effort_risk.error target=%s error=%s", target_id, result["error"][:200])
        return None

    logger.info(
        "plan_helpers.effort_risk.complete target=%s tshirt=%s risk=%s",
        target_id,
        result.get("effort", {}).get("tshirt", "unknown"),
        result.get("risk", {}).get("level", "unknown"),
    )
    return result


# ---------------------------------------------------------------------------
# Auto-plan decision comment posting (idempotent)
# ---------------------------------------------------------------------------


def append_autoplan_decision_comment(
    work_item_id: str,
    tshirt: str,
    risk_level: str,
    risk_score: int | float,
    do_plan: bool,
    author: str = "ralph",
    runner: Callable[..., Any] | None = None,
) -> None:
    """Post (or skip posting) an auto-plan decision comment, idempotently.

    Builds a deterministic hash marker from the decision values. If a comment
    with the same hash already exists, no new comment is posted.

    When ``runner`` is provided, uses it for all subprocess calls (test
    compatibility). When ``runner`` is None, uses direct subprocess calls
    (production/CLI use).
    """
    marker_key = f"autoplan-decision:{tshirt}:{risk_level}:{risk_score}"
    marker_hash = hashlib.sha256(marker_key.encode("utf-8")).hexdigest()[:16]
    marker = f"autoplan-decision-hash:{marker_hash}"

    # Check for existing comment with this marker
    if runner is not None:
        comment_list = _wl_comment_list(work_item_id, runner=runner)
    else:
        comment_list = _wl_comment_list(work_item_id)
    for existing in comment_list:
        if marker in (existing.get("comment") or ""):
            logger.debug(
                "plan_helpers.comment_exists target=%s marker=%s",
                work_item_id, marker,
            )
            return

    decision = (
        "run /plan (effort or risk above threshold)"
        if do_plan
        else "proceed to implement (effort and risk below threshold)"
    )
    comment_parts = [
        "# Ralph Auto-Plan Decision",
        marker,
        "",
        f"Effort: {tshirt}",
        f"Risk: {risk_level} (score: {risk_score})",
        f"Decision: {decision}",
    ]
    comment = "\n".join(comment_parts)
    if runner is not None:
        _wl_comment_add(work_item_id, comment, author=author, runner=runner)
    else:
        _wl_comment_add(work_item_id, comment, author=author)


# ---------------------------------------------------------------------------
# Top-level decision orchestrator
# ---------------------------------------------------------------------------


def make_autoplan_decision(
    target_id: str,
    config: dict,
    effort_skip: frozenset[str] | None = None,
    risk_skip: frozenset[str] | None = None,
    precomputed_item: dict | None = None,
    precomputed_comments: list[dict] | None = None,
    runner: Callable[..., Any] | None = None,
) -> tuple[bool, str, dict | None]:
    """Make an autoplan decision for a work item.

    Returns (do_plan, updated_stage, effort_risk):
    - do_plan: True if /plan should be invoked, False to skip
    - updated_stage: the effective stage after autoplan
        ("intake_complete" if skipping, "plan_complete" if planning)
    - effort_risk: dict with "effort" (tshirt) and "risk" (level) keys,
        or None if effort/risk could not be determined

    When ``precomputed_item`` and ``precomputed_comments`` are provided, the
    function uses those instead of fetching the work item from the worklog.
    This allows callers (like Ralph) to supply already-fetched data and avoid
    redundant wl calls.

    When ``runner`` is provided, uses it for all subprocess calls (enables
    test injection via Ralph's FakeRunner). When ``runner`` is None, uses
    direct subprocess calls (production/CLI use).
    """
    effort_skip = effort_skip or DEFAULT_AUTOPLAN_EFFORT_SKIP
    risk_skip = risk_skip or DEFAULT_AUTOPLAN_RISK_SKIP
    effort_risk: dict | None = None

    # Fetch work item data if not precomputed
    if precomputed_item is not None:
        item = precomputed_item
        comments = precomputed_comments or []
    elif runner is not None:
        item = _wl_show(target_id, runner=runner)
        comments = _wl_comment_list(target_id, runner=runner)
    else:
        item = _wl_show(target_id)
        comments = _wl_comment_list(target_id)

    # Idempotence check: skip re-computation if already computed
    if is_effort_risk_computed(item, comments):
        effort = (item.get("effort") or "").strip()
        risk = (item.get("risk") or "").strip()
        effort_risk = {"effort": effort, "risk": risk}
        do_plan = not (effort in effort_skip and risk in risk_skip)

        if do_plan:
            stage = item.get("stage", "unknown")
            if stage == "plan_complete":
                return False, "plan_complete", effort_risk
            return True, "plan_complete", effort_risk

        return False, "intake_complete", effort_risk

    # Run effort-and-risk skill
    do_plan: bool = True
    if runner is not None:
        er_result = run_effort_and_risk(target_id, runner=runner)
    else:
        er_result = run_effort_and_risk(target_id)

    if er_result is None:
        logger.info("plan_helpers.effort_risk_failed_defaults_to_plan target=%s", target_id)
        tshirt = "unknown"
        risk_level = "unknown"
        risk_score = 0
    else:
        tshirt = er_result.get("effort", {}).get("tshirt", "")
        risk_level = er_result.get("risk", {}).get("level", "")
        risk_score = er_result.get("risk", {}).get("score", 0)
        do_plan = not (tshirt in effort_skip and risk_level in risk_skip)
        logger.info(
            "plan_helpers.autoplan.result target=%s tshirt=%s risk=%s do_plan=%s",
            target_id, tshirt, risk_level, do_plan,
        )

    effort_risk = {"effort": tshirt, "risk": risk_level}

    # Post the decision comment idempotently
    if runner is not None:
        append_autoplan_decision_comment(target_id, tshirt, risk_level, risk_score, do_plan, runner=runner)
    else:
        append_autoplan_decision_comment(target_id, tshirt, risk_level, risk_score, do_plan)

    if do_plan:
        return True, "plan_complete", effort_risk

    logger.info("plan_helpers.autoplan.skip_plan target=%s", target_id)
    return False, "intake_complete", effort_risk


# ---------------------------------------------------------------------------
# Work item fetch via subprocess
# ---------------------------------------------------------------------------


def _wl_show(
    work_item_id: str,
    runner: Callable[..., Any] | None = None,
) -> dict:
    """Call ``wl show <id> --json`` and return the workItem dict.

    Returns an empty dict on failure.
    """
    cmd = ["wl", "show", work_item_id, "--json"]
    proc = _execute_subprocess(cmd, runner=runner)
    if proc.returncode != 0:
        logger.warning("wl show failed target=%s stderr=%s", work_item_id, proc.stderr)
        return {}
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        logger.warning("wl show invalid JSON target=%s", work_item_id)
        return {}
    if isinstance(data, dict) and data.get("success") is False:
        logger.warning("wl show returned error target=%s", work_item_id)
        return {}
    return data.get("workItem", {}) if isinstance(data, dict) else {}


# ---------------------------------------------------------------------------
# CLI entry points
# ---------------------------------------------------------------------------


def plan_if_needed(target_id: str) -> dict[str, Any]:
    """CLI entry point for ``plan-if-needed``.

    Returns a JSON-serializable dict with keys:
      - target_id
      - decision ("skip" | "plan")
      - effort
      - risk
    """
    do_plan, stage, _effort_risk = make_autoplan_decision(target_id, config={})
    return {
        "target_id": target_id,
        "decision": "plan" if do_plan else "skip",
        "effort": stage,  # For backward compat: the stage indicates what happens
        "risk": do_plan,
    }


def check_effort_risk(target_id: str) -> dict[str, Any]:
    """CLI entry point for ``check-effort-risk``.

    Only runs the effort-and-risk script and returns the result.
    Does NOT make a plan decision.

    Returns a JSON-serializable dict with the effort/risk values,
    or an error dict on failure.
    """
    result = run_effort_and_risk(target_id)
    if result is None:
        return {"target_id": target_id, "error": "effort-and-risk script failed"}
    effort = result.get("effort", {})
    risk = result.get("risk", {})
    return {
        "target_id": target_id,
        "effort": {"tshirt": effort.get("tshirt", "")},
        "risk": {"level": risk.get("level", ""), "score": risk.get("score", 0)},
    }


def main() -> None:
    """CLI entry point with argparse subcommands."""
    parser = argparse.ArgumentParser(
        description="Shared autoplan decision module",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # plan-if-needed subcommand
    plan_parser = subparsers.add_parser(
        "plan-if-needed",
        help="Check if a work item needs planning based on effort/risk thresholds",
    )
    plan_parser.add_argument("target_id", help="Work item ID to check")

    # check-effort-risk subcommand
    check_parser = subparsers.add_parser(
        "check-effort-risk",
        help="Run effort-and-risk skill for a work item and return the result",
    )
    check_parser.add_argument("target_id", help="Work item ID to check")

    args = parser.parse_args()

    if args.command == "plan-if-needed":
        result = plan_if_needed(args.target_id)
        print(json.dumps(result, indent=2))
    elif args.command == "check-effort-risk":
        result = check_effort_risk(args.target_id)
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
