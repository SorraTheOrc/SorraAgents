"""Tests: project name resolution in the standup report header.

Covers SA-0MTWO7HFW001N9QJ — Add project title to standup report template:
  - generate_report() includes a project_name field in its JSON output
  - format_report() header prefixes the project name:
    ## <project_name> Standup Report (<date>)
  - Resolution order: explicit arg > data dict > shared resolve_project_name()
  - Shared helper is imported (skill.shared.project_name), not inlined.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
_SKILLS_ROOT = Path(__file__).resolve().parents[3] / "skill"
if str(_SKILLS_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILLS_ROOT))

# Re-import to ensure clean state
def _reload():
    for k in list(sys.modules):
        if "generate_standup" in k or "shared.project_name" in k:
            del sys.modules[k]
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "generate_standup", str(_SCRIPTS_DIR / "generate_standup.py")
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


class TestResolveProjectNameImported:
    """Verify the shared resolve_project_name is imported, not inlined."""

    def test_resolve_project_name_is_shared_helper(self):
        mod = _reload()
        # The module must expose resolve_project_name (imported from shared)
        assert hasattr(mod, "resolve_project_name")
        # It should be the shared helper, not a local copy
        from shared.project_name import resolve_project_name as shared_fn

        assert mod.resolve_project_name is shared_fn

    def test_resolve_project_name_from_config_yaml(self, tmp_path):
        worklog_dir = tmp_path / "MyProject" / ".worklog"
        worklog_dir.mkdir(parents=True)
        config = worklog_dir / "config.yaml"
        config.write_text("projectName: MyAwesomeProject\n", encoding="utf-8")
        mod = _reload()
        result = mod.resolve_project_name(str(worklog_dir))
        assert result == "MyAwesomeProject"

    def test_fallback_to_parent_dir_when_no_config(self, tmp_path):
        worklog_dir = tmp_path / "MyProject" / ".worklog"
        worklog_dir.mkdir(parents=True)
        mod = _reload()
        result = mod.resolve_project_name(str(worklog_dir))
        assert result == "MyProject"

    def test_fallback_when_projectname_key_missing(self, tmp_path):
        worklog_dir = tmp_path / "MyProject" / ".worklog"
        worklog_dir.mkdir(parents=True)
        config = worklog_dir / "config.yaml"
        config.write_text("prefix: SA\nautoExport: true\n", encoding="utf-8")
        mod = _reload()
        result = mod.resolve_project_name(str(worklog_dir))
        assert result == "MyProject"

    def test_empty_projectname_uses_fallback(self, tmp_path):
        worklog_dir = tmp_path / "MyProject" / ".worklog"
        worklog_dir.mkdir(parents=True)
        config = worklog_dir / "config.yaml"
        config.write_text('projectName: ""\n', encoding="utf-8")
        mod = _reload()
        result = mod.resolve_project_name(str(worklog_dir))
        assert result == "MyProject"

    def test_strips_whitespace_from_name(self, tmp_path):
        worklog_dir = tmp_path / "MyProject" / ".worklog"
        worklog_dir.mkdir(parents=True)
        config = worklog_dir / "config.yaml"
        config.write_text("projectName: '  MyProject  '\n", encoding="utf-8")
        mod = _reload()
        result = mod.resolve_project_name(str(worklog_dir))
        assert result == "MyProject"


class TestReportHeaderIncludesProjectName:
    """format_report header includes the project name."""

    def test_explicit_project_name_override(self):
        mod = _reload()
        report = mod.format_report({}, project_name="OverrideProject")
        first_line = report.split("\n")[0]
        assert first_line.startswith("## OverrideProject Standup Report")

    def test_data_dict_project_name(self):
        mod = _reload()
        report = mod.format_report({"project_name": "DataProject"})
        first_line = report.split("\n")[0]
        assert first_line.startswith("## DataProject Standup Report")

    def test_fallback_to_resolve_via_WORKLOG_DIR(self, tmp_path):
        worklog_dir = tmp_path / "FallbackProject" / ".worklog"
        worklog_dir.mkdir(parents=True)
        config = worklog_dir / "config.yaml"
        config.write_text("projectName: FallbackProject\n", encoding="utf-8")
        mod = _reload()
        mod.WORKLOG_DIR = str(worklog_dir)
        report = mod.format_report({})
        first_line = report.split("\n")[0]
        assert first_line.startswith("## FallbackProject Standup Report")

    def test_explicit_arg_takes_precedence_over_data(self):
        mod = _reload()
        report = mod.format_report(
            {"project_name": "DataProject"}, project_name="ExplicitProject"
        )
        first_line = report.split("\n")[0]
        assert first_line.startswith("## ExplicitProject Standup Report")

    def test_header_date_format_unchanged(self):
        mod = _reload()
        report = mod.format_report({"project_name": "Test"}, project_name="Test")
        first_line = report.split("\n")[0]
        # Date should be in spoken form, not ISO
        assert "Standup Report" in first_line
        # Verify no ISO date pattern
        import re

        assert not re.search(r"\d{4}-\d{2}-\d{2}", first_line)


class TestGenerateReportIncludesProjectName:
    """generate_report() JSON carries the project_name field."""

    def test_json_contains_project_name(self, tmp_path):
        worklog_dir = tmp_path / "TestProject" / ".worklog"
        worklog_dir.mkdir(parents=True)
        config = worklog_dir / "config.yaml"
        config.write_text("projectName: TestProject\n", encoding="utf-8")
        mod = _reload()
        mod.WORKLOG_DIR = str(worklog_dir)
        result = mod.generate_report(
            verbose=False,
            browse_count=10,
            window_start=None,
            window_end=None,
        )
        assert "project_name" in result
        assert result["project_name"] == "TestProject"

    def test_end_to_end_header_uses_config_name(self, tmp_path):
        worklog_dir = tmp_path / "E2EProject" / ".worklog"
        worklog_dir.mkdir(parents=True)
        config = worklog_dir / "config.yaml"
        config.write_text("projectName: E2EProject\n", encoding="utf-8")
        mod = _reload()
        mod.WORKLOG_DIR = str(worklog_dir)
        data = mod.generate_report(
            verbose=False,
            browse_count=10,
            window_start=None,
            window_end=None,
        )
        report = mod.format_report(data)
        first_line = report.split("\n")[0]
        assert first_line.startswith("## E2EProject Standup Report")
