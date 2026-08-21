#!/usr/bin/env python3
"""Tests for the shared prefix-to-sibling worklog resolution seam.

Covers the promotion of the audit runner's prefix-to-sibling scan into
``skill/shared/status_lifecycle.py`` (SA-0MSG57UNY009DE51):

  - ``_extract_work_item_prefix`` / ``_find_worklog_dir_by_prefix`` resolve
    a sibling project's ``.worklog`` from a work-item id prefix.
  - ``resolve_worklog_dir`` resolves a store for an id (prefix scan, then
    cwd-chain fallback).
  - ``resolve_worklog_flags`` precedence: explicit ``--worklog-dir`` >
    prefix-to-sibling scan > cwd-chain (``worklog_dir_flag``) > no flag.
  - ``run_wl`` injects the resolved flags from a non-project cwd.
"""  # noqa: EXE001
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
_SKILLS_ROOT = REPO_ROOT / "skill"
if str(_SKILLS_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILLS_ROOT))

from shared import status_lifecycle


def _make_sibling_projects(tmp_path: Path, prefix: str = "OSL") -> tuple[Path, mock.patch]:
    """Create a tmp projects dir with a sibling target project.

    Layout::

        <tmp>/projects/
            SorraAgents/.worklog/config.yaml      (prefix: SA)
            open_source_llm/.worklog/config.yaml  (prefix: OSL)

    Returns ``(target_worklog_dir, patcher)`` where *target_worklog_dir* is
    the target project's ``.worklog`` directory and *patcher* is a
    ``mock.patch`` on ``status_lifecycle.SIBLING_SCAN_ROOT`` (call
    ``patcher.start()`` to apply).
    """
    projects = tmp_path / "projects"
    framework = projects / "SorraAgents" / ".worklog"
    framework.mkdir(parents=True)
    (framework / "config.yaml").write_text(
        "projectName: Sorra Agents\nprefix: SA\n", encoding="utf-8"
    )
    target = projects / "open_source_llm" / ".worklog"
    target.mkdir(parents=True)
    (target / "config.yaml").write_text(
        f"projectName: Open Source LLM\nprefix: {prefix}\n", encoding="utf-8"
    )
    patcher = mock.patch.object(status_lifecycle, "SIBLING_SCAN_ROOT", projects)
    return target, patcher


def _ok_proc() -> SimpleNamespace:
    """A canned wl success response."""
    return SimpleNamespace(
        returncode=0,
        stdout=json.dumps({"success": True}),
        stderr="",
    )


# ===========================================================================
# Prefix extraction (shared seam AC1)
# ===========================================================================


class TestExtractWorkItemPrefix:
    def test_extracts_prefix_from_show_command(self):
        """AC1: a work-item id argument yields its prefix."""
        assert status_lifecycle._extract_work_item_prefix(
            ["wl", "show", "OSL-0MSABC7SB001NVUN", "--json"]
        ) == "OSL"

    def test_extracts_prefix_from_update_command(self):
        """AC1: prefix extraction works regardless of argument position."""
        assert status_lifecycle._extract_work_item_prefix(
            ["wl", "update", "OSL-2", "--status", "in_progress", "--json"]
        ) == "OSL"

    def test_none_for_commands_without_id(self):
        """AC1: commands without a work-item id yield None."""
        assert status_lifecycle._extract_work_item_prefix(["wl", "list", "--json"]) is None
        assert status_lifecycle._extract_work_item_prefix(["wl", "search", "keyword"]) is None

    def test_none_for_malformed_id(self):
        """AC1: ids that do not look like work-item ids yield None."""
        assert status_lifecycle._extract_work_item_prefix(["wl", "show", "not-an-id"]) is None


# ===========================================================================
# Sibling scan (shared seam AC1)
# ===========================================================================


class TestFindWorklogDirByPrefix:
    def test_finds_sibling_by_prefix(self, tmp_path):
        """AC1: the sibling project with the matching config prefix is found."""
        target, patcher = _make_sibling_projects(tmp_path, prefix="OSL")
        with patcher:
            assert status_lifecycle._find_worklog_dir_by_prefix("OSL") == target

    def test_none_when_prefix_absent(self, tmp_path):
        """AC1: an unknown prefix resolves to None."""
        _target, patcher = _make_sibling_projects(tmp_path, prefix="OSL")
        with patcher:
            assert status_lifecycle._find_worklog_dir_by_prefix("ZZZ") is None

    def test_none_when_scan_root_missing(self, tmp_path):
        """AC1: a missing scan root degrades to None (never raises)."""
        missing = tmp_path / "does-not-exist"
        with mock.patch.object(status_lifecycle, "SIBLING_SCAN_ROOT", missing):
            assert status_lifecycle._find_worklog_dir_by_prefix("OSL") is None


# ===========================================================================
# Sibling scan caching (SA-0MSL1YX24000V2MG)
# ===========================================================================


class TestFindWorklogDirByPrefixCaching:
    """The sibling scan runs at most once per process per (scan-root, prefix).

    AC1: the scan is memoized; repeated resolutions for the same prefix and
    scan root must not re-run ``glob``/config reads. The cache key includes
    the scan root so tests that patch ``SIBLING_SCAN_ROOT`` stay isolated.
    """

    def test_scan_runs_once_per_prefix(self, tmp_path):
        """AC1: two resolutions with the same prefix run the scan once."""
        target, patcher = _make_sibling_projects(tmp_path, prefix="OSL")
        calls: list[tuple[Path, str]] = []
        original_glob = Path.glob

        def counting_glob(self, pattern: str):
            calls.append((self, pattern))
            return original_glob(self, pattern)

        with patcher, mock.patch.object(Path, "glob", counting_glob):
            first = status_lifecycle._find_worklog_dir_by_prefix("OSL")
            second = status_lifecycle._find_worklog_dir_by_prefix("OSL")

        assert first == target
        assert second == target
        scan_calls = [c for c in calls if c[1] == "*/.worklog/config.yaml"]
        assert len(scan_calls) == 1

    def test_different_prefixes_scan_separately(self, tmp_path):
        """AC1: distinct prefixes are cached independently."""
        _target, patcher = _make_sibling_projects(tmp_path, prefix="OSL")
        calls: list[tuple[Path, str]] = []
        original_glob = Path.glob

        def counting_glob(self, pattern: str):
            calls.append((self, pattern))
            return original_glob(self, pattern)

        with patcher, mock.patch.object(Path, "glob", counting_glob):
            assert status_lifecycle._find_worklog_dir_by_prefix("OSL") is not None
            assert status_lifecycle._find_worklog_dir_by_prefix("ZZZ") is None
            assert status_lifecycle._find_worklog_dir_by_prefix("OSL") is not None

        scan_calls = [c for c in calls if c[1] == "*/.worklog/config.yaml"]
        # OSL cached after first call; ZZZ cached after first call -> 2 scans.
        assert len(scan_calls) == 2

    def test_miss_is_cached_too(self, tmp_path):
        """AC1: a None (miss) result is cached like a hit."""
        _target, patcher = _make_sibling_projects(tmp_path, prefix="OSL")
        calls: list[tuple[Path, str]] = []
        original_glob = Path.glob

        def counting_glob(self, pattern: str):
            calls.append((self, pattern))
            return original_glob(self, pattern)

        with patcher, mock.patch.object(Path, "glob", counting_glob):
            assert status_lifecycle._find_worklog_dir_by_prefix("ZZZ") is None
            assert status_lifecycle._find_worklog_dir_by_prefix("ZZZ") is None

        scan_calls = [c for c in calls if c[1] == "*/.worklog/config.yaml"]
        assert len(scan_calls) == 1


# ===========================================================================
# Framework root derivation (worktree-safe, SA-0MSG57UNY009DE51)
# ===========================================================================


class TestFrameworkRootDerivation:
    def test_repo_root_is_main_checkout_not_worktree(self, tmp_path):
        """The framework root resolves to the MAIN checkout even when this
        module is imported from inside a worktree (whose root has a .git
        FILE and a full skill/ copy).
        """
        main = tmp_path / "framework"
        (main / "skill" / "shared").mkdir(parents=True)
        (main / "skill" / "shared" / "status_lifecycle.py").write_text("# x\n")
        (main / ".git").mkdir()  # main checkout: .git is a DIRECTORY

        wt = main / ".worklog" / "worktrees" / "wl-some-item"
        (wt / "skill" / "shared").mkdir(parents=True)
        (wt / "skill" / "shared" / "status_lifecycle.py").write_text("# x\n")
        (wt / ".git").write_text("gitdir: ../.git/worktrees/wl-some-item\n")

        # Patch the module file location so _resolve_repo_root walks from
        # the worktree copy.
        with mock.patch.object(
            status_lifecycle, "Path",
            lambda p: Path(wt / "skill" / "shared" / "status_lifecycle.py"),
        ):
            root = status_lifecycle._resolve_repo_root()

        assert root == main

    def test_scan_root_is_framework_repo_parent(self):
        """SIBLING_SCAN_ROOT derives from REPO_ROOT.parent — cwd-independent."""
        assert status_lifecycle.SIBLING_SCAN_ROOT == status_lifecycle.REPO_ROOT.parent


# ===========================================================================
# resolve_worklog_dir (shared seam AC1)
# ===========================================================================


class TestResolveWorklogDir:
    def test_prefix_scan_wins_over_cwd_chain(self, tmp_path):
        """AC1: a sibling prefix match resolves before the cwd-chain fallback."""
        target, patcher = _make_sibling_projects(tmp_path, prefix="OSL")
        with (
            patcher,
            mock.patch.object(
                status_lifecycle, "_detect_worklog_dir",
                return_value=Path("/wrong/.worklog"),
            ) as detect,
        ):
            assert status_lifecycle.resolve_worklog_dir("OSL-0MSABC7SB001NVUN") == target
        detect.assert_not_called()

    def test_falls_back_to_cwd_chain_when_no_prefix_match(self, tmp_path):
        """AC1: an unknown prefix falls back to the cwd-chain detection."""
        _target, patcher = _make_sibling_projects(tmp_path, prefix="OSL")
        with (
            patcher,
            mock.patch.object(
                status_lifecycle, "_detect_worklog_dir",
                return_value=Path("/cwd-chain/.worklog"),
            ),
        ):
            assert status_lifecycle.resolve_worklog_dir("ZZZ-123") == Path("/cwd-chain/.worklog")

    def test_none_when_nothing_resolves(self, tmp_path):
        """AC1: no prefix match and no cwd-chain result yields None."""
        _target, patcher = _make_sibling_projects(tmp_path, prefix="OSL")
        with (
            patcher,
            mock.patch.object(status_lifecycle, "_detect_worklog_dir", return_value=None),
        ):
            assert status_lifecycle.resolve_worklog_dir("ZZZ-123") is None


# ===========================================================================
# resolve_worklog_flags precedence (shared seam AC1)
# ===========================================================================


class TestResolveWorklogFlagsPrecedence:
    def test_explicit_dir_overrides_prefix_scan(self, tmp_path):
        """AC1: an explicit --worklog-dir value wins over the sibling scan."""
        _target, patcher = _make_sibling_projects(tmp_path, prefix="OSL")
        with patcher:
            flags = status_lifecycle.resolve_worklog_flags(
                ["wl", "show", "OSL-0MSABC7SB001NVUN", "--json"],
                explicit_dir="/explicit/.worklog",
            )
        assert flags == ["--worklog-dir", "/explicit/.worklog"]

    def test_prefix_scan_overrides_cwd_chain(self, tmp_path):
        """AC1: the sibling scan wins over the cwd-chain fallback."""
        target, patcher = _make_sibling_projects(tmp_path, prefix="OSL")
        with (
            patcher,
            mock.patch.object(
                status_lifecycle, "worklog_dir_flag",
                return_value=["--worklog-dir", "/cwd-chain/.worklog"],
            ) as cwd_flags,
        ):
            flags = status_lifecycle.resolve_worklog_flags(
                ["wl", "show", "OSL-0MSABC7SB001NVUN", "--json"])
        assert flags == ["--worklog-dir", str(target)]
        cwd_flags.assert_not_called()

    def test_cwd_chain_fallback_when_no_prefix_match(self, tmp_path):
        """AC1: without a sibling match, the cwd-chain fallback is used."""
        _target, patcher = _make_sibling_projects(tmp_path, prefix="OSL")
        with (
            patcher,
            mock.patch.object(
                status_lifecycle, "worklog_dir_flag",
                return_value=["--worklog-dir", "/cwd-chain/.worklog"],
            ),
        ):
            flags = status_lifecycle.resolve_worklog_flags(
                ["wl", "list", "--json"])
        assert flags == ["--worklog-dir", "/cwd-chain/.worklog"]

    def test_no_flags_when_nothing_resolves(self, tmp_path):
        """AC1: no explicit dir, no prefix match, empty cwd chain -> no flags."""
        _target, patcher = _make_sibling_projects(tmp_path, prefix="OSL")
        with (
            patcher,
            mock.patch.object(status_lifecycle, "worklog_dir_flag", return_value=[]),
        ):
            flags = status_lifecycle.resolve_worklog_flags(
                ["wl", "list", "--json"])
        assert flags == []


# ===========================================================================
# run_wl injection from a non-project cwd (shared seam AC5)
# ===========================================================================


class TestRunWlInjection:
    def test_run_wl_injects_resolved_flags_from_non_project_cwd(self, tmp_path):
        """AC5: run_wl injects the sibling-resolved --worklog-dir into the
        wl subprocess call when the caller's cwd is not the target project.
        """
        target, patcher = _make_sibling_projects(tmp_path, prefix="OSL")
        recorded: list[list[str]] = []

        def fake_runner(cmd):
            recorded.append(list(cmd))
            return _ok_proc()

        with patcher:
            status_lifecycle.run_wl(
                ["wl", "show", "OSL-0MSABC7SB001NVUN", "--json"],
                runner=fake_runner,
            )

        assert recorded, "fake runner should have received the command"
        cmd = recorded[0]
        assert cmd[0] == "wl"
        assert cmd[1] == "--worklog-dir"
        assert cmd[2] == str(target)
        assert "show" in cmd and "OSL-0MSABC7SB001NVUN" in cmd

    def test_run_wl_no_injection_from_project_root(self, tmp_path):
        """AC5: when the resolved dir equals the cwd store (no prefix match,
        cwd-chain returns no flag), the command runs unchanged — no behavior
        change when run from a project root.
        """
        # Empty scan root -> no sibling match; cwd chain -> no flag.
        empty_root = tmp_path / "empty-projects"
        empty_root.mkdir()
        recorded: list[list[str]] = []

        def fake_runner(cmd):
            recorded.append(list(cmd))
            return _ok_proc()

        with (
            mock.patch.object(status_lifecycle, "SIBLING_SCAN_ROOT", empty_root),
            mock.patch.object(status_lifecycle, "worklog_dir_flag", return_value=[]),
        ):
            status_lifecycle.run_wl(
                ["wl", "show", "SA-0MSABC7SB001NVUN", "--json"],
                runner=fake_runner,
            )

        assert recorded[0] == ["wl", "show", "SA-0MSABC7SB001NVUN", "--json"]
