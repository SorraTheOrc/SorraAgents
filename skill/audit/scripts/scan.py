#!/usr/bin/env python3
"""scan.py — bounded-scan helper for audit agents.

Work item: SA-0MSBR0LLT006JCXN (parent SA-0MSAEJCP7002LTIM — "Optimize audit
grep scans: only perform required and efficient scans").

Audit agents (Phase 2 deep analysis, launched with
``--tools read,bash,grep,find,ls``) historically improvised slow, unbounded
recursive greps over ``.worklog/`` (9.5 GB of audit_debug jsonl) and repo
roots. This CLI replaces those agent-decided scans with bounded,
required-and-efficient operations:

    scan.py find-workitem <work-item-id>
        Resolve a work item via `wl search` (structured index lookup,
        milliseconds) — never a recursive grep over .worklog/.

    scan.py search-code <pattern> [--path PATH] [--type TYPE] [--max-filesize N]
        Bounded rg wrapper: prunes node_modules/.git/.worklog and
        **/audit_debug_*.jsonl, enforces a max file size, and requires an
        explicit path (no implicit repo-root scan).

    scan.py list-files [--path PATH] [--type TYPE] [--maxdepth N]
        Bounded file listing with maxdepth (default 2) and the same prunes.

All subcommands run offline (stdlib + wl/rg only), never scan the repo root
implicitly, and exit non-zero with a clear message when nothing is found.
See ``docs/dev/audit-grep-scan-patterns.md`` for the recipe catalogue.
"""  # noqa: EXE001
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

# Fixed prunes shared by search-code and list-files. These keep scans out of
# the dirs that made legacy `grep -r .` slow (node_modules, .git) and out of
# the 9.5 GB .worklog audit_debug dump. Each is an rg `-g` glob pair; the
# flattened CLI form is built in _build_search_cmd/_build_list_cmd.
PRUNE_GLOBS = [
    "!node_modules/**",
    "!.git/**",
    "!.worklog/**",
    "!.audit_debug/**",
    "!**/audit_debug_*.jsonl",
]


def _prune_flag_pairs() -> list[str]:
    """Flatten PRUNE_GLOBS into alternating `-g <glob>` CLI args."""
    out: list[str] = []
    for g in PRUNE_GLOBS:
        out += ["-g", g]
    return out

DEFAULT_MAX_FILESIZE = "64M"
DEFAULT_MAXDEPTH = 2

MAX_MATCH_LINES = 200  # bound match output so a huge hit list cannot flood


def _run(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Run a command, returning the CompletedProcess (no exceptions)."""
    return subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, timeout=600,
        check=False,
    )


def cmd_find_workitem(args: argparse.Namespace) -> int:
    """Resolve a work item via `wl search`; never grep .worklog/ recursively."""
    wl = shutil.which("wl")
    if not wl:
        print("error: `wl` (worklog CLI) not found on PATH", file=sys.stderr)
        return 2
    cmd = [wl, "search", args.work_item_id, "--json"]
    if getattr(args, "worklog_dir", None):
        cmd += ["--worklog-dir", args.worklog_dir]
    proc = _run(cmd)
    if proc.returncode != 0:
        print(
            f"error: `wl search` failed for {args.work_item_id}: "
            f"{proc.stderr.strip() or proc.stdout.strip()}",
            file=sys.stderr,
        )
        return 1
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        print(f"error: unparseable `wl search` output for {args.work_item_id}",
              file=sys.stderr)
        return 1

    items = []
    if isinstance(data, dict):
        items = data.get("items") or data.get("workItems") or (
            [data["workItem"]] if data.get("workItem") else []
        )
    elif isinstance(data, list):
        items = data

    exact = [it for it in items if str(it.get("id", "")) == args.work_item_id]
    if not exact:
        print(f"Work item {args.work_item_id} not found.", file=sys.stderr)
        return 1
    print(json.dumps(exact[0], indent=2))
    return 0


def _rg_or_grep() -> str:
    """Prefer rg; fall back to bounded grep with --include prunes."""
    rg = shutil.which("rg")
    if rg:
        return rg
    grep = shutil.which("grep")
    if not grep:
        print("error: neither `rg` nor `grep` found on PATH", file=sys.stderr)
        raise SystemExit(2)
    return grep


def _build_search_cmd(pattern: str, path: str, type_: str | None,
                      max_filesize: str) -> list[str]:
    """Build a bounded search command (rg preferred, grep fallback)."""
    tool = _rg_or_grep()
    cmd = [tool]
    if Path(tool).name == "rg":
        cmd += ["-l", "--max-filesize", max_filesize]
        cmd += _prune_flag_pairs()
        if type_:
            cmd += ["-g", f"*.{type_}"]
        # --hidden so .worklog (hidden) can be searched when explicitly asked,
        # but .git stays pruned by PRUNE_GLOBS.
        cmd += ["--hidden"]
        cmd += [pattern, path]
    else:
        # grep fallback: bounded with --include and --exclude-dir prunes.
        cmd += ["-r", "-l", "--max-filesize", max_filesize]
        if type_:
            cmd += ["--include", f"*.{type_}"]
        for prune in ("node_modules", ".git", ".worklog"):
            cmd += ["--exclude-dir", prune]
        cmd += ["--exclude", "audit_debug_*.jsonl"]
        cmd += [pattern, path]
    return cmd


def cmd_search_code(args: argparse.Namespace) -> int:
    """Bounded rg/grep over an explicit path with fixed prunes."""
    path = args.path or "."
    cmd = _build_search_cmd(args.pattern, path, args.type, args.max_filesize)
    proc = _run(cmd)
    if proc.returncode == 0:
        lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
        for ln in lines[:MAX_MATCH_LINES]:
            print(ln)
        if len(lines) > MAX_MATCH_LINES:
            print(f"... ({len(lines) - MAX_MATCH_LINES} more matches truncated)",
                  file=sys.stderr)
        return 0
    if proc.returncode == 1:
        print(f"No matches for {args.pattern!r} under {path}.", file=sys.stderr)
        return 1
    print(f"error: search failed: {proc.stderr.strip()[:300]}", file=sys.stderr)
    return 2


def _build_list_cmd(path: str, type_: str | None, maxdepth: int) -> list[str]:
    """Build a bounded file-listing command (rg preferred, find fallback)."""
    tool = _rg_or_grep()
    cmd = [tool]
    if Path(tool).name == "rg":
        cmd += ["--files", "--hidden"]
        cmd += _prune_flag_pairs()
        if type_:
            cmd += ["-g", f"*.{type_}"]
        cmd += [path]
    else:
        cmd = ["find", path, "-type", "f", "-maxdepth", str(maxdepth)]
        if type_:
            cmd += ["-name", f"*.{type_}"]
        for prune in ("node_modules", ".git", ".worklog"):
            cmd += ["-not", "-path", f"*/{prune}/*"]
        cmd += ["-not", "-name", "audit_debug_*.jsonl"]
    return cmd


def cmd_list_files(args: argparse.Namespace) -> int:
    """Bounded file listing with maxdepth and prunes."""
    path = args.path or "."
    cmd = _build_list_cmd(path, args.type, args.maxdepth)
    proc = _run(cmd)
    if proc.returncode == 0:
        lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
        for ln in lines[:MAX_MATCH_LINES]:
            print(ln)
        if len(lines) > MAX_MATCH_LINES:
            print(f"... ({len(lines) - MAX_MATCH_LINES} more files truncated)",
                  file=sys.stderr)
        return 0
    print(f"error: listing failed: {proc.stderr.strip()[:300]}", file=sys.stderr)
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scan.py",
        description=(
            "Bounded-scan helper for audit agents: find-workitem / "
            "search-code / list-files. Replaces slow unbounded grep -r "
            "scans with pruned, bounded operations."
        ),
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)

    p_find = sub.add_parser("find-workitem", help="Resolve a work item via wl search")
    p_find.add_argument("work_item_id", help="Work item id, e.g. SA-XXXXXXXXXXX")
    p_find.add_argument("--worklog-dir", default=None,
                        help="Explicit path to .worklog directory (bypasses auto-resolution)")
    p_find.set_defaults(func=cmd_find_workitem)

    p_search = sub.add_parser("search-code", help="Bounded rg/grep for a pattern")
    p_search.add_argument("pattern", help="Regex/pattern to search for")
    p_search.add_argument("--path", default=None, help="Explicit path to search (default: current dir)")
    p_search.add_argument("--type", default=None, help="File extension filter, e.g. py, ts, md")
    p_search.add_argument("--max-filesize", default=DEFAULT_MAX_FILESIZE,
                          help=f"Max file size to scan (default {DEFAULT_MAX_FILESIZE})")
    p_search.set_defaults(func=cmd_search_code)

    p_list = sub.add_parser("list-files", help="Bounded file listing")
    p_list.add_argument("--path", default=None, help="Path to list (default: current dir)")
    p_list.add_argument("--type", default=None, help="File extension filter, e.g. py, ts, md")
    p_list.add_argument("--maxdepth", type=int, default=DEFAULT_MAXDEPTH,
                        help=f"Max directory depth (default {DEFAULT_MAXDEPTH})")
    p_list.set_defaults(func=cmd_list_files)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
