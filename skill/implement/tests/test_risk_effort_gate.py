"""Tests for the implement skill's risk/effort evaluation gate.

When ``implement.py start`` is invoked on a ``plan_complete`` work item that
is missing ``risk`` and/or ``effort`` estimates, the gate must run the
effort-and-risk evaluation and persist the estimates rather than refusing to
proceed (SA-0MTTSWHQE0072N9J).

These tests exercise ``_check_and_evaluate_risk_effort`` directly with the
external boundaries (``subprocess.run`` for the orchestrator, ``run_cmd`` for
child lookup, ``os.path.isfile`` for the orchestrator path) mocked so no real
``wl``/estimation work is performed.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) in sys.path:
    sys.path.remove(str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT))

from skill.implement.scripts.implement import _check_and_evaluate_risk_effort

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeCompleted:
    """Minimal ``subprocess.CompletedProcess`` stand-in."""

    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _orchestrator_output(
    *,
    tshirt: str = "Medium",
    risk_level: str = "Medium",
    update_success: bool = True,
) -> str:
    """Build a realistic orchestrate_estimate.py stdout payload."""
    return json.dumps(
        {
            "effort": {"unit": "hours", "tshirt": tshirt},
            "risk": {"level": risk_level, "score": 4},
            "confidence_percent": 74,
            "update_result": {"success": update_success, "returncode": 0},
        }
    )


def _children_payload(*child_ids: str) -> str:
    return json.dumps(
        {
            "success": True,
            "workItem": {"id": "SA-TEST"},
            "children": [{"id": cid, "title": f"child {cid}"} for cid in child_ids],
        }
    )


def _run_evaluation(
    work_item: dict,
    *,
    orchestrator_stdout: str | None = None,
    orchestrator_returncode: int = 0,
    orchestrator_stderr: str = "",
    orchestrator_raises: Exception | None = None,
    orchestrator_exists: bool = True,
    children_payload: str | None = None,
    children_returncode: int = 0,
):
    """Invoke the gate with all external boundaries patched.

    Returns a ``(result, captured)`` tuple where *captured* exposes the call
    arguments for assertion (orchestrator stdin payload, children command).
    """
    captured: dict = {}

    def _fake_run_cmd(cmd, **_kwargs):
        captured["children_cmd"] = list(cmd)
        return _FakeCompleted(
            stdout=children_payload or _children_payload(),
            returncode=children_returncode,
        )

    def _fake_subprocess_run(cmd, input=None, **kwargs):
        captured["orchestrator_cmd"] = list(cmd)
        captured["orchestrator_payload"] = json.loads(input) if input else None
        captured["orchestrator_kwargs"] = kwargs
        if orchestrator_raises is not None:
            raise orchestrator_raises
        return _FakeCompleted(
            stdout=orchestrator_stdout
            or _orchestrator_output(),
            stderr=orchestrator_stderr,
            returncode=orchestrator_returncode,
        )

    with (
        patch(
            "skill.implement.scripts.implement.run_cmd",
            side_effect=_fake_run_cmd,
        ),
        patch(
            "skill.implement.scripts.implement.subprocess.run",
            side_effect=_fake_subprocess_run,
        ),
        patch(
            "skill.implement.scripts.implement.os.path.isfile",
            return_value=orchestrator_exists,
        ),
    ):
        result = _check_and_evaluate_risk_effort(
            work_item.get("id", "SA-TEST"), work_item,
        )

    return result, captured


# ---------------------------------------------------------------------------
# 1. Items already sized — no behaviour change (AC3)
# ---------------------------------------------------------------------------


class TestEstimatesPresent:
    """Items with both risk and effort must not trigger evaluation."""

    @pytest.mark.parametrize(
        "risk,effort",
        [
            ("Low", "Small"),
            ("Medium", "Medium"),
            ("High", "Large"),
        ],
    )
    def test_both_present_skips_evaluation(self, risk, effort):
        """Both fields set → no orchestrator run, item proceeds unchanged."""
        work_item = {"id": "SA-TEST", "stage": "plan_complete", "risk": risk, "effort": effort}
        result, captured = _run_evaluation(work_item)
        assert result["success"] is True
        assert result["estimates_provided"] is False
        assert result["reason"] == "estimates already present"
        assert "orchestrator_cmd" not in captured


# ---------------------------------------------------------------------------
# 2. Non plan_complete stages are not gated (no behaviour change)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stage", ["idea", "intake_complete", "in_review", "done", ""])
def test_non_plan_complete_stage_skips_evaluation(stage):
    """Only plan_complete items are evaluated; other stages pass through."""
    work_item = {"id": "SA-TEST", "stage": stage}
    result, captured = _run_evaluation(work_item)
    assert result["success"] is True
    assert result["estimates_provided"] is False
    assert result["reason"] == f"stage={stage}"
    assert "orchestrator_cmd" not in captured


# ---------------------------------------------------------------------------
# 3. Missing estimates → evaluation runs and persists (AC1, AC2)
# ---------------------------------------------------------------------------


class TestMissingEstimates:
    """plan_complete items missing risk/effort trigger the evaluation."""

    @pytest.mark.parametrize(
        "work_item",
        [
            {"id": "SA-TEST", "stage": "plan_complete"},
            {"id": "SA-TEST", "stage": "plan_complete", "risk": ""},
            {"id": "SA-TEST", "stage": "plan_complete", "effort": ""},
            {"id": "SA-TEST", "stage": "plan_complete", "risk": "", "effort": ""},
            {"id": "SA-TEST", "stage": "plan_complete", "risk": None, "effort": None},
            {"id": "SA-TEST", "stage": "plan_complete", "risk": "Low"},
            {"id": "SA-TEST", "stage": "plan_complete", "effort": "Small"},
        ],
    )
    def test_missing_estimates_runs_evaluation_and_reports_result(self, work_item):
        """A missing field triggers evaluation; the result reports the new estimates."""
        result, captured = _run_evaluation(
            work_item,
            orchestrator_stdout=_orchestrator_output(tshirt="Large", risk_level="High"),
        )
        assert result["success"] is True
        assert result["estimates_provided"] is True
        assert result["effort"] == "Large"
        assert result["risk"] == "High"
        # The orchestrator was invoked with the work-item id in its payload.
        assert captured["orchestrator_payload"]["issue_id"] == "SA-TEST"
        assert captured["orchestrator_cmd"][0] == "python3"
        assert captured["orchestrator_cmd"][1].endswith("orchestrate_estimate.py")

    def test_evaluation_includes_children_in_payload(self):
        """Child work items are forwarded to the estimator for a better estimate."""
        work_item = {"id": "SA-TEST", "stage": "plan_complete"}
        _result, captured = _run_evaluation(
            work_item,
            children_payload=_children_payload("SA-CHILD1", "SA-CHILD2"),
        )
        payload = captured["orchestrator_payload"]
        assert payload["issue_id"] == "SA-TEST"
        assert [c["id"] for c in payload["children"]] == ["SA-CHILD1", "SA-CHILD2"]

    def test_child_fetch_failure_still_evaluates(self):
        """A failed child lookup is non-fatal — evaluation proceeds with no children."""
        work_item = {"id": "SA-TEST", "stage": "plan_complete"}
        result, captured = _run_evaluation(
            work_item,
            children_returncode=1,
        )
        assert result["success"] is True
        assert result["estimates_provided"] is True
        assert captured["orchestrator_payload"]["children"] == []

    def test_critical_risk_maps_to_severe_wl_label(self):
        """A Critical risk level maps to wl's ``Severe`` risk label."""
        work_item = {"id": "SA-TEST", "stage": "plan_complete"}
        result, _captured = _run_evaluation(
            work_item,
            orchestrator_stdout=_orchestrator_output(risk_level="Critical"),
        )
        assert result["risk"] == "Severe"
        assert result["estimates_provided"] is True


# ---------------------------------------------------------------------------
# 4. Failure paths are non-blocking (AC1: proceed, don't fail the gate)
# ---------------------------------------------------------------------------


class TestFailurePaths:
    """Evaluation failures must not raise; they return success=False."""

    def test_orchestrator_not_found_returns_error(self):
        """A missing orchestrator script yields an explicit error result."""
        work_item = {"id": "SA-TEST", "stage": "plan_complete"}
        result, captured = _run_evaluation(work_item, orchestrator_exists=False)
        assert result["success"] is False
        assert result["estimates_provided"] is False
        assert result["error"] == "orchestrator not found"
        assert "orchestrator_cmd" not in captured

    def test_orchestrator_nonzero_exit_returns_error(self):
        """A non-zero orchestrator exit yields an error with stderr detail."""
        work_item = {"id": "SA-TEST", "stage": "plan_complete"}
        result, _captured = _run_evaluation(
            work_item,
            orchestrator_returncode=3,
            orchestrator_stderr="orchestrator exploded",
        )
        assert result["success"] is False
        assert result["estimates_provided"] is False
        assert "orchestrator failed" in result["error"]
        assert "orchestrator exploded" in result["error"]

    def test_update_failure_returns_error(self):
        """If the orchestrator cannot persist via wl update, report failure."""
        work_item = {"id": "SA-TEST", "stage": "plan_complete"}
        result, _captured = _run_evaluation(
            work_item,
            orchestrator_stdout=_orchestrator_output(update_success=False),
        )
        assert result["success"] is False
        assert result["estimates_provided"] is False
        assert "persist" in result["error"]

    def test_timeout_returns_error_and_is_non_blocking(self):
        """A timeout is caught and surfaced as an error, never propagated."""
        work_item = {"id": "SA-TEST", "stage": "plan_complete"}
        result, _captured = _run_evaluation(
            work_item,
            orchestrator_raises=subprocess.TimeoutExpired(cmd="python3", timeout=300),
        )
        assert result["success"] is False
        assert result["estimates_provided"] is False
        assert "timed out" in result["error"]

    def test_unexpected_exception_returns_error(self):
        """An unexpected exception is caught and surfaced as an error."""
        work_item = {"id": "SA-TEST", "stage": "plan_complete"}
        result, _captured = _run_evaluation(
            work_item,
            orchestrator_raises=RuntimeError("kaboom"),
        )
        assert result["success"] is False
        assert result["estimates_provided"] is False
        assert result["error"] == "kaboom"

    def test_malformed_orchestrator_json_returns_update_error(self):
        """Unparseable orchestrator stdout is handled (no crash) and reports failure."""
        work_item = {"id": "SA-TEST", "stage": "plan_complete"}
        result, _captured = _run_evaluation(
            work_item,
            orchestrator_stdout="not json at all",
        )
        assert result["success"] is False
        assert result["estimates_provided"] is False


# ---------------------------------------------------------------------------
# 5. ContextHub dispatcher compatibility (AC2)
# ---------------------------------------------------------------------------


def _is_implement_dispatchable(item: dict) -> bool:
    """Mirror the ContextHub dispatcher gate.

    The dispatcher only dispatches items whose risk and effort are present and
    no larger than Medium; it fails closed when either is absent.
    """
    risk_order = ["Low", "Medium", "High", "Severe"]
    effort_order = ["Extra Small", "Small", "Medium", "Large", "Extra Large"]
    risk = item.get("risk") or ""
    effort = item.get("effort") or ""
    if not risk or not effort:
        return False
    if risk not in risk_order or effort not in effort_order:
        return False
    return (
        risk_order.index(risk) <= risk_order.index("Medium")
        and effort_order.index(effort) <= effort_order.index("Medium")
    )


class TestContextHubDispatchability:
    """After the gate, a previously-unsized item satisfies the dispatcher rule."""

    def test_unsized_item_becomes_dispatchable_after_gate(self):
        """An item shaped like OSL-0MSG7XUY0009OTVA (plan_complete, empty
        risk/effort) is not dispatchable before the gate and is dispatchable
        after the gate persists estimates via ``wl update``."""
        # Fixture mirrors the open_source_llm evidence item shape.
        item = {
            "id": "SA-OSL-SHAPE",
            "stage": "plan_complete",
            "status": "in_progress",
            "risk": "",
            "effort": "",
        }
        assert _is_implement_dispatchable(item) is False

        result, captured = _run_evaluation(
            item,
            orchestrator_stdout=_orchestrator_output(
                tshirt="Medium", risk_level="Low",
            ),
        )
        assert result["success"] is True
        assert result["estimates_provided"] is True

        # Simulate the persistence the real orchestrator performs internally
        # via ``wl update`` — the gate only returns after that write succeeds,
        # so the item now carries the estimates the dispatcher requires.
        assert captured["orchestrator_payload"]["issue_id"] == "SA-OSL-SHAPE"
        item["risk"] = result["risk"]
        item["effort"] = result["effort"]
        assert _is_implement_dispatchable(item) is True

    def test_high_effort_item_remains_non_dispatchable(self):
        """The gate does not silently shrink a genuinely large item — a Large
        estimate is persisted honestly and stays above the dispatch threshold."""
        item = {
            "id": "SA-BIG",
            "stage": "plan_complete",
            "risk": "",
            "effort": "",
        }
        result, _captured = _run_evaluation(
            item,
            orchestrator_stdout=_orchestrator_output(
                tshirt="Extra Large", risk_level="High",
            ),
        )
        assert result["success"] is True
        assert result["estimates_provided"] is True
        item["risk"] = result["risk"]
        item["effort"] = result["effort"]
        # Persisted, but genuinely over the dispatcher threshold.
        assert _is_implement_dispatchable(item) is False


# ---------------------------------------------------------------------------
# 6. phase_start wiring — the gate runs and never blocks the run
# ---------------------------------------------------------------------------


class TestPhaseStartIntegration:
    """phase_start invokes the gate and records its result.

    These tests guard the wiring (Step 6.1) itself: the gate must run for a
    plan_complete item, its result must be recorded in the report without a
    KeyError, and neither a provided-estimates result nor a failed evaluation
    may abort the start.
    """

    @staticmethod
    def _run_phase_start(work_item, gate_result, tmp_path):
        import skill.implement.scripts.implement as impl

        with (
            patch.object(impl, "is_code_freeze_active", return_value=False),
            patch.object(impl, "StatusLifecycle"),
            patch.object(impl, "git_status", return_value=""),
            patch.object(impl, "git_has_dirty_files", return_value=False),
            patch.object(
                impl,
                "check_orphaned_stashes",
                return_value={
                    "has_orphaned": False,
                    "total_stashes": 0,
                    "orphaned_stashes": [],
                    "warning": "",
                },
            ),
            patch.object(impl, "wl_show", return_value=work_item),
            patch.object(
                impl, "_check_and_evaluate_risk_effort", return_value=gate_result,
            ) as mock_gate,
            patch.object(impl, "_get_repo_root", return_value=str(tmp_path)),
            patch.object(
                impl, "_sync_parent_branch", return_value={"success": True},
            ),
            patch.object(
                impl, "worktree_path_for", return_value=str(tmp_path / "wt"),
            ),
            patch.object(impl, "git_worktree_add", return_value=True),
            patch.object(impl, "_ensure_node_modules_symlink"),
            patch.object(impl, "_ensure_submodules"),
            patch.object(impl, "wl_add_comment", return_value=True),
            patch.object(impl, "_store_signal_globals"),
            patch.object(impl, "_register_signal_handlers"),
            patch.object(impl, "write_state"),
        ):
            report = impl.phase_start("SA-TEST", json_output=True)
        return report, mock_gate

    def test_gate_runs_for_plan_complete_item_and_is_recorded(self, tmp_path):
        """The gate is invoked and its result appears in the report."""
        work_item = {"id": "SA-TEST", "title": "A task", "stage": "plan_complete"}
        gate_result = {
            "success": True,
            "estimates_provided": True,
            "risk": "Low",
            "effort": "Small",
        }
        report, mock_gate = self._run_phase_start(work_item, gate_result, tmp_path)
        mock_gate.assert_called_once_with("SA-TEST", work_item)
        assert report["success"] is True
        assert report["steps"]["risk_effort"] == gate_result

    def test_failed_evaluation_does_not_abort_start(self, tmp_path):
        """A failed evaluation is logged, not fatal — the run proceeds."""
        work_item = {"id": "SA-TEST", "title": "A task", "stage": "plan_complete"}
        gate_result = {
            "success": False,
            "estimates_provided": False,
            "error": "orchestrator failed: boom",
        }
        report, _mock_gate = self._run_phase_start(work_item, gate_result, tmp_path)
        assert report["success"] is True
        assert report["steps"]["risk_effort"]["success"] is False
        assert report["worktree_path"]

