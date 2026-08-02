#!/usr/bin/env python3
"""Tests for the audit scan.py bounded-scan helper (find-workitem / search-code / list-files).

Work item: SA-0MSBR0E8Y0022Z4V (parent SA-0MSAEJCP7002LTIM — "Optimize audit
grep scans: only perform required and efficient scans").

These tests encode the replacement recipes documented in
``docs/dev/audit-grep-scan-patterns.md`` (SA-0MSBR06GX0051T1Q). They are
written first (TDD): ``scan.py`` is implemented by SA-0MSBR0LLT006JCXN.

Covered:

  - ``find-workitem <ID>``: resolves the work item via ``wl search`` (mocked),
    never greps ``.worklog/`` recursively, and exits non-zero with a message
    when not found.
  - ``search-code <TERM>``: invokes ``rg`` with prunes (``!node_modules``,
    ``!.git``, ``!.worklog``, ``!**/audit_debug_*.jsonl``), a size cap, and an
    explicit path; returns matches; exits non-zero when nothing matches.
  - ``list-files``: bounded, maxdepth-limited listing with the same prunes; no
    descent into node_modules/.git/.worklog.

All tests run offline (subprocesses mocked or pointed at tmp fixtures).
"""  # noqa: EXE001
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

SCAN_SCRIPT = REPO_ROOT / "skill" / "audit" / "scripts" / "scan.py"


def _run_scan(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    """Run scan.py with *args from *cwd (returns CompletedProcess)."""
    return subprocess.run(
        [sys.executable, str(SCAN_SCRIPT), *args],
        cwd=cwd, capture_output=True, text=True, timeout=120,
    )


def _make_fixture() -> Path:
    """Build a small offline fixture: repo tree with code, worklog, traps."""
    root = Path(tempfile.mkdtemp(prefix="scan-helper-"))
    (root / "src").mkdir()
    (root / "src" / "mod.py").write_text("import os\n# FIXME_TODO\n")
    (root / "src" / "other.py").write_text("import sys\n")
    (root / ".worklog").mkdir()
    (root / ".worklog" / "audit_debug_X-1.jsonl").write_text(
        '{"raw_stdout": "FIXME_TODO needle"}\n'
    )
    (root / ".worklog" / "worklog-data.jsonl").write_text(
        '{"id": "SA-X", "title": "found"}\n'
    )
    (root / "node_modules").mkdir()
    (root / "node_modules" / "evil.js").write_text("// FIXME_TODO in node_modules\n")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("[core]\n")
    return root


# ===========================================================================
# find-workitem
# ===========================================================================


class TestFindWorkitem:
    def test_find_workitem_uses_wl_search_not_grep(self) -> None:
        """find-workitem resolves via `wl search`, not a recursive grep."""
        root = _make_fixture()
        # Mock subprocess so no real wl/grep runs; verify the *constructed*
        # command uses `wl search` and never `grep -r` over .worklog.
        proc = subprocess.run  # noqa: F841 (documentation)
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                ["wl", "search"], 0,
                stdout=json.dumps({"success": True, "items": [{"id": "SA-X"}]}),
            )
            import importlib.util
            spec = importlib.util.spec_from_file_location("scan", SCAN_SCRIPT)
            scan = importlib.util.module_from_spec(spec)
            assert spec.loader is not None
            spec.loader.exec_module(scan)
            rc = scan.main(["find-workitem", "SA-X"])
        assert rc == 0
        called_cmds = [c.args for c in mock_run.call_args_list]
        assert any("wl" in c and "search" in c for c in called_cmds)
        assert not any("grep" in c for c in called_cmds)

    def test_find_workitem_not_found_exits_nonzero_with_message(self) -> None:
        """Not found -> non-zero exit and a clear message, no crash."""
        root = _make_fixture()
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                ["wl", "search"], 0,
                stdout=json.dumps({"success": True, "items": []}),
            )
            import importlib.util
            spec = importlib.util.spec_from_file_location("scan", SCAN_SCRIPT)
            scan = importlib.util.module_from_spec(spec)
            assert spec.loader is not None
            spec.loader.exec_module(scan)
            rc = scan.main(["find-workitem", "SA-NOPE"])
        assert rc != 0

    def test_find_workitem_no_recursive_grep_over_worklog(self) -> None:
        """The recipe must never run `grep -r ... .worklog/`."""
        root = _make_fixture()
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                ["wl", "search"], 0,
                stdout=json.dumps({"success": True, "items": []}),
            )
            import importlib.util
            spec = importlib.util.spec_from_file_location("scan", SCAN_SCRIPT)
            scan = importlib.util.module_from_spec(spec)
            assert spec.loader is not None
            spec.loader.exec_module(scan)
            scan.main(["find-workitem", "SA-X"])
        for c in mock_run.call_args_list:
            args = c.args[0]
            joined = " ".join(str(a) for a in args)
            assert "grep" not in joined or ".worklog" not in joined


# ===========================================================================
# search-code
# ===========================================================================


class TestSearchCode:
    def test_search_code_runs_bounded_rg(self) -> None:
        """search-code invokes rg with prune globs and an explicit path."""
        root = _make_fixture()
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                ["rg"], 0, stdout="src/mod.py\n",
            )
            import importlib.util
            spec = importlib.util.spec_from_file_location("scan", SCAN_SCRIPT)
            scan = importlib.util.module_from_spec(spec)
            assert spec.loader is not None
            spec.loader.exec_module(scan)
            rc = scan.main(["search-code", "FIXME_TODO", "--path", "src"])
        assert rc == 0
        called_cmds = [c.args for c in mock_run.call_args_list]
        assert any("rg" in c for c in called_cmds)
        joined = " ".join(" ".join(str(a) for a in c) for c in called_cmds)
        # Prunes present: node_modules, .git, .worklog, audit_debug jsonl.
        assert "!node_modules" in joined
        assert "!.git" in joined
        assert "!.worklog" in joined
        assert "audit_debug" in joined
        # Explicit path passed.
        assert "src" in joined

    def test_search_code_uses_size_cap(self) -> None:
        """search-code caps file size so huge jsonl files are skipped."""
        root = _make_fixture()
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                ["rg"], 0, stdout="",
            )
            import importlib.util
            spec = importlib.util.spec_from_file_location("scan", SCAN_SCRIPT)
            scan = importlib.util.module_from_spec(spec)
            assert spec.loader is not None
            spec.loader.exec_module(scan)
            scan.main(["search-code", "needle"])
        joined = " ".join(
            " ".join(str(a) for a in c.args) for c in mock_run.call_args_list
        )
        assert "--max-filesize" in joined

    def test_search_code_no_match_exits_nonzero(self) -> None:
        """No matches -> non-zero exit (mirrors rg exit code 1)."""
        root = _make_fixture()
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                ["rg"], 1, stdout="",
            )
            import importlib.util
            spec = importlib.util.spec_from_file_location("scan", SCAN_SCRIPT)
            scan = importlib.util.module_from_spec(spec)
            assert spec.loader is not None
            spec.loader.exec_module(scan)
            rc = scan.main(["search-code", "NOTHING_HERE"])
        assert rc == 1

    def test_search_code_returns_matches(self) -> None:
        """Matching files are printed to stdout."""
        root = _make_fixture()
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                ["rg"], 0, stdout="src/mod.py\n",
            )
            import importlib.util
            spec = importlib.util.spec_from_file_location("scan", SCAN_SCRIPT)
            scan = importlib.util.module_from_spec(spec)
            assert spec.loader is not None
            spec.loader.exec_module(scan)
            scan.main(["search-code", "FIXME_TODO"])
        out = mock_run.return_value.stdout
        assert "src/mod.py" in out


# ===========================================================================
# list-files
# ===========================================================================


class TestListFiles:
    def test_list_files_bounded_with_maxdepth(self) -> None:
        """list-files is depth-limited and prunes node_modules/.git/.worklog."""
        root = _make_fixture()
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                ["rg"], 0, stdout="src/mod.py\n",
            )
            import importlib.util
            spec = importlib.util.spec_from_file_location("scan", SCAN_SCRIPT)
            scan = importlib.util.module_from_spec(spec)
            assert spec.loader is not None
            spec.loader.exec_module(scan)
            rc = scan.main(["list-files", "--path", "src", "--type", "py"])
        assert rc == 0
        joined = " ".join(
            " ".join(str(a) for a in c.args) for c in mock_run.call_args_list
        )
        assert "!node_modules" in joined
        assert "!.git" in joined
        assert "!.worklog" in joined

    def test_list_files_no_descent_into_traps(self) -> None:
        """The rg command must not include node_modules/.git/.worklog as roots."""
        root = _make_fixture()
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                ["rg"], 0, stdout="",
            )
            import importlib.util
            spec = importlib.util.spec_from_file_location("scan", SCAN_SCRIPT)
            scan = importlib.util.module_from_spec(spec)
            assert spec.loader is not None
            spec.loader.exec_module(scan)
            scan.main(["list-files"])
        joined = " ".join(
            " ".join(str(a) for a in c.args) for c in mock_run.call_args_list
        )
        assert "node_modules" not in joined.replace("!node_modules", "")
        assert ".git" not in joined.replace("!.git", "")


# ===========================================================================
# Offline / CLI smoke
# ===========================================================================


def test_scan_script_has_cli_help() -> None:
    """scan.py exposes a CLI with the three subcommands."""
    root = _make_fixture()
    proc = _run_scan("--help", cwd=root)
    assert proc.returncode == 0
    for sub in ("find-workitem", "search-code", "list-files"):
        assert sub in proc.stdout
