#!/usr/bin/env python3
"""Run the full project test suite in quiet mode and report every failure.

Suites:
  - pytest:  pytest -q -r a --disable-warnings   (canonicalized via skill/test_runner.py)
  - node:    node --test "tests/<dir>/**/*.mjs"   (npm --silent test when a test script exists)

Results are cached per-repo (see skill/test_cache.py) by default so repeated
verification at the same git state is served without re-executing the suite.

Emits structured per-failure records (test_name, stdout_excerpt, stack_trace)
compatible with the triage skill's check_or_create.py input.

Usage:
  run_tests.py [--suite pytest|node|all] [--json] [--parent-work-item-id ID] [--rerun-failures]
  run_tests.py --summary [--summary-grep PATTERN]   (read cached summary lines, no execution)
  run_tests.py --force | --no-cache                (bypass the cache)

Exit codes:
  0 - all suites passed
  1 - at least one test failed (or --summary cache miss)
  2 - runner error (missing suite binary etc.)
"""  # noqa: EXE001

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from skill.test_cache import (
    DEFAULT_TTL_SECONDS,
    query_cached,
    run_cached,
    summary_lines,
)
from skill.test_runner import canonicalize_quiet_test_command

REPO_ROOT = _REPO_ROOT

# Cache TTL exposed as a module constant so CLI tests can backdate entries.
CACHE_TTL_SECONDS = DEFAULT_TTL_SECONDS

PYTEST_CMD = canonicalize_quiet_test_command("pytest")
NODE_SUITE_DIRS = ("tests/node", "tests/cli", "tests/unit")

_FAILED_RE = re.compile(r"^FAILED\s+(.+?)\s+-\s+(.*)$", re.MULTILINE)
_SECTION_RE = re.compile(r"^_{5,}\s+(.+?)\s+_{5,}$", re.MULTILINE)
_NODE_NOT_OK_RE = re.compile(r"^not ok\s+\d+\s*-\s*(.+)$", re.MULTILINE)


# ---------------------------------------------------------------------------
# Command construction
# ---------------------------------------------------------------------------


def pytest_command() -> str:
    """Return the quiet canonicalized pytest command."""
    return PYTEST_CMD


def node_suite_commands(project_root: Path | None = None) -> list[str]:
    """Return one quiet node test command per Node suite directory.

    *project_root* defaults to ``REPO_ROOT``; pass another root to build the
    suite commands for a different project (e.g. a sibling repo targeted by
    the audit skill's read-only full-suite verification).

    Prefers ``npm --silent test`` when the repo has an npm test script;
    otherwise falls back to ``node --test <glob>`` for each suite dir.

    Globs are required (SA-0MSF8KNE3003JDVD): on node v22.22.1 ``node --test
    <dir>`` treats a bare directory argument as a module entry point and fails
    with ``MODULE_NOT_FOUND``; only glob patterns (e.g.
    ``node --test "tests/node/**/*.mjs"``) are scanned as test files.
    """
    root = Path(project_root or REPO_ROOT)
    pkg_json = root / "package.json"
    has_npm_test_script = False
    if pkg_json.exists():
        try:
            import json as _json

            has_npm_test_script = "test" in _json.loads(pkg_json.read_text()).get("scripts", {})
        except Exception:  # noqa: BLE001
            has_npm_test_script = False

    cmds: list[str] = []
    for suite_dir in NODE_SUITE_DIRS:
        if has_npm_test_script:
            cmds.append(canonicalize_quiet_test_command(f"npm test -- {suite_dir}"))
        else:
            cmds.append(f'node --test "{suite_dir}/**/*.mjs"')
    return cmds


def full_suite_commands(project_root: Path | None = None) -> list[str]:
    """Return the canonical full-suite command set for *project_root*.

    The set that constitutes "the full project test suite": the quiet
    canonicalized pytest command plus one node command per suite directory.
    Read-only consumers (e.g. the audit skill's automatic full-suite
    verification, SA-0MSIU5HFI0024D7W) query the per-repo test cache with
    exactly these commands so cache entries written by ``run_tests.py`` are
    reused without executing anything.
    """
    return [pytest_command()] + node_suite_commands(project_root)


# ---------------------------------------------------------------------------
# Failure parsing
# ---------------------------------------------------------------------------


def parse_pytest_failures(output: str) -> list[dict[str, str]]:
    """Parse pytest ``-r a`` output into per-failure structured records.

    Each record contains ``test_name``, ``stdout_excerpt`` and ``stack_trace``
    in the shape expected by triage check_or_create.py.
    """
    records: list[dict[str, str]] = []
    failed = list(_FAILED_RE.finditer(output))
    for match in failed:
        test_name = match.group(1).strip()
        # Extract the traceback section for this test from the FAILURES block.
        stack_trace = _extract_pytest_section(output, test_name)
        excerpt = stack_trace[:1000] if stack_trace else match.group(2).strip()
        records.append(
            {
                "test_name": test_name,
                "stdout_excerpt": excerpt,
                "stack_trace": stack_trace or excerpt,
            }
        )
    return records


def _extract_pytest_section(output: str, test_name: str) -> str:
    """Return the pytest FAILURES section body for a given test name."""
    # The section header is the test node id's tail (function name or full id).
    tail = test_name.split("::")[-1]
    lines = output.splitlines()
    section_start = None
    for i, line in enumerate(lines):
        m = _SECTION_RE.match(line.strip())
        if not m:
            continue
        header = m.group(1).strip()
        if header == tail or header == test_name or header.endswith("::" + tail):
            section_start = i
            break
    if section_start is None:
        return ""
    body: list[str] = []
    for line in lines[section_start + 1 :]:
        if _SECTION_RE.match(line.strip()) or line.startswith("====="):
            break
        body.append(line)
    return "\n".join(body).strip()


def parse_node_failures(output: str) -> list[dict[str, str]]:
    """Parse Node TAP output into per-failure structured records."""
    records: list[dict[str, str]] = []
    for match in _NODE_NOT_OK_RE.finditer(output):
        test_name = match.group(1).strip()
        block = _extract_node_block(output, test_name)
        error = _extract_yaml_block(block, "error")
        stack = _extract_yaml_block(block, "stack")
        stack_trace = stack or error or test_name
        excerpt = (error or stack or test_name)[:1000]
        records.append(
            {
                "test_name": test_name,
                "stdout_excerpt": excerpt,
                "stack_trace": stack_trace,
            }
        )
    return records


def _extract_node_block(output: str, test_name: str) -> str:
    """Return the TAP YAML block following the ``not ok`` line for a test."""
    idx = output.find("not ok")
    while idx != -1:
        line_end = output.find("\n", idx)
        if line_end == -1:
            break
        header = output[idx:line_end]
        if test_name in header:
            block_end = output.find("\n  ...", line_end)
            if block_end == -1:
                block_end = output.find("\n1..", line_end)
            return output[line_end : block_end if block_end != -1 else len(output)]
        idx = output.find("not ok", line_end)
    return ""


def _extract_yaml_block(block: str, key: str) -> str:
    """Return the indented YAML block value for ``key: |-`` within a TAP block."""
    marker = f"{key}: |-"
    start = block.find(marker)
    if start == -1:
        return ""
    start += len(marker)
    lines = block[start:].splitlines()
    collected: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not line.startswith("  ") and not line.startswith("\t"):
            break
        collected.append(line)
    return "\n".join(collected).strip()


# ---------------------------------------------------------------------------
# Suite execution
# ---------------------------------------------------------------------------


def _run_cmd(cmd: list[str], cwd: Path, timeout: int = 600) -> subprocess.CompletedProcess:
    """Run a suite command capturing stdout/stderr."""
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _cached_runner(command: str, cwd: str, timeout: int) -> subprocess.CompletedProcess:
    """Adapter exposing _run_cmd to the cache runner protocol.

    Uses shlex.split (not str.split) so quoted args like
    ``node --test "tests/node/**/*.mjs"`` pass the glob without literal
    quotes — otherwise node matches zero files and the suite trivially
    "passes" with 0 tests (pre-existing bug fixed with SA-0MSGN5OJ4002OZKY).
    """
    return _run_cmd(shlex.split(command), cwd=Path(cwd), timeout=timeout)


def run_suite(
    name: str,
    cwd: Path | None = None,
    timeout: int = 600,
    use_cache: bool = True,
    force: bool = False,
    no_cache: bool = False,
) -> dict[str, Any]:
    """Run a single named suite and return structured results.

    By default each command is routed through the per-repo cache: a valid
    cached result (same git state, within TTL) is served without executing.
    ``force`` bypasses lookup (still stores), ``no_cache`` bypasses lookup
    and storage. The result carries ``cached`` (True when every command in
    the suite was served from cache) and ``command`` for display.

    Returns a dict with ``success``, ``returncode``, ``failures``, ``command``,
    ``cached`` and (on missing binary) ``notice``.
    """
    cwd = cwd or REPO_ROOT
    if name == "pytest":
        command = pytest_command()
        commands = [command]
    elif name == "node":
        commands = node_suite_commands()
        command = " && ".join(commands)
    else:
        raise ValueError(f"unknown suite: {name}")

    all_failures: list[dict[str, str]] = []
    overall_returncode = 0
    cached_flags: list[bool] = []
    for cmd in commands:
        try:
            if use_cache:
                run = run_cached(
                    cmd,
                    cwd=str(cwd),
                    force=force,
                    no_cache=no_cache,
                    ttl=CACHE_TTL_SECONDS,
                    timeout=timeout,
                    runner=_cached_runner,
                )
                proc = SimpleNamespace(
                    stdout=run["stdout"], stderr=run["stderr"], returncode=run["exit_code"]
                )
                cached_flags.append(run["cached"])
            else:
                proc = _run_cmd(shlex.split(cmd), cwd=cwd, timeout=timeout)
                cached_flags.append(False)
        except FileNotFoundError as exc:
            return {
                "success": False,
                "returncode": None,
                "command": command,
                "failures": [],
                "notice": f"command not found: {exc.filename}",
            }
        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "returncode": None,
                "command": command,
                "failures": [],
                "notice": f"suite timed out after {timeout}s: {name}",
            }

        output = f"{proc.stdout}\n{proc.stderr}"
        if name == "pytest":
            failures = parse_pytest_failures(output)
        else:
            failures = parse_node_failures(output)
        all_failures.extend(failures)
        if proc.returncode != 0:
            overall_returncode = proc.returncode

    return {
        "success": overall_returncode == 0 and not all_failures,
        "returncode": overall_returncode,
        "command": command,
        "failures": all_failures,
        "cached": use_cache and all(cached_flags) if cached_flags else False,
        "notice": "",
    }


def run_all(
    suites: tuple[str, ...] = ("pytest", "node"),
    cwd: Path | None = None,
    timeout: int = 600,
    use_cache: bool = True,
    force: bool = False,
    no_cache: bool = False,
) -> dict[str, Any]:
    """Run the selected suites and aggregate failures."""
    results: dict[str, Any] = {}
    all_failures: list[dict[str, str]] = []
    notices: list[str] = []
    for name in suites:
        result = run_suite(
            name,
            cwd=cwd,
            timeout=timeout,
            use_cache=use_cache,
            force=force,
            no_cache=no_cache,
        )
        results[name] = result
        for failure in result["failures"]:
            all_failures.append({**failure, "suite": name})
        if result.get("notice"):
            notices.append(result["notice"])
    return {
        "success": all(r["success"] for r in results.values()),
        "suites": results,
        "failures": all_failures,
        "notices": notices,
    }


def rerun_failures(
    failures: list[dict[str, str]],
    cwd: Path | None = None,
    timeout: int = 600,
) -> list[dict[str, str]]:
    """Re-run individually failing tests once to verify flakiness.

    Returns the failures that still fail on re-run (stable failures). Tests
    that pass on re-run are dropped with a note appended to their record.
    """
    stable: list[dict[str, str]] = []
    for failure in failures:
        test_name = failure.get("test_name", "")
        suite = failure.get("suite", "pytest")
        if suite == "pytest" and test_name:
            cmd = canonicalize_quiet_test_command(f"pytest {test_name}")
        else:
            # Node tests are re-run per file — keep as stable without a rerun.
            stable.append(failure)
            continue
        try:
            proc = _run_cmd(cmd.split(), cwd=cwd or REPO_ROOT, timeout=timeout)
        except FileNotFoundError:
            stable.append(failure)
            continue
        if proc.returncode == 0:
            failure["flaky"] = True
            failure["note"] = "passed on re-run; treated as flaky"
            continue
        stable.append(failure)
    return stable


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the full project test suite in quiet mode and report failures."
    )
    parser.add_argument(
        "--suite",
        choices=("pytest", "node", "all"),
        default="all",
        help="Which suite(s) to run (default: all).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON output.",
    )
    parser.add_argument(
        "--parent-work-item-id",
        default=None,
        help="Parent work item id passed through to triage check_or_create.py.",
    )
    parser.add_argument(
        "--rerun-failures",
        action="store_true",
        help="Re-run failing tests once to verify flakiness before triage.",
    )
    parser.add_argument("--timeout", type=int, default=600, help="Per-suite timeout in seconds.")
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Bypass the run cache entirely (execute fresh, do not store).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force a fresh run, refreshing the cache entry.",
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="Print summary lines (e.g. 'Test Files', 'Tests', 'failed') from a cached run without executing.",
    )
    parser.add_argument(
        "--summary-grep",
        default=None,
        help="Custom grep pattern for --summary line filtering.",
    )
    return parser


def run_summary(
    suites: tuple[str, ...],
    cwd: Path | None = None,
    pattern: str | None = None,
) -> dict[str, Any]:
    """Return summary lines for each suite from the cache, executing nothing.

    Suites with no cached entry report a clear miss. The result carries
    ``success`` (True only when every selected suite had a cached entry),
    ``lines`` per suite, and ``missing`` (list of suite names).
    """
    cwd = cwd or REPO_ROOT
    result: dict[str, Any] = {"lines": {}, "missing": [], "success": True}
    for name in suites:
        if name == "pytest":
            commands = [pytest_command()]
        else:
            commands = node_suite_commands()
        lines: list[str] = []
        for cmd in commands:
            entry = query_cached(cmd, cwd=str(cwd), ttl=CACHE_TTL_SECONDS)
            if entry is None:
                result["missing"].append(name)
                result["success"] = False
                continue
            lines.extend(summary_lines(entry["stdout"], entry["stderr"], pattern=pattern))
        result["lines"][name] = lines
    return result


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    suites = ("pytest", "node") if args.suite == "all" else (args.suite,)

    if args.summary:
        summary = run_summary(suites, pattern=args.summary_grep)
        if args.json:
            print(json.dumps(summary, indent=2))
        else:
            for name, lines in summary["lines"].items():
                if name in summary["missing"]:
                    print(f"{name}: no cached result — run the suite first or use --force")
                else:
                    print(f"{name} summary:")
                    for line in lines:
                        print(f"  {line}")
        return 0 if summary["success"] else 1

    result = run_all(
        suites=suites,
        timeout=args.timeout,
        use_cache=not args.no_cache,
        force=args.force,
        no_cache=args.no_cache,
    )

    if args.rerun_failures and result["failures"]:
        result["failures"] = rerun_failures(result["failures"], timeout=args.timeout)
        result["success"] = all(r["success"] for r in result["suites"].values()) and not result["failures"]

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        for name, suite_result in result["suites"].items():
            status = "PASS" if suite_result["success"] else "FAIL"
            cached = " [cached]" if suite_result.get("cached") else ""
            print(f"{name}: {status}{cached} ({suite_result['command']})")
            if suite_result.get("notice"):
                print(f"  notice: {suite_result['notice']}")
            for failure in suite_result["failures"]:
                print(f"  FAILED: {failure['test_name']}")
        for notice in result["notices"]:
            print(f"notice: {notice}")

    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
