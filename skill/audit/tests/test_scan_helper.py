#!/usr/bin/env python3
"""Tests for the audit scan.py bounded-scan helper (search-code / list-files).

Work item: SA-0MSBR0E8Y0022Z4V (parent SA-0MSAEJCP7002LTIM — "Optimize audit
grep scans: only perform required and efficient scans").

These tests encode the replacement recipes documented in
``docs/dev/audit-grep-scan-patterns.md`` (SA-0MSBR06GX0051T1Q). They are
written first (TDD): ``scan.py`` is implemented by SA-0MSBR0LLT006JCXN.

The former ``find-workitem`` subcommand was RETIRED by SA-0MSOMINF8007K9S4
— worklog lookups now use the native wl CLI (`wl list <term> --json` for
substring matching, `wl show <id> --json` for exact/unique-ID resolution).
This file therefore asserts the subcommand's removal and tests only the two
bounded-scan subcommands that remain.

Covered:

  - ``find-workitem`` retirement: the subcommand is gone from the CLI
    (non-zero exit with a parser error; absent from --help).
  - ``search-code <TERM>``: invokes ``rg`` with prunes (``!node_modules``,
    ``!.git``, ``!.worklog``, ``!**/audit_debug_*.jsonl``), a size cap, and an
    explicit path; returns matches; exits non-zero when nothing matches.
  - ``list-files``: bounded, maxdepth-limited listing with the same prunes; no
    descent into node_modules/.git/.worklog.

All tests run offline (subprocesses mocked or pointed at tmp fixtures).
"""  # noqa: EXE001
from __future__ import annotations

import contextlib
import io
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
        check=False,
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
# find-workitem (retired by SA-0MSOMINF8007K9S4)
# ===========================================================================


class TestFindWorkitemRetired:
    """AC3: scan.py find-workitem is retired; worklog lookups use the native
    wl CLI (`wl list <term> --json` substring, `wl show <id> --json` exact).
    """

    def test_find_workitem_subcommand_removed(self) -> None:
        """The find-workitem subcommand no longer parses (parser error)."""
        root = _make_fixture()
        proc = _run_scan("find-workitem", "SA-X", cwd=root)
        assert proc.returncode != 0
        assert "invalid choice" in proc.stderr

    def test_help_lists_only_bounded_subscommands(self) -> None:
        """--help advertises search-code/list-files, never find-workitem."""
        root = _make_fixture()
        proc = _run_scan("--help", cwd=root)
        assert proc.returncode == 0
        assert "search-code" in proc.stdout
        assert "list-files" in proc.stdout
        assert "find-workitem" not in proc.stdout


# ===========================================================================
# search-code
# ===========================================================================


class TestSearchCode:
    def test_search_code_runs_bounded_rg(self) -> None:
        """search-code invokes rg with prune globs and an explicit path."""
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
        called_cmds = [c.args[0] for c in mock_run.call_args_list]
        joined_all = " ".join(" ".join(str(a) for a in c) for c in called_cmds)
        assert "rg" in joined_all
        joined = joined_all
        # Prunes present: node_modules, .git, .worklog, audit_debug jsonl.
        assert "!node_modules" in joined
        assert "!.git" in joined
        assert "!.worklog" in joined
        assert "audit_debug" in joined
        # Explicit path passed.
        assert "src" in joined

    def test_search_code_uses_size_cap(self) -> None:
        """search-code caps file size so huge jsonl files are skipped."""
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
            " ".join(str(a) for a in c.args[0]) for c in mock_run.call_args_list
        )
        assert "--max-filesize" in joined

    def test_search_code_no_match_exits_nonzero(self) -> None:
        """No matches -> non-zero exit (mirrors rg exit code 1)."""
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
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                ["rg"], 0, stdout="src/mod.py\n",
            )
            import importlib.util
            spec = importlib.util.spec_from_file_location("scan", SCAN_SCRIPT)
            scan = importlib.util.module_from_spec(spec)
            assert spec.loader is not None
            spec.loader.exec_module(scan)
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                rc = scan.main(["search-code", "FIXME_TODO"])
        assert rc == 0
        assert "src/mod.py" in stdout.getvalue()

    def test_search_code_grep_fallback_is_bounded(self) -> None:
        """grep fallback (no rg on PATH) stays bounded: -r, --exclude-dir prunes,
        and NO rg-only flags like --max-filesize (GNU grep rejects them)."""
        with mock.patch("shutil.which", side_effect=lambda name: {"rg": None, "grep": "/usr/bin/grep"}.get(name)), \
             mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                ["grep"], 1, stdout="",
            )
            import importlib.util
            spec = importlib.util.spec_from_file_location("scan", SCAN_SCRIPT)
            scan = importlib.util.module_from_spec(spec)
            assert spec.loader is not None
            spec.loader.exec_module(scan)
            rc = scan.main(["search-code", "FIXME_TODO", "--path", "src"])
        assert rc == 1
        called_cmds = [c.args[0] for c in mock_run.call_args_list]
        joined = " ".join(" ".join(str(a) for a in c) for c in called_cmds)
        assert "--max-filesize" not in joined
        assert "--exclude-dir" in joined
        assert "--exclude" in joined
        assert "node_modules" in joined


# ===========================================================================
# list-files
# ===========================================================================


class TestListFiles:
    def test_list_files_bounded_with_maxdepth(self) -> None:
        """list-files is depth-limited and prunes node_modules/.git/.worklog."""
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
            " ".join(str(a) for a in c.args[0]) for c in mock_run.call_args_list
        )
        assert "!node_modules" in joined
        assert "!.git" in joined
        assert "!.worklog" in joined

    def test_list_files_applies_maxdepth(self) -> None:
        """list-files passes a --max-depth limit (default 2) to rg."""
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                ["rg"], 0, stdout="src/mod.py\n",
            )
            import importlib.util
            spec = importlib.util.spec_from_file_location("scan", SCAN_SCRIPT)
            scan = importlib.util.module_from_spec(spec)
            assert spec.loader is not None
            spec.loader.exec_module(scan)
            scan.main(["list-files", "--path", "src"])
            joined_default = " ".join(
                " ".join(str(a) for a in c.args[0]) for c in mock_run.call_args_list
            )
            assert "--max-depth" in joined_default
            assert "2" in joined_default

            mock_run.reset_mock()
            mock_run.return_value = subprocess.CompletedProcess(
                ["rg"], 0, stdout="src/mod.py\n",
            )
            scan.main(["list-files", "--path", "src", "--maxdepth", "4"])
            joined_custom = " ".join(
                " ".join(str(a) for a in c.args[0]) for c in mock_run.call_args_list
            )
            assert "--max-depth" in joined_custom
            assert " 4" in joined_custom

    def test_list_files_no_descent_into_traps(self) -> None:
        """The rg command must not include node_modules/.git/.worklog as roots."""
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
            " ".join(str(a) for a in c.args[0]) for c in mock_run.call_args_list
        )
        assert "node_modules" not in joined.replace("!node_modules", "")
        assert ".git" not in joined.replace("!.git", "")


# ===========================================================================
# Offline / CLI smoke
# ===========================================================================


def test_scan_script_has_cli_help() -> None:
    """scan.py exposes a CLI with the bounded-scan subcommands; the retired
    find-workitem subcommand is absent (SA-0MSOMINF8007K9S4 AC3)."""
    root = _make_fixture()
    proc = _run_scan("--help", cwd=root)
    assert proc.returncode == 0
    for sub in ("search-code", "list-files"):
        assert sub in proc.stdout
    assert "find-workitem" not in proc.stdout
