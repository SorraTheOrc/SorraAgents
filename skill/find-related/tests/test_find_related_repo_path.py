#!/usr/bin/env python3
"""Tests: find_related.py --repo-path auto-detection (SA-0MSIKRXUA003XRXI).

Covers:
  - ``--repo-path`` default derives from the target work item's worklog
    store (parent of ``.worklog``), not the script's own location, so a
    non-framework item's repo scan targets the analyzed project (AC1).
  - Sidecar full report is persisted under the target project's
    ``.worklog/tmp/`` (AC2).
  - Re-running replaces ALL prior automated report sections without
    duplication (AC3).
  - Framework repo files never leak into matches when the store resolves
    (AC4 — the scan path is the target project root).
  - Fallback to the framework ``REPO_ROOT`` when no store resolves
    (framework items / unknown prefixes) — existing behavior unchanged
    (AC5).
"""  # noqa: EXE001
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
_SKILLS_ROOT = REPO_ROOT / "skill"
if str(_SKILLS_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILLS_ROOT))

import importlib.util

_SCRIPT_PATH = REPO_ROOT / "skill" / "find-related" / "scripts" / "find_related.py"
_spec = importlib.util.spec_from_file_location("find_related", _SCRIPT_PATH)
mod = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(mod)

REPORT_HEADING = "## Related work (automated report)"
REPORT = f"\n{REPORT_HEADING}\n\n- **OSL-2** – related item\n"


def _make_projects(tmp_path: Path) -> Path:
    """Create a tmp sibling-projects root with SA + OSL stores.

    Layout::

        <tmp>/projects/
            SorraAgents/.worklog/config.yaml      (prefix: SA)
            open_source_llm/.worklog/config.yaml  (prefix: OSL)

    Returns the projects root (patch ``SIBLING_SCAN_ROOT`` with it).
    """
    projects = tmp_path / "projects"
    for name, prefix in (("SorraAgents", "SA"), ("open_source_llm", "OSL")):
        wl = projects / name / ".worklog"
        wl.mkdir(parents=True)
        (wl / "config.yaml").write_text(
            f"projectName: {name}\nprefix: {prefix}\n", encoding="utf-8"
        )
    return projects


class TestDefaultRepoPath:
    def test_sibling_project_item_resolves_to_its_repo_root(self, tmp_path):
        """AC1: an OSL item's default repo path is the OSL project root."""
        projects = _make_projects(tmp_path)
        with mock.patch("shared.status_lifecycle.SIBLING_SCAN_ROOT", projects):
            repo = mod._default_repo_path("OSL-0MSABC7SB001NVUN")
        assert repo == projects / "open_source_llm"

    def test_framework_item_resolves_to_framework_root(self, tmp_path):
        """AC5: a SorraAgents item's default repo path is unchanged."""
        projects = _make_projects(tmp_path)
        with mock.patch("shared.status_lifecycle.SIBLING_SCAN_ROOT", projects):
            repo = mod._default_repo_path("SA-0MSABC7SB001NVUN")
        assert repo == projects / "SorraAgents"

    def test_fallback_to_framework_when_no_store_resolves(self, tmp_path):
        """Unknown prefix + empty cwd chain → framework REPO_ROOT (pre-fix)."""
        empty = tmp_path / "empty-projects"
        empty.mkdir()
        with (
            mock.patch("shared.status_lifecycle.SIBLING_SCAN_ROOT", empty),
            mock.patch(
                "shared.status_lifecycle._detect_worklog_dir",
                return_value=None,
            ),
        ):
            repo = mod._default_repo_path("XX-0MSABC7SB001NVUN")
        assert repo == mod.REPO_ROOT


class TestRepoPathCli:
    def test_repo_path_defaults_to_none_for_late_resolution(self):
        """The CLI default is None; _main resolves it from the item id."""
        args = mod.parse_args(["--work-item-id", "OSL-1"])
        assert args.repo_path is None

    def test_explicit_repo_path_overrides_default(self):
        args = mod.parse_args(["--work-item-id", "OSL-1", "--repo-path", "/tmp/x"])
        assert args.repo_path == "/tmp/x"


class TestMainRepoPathWiring:
    """_main resolves the default repo path and uses it for scan + sidecar."""

    def _run_main(self, tmp_path, work_item_id="OSL-0MSABC7SB001NVUN",
                  repo_path=None):
        projects = _make_projects(tmp_path)
        calls = {"search_repo": [], "write_full_report": [], "update": []}

        def fake_search_repo(path, keywords):
            calls["search_repo"].append((path, keywords))
            return []

        def fake_write_full_report(wid, related, matches, repo_root=mod.REPO_ROOT):
            calls["write_full_report"].append((wid, repo_root))

        def fake_run_wl_update(wid, desc, worklog_flags=None):
            calls["update"].append((wid, desc))
            return True

        argv = ["find_related.py", "--work-item-id", work_item_id, "--json"]
        if repo_path is not None:
            argv += ["--repo-path", repo_path]

        with (
            mock.patch("shared.status_lifecycle.SIBLING_SCAN_ROOT", projects),
            mock.patch.object(mod, "StatusLifecycle"),
            mock.patch.object(
                mod, "run_wl_show",
                return_value={"id": work_item_id, "title": "T", "description": ""},
            ),
            mock.patch.object(mod, "is_semantic_available", return_value=False),
            mock.patch.object(mod, "search_and_dedup", return_value=[]),
            mock.patch.object(mod, "search_repo", side_effect=fake_search_repo),
            mock.patch.object(
                mod, "write_full_report", side_effect=fake_write_full_report
            ),
            mock.patch.object(
                mod, "run_wl_update", side_effect=fake_run_wl_update
            ),
            mock.patch.object(mod.sys, "argv", argv),
            mock.patch.object(mod.sys, "exit", side_effect=SystemExit),
            pytest.raises(SystemExit),
        ):
            mod._main()

        return projects, calls

    def test_default_repo_path_scans_target_project(self, tmp_path):
        """AC1: repo scan targets the OSL project, not SorraAgents."""
        projects, calls = self._run_main(tmp_path)
        scanned_path = calls["search_repo"][0][0]
        assert Path(scanned_path) == projects / "open_source_llm"

    def test_sidecar_uses_target_project_root(self, tmp_path):
        """AC2: write_full_report receives the target project root."""
        projects, calls = self._run_main(tmp_path)
        _wid, repo_root = calls["write_full_report"][0]
        assert Path(repo_root) == projects / "open_source_llm"

    def test_explicit_repo_path_overrides(self, tmp_path):
        _, calls = self._run_main(tmp_path, repo_path="/tmp/custom")
        assert calls["search_repo"][0][0] == "/tmp/custom"
        assert Path(calls["write_full_report"][0][1]) == Path("/tmp/custom")


class TestUpdateDescriptionReplacesAllSections:
    def test_appends_when_no_section(self):
        """AC3: no existing section → report appended once."""
        out = mod.update_description("Some intro", REPORT)
        assert out == "Some intro" + REPORT
        assert out.count(REPORT_HEADING) == 1

    def test_replaces_single_existing_section(self):
        """AC3: a single existing section is replaced in place."""
        desc = ("Intro\n\n" + REPORT_HEADING + "\n\n- **OLD** – old\n\n"
                "## Other section")
        out = mod.update_description(desc, REPORT)
        assert out.count(REPORT_HEADING) == 1
        assert "OLD" not in out
        assert "Intro" in out and "## Other section" in out
        # New report sits where the old one was (before the other section)
        assert out.index(REPORT_HEADING) < out.index("## Other section")

    def test_removes_all_duplicate_sections(self):
        """AC3: duplicate sections from prior runs are all removed."""
        desc = ("Intro\n\n" + REPORT_HEADING + "\n\n- **OLD1**\n\n"
                + REPORT_HEADING + "\n\n- **OLD2**\n\n## Tail")
        out = mod.update_description(desc, REPORT)
        assert out.count(REPORT_HEADING) == 1
        assert "OLD1" not in out and "OLD2" not in out
        assert "Intro" in out and "## Tail" in out

    def test_only_report_sections_yield_single_section(self):
        """AC3: description made only of duplicates collapses to one."""
        desc = (REPORT_HEADING + "\n\n- **A**\n\n"
                + REPORT_HEADING + "\n\n- **B**")
        out = mod.update_description(desc, REPORT)
        assert out.count(REPORT_HEADING) == 1
        assert "**A**" not in out and "**B**" not in out

    def test_preserves_manual_related_work_section(self):
        """Manual 'Related work' (without the automated marker) is kept."""
        desc = ("Intro\n\n## Related work\n\n- manual stuff\n\n"
                + REPORT_HEADING + "\n\n- **OLD**")
        out = mod.update_description(desc, REPORT)
        assert out.count(REPORT_HEADING) == 1
        assert "manual stuff" in out and "**OLD**" not in out

    def test_rerun_via_main_does_not_duplicate(self, tmp_path):
        """AC3 end-to-end: two _main runs yield exactly one report section."""
        projects = _make_projects(tmp_path)
        descriptions: list[str] = []

        def fake_update(wid, desc, worklog_flags=None):
            descriptions.append(desc)
            return True

        def fake_show(wid, worklog_flags=None):
            # wl round-trip: the store returns the latest description
            return {
                "id": wid,
                "title": "T",
                "description": descriptions[-1] if descriptions else "",
            }

        argv = ["find_related.py", "--work-item-id", "OSL-0MSABC7SB001NVUN", "--json"]
        with (
            mock.patch("shared.status_lifecycle.SIBLING_SCAN_ROOT", projects),
            mock.patch.object(mod, "StatusLifecycle"),
            mock.patch.object(mod, "run_wl_show", side_effect=fake_show),
            mock.patch.object(mod, "is_semantic_available", return_value=False),
            mock.patch.object(mod, "search_and_dedup", return_value=[]),
            mock.patch.object(mod, "search_repo", return_value=[]),
            mock.patch.object(mod, "write_full_report", return_value=None),
            mock.patch.object(mod, "run_wl_update", side_effect=fake_update),
            mock.patch.object(mod.sys, "argv", argv),
            mock.patch.object(mod.sys, "exit", side_effect=SystemExit),
        ):
            with pytest.raises(SystemExit):
                mod._main()
            with pytest.raises(SystemExit):
                mod._main()

        assert len(descriptions) == 2
        assert descriptions[-1].count(REPORT_HEADING) == 1


class TestWriteFullReportSidecar:
    def test_sidecar_written_under_repo_root_worklog_tmp(self, tmp_path):
        """AC2: full report persists to <repo-root>/.worklog/tmp/<id>.md."""
        target_root = tmp_path / "open_source_llm"
        target_root.mkdir()
        written = mod.write_full_report(
            "OSL-0MSABC7SB001NVUN", [], [],
            repo_root=target_root,
        )
        assert written is not None
        expected = target_root / ".worklog" / "tmp" / "find-related-full-OSL-0MSABC7SB001NVUN.md"
        assert written == expected
        assert expected.is_file()
        assert "No related work items or documentation matches found." in expected.read_text(encoding="utf-8")
