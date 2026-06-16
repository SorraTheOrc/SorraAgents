"""Tests for the shared autoplan decision module (command/plan_helpers.py).

This test file is written before the implementation module exists (TDD).
Once command/plan_helpers.py is implemented, these tests should all pass.
"""

import json
import subprocess
from unittest.mock import ANY, MagicMock, call, patch

import pytest

# The module under test does not exist yet — these imports will fail until
# command/plan_helpers.py is implemented in the sibling work item
# SA-0MQH8CA8K008GM1L (Implement: Shared Decision Module).
from command.plan_helpers import (
    DEFAULT_AUTOPLAN_EFFORT_SKIP,
    DEFAULT_AUTOPLAN_RISK_SKIP,
    append_autoplan_decision_comment,
    check_effort_risk,
    is_effort_risk_computed,
    make_autoplan_decision,
    plan_if_needed,
    resolve_complexity_tier,
    run_effort_and_risk,
)


# =========================================================================
# 1. Default threshold constants
# =========================================================================


class TestDefaultThresholds:
    """Verify the default autoplan thresholds."""

    def test_effort_skip_defaults(self):
        assert DEFAULT_AUTOPLAN_EFFORT_SKIP == frozenset({"Extra Small", "Small"})

    def test_risk_skip_defaults(self):
        assert DEFAULT_AUTOPLAN_RISK_SKIP == frozenset({"Low"})


# =========================================================================
# 2. Complexity tier resolution  (resolve_complexity_tier)
# =========================================================================


class TestResolveComplexityTier:
    """Verify complexity tier resolution logic (extracted from ralph_loop._resolve_complexity_tier)."""

    def test_low_xs_low(self):
        """Extra Small + Low  -> low."""
        tier = resolve_complexity_tier({"effort": "Extra Small", "risk": "Low"}, {})
        assert tier == "low"

    def test_low_small_low(self):
        """Small + Low  -> low."""
        tier = resolve_complexity_tier({"effort": "Small", "risk": "Low"}, {})
        assert tier == "low"

    def test_medium_medium_medium(self):
        """Medium + Medium  -> medium."""
        tier = resolve_complexity_tier({"effort": "Medium", "risk": "Medium"}, {})
        assert tier == "medium"

    def test_high_large_high(self):
        """Large + High  -> high."""
        tier = resolve_complexity_tier({"effort": "Large", "risk": "High"}, {})
        assert tier == "high"

    def test_high_xl_high(self):
        """Extra Large + High  -> high."""
        tier = resolve_complexity_tier({"effort": "Extra Large", "risk": "High"}, {})
        assert tier == "high"

    def test_high_medium_large(self):
        """Large effort + Medium risk  -> high (effort triggers high)."""
        tier = resolve_complexity_tier({"effort": "Large", "risk": "Medium"}, {})
        assert tier == "high"

    def test_high_small_high_risk(self):
        """Small effort + High risk  -> high (risk triggers high)."""
        tier = resolve_complexity_tier({"effort": "Small", "risk": "High"}, {})
        assert tier == "high"

    def test_medium_medium_low_risk(self):
        """Medium + Low  -> medium (medium triggers medium even with low risk)."""
        tier = resolve_complexity_tier({"effort": "Medium", "risk": "Low"}, {})
        assert tier == "medium"

    def test_medium_low_effort_medium_risk(self):
        """Extra Small + Medium risk  -> medium (risk triggers medium)."""
        tier = resolve_complexity_tier({"effort": "Extra Small", "risk": "Medium"}, {})
        assert tier == "medium"

    def test_missing_values_default_medium(self):
        """Missing effort/risk values default to Medium -> medium."""
        tier = resolve_complexity_tier({"effort": "", "risk": ""}, {})
        assert tier == "medium"

    def test_none_values_default_medium(self):
        """None effort/risk values default to Medium -> medium."""
        tier = resolve_complexity_tier({"effort": None, "risk": None}, {})
        assert tier == "medium"

    def test_custom_thresholds_via_config(self):
        """Custom config overrides default thresholds."""
        config = {
            "complexity_tier": {
                "low": {"max_effort": "Medium", "max_risk": "Low"},
                "high": {"min_effort": "Extra Large", "min_risk": "High"},
            }
        }
        # Medium + Low -> low (under custom thresholds)
        tier = resolve_complexity_tier({"effort": "Medium", "risk": "Low"}, config)
        assert tier == "low"

        # Large + Medium -> medium (below XL min_effort and High min_risk)
        tier = resolve_complexity_tier({"effort": "Large", "risk": "Medium"}, config)
        assert tier == "medium"

        # Extra Large + High -> high (both meet custom high thresholds)
        tier = resolve_complexity_tier({"effort": "Extra Large", "risk": "High"}, config)
        assert tier == "high"

        # Compare with defaults: Large + Low is 'high' by default (effort >= Large)
        # But with custom high min_effort="Extra Large", Large + Low should be 'medium'
        tier = resolve_complexity_tier({"effort": "Large", "risk": "Low"}, config)
        assert tier == "medium", f"Expected medium with custom thresholds but got {tier}"

    def test_unknown_effort_value(self):
        """Unknown effort value defaults to Medium -> medium when risk is Low."""
        tier = resolve_complexity_tier({"effort": "Gigantic", "risk": "Low"}, {})
        assert tier == "medium"

    def test_unknown_risk_value(self):
        """Unknown risk value defaults to Medium -> medium."""
        tier = resolve_complexity_tier({"effort": "Small", "risk": "Extreme"}, {})
        assert tier == "medium"


# =========================================================================
# 3. Idempotence checks  (is_effort_risk_computed)
# =========================================================================


class TestIsEffortRiskComputed:
    """Verify idempotence detection logic (extracted from ralph_loop._is_effort_risk_computed)."""

    def test_both_fields_set_returns_true(self):
        """When both effort and risk are non-empty, return True."""
        item = {"effort": "Small", "risk": "Low"}
        assert is_effort_risk_computed(item, comments=[]) is True

    def test_missing_effort_returns_false(self):
        """When effort is empty and risk is set, return False."""
        item = {"effort": "", "risk": "Low"}
        assert is_effort_risk_computed(item, comments=[]) is False

    def test_missing_risk_returns_false(self):
        """When risk is empty and effort is set, return False."""
        item = {"effort": "Small", "risk": ""}
        assert is_effort_risk_computed(item, comments=[]) is False

    def test_both_missing_returns_false(self):
        """When both are empty, return False."""
        item = {"effort": "", "risk": ""}
        assert is_effort_risk_computed(item, comments=[]) is False

    def test_existing_autoplan_comment_returns_true(self):
        """Existing comment with autoplan-decision-hash returns True even without fields."""
        comments = [
            {"comment": "# Ralph Auto-Plan Decision\nautoplan-decision-hash:abc123\n\nEffort: Small\nRisk: Low", "author": "ralph"}
        ]
        item = {"effort": "", "risk": ""}
        assert is_effort_risk_computed(item, comments=comments) is True

    def test_comment_without_autoplan_hash_returns_false(self):
        """Comments without autoplan-decision-hash do not trigger idempotence."""
        comments = [
            {"comment": "Some other comment", "author": "user"}
        ]
        item = {"effort": "", "risk": ""}
        assert is_effort_risk_computed(item, comments=comments) is False

    def test_empty_comments_list(self):
        """Empty comments list with no fields returns False."""
        item = {"effort": "", "risk": ""}
        assert is_effort_risk_computed(item, comments=[]) is False

    def test_fields_trumped_by_comment(self):
        """Both fields set with existing comment still returns True."""
        item = {"effort": "Small", "risk": "Low"}
        comments = [{"comment": "autoplan-decision-hash:def456", "author": "ralph"}]
        assert is_effort_risk_computed(item, comments=comments) is True


# =========================================================================
# 4. Threshold decision logic  (make_autoplan_decision integration)
# =========================================================================


class TestThresholdDecision:
    """Verify the core decision logic embedded in make_autoplan_decision."""

    def test_skip_plan_for_small_low(self):
        """Small effort + Low risk  -> do_plan=False (skip plan)."""
        do_plan, stage = make_autoplan_decision(
            target_id="SA-TEST",
            config={},
            effort_skip=DEFAULT_AUTOPLAN_EFFORT_SKIP,
            risk_skip=DEFAULT_AUTOPLAN_RISK_SKIP,
            # Provide pre-computed effort/risk so the mock doesn't run the script
            precomputed_item={"effort": "Small", "risk": "Low"},
            precomputed_comments=[],
        )
        assert do_plan is False
        assert stage == "intake_complete"

    def test_skip_plan_for_xs_low(self):
        """Extra Small + Low  -> do_plan=False."""
        do_plan, stage = make_autoplan_decision(
            target_id="SA-TEST", config={},
            effort_skip=DEFAULT_AUTOPLAN_EFFORT_SKIP,
            risk_skip=DEFAULT_AUTOPLAN_RISK_SKIP,
            precomputed_item={"effort": "Extra Small", "risk": "Low"},
            precomputed_comments=[],
        )
        assert do_plan is False

    def test_invoke_plan_for_medium_high(self):
        """Medium + High  -> do_plan=True."""
        do_plan, stage = make_autoplan_decision(
            target_id="SA-TEST", config={},
            effort_skip=DEFAULT_AUTOPLAN_EFFORT_SKIP,
            risk_skip=DEFAULT_AUTOPLAN_RISK_SKIP,
            precomputed_item={"effort": "Medium", "risk": "High"},
            precomputed_comments=[],
        )
        assert do_plan is True

    def test_invoke_plan_for_large_high(self):
        """Large + High  -> do_plan=True."""
        do_plan, stage = make_autoplan_decision(
            target_id="SA-TEST", config={},
            effort_skip=DEFAULT_AUTOPLAN_EFFORT_SKIP,
            risk_skip=DEFAULT_AUTOPLAN_RISK_SKIP,
            precomputed_item={"effort": "Large", "risk": "High"},
            precomputed_comments=[],
        )
        assert do_plan is True

    def test_invoke_plan_for_xl_low(self):
        """Extra Large + Low  -> do_plan=True (effort above threshold even though risk low)."""
        do_plan, stage = make_autoplan_decision(
            target_id="SA-TEST", config={},
            effort_skip=DEFAULT_AUTOPLAN_EFFORT_SKIP,
            risk_skip=DEFAULT_AUTOPLAN_RISK_SKIP,
            precomputed_item={"effort": "Extra Large", "risk": "Low"},
            precomputed_comments=[],
        )
        assert do_plan is True

    def test_invoke_plan_for_small_high(self):
        """Small + High  -> do_plan=True (risk above threshold)."""
        do_plan, stage = make_autoplan_decision(
            target_id="SA-TEST", config={},
            effort_skip=DEFAULT_AUTOPLAN_EFFORT_SKIP,
            risk_skip=DEFAULT_AUTOPLAN_RISK_SKIP,
            precomputed_item={"effort": "Small", "risk": "High"},
            precomputed_comments=[],
        )
        assert do_plan is True

    def test_custom_thresholds_medium_effort_skip(self):
        """Custom thresholds: Medium effort included in skip set."""
        do_plan, stage = make_autoplan_decision(
            target_id="SA-TEST", config={},
            effort_skip=frozenset({"Extra Small", "Small", "Medium"}),
            risk_skip=frozenset({"Low"}),
            precomputed_item={"effort": "Medium", "risk": "Low"},
            precomputed_comments=[],
        )
        assert do_plan is False

    def test_custom_thresholds_should_plan(self):
        """Custom thresholds: Medium + Medium risk -> plan when risk not in skip set."""
        do_plan, stage = make_autoplan_decision(
            target_id="SA-TEST", config={},
            effort_skip=frozenset({"Extra Small", "Small", "Medium"}),
            risk_skip=frozenset({"Low"}),
            precomputed_item={"effort": "Medium", "risk": "Medium"},
            precomputed_comments=[],
        )
        assert do_plan is True


# =========================================================================
# 5. Idempotence in make_autoplan_decision
# =========================================================================


class TestMakeAutoplanDecisionIdempotence:
    """Verify idempotence behavior when effort/risk are already computed."""

    def test_existing_fields_skip_recomputation(self):
        """When effort and risk are already set, do not run effort-and-risk script."""
        with patch("command.plan_helpers.run_effort_and_risk") as mock_er:
            do_plan, stage = make_autoplan_decision(
                target_id="SA-TEST", config={},
                effort_skip=DEFAULT_AUTOPLAN_EFFORT_SKIP,
                risk_skip=DEFAULT_AUTOPLAN_RISK_SKIP,
                precomputed_item={"effort": "Small", "risk": "Low"},
                precomputed_comments=[],
            )
            mock_er.assert_not_called()
            assert do_plan is False

    def test_existing_autoplan_comment_skips_recomputation(self):
        """When comment with autoplan-decision-hash exists, skip effort-and-risk."""
        comments = [{"comment": "# Ralph Auto-Plan Decision\nautoplan-decision-hash:abc123\n\nEffort: Small\nRisk: Low", "author": "ralph"}]
        with patch("command.plan_helpers.run_effort_and_risk") as mock_er:
            do_plan, stage = make_autoplan_decision(
                target_id="SA-TEST", config={},
                effort_skip=DEFAULT_AUTOPLAN_EFFORT_SKIP,
                risk_skip=DEFAULT_AUTOPLAN_RISK_SKIP,
                precomputed_item={"effort": "", "risk": ""},
                precomputed_comments=comments,
            )
            mock_er.assert_not_called()
            # With existing comment but no fields, we can't determine effort/risk
            # Default to plan (safety-first) when we can't determine values
            assert do_plan is True

    def test_comment_does_not_create_duplicate_decision_comment(self):
        """When autoplan decision already determined, append_autoplan_decision_comment is not called."""
        with (
            patch("command.plan_helpers.run_effort_and_risk") as mock_er,
            patch("command.plan_helpers.append_autoplan_decision_comment") as mock_append,
        ):
            do_plan, stage = make_autoplan_decision(
                target_id="SA-TEST", config={},
                effort_skip=DEFAULT_AUTOPLAN_EFFORT_SKIP,
                risk_skip=DEFAULT_AUTOPLAN_RISK_SKIP,
                precomputed_item={"effort": "Small", "risk": "Low"},
                precomputed_comments=[],
            )
            # append should NOT be called when using precomputed_item (the
            # presumption is that the decision was already recorded)
            mock_append.assert_not_called()


# =========================================================================
# 6. Effort-and-risk invocation wrapper  (run_effort_and_risk)
# =========================================================================


class TestRunEffortAndRisk:
    """Verify the effort-and-risk orchestrator invocation."""

    @patch("subprocess.run")
    def test_successful_invocation(self, mock_run):
        """Successful orchestrator run returns parsed JSON."""
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = json.dumps({
            "effort": {"tshirt": "Small"},
            "risk": {"level": "Low", "score": 2},
        })
        mock_proc.stderr = ""
        mock_run.return_value = mock_proc

        result = run_effort_and_risk("SA-TEST")
        assert result is not None
        assert result["effort"]["tshirt"] == "Small"
        assert result["risk"]["level"] == "Low"

    @patch("subprocess.run")
    def test_failure_returns_none(self, mock_run):
        """Failed orchestrator run returns None."""
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.stdout = ""
        mock_proc.stderr = "error"
        mock_run.return_value = mock_proc

        result = run_effort_and_risk("SA-TEST")
        assert result is None

    @patch("subprocess.run")
    def test_invalid_json_returns_none(self, mock_run):
        """Invalid JSON output returns None."""
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = "not json"
        mock_proc.stderr = ""
        mock_run.return_value = mock_proc

        result = run_effort_and_risk("SA-TEST")
        assert result is None

    @patch("subprocess.run")
    def test_error_key_in_result_returns_none(self, mock_run):
        """Result with 'error' key returns None."""
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = json.dumps({"error": "Something went wrong"})
        mock_proc.stderr = ""
        mock_run.return_value = mock_proc

        result = run_effort_and_risk("SA-TEST")
        assert result is None

    @patch("subprocess.run")
    def test_invokes_orchestrate_script(self, mock_run):
        """Verifies the correct orchestrate_estimate.py is invoked."""
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = json.dumps({"effort": {"tshirt": "Small"}, "risk": {"level": "Low", "score": 0}})
        mock_proc.stderr = ""
        mock_run.return_value = mock_proc

        run_effort_and_risk("SA-TEST")

        # Check that the correct script path and target_id were used
        call_args = mock_run.call_args[0][0]
        assert "python3" in call_args or "python" in call_args
        assert any("orchestrate_estimate.py" in arg for arg in call_args)

        # Verify the input payload contains the target_id
        kwargs = mock_run.call_args[1]
        input_data = kwargs.get("input")
        assert input_data is not None
        payload = json.loads(input_data)
        assert payload["issue_id"] == "SA-TEST"


# =========================================================================
# 7. Decision comment posting  (append_autoplan_decision_comment)
# =========================================================================


class TestAppendAutoplanDecisionComment:
    """Verify idempotent autoplan decision comment posting."""

    @patch("subprocess.run")
    def test_posts_skip_comment(self, mock_run):
        """Posts a decision comment when autoplan decides to skip /plan."""
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = json.dumps({"success": True})
        mock_proc.stderr = ""
        mock_run.return_value = mock_proc

        # Simulate no existing comments
        with patch("command.plan_helpers._wl_comment_list", return_value=[]):
            append_autoplan_decision_comment("SA-TEST", "Small", "Low", 2, do_plan=False)

            # Verify wl comment add was called
            assert mock_run.call_count >= 1
            # Find the comment add call
            comment_calls = [
                c for c in mock_run.call_args_list
                if "comment" in str(c) and "add" in str(c)
            ]
            assert len(comment_calls) >= 1

    @patch("subprocess.run")
    def test_posts_plan_comment(self, mock_run):
        """Posts a decision comment when autoplan decides to run /plan."""
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = json.dumps({"success": True})
        mock_proc.stderr = ""
        mock_run.return_value = mock_proc

        with patch("command.plan_helpers._wl_comment_list", return_value=[]):
            append_autoplan_decision_comment("SA-TEST", "Medium", "High", 15, do_plan=True)

            comment_calls = [
                c for c in mock_run.call_args_list
                if "comment" in str(c) and "add" in str(c)
            ]
            assert len(comment_calls) >= 1

    @patch("subprocess.run")
    def test_skip_comment_contains_proceed_to_implement(self, mock_run):
        """Skip decision comment contains expected text."""
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = json.dumps({"success": True})
        mock_proc.stderr = ""
        mock_run.return_value = mock_proc

        with patch("command.plan_helpers._wl_comment_list", return_value=[]):
            append_autoplan_decision_comment("SA-TEST", "Small", "Low", 2, do_plan=False)

            # Find the comment text
            for call_args in mock_run.call_args_list:
                args, kwargs = call_args
                comment_arg = kwargs.get("input") or (args[0] if args else "")
                comment_str = str(comment_arg)
                if "proceed to implement" in comment_str:
                    return
            # If we didn't find it via input, check command-line args
            for call_args in mock_run.call_args_list:
                args, kwargs = call_args
                cmd_str = " ".join(str(a) for a in args[0]) if args else ""
                if "proceed to implement" in cmd_str:
                    return
            pytest.fail("No autoplan comment with 'proceed to implement' found")

    @patch("subprocess.run")
    def test_plan_comment_contains_run_plan(self, mock_run):
        """Plan decision comment contains expected text."""
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = json.dumps({"success": True})
        mock_proc.stderr = ""
        mock_run.return_value = mock_proc

        with patch("command.plan_helpers._wl_comment_list", return_value=[]):
            append_autoplan_decision_comment("SA-TEST", "Medium", "High", 15, do_plan=True)

            for call_args in mock_run.call_args_list:
                args, kwargs = call_args
                comment_arg = kwargs.get("input") or (args[0] if args else "")
                comment_str = str(comment_arg)
                if "run /plan" in comment_str:
                    return
            for call_args in mock_run.call_args_list:
                args, kwargs = call_args
                cmd_str = " ".join(str(a) for a in args[0]) if args else ""
                if "run /plan" in cmd_str:
                    return
            pytest.fail("No autoplan comment with 'run /plan' found")

    @patch("subprocess.run")
    def test_idempotent_does_not_duplicate_comment(self, mock_run):
        """When the same decision marker already exists, do not post again."""
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = json.dumps({"success": True})
        mock_proc.stderr = ""
        mock_run.return_value = mock_proc

        # Existing comment with the same decision hash (Small, Low, score 2)
        expected_hash = "dfb88f9074e1c02e"  # sha256("autoplan-decision:Small:Low:2")[:16]
        existing_comments = [
            {"comment": f"# Ralph Auto-Plan Decision\nautoplan-decision-hash:{expected_hash}\n\nEffort: Small\nRisk: Low (score: 2)\nDecision: proceed to implement (effort and risk below threshold)", "author": "ralph"}
        ]
        with patch("command.plan_helpers._wl_comment_list", return_value=existing_comments):
            initial_call_count = mock_run.call_count
            append_autoplan_decision_comment("SA-TEST", "Small", "Low", 2, do_plan=False)

            # No new comment add should have been made
            assert mock_run.call_count == initial_call_count

    @patch("subprocess.run")
    def test_different_decision_posts_new_comment(self, mock_run):
        """When the decision changed, post a new comment."""
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = json.dumps({"success": True})
        mock_proc.stderr = ""
        mock_run.return_value = mock_proc

        # Existing comment with Small/Low decision, now we have different values
        existing_comments = [
            {"comment": "# Ralph Auto-Plan Decision\nautoplan-decision-hash:abc123\n\nEffort: Small\nRisk: Low (score: 2)\nDecision: proceed to implement (effort and risk below threshold)", "author": "ralph"}
        ]
        with patch("command.plan_helpers._wl_comment_list", return_value=existing_comments):
            initial_call_count = mock_run.call_count
            append_autoplan_decision_comment("SA-TEST", "Medium", "High", 15, do_plan=True)

            # New comment should be posted (different hash)
            assert mock_run.call_count > initial_call_count


# =========================================================================
# 8. Full orchestration error handling
# =========================================================================


class TestMakeAutoplanDecisionErrors:
    """Verify error handling in the full orchestration path."""

    @patch("command.plan_helpers.run_effort_and_risk", return_value=None)
    def test_effort_risk_failure_defaults_to_plan(self, mock_er):
        """When effort-and-risk fails, default to running /plan (safety-first)."""
        do_plan, stage = make_autoplan_decision(
            target_id="SA-TEST", config={},
            effort_skip=DEFAULT_AUTOPLAN_EFFORT_SKIP,
            risk_skip=DEFAULT_AUTOPLAN_RISK_SKIP,
            precomputed_item={"effort": "", "risk": ""},
            precomputed_comments=[],
        )
        assert do_plan is True
        mock_er.assert_called_once_with("SA-TEST")

    @patch("command.plan_helpers.run_effort_and_risk")
    def test_ambiguous_result_defaults_to_plan(self, mock_er):
        """When effort-and-risk returns ambiguous/malformed data, default to plan."""
        mock_er.return_value = {"effort": {}, "risk": {}}
        do_plan, stage = make_autoplan_decision(
            target_id="SA-TEST", config={},
            effort_skip=DEFAULT_AUTOPLAN_EFFORT_SKIP,
            risk_skip=DEFAULT_AUTOPLAN_RISK_SKIP,
            precomputed_item={"effort": "", "risk": ""},
            precomputed_comments=[],
        )
        # Missing tshirt/level should default to plan (safety-first)
        assert do_plan is True

    @patch("command.plan_helpers.run_effort_and_risk")
    def test_append_comment_called_on_successful_er_run(self, mock_er):
        """When effort-and-risk succeeds, append_autoplan_decision_comment is called."""
        mock_er.return_value = {
            "effort": {"tshirt": "Small"},
            "risk": {"level": "Low", "score": 2},
        }
        with patch("command.plan_helpers.append_autoplan_decision_comment") as mock_append:
            do_plan, stage = make_autoplan_decision(
                target_id="SA-TEST", config={},
                effort_skip=DEFAULT_AUTOPLAN_EFFORT_SKIP,
                risk_skip=DEFAULT_AUTOPLAN_RISK_SKIP,
                precomputed_item={"effort": "", "risk": ""},
                precomputed_comments=[],
            )
            mock_append.assert_called_once()
            args, kwargs = mock_append.call_args
            assert "SA-TEST" in args
            assert "Small" in args
            assert "Low" in args


# =========================================================================
# 9. CLI entry points
# =========================================================================


class TestCliPlanIfNeeded:
    """Verify the plan-if-needed CLI entry point."""

    @patch("command.plan_helpers.make_autoplan_decision")
    def test_skip_decision_json_output(self, mock_make):
        """plan-if-needed returns JSON with decision=skip when thresholds met."""
        mock_make.return_value = (False, "intake_complete")
        result = plan_if_needed("SA-TEST")
        assert result["decision"] == "skip"
        assert result["target_id"] == "SA-TEST"

    @patch("command.plan_helpers.make_autoplan_decision")
    def test_plan_decision_json_output(self, mock_make):
        """plan-if-needed returns JSON with decision=plan when thresholds exceeded."""
        mock_make.return_value = (True, "plan_complete")
        result = plan_if_needed("SA-TEST")
        assert result["decision"] == "plan"
        assert result["target_id"] == "SA-TEST"

    @patch("command.plan_helpers.make_autoplan_decision")
    def test_includes_effort_and_risk_in_output(self, mock_make):
        """plan-if-needed output includes effort and risk values."""
        mock_make.return_value = (False, "intake_complete")
        # When called with precomputed_item and precomputed_comments, the test
        # should verify those are passed through. We rely on make_autoplan_decision
        # returning effort/risk info in the output.
        result = plan_if_needed("SA-TEST")
        assert "effort" in result
        assert "risk" in result


class TestCliCheckEffortRisk:
    """Verify the check-effort-risk CLI entry point."""

    @patch("command.plan_helpers.run_effort_and_risk")
    def test_returns_effort_risk_values(self, mock_er):
        """check-effort-risk returns effort and risk values."""
        mock_er.return_value = {
            "effort": {"tshirt": "Small"},
            "risk": {"level": "Low", "score": 2},
        }
        result = check_effort_risk("SA-TEST")
        assert result["effort"]["tshirt"] == "Small"
        assert result["risk"]["level"] == "Low"
        assert result["risk"]["score"] == 2
        assert result["target_id"] == "SA-TEST"

    @patch("command.plan_helpers.run_effort_and_risk", return_value=None)
    def test_failure_returns_error(self, mock_er):
        """check-effort-risk returns error when effort-and-risk fails."""
        result = check_effort_risk("SA-TEST")
        assert "error" in result
        assert result["target_id"] == "SA-TEST"

    @patch("command.plan_helpers.run_effort_and_risk")
    def test_handles_missing_keys_gracefully(self, mock_er):
        """check-effort-risk handles malformed output gracefully."""
        mock_er.return_value = {"effort": {}, "risk": {}}
        result = check_effort_risk("SA-TEST")
        assert result["target_id"] == "SA-TEST"
        # Should not crash; missing values become empty or None
        assert "effort" in result
        assert "risk" in result


# =========================================================================
# 10. Pre-computation integration  (no effort-and-risk needed)
# =========================================================================


class TestMakeAutoplanDecisionPrecomputed:
    """Verify that precomputed values bypass effort-and-risk entirely."""

    @patch("command.plan_helpers.run_effort_and_risk")
    def test_small_low_with_precomputed_skips_er_script(self, mock_er):
        """When precomputed_item has effort/risk, do not call run_effort_and_risk."""
        do_plan, stage = make_autoplan_decision(
            target_id="SA-TEST", config={},
            effort_skip=DEFAULT_AUTOPLAN_EFFORT_SKIP,
            risk_skip=DEFAULT_AUTOPLAN_RISK_SKIP,
            precomputed_item={"effort": "Small", "risk": "Low"},
            precomputed_comments=[],
        )
        mock_er.assert_not_called()
        assert do_plan is False

    @patch("command.plan_helpers.run_effort_and_risk")
    def test_medium_high_with_precomputed_skips_er_script(self, mock_er):
        """When precomputed_item has effort/risk, do not call run_effort_and_risk."""
        do_plan, stage = make_autoplan_decision(
            target_id="SA-TEST", config={},
            effort_skip=DEFAULT_AUTOPLAN_EFFORT_SKIP,
            risk_skip=DEFAULT_AUTOPLAN_RISK_SKIP,
            precomputed_item={"effort": "Medium", "risk": "High"},
            precomputed_comments=[],
        )
        mock_er.assert_not_called()
        assert do_plan is True

    @patch("command.plan_helpers.run_effort_and_risk")
    def test_precomputed_with_existing_comment(self, mock_er):
        """Precomputed with existing autoplan comment uses cached data."""
        comments = [{"comment": "autoplan-decision-hash:xyz", "author": "ralph"}]
        do_plan, stage = make_autoplan_decision(
            target_id="SA-TEST", config={},
            effort_skip=DEFAULT_AUTOPLAN_EFFORT_SKIP,
            risk_skip=DEFAULT_AUTOPLAN_RISK_SKIP,
            precomputed_item={"effort": "Small", "risk": "Low"},
            precomputed_comments=comments,
        )
        mock_er.assert_not_called()
        # With effort/risk fields AND comment, both indicate computed -> skip
        assert do_plan is False
