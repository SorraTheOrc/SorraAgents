#!/usr/bin/env python3
"""Tests for the small/low-risk Phase 2 deep-analysis skip (SA-0MSQ026T3009QY2L).

The audit runner skips Phase 2 deep code analysis when a work item (parent
or child, evaluated independently) has effort ∈ {Extra Small, Small} AND
risk = Low. Phase 1 verdicts stand unchanged with evidence noting the skip
reason. The rule is fail-closed: missing/unknown effort or risk ⇒ deep
analysis runs as usual.

Covers:
  - _is_low_risk_small() fail-closed semantics (AC1, AC2)
  - _annotate_skip_evidence() preserving verdicts (AC1)
  - _run_phase2_deep_analysis() child pending-list filtering (AC3)
  - report narrative via phase2_skip_note (AC1, AC5)
  - cmd_issue Phase 2 gate integration (AC1, AC2, AC4, AC5)
"""  # noqa: EXE001
from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

# ---------------------------------------------------------------------------
# Ensure repo root is on sys.path so the audit_runner module is importable.
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pytest

from skill.audit.scripts import audit_runner

_DESCRIPTION = (
    "## Acceptance Criteria\n"
    "- AC1: the runner skips Phase 2 for small, low-risk items\n"
    "- AC2: fail-closed on missing values\n"
)

_SKIP_NOTE = (
    "Phase 2 deep analysis skipped (effort=Small, risk=Low): small, "
    "low-risk item per SA-0MSQ026T3009QY2L. Phase 1 verdict stands."
)


@pytest.fixture(autouse=True)
def _free_audit_slot():
    """Neutralize the host-wide audit semaphore (see test_audit_runner.py)."""
    with mock.patch.object(
        audit_runner, "_acquire_audit_slot", return_value=contextlib.nullcontext()
    ):
        yield


# ===========================================================================
# _is_low_risk_small unit tests
# ===========================================================================


class TestIsLowRiskSmall:
    """Tests for the Phase 2 skip criterion helper (AC1, AC2)."""

    def test_extra_small_low_qualifies(self):
        """AC1: effort='Extra Small' + risk='Low' → skip."""
        assert audit_runner._is_low_risk_small("Extra Small", "Low") is True

    def test_small_low_qualifies(self):
        """AC1: effort='Small' + risk='Low' → skip."""
        assert audit_runner._is_low_risk_small("Small", "Low") is True

    def test_tshirt_code_forms_qualify(self):
        """The helper tolerates t-shirt-code forms (XS/S effort, L risk)."""
        assert audit_runner._is_low_risk_small("XS", "Low") is True
        assert audit_runner._is_low_risk_small("S", "L") is True

    def test_medium_effort_does_not_qualify(self):
        """effort must be XS or Small exactly — Medium does not qualify."""
        assert audit_runner._is_low_risk_small("Medium", "Low") is False

    def test_non_low_risk_does_not_qualify(self):
        """risk must be Low exactly — Medium/High do not qualify."""
        assert audit_runner._is_low_risk_small("Small", "Medium") is False
        assert audit_runner._is_low_risk_small("Small", "High") is False
        assert audit_runner._is_low_risk_small("Small", "Critical") is False

    def test_missing_values_fail_closed(self):
        """AC2: None effort/risk → deep analysis runs (never skip on absence)."""
        assert audit_runner._is_low_risk_small(None, "Low") is False
        assert audit_runner._is_low_risk_small("Small", None) is False
        assert audit_runner._is_low_risk_small(None, None) is False

    def test_empty_values_fail_closed(self):
        """AC2: empty-string effort/risk → deep analysis runs."""
        assert audit_runner._is_low_risk_small("", "Low") is False
        assert audit_runner._is_low_risk_small("Small", "") is False


# ===========================================================================
# _annotate_skip_evidence unit tests
# ===========================================================================


class TestAnnotateSkipEvidence:
    """AC1: Phase 1 verdicts stand unchanged; evidence notes the skip reason."""

    def test_verdicts_preserved_and_evidence_annotated(self):
        acs = [
            {"text": "AC1", "verdict": "met", "evidence": "phase1 evidence"},
            {"text": "AC2", "verdict": "partial", "evidence": ""},
        ]
        out = audit_runner._annotate_skip_evidence(acs, _SKIP_NOTE)

        assert out[0]["verdict"] == "met"
        assert out[1]["verdict"] == "partial"
        assert _SKIP_NOTE in out[0]["evidence"]
        assert "phase1 evidence" in out[0]["evidence"]
        # ACs without prior evidence still record the skip reason.
        assert out[1]["evidence"] == _SKIP_NOTE

    def test_original_list_not_mutated(self):
        acs = [{"text": "AC1", "verdict": "met", "evidence": "x"}]
        audit_runner._annotate_skip_evidence(acs, _SKIP_NOTE)
        assert acs[0]["evidence"] == "x"


# ===========================================================================
# _run_phase2_deep_analysis child pending-list filtering (AC3)
# ===========================================================================


class TestRunPhase2ChildSkip:
    """Children are evaluated independently against the skip criterion."""

    def _make_child(self, child_id, effort=None, risk=None, verdict="met"):
        return {
            "id": child_id,
            "title": f"Child {child_id}",
            "stage": "plan_complete",
            "status": "open",
            "effort": effort,
            "risk": risk,
            "ac_results": [
                {"text": "Child AC 1", "verdict": verdict, "evidence": "phase1"}
            ],
        }

    def _run(self, issue, acs, children, mock_call):
        mock_call.return_value = {"extracted_text": "[]"}
        return audit_runner._run_phase2_deep_analysis(
            issue, acs, children, "test-model",
        )

    def test_qualifying_child_dropped_no_pi_call(self):
        """AC3: a Small/Low child is dropped from Phase 2 — no phase2_child call."""
        issue = {"id": "PARENT-1", "title": "Parent"}
        acs = [{"text": "AC1", "verdict": "met", "evidence": ""}]
        child = self._make_child("CHILD-1", effort="Small", risk="Low")
        with mock.patch.object(audit_runner, "_call_pi_and_maybe_log") as mock_call:
            updated_acs, updated_children, _completed = self._run(
                issue, acs, [child], mock_call,
            )
        # No child deep-analysis call for the qualifying child.
        child_calls = [
            c for c in mock_call.call_args_list
            if c[0][0] == "CHILD-1"
        ]
        assert child_calls == []
        # Verdicts stand unchanged; evidence notes the skip.
        assert updated_children[0]["ac_results"][0]["verdict"] == "met"
        assert "Phase 2 deep analysis skipped" in updated_children[0]["ac_results"][0]["evidence"]
        assert updated_acs == acs

    def test_non_qualifying_child_still_deep_analyzed(self):
        """AC3: a Medium/Low child still gets Phase 2 deep analysis."""
        issue = {"id": "PARENT-1", "title": "Parent"}
        acs = [{"text": "AC1", "verdict": "met", "evidence": ""}]
        child = self._make_child("CHILD-1", effort="Medium", risk="Low")
        with mock.patch.object(audit_runner, "_call_pi_and_maybe_log") as mock_call:
            self._run(issue, acs, [child], mock_call)
        child_calls = [
            c for c in mock_call.call_args_list
            if c[0][0] == "CHILD-1"
        ]
        assert len(child_calls) >= 1

    def test_missing_risk_fail_closed_still_deep_analyzed(self):
        """AC2: missing risk on a child → deep analysis runs (fail-closed)."""
        issue = {"id": "PARENT-1", "title": "Parent"}
        acs = [{"text": "AC1", "verdict": "met", "evidence": ""}]
        child = self._make_child("CHILD-1", effort="Small", risk=None)
        with mock.patch.object(audit_runner, "_call_pi_and_maybe_log") as mock_call:
            self._run(issue, acs, [child], mock_call)
        child_calls = [
            c for c in mock_call.call_args_list
            if c[0][0] == "CHILD-1"
        ]
        assert len(child_calls) >= 1

    def test_mixed_children_independently_evaluated(self):
        """AC3: qualifying child skipped; non-qualifying child deep-analyzed."""
        issue = {"id": "PARENT-1", "title": "Parent"}
        acs = [{"text": "AC1", "verdict": "met", "evidence": ""}]
        small_child = self._make_child("CHILD-SMALL", effort="Small", risk="Low")
        medium_child = self._make_child("CHILD-MEDIUM", effort="Medium", risk="Low")
        with mock.patch.object(audit_runner, "_call_pi_and_maybe_log") as mock_call:
            _, updated_children, _ = self._run(
                issue, acs, [small_child, medium_child], mock_call,
            )
        child_ids_called = [
            c[0][0] for c in mock_call.call_args_list if c[0][0].startswith("CHILD-")
        ]
        assert "CHILD-SMALL" not in child_ids_called
        assert "CHILD-MEDIUM" in child_ids_called
        # The qualifying child's evidence records the skip.
        small_evidence = updated_children[0]["ac_results"][0]["evidence"]
        assert "Phase 2 deep analysis skipped" in small_evidence

    def test_skip_parent_deep_still_filters_children(self):
        """The child filter applies on the skip_parent_deep path too."""
        issue = {"id": "PARENT-1", "title": "Parent"}
        acs = [{"text": "AC1", "verdict": "met", "evidence": "parent-evidence"}]
        child = self._make_child("CHILD-1", effort="Small", risk="Low")
        with mock.patch.object(audit_runner, "_call_pi_and_maybe_log") as mock_call:
            mock_call.return_value = {"extracted_text": "[]"}
            updated_acs, updated_children, _completed = audit_runner._run_phase2_deep_analysis(
                issue, acs, [child], "test-model", skip_parent_deep=True,
            )
        # No pi calls at all (parent deep skipped, child dropped).
        assert mock_call.call_args_list == []
        # Parent ACs pass through with their prior (annotated) evidence.
        assert updated_acs[0]["evidence"] == "parent-evidence"
        assert updated_children[0]["ac_results"][0]["verdict"] == "met"
        assert "Phase 2 deep analysis skipped" in updated_children[0]["ac_results"][0]["evidence"]


# ===========================================================================
# Report narrative (phase2_skip_note)
# ===========================================================================


class TestReportSkipNarrative:
    """AC1/AC5: the report records the skip instead of claiming completion."""

    def test_assemble_report_emits_skip_note_not_completed(self):
        acs = [{"text": "AC1", "verdict": "met", "evidence": _SKIP_NOTE}]
        report = audit_runner._assemble_issue_report(
            {"id": "TEST-1", "title": "T"}, acs, [],
            phase2_completed=False,
            phase2_skip_note="small, low-risk item (effort=Small, risk=Low) per SA-0MSQ026T3009QY2L",
        )
        assert "Phase 2 deep analysis skipped" in report
        assert "small, low-risk item" in report
        assert "completed and confirmed all verdicts" not in report

    def test_assemble_report_omits_skip_note_when_none(self):
        acs = [{"text": "AC1", "verdict": "met", "evidence": ""}]
        report = audit_runner._assemble_issue_report(
            {"id": "TEST-1", "title": "T"}, acs, [],
            phase2_completed=True,
        )
        assert "completed and confirmed all verdicts" in report
        assert "Phase 2 deep analysis skipped" not in report

    def test_build_issue_json_summary_has_skip_note(self):
        acs = [{"text": "AC1", "verdict": "met", "evidence": _SKIP_NOTE}]
        payload = audit_runner._build_issue_json(
            {"id": "TEST-1", "title": "T"}, acs, [],
            phase2_completed=False,
            phase2_skip_note="small, low-risk item (effort=Small, risk=Low)",
        )
        assert "Phase 2 deep analysis skipped" in payload["summary"]
        assert payload["pipeline"]["phase2_completed"] is False


# ===========================================================================
# cmd_issue Phase 2 gate integration (parent-first default flow)
# ===========================================================================


class TestCmdIssuePhase2GateSkip:
    """The Phase 2 gate skips deep analysis for qualifying items only."""

    def _make_runner(self, updates, effort=None, risk=None):
        """Mock runner serving the wl command sequence for cmd_issue."""
        mock_runner = mock.MagicMock()

        def _side_effect(cmd):
            cmd_str = " ".join(cmd)
            if "update" in cmd_str:
                updates.append(list(cmd))
                return SimpleNamespace(
                    returncode=0, stdout=json.dumps({"success": True}), stderr="",
                )
            if "show" in cmd_str and "--children" not in cmd_str:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({
                        "success": True,
                        "workItem": {"id": "TEST-1", "status": "open",
                                     "stage": "plan_complete"},
                    }),
                    stderr="",
                )
            if "--children" in cmd_str:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({
                        "success": True,
                        "workItem": {
                            "id": "TEST-1",
                            "title": "Test",
                            "description": _DESCRIPTION,
                            "status": "open",
                            "stage": "plan_complete",
                            "effort": effort,
                            "risk": risk,
                        },
                        "children": [],
                    }),
                    stderr="",
                )
            return SimpleNamespace(
                returncode=0, stdout=json.dumps({"success": True}), stderr="",
            )

        mock_runner.side_effect = _side_effect
        return mock_runner

    def _run_issue(self, updates, effort, risk, mock_phase2, capsys):
        """Run cmd_issue through the real gate with a qualifying/non-qualifying item.

        *mock_phase2* replaces _run_phase2_deep_analysis so we can observe
        whether the parent deep call would have been attempted.
        """
        mock_runner = self._make_runner(updates, effort=effort, risk=risk)

        def _pi(issue_id, context, prompt, **kwargs):
            return {
                "extracted_text": json.dumps([
                    {"index": i, "verdict": "met", "evidence": "file.py:1"}
                    for i in range(2)
                ]),
                "elapsed_seconds": 0.1,
            }

        with (
            mock.patch.object(
                audit_runner, "_call_pi_and_maybe_log", side_effect=_pi,
            ),
            mock.patch.object(
                audit_runner, "_run_phase2_deep_analysis", side_effect=mock_phase2,
            ),
            mock.patch(
                "skill.code_review.scripts.code_quality.run_code_quality",
                return_value={"success": True, "findings": [], "fixes_applied": 0},
            ),
        ):
            rc = audit_runner.cmd_issue(
                "TEST-1", persist=False, force=True, runner=mock_runner,
            )
        return rc

    @staticmethod
    def _pass_through(issue, ac_results, child_results, **kwargs):
        return ac_results, child_results, True

    def test_qualifying_item_skips_parent_deep_analysis(self, capsys):
        """AC1/AC4/AC5: Small+Low → Phase 2 skipped unconditionally, verdicts stand."""
        updates = []
        with mock.patch.object(
            audit_runner, "_run_phase2_deep_analysis",
            side_effect=self._pass_through,
        ) as mock_phase2:
            rc = self._run_issue(updates, "Small", "Low", mock_phase2, capsys)
        assert rc == 0
        # Parent deep analysis is never attempted (no skip_parent_deep call).
        assert mock_phase2.call_args_list == []
        captured = capsys.readouterr()
        assert "Skipping Phase 2 deep analysis" in captured.err
        assert "effort=Small, risk=Low" in captured.err
        # Phase 1 verdicts stand: report still ready to close.
        assert "Ready to close: Yes" in captured.out
        assert "Phase 2 deep analysis skipped" in captured.out

    def test_non_qualifying_item_runs_deep_analysis(self, capsys):
        """AC2: Medium effort → deep analysis runs as today."""
        updates = []
        with mock.patch.object(
            audit_runner, "_run_phase2_deep_analysis",
            side_effect=self._pass_through,
        ) as mock_phase2:
            rc = self._run_issue(updates, "Medium", "Low", mock_phase2, capsys)
        assert rc == 0
        assert mock_phase2.call_args_list  # parent deep call attempted
        call_kwargs = mock_phase2.call_args_list[0].kwargs
        assert call_kwargs.get("skip_parent_deep") is not True
        captured = capsys.readouterr()
        assert "running Phase 2 deep code analysis" in captured.err
        assert "Skipping Phase 2 deep analysis" not in captured.err

    def test_missing_effort_fail_closed_runs_deep_analysis(self, capsys):
        """AC2: absent effort → deep analysis runs (fail-closed)."""
        updates = []
        with mock.patch.object(
            audit_runner, "_run_phase2_deep_analysis",
            side_effect=self._pass_through,
        ) as mock_phase2:
            rc = self._run_issue(updates, None, "Low", mock_phase2, capsys)
        assert rc == 0
        assert mock_phase2.call_args_list
        captured = capsys.readouterr()
        assert "Skipping Phase 2 deep analysis" not in captured.err
