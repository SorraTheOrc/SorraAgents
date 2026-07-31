#!/usr/bin/env python3
"""
run_skill.py

Convenience wrapper to run the canonical skill flow for an issue.

Usage (example):
  python3 run_skill.py --issue SA-0MKT9O9AY002COU8 --o 2 --m 4 --p 8 --coord 1 --review 1 --testing 1 --risk_buffer 1 --certainty 85

The script will:
 - fetch the issue and its children with `wl show <issue> --children --json`
  - assemble a JSON payload using provided O/M/P (and optional per-item estimates) and overheads (defaults applied if omitted)
 - call orchestrate_estimate.py with the assembled payload and print the final JSON output

Status lifecycle: the pre-run status is captured and restored
deterministically. The StatusLifecycle context manager is deliberately NOT
used here — its success exit sets status=completed, which would violate the
documented lifecycle for intake/planning items (status stays `open` until the
post-release close). See SA-0MS93J0ZC007IO8V.
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

# Ensure the repository root is on sys.path so skill package imports work
_REPO_ROOT = Path(__file__).resolve().parents[3]  # <repo>/skill/effort-and-risk/scripts/
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from skill.shared.status_lifecycle import StatusLifecycle, run_wl


def wl_show(issue_id):
    """Fetch an issue with children via the shared run_wl helper.

    The shared helper resolves the target worklog dir and surfaces real
    error detail on failure.
    """
    try:
        return run_wl(["wl", "show", issue_id, "--children", "--json"])
    except RuntimeError as exc:
        print(json.dumps({"error": "wl show failed", "detail": str(exc)}))
        sys.exit(2)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--issue", required=True)
    parser.add_argument("--o", type=float, default=2.0)
    parser.add_argument("--m", type=float, default=4.0)
    parser.add_argument("--p", type=float, default=8.0)
    parser.add_argument("--coord", type=float, default=1.0)
    parser.add_argument("--review", type=float, default=1.0)
    parser.add_argument("--testing", type=float, default=1.0)
    parser.add_argument("--risk_buffer", type=float, default=1.0)
    parser.add_argument("--certainty", type=float, default=85.0)
    parser.add_argument("--parent_prob", type=float, default=3.0)
    parser.add_argument("--parent_imp", type=float, default=3.0)
    parser.add_argument("--assumptions", type=str, default="[]")
    parser.add_argument("--unknowns", type=str, default="[]")
    args = parser.parse_args()

    issue_id = args.issue

    # Capture the pre-run status so it can be restored deterministically.
    # The flow itself never changes status (effort/risk fields and the comment
    # are updated by the orchestrator); restoring guarantees the item is not
    # left in an unintended state if that ever changes.
    show = wl_show(issue_id)
    original_status = (show.get("workItem") or {}).get("status", "open")

    try:
        def flatten_children(children):
            out = []
            for c in children or []:
                out.append(c)
                out.extend(flatten_children(c.get("children", [])))
            return out

        # Collect children (recursive) if present
        children_nodes = flatten_children(show.get("children", []))
        children_info = []
        for c in children_nodes:
            cid = c.get("id")
            title = c.get("title", "")
            children_info.append({"id": cid, "title": title, "probability": 2, "impact": 1})

        # Build WBS items from child work items (with proportionate O/M/P distribution)
        items = []
        if children_nodes:
            # Distribute O/M/P proportionally across children when we have O/M/P totals
            total_omp = args.o + args.m + args.p
            if total_omp > 0:
                for c in children_nodes:
                    cid = c.get("id", "")
                    title = c.get("title", "")
                    # Equal split as default when no per-item estimates provided
                    weight = 1.0 / len(children_nodes)
                    items.append({
                        "id": cid,
                        "title": title,
                        "o": round(args.o * weight, 2),
                        "m": round(args.m * weight, 2),
                        "p": round(args.p * weight, 2),
                    })

        payload = {
            "o": args.o,
            "m": args.m,
            "p": args.p,
            "overheads": {
                "coordination": args.coord,
                "review": args.review,
                "testing": args.testing,
                "risk_buffer": args.risk_buffer,
            },
            "parent": {"probability": args.parent_prob, "impact": args.parent_imp},
            "children": children_info,
            "items": items,
            "certainty": args.certainty,
            "assumptions": json.loads(args.assumptions),
            "unknowns": json.loads(args.unknowns),
            "issue_id": issue_id,
        }

        if not sys.stdin.isatty():
            try:
                stdin_payload = json.load(sys.stdin)
                if isinstance(stdin_payload, dict):
                    # Allow stdin override to replace items if provided
                    if "items" in stdin_payload:
                        payload["items"] = stdin_payload["items"]
                    payload.update(stdin_payload)
            except Exception:  # noqa: S110, BLE001 -- stdin parsing enhancement, ignore on failure
                pass

        # Call orchestrate_estimate.py located in the same scripts directory
        script_dir = os.path.dirname(__file__)
        orchestrator = os.path.join(script_dir, "orchestrate_estimate.py")
        proc = subprocess.run(
            ["python3", orchestrator],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            check=False,
        )
        if proc.returncode != 0:
            print(
                json.dumps(
                    {
                        "error": "orchestrator failed",
                        "stdout": proc.stdout,
                        "stderr": proc.stderr,
                    }
                )
            )
            sys.exit(3)

        # Print orchestrator output
        print(proc.stdout)
    finally:
        # Restore the pre-run status (never flip to completed).
        try:
            StatusLifecycle.update_status(issue_id, original_status)
        except RuntimeError as exc:
            print(
                f"WARNING: failed to restore status {original_status!r} "
                f"for {issue_id}: {exc}",
                file=sys.stderr,
            )


if __name__ == "__main__":
    main()
