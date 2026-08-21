"""Chore work-item creation for config fixes + tracking (SA-0MST01PQQ009T0CI).

F3 implementation tests:
- AC1: a `chore` work item is created per applied config fix via
  `_run_wl` (full worklog resolution); its description links the
  false-positive finding (file, rule, justification) and the commit sha.
- AC2: a `chore` is created for each medium/low confident-false-positive
  finding — no config change, no commit link, annotated with
  FP_CHORE_ANNOTATION ("candidate false positive — producer decision
  required").
- AC3: no chore for `uncertain` / `genuine` findings.
- AC5: fail-safe — a `wl create` failure never reverts the remediation
  commit; the affected finding stays blocking `genuine` (chore_failed)
  and the failure is recorded in the report.
- Report + `_build_issue_json` surface chore items and failures.
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
from audit.scripts import audit_runner


@pytest.fixture(autouse=True)
def _free_audit_slot():
    with mock.patch.object(
        audit_runner, "_acquire_audit_slot", return_value=contextlib.nullcontext()
    ):
        yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _runner(create_succeeds: bool = True) -> mock.MagicMock:
    """Runner whose `wl create` succeeds (returning CHORE-1) or fails."""
    runner = mock.MagicMock()

    def _side(cmd, **kwargs):
        cmd_str = " ".join(cmd)
        if cmd_str.startswith("git rev-parse"):
            return SimpleNamespace(returncode=0, stdout="abc1234\n", stderr="")
        if " create " in cmd_str or cmd_str.endswith(" create"):
            if not create_succeeds:
                return SimpleNamespace(returncode=1, stdout="", stderr="boom")
            return SimpleNamespace(returncode=0, stdout=json.dumps({
                "success": True,
                "workItem": {"id": "CHORE-1", "status": "open"},
            }), stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    runner.side_effect = _side
    return runner


def _run_loop(tmp_path, cq_findings, fp_screen_results, runner=None,
              screen_returns=None):
    """Drive _run_remediation_loop; the screen mock returns *screen_returns*
    (single entry or list; default: nothing left after the scan)."""
    runner = runner or _runner()
    screen_return = screen_returns if screen_returns is not None else []
    if isinstance(screen_return, dict):
        screen_return = [screen_return]
    with (
        mock.patch.object(audit_runner, "_git_changed_files",
                          return_value=["src/bad.py"]),
        mock.patch(
            "code_review.scripts.code_quality.run_code_quality",
            return_value={"success": True, "findings": [],
                          "fixes_applied": 0},
        ),
        mock.patch.object(audit_runner, "_screen_ruff_findings",
                          return_value=screen_return),
    ):
        return audit_runner._run_remediation_loop(
            "TEST-1", cq_findings, fp_screen_results, runner, "pi", "m",
            None, None, mock.Mock(), tmp_path, "TEST-WORKLOG",
            {"id": "TEST-1"}, "fp-before",
        )


def _create_commands(runner) -> list[list[str]]:
    return [
        list(c.args[0]) for c in runner.call_args_list
        if c.args and (" create " in " ".join(c.args[0])
                       or " ".join(c.args[0]).endswith(" create"))
    ]


# ---------------------------------------------------------------------------
# AC1: per-config-fix chore linking finding + commit
# ---------------------------------------------------------------------------


class TestConfigFixChore:
    def test_per_iteration_chore_created_with_commit_link(self, tmp_path):
        """AC1: each applied config fix gets a chore linking the finding
        and the local commit sha, dispatched through _run_wl with the
        worklog dir resolved."""
        finding = _finding(severity="critical", code="F401")
        entry = _screen_entry(finding, classification="confident-false-positive",
                              remediable=True, justification="rule misfires here")
        runner = _runner()
        results = _run_loop(tmp_path, [finding], [entry], runner=runner)
        assert results["iterations"] == 1
        assert results["chore_items"] == [{
            "id": "CHORE-1", "commit_sha": "abc1234",
        }]
        assert results["chore_failures"] == []
        cmds = _create_commands(runner)
        assert len(cmds) == 1
        dispatched = " ".join(cmds[0])
        # Full worklog resolution + chore shape + finding + commit refs.
        assert "--worklog-dir TEST-WORKLOG" in dispatched
        assert "--issue-type chore" in dispatched
        assert "F401" in dispatched
        assert "src/bad.py" in dispatched
        assert "rule misfires here" in dispatched
        assert "config commit abc1234" in dispatched

    def test_no_chore_when_no_remediation(self, tmp_path):
        """No config fix → no per-fix chore (AC3: nothing to track)."""
        finding = _finding(severity="critical", code="F401")
        entry = _screen_entry(finding, classification="genuine", remediable=False)
        runner = _runner()
        results = _run_loop(tmp_path, [finding], [entry], runner=runner)
        assert results["iterations"] == 0
        assert results["commits"] == []
        assert results["chore_items"] == []
        assert _create_commands(runner) == []


# ---------------------------------------------------------------------------
# AC2/AC3: medium/low CFP tracking chores; never for uncertain/genuine
# ---------------------------------------------------------------------------


class TestTrackingChores:
    def test_medium_cfp_tracking_chore_no_commit_link(self, tmp_path):
        """AC2: a medium CFP finding gets a tracking chore with the
        producer-decision annotation and NO commit link."""
        finding = _finding(severity="medium", code="F841")
        entry = _screen_entry(finding, classification="confident-false-positive",
                              remediable=False, justification="misfires")
        runner = _runner()
        results = _run_loop(tmp_path, [finding], [entry], runner=runner)
        assert results["iterations"] == 0
        assert results["commits"] == []
        assert results["chore_items"] == [{"id": "CHORE-1"}]
        assert "commit_sha" not in results["chore_items"][0]
        cmds = _create_commands(runner)
        assert len(cmds) == 1
        dispatched = " ".join(cmds[0])
        assert audit_runner.FP_CHORE_ANNOTATION in dispatched
        assert "config commit" not in dispatched

    def test_uncertain_never_gets_chore(self, tmp_path):
        """AC3: uncertain findings never get a work item."""
        finding = _finding(severity="critical", code="E402")
        entry = _screen_entry(finding, classification="uncertain", remediable=False)
        runner = _runner()
        results = _run_loop(tmp_path, [finding], [entry], runner=runner)
        assert results["chore_items"] == []
        assert _create_commands(runner) == []

    def test_genuine_never_gets_chore(self, tmp_path):
        """AC3: genuine findings never get a work item."""
        finding = _finding(severity="critical", code="F401")
        entry = _screen_entry(finding, classification="genuine", remediable=False)
        runner = _runner()
        results = _run_loop(tmp_path, [finding], [entry], runner=runner)
        assert results["chore_items"] == []
        assert _create_commands(runner) == []


# ---------------------------------------------------------------------------
# AC5: fail-safe chore creation failure
# ---------------------------------------------------------------------------


class TestFailSafe:
    def test_chore_failure_keeps_commit_and_blocks_genuine(self, tmp_path):
        """AC5: a `wl create` failure never reverts the remediation commit;
        the finding stays blocking 'genuine' (chore_failed) and the failure
        is recorded."""
        finding = _finding(severity="critical", code="F401")
        entry = _screen_entry(finding, classification="confident-false-positive",
                              remediable=True, justification="misfires")
        runner = _runner(create_succeeds=False)
        results = _run_loop(tmp_path, [finding], [entry], runner=runner)
        # The commit still stands (the fix was applied and committed).
        assert len(results["commits"]) == 1
        assert results["commits"][0]["sha"] == "abc1234"
        # The failure is recorded.
        assert len(results["chore_failures"]) == 1
        assert "change" in results["chore_failures"][0]
        assert results["chore_items"] == []

    def test_chore_failure_demotes_persisting_finding_to_genuine(self, tmp_path):
        """AC5: when a config-fix chore fails and the finding persists (the
        loop stops without exhaustion — e.g. the re-screen no longer flags
        it remediable), the finding is demoted to blocking 'genuine' with
        chore_failed=True — never silently suppressed."""
        finding = _finding(severity="critical", code="F401")
        entry = _screen_entry(finding, classification="confident-false-positive",
                              remediable=True, justification="misfires")
        uncertain_entry = _screen_entry(
            finding, classification="uncertain", remediable=False,
            justification="re-screen uncertain")
        runner = _runner(create_succeeds=False)
        # Iteration 1: chore fails. Re-scan surfaces the finding again but
        # the screen now classifies it uncertain (non-remediable) so the
        # loop stops without cap exhaustion.
        results = _run_loop(tmp_path, [finding], [entry], runner=runner,
                            screen_returns=uncertain_entry)
        assert results["chore_failures"]
        assert len(results["commits"]) == 1  # the commit stands
        # The persisting finding was demoted to blocking genuine.
        assert any(e.get("chore_failed") for e in results["fp_screen_results"])
        assert any(
            e.get("classification") == "genuine"
            for e in results["fp_screen_results"]
        )
        assert any(
            "finding remains blocking genuine" in e.get("justification", "")
            for e in results["fp_screen_results"]
        )


# ---------------------------------------------------------------------------
# Report + JSON surfacing
# ---------------------------------------------------------------------------


class TestSurfacing:
    def test_report_lists_chore_items_and_failures(self):
        """The audit report's Remediation loop section lists created chore
        items and recorded failures."""
        remediation = {
            "iterations": 1,
            "max_iterations": 3,
            "exhausted": False,
            "commits": [{"sha": "abc1234", "file": "ruff.toml",
                         "fingerprint_after": "fp-1"}],
            "chore_items": [
                {"id": "CHORE-1", "commit_sha": "abc1234"},
                {"id": "CHORE-2"},
            ],
            "chore_failures": [{
                "change": "src/bad.py -> F401",
                "error": "wl create failed",
            }],
            "fingerprint_before": "fp-0",
            "fingerprint_after": "fp-1",
            "cq_findings": [],
            "fp_screen_results": [],
        }
        report = audit_runner._assemble_issue_report(
            {"id": "TEST-1"}, [], [],
            code_quality_findings=[],
            remediation_results=remediation,
            model="test-model",
        )
        assert "Chore work item CHORE-1 tracks the config fix (commit abc1234)" in report
        assert audit_runner.FP_CHORE_ANNOTATION in report
        assert "Chore tracking failed" in report
        assert "src/bad.py -> F401" in report
        assert "the commit stands, the finding stays blocking 'genuine'" in report

    def test_json_includes_chore_items_and_failures(self):
        """_build_issue_json surfaces the remediation dict including the
        chore items/failures."""
        remediation = {
            "iterations": 1,
            "max_iterations": 3,
            "exhausted": False,
            "commits": [],
            "chore_items": [{"id": "CHORE-1", "commit_sha": "abc1234"}],
            "chore_failures": [{"error": "wl create failed",
                                "finding": _finding(severity="medium")}],
            "cq_findings": [],
            "fp_screen_results": [],
        }
        payload = audit_runner._build_issue_json(
            {"id": "TEST-1"}, [], [],
            code_quality_findings=[],
            remediation_results=remediation,
        )
        rem = payload["code_quality"]["remediation"]
        assert rem["chore_items"] == [{"id": "CHORE-1", "commit_sha": "abc1234"}]
        assert rem["chore_failures"][0]["error"] == "wl create failed"


# ---------------------------------------------------------------------------
# _create_chore_item unit paths
# ---------------------------------------------------------------------------


class TestCreateChoreItemUnit:
    def test_returns_id_and_resolves_worklog(self):
        """_create_chore_item dispatches wl create with the worklog dir and
        returns the new id."""
        runner = _runner()
        cid = audit_runner._create_chore_item(
            runner, title="t", description="d", worklog_dir="/owning/.worklog")
        assert cid == "CHORE-1"
        dispatched = " ".join(runner.call_args_list[0].args[0])
        assert "--worklog-dir /owning/.worklog" in dispatched
        assert "--issue-type chore" in dispatched

    def test_failure_returns_none(self):
        """A failing wl create returns None (caller handles fail-safely)."""
        runner = _runner(create_succeeds=False)
        assert audit_runner._create_chore_item(
            runner, title="t", description="d", worklog_dir=None) is None

    def test_non_success_response_returns_none(self):
        """A wl create reporting success=False returns None."""
        runner = mock.MagicMock()
        runner.side_effect = lambda cmd, **kw: SimpleNamespace(
            returncode=0, stdout=json.dumps({"success": False,
                                             "error": "boom"}), stderr="")
        assert audit_runner._create_chore_item(
            runner, title="t", description="d", worklog_dir=None) is None
