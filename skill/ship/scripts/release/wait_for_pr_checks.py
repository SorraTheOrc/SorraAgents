#!/usr/bin/env python3
"""Background poller for GitHub PR status checks (SA-0MVDP9B83004UD5E).

This is the *job* half of the background-job etiquette pattern
(``skill/shared/background-job-etiquette.md``): the release wrapper launches
it detached (its stdout captured to a tailable log), then waits for it
completion-aware with the shared helper
[``wait_for_job.py``](../../shared/wait_for_job.py) — returning the moment the
outcome marker appears instead of after a blind fixed-length sleep.

It polls ``gh pr view <pr> --json statusCheckRollup`` until the PR's checks
reach a terminal state (or ``--timeout`` elapses) and writes a JSON outcome to
``--marker``. Outcomes:

- ``no_checks`` — the repo has no CI checks on the PR; the caller proceeds.
- ``passed``    — every present check completed successfully.
- ``failed``    — at least one check failed or was cancelled.
- ``timeout``   — the checks did not settle before the deadline.

CLI::

    wait_for_pr_checks.py --pr 42 --timeout 600 --interval 10 --marker /tmp/pc.json

Exit codes: 0 terminal success (``no_checks``/``passed``), 1 ``failed``,
2 ``timeout``, 3 usage error. The marker is written for every terminal
outcome so the caller can always read a decision.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_TIMEOUT = 2
EXIT_USAGE = 3

#: Default poll cadence (seconds) between ``gh`` queries.
DEFAULT_INTERVAL = 10.0


def classify_checks(checks: list) -> str | None:
    """Classify a ``statusCheckRollup`` into an outcome, or ``None`` if pending.

    Mirrors the release wrapper's historical semantics: no checks → proceed;
    any ``FAILURE``/``CANCELLED`` → block; all ``COMPLETED`` → pass; anything
    else → keep waiting.
    """
    if not checks:
        return "no_checks"
    if any(c.get("conclusion") in ("FAILURE", "CANCELLED") for c in checks):
        return "failed"
    if all(c.get("status") == "COMPLETED" for c in checks):
        return "passed"
    return None


def query_checks(pr: str) -> list | None:
    """Query the PR's statusCheckRollup via ``gh`` (``None`` on a failed query)."""
    try:
        proc = subprocess.run(
            ["gh", "pr", "view", str(pr), "--json", "statusCheckRollup"],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout or "{}").get("statusCheckRollup") or []
    except (ValueError, AttributeError):
        return None


def write_marker(path: str, outcome: str) -> None:
    """Write the terminal outcome JSON so the waiting caller can read it."""
    marker = Path(path)
    if marker.parent and not marker.parent.exists():
        marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"outcome": outcome}), encoding="utf-8")


def wait_for_checks(
    pr: str,
    *,
    timeout: float,
    interval: float = DEFAULT_INTERVAL,
    marker: str | None = None,
    out=None,
    json_output: bool = False,
) -> int:
    """Poll ``gh`` until the checks settle; write *marker*; return the exit code.

    The first query runs immediately, so an already-terminal PR (or no CI at
    all) completes without any sleep.
    """
    out = out or sys.stdout
    deadline = time.monotonic() + timeout
    while True:
        checks = query_checks(pr)
        outcome = classify_checks(checks) if checks is not None else None
        if outcome is not None:
            if marker:
                write_marker(marker, outcome)
            _emit(out, json_output, "outcome", outcome)
            if outcome == "failed":
                return EXIT_FAILED
            return EXIT_OK
        if time.monotonic() >= deadline:
            if marker:
                write_marker(marker, "timeout")
            _emit(out, json_output, "outcome", "timeout")
            return EXIT_TIMEOUT
        _emit(out, json_output, "waiting", f"checks for PR {pr} still pending")
        time.sleep(min(interval, max(0.0, deadline - time.monotonic())))


def _emit(out, json_output: bool, event: str, message: str) -> None:
    if json_output:
        out.write(json.dumps({"event": event, "message": message}) + "\n")
    else:
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        out.write(f"[{ts}] {event}: {message}\n")
    out.flush()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wait_for_pr_checks.py",
        description="Background poller for GitHub PR status checks "
                    "(SA-0MVDP9B83004UD5E); waited on completion-aware via "
                    "wait_for_job.py.",
    )
    parser.add_argument("--pr", required=True,
                        help="PR number or URL to poll")
    parser.add_argument("--timeout", type=float, required=True,
                        help="hard timeout in seconds (required)")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL,
                        help=f"poll cadence in seconds (default {DEFAULT_INTERVAL:g})")
    parser.add_argument("--marker", default=None,
                        help="write the terminal outcome JSON to this path")
    parser.add_argument("--json", action="store_true",
                        help="emit newline-delimited JSON progress lines")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.timeout <= 0:
        print("error: --timeout must be positive", file=sys.stderr)
        return EXIT_USAGE
    if args.interval <= 0:
        print("error: --interval must be positive", file=sys.stderr)
        return EXIT_USAGE
    return wait_for_checks(
        args.pr,
        timeout=args.timeout,
        interval=args.interval,
        marker=args.marker,
        json_output=args.json,
    )


if __name__ == "__main__":
    sys.exit(main())
