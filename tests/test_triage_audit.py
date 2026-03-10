"""Integration and behaviour tests for the descriptor-driven triage-audit flow.

Refactored from the Scheduler-based tests to use the descriptor-driven audit
API (ampa.audit.handlers) directly.  All external side-effects are mocked:
wl, opencode run, and gh are never invoked as real processes.

Coverage:
1. Descriptor-driven handler integration:
   - AuditResultHandler: runs opencode, extracts structured report, posts comment.
   - CloseWithAuditHandler: closes item, sets needs-producer-review, sends Discord.
2. Audit poller (poll_and_handoff): candidate query, cooldown, handoff.
3. TriageAuditRunner: comment posting, gh auto-complete, Discord notification.
4. Utility helpers: _extract_audit_report, _extract_summary_from_report,
   _get_github_repo, _build_github_issue_url.
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
import re
from pathlib import Path
from typing import Any, Callable

import pytest

from ampa.audit.handlers import (
    AuditResultHandler,
    CloseWithAuditHandler,
    HandlerResult,
)
from ampa.audit.result import (
    AUDIT_REPORT_END,
    AUDIT_REPORT_START,
    AuditResult,
    CriterionResult,
)
from ampa.audit_poller import (
    PollerOutcome,
    poll_and_handoff,
)
from ampa.engine.descriptor import WorkflowDescriptor, load_descriptor
from ampa.engine.invariants import InvariantEvaluator, NullQuerier
from ampa.scheduler_store import SchedulerStore
from ampa.scheduler_types import CommandSpec
from ampa.triage_audit import (
    TriageAuditRunner,
    _extract_audit_report,
    _extract_summary_from_report,
    _get_github_repo,
    _build_github_issue_url,
)
from ampa import notifications


# ---------------------------------------------------------------------------
# Shared fixtures and helpers
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def descriptor() -> WorkflowDescriptor:
    """Load the real workflow descriptor."""
    return load_descriptor(
        REPO_ROOT / "docs" / "workflow" / "workflow.yaml",
        schema_path=REPO_ROOT / "docs" / "workflow" / "workflow-schema.json",
    )


@pytest.fixture
def evaluator(descriptor: WorkflowDescriptor) -> InvariantEvaluator:
    """Build evaluator from the real workflow descriptor."""
    return InvariantEvaluator(descriptor.invariants, querier=NullQuerier())


class DummyStore(SchedulerStore):
    """In-memory SchedulerStore for hermetic tests."""

    def __init__(self) -> None:
        self.path = ":memory:"
        self.data: dict[str, Any] = {
            "commands": {},
            "state": {},
            "last_global_start_ts": None,
            "config": {},
        }

    def save(self) -> None:
        return None


class MockUpdater:
    """Mock WorkItemUpdater."""

    def __init__(self, succeed: bool = True) -> None:
        self.calls: list[dict[str, Any]] = []
        self._succeed = succeed

    def update(
        self,
        work_item_id: str,
        *,
        status: str | None = None,
        stage: str | None = None,
        assignee: str | None = None,
    ) -> bool:
        self.calls.append(
            {
                "work_item_id": work_item_id,
                "status": status,
                "stage": stage,
                "assignee": assignee,
            }
        )
        return self._succeed


class MockCommentWriter:
    """Mock WorkItemCommentWriter."""

    def __init__(self, succeed: bool = True) -> None:
        self.calls: list[dict[str, Any]] = []
        self._succeed = succeed

    def write_comment(
        self, work_item_id: str, comment: str, author: str = "ampa-engine"
    ) -> bool:
        self.calls.append(
            {
                "work_item_id": work_item_id,
                "comment": comment,
                "author": author,
            }
        )
        return self._succeed


class MockFetcher:
    """Mock WorkItemFetcher."""

    def __init__(self, result: dict[str, Any] | None = None) -> None:
        self._result = result
        self.calls: list[str] = []

    def fetch(self, work_item_id: str) -> dict[str, Any] | None:
        self.calls.append(work_item_id)
        return self._result


class MockNotifier:
    """Mock NotificationSender."""

    def __init__(self, succeed: bool = True) -> None:
        self.calls: list[dict[str, Any]] = []
        self._succeed = succeed

    def send(self, message: str, *, title: str = "", level: str = "info") -> bool:
        self.calls.append({"message": message, "title": title, "level": level})
        return self._succeed


def _make_work_item(
    work_item_id: str = "TEST-001",
    title: str = "Test work item",
    status: str = "in_progress",
    stage: str = "in_review",
    tags: list[str] | None = None,
    comments: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Build a mock work item dict (wl show --json shape)."""
    return {
        "workItem": {
            "id": work_item_id,
            "title": title,
            "description": "Test description",
            "status": status,
            "stage": stage,
            "tags": tags or [],
            "assignee": "",
            "priority": "medium",
        },
        "comments": comments or [],
    }


def _make_structured_audit_output(
    recommends_closure: bool = True,
    work_item_id: str = "TEST-001",
) -> str:
    """Build a realistic structured audit output with report markers."""
    if recommends_closure:
        criteria = (
            "| 1 | Feature works | met | tests pass |\n"
            "| 2 | Documentation | met | README updated |"
        )
        recommendation = (
            "Can this item be closed? **Yes**. All acceptance criteria are met."
        )
    else:
        criteria = (
            "| 1 | Feature works | met | tests pass |\n"
            "| 2 | Documentation | unmet | README missing |"
        )
        recommendation = (
            "Can this item be closed? **No**. Documentation is missing."
        )

    return (
        "Some preamble noise from opencode...\n"
        f"{AUDIT_REPORT_START}\n"
        f"## Summary\n\n"
        f"Audit of {work_item_id}.\n\n"
        f"## Acceptance Criteria Status\n\n"
        f"| # | Criterion | Verdict | Evidence |\n"
        f"|---|-----------|---------|----------|\n"
        f"{criteria}\n\n"
        f"## Recommendation\n\n"
        f"{recommendation}\n"
        f"{AUDIT_REPORT_END}\n"
        "Trailing agent noise...\n"
    )


def _make_shell_for_opencode(
    work_item_id: str,
    audit_output: str,
    *,
    extra_responses: dict[str, subprocess.CompletedProcess] | None = None,
) -> Callable[..., subprocess.CompletedProcess]:
    """Return a run_shell mock that intercepts opencode run and optionally gh."""

    def run_shell(cmd: str, **kwargs: Any) -> subprocess.CompletedProcess:
        if f'opencode run "/audit {work_item_id}"' in cmd:
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=audit_output, stderr=""
            )
        if extra_responses:
            for prefix, result in extra_responses.items():
                if cmd.strip().startswith(prefix):
                    return result
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout='{"success": true}', stderr=""
        )

    return run_shell


# ---------------------------------------------------------------------------
# Section 1: Descriptor-driven handler integration
# ---------------------------------------------------------------------------


class TestAuditResultHandlerIntegration:
    """Integration tests for AuditResultHandler using the descriptor-driven API.

    Mocks: wl (updater, comment_writer, fetcher), opencode run (run_shell).
    No real processes are spawned.
    """

    def _make_handler(
        self,
        descriptor: WorkflowDescriptor,
        evaluator: InvariantEvaluator,
        *,
        work_item_id: str = "TEST-001",
        audit_output: str | None = None,
        recommends_closure: bool = True,
        comment_writer: MockCommentWriter | None = None,
        updater: MockUpdater | None = None,
        fetcher: MockFetcher | None = None,
    ) -> tuple[AuditResultHandler, MockCommentWriter, MockUpdater]:
        if comment_writer is None:
            comment_writer = MockCommentWriter()
        if updater is None:
            updater = MockUpdater()
        if fetcher is None:
            # Fetcher returns work item with audit comment (satisfies pre-invariant)
            fetcher = MockFetcher(
                _make_work_item(
                    work_item_id=work_item_id,
                    comments=[
                        {
                            "comment": (
                                "# AMPA Audit Result\n\n"
                                "Can this item be closed? Yes."
                            )
                        }
                    ],
                )
            )
        output = (
            audit_output
            if audit_output is not None
            else _make_structured_audit_output(recommends_closure, work_item_id)
        )
        run_shell = _make_shell_for_opencode(work_item_id, output)
        handler = AuditResultHandler(
            descriptor=descriptor,
            evaluator=evaluator,
            updater=updater,
            comment_writer=comment_writer,
            fetcher=fetcher,
            run_shell=run_shell,
        )
        return handler, comment_writer, updater

    def test_posts_structured_comment(
        self, descriptor: WorkflowDescriptor, evaluator: InvariantEvaluator
    ) -> None:
        """AuditResultHandler posts a comment with # AMPA Audit Result heading."""
        work_id = "AUDIT-COMMENT-001"
        handler, comment_writer, _ = self._make_handler(
            descriptor, evaluator, work_item_id=work_id
        )
        wi = _make_work_item(work_item_id=work_id, status="in_progress", stage="in_review")

        result = handler.execute(wi)

        assert result.success is True
        assert len(comment_writer.calls) == 1
        comment = comment_writer.calls[0]["comment"]
        assert "# AMPA Audit Result" in comment

    def test_comment_excludes_preamble_and_trailing_noise(
        self, descriptor: WorkflowDescriptor, evaluator: InvariantEvaluator
    ) -> None:
        """Comment body contains the structured report, not raw opencode preamble."""
        work_id = "AUDIT-NOISE-001"
        handler, comment_writer, _ = self._make_handler(
            descriptor, evaluator, work_item_id=work_id
        )
        wi = _make_work_item(work_item_id=work_id, status="in_progress", stage="in_review")

        handler.execute(wi)

        comment = comment_writer.calls[0]["comment"]
        assert "preamble noise" not in comment
        assert "Trailing agent noise" not in comment
        assert AUDIT_REPORT_START not in comment
        assert AUDIT_REPORT_END not in comment

    def test_comment_contains_acceptance_criteria(
        self, descriptor: WorkflowDescriptor, evaluator: InvariantEvaluator
    ) -> None:
        """Posted comment includes extracted acceptance criteria table."""
        work_id = "AUDIT-AC-001"
        handler, comment_writer, _ = self._make_handler(
            descriptor, evaluator, work_item_id=work_id
        )
        wi = _make_work_item(work_item_id=work_id, status="in_progress", stage="in_review")

        handler.execute(wi)

        comment = comment_writer.calls[0]["comment"]
        assert "Acceptance Criteria" in comment
        assert "Feature works" in comment

    def test_recommends_closure_transitions_to_audit_passed(
        self, descriptor: WorkflowDescriptor, evaluator: InvariantEvaluator
    ) -> None:
        """When audit recommends closure, handler transitions state to audit_passed."""
        work_id = "AUDIT-PASS-001"
        handler, _, updater = self._make_handler(
            descriptor, evaluator, work_item_id=work_id, recommends_closure=True
        )
        wi = _make_work_item(work_item_id=work_id, status="in_progress", stage="in_review")

        result = handler.execute(wi)

        assert result.success is True
        assert result.reason == "audit_result_recorded"
        assert len(updater.calls) == 1
        assert updater.calls[0]["stage"] == "audit_passed"

    def test_legacy_output_no_markers_still_posts_comment(
        self, descriptor: WorkflowDescriptor, evaluator: InvariantEvaluator
    ) -> None:
        """When opencode output has no markers, handler falls back and still posts comment."""
        work_id = "AUDIT-LEGACY-001"
        legacy_output = (
            "## Summary\n\nAudit complete. All criteria met.\n\n"
            "## Recommendation\n\nCan this item be closed? Yes.\n"
        )
        fetcher = MockFetcher(
            _make_work_item(
                work_item_id=work_id,
                comments=[
                    {
                        "comment": (
                            "# AMPA Audit Result\n\n"
                            "Can this item be closed? Yes."
                        )
                    }
                ],
            )
        )
        handler, comment_writer, _ = self._make_handler(
            descriptor,
            evaluator,
            work_item_id=work_id,
            audit_output=legacy_output,
            fetcher=fetcher,
        )
        wi = _make_work_item(work_item_id=work_id, status="in_progress", stage="in_review")

        result = handler.execute(wi)

        assert result.success is True
        assert len(comment_writer.calls) == 1
        assert "# AMPA Audit Result" in comment_writer.calls[0]["comment"]

    def test_opencode_empty_output_returns_parse_error(
        self, descriptor: WorkflowDescriptor, evaluator: InvariantEvaluator
    ) -> None:
        """When opencode returns empty output, handler returns parse error, no comment posted."""
        work_id = "AUDIT-EMPTY-001"
        handler, comment_writer, _ = self._make_handler(
            descriptor, evaluator, work_item_id=work_id, audit_output=""
        )
        wi = _make_work_item(work_item_id=work_id, status="in_progress", stage="in_review")

        result = handler.execute(wi)

        assert result.success is False
        assert result.reason == "audit_parse_error"
        assert len(comment_writer.calls) == 0

    def test_comment_does_not_contain_template_headings(
        self, descriptor: WorkflowDescriptor, evaluator: InvariantEvaluator
    ) -> None:
        """Posted comment does not contain template or lifecycle headings."""
        work_id = "AUDIT-TEMPLATE-001"
        handler, comment_writer, _ = self._make_handler(
            descriptor, evaluator, work_item_id=work_id
        )
        wi = _make_work_item(work_item_id=work_id, status="in_progress", stage="in_review")

        handler.execute(wi)

        comment = comment_writer.calls[0]["comment"]
        assert "## Intake" not in comment
        assert "## Plan" not in comment
        assert "Proposed child work items" not in comment

    def test_invalid_from_state_returns_failure(
        self, descriptor: WorkflowDescriptor, evaluator: InvariantEvaluator
    ) -> None:
        """Handler rejects work items not in in_review state."""
        handler, comment_writer, _ = self._make_handler(descriptor, evaluator)
        wi = _make_work_item(status="open", stage="idea")

        result = handler.execute(wi)

        assert result.success is False
        assert result.reason == "invalid_from_state"
        assert len(comment_writer.calls) == 0

    def test_no_wl_update_outside_state_transition(
        self, descriptor: WorkflowDescriptor, evaluator: InvariantEvaluator
    ) -> None:
        """AuditResultHandler only calls updater for the state transition, not extra updates."""
        work_id = "AUDIT-NOUPDATE-001"
        handler, _, updater = self._make_handler(
            descriptor, evaluator, work_item_id=work_id
        )
        wi = _make_work_item(work_item_id=work_id, status="in_progress", stage="in_review")

        handler.execute(wi)

        # Exactly one updater call (the state transition to audit_passed)
        assert len(updater.calls) == 1


class TestCloseWithAuditHandlerIntegration:
    """Integration tests for CloseWithAuditHandler using the descriptor-driven API.

    Mocks: wl (updater, notifier, run_shell), no real processes spawned.
    """

    def _make_handler(
        self,
        descriptor: WorkflowDescriptor,
        evaluator: InvariantEvaluator,
        *,
        updater: MockUpdater | None = None,
        notifier: MockNotifier | None = None,
    ) -> tuple[CloseWithAuditHandler, MockUpdater, MockNotifier]:
        if updater is None:
            updater = MockUpdater()
        if notifier is None:
            notifier = MockNotifier()
        handler = CloseWithAuditHandler(
            descriptor=descriptor,
            evaluator=evaluator,
            updater=updater,
            notifier=notifier,
            run_shell=lambda *a, **kw: subprocess.CompletedProcess(
                args="", returncode=0, stdout='{"success": true}', stderr=""
            ),
        )
        return handler, updater, notifier

    def _wi_audit_passed(
        self,
        work_item_id: str = "CLOSE-001",
        title: str = "Feature complete",
        *,
        recommends_closure: bool = True,
    ) -> dict[str, Any]:
        closure_text = "Yes" if recommends_closure else "No"
        return _make_work_item(
            work_item_id=work_item_id,
            title=title,
            status="completed",
            stage="audit_passed",
            comments=[
                {
                    "comment": (
                        f"# AMPA Audit Result\n\n"
                        f"Can this item be closed? {closure_text}. All criteria met."
                    )
                }
            ],
        )

    def test_auto_complete_transitions_state(
        self, descriptor: WorkflowDescriptor, evaluator: InvariantEvaluator
    ) -> None:
        """CloseWithAuditHandler transitions state when audit recommends closure."""
        handler, updater, _ = self._make_handler(descriptor, evaluator)
        wi = self._wi_audit_passed()

        result = handler.execute(wi)

        assert result.success is True
        assert result.reason == "close_with_audit_completed"
        assert len(updater.calls) == 1

    def test_auto_complete_sends_discord_notification(
        self, descriptor: WorkflowDescriptor, evaluator: InvariantEvaluator
    ) -> None:
        """CloseWithAuditHandler sends a Discord notification on success."""
        handler, _, notifier = self._make_handler(descriptor, evaluator)
        wi = self._wi_audit_passed(title="My Completed Feature")

        handler.execute(wi)

        assert len(notifier.calls) == 1
        assert "My Completed Feature" in notifier.calls[0]["message"]

    def test_no_closure_when_audit_does_not_recommend(
        self, descriptor: WorkflowDescriptor, evaluator: InvariantEvaluator
    ) -> None:
        """CloseWithAuditHandler fails when audit does not recommend closure."""
        handler, updater, _ = self._make_handler(descriptor, evaluator)
        wi = self._wi_audit_passed(recommends_closure=False)

        result = handler.execute(wi)

        assert result.success is False
        assert result.reason == "pre_invariant_failed"
        assert len(updater.calls) == 0

    def test_invalid_from_state_is_rejected(
        self, descriptor: WorkflowDescriptor, evaluator: InvariantEvaluator
    ) -> None:
        """CloseWithAuditHandler rejects work items not in audit_passed state."""
        handler, updater, _ = self._make_handler(descriptor, evaluator)
        wi = _make_work_item(status="in_progress", stage="in_review")

        result = handler.execute(wi)

        assert result.success is False
        assert result.reason == "invalid_from_state"
        assert len(updater.calls) == 0


# ---------------------------------------------------------------------------
# Section 2: Audit poller (poll_and_handoff) integration
# ---------------------------------------------------------------------------


def _make_wl_list_shell(
    items: list[dict[str, Any]],
) -> Callable[..., subprocess.CompletedProcess]:
    """Return a run_shell mock that returns the given items from wl list."""
    output = json.dumps({"workItems": items})

    def run_shell(cmd: str, **kwargs: Any) -> subprocess.CompletedProcess:
        if "wl list --stage in_review" in cmd:
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=output, stderr=""
            )
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    return run_shell


def test_poller_no_candidates_returns_no_candidates() -> None:
    """poll_and_handoff returns no_candidates when wl list returns empty list."""
    store = DummyStore()
    spec = CommandSpec(
        command_id="wl-triage-audit",
        command="true",
        requires_llm=False,
        frequency_minutes=1,
        priority=0,
        metadata={"audit_cooldown_hours": 0},
        command_type="triage-audit",
    )
    store.add_command(spec)
    handler_calls: list[dict[str, Any]] = []

    result = poll_and_handoff(
        run_shell=_make_wl_list_shell([]),
        cwd="/tmp",
        store=store,
        spec=spec,
        handler=lambda wi: handler_calls.append(wi) or True,
    )

    assert result.outcome == PollerOutcome.no_candidates
    assert result.selected_item_id is None
    assert len(handler_calls) == 0


def test_poller_only_queries_in_review_stage() -> None:
    """poll_and_handoff issues exactly one wl list --stage in_review query."""
    store = DummyStore()
    spec = CommandSpec(
        command_id="wl-triage-audit",
        command="true",
        requires_llm=False,
        frequency_minutes=1,
        priority=0,
        metadata={"audit_cooldown_hours": 0},
        command_type="triage-audit",
    )
    store.add_command(spec)
    shell_calls: list[str] = []

    def tracking_shell(cmd: str, **kwargs: Any) -> subprocess.CompletedProcess:
        shell_calls.append(cmd)
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout=json.dumps({"workItems": []}),
            stderr="",
        )

    poll_and_handoff(
        run_shell=tracking_shell,
        cwd="/tmp",
        store=store,
        spec=spec,
        handler=lambda wi: True,
    )

    assert all("in_review" in c for c in shell_calls if "wl" in c)
    assert not any("in_progress" in c for c in shell_calls)
    assert not any("blocked" in c for c in shell_calls)


def test_poller_cooldown_skips_recently_audited_item() -> None:
    """poll_and_handoff skips items audited within the cooldown window."""
    work_id = "WID-RECENT"
    now = dt.datetime.now(dt.timezone.utc)
    store = DummyStore()
    spec = CommandSpec(
        command_id="wl-triage-audit",
        command="true",
        requires_llm=False,
        frequency_minutes=1,
        priority=0,
        metadata={"audit_cooldown_hours": 6},
        command_type="triage-audit",
    )
    store.add_command(spec)
    # Persisted timestamp: 3 hours ago → within the 6-hour cooldown
    store.update_state(
        spec.command_id,
        {
            "last_audit_at_by_item": {
                work_id: (now - dt.timedelta(hours=3)).isoformat(),
            }
        },
    )
    handler_calls: list[dict[str, Any]] = []

    result = poll_and_handoff(
        run_shell=_make_wl_list_shell(
            [{"id": work_id, "title": "Recent item", "updated_at": now.isoformat()}]
        ),
        cwd="/tmp",
        store=store,
        spec=spec,
        handler=lambda wi: handler_calls.append(wi) or True,
        now=now,
    )

    assert result.outcome == PollerOutcome.no_candidates
    assert len(handler_calls) == 0


def test_poller_cooldown_audits_expired_item() -> None:
    """poll_and_handoff hands off items whose cooldown has expired."""
    work_id = "WID-FRESH"
    now = dt.datetime.now(dt.timezone.utc)
    store = DummyStore()
    spec = CommandSpec(
        command_id="wl-triage-audit",
        command="true",
        requires_llm=False,
        frequency_minutes=1,
        priority=0,
        metadata={"audit_cooldown_hours": 6},
        command_type="triage-audit",
    )
    store.add_command(spec)
    # Persisted timestamp: 7 hours ago → past the 6-hour cooldown
    store.update_state(
        spec.command_id,
        {
            "last_audit_at_by_item": {
                work_id: (now - dt.timedelta(hours=7)).isoformat(),
            }
        },
    )
    handler_calls: list[dict[str, Any]] = []

    result = poll_and_handoff(
        run_shell=_make_wl_list_shell(
            [
                {
                    "id": work_id,
                    "title": "Fresh item",
                    "updated_at": (now - dt.timedelta(hours=8)).isoformat(),
                }
            ]
        ),
        cwd="/tmp",
        store=store,
        spec=spec,
        handler=lambda wi: handler_calls.append(wi) or True,
        now=now,
    )

    assert result.outcome == PollerOutcome.handed_off
    assert result.selected_item_id == work_id
    assert len(handler_calls) == 1
    assert handler_calls[0]["id"] == work_id


def test_poller_query_failure_returns_query_failed() -> None:
    """poll_and_handoff returns query_failed when wl list exits non-zero."""
    store = DummyStore()
    spec = CommandSpec(
        command_id="wl-triage-audit",
        command="true",
        requires_llm=False,
        frequency_minutes=1,
        priority=0,
        metadata={"audit_cooldown_hours": 6},
        command_type="triage-audit",
    )
    store.add_command(spec)

    def failing_shell(cmd: str, **kwargs: Any) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(
            args=cmd, returncode=1, stdout="", stderr="connection refused"
        )

    result = poll_and_handoff(
        run_shell=failing_shell,
        cwd="/tmp",
        store=store,
        spec=spec,
        handler=lambda wi: True,
    )

    assert result.outcome == PollerOutcome.query_failed


def test_poller_logs_no_candidates(caplog: pytest.LogCaptureFixture) -> None:
    """poll_and_handoff logs an info message when no candidates are found."""
    store = DummyStore()
    spec = CommandSpec(
        command_id="wl-triage-audit",
        command="true",
        requires_llm=False,
        frequency_minutes=1,
        priority=0,
        metadata={"audit_cooldown_hours": 0},
        command_type="triage-audit",
    )
    store.add_command(spec)

    with caplog.at_level("INFO"):
        poll_and_handoff(
            run_shell=_make_wl_list_shell([]),
            cwd="/tmp",
            store=store,
            spec=spec,
            handler=lambda wi: True,
        )

    assert any(
        "no items" in msg.lower() or "no candidates" in msg.lower()
        for msg in caplog.messages
    )


def test_poller_selects_oldest_candidate() -> None:
    """poll_and_handoff selects the candidate with the oldest updated_at timestamp."""
    now = dt.datetime.now(dt.timezone.utc)
    older_id = "WID-OLDER"
    newer_id = "WID-NEWER"
    store = DummyStore()
    spec = CommandSpec(
        command_id="wl-triage-audit",
        command="true",
        requires_llm=False,
        frequency_minutes=1,
        priority=0,
        metadata={"audit_cooldown_hours": 0},
        command_type="triage-audit",
    )
    store.add_command(spec)
    handler_calls: list[dict[str, Any]] = []

    poll_and_handoff(
        run_shell=_make_wl_list_shell(
            [
                {
                    "id": newer_id,
                    "title": "Newer item",
                    "updated_at": (now - dt.timedelta(hours=1)).isoformat(),
                },
                {
                    "id": older_id,
                    "title": "Older item",
                    "updated_at": (now - dt.timedelta(hours=10)).isoformat(),
                },
            ]
        ),
        cwd="/tmp",
        store=store,
        spec=spec,
        handler=lambda wi: handler_calls.append(wi) or True,
        now=now,
    )

    assert len(handler_calls) == 1
    assert handler_calls[0]["id"] == older_id


# ---------------------------------------------------------------------------
# Section 3: TriageAuditRunner (gh auto-complete, comment posting, Discord)
# ---------------------------------------------------------------------------


def test_triage_audit_runner_requires_work_item() -> None:
    """TriageAuditRunner.run() raises TypeError when called without work_item."""
    runner = TriageAuditRunner(
        run_shell=lambda *a, **kw: subprocess.CompletedProcess("", 0),
        command_cwd="/tmp",
        store=DummyStore(),
    )
    spec = CommandSpec(
        command_id="wl-triage-audit",
        command="true",
        requires_llm=False,
        frequency_minutes=1,
        priority=0,
        metadata={},
        command_type="triage-audit",
    )
    with pytest.raises(TypeError, match="requires a pre-selected work_item"):
        runner.run(spec, None, None)


def test_triage_audit_runner_posts_comment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """TriageAuditRunner.run() calls opencode then posts a wl comment."""
    work_id = "RUNNER-COMMENT-001"
    calls: list[str] = []

    monkeypatch.setattr(notifications, "notify", lambda *a, **k: True)

    canned_output = _make_structured_audit_output(True, work_id)

    def fake_run_shell(cmd: str, **kwargs: Any) -> subprocess.CompletedProcess:
        calls.append(cmd)
        if f'opencode run "/audit {work_id}"' in cmd:
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=canned_output, stderr=""
            )
        if f"wl show {work_id}" in cmd:
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=json.dumps({}), stderr=""
            )
        if f"wl comment list {work_id}" in cmd:
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=json.dumps({"comments": []}), stderr=""
            )
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout='{"success": true}', stderr=""
        )

    runner = TriageAuditRunner(
        run_shell=fake_run_shell,
        command_cwd=str(tmp_path),
        store=DummyStore(),
    )
    spec = CommandSpec(
        command_id="wl-triage-audit",
        command="true",
        requires_llm=False,
        frequency_minutes=1,
        priority=0,
        metadata={"truncate_chars": 65536, "audit_cooldown_hours": 0},
        command_type="triage-audit",
    )

    result = runner.run(spec, None, None, work_item={"id": work_id, "title": "Test item"})

    assert result is True
    assert any(f'opencode run "/audit {work_id}"' in c for c in calls)
    assert any(f"wl comment add {work_id}" in c for c in calls)


def test_triage_audit_runner_comment_contains_ampa_heading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TriageAuditRunner posts a comment with # AMPA Audit Result heading."""
    work_id = "RUNNER-HEADING-001"
    comment_content: dict[str, str] = {"text": ""}

    monkeypatch.setattr(notifications, "notify", lambda *a, **k: True)

    canned_output = _make_structured_audit_output(True, work_id)

    def fake_run_shell(cmd: str, **kwargs: Any) -> subprocess.CompletedProcess:
        if f'opencode run "/audit {work_id}"' in cmd:
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=canned_output, stderr=""
            )
        if f"wl comment add {work_id}" in cmd:
            m = re.search(r"cat '([^']+)'", cmd)
            if m:
                try:
                    with open(m.group(1), encoding="utf-8") as fh:
                        comment_content["text"] = fh.read()
                except OSError:
                    pass
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout='{"success": true}', stderr=""
            )
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout='{"success": true}', stderr=""
        )

    runner = TriageAuditRunner(
        run_shell=fake_run_shell,
        command_cwd=str(tmp_path),
        store=DummyStore(),
    )
    spec = CommandSpec(
        command_id="wl-triage-audit",
        command="true",
        requires_llm=False,
        frequency_minutes=1,
        priority=0,
        metadata={"truncate_chars": 65536, "audit_cooldown_hours": 0},
        command_type="triage-audit",
    )

    runner.run(spec, None, None, work_item={"id": work_id, "title": "Test item"})

    assert "# AMPA Audit Result" in comment_content["text"]


def test_triage_audit_runner_structured_report_not_raw_noise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Comment posted by TriageAuditRunner contains structured report, not preamble noise."""
    work_id = "RUNNER-STRUCT-001"
    comment_content: dict[str, str] = {"text": ""}

    monkeypatch.setattr(notifications, "notify", lambda *a, **k: True)

    canned_output = (
        "Some preamble noise from the agent stdout\n"
        f"{AUDIT_REPORT_START}\n"
        "## Summary\n\n"
        "All 3 acceptance criteria are met.\n\n"
        "## Acceptance Criteria Status\n\n"
        "| # | Criterion | Verdict | Evidence |\n"
        "|---|-----------|---------|----------|\n"
        "| 1 | Widget renders | met | src/widget.tsx:15 |\n"
        "| 2 | API returns 200 | met | src/api.ts:42 |\n"
        "| 3 | Tests pass | met | tests/widget.test.ts:8 |\n\n"
        "## Recommendation\n\n"
        "This item can be closed: all acceptance criteria are met.\n"
        f"{AUDIT_REPORT_END}\n"
        "trailing agent noise\n"
    )

    def fake_run_shell(cmd: str, **kwargs: Any) -> subprocess.CompletedProcess:
        if f'opencode run "/audit {work_id}"' in cmd:
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=canned_output, stderr=""
            )
        if f"wl comment add {work_id}" in cmd:
            m = re.search(r"cat '([^']+)'", cmd)
            if m:
                try:
                    with open(m.group(1), encoding="utf-8") as fh:
                        comment_content["text"] = fh.read()
                except OSError:
                    pass
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout='{"success": true}', stderr=""
            )
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout='{"success": true}', stderr=""
        )

    runner = TriageAuditRunner(
        run_shell=fake_run_shell,
        command_cwd=str(tmp_path),
        store=DummyStore(),
    )
    spec = CommandSpec(
        command_id="wl-triage-audit",
        command="true",
        requires_llm=False,
        frequency_minutes=1,
        priority=0,
        metadata={"truncate_chars": 65536, "audit_cooldown_hours": 0},
        command_type="triage-audit",
    )

    runner.run(spec, None, None, work_item={"id": work_id, "title": "Test"})

    text = comment_content["text"]
    assert "# AMPA Audit Result" in text
    assert "## Summary" in text
    assert "All 3 acceptance criteria are met." in text
    assert "Widget renders" in text
    assert "preamble noise" not in text
    assert "trailing agent noise" not in text
    assert AUDIT_REPORT_START not in text
    assert AUDIT_REPORT_END not in text


def test_triage_audit_runner_comment_no_template_headings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TriageAuditRunner comment does not include lifecycle template headings."""
    work_id = "RUNNER-NOTEMP-001"
    comment_content: dict[str, str] = {"text": ""}

    monkeypatch.setattr(notifications, "notify", lambda *a, **k: True)

    def fake_run_shell(cmd: str, **kwargs: Any) -> subprocess.CompletedProcess:
        if f'opencode run "/audit {work_id}"' in cmd:
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="Summary:\nAudit only output\n", stderr=""
            )
        if f"wl comment add {work_id}" in cmd:
            m = re.search(r"cat '([^']+)'", cmd)
            if m:
                try:
                    with open(m.group(1), encoding="utf-8") as fh:
                        comment_content["text"] = fh.read()
                except OSError:
                    pass
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout='{"success": true}', stderr=""
            )
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout='{"success": true}', stderr=""
        )

    runner = TriageAuditRunner(
        run_shell=fake_run_shell,
        command_cwd=str(tmp_path),
        store=DummyStore(),
    )
    spec = CommandSpec(
        command_id="wl-triage-audit",
        command="true",
        requires_llm=False,
        frequency_minutes=1,
        priority=0,
        metadata={"truncate_chars": 65536, "audit_cooldown_hours": 0},
        command_type="triage-audit",
    )

    runner.run(spec, None, None, work_item={"id": work_id, "title": "Test"})

    text = comment_content["text"]
    assert "Proposed child work items" not in text
    assert "## Intake" not in text
    assert "## Plan" not in text


def test_triage_audit_auto_complete_with_gh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TriageAuditRunner auto-completes (wl update) when gh confirms PR merged."""
    work_id = "RUNNER-GH-001"
    calls: list[str] = []

    monkeypatch.setattr(notifications, "notify", lambda *a, **k: True)

    def fake_run_shell(cmd: str, **kwargs: Any) -> subprocess.CompletedProcess:
        calls.append(cmd)
        if f'opencode run "/audit {work_id}"' in cmd:
            out = (
                "Summary:\nPR merged: https://github.com/example/repo/pull/42\n\n"
                "Details: ready to close"
            )
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=out, stderr=""
            )
        if cmd.strip().startswith("gh pr view"):
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=json.dumps({"merged": True}), stderr=""
            )
        if f"wl show {work_id}" in cmd:
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=json.dumps({}), stderr=""
            )
        if f"wl comment add {work_id}" in cmd:
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout='{"success": true}', stderr=""
            )
        if f"wl update {work_id}" in cmd:
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout='{"success": true}', stderr=""
            )
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setenv("AMPA_VERIFY_PR_WITH_GH", "1")

    runner = TriageAuditRunner(
        run_shell=fake_run_shell,
        command_cwd=str(tmp_path),
        store=DummyStore(),
    )
    spec = CommandSpec(
        command_id="wl-triage-audit",
        command="true",
        requires_llm=False,
        frequency_minutes=1,
        priority=0,
        metadata={
            "truncate_chars": 65536,
            "audit_cooldown_hours": 0,
            "verify_pr_with_gh": True,
        },
        command_type="triage-audit",
    )

    runner.run(spec, None, None, work_item={"id": work_id, "title": "PR item"})

    assert any(c.strip().startswith("gh pr view") for c in calls)
    assert any(f"wl update {work_id}" in c for c in calls)
    update_cmds = [c for c in calls if f"wl update {work_id}" in c]
    assert any("--needs-producer-review true" in c for c in update_cmds), (
        f"Expected --needs-producer-review true in update command, got: {update_cmds}"
    )


def test_triage_audit_runner_discord_notification_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TriageAuditRunner Discord notification includes Work Item ID, title, and summary."""
    work_id = "RUNNER-DISCORD-001"
    captured: dict[str, Any] = {}

    wl_dir = tmp_path / ".worklog"
    wl_dir.mkdir(parents=True, exist_ok=True)
    (wl_dir / "config.yaml").write_text("githubRepo: TestOwner/TestRepo\n")

    def fake_notify(title: str, body: str = "", message_type: str = "other", *, payload: Any = None) -> bool:
        captured["title"] = title
        captured["payload"] = payload
        return True

    monkeypatch.setattr(notifications, "notify", fake_notify)

    def fake_run_shell(cmd: str, **kwargs: Any) -> subprocess.CompletedProcess:
        if f'opencode run "/audit {work_id}"' in cmd:
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout="Summary:\nA short summary for Discord.\n",
                stderr="",
            )
        if f"wl show {work_id}" in cmd:
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout=json.dumps({"workItem": {"githubIssueNumber": 42}}),
                stderr="",
            )
        if f"wl comment list {work_id}" in cmd:
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=json.dumps({"comments": []}), stderr=""
            )
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout='{"success": true}', stderr=""
        )

    monkeypatch.setenv("AMPA_DISCORD_BOT_TOKEN", "test-token")

    runner = TriageAuditRunner(
        run_shell=fake_run_shell,
        command_cwd=str(tmp_path),
        store=DummyStore(),
    )
    spec = CommandSpec(
        command_id="wl-triage-audit",
        command="true",
        requires_llm=False,
        frequency_minutes=1,
        priority=0,
        metadata={"truncate_chars": 65536, "audit_cooldown_hours": 0},
        command_type="triage-audit",
    )

    runner.run(spec, None, None, work_item={"id": work_id, "title": "Discord summary item"})

    content = captured.get("payload", {}).get("content", "")
    assert "# Triage Audit — Discord summary item" in content
    assert "Summary: A short summary for Discord." in content
    assert f"Work Item: {work_id}" in content
    assert "GitHub: https://github.com/TestOwner/TestRepo/issues/42" in content


# ---------------------------------------------------------------------------
# Section 4: Utility helper tests (_extract_audit_report, _extract_summary_from_report,
#             _get_github_repo, _build_github_issue_url)
# ---------------------------------------------------------------------------

# --- _get_github_repo / _build_github_issue_url ---


def test_get_github_repo_happy_path(tmp_path):
    """Reads githubRepo from .worklog/config.yaml."""
    wl_dir = tmp_path / ".worklog"
    wl_dir.mkdir()
    (wl_dir / "config.yaml").write_text("githubRepo: MyOrg/MyRepo\n")
    assert _get_github_repo(str(tmp_path)) == "MyOrg/MyRepo"


def test_get_github_repo_missing_file(tmp_path):
    """Returns None when config.yaml does not exist."""
    assert _get_github_repo(str(tmp_path)) is None


def test_get_github_repo_missing_key(tmp_path):
    """Returns None when githubRepo key is absent."""
    wl_dir = tmp_path / ".worklog"
    wl_dir.mkdir()
    (wl_dir / "config.yaml").write_text("someOtherKey: value\n")
    assert _get_github_repo(str(tmp_path)) is None


def test_get_github_repo_not_set(tmp_path):
    """Returns None when githubRepo is '(not set)'."""
    wl_dir = tmp_path / ".worklog"
    wl_dir.mkdir()
    (wl_dir / "config.yaml").write_text("githubRepo: (not set)\n")
    assert _get_github_repo(str(tmp_path)) is None


def test_get_github_repo_none_cwd():
    """Returns None gracefully when command_cwd is None and ./worklog doesn't exist."""
    # This should not raise — it should return None
    result = _get_github_repo(None)
    # Result depends on whether ./.worklog/config.yaml exists in the cwd
    assert result is None or isinstance(result, str)


def test_build_github_issue_url_happy_path():
    """Builds correct URL from repo slug and issue number."""
    assert (
        _build_github_issue_url("MyOrg/MyRepo", 42)
        == "https://github.com/MyOrg/MyRepo/issues/42"
    )


def test_build_github_issue_url_string_number():
    """Accepts issue number as a string."""
    assert (
        _build_github_issue_url("MyOrg/MyRepo", "7")
        == "https://github.com/MyOrg/MyRepo/issues/7"
    )


def test_build_github_issue_url_none_repo():
    """Returns None when repo_slug is None."""
    assert _build_github_issue_url(None, 42) is None


def test_build_github_issue_url_none_number():
    """Returns None when issue_number is None."""
    assert _build_github_issue_url("MyOrg/MyRepo", None) is None


def test_build_github_issue_url_invalid_number():
    """Returns None when issue_number is not a valid integer."""
    assert _build_github_issue_url("MyOrg/MyRepo", "not-a-number") is None


def test_build_github_issue_url_zero():
    """Returns None when issue_number is 0 (falsy)."""
    assert _build_github_issue_url("MyOrg/MyRepo", 0) is None


# ---------------------------------------------------------------------------
# _extract_audit_report tests
# ---------------------------------------------------------------------------


def test_extract_audit_report_happy_path():
    """Extracts content between start and end markers."""
    raw = (
        "Some preamble noise from the agent\n"
        "--- AUDIT REPORT START ---\n"
        "## Summary\n"
        "\n"
        "Everything looks great.\n"
        "\n"
        "## Recommendation\n"
        "\n"
        "This item can be closed.\n"
        "--- AUDIT REPORT END ---\n"
        "trailing noise\n"
    )
    result = _extract_audit_report(raw)
    assert result.startswith("## Summary")
    assert "Everything looks great." in result
    assert "This item can be closed." in result
    assert "--- AUDIT REPORT START ---" not in result
    assert "--- AUDIT REPORT END ---" not in result
    assert "preamble" not in result
    assert "trailing noise" not in result


def test_extract_audit_report_missing_start_marker(caplog):
    """Falls back to full output when start marker is missing."""
    raw = "No markers here, just plain audit output."
    with caplog.at_level("WARNING"):
        result = _extract_audit_report(raw)
    assert result == raw
    assert any("missing start marker" in m.lower() for m in caplog.messages)


def test_extract_audit_report_missing_end_marker(caplog):
    """Uses content after start marker when end marker is missing."""
    raw = (
        "preamble\n--- AUDIT REPORT START ---\n## Summary\n\nThe end marker was lost.\n"
    )
    with caplog.at_level("WARNING"):
        result = _extract_audit_report(raw)
    assert "The end marker was lost." in result
    assert "## Summary" in result
    assert "preamble" not in result
    assert any("missing end marker" in m.lower() for m in caplog.messages)


def test_extract_audit_report_empty_content(caplog):
    """Falls back to full output when content between markers is empty."""
    raw = "--- AUDIT REPORT START ---\n--- AUDIT REPORT END ---\n"
    with caplog.at_level("WARNING"):
        result = _extract_audit_report(raw)
    assert result == raw
    assert any("empty" in m.lower() for m in caplog.messages)


def test_extract_audit_report_whitespace_only_content(caplog):
    """Falls back to full output when content between markers is whitespace-only."""
    raw = "--- AUDIT REPORT START ---\n   \n\n--- AUDIT REPORT END ---\n"
    with caplog.at_level("WARNING"):
        result = _extract_audit_report(raw)
    assert result == raw
    assert any("empty" in m.lower() for m in caplog.messages)


def test_extract_audit_report_multiple_marker_pairs():
    """Only the first pair of markers is used."""
    raw = (
        "--- AUDIT REPORT START ---\n"
        "First report.\n"
        "--- AUDIT REPORT END ---\n"
        "--- AUDIT REPORT START ---\n"
        "Second report.\n"
        "--- AUDIT REPORT END ---\n"
    )
    result = _extract_audit_report(raw)
    assert result == "First report."
    assert "Second report." not in result


def test_extract_audit_report_empty_input():
    """Returns empty string for empty input."""
    assert _extract_audit_report("") == ""
    assert _extract_audit_report(None) == ""  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# _extract_summary_from_report tests
# ---------------------------------------------------------------------------


def test_extract_summary_from_report_happy_path():
    """Extracts the Summary section from a structured report."""
    report = (
        "## Summary\n"
        "\n"
        "Everything looks great. All criteria are met.\n"
        "\n"
        "## Acceptance Criteria Status\n"
        "\n"
        "| # | Criterion | Verdict |\n"
    )
    result = _extract_summary_from_report(report)
    assert result == "Everything looks great. All criteria are met."


def test_extract_summary_from_report_no_heading():
    """Returns empty string when no ## Summary heading exists."""
    report = "## Acceptance Criteria Status\n\nSome table here.\n"
    assert _extract_summary_from_report(report) == ""


def test_extract_summary_from_report_summary_at_end():
    """Extracts summary when it is the last section."""
    report = "## Summary\n\nFinal summary with no sections after it.\n"
    result = _extract_summary_from_report(report)
    assert result == "Final summary with no sections after it."


def test_extract_summary_from_report_empty_input():
    """Returns empty string for empty input."""
    assert _extract_summary_from_report("") == ""


def test_extract_summary_from_report_multiline():
    """Extracts multi-line summary content."""
    report = (
        "## Summary\n"
        "\n"
        "Line one of the summary.\n"
        "Line two continues.\n"
        "\n"
        "Another paragraph in summary.\n"
        "\n"
        "## Recommendation\n"
        "\n"
        "Close it.\n"
    )
    result = _extract_summary_from_report(report)
    assert "Line one of the summary." in result
    assert "Line two continues." in result
    assert "Another paragraph in summary." in result
    assert "Close it." not in result
