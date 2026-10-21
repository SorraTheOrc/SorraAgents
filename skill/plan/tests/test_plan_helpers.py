"""Tests for the plan-approval gate helpers in skill/plan/plan_helpers.py.

The plan skill's step 4 asks the user to approve a proposed feature plan.
Approval is requested only when the work item is BOTH the largest (effort
t-shirt "Extra Large") AND the highest risk (High/Severe) — an AND gate.
Every other effort/risk combination proceeds directly to the automated
review stages without an approval pause (SA-0MUYG0HE6001YTEM).

Missing *or unrecognised* effort/risk values default conservatively to
requesting approval (mirroring ``resolve_complexity_tier``'s Medium default)
so a human checkpoint is never silently skipped.

Related work items: SA-0MSHID94D009P0TL, SA-0MUYG0HE6001YTEM,
SA-0MUIUTJJS008W06Y
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
    normalise_effort_label,
    plan_approval_gate,
    plan_if_needed,
    resolve_complexity_tier,
    should_request_plan_approval,
)

# Full t-shirt effort scale and wl risk scale used by the matrix tests.
EFFORT_LEVELS = ["Extra Small", "Small", "Medium", "Large", "Extra Large"]
RISK_LEVELS = ["Low", "Medium", "High", "Severe"]

# =========================================================================
# 0. normalise_effort_label — spelling parity (abbreviations vs full labels)
#
# Root cause c04ecb5b: the effort-and-risk skill writes FULL labels
# ("Extra Small" …) via `wl update --effort`, while wl's canonical
# vocabulary is ABBREVIATED (XS/S/M/L/XL per WorkItemEffortLevel and the
# `wl update --effort` help text). plan_helpers compared only full labels,
# so DEFAULT_AUTOPLAN_EFFORT_SKIP never matched a stored "S"/"XS" and
# PLAN_APPROVAL_EFFORT never matched a stored "XL". The fix normalises
# both spellings to one canonical form at every threshold site.
# =========================================================================


class TestNormaliseEffortLabel:
    """Both spellings of every effort level map to one canonical full label."""

    @pytest.mark.parametrize(
        ("raw", "canonical"),
        [
            ("XS", "Extra Small"),
            ("S", "Small"),
            ("M", "Medium"),
            ("L", "Large"),
            ("XL", "Extra Large"),
            ("Extra Small", "Extra Small"),
            ("Small", "Small"),
            ("Medium", "Medium"),
            ("Large", "Large"),
            ("Extra Large", "Extra Large"),
        ],
    )
    def test_both_spellings_map_to_canonical_full_label(self, raw, canonical):
        assert normalise_effort_label(raw) == canonical

    @pytest.mark.parametrize("raw", ["", None, "  ", "unknown-label", "Small "])
    def test_blank_and_unknown_values_degrade_gracefully(self, raw):
        """Blank values become empty string; unknown spellings pass through (stripped)."""
        result = normalise_effort_label(raw)
        if raw is None or raw.strip() == "":
            assert result == ""
        else:
            assert result == raw.strip()


# =========================================================================
# 1. should_request_plan_approval — pure decision logic
# =========================================================================


class TestShouldRequestPlanApproval:
    """Verify the approval-gate decision across the full effort/risk matrix."""

    def test_extra_large_and_high_requests_approval(self):
        """Extra Large effort AND High risk is the gate trigger."""
        request, reason = should_request_plan_approval(
            {"effort": "Extra Large", "risk": "High"}
        )
        assert request is True
        assert "Extra Large" in reason
        assert "High" in reason

    def test_extra_large_and_severe_requests_approval(self):
        """Extra Large effort AND Severe risk also requests approval."""
        request, reason = should_request_plan_approval(
            {"effort": "Extra Large", "risk": "Severe"}
        )
        assert request is True
        assert "Extra Large" in reason
        assert "Severe" in reason

    @pytest.mark.parametrize("risk", RISK_LEVELS)
    @pytest.mark.parametrize("effort", EFFORT_LEVELS)
    def test_full_effort_risk_matrix(self, effort, risk):
        """Only Extra Large effort AND High/Severe risk triggers approval.

        Full 5×4 matrix (effort × risk); every other combination proceeds
        without an approval pause (SA-0MUYG0HE6001YTEM)."""
        request, reason = should_request_plan_approval(
            {"effort": effort, "risk": risk}
        )
        expected = effort == "Extra Large" and risk in {"High", "Severe"}
        assert request is expected, f"effort={effort!r} risk={risk!r}"
        if expected:
            assert "Extra Large" in reason
            assert risk in reason
        else:
            assert reason == ""

    @pytest.mark.parametrize("risk", ["Low", "Medium"])
    def test_extra_large_effort_alone_skips_approval(self, risk):
        """Extra Large effort with Low/Medium risk is no longer enough."""
        request, reason = should_request_plan_approval(
            {"effort": "Extra Large", "risk": risk}
        )
        assert request is False
        assert reason == ""

    @pytest.mark.parametrize("risk", ["High", "Severe"])
    @pytest.mark.parametrize("effort", ["Extra Small", "Small", "Medium", "Large"])
    def test_high_risk_alone_skips_approval(self, effort, risk):
        """High/Severe risk with less than Extra Large effort is not enough."""
        request, reason = should_request_plan_approval(
            {"effort": effort, "risk": risk}
        )
        assert request is False, f"effort={effort!r} risk={risk!r}"
        assert reason == ""

    def test_medium_risk_alone_no_longer_triggers_approval(self):
        """Medium risk no longer triggers the gate by itself
        (PLAN_APPROVAL_RISK is High/Severe only since SA-0MTGX1I00007DBLX);
        a small, Medium-risk item proceeds without an approval pause."""
        request, reason = should_request_plan_approval(
            {"effort": "Small", "risk": "Medium"}
        )
        assert request is False
        assert reason == ""

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

    @pytest.mark.parametrize(
        ("effort", "risk"),
        [
            ("Huge", "Low"),
            ("Gigantic", "Medium"),
            ("Small", "Extreme"),
            ("Small", "Critical"),
            ("Extra Large", "Critical"),
            ("Huge", "Severe"),
        ],
    )
    def test_unrecognised_values_default_to_approval(self, effort, risk):
        """A present-but-unrecognised effort or risk defaults conservatively to
        approval — a future vocabulary addition must never silently skip the
        human checkpoint (SA-0MUIUTJJS008W06Y)."""
        request, reason = should_request_plan_approval(
            {"effort": effort, "risk": risk}
        )
        assert request is True, f"effort={effort!r} risk={risk!r}"
        assert reason  # the reason must explain the conservative default

    @pytest.mark.parametrize(
        ("item", "unrecognised"),
        [
            ({"effort": "Huge", "risk": "Low"}, "Huge"),
            ({"effort": "Small", "risk": "Critical"}, "Critical"),
        ],
    )
    def test_unrecognised_reason_names_the_offending_value(self, item, unrecognised):
        """The conservative reason identifies which value was unrecognised."""
        request, reason = should_request_plan_approval(item)
        assert request is True
        assert unrecognised in reason


class TestResolveComplexityTierVocabularyParity:
    """``resolve_complexity_tier`` accepts the canonical ``wl`` vocabularies.

    The ``wl`` risk scale includes ``Severe`` (above ``High``) and effort is
    stored either abbreviated (``XS``/``S``/``M``/``L``/``XL``) or as a full
    label (``Extra Small`` … ``Extra Large``). Both spellings must resolve to
    the same tier, and ``Severe`` must rank above ``High``
    (SA-0MUIUTJJS008W06Y).
    """

    @pytest.mark.parametrize(
        ("effort", "risk", "expected"),
        [
            ("Small", "Severe", "high"),
            ("Extra Small", "Severe", "high"),
            ("Small", "High", "high"),
            ("XL", "Low", "high"),
            ("Extra Large", "Low", "high"),
            ("L", "Low", "high"),
            ("Large", "Low", "high"),
            ("M", "Low", "medium"),
            ("Medium", "Low", "medium"),
            ("S", "Low", "low"),
            ("Small", "Low", "low"),
            ("XS", "Low", "low"),
            ("Extra Small", "Low", "low"),
        ],
    )
    def test_canonical_vocabulary_resolves_expected_tier(self, effort, risk, expected):
        assert resolve_complexity_tier({"effort": effort, "risk": risk}, {}) == expected

    @pytest.mark.parametrize("effort", ["XL", "Extra Large"])
    def test_abbreviated_and_full_effort_agree_for_severe_risk(self, effort):
        """Stored ``XL`` and ``Extra Large`` agree when risk is ``Severe``."""
        assert resolve_complexity_tier({"effort": effort, "risk": "Severe"}, {}) == "high"

    def test_config_thresholds_accept_abbreviated_effort(self):
        """Custom thresholds written with wl abbreviations behave like full labels."""
        config = {
            "complexity_tier": {
                "low": {"max_effort": "S"},
                "high": {"min_effort": "XL"},
            }
        }
        assert resolve_complexity_tier({"effort": "S", "risk": "Low"}, config) == "low"
        assert resolve_complexity_tier({"effort": "L", "risk": "Low"}, config) == "medium"
        assert resolve_complexity_tier({"effort": "XL", "risk": "Low"}, config) == "high"


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

    def test_approval_when_xl_abbreviation_and_high_risk(self):
        """Stored wl abbreviation XL + High risk yields request_approval=True."""
        runner = _FakeWlShow({"id": "X", "effort": "XL", "risk": "High"})
        result = plan_approval_gate("X", runner=runner)
        assert result["request_approval"] is True
        assert "Extra Large" in result["reason"]
        assert "High" in result["reason"]

    def test_skip_when_s_abbreviation_and_low_risk(self):
        """Stored wl abbreviation S + Low risk yields request_approval=False."""
        runner = _FakeWlShow({"id": "X", "effort": "S", "risk": "Low"})
        result = plan_approval_gate("X", runner=runner)
        assert result["request_approval"] is False
        assert result["reason"] == ""

    def test_approval_when_extra_large_and_high_risk(self):
        """Extra Large + High risk yields request_approval=True naming both."""
        runner = _FakeWlShow({"id": "X", "effort": "Extra Large", "risk": "High"})
        result = plan_approval_gate("X", runner=runner)
        assert result["request_approval"] is True
        assert "Extra Large" in result["reason"]
        assert "High" in result["reason"]

    def test_approval_when_extra_large_and_severe_risk(self):
        """Extra Large + Severe risk (higher than High) requests approval."""
        runner = _FakeWlShow({"id": "X", "effort": "Extra Large", "risk": "Severe"})
        result = plan_approval_gate("X", runner=runner)
        assert result["request_approval"] is True
        assert "Severe" in result["reason"]

    def test_skip_when_extra_large_but_low_risk(self):
        """Extra Large effort with Low risk no longer requests approval."""
        runner = _FakeWlShow({"id": "X", "effort": "Extra Large", "risk": "Low"})
        result = plan_approval_gate("X", runner=runner)
        assert result["request_approval"] is False
        assert result["reason"] == ""

    def test_skip_when_high_risk_but_small_effort(self):
        """High risk with Small effort no longer requests approval."""
        runner = _FakeWlShow({"id": "X", "effort": "Small", "risk": "High"})
        result = plan_approval_gate("X", runner=runner)
        assert result["request_approval"] is False
        assert result["reason"] == ""

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


# =========================================================================
# 4. Claim invariant — approval gate reclaim (SA-0MTFTFUIH000UWM9)
#
# The AH-0MTFPDKDU006QUDC incident released to `open` at 12:08:51Z to pause
# for producer approval and never re-claimed after 12:09:42Z approval,
# leaving `open` while 3 children were created until 12:18:40Z — allowing
# herdr downtime to dispatch a duplicate plan at 12:13:02Z. With
# --reclaim-if-open the gate re-claims in_progress before any mutation.
# =========================================================================


class _SequenceRunner:
    """Runner that returns a pre-defined sequence of _FakeResult payloads."""

    def __init__(self, results: list):
        self._results = list(results)
        self.calls: list[list[str]] = []

    def __call__(self, cmd):
        self.calls.append(list(cmd))
        assert self._results, "SequenceRunner exhausted"
        return self._results.pop(0)


def _show_payload(work_item: dict) -> str:
    return json.dumps({"success": True, "workItem": work_item})


def _update_payload() -> str:
    return json.dumps({"success": True})


class TestPlanApprovalReclaimInvariant:
    """The approval gate re-claims in_progress before evaluation when requested."""

    def test_reclaim_if_open_reclaims_open_before_gate(self):
        """With reclaim_if_open=True and status open, gate re-claims before evaluating."""
        runner = _SequenceRunner([
            _FakeResult(_show_payload({"id": "X", "status": "open", "effort": "Extra Small", "risk": "Low"})),
            _FakeResult(_update_payload()),
            _FakeResult(_show_payload({"id": "X", "effort": "Extra Small", "risk": "Low"})),
        ])
        result = plan_approval_gate("X", runner=runner, reclaim_if_open=True)
        assert result["request_approval"] is False
        # Calls: 1) show for reclaim check, 2) update in-progress, 3) show for gate
        assert len(runner.calls) == 3
        assert "show" in runner.calls[0]
        assert "update" in runner.calls[1]
        assert "in-progress" in runner.calls[1]
        assert "show" in runner.calls[2]

    def test_reclaim_if_open_noop_when_already_in_progress(self):
        """With reclaim_if_open=True and already in_progress, no update is issued."""
        runner = _SequenceRunner([
            _FakeResult(_show_payload({"id": "X", "status": "in-progress", "effort": "Extra Small", "risk": "Low"})),
            _FakeResult(_show_payload({"id": "X", "effort": "Extra Small", "risk": "Low"})),
        ])
        result = plan_approval_gate("X", runner=runner, reclaim_if_open=True)
        assert result["request_approval"] is False
        assert len(runner.calls) == 2
        assert all("update" not in c for c in runner.calls)

    def test_without_reclaim_flag_does_not_reclaim(self):
        """Without reclaim_if_open the gate never touches status — only shows."""
        runner = _SequenceRunner([
            _FakeResult(_show_payload({"id": "X", "effort": "Extra Small", "risk": "Low"})),
        ])
        result = plan_approval_gate("X", runner=runner)
        assert result["request_approval"] is False
        assert len(runner.calls) == 1
        assert "update" not in runner.calls[0]

    def test_reclaim_failure_is_best_effort_gate_still_evaluates(self):
        """A failing reclaim (show error) does not abort the gate — it proceeds."""
        runner = _SequenceRunner([
            _FakeResult("{}", returncode=1, stderr="wl show failed"),
            _FakeResult(_show_payload({"id": "X", "effort": "Extra Large", "risk": "High"})),
        ])
        result = plan_approval_gate("X", runner=runner, reclaim_if_open=True)
        assert result["request_approval"] is True
        assert "Extra Large" in result["reason"]

    def test_continuous_in_progress_from_approval_through_gate(self):
        """End-to-end: open at approval → reclaim → gate + subsequent require_claimed."""
        # Step 1: approval resume re-claims
        gate_runner = _SequenceRunner([
            _FakeResult(_show_payload({"id": "X", "status": "open", "effort": "Small", "risk": "Low"})),
            _FakeResult(_update_payload()),
            _FakeResult(_show_payload({"id": "X", "effort": "Small", "risk": "Low"})),
        ])
        gate = plan_approval_gate("X", runner=gate_runner, reclaim_if_open=True)
        assert gate["request_approval"] is False
        # Step 2: mutation guard would pass (verified via StatusLifecycle with shim)
        from skill.plan.plan_helpers import _wl_reclaim

        # After gate reclaim, a subsequent require_claimed == in_progress
        verify_runner = _FakeResult(_show_payload({"id": "X", "status": "in-progress", "effort": "Small", "risk": "Low"}))

        class _VerifyRunner:
            def __call__(self, cmd):
                return verify_runner

        # _wl_reclaim is a no-op when already in_progress
        assert _wl_reclaim("X", runner=_VerifyRunner()) is not None

    def test_does_not_regress_existing_gate_behavior(self):
        """Without the flag, gate semantics are byte-identical to before."""
        for effort, risk, expected in [
            ("Extra Small", "Low", False),
            ("Extra Small", "Medium", False),
            ("Extra Large", "Low", False),
            ("Extra Large", "Medium", False),
            ("Medium", "Severe", False),
            ("Large", "High", False),
            ("Extra Large", "High", True),
            ("Extra Large", "Severe", True),
        ]:
            runner = _FakeWlShow({"id": "X", "effort": effort, "risk": risk})
            result = plan_approval_gate("X", runner=runner)
            assert result["request_approval"] is expected, (effort, risk)



# =========================================================================
# 5. Effort spelling parity at the threshold evaluation sites
#
# The autoplan skip and approval gates must accept BOTH the abbreviated wl
# vocabulary (XS/S/M/L/XL) and the full labels written by the effort-and-risk
# skill (SA-0MV85VT250096CTY). Without normalisation the skip set never
# matched stored "S"/"XS" (small items always planned) and the approval set
# never matched stored "XL" (the producer safety gate never fired).
# =========================================================================


class TestShouldRequestPlanApprovalEffortSpellingParity:
    """Abbreviated wl spellings trigger the same approval gate as full labels."""

    @pytest.mark.parametrize(
        "effort", ["XL", "Extra Large"],
        ids=["abbrev-xl", "full-xl"],
    )
    @pytest.mark.parametrize("risk", ["High", "Severe"])
    def test_xl_high_or_severe_requests_approval_under_both_spellings(self, effort, risk):
        request, reason = should_request_plan_approval({"effort": effort, "risk": risk})
        assert request is True
        assert "Extra Large" in reason
        assert risk in reason

    @pytest.mark.parametrize(
        "effort", ["XL", "Extra Large"],
        ids=["abbrev-xl", "full-xl"],
    )
    @pytest.mark.parametrize("risk", ["Low", "Medium"])
    def test_xl_alone_never_requests_approval(self, effort, risk):
        request, reason = should_request_plan_approval({"effort": effort, "risk": risk})
        assert request is False
        assert reason == ""

    @pytest.mark.parametrize(
        "effort",
        ["XS", "S", "M", "L", "Extra Small", "Small", "Medium", "Large"],
        ids=["abbrev-xs", "abbrev-s", "abbrev-m", "abbrev-l", "full-xs", "full-s", "full-m", "full-l"],
    )
    @pytest.mark.parametrize("risk", ["High", "Severe"])
    def test_sub_xl_effort_never_requests_approval(self, effort, risk):
        request, reason = should_request_plan_approval({"effort": effort, "risk": risk})
        assert request is False, f"effort={effort!r} risk={risk!r}"
        assert reason == ""


class TestMakeAutoplanDecisionEffortSpellingParity:
    """The autoplan skip threshold accepts abbreviated stored effort values.

    The idempotence path (stored effort/risk read back from the item) is the
    path that runs in practice for previously-computed items
    (make_autoplan_decision reads the stored field when
    is_effort_risk_computed), so abbreviations there are the critical
    regression (SA-0MV85VT250096CTY).
    """

    @pytest.mark.parametrize(
        ("effort", "canonical"),
        [
            ("S", "Small"),
            ("XS", "Extra Small"),
            ("Small", "Small"),
            ("Extra Small", "Extra Small"),
        ],
        ids=["abbrev-s", "abbrev-xs", "full-s", "full-xs"],
    )
    def test_skip_for_small_or_xs_low_under_both_spellings(self, effort, canonical):
        do_plan, stage, effort_risk = make_autoplan_decision(
            target_id="SA-TEST",
            config={},
            precomputed_item={"effort": effort, "risk": "Low"},
            precomputed_comments=[],
        )
        assert do_plan is False, f"effort={effort!r} risk=Low"
        assert stage == "intake_complete"
        assert effort_risk == {"effort": canonical, "risk": "Low"}

    @pytest.mark.parametrize(
        "effort",
        ["M", "L", "XL", "Medium", "Large", "Extra Large"],
        ids=["abbrev-m", "abbrev-l", "abbrev-xl", "full-m", "full-l", "full-xl"],
    )
    def test_plan_for_medium_or_larger_under_both_spellings(self, effort):
        do_plan, stage, _ = make_autoplan_decision(
            target_id="SA-TEST",
            config={},
            precomputed_item={"effort": effort, "risk": "Low"},
            precomputed_comments=[],
        )
        assert do_plan is True, f"effort={effort!r} risk=Low"
        assert stage == "plan_complete"

    def test_plan_if_needed_skips_for_stored_s_low(self):
        """End-to-end evidence case: stored effort 'S' + risk 'Low' -> skip."""
        runner = _FakeWlShow({"id": "SA-TEST", "effort": "S", "risk": "Low"})
        with patch("skill.plan.plan_helpers._wl_comment_list", return_value=[]):
            result = plan_if_needed("SA-TEST", runner=runner)
        assert result["decision"] == "skip"
        assert result["effort"] == "Small"
        assert result["risk"] == "Low"

    def test_plan_if_needed_skips_for_stored_xs_low(self):
        """End-to-end evidence case: stored effort 'XS' + risk 'Low' -> skip."""
        runner = _FakeWlShow({"id": "SA-TEST", "effort": "XS", "risk": "Low"})
        with patch("skill.plan.plan_helpers._wl_comment_list", return_value=[]):
            result = plan_if_needed("SA-TEST", runner=runner)
        assert result["decision"] == "skip"
        assert result["effort"] == "Extra Small"
        assert result["risk"] == "Low"

    def test_plan_approval_gate_xl_high_under_abbreviated_stored_value(self):
        """End-to-end: stored effort 'XL' + risk 'High' -> request_approval."""
        runner = _FakeWlShow({"id": "SA-TEST", "effort": "XL", "risk": "High"})
        result = plan_approval_gate("SA-TEST", runner=runner)
        assert result["request_approval"] is True
        assert "Extra Large" in result["reason"]
        assert "High" in result["reason"]

    def test_plan_approval_gate_xl_severe_under_abbreviated_stored_value(self):
        """End-to-end: stored effort 'XL' + risk 'Severe' -> request_approval."""
        runner = _FakeWlShow({"id": "SA-TEST", "effort": "XL", "risk": "Severe"})
        result = plan_approval_gate("SA-TEST", runner=runner)
        assert result["request_approval"] is True
        assert "Severe" in result["reason"]


class TestMakeAutoplanDecisionFreshPathEffortSpellingParity:
    """The freshly-computed effort-and-risk path normalises labels too."""

    @staticmethod
    def _fresh_decision(tshirt: str, risk: str) -> tuple[bool, str, dict]:
        runner = _FakeWlShow({"id": "SA-TEST", "effort": "", "risk": ""})
        er_result = {
            "effort": {"tshirt": tshirt},
            "risk": {"level": risk, "score": 0},
        }
        with (
            patch("skill.plan.plan_helpers.run_effort_and_risk", return_value=er_result),
            patch("skill.plan.plan_helpers.append_autoplan_decision_comment") as mock_append,
            patch("skill.plan.plan_helpers._wl_comment_list", return_value=[]),
        ):
            do_plan, stage, effort_risk = make_autoplan_decision(
                "SA-TEST", config={}, runner=runner
            )
            mock_append.assert_called_once()
        return do_plan, stage, effort_risk

    def test_fresh_small_low_skips(self):
        """Freshly computed tshirt 'S' + Low -> skip plan."""
        do_plan, stage, effort_risk = self._fresh_decision("S", "Low")
        assert do_plan is False
        assert stage == "intake_complete"
        assert effort_risk == {"effort": "Small", "risk": "Low"}

    def test_fresh_xs_low_skips(self):
        """Freshly computed tshirt 'XS' + Low -> skip plan."""
        do_plan, stage, _ = self._fresh_decision("XS", "Low")
        assert do_plan is False
        assert stage == "intake_complete"

    def test_fresh_xl_high_plans(self):
        """Freshly computed tshirt 'XL' + High -> run /plan."""
        do_plan, stage, _ = self._fresh_decision("XL", "High")
        assert do_plan is True
        assert stage == "plan_complete"

    def test_fresh_medium_low_plans(self):
        """Freshly computed tshirt 'M' + Low -> run /plan (above skip effort)."""
        do_plan, stage, _ = self._fresh_decision("M", "Low")
        assert do_plan is True
        assert stage == "plan_complete"
