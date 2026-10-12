"""End-to-end tests for the test-churn skill entry point.

Exercises the CLI exactly as an agent would invoke it against a real (small)
fixture repository and asserts the two documented artefacts are produced with
the expected content.  This is the AC5 "entry point works end to end" test.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ENTRY_POINT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "test_churn_analysis.py"
)


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(ENTRY_POINT), *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


def test_entry_point_is_a_file():
    assert ENTRY_POINT.is_file()


def test_entry_point_writes_both_artefacts(git_repo, tmp_path):
    out = tmp_path / "out"
    result = _run(
        "--project-root",
        str(git_repo),
        "--branch",
        "dev",
        "--output-dir",
        str(out),
        "--format",
        "both",
        "--quiet",
    )
    assert result.returncode == 0, result.stderr

    report = out / "test_churn_report.md"
    data = out / "test_churn_data.json"
    assert report.is_file()
    assert data.is_file()

    text = report.read_text(encoding="utf-8")
    assert "# Test Churn Report" in text
    assert "Scope and definitions" in text
    assert "TDD value assessment" in text

    payload = json.loads(data.read_text(encoding="utf-8"))
    assert payload["metadata"]["branch"] == "dev"
    assert payload["metadata"]["project_root"] == str(git_repo.resolve())
    assert payload["primary_metrics"]["total_commits"] == 4
    assert payload["primary_metrics"]["deleted_test_files"] == 1


def test_entry_point_format_json_writes_only_json(git_repo, tmp_path):
    out = tmp_path / "json-only"
    result = _run(
        "--project-root",
        str(git_repo),
        "--output-dir",
        str(out),
        "--format",
        "json",
        "--quiet",
    )
    assert result.returncode == 0, result.stderr
    assert (out / "test_churn_data.json").is_file()
    assert not (out / "test_churn_report.md").exists()


def test_entry_point_stdout_prints_report(git_repo, tmp_path):
    out = tmp_path / "stdout"
    result = _run(
        "--project-root",
        str(git_repo),
        "--output-dir",
        str(out),
        "--format",
        "json",
        "--stdout",
        "--quiet",
    )
    assert result.returncode == 0, result.stderr
    assert "# Test Churn Report" in result.stdout


def test_entry_point_rejects_non_repository(tmp_path):
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    result = _run("--project-root", str(not_a_repo))
    assert result.returncode != 0
    assert "not a git repository" in result.stderr.lower()
