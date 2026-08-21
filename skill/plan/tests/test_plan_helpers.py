"""Tests for the plan-approval gate helpers in skill/plan/plan_helpers.py.

The plan skill's step 4 asks the user to approve a proposed feature plan.
Approval is requested only when the work item's effort t-shirt size is
Medium/Large/Extra Large ("scale") OR its risk level is Medium/High.
When effort is Extra Small/Small AND risk is Low, the plan proceeds
directly to the automated review stages without an approval pause.

Missing effort/risk values default conservatively to requesting approval
(mirroring ``resolve_complexity_tier``'s Medium default) so a human
checkpoint is never silently skipped.

Related work item: SA-0MSHID94D009P0TL
"""


import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
# The repo root must stay ahead of the skills root so top-level `plan`
# resolves to the ROOT plan/ package (see tests/test_plan_package_resolution.py).
# plan_helpers.py applies its own skills-root bootstrap internally, so only the
# repo root is needed here.
if str(REPO_ROOT) in sys.path:
    sys.path.remove(str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT))
import json
from unittest.mock import patch

import pytest

from skill.plan.plan_helpers import (
    make_autoplan_decision,
    plan_approval_gate,
    plan_if_needed,
    should_request_plan_approval,
)

# =========================================================================
# 1. should_request_plan_approval — pure decision logic
# =========================================================================


class TestShouldRequestPlanApproval:
    """Verify the approval-gate decision across effort/risk combinations."""

    @pytest.mark.parametrize(
        "effort,risk",
        [
            ("Extra Small", "Low"),
            ("Small", "Low"),
        ],
    )
    def test_low_effort_low_risk_skips_approval(self, effort, risk):
        """Extra Small/Small effort with Low risk proceeds without approval."""
        request, reason = should_request_plan_approval({"effort": effort, "risk": risk})
        assert request is False
        assert reason == ""

    @pytest.mark.parametrize(
        "effort",
        ["Medium", "Large", "Extra Large"],
    )
    def test_medium_or_higher_effort_requests_approval(self, effort):
        """Medium/Large/Extra Large effort requests approval even with Low risk."""
        request, reason = should_request_plan_approval({"effort": effort, "risk": "Low"})
        assert request is True
        assert effort in reason

    @pytest.mark.parametrize(
        "risk",
        ["Medium", "High"],
    )
    def test_medium_or_high_risk_requests_approval(self, risk):
        """Medium/High risk requests approval even with Extra Small effort."""
        request, reason = should_request_plan_approval(
            {"effort": "Extra Small", "risk": risk}
        )
        assert request is True
        assert risk in reason

    def test_high_effort_and_high_risk_lists_both_reasons(self):
        """The reason names both the scale and the risk that triggered the gate."""
        request, reason = should_request_plan_approval(
            {"effort": "Large", "risk": "High"}
        )
        assert request is True
        assert "Large" in reason
        assert "High" in reason

    @pytest.mark.parametrize(
        "item",
        [
            {},
            {"effort": ""},
            {"risk": ""},
            {"effort": "", "risk": ""},
            {"effort": None, "risk": None},
            {"effort": "Small"},
            {"risk": "Low"},
            {"effort": "", "risk": "Low"},
            {"effort": "Small", "risk": ""},
        ],
    )
    def test_missing_values_default_to_approval(self, item):
        """Absent effort/risk values default conservatively to requesting approval."""
        request, reason = should_request_plan_approval(item)
        assert request is True
        assert reason  # the reason must explain the conservative default


# =========================================================================
# 2. plan_approval_gate — CLI helper (fetches the work item)
# =========================================================================


class _FakeResult:
    """Minimal stand-in for subprocess.CompletedProcess."""

    def __init__(self, stdout: str, returncode: int = 0, stderr: str = ""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


class _FakeWlShow:
    """Runner that answers ``wl show <id> --json`` with a fixed work item."""

    def __init__(self, work_item: dict):
        self._work_item = work_item

    def __call__(self, cmd):
        assert cmd[0] == "wl"
        assert "show" in cmd
        assert "--json" in cmd
        # The command may carry resolved --worklog-dir flags after "wl"
        payload = json.dumps({"success": True, "workItem": self._work_item})
        return _FakeResult(payload)


class TestPlanApprovalGate:
    """Verify the CLI helper fetches the item and delegates to the gate."""

    def test_skip_when_small_and_low_risk(self):
        """Extra Small + Low risk yields request_approval=False."""
        runner = _FakeWlShow({"id": "X", "effort": "Extra Small", "risk": "Low"})
        result = plan_approval_gate("X", runner=runner)
        assert result["target_id"] == "X"
        assert result["request_approval"] is False
        assert result["reason"] == ""

    def test_approval_when_effort_scale_is_high(self):
        """Large + Low risk yields request_approval=True with scale in reason."""
        runner = _FakeWlShow({"id": "X", "effort": "Large", "risk": "Low"})
        result = plan_approval_gate("X", runner=runner)
        assert result["request_approval"] is True
        assert "Large" in result["reason"]

    def test_approval_when_risk_is_high(self):
        """Extra Small + High risk yields request_approval=True with risk in reason."""
        runner = _FakeWlShow({"id": "X", "effort": "Extra Small", "risk": "High"})
        result = plan_approval_gate("X", runner=runner)
        assert result["request_approval"] is True
        assert "High" in result["reason"]

    def test_approval_when_values_absent(self):
        """Absent effort/risk default conservatively to requesting approval."""
        runner = _FakeWlShow({"id": "X"})
        result = plan_approval_gate("X", runner=runner)
        assert result["request_approval"] is True
        assert "not set" in result["reason"]

    def test_failed_fetch_defaults_to_approval(self):
        """A failed ``wl show`` defaults conservatively to requesting approval."""
        result = plan_approval_gate("X", runner=_FailingRunner())
        assert result["request_approval"] is True
        assert result["reason"]  # conservative default is explained


# =========================================================================
# 3. Fetch-failure safety (regression for SA-0MSIUJQAK008AQV8)
#
# The autoplan decision must NEVER invoke the effort-and-risk orchestration
# (which WRITES effort/risk fields and posts a comment) when the work item
# could not be read. Previously a failing ``wl show`` returned ``{}`` and
# make_autoplan_decision proceeded with a zeroed placeholder payload,
# overwriting genuine estimates.
# =========================================================================


class _FailingRunner:
    """Runner that answers every wl command with a failure."""

    def __call__(self, cmd):
        return _FakeResult("{}", returncode=1, stderr="wl show failed")


class TestMakeAutoplanDecisionFetchFailure:
    """A failed work-item fetch must yield an explicit error and no writes."""

    def test_fetch_failure_returns_explicit_error_and_skips_effort_and_risk(self):
        """A failing ``wl show`` yields an explicit error result; the effort-and-risk
        orchestration and comment posting are never invoked."""
        runner = _FailingRunner()
        with (
            patch("skill.plan.plan_helpers.run_effort_and_risk") as mock_er,
            patch("skill.plan.plan_helpers.append_autoplan_decision_comment") as mock_append,
        ):
            do_plan, stage, effort_risk = make_autoplan_decision(
                "SA-TEST", config={}, runner=runner
            )
            mock_er.assert_not_called()
            mock_append.assert_not_called()
        assert stage == "error"
        assert do_plan is True  # safety-first: default to planning
        assert effort_risk is not None
        assert "error" in effort_risk
        assert "SA-TEST" in effort_risk["error"]

    def test_plan_if_needed_fetch_failure_returns_error_decision(self):
        """plan-if-needed with a failing ``wl show`` returns decision=error and
        never invokes the effort-and-risk orchestration (no writes, no comments)."""
        runner = _FailingRunner()
        with (
            patch("skill.plan.plan_helpers.run_effort_and_risk") as mock_er,
            patch("skill.plan.plan_helpers.append_autoplan_decision_comment") as mock_append,
        ):
            result = plan_if_needed("SA-TEST", runner=runner)
            mock_er.assert_not_called()
            mock_append.assert_not_called()
        assert result["target_id"] == "SA-TEST"
        assert result["decision"] == "error"
        assert "error" in result

    def test_genuine_estimate_never_overwritten_by_placeholder(self):
        """A work item with a genuine estimate (Small/Medium) is left untouched:
        the zeroed autoplan placeholder orchestration is never run."""
        runner = _FakeWlShow({"id": "SA-TEST", "effort": "Small", "risk": "Medium"})
        with (
            patch("skill.plan.plan_helpers.run_effort_and_risk") as mock_er,
            patch("skill.plan.plan_helpers._wl_comment_list", return_value=[]),
        ):
            _do_plan, _stage, effort_risk = make_autoplan_decision(
                "SA-TEST", config={}, runner=runner
            )
            mock_er.assert_not_called()
        assert effort_risk == {"effort": "Small", "risk": "Medium"}


class TestWlSubprocessWorklogFlags:
    """wl subprocess calls must carry resolved --worklog-dir flags so they
    succeed from any cwd (parity with orchestrate_estimate.py)."""

    def test_wl_show_includes_resolved_worklog_flags(self):
        """The wl show command includes the flags returned by resolve_worklog_flags."""
        captured = []

        class _RecordingRunner:
            def __call__(self, cmd):
                captured.append(list(cmd))
                return _FakeResult(
                    json.dumps({"success": True, "workItem": {"id": "SA-TEST"}})
                )

        with patch(
            "skill.plan.plan_helpers.resolve_worklog_flags",
            return_value=["--worklog-dir", "/some/wl"],
        ) as mock_flags:
            result = plan_approval_gate("SA-TEST", runner=_RecordingRunner())
        assert result["target_id"] == "SA-TEST"
        assert len(captured) == 1
        show_cmd = captured[0]
        assert show_cmd[0] == "wl"
        assert show_cmd[1:3] == ["--worklog-dir", "/some/wl"]
        assert "show" in show_cmd
        assert "--json" in show_cmd
        mock_flags.assert_called_once()

    def test_plan_if_needed_cli_output_uses_real_effort_and_risk(self):
        """plan-if-needed reports the work item's effort t-shirt and risk level
        (not the stage mislabeled as effort, and not the boolean decision)."""
        with patch(
            "skill.plan.plan_helpers.make_autoplan_decision",
            return_value=(
                False,
                "intake_complete",
                {"effort": "Small", "risk": "Low"},
            ),
        ):
            result = plan_if_needed("SA-TEST")
        assert result["decision"] == "skip"
        assert result["effort"] == "Small"
        assert result["risk"] == "Low"

    def test_plan_if_needed_error_result_maps_to_error_decision(self):
        """An error tuple from make_autoplan_decision surfaces as decision=error."""
        with patch(
            "skill.plan.plan_helpers.make_autoplan_decision",
            return_value=(
                True,
                "error",
                {"error": "could not fetch work item SA-TEST"},
            ),
        ):
            result = plan_if_needed("SA-TEST")
        assert result["decision"] == "error"
        assert "could not fetch" in result["error"]
