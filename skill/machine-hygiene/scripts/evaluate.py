#!/usr/bin/env python3
"""
Machine-hygiene orchestrator.

Runs the full 6-step workflow:

1. collect metrics (before table)
2. analyse pressures
3. propose remediation actions
4. approval gate (mandatory)
5. execute only the approved actions
6. collect metrics again (after table) and report residual load

Read-only by default: nothing destructive happens without explicit operator
approval at Step 4. ``--dry-run`` previews without executing.

Usage
-----
    python3 evaluate.py            # interactive approval gate
    python3 evaluate.py --json     # machine-readable output
    python3 evaluate.py --yes      # non-interactive approval (explicit)
    python3 evaluate.py --dry-run  # never execute
"""

import argparse
import importlib.util
import json
import os
import sys
from collections.abc import Callable
from typing import Any

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(_SCRIPT_DIR, filename)
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_metrics = _load("machine_hygiene_metrics", "metrics.py")
_pa = _load("machine_hygiene_pressure_analysis", "pressure_analysis.py")
_tables = _load("machine_hygiene_tables", "tables.py")
_actions = _load("machine_hygiene_actions", "actions.py")


def run_workflow(
    *,
    metrics_fn: Callable[[], dict[str, Any]] | None = None,
    ancestry_fn: Callable[[int], dict[str, Any]] | None = None,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
    auto_approve: bool = False,
    dry_run: bool = False,
    ascii_only: bool = False,
) -> dict[str, Any]:
    """Run the full workflow and return a structured result dict."""
    metrics_fn = metrics_fn or _metrics.collect_metrics
    ancestry_fn = ancestry_fn or _pa.find_ancestry

    # Step 1 — before metrics.
    before = metrics_fn()
    # Step 2 — pressure analysis.
    pressures = _pa.analyze_pressures(before, ancestry_fn=ancestry_fn)
    # Step 3 — remediation proposal.
    actions = _pa.propose_remediations_from_metrics(before, pressures)

    # Step 4 — approval gate.
    if auto_approve:
        approved = list(actions)
    else:
        output_fn(_tables.render_approval_gate(
            before, pressures, actions, ascii_only=ascii_only
        ))
        approved = _tables.prompt_for_approval(
            actions, input_fn=input_fn, output_fn=output_fn
        )
    approved_ids = {id(a) for a in approved}
    declined = [a for a in actions if id(a) not in approved_ids]

    # Step 5 — execute approved only.
    outcome = _actions.execute_actions(
        approved, declined, dry_run=dry_run, ancestry_fn=ancestry_fn
    )

    # Step 6 — after metrics + tables.
    after = metrics_fn()
    before_table = _tables.render_before_table(before, ascii_only=ascii_only)
    after_table = _tables.render_after_table(before, after, ascii_only=ascii_only)

    return {
        "before": before,
        "pressures": pressures,
        "actions": actions,
        "approved": approved,
        "declined": declined,
        "outcome": outcome,
        "after": after,
        "before_table": before_table,
        "after_table": after_table,
    }


def render_human(result: dict[str, Any]) -> str:
    """Render the workflow result as human-readable text."""
    parts = [result["before_table"], "", "## Pressure analysis", ""]
    if result["pressures"]:
        for pressure in result["pressures"]:
            parts.append(
                f"- **{pressure['metric']}** ({pressure['status']}): "
                f"{pressure.get('summary', '')}"
            )
    else:
        parts.append("No actionable pressure detected.")

    parts.extend(["", _tables.render_proposed_actions(result["actions"])])

    outcome = result["outcome"]
    parts.extend([
        "",
        "## Outcome",
        "",
        f"- Approved: {outcome['approved_count']}",
        f"- Declined: {outcome['declined_count']}",
        f"- Executed: {len(outcome['executed'])}",
        f"- Dry-run: {len(outcome['dry_run'])}",
        f"- Skipped: {len(outcome['skipped'])}",
        f"- Failed: {len(outcome['failed'])}",
        "",
        result["after_table"],
    ])
    return "\n".join(parts)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Machine-hygiene diagnostic and remediation")
    parser.add_argument("--json", action="store_true", help="emit JSON output")
    parser.add_argument(
        "--yes", action="store_true",
        help="non-interactive: treat all proposed actions as approved",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="preview actions without executing them (read-only)",
    )
    parser.add_argument(
        "--ascii", action="store_true",
        help="use ASCII status labels instead of Unicode symbols",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: parse args, run the workflow and emit output.

    Returns 0 on success, 3 when actions were proposed but none approved,
    and 1 on error.
    """
    args = _build_parser().parse_args(argv)
    try:
        result = run_workflow(
            auto_approve=args.yes,
            dry_run=args.dry_run,
            ascii_only=args.ascii,
        )
    except Exception as exc:  # noqa: BLE001 — surface a clean CLI error
        print(f"machine-hygiene error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        payload = {
            "before": result["before"],
            "pressures": result["pressures"],
            "actions": result["actions"],
            "approved_count": result["outcome"]["approved_count"],
            "declined_count": result["outcome"]["declined_count"],
            "results": result["outcome"]["results"],
            "after": result["after"],
        }
        print(json.dumps(payload, indent=2, default=str))
    else:
        print(render_human(result))

    if result["actions"] and result["outcome"]["approved_count"] == 0:
        return 3  # approval declined / nothing executed
    return 0


if __name__ == "__main__":
    sys.exit(main())
