"""Finish gate classifies a moved-checkout run as infrastructure.

Covers SA-0MW3FF18I003KGZE AC4 (first half): ``implement.py``'s finish gate
must classify a flagged run as infrastructure — never ``test_failure`` — with
an operator message naming the checkout movement, both commits and the
required re-run, checked BEFORE any failure-based classification.
"""
from __future__ import annotations

from skill.implement.scripts import implement


def _flag(checkout: str = "/shared/checkout") -> dict:
    return {
        "notice_code": "state_changed_during_run",
        "checkout": checkout,
        "before_commit": "a" * 40,
        "after_commit": "b" * 40,
        "message": "checkout moved while the suite executed",
    }


def _result(flag: dict | None = None, failures: list | None = None) -> dict:
    return {
        "success": False,
        "state_changed_during_run": [flag] if flag else [],
        "failures": failures or [],
        "notices": [],
        "notice_codes": ["state_changed_during_run"] if flag else [],
    }


class TestGateFailureKind:
    def test_flagged_result_is_infrastructure(self) -> None:
        """The flag wins the classification, even with stale failures."""
        assert implement._gate_failure_kind(_result(flag=_flag())) == (
            implement.GATE_FAILURE_INFRASTRUCTURE
        )

    def test_flag_beats_stale_failure_records(self) -> None:
        """A run still carrying failure records stays infrastructure."""
        result = _result(
            flag=_flag(),
            failures=[{"test_name": "mixed_tree_fiche.txt"}],
        )
        assert implement._gate_failure_kind(result) == (
            implement.GATE_FAILURE_INFRASTRUCTURE
        )

    def test_stable_red_run_is_still_test_failure(self) -> None:
        """Control: no flag, real failures -> test_failure."""
        result = _result(failures=[{"test_name": "test_real.py::t"}])
        result["notice_codes"] = []
        assert implement._gate_failure_kind(result) == (
            implement.GATE_FAILURE_TESTS
        )


class TestGateFailureDetail:
    def test_detail_names_checkout_and_commits(self) -> None:
        detail = implement._gate_failure_detail(_result(flag=_flag()))
        assert "moved" in detail
        assert "/shared/checkout" in detail
        assert "a" * 40 in detail and "b" * 40 in detail
        assert "re-run" in detail
        assert "Failures:" not in detail

    def test_detail_never_an_empty_failures_block(self) -> None:
        detail = implement._gate_failure_detail(_result(flag=_flag()))
        assert detail.strip()


class TestGateFailureMessage:
    def test_message_names_the_movement_and_re_run(self) -> None:
        message = implement._gate_failure_message(
            _result(flag=_flag()), attempt=1, max_retry=3, work_item_id="SA-X"
        )
        assert "moved while the suite executed" in message
        assert "/shared/checkout" in message
        assert "python3 scripts/implement.py finish SA-X" in message
        assert "not a code failure" in message

    def test_stable_red_message_unchanged(self) -> None:
        """Control: a genuine red run keeps the failure headline."""
        result = _result(failures=[{"test_name": "test_real.py::t"}])
        result["notice_codes"] = []
        message = implement._gate_failure_message(
            result, attempt=1, max_retry=3, work_item_id="SA-X"
        )
        assert "Test run 1/3 failed" in message
        assert "Failures:" in message
