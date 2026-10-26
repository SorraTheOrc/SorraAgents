"""Non-Python changed files must never reach ruff — end to end (SA-0MSTMAPL40019JX5).

The audit's Phase-1 code-quality scan calls::

    cq_scope_files = _git_changed_files(runner)
    run_code_quality(project_root=..., runner=..., fix=False,
                     files=cq_scope_files or None)

On 2026-08-11 the changed-file list contained ``proxy/config-cheap.yaml``;
the YAML path was handed to ``ruff check`` as an explicit argument, ruff
parsed it as Python, emitted 121 false-positive ``invalid-syntax`` findings,
and ``create_epics_for_findings`` turned every one of them into a work item
that a later audit tier had to drain one by one.

These tests pin the behaviour at the two call sites that caused the damage:

  * the scan (AC1/AC3/AC4): a YAML/JSON/Markdown entry in the scoped
    changed-file list is never passed to ruff, while a genuine ``.py``
    finding still surfaces unchanged;
  * the epic creation (AC2): no finding work item is created for a
    non-Python file — even if such a finding somehow reaches the epic
    creator (defence in depth against a future ruff exit-code/format
    regression).

Every test asserts observable behaviour through the production entry points
(``run_code_quality`` and ``create_epics_for_findings``), not source text.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from code_review.scripts import create_quality_epics
from code_review.scripts.code_quality import run_code_quality

#: The exact YAML path from the 2026-08-11 incident.
INCIDENT_YAML = "proxy/config-cheap.yaml"


def _invalid_syntax_output(path: str) -> str:
    """Ruff's JSON output for a non-Python file parsed as Python."""
    return json.dumps([{
        "code": "invalid-syntax",
        "message": "Expected a statement",
        "location": {"row": 1, "column": 1},
        "filename": path,
    }])


def _ruff_finding_output(path: str) -> str:
    """Ruff's JSON output for a genuine ``.py`` finding (F401)."""
    return json.dumps([{
        "code": "F401",
        "message": "`os` imported but unused",
        "location": {"row": 1, "column": 8},
        "filename": path,
    }])


class _RuffRunner:
    """Runner double that records commands and mimics real ruff output.

    Real ruff parses every explicitly-passed path as Python, so a YAML path
    in the command yields ``invalid-syntax`` findings. Reproducing that makes
    a filtering regression show up in *both* the recorded command and the
    findings — exactly the failure mode of the incident.
    """

    def __init__(self, py_output: str = "[]"):
        self.calls: list[list[str]] = []
        self._py_output = py_output

    def __call__(self, cmd):
        self.calls.append(list(cmd))
        if cmd and cmd[0] == "ruff":
            # Positional targets are the args between ``check`` and the first
            # option flag — the exclude globs after ``--extend-exclude`` are
            # not targets, so they must not be mistaken for files.
            targets: list[str] = []
            for arg in cmd[2:]:
                if str(arg).startswith("-"):
                    break
                targets.append(str(arg))
            non_python = [
                t for t in targets
                if t.endswith((".yaml", ".yml", ".json", ".toml", ".md"))
            ]
            if non_python:
                return SimpleNamespace(
                    returncode=1,
                    stdout=_invalid_syntax_output(non_python[0]),
                    stderr="",
                )
            return SimpleNamespace(
                returncode=0 if self._py_output == "[]" else 1,
                stdout=self._py_output,
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def ruff_calls(self) -> list[list[str]]:
        return [c for c in self.calls if c and c[0] == "ruff"]


class _WlRunner:
    """Runner double for the ``wl`` calls made by the epic creator."""

    def __init__(self, existing_epic: str | None = "SA-EPIC"):
        self.calls: list[list[str]] = []
        self._existing_epic = existing_epic

    def __call__(self, cmd):
        self.calls.append(list(cmd))
        joined = " ".join(str(c) for c in cmd)
        if "wl list" in joined:
            items = (
                [{"id": self._existing_epic,
                  "title": create_quality_epics.EPIC_TITLE,
                  "issueType": "epic",
                  "createdAt": "2026-08-01T00:00:00.000Z"}]
                if self._existing_epic else []
            )
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"success": True, "workItems": items}),
                stderr="",
            )
        if "wl show" in joined:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"success": True, "children": []}),
                stderr="",
            )
        if "wl create" in joined:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({
                    "success": True,
                    "workItem": {"id": "SA-CREATED"},
                }),
                stderr="",
            )
        return SimpleNamespace(
            returncode=0, stdout=json.dumps({"success": True}), stderr="",
        )

    def create_calls(self) -> list[list[str]]:
        return [c for c in self.calls if c[:2] == ["wl", "create"]]


@pytest.fixture()
def project(tmp_path: Path, monkeypatch) -> Path:
    """A project root shaped like the incident: a real .py plus a YAML file.

    ``monkeypatch.chdir`` mirrors the audit, which scopes the linters with
    repo-relative paths resolved from the project root.
    """
    (tmp_path / "app.py").write_text("import os\n", encoding="utf-8")
    (tmp_path / "proxy").mkdir()
    (tmp_path / INCIDENT_YAML).write_text(
        "name: cheap-model\nrate_limit:\n  rpm: 60\n", encoding="utf-8",
    )
    (tmp_path / "ruff.toml").write_text(
        'include = ["*.py", "*.yaml"]\n', encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# AC1 / AC3 / AC4 — the scoped scan
# ---------------------------------------------------------------------------


class TestScopedScanIgnoresNonPythonChangedFiles:
    """The audit's scoped code-quality scan never hands non-Python files to ruff."""

    def test_yaml_in_changed_file_list_is_never_passed_to_ruff(self, project):
        runner = _RuffRunner()
        result = run_code_quality(
            project_root=project,
            runner=runner,
            fix=False,
            files=["app.py", INCIDENT_YAML],
        )

        ruff_calls = runner.ruff_calls()
        assert ruff_calls, "expected ruff to be invoked for the .py file"
        for cmd in ruff_calls:
            assert INCIDENT_YAML not in cmd, f"YAML leaked into ruff: {cmd}"
            targets = []
            for arg in cmd[2:]:
                if str(arg).startswith("-"):
                    break
                targets.append(str(arg))
            assert not any(
                t.endswith((".yaml", ".yml")) for t in targets
            ), f"non-Python target in ruff command: {cmd}"
        # AC1: the YAML is skipped, so it can never yield a finding.
        assert result["findings"] == []
        assert result["total_findings"] == 0

    def test_non_python_only_changed_list_skips_ruff_entirely(self, project):
        """When nothing lintable changed, ruff is not invoked at all."""
        runner = _RuffRunner()
        result = run_code_quality(
            project_root=project,
            runner=runner,
            fix=False,
            files=[INCIDENT_YAML, "README.md", "data.json"],
        )

        assert runner.ruff_calls() == []
        assert result["findings"] == []

    def test_genuine_python_finding_still_surfaces(self, project):
        """AC4: filtering non-Python files never suppresses .py findings."""
        runner = _RuffRunner(py_output=_ruff_finding_output("app.py"))
        result = run_code_quality(
            project_root=project,
            runner=runner,
            fix=False,
            files=["app.py", INCIDENT_YAML],
        )

        assert len(result["findings"]) == 1
        finding = result["findings"][0]
        assert finding["file"] == "app.py"
        assert finding["code"] == "F401"
        assert finding["linter"] == "ruff"

    def test_whole_project_scan_still_targets_root(self, project):
        """AC3: files=None keeps the whole-project (root) scan unchanged."""
        runner = _RuffRunner()
        run_code_quality(
            project_root=project, runner=runner, fix=False, files=None,
        )

        ruff_calls = runner.ruff_calls()
        assert len(ruff_calls) == 1
        cmd = ruff_calls[0]
        assert str(project) in cmd or "." in cmd


# ---------------------------------------------------------------------------
# AC2 — no finding work items for non-Python files
# ---------------------------------------------------------------------------


class TestNoFindingWorkItemsForNonPythonFiles:
    """The audit's epic-creation site creates no items for non-Python files."""

    def test_audit_scope_creates_no_work_items(self, project):
        """Mirrors audit_runner: scan, then create epics only for findings."""
        runner = _RuffRunner()
        cq_result = run_code_quality(
            project_root=project,
            runner=runner,
            fix=False,
            files=["app.py", INCIDENT_YAML],
        )

        wl = _WlRunner()
        if cq_result["findings"]:  # the audit's `if cq_findings:` gate
            create_quality_epics.create_epics_for_findings(
                cq_result["findings"], runner=wl,
            )

        assert wl.create_calls() == [], (
            f"work items created for a non-Python changed file: "
            f"{wl.create_calls()}"
        )

    def test_ruff_finding_on_yaml_never_creates_a_work_item(self):
        """Defence in depth: even a stray non-Python finding creates nothing."""
        stray = [{
            "file": INCIDENT_YAML,
            "line": 1,
            "severity": "critical",
            "message": "Expected a statement",
            "linter": "ruff",
            "code": "invalid-syntax",
        }]
        wl = _WlRunner()
        result = create_quality_epics.create_epics_for_findings(
            stray, runner=wl,
        )

        assert result["children_created"] == 0
        assert result["epic_id"] is None
        assert wl.create_calls() == []

    def test_mixed_findings_create_only_the_python_work_item(self):
        findings = [
            {
                "file": "app.py",
                "line": 1,
                "severity": "critical",
                "message": "`os` imported but unused",
                "linter": "ruff",
                "code": "F401",
            },
            {
                "file": INCIDENT_YAML,
                "line": 1,
                "severity": "critical",
                "message": "Expected a statement",
                "linter": "ruff",
                "code": "invalid-syntax",
            },
        ]
        wl = _WlRunner()
        result = create_quality_epics.create_epics_for_findings(
            findings, runner=wl,
        )

        assert result["children_created"] == 1
        creates = wl.create_calls()
        assert len(creates) == 1
        assert INCIDENT_YAML not in " ".join(creates[0])

    def test_non_python_linter_findings_are_untouched(self):
        """The guard is ruff-specific: eslint findings on .ts still create items."""
        findings = [{
            "file": "src/app.ts",
            "line": 3,
            "severity": "high",
            "message": "no-unused-vars",
            "linter": "eslint",
            "code": "no-unused-vars",
        }]
        wl = _WlRunner()
        result = create_quality_epics.create_epics_for_findings(
            findings, runner=wl,
        )

        assert result["children_created"] == 1
