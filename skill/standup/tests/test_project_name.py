"""Tests for the project name resolution in the standup script."""

import sys
from pathlib import Path

import yaml

# Ensure the script module is importable from tests
SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

# Import after path is set
import generate_standup

# Track original WORKLOG_DIR for restoration
_ORIGINAL_WORKLOG_DIR = None


def setup_function():
    """Save original WORKLOG_DIR before each test."""
    global _ORIGINAL_WORKLOG_DIR
    _ORIGINAL_WORKLOG_DIR = generate_standup.WORKLOG_DIR
    generate_standup.WORKLOG_DIR = None


def teardown_function():
    """Restore WORKLOG_DIR after each test."""
    generate_standup.WORKLOG_DIR = _ORIGINAL_WORKLOG_DIR


class TestResolveProjectName:
    """Tests for resolve_project_name()."""

    def test_reads_projectname_from_config_yaml(self, tmp_path):
        """Config YAML projectName key takes priority."""
        worklog_dir = tmp_path / "MyProject" / ".worklog"
        worklog_dir.mkdir(parents=True)
        config = worklog_dir / "config.yaml"
        config.write_text("projectName: MyAwesomeProject\n", encoding="utf-8")
        result = generate_standup.resolve_project_name(str(worklog_dir))
        assert result == "MyAwesomeProject"

    def test_falls_back_to_parent_dir_name_when_no_config(self, tmp_path):
        """When config.yaml is absent, falls back to project root dir name."""
        worklog_dir = tmp_path / "MyProject" / ".worklog"
        worklog_dir.mkdir(parents=True)
        # No config.yaml present
        result = generate_standup.resolve_project_name(str(worklog_dir))
        assert result == "MyProject"

    def test_falls_back_to_parent_dir_name_when_projectname_missing(
        self, tmp_path
    ):
        """When projectName key is missing in config, falls back to project root dir name."""
        worklog_dir = tmp_path / "MyProject" / ".worklog"
        worklog_dir.mkdir(parents=True)
        config = worklog_dir / "config.yaml"
        config.write_text("prefix: SA\nautoExport: true\n", encoding="utf-8")
        result = generate_standup.resolve_project_name(str(worklog_dir))
        assert result == "MyProject"

    def test_empty_projectname_uses_fallback(self, tmp_path):
        """Empty string projectName falls back to project root dir name."""
        worklog_dir = tmp_path / "MyProject" / ".worklog"
        worklog_dir.mkdir(parents=True)
        config = worklog_dir / "config.yaml"
        config.write_text('projectName: ""\n', encoding="utf-8")
        result = generate_standup.resolve_project_name(str(worklog_dir))
        assert result == "MyProject"

    def test_no_worklog_dir_uses_cwd(self, tmp_path, monkeypatch):
        """When worklog_dir is None, falls back to cwd name."""
        generate_standup.WORKLOG_DIR = None
        monkeypatch.chdir(tmp_path)
        result = generate_standup.resolve_project_name(None)
        assert result == str(tmp_path).split("/")[-1]

    def test_global_worklog_dir_used_when_arg_none(self, tmp_path):
        """When worklog_dir arg is None but global WORKLOG_DIR is set, uses it."""
        worklog_dir = tmp_path / "EnvProject" / ".worklog"
        worklog_dir.mkdir(parents=True)
        config = worklog_dir / "config.yaml"
        config.write_text("projectName: EnvProject\n", encoding="utf-8")
        generate_standup.WORKLOG_DIR = str(worklog_dir)
        result = generate_standup.resolve_project_name(None)
        assert result == "EnvProject"

    def test_strips_whitespace_from_name(self, tmp_path):
        """Whitespace around projectName is stripped."""
        worklog_dir = tmp_path / "MyProject" / ".worklog"
        worklog_dir.mkdir(parents=True)
        config = worklog_dir / "config.yaml"
        config.write_text("projectName: '  MyProject  '\n", encoding="utf-8")
        result = generate_standup.resolve_project_name(str(worklog_dir))
        assert result == "MyProject"

    def test_real_config(self):
        """Real SorraAgents config returns 'Sorra Agents'."""
        worklog_dir = Path("/home/rgardler/projects/SorraAgents/.worklog")
        if not worklog_dir.exists():
            import pytest

            pytest.skip("SorraAgents worklog not available")
        result = generate_standup.resolve_project_name(str(worklog_dir))
        assert result == "Sorra Agents"


class TestReportHeaderIncludesProjectName:
    """Tests that the formatted report header includes the project name."""

    def test_format_report_uses_explicit_project_name(self):
        """format_report can receive an explicit project_name override."""
        report = generate_standup.format_report({}, project_name="OverrideProject")
        first_line = report.split("\n")[0]
        assert first_line.startswith("## OverrideProject Standup Report")

    def test_format_report_uses_data_project_name(self):
        """format_report uses data['project_name'] when no explicit name."""
        report = generate_standup.format_report({"project_name": "DataProject"})
        first_line = report.split("\n")[0]
        assert first_line.startswith("## DataProject Standup Report")

    def test_format_report_fallback_to_resolve(self, tmp_path):
        """format_report falls back to resolve_project_name when no project_name in data."""
        worklog_dir = tmp_path / "FallbackProject" / ".worklog"
        worklog_dir.mkdir(parents=True)
        config = worklog_dir / "config.yaml"
        config.write_text("projectName: FallbackProject\n", encoding="utf-8")
        generate_standup.WORKLOG_DIR = str(worklog_dir)
        report = generate_standup.format_report({})
        first_line = report.split("\n")[0]
        assert first_line.startswith("## FallbackProject Standup Report")

    def test_end_to_end_header_uses_config_name(self, tmp_path):
        """Full generate_report + format_report chain includes project name."""
        worklog_dir = tmp_path / "TestProject" / ".worklog"
        worklog_dir.mkdir(parents=True)
        config = worklog_dir / "config.yaml"
        config.write_text("projectName: TestProject\n", encoding="utf-8")
        generate_standup.WORKLOG_DIR = str(worklog_dir)
        result = generate_standup.generate_report(
            verbose=False,
            browse_count=10,
            window_start=None,
            window_end=None,
        )
        assert "project_name" in result
        assert result["project_name"] == "TestProject"
        report = generate_standup.format_report(result)
        first_line = report.split("\n")[0]
        assert first_line.startswith("## TestProject Standup Report")
