from __future__ import annotations

import argparse
import json
from typing import Any

from skill.cleanup.scripts import lib


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Summarize open PRs targeting default branch"
    )
    lib.add_common_args(parser)
    parser.add_argument("--default", help="Override default branch name")
    args = parser.parse_args(argv)

    lib.configure_logging(args.verbose)
    runner = lib.CommandRunner()

    default_branch = lib.parse_default_branch(runner, args.default)

    if not lib.ensure_tool_available("gh"):
        report = {
            "operation": "summarize_open_prs",
            "warning": "gh not available; cannot list PRs",
            "prs": [],
        }
        lib.write_report(report, args.report, print_output=not args.quiet)
        return 0

    proc = runner.run(
        [
            "gh",
            "pr",
            "list",
            "--state",
            "open",
            "--base",
            default_branch,
            "--json",
            "number,title,headRefName,url,author",
        ]
    )  # noqa: E501
    payload = lib.parse_json_payload(proc.stdout)
    report = {
        "operation": "summarize_open_prs",
        "default_branch": default_branch,
        "prs": payload or [],
    }
    lib.write_report(report, args.report, print_output=not args.quiet)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
