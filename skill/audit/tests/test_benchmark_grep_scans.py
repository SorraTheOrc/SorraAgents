#!/usr/bin/env python3
"""Tests for the audit grep-scan benchmark harness and catalogue.

Work item: SA-0MSBR06GX0051T1Q (parent SA-0MSAEJCP7002LTIM).

Covers:

  - The benchmark harness (`benchmark_grep_scans.py`) runs offline, generates
    a fixture, and emits JSON with wall-clock + CPU seconds per recipe.
  - Replacement recipes find the same needle files as the legacy recipes they
    replace (equivalence), so the speedup comparison is apples-to-apples.
  - The replacement recipes actually prune node_modules/.git (boundedness).
  - The catalogue (`docs/dev/audit-grep-scan-patterns.md`) lists >= 8
    reproducible scan patterns, each with origin and replacement recipe.
"""  # noqa: EXE001
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

sys.path.insert(0, str(Path(__file__).resolve().parent))

import benchmark_grep_scans as bench  # noqa: E402

# ===========================================================================
# Fixture generation
# ===========================================================================


def test_fixture_generation_creates_expected_tree() -> None:
    """The generated fixture has worklog jsonl, source files and traps."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        meta = bench.generate_fixture(root, 2 * 1024 * 1024)
        assert meta["worklog_files"] == bench.FAKE_DEBUG_FILES
        worklog = root / ".worklog"
        jsonl = list(worklog.glob("*.jsonl"))
        assert len(jsonl) == bench.FAKE_DEBUG_FILES + 3
        # The needle is inside the first debug file (deep, not first line).
        first = worklog / "audit_debug_WL-0MS4FHW290053SH4_00.jsonl"
        content = first.read_text()
        assert bench.NEEDLE in content
        assert not content.startswith('{"issue_id": "WL-0MS4FHW290053SH4"')
        # Traps exist so pruning recipes have something to skip.
        assert (root / "node_modules").is_dir()
        assert (root / ".git" / "objects" / "pack").is_dir()


def test_needle_is_rare_in_fixture() -> None:
    """Only the first debug file contains the needle (scans must read all)."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        bench.generate_fixture(root, 2 * 1024 * 1024)
        hits = []
        for p in (root / ".worklog").glob("*.jsonl"):
            if bench.NEEDLE in p.read_text():
                hits.append(p.name)
        assert hits == ["audit_debug_WL-0MS4FHW290053SH4_00.jsonl"]


# ===========================================================================
# Recipe behaviour (offline, no network)
# ===========================================================================


def _recipe_matches(recipe: dict, root: Path) -> set[str]:
    """Run a recipe against a fixture root and return matching file names.

    Handles both plain path output (``grep -rl`` / ``rg -l``) and
    ``path:matchline`` output (``grep -r`` without ``-l``).
    """
    proc = subprocess.run(
        recipe["cmd"], cwd=root, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, f"{recipe['cmd']} failed: {proc.stderr[:200]}"
    names: set[str] = set()
    for ln in proc.stdout.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        if ":" in ln and not ln.startswith("./") and "://" not in ln:
            # grep -r prints path:matchline — take the path prefix.
            candidate, _, _ = ln.partition(":")
        else:
            candidate = ln
        names.add(Path(candidate).name)
    return names


def test_replacement_recipes_match_legacy_recipes() -> None:
    """Replacement recipes find the same needle files as the legacy ones."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        bench.generate_fixture(root, 2 * 1024 * 1024)
        legacy_worklog = _recipe_matches(bench.RECIPES["legacy:grep-r-worklog"], root)
        repl_worklog = _recipe_matches(bench.RECIPES["replacement:rg-worklog"], root)
        assert legacy_worklog == repl_worklog
        assert len(legacy_worklog) == 1  # only the first debug file has the needle

        legacy_root = _recipe_matches(bench.RECIPES["legacy:grep-rln-repo-root"], root)
        repl_root = _recipe_matches(bench.RECIPES["replacement:rg-bounded-glob"], root)
        assert legacy_root == repl_root


def test_replacement_recipes_prune_traps() -> None:
    """Bounded recipes do not scan node_modules/.git (pruning is effective)."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        bench.generate_fixture(root, 2 * 1024 * 1024)
        # Put the needle in a node_modules file: bounded recipes must NOT find it.
        (root / "node_modules" / "evil.jsonl").write_text(f"// {bench.NEEDLE}\n")
        repl = _recipe_matches(bench.RECIPES["replacement:rg-bounded-glob"], root)
        assert "evil.jsonl" not in repl
        # The legacy recipe WOULD find it (unbounded, walks node_modules).
        legacy = _recipe_matches(bench.RECIPES["legacy:grep-rln-repo-root"], root)
        assert "evil.jsonl" in legacy


def test_legacy_worklog_recipe_finds_needle() -> None:
    """The legacy worklog recipe returns the matching debug file."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        bench.generate_fixture(root, 2 * 1024 * 1024)
        matches = _recipe_matches(bench.RECIPES["legacy:grep-r-worklog"], root)
        assert matches == {"audit_debug_WL-0MS4FHW290053SH4_00.jsonl"}


# ===========================================================================
# Harness JSON output
# ===========================================================================


def test_harness_emits_json_with_timings() -> None:
    """The harness runs offline and emits wall-clock + CPU seconds as JSON."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        bench.generate_fixture(root, 2 * 1024 * 1024)
        results: list[dict] = []
        for name, recipe in bench.RECIPES.items():
            run = bench._run_and_measure(recipe["cmd"], root)  # noqa: SLF001
            assert run["returncode"] == 0
            assert run["wall_seconds"] >= 0
            assert run["cpu_seconds"] >= 0
            results.append({"recipe": name, "best": run})
        assert len(results) == len(bench.RECIPES)
        # Speedup summary present for paired recipes.
        by_name = {r["recipe"]: r["best"]["wall_seconds"] for r in results}
        for r in results:
            if r["recipe"].startswith("replacement:"):
                legacy = r["recipe"].replace("replacement:", "legacy:")
                if legacy in by_name and by_name[legacy] > 0:
                    assert by_name[legacy] >= r["best"]["wall_seconds"] * 0.5


def test_main_json_flag_outputs_parseable_json() -> None:
    """`--json` prints a single parseable JSON document to stdout."""
    proc = subprocess.run(
        [sys.executable, str(Path(__file__).parent / "benchmark_grep_scans.py"),
         "--json", "--iterations", "1"],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, proc.stderr[:500]
    report = json.loads(proc.stdout)
    assert report["work_item"] == "SA-0MSBR06GX0051T1Q"
    assert "recipes" in report
    assert any(r["recipe"] == "legacy:grep-r-worklog" for r in report["recipes"])
    assert any(r["recipe"] == "replacement:rg-worklog" for r in report["recipes"])


# ===========================================================================
# Catalogue document
# ===========================================================================


def test_catalogue_documents_at_least_8_patterns_with_origins() -> None:
    """docs/dev/audit-grep-scan-patterns.md lists >= 8 patterns w/ origins."""
    catalogue = REPO_ROOT / "docs" / "dev" / "audit-grep-scan-patterns.md"
    assert catalogue.is_file(), f"missing catalogue: {catalogue}"
    text = catalogue.read_text()

    # >= 8 documented pattern sections
    pattern_headers = [
        line for line in text.splitlines() if line.startswith("### P")
    ]
    assert len(pattern_headers) >= 8, f"only {len(pattern_headers)} patterns"

    # Each pattern records command, directories scanned, impact, replacement.
    for header in pattern_headers:
        idx = text.index(header)
        section_end = text.find("\n### ", idx + 1)
        section = text[idx: section_end if section_end != -1 else None]
        for needle in ("**Command:**", "**Scans:**", "**Impact:**",
                       "**Replacement:**"):
            assert needle in section, f"{header} missing {needle}"

    # Origins reference audit_runner.py or SKILL.md with file:line.
    assert "audit_runner.py" in text
    assert "SKILL.md" in text
    assert "L753" in text  # tools allowlist origin
    assert "L2230" in text  # parent prompt FILE SCOPE origin


def test_catalogue_records_baseline_numbers() -> None:
    """The catalogue includes the observed production baseline numbers."""
    text = (REPO_ROOT / "docs" / "dev" / "audit-grep-scan-patterns.md").read_text()
    for token in ("17:54", "20:27", "7:55", "40% CPU", "36% CPU", "45% CPU"):
        assert token in text, f"baseline token missing: {token}"
    # Benchmark results table present.
    assert "Benchmark results" in text or "Baseline results" in text
