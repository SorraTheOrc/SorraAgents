"""Unit tests for the audit poller protocol and result types.

Work item: SA-0MM2FCXG11VU3CV3
"""

from __future__ import annotations

from typing import Any, Dict

from ampa.audit_poller import AuditHandoffHandler, PollerOutcome, PollerResult


# ---------------------------------------------------------------------------
# PollerOutcome
# ---------------------------------------------------------------------------


class TestPollerOutcome:
    def test_enum_members(self) -> None:
        assert PollerOutcome.no_candidates.value == "no_candidates"
        assert PollerOutcome.handed_off.value == "handed_off"
        assert PollerOutcome.query_failed.value == "query_failed"

    def test_enum_has_exactly_three_members(self) -> None:
        assert len(PollerOutcome) == 3


# ---------------------------------------------------------------------------
# PollerResult
# ---------------------------------------------------------------------------


class TestPollerResult:
    def test_no_candidates_result(self) -> None:
        result = PollerResult(outcome=PollerOutcome.no_candidates)
        assert result.outcome is PollerOutcome.no_candidates
        assert result.selected_item_id is None
        assert result.error is None

    def test_handed_off_result(self) -> None:
        result = PollerResult(
            outcome=PollerOutcome.handed_off,
            selected_item_id="WL-123",
        )
        assert result.outcome is PollerOutcome.handed_off
        assert result.selected_item_id == "WL-123"
        assert result.error is None

    def test_query_failed_result(self) -> None:
        result = PollerResult(
            outcome=PollerOutcome.query_failed,
            error="non-zero exit code: 1",
        )
        assert result.outcome is PollerOutcome.query_failed
        assert result.selected_item_id is None
        assert result.error == "non-zero exit code: 1"

    def test_frozen(self) -> None:
        result = PollerResult(outcome=PollerOutcome.no_candidates)
        try:
            result.outcome = PollerOutcome.handed_off  # type: ignore[misc]
            assert False, "Expected FrozenInstanceError"
        except AttributeError:
            pass


# ---------------------------------------------------------------------------
# AuditHandoffHandler protocol
# ---------------------------------------------------------------------------


class _ValidHandler:
    """A handler that satisfies the AuditHandoffHandler protocol."""

    def __call__(self, work_item: Dict[str, Any]) -> bool:
        return True


class _InvalidHandler:
    """A handler missing the required __call__ signature."""

    def do_audit(self, work_item: Dict[str, Any]) -> bool:
        return True


def _valid_function_handler(work_item: Dict[str, Any]) -> bool:
    """A bare function also satisfies the protocol."""
    return False


class TestAuditHandoffHandler:
    def test_class_satisfies_protocol(self) -> None:
        handler = _ValidHandler()
        assert isinstance(handler, AuditHandoffHandler)

    def test_function_satisfies_protocol(self) -> None:
        assert isinstance(_valid_function_handler, AuditHandoffHandler)

    def test_lambda_satisfies_protocol(self) -> None:
        handler = lambda work_item: True  # noqa: E731
        assert isinstance(handler, AuditHandoffHandler)

    def test_class_without_call_does_not_satisfy_protocol(self) -> None:
        handler = _InvalidHandler()
        assert not isinstance(handler, AuditHandoffHandler)

    def test_handler_can_be_called(self) -> None:
        handler: AuditHandoffHandler = _ValidHandler()
        item = {
            "id": "WL-1",
            "title": "Test",
            "status": "in-progress",
            "stage": "in_review",
        }
        assert handler(item) is True

    def test_handler_receives_work_item_dict(self) -> None:
        received: list[Dict[str, Any]] = []

        def capturing_handler(work_item: Dict[str, Any]) -> bool:
            received.append(work_item)
            return True

        item = {"id": "WL-42", "title": "Check", "stage": "in_review"}
        capturing_handler(item)
        assert len(received) == 1
        assert received[0] is item
