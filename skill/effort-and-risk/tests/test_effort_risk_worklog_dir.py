#!/usr/bin/env python3
"""Tests: orchestrate_estimate.py wl calls resolve the worklog store by prefix.

Covers SA-0MSG57UNY009DE51 / SA-0MSGH56BQ005PGCX:
  - ``_fetch_issue_stage`` (``wl show <id>``) injects flags resolved via the
    prefix-to-sibling scan; a non-SorraAgents item is fetched from its own
    store when cwd is the framework repo.
  - The ``wl update`` (effort/risk) and ``wl comment add`` call sites inject
    the same resolved flags.
  - No behavior change when run from a project root (no flags injected when
    the resolved dir equals the cwd store).
"""  # noqa: EXE001
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_SCRIPT_DIR = REPO_ROOT / "skill" / "effort-and-risk" / "scripts"
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import orchestrate_estimate as oe


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


def _recording_run(recorded: list[list[str]], returncode: int = 0,
                   stdout: str = "{}") -> mock.MagicMock:
    """A fake ``subprocess.run`` that records the command and returns success."""
    def fake_run(cmd, *args, **kwargs):
        recorded.append(list(cmd))
        return subprocess.CompletedProcess(cmd, returncode, stdout, "")
    return mock.MagicMock(side_effect=fake_run)


def _ok_show_proc(stage: str = "plan_complete"):
    return subprocess.CompletedProcess(
        ["wl", "show", "OSL-1", "--json"], 0,
        json.dumps({"workItem": {"stage": stage}}), "")


# ===========================================================================
# _fetch_issue_stage (AC1)
# ===========================================================================


class TestFetchIssueStageWorklogDir:
    def test_show_carries_resolved_worklog_dir(self, tmp_path):
        """AC1: `wl show <id>` from a non-target cwd carries --worklog-dir
        resolved via the prefix-to-sibling scan.
        """
        target, patcher = _make_sibling_projects(tmp_path, prefix="OSL")
        recorded: list[list[str]] = []

        with (
            patcher,
            mock.patch("subprocess.run",
                       _recording_run(recorded, stdout=_ok_show_proc().stdout)),
        ):
            stage = oe._fetch_issue_stage("OSL-0MSABC7SB001NVUN")

        assert stage == "plan_complete"
        assert recorded, "expected wl show to be invoked"
        cmd = recorded[0]
        assert cmd[0] == "wl"
        assert cmd[1] == "--worklog-dir"
        assert cmd[2] == str(target)
        assert "show" in cmd and "OSL-0MSABC7SB001NVUN" in cmd


# ===========================================================================
# _update_work_item (AC2)
# ===========================================================================


class TestUpdateWorkItemWorklogDir:
    def test_update_carries_resolved_worklog_dir(self, tmp_path):
        """AC2: the `wl update` (effort/risk) call injects the resolved flags."""
        target, patcher = _make_sibling_projects(tmp_path, prefix="OSL")
        recorded: list[list[str]] = []

        with (
            patcher,
            mock.patch("subprocess.run",
                       _recording_run(recorded, returncode=0)),
        ):
            result = oe._update_work_item("OSL-0MSABC7SB001NVUN", "Small", "Medium")

        assert result["success"] is True
        cmd = recorded[0]
        assert cmd[0] == "wl"
        assert cmd[1] == "--worklog-dir"
        assert cmd[2] == str(target)
        assert "update" in cmd and "--effort" in cmd and "--risk" in cmd


# ===========================================================================
# _post_comment (AC2)
# ===========================================================================


class TestPostCommentWorklogDir:
    def test_comment_carries_resolved_worklog_dir(self, tmp_path):
        """AC2: the `wl comment add` call injects the resolved flags."""
        target, patcher = _make_sibling_projects(tmp_path, prefix="OSL")
        recorded: list[list[str]] = []

        with (
            patcher,
            mock.patch("subprocess.run",
                       _recording_run(recorded, returncode=0)),
        ):
            result = oe._post_comment("OSL-0MSABC7SB001NVUN", "Body text")

        assert result["success"] is True
        cmd = recorded[0]
        assert cmd[0] == "wl"
        assert cmd[1] == "--worklog-dir"
        assert cmd[2] == str(target)
        assert "comment" in cmd and "add" in cmd


# ===========================================================================
# No behavior change from a project root (AC3)
# ===========================================================================


class TestNoBehaviorChangeFromProjectRoot:
    def test_no_flag_when_no_prefix_match_and_cwd_chain_empty(self, tmp_path):
        """AC3: from a project root (no sibling match, cwd chain empty) the
        wl commands run unchanged — same schema, no --worklog-dir injected.
        """
        empty_root = tmp_path / "empty-projects"
        empty_root.mkdir()
        recorded: list[list[str]] = []

        with (
            mock.patch("skill.shared.status_lifecycle.SIBLING_SCAN_ROOT",
                       empty_root),
            mock.patch("skill.shared.status_lifecycle.worklog_dir_flag",
                       return_value=[]),
            mock.patch("subprocess.run",
                       _recording_run(recorded, returncode=0)),
        ):
            result = oe._update_work_item("SA-0MSABC7SB001NVUN", "Small", "Medium")

        assert result["success"] is True
        assert recorded[0][:4] == ["wl", "update", "SA-0MSABC7SB001NVUN", "--effort"]
