"""Unit tests for the audit poller protocol, result types, and query logic.

Work items: SA-0MM2FCXG11VU3CV3, SA-0MM2FD8O70OBNOI6
"""

from __future__ import annotations

import json
import subprocess
from typing import Any, Dict

from ampa.audit_poller import (
    AuditHandoffHandler,
    PollerOutcome,
    PollerResult,
    _query_candidates,
)


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


# ---------------------------------------------------------------------------
# _query_candidates
# ---------------------------------------------------------------------------


def _make_proc(stdout: str = "", returncode: int = 0, stderr: str = ""):
    """Helper to build a subprocess.CompletedProcess for tests."""
    return subprocess.CompletedProcess(
        args="wl list --stage in_review --json",
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


class TestQueryCandidates:
    def test_empty_list_response(self) -> None:
        def run_shell(cmd, **kw):
            return _make_proc(stdout="[]")

        result = _query_candidates(run_shell, "/tmp")
        assert result == []

    def test_list_response(self) -> None:
        items = [
            {"id": "WL-1", "title": "Item 1", "stage": "in_review"},
            {"id": "WL-2", "title": "Item 2", "stage": "in_review"},
        ]

        def run_shell(cmd, **kw):
            return _make_proc(stdout=json.dumps(items))

        result = _query_candidates(run_shell, "/tmp")
        assert len(result) == 2
        assert result[0]["id"] == "WL-1"
        assert result[1]["id"] == "WL-2"

    def test_dict_workItems_response(self) -> None:
        payload = {
            "workItems": [
                {"id": "WL-1", "title": "Item 1"},
                {"id": "WL-2", "title": "Item 2"},
            ]
        }

        def run_shell(cmd, **kw):
            return _make_proc(stdout=json.dumps(payload))

        result = _query_candidates(run_shell, "/tmp")
        assert len(result) == 2

    def test_dict_items_response(self) -> None:
        payload = {"items": [{"id": "WL-10", "title": "A"}]}

        def run_shell(cmd, **kw):
            return _make_proc(stdout=json.dumps(payload))

        result = _query_candidates(run_shell, "/tmp")
        assert len(result) == 1
        assert result[0]["id"] == "WL-10"

    def test_dict_data_response(self) -> None:
        payload = {"data": [{"id": "WL-20", "title": "B"}]}

        def run_shell(cmd, **kw):
            return _make_proc(stdout=json.dumps(payload))

        result = _query_candidates(run_shell, "/tmp")
        assert len(result) == 1
        assert result[0]["id"] == "WL-20"

    def test_dict_work_items_response(self) -> None:
        payload = {"work_items": [{"id": "WL-30", "title": "C"}]}

        def run_shell(cmd, **kw):
            return _make_proc(stdout=json.dumps(payload))

        result = _query_candidates(run_shell, "/tmp")
        assert len(result) == 1
        assert result[0]["id"] == "WL-30"

    def test_dict_fallback_workitems_key(self) -> None:
        payload = {"allWorkItems": [{"id": "WL-40", "title": "D"}]}

        def run_shell(cmd, **kw):
            return _make_proc(stdout=json.dumps(payload))

        result = _query_candidates(run_shell, "/tmp")
        assert len(result) == 1
        assert result[0]["id"] == "WL-40"

    def test_deduplicates_by_id(self) -> None:
        items = [
            {"id": "WL-1", "title": "First"},
            {"id": "WL-1", "title": "Duplicate"},
            {"id": "WL-2", "title": "Second"},
        ]

        def run_shell(cmd, **kw):
            return _make_proc(stdout=json.dumps(items))

        result = _query_candidates(run_shell, "/tmp")
        assert len(result) == 2
        ids = {r["id"] for r in result}
        assert ids == {"WL-1", "WL-2"}

    def test_work_item_id_key(self) -> None:
        """Items using 'work_item_id' key are normalised to 'id'."""
        items = [{"work_item_id": "WL-50", "title": "E"}]

        def run_shell(cmd, **kw):
            return _make_proc(stdout=json.dumps(items))

        result = _query_candidates(run_shell, "/tmp")
        assert len(result) == 1
        assert result[0]["id"] == "WL-50"

    def test_work_item_key(self) -> None:
        """Items using 'work_item' key are normalised to 'id'."""
        items = [{"work_item": "WL-60", "title": "F"}]

        def run_shell(cmd, **kw):
            return _make_proc(stdout=json.dumps(items))

        result = _query_candidates(run_shell, "/tmp")
        assert len(result) == 1
        assert result[0]["id"] == "WL-60"

    def test_items_without_id_are_dropped(self) -> None:
        items = [
            {"id": "WL-1", "title": "Has ID"},
            {"title": "No ID"},
        ]

        def run_shell(cmd, **kw):
            return _make_proc(stdout=json.dumps(items))

        result = _query_candidates(run_shell, "/tmp")
        assert len(result) == 1
        assert result[0]["id"] == "WL-1"

    def test_non_zero_exit_code_returns_empty(self) -> None:
        def run_shell(cmd, **kw):
            return _make_proc(returncode=1, stderr="error")

        result = _query_candidates(run_shell, "/tmp")
        assert result == []

    def test_invalid_json_returns_empty(self) -> None:
        def run_shell(cmd, **kw):
            return _make_proc(stdout="not json at all")

        result = _query_candidates(run_shell, "/tmp")
        assert result == []

    def test_null_json_returns_empty(self) -> None:
        def run_shell(cmd, **kw):
            return _make_proc(stdout="null")

        result = _query_candidates(run_shell, "/tmp")
        assert result == []

    def test_empty_string_stdout_returns_empty(self) -> None:
        def run_shell(cmd, **kw):
            return _make_proc(stdout="")

        result = _query_candidates(run_shell, "/tmp")
        assert result == []

    def test_run_shell_exception_returns_empty(self) -> None:
        def run_shell(cmd, **kw):
            raise OSError("connection refused")

        result = _query_candidates(run_shell, "/tmp")
        assert result == []

    def test_passes_cwd_and_timeout(self) -> None:
        received_kwargs: list[dict] = []

        def run_shell(cmd, **kw):
            received_kwargs.append(kw)
            return _make_proc(stdout="[]")

        _query_candidates(run_shell, "/my/project", timeout=42)
        assert len(received_kwargs) == 1
        assert received_kwargs[0]["cwd"] == "/my/project"
        assert received_kwargs[0]["timeout"] == 42
