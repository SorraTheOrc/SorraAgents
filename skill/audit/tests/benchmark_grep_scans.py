#!/usr/bin/env python3
"""Benchmark legacy vs replacement grep-scan recipes on a generated fixture.

Work item: SA-0MSBR06GX0051T1Q (parent SA-0MSAEJCP7002LTIM — "Optimize audit
grep scans: only perform required and efficient scans").

Audit agents (launched by audit_runner.py with ``--tools read,bash,grep,find,ls``)
frequently run slow, recursive ``grep -r`` scans over ``.worklog/`` directories
and project roots. Observed on the rgardler workstation (2025-02):

- ``grep -l worklog-ref gate .worklog/audit_debug_WL-*.jsonl ...`` — 17:54 at ~40% CPU
- ``grep -rl CG-0MS9AGG3N003ASCR .worklog/`` — 20:27 at ~36% CPU
- ``grep -rln WL-0MS4FHW290053SH4 --include=*.jsonl --include=*.db .`` — 7:55 at ~45% CPU

This harness reproduces those scan shapes on a **generated fixture** (default
1 GB of fake ``audit_debug_*.jsonl`` + a small source tree with
node_modules/.git traps) so the comparison is reproducible offline without the
real 9.5 GB worklog. It runs the legacy recipes (unbounded ``grep -r`` /
``find | xargs grep``) against bounded replacement recipes (``rg`` with
--hidden/glob/max-filesize pruning, or bounded ``grep --include``) and reports
wall-clock + CPU seconds as JSON.

Run (from repo root):

    python3 skill/audit/tests/benchmark_grep_scans.py
    python3 skill/audit/tests/benchmark_grep_scans.py --json   # same output, JSON-only
    AUDIT_BENCH_FIXTURE_BYTES=100000000 python3 skill/audit/tests/benchmark_grep_scans.py

No network access is required. The fixture is generated in a temp dir and
removed on exit.
"""  # noqa: EXE001
from __future__ import annotations

import argparse
import json
import os
import resource
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# The needle that scans look for. Modelled on a real work-item id appearing in
# audit_debug jsonl bodies and a source file, so both legacy and replacement
# recipes must actually search file contents (not just names).
NEEDLE = "WL-0MS4FHW290053SH4"

DEFAULT_FIXTURE_BYTES = int(os.environ.get("AUDIT_BENCH_FIXTURE_BYTES", "1073741824"))

# Number of fake audit_debug jsonl "files" (each entry mirrors a debug-log line).
FAKE_DEBUG_FILES = 12

# Number of source files in the fake repo tree (with and without the needle).
FAKE_SOURCE_FILES = 40


def _human(n: float) -> str:
    if n >= 1:
        return f"{n:.2f}s"
    return f"{n * 1000:.0f}ms"


def generate_fixture(root: Path, target_bytes: int) -> dict:
    """Generate a fake repo tree with a .worklog/ dir of jsonl debug files.

    Returns metadata dict describing what was generated.
    """
    worklog = root / ".worklog"
    worklog.mkdir(parents=True)
    src = root / "src"
    src.mkdir(parents=True)

    # Baseline single jsonl "line" body (mirrors an audit debug-log entry).
    body = (
        '{"issue_id": "WL-0MS4FHW290053SH4", "context": "parent", '
        '"raw_stdout": "{\\"type\\":\\"session\\",\\"id\\":\\"019f\\"}", '
        '"elapsed_seconds": 123.45, "prompt": "verify AC 0 against code"}\n'
    )

    # Grow the body with padding so grep/rg must read a large volume of
    # content. The needle stays out of the padding: only the single needle
    # line above (written once per file, see below) contains it, so scans
    # must read every file to completion rather than stopping at an early
    # match — mirroring the real 9.5 GB worklog where needles are rare.
    filler = 'x' * 1024
    padded = '{"padding": "' + filler + '"}\n' * 20
    while len(padded) < 64 * 1024:
        padded += '{"padding": "' + filler + '"}\n'

    per_file = target_bytes // FAKE_DEBUG_FILES
    for i in range(FAKE_DEBUG_FILES):
        p = worklog / f"audit_debug_WL-0MS4FHW290053SH4_{i:02d}.jsonl"
        written = 0
        with p.open("w") as fh:
            # Only the FIRST file contains the needle, deep inside (not the
            # first line) so the scan must read it fully to find it.
            if i == 0:
                fh.write('{"padding": "' + filler + '"}\n' * 8)
                fh.write(body)
                written = len(body) + 8 * len('{"padding": "' + filler + '"}\n')
            while written < per_file:
                fh.write(padded)
                written += len(padded)

    # A few files that do NOT contain the needle (grep -r must skip them).
    for i in range(3):
        (worklog / f"audit_debug_OTHER-0MS{i:04d}.jsonl").write_text(
            '{"issue_id": "OTHER", "raw_stdout": "nothing here"}\n' * 100
        )

    # Source tree: some files contain the needle, most do not.
    for i in range(FAKE_SOURCE_FILES):
        p = src / f"module_{i:02d}.py"
        if i % 5 == 0:
            p.write_text(f"# contains {NEEDLE}\nimport os\n")
        else:
            p.write_text(f"import os\nimport sys  # module {i}\n")

    # node_modules / .git traps that unbounded scans walk. These are
    # many-small-file trees: walking them is the dominant cost of an
    # unbounded repo-root scan, and pruning them is what the replacement
    # recipes gain from.
    (root / "node_modules").mkdir()
    for i in range(1200):
        (root / "node_modules" / f"pkg_{i:04d}.js").write_text(
            "var x = " + str(i) + ";\n" * 20
        )
    (root / ".git").mkdir()
    (root / ".git" / "objects").mkdir()
    (root / ".git" / "objects" / "pack").mkdir()
    for i in range(800):
        (root / ".git" / "objects" / "pack" / f"pack-{i:04d}.pack").write_bytes(
            b"\x00" * (i % 32 + 1) * 1024
        )

    return {
        "worklog_files": FAKE_DEBUG_FILES,
        "source_files": FAKE_SOURCE_FILES,
        "target_bytes": target_bytes,
        "generated_bytes": sum(f.stat().st_size for f in worklog.iterdir()),
    }


def _run_and_measure(cmd: list[str], cwd: Path) -> dict:
    """Run *cmd* once and return wall-clock + CPU seconds."""
    cpu_before = resource.getrusage(resource.RUSAGE_CHILDREN)
    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=600, check=False,
        )
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        rc = "timeout"
    wall = time.monotonic() - t0
    cpu_after = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu = (cpu_after.ru_utime + cpu_after.ru_stime) - (cpu_before.ru_utime + cpu_before.ru_stime)
    return {"returncode": rc, "wall_seconds": wall, "cpu_seconds": cpu}


def _matching_files(cmd: list[str], cwd: Path) -> set[str]:
    """Run a scan and return the set of matching files (for equivalence checks)."""
    proc = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, timeout=600,
        check=False,
    )
    if proc.returncode != 0:
        return set()
    return {ln.strip() for ln in proc.stdout.splitlines() if ln.strip()}


RECIPES = {
    "legacy:grep-r-worklog": {
        "cmd": ["grep", "-r", NEEDLE, ".worklog/"],
        "desc": "Unbounded recursive grep over .worklog/ (observed 17-20 min at ~40% CPU on 9.5 GB)",
        "replacement_for": None,
    },
    "legacy:grep-rln-repo-root": {
        "cmd": ["grep", "-rln", NEEDLE, "--include=*.jsonl", "--include=*.db", "."],
        "desc": "Recursive grep from repo root with include filters (observed 7:55 at ~45% CPU)",
        "replacement_for": None,
    },
    "legacy:find-xargs-grep": {
        "cmd": ["bash", "-c", f'find . -type f -name "*.py" | xargs grep -l {NEEDLE} 2>/dev/null'],
        "desc": "find -type f | xargs grep -l over source tree",
        "replacement_for": None,
    },
    "replacement:rg-worklog": {
        "cmd": ["rg", "--hidden", "-l", "-g", "*.jsonl", "-g", "!.git/**", "--max-filesize", "256M", NEEDLE, ".worklog/"],
        "desc": "rg --hidden limited to *.jsonl in .worklog/, pruning .git and >256M files",
        "replacement_for": "legacy:grep-r-worklog",
    },
    "replacement:rg-bounded-glob": {
        "cmd": ["rg", "--hidden", "-l", "-g", "*.jsonl", "-g", "*.db", "-g", "!node_modules/**", "-g", "!.git/**", "--max-filesize", "256M", NEEDLE, "."],
        "desc": "rg --hidden from repo root with file-type/glob pruning (node_modules, .git, size cap)",
        "replacement_for": "legacy:grep-rln-repo-root",
    },
    "replacement:rg-type-py": {
        "cmd": ["rg", "-l", "--type", "py", "-g", "!.git/**", "-g", "!node_modules/**", NEEDLE, "."],
        "desc": "rg --type py over source tree (replaces find|xargs grep)",
        "replacement_for": "legacy:find-xargs-grep",
    },
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="JSON-only output")
    parser.add_argument("--iterations", type=int, default=3, help="Runs per recipe (default 3)")
    parser.add_argument(
        "--keep-fixture", action="store_true",
        help="Keep the generated fixture dir and print its path (debugging)",
    )
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="audit-grep-bench-") as tmp:
        root = Path(tmp)
        fixture = generate_fixture(root, DEFAULT_FIXTURE_BYTES)

        results: list[dict] = []
        for name, recipe in RECIPES.items():
            cmd = recipe["cmd"]
            # Equivalence check: replacement must find the same needle files.
            matches = _matching_files(cmd, root)
            runs = []
            for _ in range(max(1, args.iterations)):
                runs.append(_run_and_measure(cmd, root))
            best = min(runs, key=lambda r: r["wall_seconds"])
            results.append({
                "recipe": name,
                "desc": recipe["desc"],
                "replaces": recipe["replacement_for"],
                "matching_files": len(matches),
                "best": {
                    "wall_seconds": round(best["wall_seconds"], 4),
                    "cpu_seconds": round(best["cpu_seconds"], 4),
                },
                "all_runs": [
                    {"wall_seconds": round(r["wall_seconds"], 4), "cpu_seconds": round(r["cpu_seconds"], 4)}
                    for r in runs
                ],
            })

        # Speedup summary: replacement vs the legacy recipe it replaces.
        by_name = {r["recipe"]: r["best"]["wall_seconds"] for r in results}
        speedups = []
        for r in results:
            if r["replaces"] and r["replaces"] in by_name:
                legacy = by_name[r["replaces"]]
                if legacy > 0:
                    speedups.append({
                        "replacement": r["recipe"],
                        "legacy": r["replaces"],
                        "speedup_x": round(legacy / r["best"]["wall_seconds"], 2),
                    })

        report = {
            "work_item": "SA-0MSBR06GX0051T1Q",
            "needle": NEEDLE,
            "fixture": {
                "generated_bytes": fixture["generated_bytes"],
                "target_bytes": DEFAULT_FIXTURE_BYTES,
                "worklog_files": fixture["worklog_files"],
            },
            "recipes": results,
            "speedups": speedups,
        }

        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print(f"Fixture: {fixture['generated_bytes']} bytes in .worklog/ + source tree")
            print(f"Needle:  {NEEDLE}")
            print(f"{'recipe':<34} {'wall':>10} {'cpu':>10} {'files':>6}")
            print("-" * 70)
            for r in results:
                b = r["best"]
                print(f"{r['recipe']:<34} {_human(b['wall_seconds']):>10} {_human(b['cpu_seconds']):>10} {r['matching_files']:>6}")
            print()
            if speedups:
                print("Speedups (best wall-clock):")
                for s in speedups:
                    print(f"  {s['replacement']} vs {s['legacy']}: {s['speedup_x']}x")
            else:
                print("Speedups: (no paired legacy/replacement recipes ran)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
