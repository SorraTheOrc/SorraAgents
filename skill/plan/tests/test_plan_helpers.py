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


import json

import pytest

from skill.plan.plan_helpers import (
    plan_approval_gate,
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
        assert cmd[:2] == ["wl", "show"]
        assert "--json" in cmd
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

        class _FailingRunner:
            def __call__(self, cmd):
                return _FakeResult("{}", returncode=1, stderr="boom")

        result = plan_approval_gate("X", runner=_FailingRunner())
        assert result["request_approval"] is True
        assert result["reason"]  # conservative default is explained
