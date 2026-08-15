"""Tests: config remediation loop (ruff) (T2 — SA-0MST01OIN008MXYT).

Defines the confident-false-positive remediation-loop contract that F2 will
implement (the loop itself ships with this suite, mirroring the T1/F1
test-first pattern): minimal surgical ruff config edits targeted at the
flagged files, local commits (no push), content-fingerprint re-hash AFTER
each commit, a code-quality-only re-run (pipeline never restarted), a
3-iteration cap (env-configurable) with exhaustion annotation, and
uncertain findings never entering the loop.

Coverage per T2 ACs:
  1. Ruff config locating (pyproject.toml [tool.ruff] + ruff.toml) with
     creation of a missing config — both formats.
  2. Remediation edits are minimal and surgical: per-file-ignores targeted
     at the flagged files only; no sweeping rule changes; no `# noqa`; no
     edits to _RUFF_SEVERITY_MAP / _classify_ruff.
  3. Each applied config fix is committed locally (no push) and the content
     fingerprint is recomputed AFTER the commit (working-tree clean).
  4. Re-run after remediation is scoped to the code-quality scan only
     (same scoped changed-file list, fix=False) — the pipeline is NOT
     restarted; remaining findings are re-classified.
  5. The loop is capped at 3 config-fix iterations (default; env-
     configurable); a finding persisting after cap exhaustion remains a
     blocking `genuine` finding annotated "remediation loop exhausted".
  6. Uncertain findings never enter the loop (no config edit, no commit).
"""

from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pytest

from skill.audit.scripts import audit_runner
from skill.code_review.scripts import linter_runner


@pytest.fixture(autouse=True)
def _free_audit_slot():
    """Neutralize the host-wide audit semaphore (same pattern as the other
    audit test modules — SA-0MSCDC4750019G9Y)."""
    with mock.patch.object(
        audit_runner, "_acquire_audit_slot", return_value=contextlib.nullcontext()
    ):
        yield


def _finding(severity: str = "critical", code: str = "F841",
             file: str = "src/bad.py", linter: str = "ruff") -> dict:
    return {
        "severity": severity,
        "file": file,
        "line": 1,
        "message": f"{code} message",
        "linter": linter,
        "code": code,
    }


def _screen_entry(finding: dict, classification: str = "confident-false-positive",
                  remediable: bool = True, justification: str = "misfires") -> dict:
    return {
        "index": 0,
        "finding": finding,
        "classification": classification,
        "justification": justification,
        "remediable": remediable,
        "screen_failed": False,
    }


def _git_runner():
    """Mock runner answering git commands with success + a stable sha."""
    runner = mock.MagicMock()

    def _side(cmd, **kwargs):
        cmd_str = " ".join(cmd)
        if cmd_str.startswith("git rev-parse"):
            return SimpleNamespace(returncode=0, stdout="abc1234\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    runner.side_effect = _side
    return runner


def _run_loop(tmp_path, cq_findings, fp_screen_results, runner=None,
              screen_returns=None, cq_returns=None, **kwargs):
    """Invoke _run_remediation_loop with the standard mocks applied."""
    if runner is None:
        runner = _git_runner()
    if screen_returns is None:
        screen_returns = []
    if cq_returns is None:
        cq_returns = {"success": True, "findings": [], "fixes_applied": 0}

    with (
        mock.patch.object(
            audit_runner, "_git_changed_files", return_value=["src/bad.py"]
        ),
        mock.patch(
            "skill.code_review.scripts.code_quality.run_code_quality",
            return_value=cq_returns,
        ),
        mock.patch.object(
            audit_runner, "_screen_ruff_findings", return_value=screen_returns
        ),
    ):
        return audit_runner._run_remediation_loop(
            issue_id="TEST-1",
            cq_findings=cq_findings,
            fp_screen_results=fp_screen_results,
            runner=runner,
            pi_bin="pi",
            resolved_model="m",
            debug_log=None,
            timeout=None,
            ac_fallback_used=mock.Mock(),
            project_root=tmp_path,
            worklog_dir=None,
            work_item={"id": "TEST-1"},
            content_fingerprint="fp-before",
        )


# ===========================================================================
# AC1 — ruff config locating (both formats) + creation
# ===========================================================================

class TestLocateRuffConfig:
    """AC1: pyproject.toml [tool.ruff] and ruff.toml are both located; a
    missing config is created (Q6)."""

    def test_existing_ruff_toml_located(self, tmp_path):
        (tmp_path / "ruff.toml").write_text("# existing\n")
        cfg = linter_runner.locate_ruff_config(tmp_path)
        assert cfg == tmp_path / "ruff.toml"

    def test_pyproject_with_tool_ruff_located(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            "[tool.ruff]\nline-length = 100\n"
        )
        cfg = linter_runner.locate_ruff_config(tmp_path)
        assert cfg == tmp_path / "pyproject.toml"

    def test_pyproject_without_tool_ruff_created_in_place(self, tmp_path):
        """pyproject.toml exists but lacks [tool.ruff] → section created."""
        (tmp_path / "pyproject.toml").write_text(
            "[project]\nname = \"x\"\n"
        )
        cfg = linter_runner.locate_ruff_config(tmp_path)
        assert cfg == tmp_path / "pyproject.toml"
        assert "[tool.ruff]" in cfg.read_text()
        # The rest of pyproject.toml is preserved.
        assert "[project]" in cfg.read_text()

    def test_missing_config_creates_ruff_toml(self, tmp_path):
        cfg = linter_runner.locate_ruff_config(tmp_path)
        assert cfg == tmp_path / "ruff.toml"
        assert cfg.exists()


# ===========================================================================
# AC2 — minimal, surgical remediation edits
# ===========================================================================

class TestApplyRuffRemediation:
    """AC2: edits are surgical per-file-ignores; no sweeping changes, no
    `# noqa`, no severity-map edits."""

    def test_ruff_toml_format(self, tmp_path):
        cfg = tmp_path / "ruff.toml"
        cfg.write_text("# header\n")
        assert linter_runner.apply_ruff_remediation(
            cfg, [_screen_entry(_finding(file="src/bad.py", code="F841"))]
        )
        text = cfg.read_text()
        assert "# header" in text
        assert "[per-file-ignores]" in text
        assert '"src/bad.py" = ["F841"]' in text

    def test_pyproject_format(self, tmp_path):
        cfg = tmp_path / "pyproject.toml"
        cfg.write_text("[tool.ruff]\n")
        assert linter_runner.apply_ruff_remediation(
            cfg, [_screen_entry(_finding(file="src/a.py", code="S101"))]
        )
        text = cfg.read_text()
        assert "[tool.ruff.per-file-ignores]" in text
        assert '"src/a.py" = ["S101"]' in text

    def test_targets_only_flagged_files(self, tmp_path):
        """Only the flagged file+code pairs are ignored — no sweeping
        rule-level changes (no `select`/`ignore`/`extend-select` keys)."""
        cfg = tmp_path / "ruff.toml"
        cfg.write_text("")
        assert linter_runner.apply_ruff_remediation(
            cfg, [
                _screen_entry(_finding(file="src/bad.py", code="F841")),
                _screen_entry(_finding(file="src/other.py", code="E402")),
            ]
        )
        text = cfg.read_text()
        assert '"src/bad.py" = ["F841"]' in text
        assert '"src/other.py" = ["E402"]' in text
        for sweeping in ("select", "extend-select", "ignore =", "exclude"):
            assert sweeping not in text, f"sweeping change detected: {sweeping}"

    def test_no_noqa_and_severity_map_untouched(self, tmp_path):
        before_map = dict(linter_runner._RUFF_SEVERITY_MAP)
        before_classify = linter_runner._classify_ruff("F841")
        cfg = tmp_path / "ruff.toml"
        cfg.write_text("")
        assert linter_runner.apply_ruff_remediation(
            cfg, [_screen_entry(_finding(file="src/bad.py", code="F841"))]
        )
        assert "noqa" not in cfg.read_text().lower()
        # The severity classifier is never touched (T2 AC2).
        assert linter_runner._RUFF_SEVERITY_MAP == before_map
        assert linter_runner._classify_ruff("F841") == before_classify

    def test_merges_into_existing_entries_idempotent(self, tmp_path):
        cfg = tmp_path / "ruff.toml"
        cfg.write_text('[per-file-ignores]\n"src/bad.py" = ["F841"]\n')
        # Same pair → no change (idempotent).
        assert not linter_runner.apply_ruff_remediation(
            cfg, [_screen_entry(_finding(file="src/bad.py", code="F841"))]
        )
        # New code for the same file → merged.
        assert linter_runner.apply_ruff_remediation(
            cfg, [_screen_entry(_finding(file="src/bad.py", code="E402"))]
        )
        assert '"src/bad.py" = ["F841", "E402"]' in cfg.read_text()


# ===========================================================================
# AC3/AC4/AC5/AC6 — the remediation loop
# ===========================================================================

class TestRemediationLoop:
    """The loop: commit + re-hash, CQ-only re-run, cap + exhaustion,
    uncertain findings never entering the loop."""

    def test_loop_commits_and_rehashes_fingerprint_after_commit(self, tmp_path):
        """AC3: each config fix is committed locally; the content fingerprint
        is recomputed AFTER the commit (working-tree clean)."""
        finding = _finding()
        fp_results = [_screen_entry(finding)]
        order: list[str] = []

        def _fp(runner, issue_id, **kw):
            order.append("fp")
            return f"fp-{order.count('fp')}"

        def _commit(runner, config_path, project_root):
            order.append("commit")
            return f"sha-{order.count('commit')}"

        with (
            mock.patch.object(audit_runner, "_git_changed_files",
                              return_value=["src/bad.py"]),
            mock.patch(
                "skill.code_review.scripts.code_quality.run_code_quality",
                return_value={"success": True, "findings": [],
                              "fixes_applied": 0},
            ),
            mock.patch.object(audit_runner, "_screen_ruff_findings",
                              return_value=[]),
            mock.patch.object(audit_runner, "_compute_content_fingerprint",
                              side_effect=_fp),
            mock.patch.object(audit_runner, "_commit_config_remediation",
                              side_effect=_commit),
        ):
            results = audit_runner._run_remediation_loop(
                "TEST-1", [finding], fp_results, _git_runner(), "pi", "m",
                None, None, mock.Mock(), tmp_path, None, {"id": "TEST-1"},
                "fp-before",
            )
        # Commit happened BEFORE the fingerprint recompute (working tree
        # clean at re-hash time).
        assert order == ["commit", "fp"]
        assert results["iterations"] == 1
        assert len(results["commits"]) == 1
        assert results["commits"][0]["sha"] == "sha-1"
        assert results["commits"][0]["fingerprint_after"] == "fp-1"
        assert results["exhausted"] is False
        # The config file was created + edited in the project root.
        assert (tmp_path / "ruff.toml").exists()
        assert '"src/bad.py" = ["F841"]' in (tmp_path / "ruff.toml").read_text()

    def test_loop_reruns_scoped_code_quality_only(self, tmp_path):
        """AC4: the re-run is scoped to the code-quality scan only — same
        changed-file list, fix=False, exactly once per iteration. The
        pipeline is never restarted (only the scan is re-invoked)."""
        finding = _finding()
        fp_results = [_screen_entry(finding)]
        with (
            mock.patch.object(audit_runner, "_git_changed_files",
                              return_value=["src/bad.py"]) as git_changed,
            mock.patch(
                "skill.code_review.scripts.code_quality.run_code_quality"
            ) as cq,
            mock.patch.object(audit_runner, "_screen_ruff_findings",
                              return_value=[]),
            mock.patch.object(audit_runner, "_compute_content_fingerprint",
                              return_value="fp-1"),
            mock.patch.object(audit_runner, "_commit_config_remediation",
                              return_value="sha-1"),
        ):
            results = audit_runner._run_remediation_loop(
                "TEST-1", [finding], fp_results, _git_runner(), "pi", "m",
                None, None, mock.Mock(), tmp_path, None, {"id": "TEST-1"},
                "fp-before",
            )
        assert results["iterations"] == 1
        cq.assert_called_once()
        _, cq_kwargs = cq.call_args
        assert cq_kwargs.get("fix") is False
        assert cq_kwargs.get("files") == ["src/bad.py"]
        assert cq_kwargs.get("project_root") == tmp_path
        git_changed.assert_called_once()

    def test_loop_caps_at_max_and_annotates_exhaustion(self, tmp_path):
        """AC5: capped at 3 iterations (default); a persisting finding after
        cap exhaustion becomes blocking 'genuine' with the annotation."""
        finding = _finding()
        fp_results = [_screen_entry(finding)]
        with (
            mock.patch.object(audit_runner, "_git_changed_files",
                              return_value=["src/bad.py"]),
            mock.patch(
                "skill.code_review.scripts.code_quality.run_code_quality",
                return_value={"success": True, "findings": [finding],
                              "fixes_applied": 0},
            ),
            # The finding persists across every iteration.
            mock.patch.object(audit_runner, "_screen_ruff_findings",
                              return_value=fp_results),
            mock.patch.object(audit_runner, "_compute_content_fingerprint",
                              side_effect=lambda *a, **k: "fp-x"),
            mock.patch.object(audit_runner, "_commit_config_remediation",
                              return_value="sha-x"),
            # Each iteration "applies" a config change (real apply would
            # return False on the 2nd pass — the cap is about the loop).
            mock.patch.object(linter_runner, "apply_ruff_remediation",
                              return_value=True),
        ):
            results = audit_runner._run_remediation_loop(
                "TEST-1", [finding], fp_results, _git_runner(), "pi", "m",
                None, None, mock.Mock(), tmp_path, None, {"id": "TEST-1"},
                "fp-before",
            )
        assert results["iterations"] == 3
        assert results["max_iterations"] == 3
        assert len(results["commits"]) == 3
        assert results["exhausted"] is True
        entry = results["fp_screen_results"][0]
        assert entry["classification"] == "genuine"
        assert entry["remediable"] is False
        assert entry["remediation_exhausted"] is True
        assert "remediation loop exhausted" in entry["justification"]

    def test_loop_env_configurable_cap(self, tmp_path):
        """AC5: the cap is env-configurable (AUDIT_REMEDIATION_MAX_ITERATIONS)."""
        finding = _finding()
        fp_results = [_screen_entry(finding)]
        with (
            mock.patch.dict(
                audit_runner.os.environ,
                {"AUDIT_REMEDIATION_MAX_ITERATIONS": "5"},
            ),
            mock.patch.object(audit_runner, "_git_changed_files",
                              return_value=["src/bad.py"]),
            mock.patch(
                "skill.code_review.scripts.code_quality.run_code_quality",
                return_value={"success": True, "findings": [finding],
                              "fixes_applied": 0},
            ),
            mock.patch.object(audit_runner, "_screen_ruff_findings",
                              return_value=fp_results),
            mock.patch.object(audit_runner, "_compute_content_fingerprint",
                              return_value="fp-x"),
            mock.patch.object(audit_runner, "_commit_config_remediation",
                              return_value="sha-x"),
            mock.patch.object(linter_runner, "apply_ruff_remediation",
                              return_value=True),
        ):
            results = audit_runner._run_remediation_loop(
                "TEST-1", [finding], fp_results, _git_runner(), "pi", "m",
                None, None, mock.Mock(), tmp_path, None, {"id": "TEST-1"},
                "fp-before",
            )
        assert results["max_iterations"] == 5
        assert results["iterations"] == 5

    def test_loop_uncertain_never_enters(self, tmp_path):
        """AC6: uncertain findings never enter the loop — no config edit,
        no commit, no scan re-run."""
        uncertain = _screen_entry(_finding(), classification="uncertain",
                                  remediable=False,
                                  justification="cannot tell")
        results = _run_loop(tmp_path, [_finding()], [uncertain])
        assert results["iterations"] == 0
        assert results["commits"] == []
        assert results["exhausted"] is False
        # No config file was created in the project root.
        assert not (tmp_path / "ruff.toml").exists()
        assert not (tmp_path / "pyproject.toml").exists()

    def test_loop_stops_when_finding_resolved(self, tmp_path):
        """After a successful edit the re-scan reports nothing → the loop
        stops (iterations=1, no exhaustion)."""
        finding = _finding()
        results = _run_loop(tmp_path, [finding], [_screen_entry(finding)])
        assert results["iterations"] == 1
        assert results["exhausted"] is False
        assert results["cq_findings"] == []
        assert results["fp_screen_results"] == []
        assert results["fingerprint_after"] is not None

    def test_loop_no_remediable_no_config_created(self, tmp_path):
        """A screen with zero remediable entries (mixed genuine + uncertain)
        never touches the filesystem."""
        genuine = _screen_entry(_finding(code="F401"),
                                classification="genuine", remediable=False,
                                justification="real defect")
        uncertain = _screen_entry(_finding(code="E402"),
                                  classification="uncertain", remediable=False)
        results = _run_loop(tmp_path, [_finding(code="F401"), _finding(code="E402")],
                            [genuine, uncertain])
        assert results["iterations"] == 0
        assert not (tmp_path / "ruff.toml").exists()

    def test_cmd_issue_wires_remediation_loop_after_screen(self, capsys):
        """The loop is invoked from the pipeline for a remediable CFP finding
        (after the screen, before the blocking decision). The loop itself is
        mocked here so no real config file is written into the checkout; the
        assertion is that the pipeline CALLS it with the screened findings."""
        captured = {}

        def _neutral_loop(**kwargs):
            captured["cq_findings"] = kwargs.get("cq_findings")
            captured["fp_screen_results"] = kwargs.get("fp_screen_results")
            return {
                "iterations": 0,
                "max_iterations": 3,
                "exhausted": False,
                "commits": [],
                "fingerprint_before": kwargs.get("content_fingerprint"),
                "fingerprint_after": kwargs.get("content_fingerprint"),
                "cq_findings": kwargs.get("cq_findings"),
                "fp_screen_results": kwargs.get("fp_screen_results"),
            }

        finding = _finding(severity="critical", code="F401")
        runner = mock.MagicMock()

        def _side(cmd, **kw):
            cmd_str = " ".join(cmd)
            if "show" in cmd_str and "--children" not in cmd_str and "--json" in cmd_str:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({
                        "success": True,
                        "workItem": {"id": "TEST-1", "status": "open"},
                    }),
                    stderr="",
                )
            if "update" in cmd_str:
                return SimpleNamespace(returncode=0, stdout=json.dumps({"success": True}), stderr="")
            if "--children" in cmd_str:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({
                        "success": True,
                        "workItem": {
                            "id": "TEST-1",
                            "description": "## Acceptance Criteria\n- AC1: thing",
                            "status": "in_progress",
                        },
                        "children": [],
                    }),
                    stderr="",
                )
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        runner.side_effect = _side

        def _fake_pi(issue_id, context, prompt, **kwargs):
            if context == audit_runner.FP_SCREEN_CONTEXT:
                return {"extracted_text": json.dumps([
                    {"index": 0, "classification": "confident-false-positive",
                     "justification": "misfires"},
                ])}
            return {"extracted_text": json.dumps([
                {"index": 0, "verdict": "met", "evidence": "file.py:1"},
            ])}

        with (
            mock.patch.object(audit_runner, "_call_pi_and_maybe_log",
                              side_effect=_fake_pi),
            mock.patch(
                "skill.code_review.scripts.code_quality.run_code_quality",
                return_value={"success": True, "findings": [finding],
                              "fixes_applied": 0},
            ),
            mock.patch.object(audit_runner, "_run_remediation_loop",
                              side_effect=_neutral_loop),
            mock.patch(
                "skill.code_review.scripts.create_quality_epics."
                "create_epics_for_findings",
                return_value={"epic_id": None},
            ),
        ):
            rc = audit_runner.cmd_issue(
                "TEST-1", persist=False, force=True, runner=runner,
            )
        assert rc == 0
        # The loop received the screened (CFP) findings from the pipeline.
        assert captured["fp_screen_results"][0]["classification"] == \
            "confident-false-positive"
        assert captured["cq_findings"] == [finding]


# ===========================================================================
# Report surfacing (T2 AC4 evidence in report + JSON)
# ===========================================================================

class TestRemediationReport:
    """Remediation loop outcomes surface in the audit report + JSON."""

    def test_report_includes_remediation_section(self):
        findings = [_finding()]
        remediation = {
            "iterations": 2,
            "max_iterations": 3,
            "exhausted": False,
            "commits": [
                {"sha": "aaa111", "file": "ruff.toml",
                 "change": "src/a.py -> E501, F401",
                 "fingerprint_after": "fp-1"},
                {"sha": "bbb222", "file": "ruff.toml",
                 "change": "src/b.py -> E402",
                 "fingerprint_after": "fp-2"},
            ],
            "fingerprint_before": "fp-before",
            "fingerprint_after": "fp-2",
            "cq_findings": [],
            "fp_screen_results": [],
        }
        report = audit_runner._assemble_issue_report(
            {"id": "TEST-1"}, [], [],
            code_quality_findings=findings,
            remediation_results=remediation,
            model="test-model",
        )
        assert "#### Remediation loop" in report
        assert "Config-fix iterations: 2 / 3" in report
        assert "aaa111" in report and "bbb222" in report
        assert "src/a.py -> E501, F401" in report
        assert "src/b.py -> E402" in report
        assert "fingerprint re-hashed after commit" in report

    def test_report_annotates_cap_exhaustion(self):
        remediation = {
            "iterations": 3,
            "max_iterations": 3,
            "exhausted": True,
            "commits": [],
            "fingerprint_before": "fp-before",
            "fingerprint_after": "fp-3",
        }
        report = audit_runner._assemble_issue_report(
            {"id": "TEST-1"}, [], [],
            code_quality_findings=[],
            remediation_results=remediation,
            model="test-model",
        )
        assert "#### Remediation loop" in report
        assert "Cap exhausted" in report
        assert "remediation loop exhausted" in report

    def test_json_includes_remediation(self):
        remediation = {
            "iterations": 1,
            "max_iterations": 3,
            "exhausted": False,
            "commits": [{"sha": "abc123", "file": "ruff.toml",
                         "fingerprint_after": "fp-1"}],
            "fingerprint_before": "fp-before",
            "fingerprint_after": "fp-1",
        }
        payload = audit_runner._build_issue_json(
            {"id": "TEST-1"}, [], [],
            code_quality_findings=[],
            remediation_results=remediation,
        )
        rem = payload["code_quality"]["remediation"]
        assert rem["iterations"] == 1
        assert rem["commits"][0]["sha"] == "abc123"
        assert rem["exhausted"] is False
