from __future__ import annotations

import argparse
import json
from typing import Any

from skill.cleanup.scripts import lib


def get_unpushed_count(runner: lib.CommandRunner, branch: str) -> int:
    if not branch:
        return 0
    proc = runner.run(
        [
            "git",
            "rev-list",
            "--count",
            f"refs/remotes/origin/{branch}..refs/heads/{branch}",
        ]
    )
    try:
        return int(proc.stdout.strip() or 0)
    except Exception:
        return 0


def get_last_commit(branch: str, runner: lib.CommandRunner) -> dict[str, Any]:
    proc = runner.run(["git", "log", "-1", "--format=%H%x09%ci%x09%an", branch])
    if proc.returncode != 0 or not proc.stdout.strip():
        return {}
    parts = proc.stdout.strip().split("\t")
    return (
        {"sha": parts[0], "date": parts[1], "author": parts[2]}
        if len(parts) >= 3
        else {}
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Inspect current branch and default branch status"
    )
    lib.add_common_args(parser)
    parser.add_argument("--default", help="Override default branch name")
    args = parser.parse_args(argv)

    lib.configure_logging(args.verbose)
    runner = lib.CommandRunner()

    if not lib.ensure_tool_available("git"):
        lib.exit_with_error("git is required")

    default_branch = lib.parse_default_branch(runner, args.default)
    default_ref = lib.get_default_ref(runner, default_branch)
    current_branch = runner.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"]
    ).stdout.strip()

    merged = False
    if current_branch and default_ref:
        merged = (
            runner.run(
                ["git", "merge-base", "--is-ancestor", "HEAD", default_ref]
            ).returncode
            == 0
        )

    last_commit = get_last_commit(current_branch, runner)
    unpushed = get_unpushed_count(runner, current_branch)

    # parse work item token from branch name
    token = ""
    wid = ""
    import re

    m = re.search(r"([A-Za-z]+-[0-9]+)", current_branch or "")
    if m:
        token = m.group(1)
        if token.rsplit("-", 1)[-1].isdigit():
            wid = token

    result = {
        "current_branch": current_branch,
        "default_branch": default_branch,
        "merged_into_default": merged,
        "last_commit": last_commit,
        "unpushed_commits": unpushed,
        "work_item_token": token,
        "work_item_id": wid,
    }

    lib.write_report(result, args.report, print_output=not args.quiet)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
