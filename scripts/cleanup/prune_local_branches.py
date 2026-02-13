from __future__ import annotations

import argparse
from typing import Any

from scripts.cleanup import lib


PROTECTED_BRANCHES = {"main", "master", "develop"}


def parse_branch_list(output: str) -> list[str]:
    return [line.strip() for line in output.splitlines() if line.strip()]


def is_merged(runner: lib.CommandRunner, branch: str, default_ref: str) -> bool:
    proc = runner.run(["git", "merge-base", "--is-ancestor", branch, default_ref])
    return proc.returncode == 0


def get_current_branch(runner: lib.CommandRunner) -> str:
    proc = runner.run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    return proc.stdout.strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Prune local branches merged into the default branch."
    )
    lib.add_common_args(parser)
    parser.add_argument("--default", help="Override default branch name")
    parser.add_argument(
        "--fetch",
        action="store_true",
        help="Fetch and prune remote tracking branches before scanning",
    )
    args = parser.parse_args(argv)

    lib.configure_logging(args.verbose)
    runner = lib.CommandRunner()

    if not lib.ensure_tool_available("git"):
        lib.exit_with_error("git is required")

    if args.fetch:
        lib.run_command(
            ["git", "fetch", "origin", "--prune"],
            dry_run=args.dry_run,
            destructive=False,
            runner=runner,
        )

    default_branch = lib.get_default_branch(runner, args.default)
    default_ref = lib.get_default_ref(runner, default_branch)
    current_branch = get_current_branch(runner)

    list_proc = runner.run(
        ["git", "for-each-ref", "--format=%(refname:short)", "refs/heads/"]
    )
    branches = parse_branch_list(list_proc.stdout)

    actions: list[dict[str, Any]] = []
    for branch in branches:
        if branch in PROTECTED_BRANCHES:
            actions.append(
                {
                    "branch": branch,
                    "action": "skip",
                    "result": "protected",
                }
            )
            continue
        if branch == current_branch:
            actions.append(
                {
                    "branch": branch,
                    "action": "skip",
                    "result": "current",
                }
            )
            continue
        merged = is_merged(runner, branch, default_ref)
        if not merged:
            actions.append(
                {
                    "branch": branch,
                    "action": "skip",
                    "result": "not_merged",
                }
            )
            continue
        if not lib.confirm_action(
            f"Delete local branch '{branch}'?", args.yes, args.dry_run
        ):
            actions.append(
                {
                    "branch": branch,
                    "action": "skip",
                    "result": "declined",
                }
            )
            continue
        proc = lib.run_command(
            ["git", "branch", "-d", branch],
            dry_run=args.dry_run,
            destructive=True,
            runner=runner,
        )
        actions.append(
            {
                "branch": branch,
                "action": "delete",
                "result": "deleted" if proc.returncode == 0 else "failed",
                "stderr": proc.stderr.strip(),
            }
        )

    report = {
        "operation": "prune_local_branches",
        "default_branch": default_branch,
        "dry_run": args.dry_run,
        "actions": actions,
        "summary": lib.render_summary(actions),
    }
    lib.write_report(report, args.report, print_output=not args.quiet)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
