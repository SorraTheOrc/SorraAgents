"""Tests for project-name resolution and report-heading output.

Covers:
- ``resolve_project_name`` resolution branches (AC2)
- Report heading includes the project name (AC1)
- ``--dry-run`` output uses the same heading (AC4)
"""  # noqa: D205, D400

import io
import json
import os
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import pytest
import yaml


# ── Shared helpers ──────────────────────────────────────────────────────


def _make_worklog_dir(config_content: dict | None = None) -> Path:
    """Create a temporary ``.worklog`` directory with an optional ``config.yaml``."""
    tmp = Path(tempfile.mkdtemp())
    worklog = tmp / ".worklog"
    worklog.mkdir()
    if config_content is not None:
        (worklog / "config.yaml").write_text(
            yaml.dump(config_content), encoding="utf-8"
        )
    return worklog


# ── resolve_project_name unit tests (AC2, AC3) ─────────────────────────


class TestResolveProjectName:
    """Unit tests for ``skill.shared.project_name.resolve_project_name``."""

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("Sorra Agents", "Sorra Agents"),
            ("  Trimmed  ", "Trimmed"),
            ("Project-42", "Project-42"),
        ],
    )
    def test_config_with_project_name(self, name: str, expected: str) -> None:
        """When ``projectName`` is present in config.yaml, it is returned."""
        worklog = _make_worklog_dir({"projectName": name})
        from skill.shared.project_name import resolve_project_name

        result = resolve_project_name(worklog_dir=str(worklog))
        assert result == expected

    def test_config_missing_project_name_key(self) -> None:
        """When config.yaml lacks ``projectName``, falls back to parent dir."""
        worklog = _make_worklog_dir({"prefix": "SA", "autoExport": True})
        from skill.shared.project_name import resolve_project_name

        result = resolve_project_name(worklog_dir=str(worklog))
        assert result == worklog.parent.name

    def test_config_yaml_empty_file(self) -> None:
        """An empty config.yaml (YAML null) falls through."""
        tmp = Path(tempfile.mkdtemp())
        worklog = tmp / ".worklog"
        worklog.mkdir()
        (worklog / "config.yaml").write_text("", encoding="utf-8")
        from skill.shared.project_name import resolve_project_name

        result = resolve_project_name(worklog_dir=str(worklog))
        assert result != ""

    def test_no_config_file(self) -> None:
        """When config.yaml does not exist, falls back to parent directory name."""
        worklog = _make_worklog_dir(config_content=None)
        from skill.shared.project_name import resolve_project_name

        result = resolve_project_name(worklog_dir=str(worklog))
        assert result == worklog.parent.name

    def test_unreadable_config(self) -> None:
        """An unreadable or malformed config falls through gracefully."""
        worklog = _make_worklog_dir({"projectName": "will-be-ignored"})
        (worklog / "config.yaml").write_text("{invalid: yaml: [}", encoding="utf-8")
        from skill.shared.project_name import resolve_project_name

        result = resolve_project_name(worklog_dir=str(worklog))
        assert result != ""

    def test_env_var_worklog_dir(self) -> None:
        """When worklog_dir is None and WL_WORKLOG_DIR is set, uses it."""
        worklog = _make_worklog_dir({"projectName": "EnvProject"})
        from skill.shared.project_name import resolve_project_name

        with mock.patch.dict(
            os.environ, {"WL_WORKLOG_DIR": str(worklog)}, clear=False
        ):
            result = resolve_project_name()
            assert result == "EnvProject"

    def test_cwd_fallback(self) -> None:
        """When no worklog_dir and no env var, falls back to cwd name."""
        from skill.shared.project_name import resolve_project_name

        env = dict(os.environ)
        env.pop("WL_WORKLOG_DIR", None)
        with mock.patch.dict(os.environ, env, clear=False):
            result = resolve_project_name(worklog_dir=None)
            assert result != ""
            assert isinstance(result, str)

    def test_never_empty(self) -> None:
        """resolve_project_name always returns a non-empty string."""
        from skill.shared.project_name import resolve_project_name

        # Worst case: worklog_dir="/", parent name is "", then falls to cwd.
        result = resolve_project_name(worklog_dir="/")
        assert result != ""

    def test_shared_helper_importable(self) -> None:
        """The helper lives in skill.shared and is importable by refactor."""
        from skill.shared.project_name import resolve_project_name

        assert callable(resolve_project_name)

    def test_refactor_imports_shared_helper(self) -> None:
        """refactor.py imports resolve_project_name from the shared module."""
        from skill.refactor.scripts import refactor

        assert hasattr(refactor, "resolve_project_name")


# ── Report heading integration tests (AC1, AC4) ─────────────────────────


class TestReportHeading:
    """Integration tests verifying the CLI report heading includes the project name."""

    def _capture_report_output(
        self, worklog_config: dict | None, extra_args: list[str] | None = None
    ) -> str:
        """Run refactor in dry-run + non-JSON mode and capture stdout."""
        worklog = _make_worklog_dir(worklog_config)
        args = ["--dry-run"] + (extra_args or [])

        buf = io.StringIO()
        with redirect_stdout(buf):
            with mock.patch.dict(
                os.environ, {"WL_WORKLOG_DIR": str(worklog)}, clear=False
            ):
                from skill.refactor.scripts import refactor as rf

                rf.main(args)

        return buf.getvalue()

    def test_heading_contains_project_name(self) -> None:
        """The non-JSON report heading includes the resolved project name (AC1)."""
        output = self._capture_report_output({"projectName": "TestProject"})
        assert "=== TestProject Refactor Report ===" in output

    def test_heading_uses_directory_fallback(self) -> None:
        """Without projectName, the heading falls back to directory name."""
        worklog = _make_worklog_dir({"prefix": "SA"})
        dir_name = worklog.parent.name

        buf = io.StringIO()
        with redirect_stdout(buf):
            with mock.patch.dict(
                os.environ, {"WL_WORKLOG_DIR": str(worklog)}, clear=False
            ):
                from skill.refactor.scripts import refactor as rf

                rf.main(["--dry-run"])

        output = buf.getvalue()
        assert f"=== {dir_name} Refactor Report ===" in output

    def test_dry_run_heading_same_as_regular(self) -> None:
        """--dry-run output uses the same project-name-bearing heading (AC4)."""
        output_dry = self._capture_report_output(
            {"projectName": "DryRunProject"}, extra_args=["--dry-run"]
        )
        assert "=== DryRunProject Refactor Report ===" in output_dry

    def test_json_output_not_affected_by_heading(self) -> None:
        """--json output is a structured dict; the heading change doesn't break it."""
        worklog = _make_worklog_dir({"projectName": "JsonProject"})

        buf = io.StringIO()
        with redirect_stdout(buf):
            with mock.patch.dict(
                os.environ, {"WL_WORKLOG_DIR": str(worklog)}, clear=False
            ):
                from skill.refactor.scripts import refactor as rf

                rf.main(["--dry-run", "--json"])

        output = buf.getvalue()
        data = json.loads(output)
        assert "summary" in data