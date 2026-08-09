#!/usr/bin/env python3
"""Tests: find_related.py wl calls carry the pinned --worklog-dir.

Covers SA-0MSG57UNY009DE51 / SA-0MSGH56Y4007UCE2:
  - ``run_wl_show`` / ``run_wl_update`` inject the resolved ``--worklog-dir``
    (prefix-to-sibling scan) into every wl subprocess call.
  - ``wl search`` and the ``--semantic`` probe target the item's worklog
    store (dir pinned from the work-item id), not the cwd store.
  - No behavior change when run from a project root (no flag injected when
    the resolved dir equals the cwd store).
"""  # noqa: EXE001
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import importlib.util

_SCRIPT_PATH = REPO_ROOT / "skill" / "find-related" / "scripts" / "find_related.py"
_spec = importlib.util.spec_from_file_location("find_related", _SCRIPT_PATH)
mod = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(mod)


def _make_sibling_projects(tmp_path: Path, prefix: str = "OSL") -> tuple[Path, mock.patch]:
    """Create a tmp projects dir with a sibling target project.

    Layout::

        <tmp>/projects/
            SorraAgents/.worklog/config.yaml      (prefix: SA)
            open_source_llm/.worklog/config.yaml  (prefix: OSL)

    Returns ``(target_worklog_dir, patcher)`` patching the shared
    ``SIBLING_SCAN_ROOT`` constant (the seam lives in
    ``skill.shared.status_lifecycle``).
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
    patcher = mock.patch(
        "skill.shared.status_lifecycle.SIBLING_SCAN_ROOT", projects
    )
    return target, patcher


def _recording_check_output(recorded: list[list[str]], payload: str = "{}"):
    """A fake ``subprocess.check_output`` recording commands."""
    def fake_check_output(cmd, **kwargs):
        recorded.append(list(cmd))
        return payload
    return fake_check_output


# ===========================================================================
# run_wl_show (AC1)
# ===========================================================================


class TestRunWlShowWorklogDir:
    def test_show_carries_resolved_worklog_dir(self, tmp_path):
        """AC1: `wl show <id>` carries --worklog-dir resolved via the
        prefix-to-sibling scan when cwd is not the target project.
        """
        target, patcher = _make_sibling_projects(tmp_path, prefix="OSL")
        recorded: list[list[str]] = []

        with (
            patcher,
            mock.patch.object(
                mod.subprocess, "check_output",
                _recording_check_output(recorded, payload=json.dumps(
                    {"workItem": {"id": "OSL-1", "title": "T"}})),
            ),
        ):
            item = mod.run_wl_show("OSL-0MSABC7SB001NVUN")

        assert item is not None and item["id"] == "OSL-1"
        cmd = recorded[0]
        assert cmd[0] == "wl"
        assert cmd[1] == "--worklog-dir"
        assert cmd[2] == str(target)
        assert "show" in cmd and "OSL-0MSABC7SB001NVUN" in cmd


# ===========================================================================
# run_wl_update (AC1)
# ===========================================================================


class TestRunWlUpdateWorklogDir:
    def test_update_carries_resolved_worklog_dir(self, tmp_path):
        """AC1: `wl update <id>` carries --worklog-dir from the sibling scan."""
        target, patcher = _make_sibling_projects(tmp_path, prefix="OSL")
        recorded: list[list[str]] = []

        with (
            patcher,
            mock.patch.object(
                mod.subprocess, "check_output",
                _recording_check_output(recorded, payload=json.dumps({"success": True})),
            ),
        ):
            ok = mod.run_wl_update("OSL-0MSABC7SB001NVUN", "New desc")

        assert ok is True
        cmd = recorded[0]
        assert cmd[0] == "wl"
        assert cmd[1] == "--worklog-dir"
        assert cmd[2] == str(target)
        assert "update" in cmd and "--description" in cmd


# ===========================================================================
# wl search (AC2)
# ===========================================================================


class TestRunWlSearchWorklogDir:
    def test_search_carries_pinned_worklog_dir(self, tmp_path):
        """AC2: `wl search` targets the store pinned from the work-item id."""
        target, patcher = _make_sibling_projects(tmp_path, prefix="OSL")
        recorded: list[list[str]] = []

        with (
            patcher,
            mock.patch.object(
                mod.subprocess, "check_output",
                _recording_check_output(recorded, payload=json.dumps(
                    {"success": True, "workItems": []})),
            ),
        ):
            flags = mod._wl_flags_for("OSL-0MSABC7SB001NVUN")
            results = mod.run_wl_search("keyword", use_semantic=True,
                                        worklog_flags=flags)

        assert results == []
        cmd = recorded[0]
        assert cmd[0] == "wl"
        assert cmd[1] == "--worklog-dir"
        assert cmd[2] == str(target)
        # wl search --semantic <kw> --json shape preserved after injection
        assert "search" in cmd and "--semantic" in cmd and "keyword" in cmd

    def test_search_without_flags_keeps_legacy_shape(self, tmp_path):
        """AC2: without pinned flags (legacy direct call) the command shape is
        unchanged — wl resolves from cwd.
        """
        _target, patcher = _make_sibling_projects(tmp_path, prefix="OSL")
        recorded: list[list[str]] = []

        with (
            patcher,
            mock.patch.object(
                mod.subprocess, "check_output",
                _recording_check_output(recorded, payload=json.dumps(
                    {"success": True, "workItems": []})),
            ),
        ):
            results = mod.run_wl_search("keyword", use_semantic=True)

        assert results == []
        assert recorded[0] == ["wl", "search", "--semantic", "keyword", "--json"]


# ===========================================================================
# semantic probe (AC2)
# ===========================================================================


class TestSemanticProbeWorklogDir:
    def test_probe_carries_pinned_worklog_dir(self, tmp_path):
        """AC2: the --semantic availability probe targets the item's store."""
        target, patcher = _make_sibling_projects(tmp_path, prefix="OSL")
        recorded: list[list[str]] = []

        with (
            patcher,
            mock.patch.object(
                mod.subprocess, "check_output",
                _recording_check_output(recorded, payload=json.dumps(
                    {"success": True, "workItems": []})),
            ),
        ):
            flags = mod._wl_flags_for("OSL-0MSABC7SB001NVUN")
            available = mod.is_semantic_available(worklog_flags=flags)

        assert available is True
        cmd = recorded[0]
        assert cmd[0] == "wl"
        assert cmd[1] == "--worklog-dir"
        assert cmd[2] == str(target)
        assert "search" in cmd and "--semantic" in cmd and "probe" in cmd


# ===========================================================================
# No behavior change from a project root (AC3)
# ===========================================================================


class TestNoBehaviorChangeFromProjectRoot:
    def test_no_flag_when_no_prefix_match_and_cwd_chain_empty(self, tmp_path):
        """AC3: from a project root (no sibling match, cwd chain empty) the
        wl commands run unchanged — no --worklog-dir injected.
        """
        empty_root = tmp_path / "empty-projects"
        empty_root.mkdir()
        recorded: list[list[str]] = []

        with (
            mock.patch("skill.shared.status_lifecycle.SIBLING_SCAN_ROOT",
                       empty_root),
            mock.patch("skill.shared.status_lifecycle.worklog_dir_flag",
                       return_value=[]),
            mock.patch.object(
                mod.subprocess, "check_output",
                _recording_check_output(recorded, payload=json.dumps(
                    {"workItem": {"id": "SA-1", "title": "T", "description": ""}})),
            ),
        ):
            item = mod.run_wl_show("SA-0MSABC7SB001NVUN")

        assert item is not None
        assert recorded[0] == ["wl", "show", "SA-0MSABC7SB001NVUN", "--json"]
