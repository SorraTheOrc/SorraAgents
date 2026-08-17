"""Tests for file-scoped, read-only code-quality scans (SA-0MSKB6VWU000RT58).

The audit's code-quality check must:
  - run linters only over the git changed-file list instead of the whole repo
  - never mutate files during an audit (fix=False — audits are read-only)
  - keep severity classification unchanged
  - preserve cwd-independence (scoping derives from the same git changed-file
    resolution used for the Phase 1/2 file-scope manifest)
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from skill.audit.tests.wl_helpers import make_stateful_runner


def _mock_result(returncode=0, stdout="", stderr=""):
    result = MagicMock(spec=subprocess.CompletedProcess)
    result.returncode = returncode
    result.stdout = stdout
    result.stderr = stderr
    return result


class TestCodeQualityFileScoping:
    """run_code_quality / run_linters_for_project honor the files scope."""

    def test_run_code_quality_forwards_files_to_linters(self):
        """run_code_quality passes the files list through to the linter runner."""
        from skill.code_review.scripts.code_quality import run_code_quality

        captured = {}

        def fake_linters(project_root, runner=None, fix=False, files=None):
            captured["files"] = files
            captured["fix"] = fix
            return {
                "languages": ["python"], "linters": [],
                "total_findings": 0, "findings_by_severity": {},
                "findings": [], "fixes_applied": 0,
            }

        with patch(
            "skill.code_review.scripts.code_quality.run_linters_for_project",
            side_effect=fake_linters,
        ):
            result = run_code_quality(
                str(REPO_ROOT), runner=MagicMock(), files=["src/a.py"],
            )

        assert result["success"] is True
        assert captured["files"] == ["src/a.py"]
        assert captured["fix"] is False

    def test_run_code_quality_defaults_files_none(self):
        """Without files, the whole-project behavior is preserved (files=None)."""
        from skill.code_review.scripts.code_quality import run_code_quality

        captured = {}

        def fake_linters(project_root, runner=None, fix=False, files=None):
            captured["files"] = files
            return {
                "languages": ["python"], "linters": [],
                "total_findings": 0, "findings_by_severity": {},
                "findings": [], "fixes_applied": 0,
            }

        with patch(
            "skill.code_review.scripts.code_quality.run_linters_for_project",
            side_effect=fake_linters,
        ):
            run_code_quality(str(REPO_ROOT), runner=MagicMock())

        assert captured["files"] is None

    def test_run_linters_for_project_scopes_ruff_command(self):
        """run_linters_for_project passes files to run_ruff, which targets only
        the scoped files in the ruff command."""
        from skill.code_review.scripts.linter_runner import run_linters_for_project

        commands: list[list[str]] = []

        def runner(cmd):
            commands.append(list(cmd))
            return _mock_result(returncode=0, stdout="")

        with (
            patch("skill.code_review.scripts.linter_runner.detect_languages",
                  return_value=["python"]),
            patch("skill.code_review.scripts.linter_runner.probe_linter",
                  return_value={"name": "ruff", "available": True}),
        ):
            run_linters_for_project(
                str(REPO_ROOT), runner=runner, files=["src/changed.py"],
            )

        ruff_cmds = [c for c in commands if c and c[0] == "ruff"]
        assert ruff_cmds, "expected a ruff command"
        assert "src/changed.py" in ruff_cmds[0]
        # The whole project root must NOT be passed as the sole target.
        assert str(REPO_ROOT) not in ruff_cmds[0]

    def test_run_linters_for_project_full_scan_unchanged(self):
        """Without files, the full-project ruff command is unchanged."""
        from skill.code_review.scripts.linter_runner import run_linters_for_project

        commands: list[list[str]] = []

        def runner(cmd):
            commands.append(list(cmd))
            return _mock_result(returncode=0, stdout="")

        with (
            patch("skill.code_review.scripts.linter_runner.detect_languages",
                  return_value=["python"]),
            patch("skill.code_review.scripts.linter_runner.probe_linter",
                  return_value={"name": "ruff", "available": True}),
        ):
            run_linters_for_project(str(REPO_ROOT), runner=runner)

        ruff_cmds = [c for c in commands if c and c[0] == "ruff"]
        assert ruff_cmds
        assert str(REPO_ROOT) in ruff_cmds[0]

    def test_run_shellcheck_filters_by_scope(self):
        """run_shellcheck only checks shell files in the provided scope."""
        from skill.code_review.scripts.linter_runner import run_shellcheck

        checked: list[str] = []

        def runner(cmd):
            checked.append(cmd[-1])
            return _mock_result(returncode=0, stdout="")

        # Two shell files under a temp project; only one is in scope.
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.sh").write_text("#!/bin/bash\necho hi\n")
            (root / "b.sh").write_text("#!/bin/bash\necho hi\n")
            with (
                patch("skill.code_review.scripts.linter_runner.detect_languages",
                      return_value=["shell"]),
                patch("skill.code_review.scripts.linter_runner.probe_linter",
                      return_value={"name": "shellcheck", "available": True}),
            ):
                run_shellcheck(str(root), runner=runner,
                               files=[str(root / "a.sh")])

        assert checked == [str(root / "a.sh")], f"got {checked}"


class TestAuditReadOnlyCodeQuality:
    """The audit path invokes code quality scoped + read-only (fix=False)."""

    def test_cmd_issue_passes_files_and_fix_false(self):
        """cmd_issue scopes the code-quality scan to git changed files and
        never passes fix=True (read-only mandate)."""
        sys.path.insert(0, str(REPO_ROOT))
        from skill.audit.scripts import audit_runner

        captured = {}

        def fake_run_code_quality(project_root=None, runner=None, fix=False,
                                  files=None):
            captured["fix"] = fix
            captured["files"] = files
            return {
                "success": True, "languages": ["python"], "linters": [],
                "total_findings": 0, "findings_by_severity": {},
                "findings": [], "fixes_applied": 0,
            }

        # Mock git to report two changed files.
        def fake_runner(cmd):
            cmd_str = " ".join(cmd)
            if cmd[:3] == ["git", "diff", "--name-only"]:
                return _mock_result(returncode=0, stdout="src/a.py\n")
            if cmd[:3] == ["git", "status", "--porcelain=v1"]:
                return _mock_result(returncode=0, stdout=" M src/b.py\n")
            if "rev-parse HEAD" in cmd_str:
                return _mock_result(returncode=0, stdout="a" * 40)
            if "wl show" in cmd_str:
                return _mock_result(
                    returncode=0,
                    stdout=json.dumps({
                        "success": True,
                        "workItem": {
                            "id": "TEST-1", "title": "T",
                            "description": "## Acceptance Criteria\n- AC1: x",
                            "status": "open",
                        },
                        "children": [],
                    }),
                )
            return _mock_result(
                returncode=0, stdout=json.dumps({"success": True}),
            )

        with (
            patch.object(audit_runner, "_call_pi_and_maybe_log",
                         return_value={"extracted_text": json.dumps([
                             {"index": 0, "verdict": "met", "evidence": "f.py:1"},
                         ])}),
            patch("skill.code_review.scripts.code_quality.run_code_quality",
                  side_effect=fake_run_code_quality),
            patch.object(audit_runner, "_git_changed_files",
                         return_value=["src/a.py", "src/b.py"]),
            patch.object(audit_runner, "_run_phase2_deep_analysis",
                         side_effect=lambda issue, ac, ch, **kw: (ac, ch, True)),
        ):
            rc = audit_runner.cmd_issue(
                "TEST-1", persist=False, force=True,
                runner=make_stateful_runner(fake_runner),
            )

        assert rc == 0
        assert captured["fix"] is False, "audits must be read-only (fix=False)"
        assert set(captured["files"]) == {"src/a.py", "src/b.py"}, (
            "code-quality scan must be scoped to the git changed-file list"
        )
