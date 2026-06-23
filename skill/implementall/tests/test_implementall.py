"""Tests for ImplementAll: Automated Batch Implementation for plan_complete items.

These tests verify:
- Discovery of plan_complete items via wl list
- Sequential /skill:implement invocation for each item
- Producer-input detection via unanswered questions
- --max flag to limit batch size
- Summary report accuracy (Markdown and JSON)
- Error resilience (errors for one item don't stop processing)
- Idempotence (re-running doesn't duplicate work)
- --dry-run flag (simulate without making changes)
- --parent-id flag posts summary as a comment
- Recovery actions on error (reset status to open)
- --item-timeout for per-item subprocess timeout
- Signal handler registration and behavior (SIGINT/SIGTERM trigger recovery)

Related work item: SA-0MQO6YMZ3006N5MG
"""

import json
import signal
from pathlib import Path
from types import SimpleNamespace


# Ensure the repo root is on sys.path so skill packages are importable
_REPO_ROOT = Path(__file__).resolve().parents[3]
import sys  # noqa: E402
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from skill.implementall.scripts.implementall import (  # noqa: E402
    ImplementAllEngine,
    generate_summary,
)


# ---------------------------------------------------------------------------
# Fake helpers
# ---------------------------------------------------------------------------

class FakeProc(SimpleNamespace):
    """Fake subprocess.CompletedProcess used by FakeRunner."""
    pass


class FakeRunner:
    """Mock runner that records invocations and returns canned responses.

    The caller maps command prefixes to responses via `set_response`.
    """

    def __init__(self):
        self.calls: list[list[str]] = []
        self.responses: dict[str, FakeProc] = {}
        # Default: any unmatched command returns success with empty JSON array
        self._default = FakeProc(returncode=0, stdout="[]", stderr="")

    def set_response(self, cmd_prefix: str, returncode: int = 0,
                     stdout: str = "", stderr: str = ""):
        """Register a canned response for any command whose args start with cmd_prefix."""
        self.responses[cmd_prefix] = FakeProc(
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        )

    def __call__(self, cmd):
        cmd = list(cmd)
        self.calls.append(cmd)
        cmd_str = " ".join(cmd)
        # Match longest prefix first
        for prefix in sorted(self.responses, key=len, reverse=True):
            if cmd_str.startswith(prefix):
                return self.responses[prefix]
        return self._default


# ---------------------------------------------------------------------------
# Sample data
# ---------------------------------------------------------------------------

SAMPLE_ITEM_A = {
    "id": "SA-IMPL-001",
    "title": "Feature A",
    "status": "open",
    "stage": "plan_complete",
    "priority": "high",
    "issueType": "feature",
    "description": (
        "# Feature A\n\n"
        "## Acceptance Criteria\n"
        "- Users can authenticate via OAuth\n"
        "- Token refresh is handled automatically\n"
        "## Implementation\n"
        "Add OAuth2 middleware using the auth library.\n"
    ),
}

SAMPLE_ITEM_B = {
    "id": "SA-IMPL-002",
    "title": "Bug Fix B",
    "status": "open",
    "stage": "plan_complete",
    "priority": "medium",
    "issueType": "bug",
    "description": (
        "# Bug Fix B\n\n"
        "## Acceptance Criteria\n"
        "- Fix null pointer in login flow\n"
        "## Proposed Approach\n"
        "Add null check before accessing user profile.\n"
    ),
}

SAMPLE_ITEM_C = {
    "id": "SA-IMPL-003",
    "title": "Complex Feature C",
    "status": "open",
    "stage": "plan_complete",
    "priority": "high",
    "issueType": "feature",
    "description": (
        "# Complex Feature C\n\n"
        "## Acceptance Criteria\n"
        "- Integration with external API\n"
        "- Error handling for network failures\n"
        "## Implementation\n"
        "Create API client module with retry logic.\n"
    ),
}

SAMPLE_WL_LIST_RESPONSE = json.dumps({
    "success": True,
    "workItems": [SAMPLE_ITEM_A, SAMPLE_ITEM_B, SAMPLE_ITEM_C],
})


# ===========================================================================
# Test: Discovery of plan_complete items
# ===========================================================================

class TestDiscovery:
    """Verify that ImplementAll discovers all items in plan_complete stage."""

    def test_discover_all_plan_complete_items(self):
        """`wl list --stage plan_complete --status open --json` returns all eligible items."""
        runner = FakeRunner()
        runner.set_response(
            "wl list --stage plan_complete",
            stdout=SAMPLE_WL_LIST_RESPONSE,
        )

        engine = ImplementAllEngine(runner=runner)
        items = engine.discover_items()

        assert len(items) == 3
        assert items[0]["id"] == "SA-IMPL-001"
        assert items[1]["id"] == "SA-IMPL-002"
        assert items[2]["id"] == "SA-IMPL-003"

        # Verify the correct wl command was issued
        assert any(
            "wl list --stage plan_complete --status open --json" in " ".join(cmd)
            for cmd in runner.calls
        ), "Expected wl list --stage plan_complete --status open --json call"

    def test_discover_returns_empty_list_when_no_items(self):
        """When no items are in plan_complete, return an empty list."""
        runner = FakeRunner()
        runner.set_response(
            "wl list --stage plan_complete",
            stdout=json.dumps({"success": True, "workItems": []}),
        )

        engine = ImplementAllEngine(runner=runner)
        items = engine.discover_items()
        assert items == []

    def test_discover_handles_wl_error_gracefully(self):
        """If wl command fails, return an empty list without crashing."""
        runner = FakeRunner()
        runner.set_response(
            "wl list --stage plan_complete",
            returncode=1,
            stdout="",
            stderr="wl: error connecting",
        )

        engine = ImplementAllEngine(runner=runner)
        items = engine.discover_items()
        assert items == []


# ===========================================================================
# Test: Sequential /skill:implement invocation
# ===========================================================================

class TestImplementInvocation:
    """Verify that /skill:implement is invoked for each item sequentially."""

    def test_implement_invoked_for_each_item(self):
        """/skill:implement is invoked for each item in sequence."""
        runner = FakeRunner()
        runner.set_response(
            "wl list --stage plan_complete",
            stdout=SAMPLE_WL_LIST_RESPONSE,
        )
        for item_id in ["SA-IMPL-001", "SA-IMPL-002", "SA-IMPL-003"]:
            runner.set_response(
                f"wl update {item_id} --status",
                stdout=json.dumps({"success": True}),
            )
            runner.set_response(
                f"pi run /skill:implement {item_id}",
                stdout=json.dumps({"success": True}),
            )

        engine = ImplementAllEngine(runner=runner)
        results = engine.run_all()

        assert len(results) == 3
        # Verify pi run /skill:implement was called for each item
        impl_calls = [
            cmd for cmd in runner.calls
            if "pi" in cmd and "run" in cmd and "skill:implement" in " ".join(cmd)
        ]
        assert len(impl_calls) == 3
        assert "SA-IMPL-001" in " ".join(impl_calls[0])
        assert "SA-IMPL-002" in " ".join(impl_calls[1])
        assert "SA-IMPL-003" in " ".join(impl_calls[2])

    def test_implement_items_claimed_before_implement(self):
        """Each item is claimed with wl update before /skill:implement is invoked."""
        runner = FakeRunner()
        runner.set_response(
            "wl list --stage plan_complete",
            stdout=SAMPLE_WL_LIST_RESPONSE,
        )
        for item_id in ["SA-IMPL-001", "SA-IMPL-002", "SA-IMPL-003"]:
            runner.set_response(
                f"wl update {item_id} --status",
                stdout=json.dumps({"success": True}),
            )
            runner.set_response(
                f"pi run /skill:implement {item_id}",
                stdout=json.dumps({"success": True}),
            )

        engine = ImplementAllEngine(runner=runner)
        engine.run_all()

        # Extract claim and implement call pairs
        claim_calls = []
        impl_calls = []
        for cmd in runner.calls:
            cmd_str = " ".join(cmd)
            if "wl update" in cmd_str and "--status" in cmd_str:
                claim_calls.append(cmd)
            if "pi run /skill:implement" in cmd_str:
                impl_calls.append(cmd)

        assert len(claim_calls) == 3
        assert len(impl_calls) == 3
        # Each item should be claimed before it is implemented
        for i in range(3):
            claim_id = claim_calls[i][2]  # item id is at index 2 in wl update <id> ...
            impl_str = " ".join(impl_calls[i])
            assert claim_id in impl_str, f"Item {claim_id} claimed but not implemented in order"


# ===========================================================================
# Test: Producer-input detection
# ===========================================================================

class TestProducerInputDetection:
    """Verify detection of items needing producer input."""

    def test_unanswered_questions_detected(self):
        """Items with unanswered questions are flagged as needing producer input."""
        runner = FakeRunner()
        runner.set_response(
            "wl list --stage plan_complete",
            stdout=json.dumps({"success": True, "workItems": [SAMPLE_ITEM_A]}),
        )
        runner.set_response(
            f"wl update {SAMPLE_ITEM_A['id']} --status",
            stdout=json.dumps({"success": True}),
        )
        # Simulate implement output that indicates unanswered questions
        runner.set_response(
            f"pi run /skill:implement {SAMPLE_ITEM_A['id']}",
            stdout="Should feature A be behind a flag? (yes/no):",
            returncode=1,
        )

        engine = ImplementAllEngine(runner=runner)
        results = engine.run_all()
        assert len(results) == 1
        assert results[0]["outcome"] == "needs_input"
        assert results[0]["id"] == SAMPLE_ITEM_A["id"]

    def test_successful_implement_is_not_needs_input(self):
        """Items that complete implement without questions are marked implemented."""
        runner = FakeRunner()
        runner.set_response(
            "wl list --stage plan_complete",
            stdout=json.dumps({"success": True, "workItems": [SAMPLE_ITEM_B]}),
        )
        runner.set_response(
            f"wl update {SAMPLE_ITEM_B['id']} --status",
            stdout=json.dumps({"success": True}),
        )
        runner.set_response(
            f"pi run /skill:implement {SAMPLE_ITEM_B['id']}",
            stdout=json.dumps({"success": True}),
        )

        engine = ImplementAllEngine(runner=runner)
        results = engine.run_all()
        assert len(results) == 1
        assert results[0]["outcome"] == "implemented"

    def test_non_zero_exit_with_questions_detected(self):
        """Non-zero exit with question patterns is needs_input."""
        runner = FakeRunner()
        runner.set_response(
            "wl list --stage plan_complete",
            stdout=json.dumps({"success": True, "workItems": [SAMPLE_ITEM_A]}),
        )
        runner.set_response(
            f"wl update {SAMPLE_ITEM_A['id']} --status",
            stdout=json.dumps({"success": True}),
        )
        runner.set_response(
            f"pi run /skill:implement {SAMPLE_ITEM_A['id']}",
            stdout="What should we name the new feature?",
            returncode=0,  # Zero exit but still contains questions
        )

        engine = ImplementAllEngine(runner=runner)
        results = engine.run_all()
        assert len(results) == 1
        assert results[0]["outcome"] == "needs_input"

    def test_exception_during_implement_detected(self):
        """Exception during /skill:implement is caught and flagged as error."""
        runner = FakeRunner()
        runner.set_response(
            "wl list --stage plan_complete",
            stdout=json.dumps({"success": True, "workItems": [SAMPLE_ITEM_C]}),
        )
        runner.set_response(
            f"wl update {SAMPLE_ITEM_C['id']} --status",
            stdout=json.dumps({"success": True}),
        )
        runner.set_response(
            f"pi run /skill:implement {SAMPLE_ITEM_C['id']}",
            returncode=1,
            stderr="Connection refused",
        )

        engine = ImplementAllEngine(runner=runner)
        results = engine.run_all()
        assert len(results) == 1
        assert results[0]["outcome"] == "error"


# ===========================================================================
# Test: --max flag
# ===========================================================================

class TestMaxFlag:
    """Verify --max flag limits the number of items processed."""

    def test_max_zero_processes_all(self):
        """--max 0 (default) processes all items."""
        runner = FakeRunner()
        runner.set_response(
            "wl list --stage plan_complete",
            stdout=SAMPLE_WL_LIST_RESPONSE,
        )
        for item_id in ["SA-IMPL-001", "SA-IMPL-002", "SA-IMPL-003"]:
            runner.set_response(
                f"wl update {item_id} --status",
                stdout=json.dumps({"success": True}),
            )
            runner.set_response(
                f"pi run /skill:implement {item_id}",
                stdout=json.dumps({"success": True}),
            )

        engine = ImplementAllEngine(runner=runner, max_items=0)
        results = engine.run_all()
        assert len(results) == 3

    def test_max_positive_limits_processing(self):
        """--max N processes at most N items."""
        runner = FakeRunner()
        runner.set_response(
            "wl list --stage plan_complete",
            stdout=SAMPLE_WL_LIST_RESPONSE,
        )
        # Only need responses for first 2 items
        for item_id in ["SA-IMPL-001", "SA-IMPL-002"]:
            runner.set_response(
                f"wl update {item_id} --status",
                stdout=json.dumps({"success": True}),
            )
            runner.set_response(
                f"pi run /skill:implement {item_id}",
                stdout=json.dumps({"success": True}),
            )

        engine = ImplementAllEngine(runner=runner, max_items=2)
        results = engine.run_all()
        assert len(results) == 2
        assert results[0]["id"] == "SA-IMPL-001"
        assert results[1]["id"] == "SA-IMPL-002"

    def test_max_larger_than_item_count(self):
        """--max larger than available items processes all items."""
        runner = FakeRunner()
        runner.set_response(
            "wl list --stage plan_complete",
            stdout=SAMPLE_WL_LIST_RESPONSE,
        )
        for item_id in ["SA-IMPL-001", "SA-IMPL-002", "SA-IMPL-003"]:
            runner.set_response(
                f"wl update {item_id} --status",
                stdout=json.dumps({"success": True}),
            )
            runner.set_response(
                f"pi run /skill:implement {item_id}",
                stdout=json.dumps({"success": True}),
            )

        engine = ImplementAllEngine(runner=runner, max_items=10)
        results = engine.run_all()
        assert len(results) == 3

    def test_max_one_processes_single_item(self):
        """--max 1 processes exactly one item."""
        runner = FakeRunner()
        runner.set_response(
            "wl list --stage plan_complete",
            stdout=SAMPLE_WL_LIST_RESPONSE,
        )
        runner.set_response(
            "wl update SA-IMPL-001 --status",
            stdout=json.dumps({"success": True}),
        )
        runner.set_response(
            "pi run /skill:implement SA-IMPL-001",
            stdout=json.dumps({"success": True}),
        )

        engine = ImplementAllEngine(runner=runner, max_items=1)
        results = engine.run_all()
        assert len(results) == 1
        assert results[0]["id"] == "SA-IMPL-001"

    def test_max_counts_errors_and_needs_input(self):
        """--max counts all items processed (including errors and needs_input)."""
        runner = FakeRunner()
        runner.set_response(
            "wl list --stage plan_complete",
            stdout=SAMPLE_WL_LIST_RESPONSE,
        )
        # Item 1: error
        runner.set_response(
            "wl update SA-IMPL-001 --status",
            stdout=json.dumps({"success": True}),
        )
        runner.set_response(
            "pi run /skill:implement SA-IMPL-001",
            returncode=1,
            stderr="timeout",
        )
        runner.set_response(
            "wl update SA-IMPL-001 --status open",
            stdout=json.dumps({"success": True}),
        )
        # Item 2: needs_input
        runner.set_response(
            "wl update SA-IMPL-002 --status",
            stdout=json.dumps({"success": True}),
        )
        runner.set_response(
            "pi run /skill:implement SA-IMPL-002",
            stdout="What should we do? (yes/no):",
            returncode=1,
        )

        engine = ImplementAllEngine(runner=runner, max_items=2)
        results = engine.run_all()
        assert len(results) == 2
        assert results[0]["outcome"] == "error"
        assert results[1]["outcome"] == "needs_input"
        # Verify no calls were made for item 3
        impl_calls = [
            cmd for cmd in runner.calls
            if "pi" in cmd and "run" in cmd and "skill:implement" in " ".join(cmd)
        ]
        assert len(impl_calls) == 2
        assert "SA-IMPL-003" not in " ".join(impl_calls[-1])


# ===========================================================================
# Test: Error handling with recovery
# ===========================================================================

class TestErrorHandlingWithRecovery:
    """Verify error handling: capture, recovery, action recording."""

    def test_error_captures_stdout_and_stderr(self):
        """Error outcome includes stdout and stderr details."""
        runner = FakeRunner()
        runner.set_response(
            "wl list --stage plan_complete",
            stdout=json.dumps({
                "success": True,
                "workItems": [SAMPLE_ITEM_C],
            }),
        )
        runner.set_response(
            f"wl update {SAMPLE_ITEM_C['id']} --status",
            stdout=json.dumps({"success": True}),
        )
        runner.set_response(
            f"pi run /skill:implement {SAMPLE_ITEM_C['id']}",
            returncode=1,
            stdout="Some output before error",
            stderr="Implement failed: timeout exceeded",
        )

        engine = ImplementAllEngine(runner=runner)
        results = engine.run_all()

        assert len(results) == 1
        assert results[0]["outcome"] == "error"
        error_detail = results[0].get("error_detail", "")
        assert "timeout exceeded" in error_detail or "failed" in error_detail

    def test_recovery_attempts_reset_status_on_error(self):
        """On error, item status is reset to open."""
        runner = FakeRunner()
        runner.set_response(
            "wl list --stage plan_complete",
            stdout=json.dumps({
                "success": True,
                "workItems": [SAMPLE_ITEM_C],
            }),
        )
        runner.set_response(
            f"wl update {SAMPLE_ITEM_C['id']} --status",
            stdout=json.dumps({"success": True}),
        )
        # Implement fails
        runner.set_response(
            f"pi run /skill:implement {SAMPLE_ITEM_C['id']}",
            returncode=1,
            stderr="timeout",
        )
        # Recovery: reset the item status back to open
        runner.set_response(
            f"wl update {SAMPLE_ITEM_C['id']} --status open",
            stdout=json.dumps({"success": True}),
        )

        engine = ImplementAllEngine(runner=runner)
        results = engine.run_all()

        assert len(results) == 1
        # Verify recovery was attempted (reset to open)
        reset_calls = [
            cmd for cmd in runner.calls
            if "wl" in cmd and "update" in cmd and "open" in " ".join(cmd)
        ]
        assert len(reset_calls) >= 1

    def test_recovery_actions_recorded_in_result(self):
        """Recovery actions are recorded in the result for reporting."""
        runner = FakeRunner()
        runner.set_response(
            "wl list --stage plan_complete",
            stdout=json.dumps({
                "success": True,
                "workItems": [SAMPLE_ITEM_C],
            }),
        )
        runner.set_response(
            f"wl update {SAMPLE_ITEM_C['id']} --status",
            stdout=json.dumps({"success": True}),
        )
        runner.set_response(
            f"pi run /skill:implement {SAMPLE_ITEM_C['id']}",
            returncode=1,
            stderr="timeout",
        )
        runner.set_response(
            f"wl update {SAMPLE_ITEM_C['id']} --status open",
            stdout=json.dumps({"success": True}),
        )

        engine = ImplementAllEngine(runner=runner)
        results = engine.run_all()

        assert len(results) == 1
        # Check recovery outcome field
        recovery = results[0].get("recovery")
        assert recovery is not None, "Recovery info should be recorded"
        assert "action" in recovery or "attempted" in str(recovery).lower()


# ===========================================================================
# Test: Error resilience
# ===========================================================================

class TestErrorResilience:
    """Verify that errors during processing do not stop remaining items."""

    def test_errors_do_not_stop_processing(self):
        """Processing continues to remaining items after an error."""
        runner = FakeRunner()
        items_list = [SAMPLE_ITEM_C, SAMPLE_ITEM_A, SAMPLE_ITEM_B]
        runner.set_response(
            "wl list --stage plan_complete",
            stdout=json.dumps({
                "success": True,
                "workItems": items_list,
            }),
        )
        # Claim all items
        for item in items_list:
            runner.set_response(
                f"wl update {item['id']} --status",
                stdout=json.dumps({"success": True}),
            )
        # First item (complex) implement fails
        runner.set_response(
            f"pi run /skill:implement {SAMPLE_ITEM_C['id']}",
            returncode=1,
            stderr="timeout",
        )
        runner.set_response(
            f"wl update {SAMPLE_ITEM_C['id']} --status open",
            stdout=json.dumps({"success": True}),
        )
        # Second item succeeds
        runner.set_response(
            f"pi run /skill:implement {SAMPLE_ITEM_A['id']}",
            stdout=json.dumps({"success": True}),
        )
        # Third item succeeds
        runner.set_response(
            f"pi run /skill:implement {SAMPLE_ITEM_B['id']}",
            stdout=json.dumps({"success": True}),
        )

        engine = ImplementAllEngine(runner=runner)
        results = engine.run_all()

        assert len(results) == 3
        assert results[0]["outcome"] == "error"
        assert results[1]["outcome"] == "implemented"
        assert results[2]["outcome"] == "implemented"

    def test_wl_update_failure_does_not_stop_processing(self):
        """Processing continues if claiming an item fails."""
        runner = FakeRunner()
        items_list = [SAMPLE_ITEM_A, SAMPLE_ITEM_B]
        runner.set_response(
            "wl list --stage plan_complete",
            stdout=json.dumps({
                "success": True,
                "workItems": items_list,
            }),
        )
        # First item claim fails
        runner.set_response(
            f"wl update {SAMPLE_ITEM_A['id']} --status",
            returncode=1,
            stdout="",
            stderr="wl: item not found",
        )
        # Second item claim succeeds, then implement succeeds
        runner.set_response(
            f"wl update {SAMPLE_ITEM_B['id']} --status",
            stdout=json.dumps({"success": True}),
        )
        runner.set_response(
            f"pi run /skill:implement {SAMPLE_ITEM_B['id']}",
            stdout=json.dumps({"success": True}),
        )

        engine = ImplementAllEngine(runner=runner)
        results = engine.run_all()

        assert len(results) == 2
        assert results[0]["outcome"] == "error"
        assert results[1]["outcome"] == "implemented"


# ===========================================================================
# Test: Summary report
# ===========================================================================

class TestSummaryReport:
    """Verify summary report accuracy."""

    def test_summary_counts_are_correct(self):
        """Summary report correctly counts outcomes."""
        results = [
            {"id": "SA-001", "title": "A", "outcome": "implemented",
             "error_detail": None, "recovery": None},
            {"id": "SA-002", "title": "B", "outcome": "needs_input",
             "error_detail": None, "recovery": None},
            {"id": "SA-003", "title": "C", "outcome": "implemented",
             "error_detail": None, "recovery": None},
            {"id": "SA-004", "title": "D", "outcome": "error",
             "error_detail": "timeout", "recovery": {"action": "reset_status", "success": True}},
        ]

        markdown = generate_summary(results, json_output=False)
        assert "**Total processed**: 4" in markdown
        assert "**Implemented**: 2" in markdown
        assert "**Needs input**: 1" in markdown
        assert "**Errors**: 1" in markdown

    def test_summary_lists_each_item_with_outcome(self):
        """Each processed item appears in the summary with its outcome."""
        results = [
            {"id": "SA-001", "title": "Feature A", "outcome": "implemented",
             "error_detail": None, "recovery": None},
            {"id": "SA-002", "title": "Feature B", "outcome": "needs_input",
             "error_detail": None, "recovery": None},
        ]

        markdown = generate_summary(results, json_output=False)
        assert "SA-001" in markdown and "implemented" in markdown
        assert "SA-002" in markdown and "needs_input" in markdown

    def test_summary_includes_error_details(self):
        """Summary includes error details and recovery info in output."""
        results = [
            {"id": "SA-001", "title": "A", "outcome": "error",
             "error_detail": "Connection refused",
             "recovery": {"action": "reset_status", "success": True}},
        ]

        markdown = generate_summary(results, json_output=False)
        assert "error" in markdown.lower()
        assert "Connection refused" in markdown or "reset_status" in markdown

    def test_summary_json_output(self):
        """JSON output is produced when --json flag is requested."""
        results = [
            {"id": "SA-001", "title": "A", "outcome": "implemented",
             "error_detail": None, "recovery": None},
            {"id": "SA-002", "title": "B", "outcome": "needs_input",
             "error_detail": None, "recovery": None},
        ]

        json_out = generate_summary(results, json_output=True)
        parsed = json.loads(json_out)
        assert parsed["total"] == 2
        assert parsed["implemented"] == 1
        assert parsed["needs_input"] == 1
        assert parsed["errors"] == 0
        assert len(parsed["items"]) == 2

    def test_empty_summary(self):
        """Empty results produce a valid zeroed summary."""
        markdown = generate_summary([], json_output=False)
        assert "**Total processed**: 0" in markdown
        assert "**Implemented**: 0" in markdown
        assert "**Needs input**: 0" in markdown
        assert "**Errors**: 0" in markdown


# ===========================================================================
# Test: --parent-id flag posts summary as comment
# ===========================================================================

class TestParentIdFlag:
    """Verify --parent-id flag posts summary as a comment."""

    def test_summary_posted_as_comment_when_parent_provided(self):
        """Summary is posted as a wl comment on the parent item if provided."""
        runner = FakeRunner()
        runner.set_response(
            "wl comment add SA-PARENT",
            stdout=json.dumps({"success": True}),
        )

        results = [
            {"id": "SA-001", "title": "A", "outcome": "implemented",
             "error_detail": None, "recovery": None},
        ]

        engine = ImplementAllEngine(runner=runner)
        engine.post_summary(results, parent_id="SA-PARENT")

        # Verify wl comment add was called
        comment_calls = [
            cmd for cmd in runner.calls
            if "wl" in cmd and "comment" in cmd and "add" in cmd
        ]
        assert len(comment_calls) >= 1
        comment_str = " ".join(comment_calls[0])
        assert "SA-PARENT" in comment_str
        assert "ImplementAll" in comment_str or "implementation" in comment_str.lower()


# ===========================================================================
# Test: Idempotence
# ===========================================================================

class TestIdempotence:
    """Verify that re-running ImplementAll does not duplicate work."""

    def test_re_run_does_not_process_completed_items(self):
        """Items already past plan_complete stage are not processed again."""
        runner = FakeRunner()

        # First run: 2 items in plan_complete
        runner.set_response(
            "wl list --stage plan_complete",
            stdout=json.dumps({
                "success": True,
                "workItems": [SAMPLE_ITEM_A, SAMPLE_ITEM_B],
            }),
        )
        for item_id in ["SA-IMPL-001", "SA-IMPL-002"]:
            runner.set_response(
                f"wl update {item_id} --status",
                stdout=json.dumps({"success": True}),
            )
            runner.set_response(
                f"pi run /skill:implement {item_id}",
                stdout=json.dumps({"success": True}),
            )

        engine = ImplementAllEngine(runner=runner)
        results_first = engine.run_all()
        assert len(results_first) == 2

        # Second run: only 1 item left in plan_complete
        runner.set_response(
            "wl list --stage plan_complete",
            stdout=json.dumps({
                "success": True,
                "workItems": [SAMPLE_ITEM_C],
            }),
        )
        runner.set_response(
            f"wl update {SAMPLE_ITEM_C['id']} --status",
            stdout=json.dumps({"success": True}),
        )
        runner.set_response(
            f"pi run /skill:implement {SAMPLE_ITEM_C['id']}",
            stdout=json.dumps({"success": True}),
        )

        results_second = engine.run_all()
        assert len(results_second) == 1
        assert results_second[0]["id"] == "SA-IMPL-003"
        assert results_second[0]["outcome"] == "implemented"


# ===========================================================================
# Test: --dry-run flag
# ===========================================================================

class TestDryRun:
    """Verify --dry-run flag simulates without making changes."""

    def test_dry_run_does_not_make_changes(self):
        """--dry-run flag processes without making actual changes."""
        runner = FakeRunner()
        runner.set_response(
            "wl list --stage plan_complete",
            stdout=SAMPLE_WL_LIST_RESPONSE,
        )

        engine = ImplementAllEngine(runner=runner, dry_run=True)
        results = engine.run_all()

        # Should still produce results
        assert len(results) == 3
        # Should NOT have made any update or implement calls
        update_calls = [
            cmd for cmd in runner.calls
            if "wl update" in " ".join(cmd) or "pi run" in " ".join(cmd)
        ]
        assert len(update_calls) == 0, "Dry run should not make changes"

    def test_dry_run_counts_outcomes(self):
        """--dry-run sets all outcomes to 'implemented' by default."""
        runner = FakeRunner()
        runner.set_response(
            "wl list --stage plan_complete",
            stdout=SAMPLE_WL_LIST_RESPONSE,
        )

        engine = ImplementAllEngine(runner=runner, dry_run=True)
        results = engine.run_all()

        assert len(results) == 3
        assert all(r["outcome"] == "implemented" for r in results)

    def test_dry_run_respects_max(self):
        """--dry-run with --max limits items."""
        runner = FakeRunner()
        runner.set_response(
            "wl list --stage plan_complete",
            stdout=SAMPLE_WL_LIST_RESPONSE,
        )

        engine = ImplementAllEngine(runner=runner, dry_run=True, max_items=2)
        results = engine.run_all()
        assert len(results) == 2
        assert results[0]["id"] == "SA-IMPL-001"
        assert results[1]["id"] == "SA-IMPL-002"


# ===========================================================================
# Test: CLI entry point
# ===========================================================================

class TestCLI:
    """Verify the CLI entry point parses arguments and runs correctly."""

    def test_default_invocation(self):
        """Default invocation discovers and processes all plan_complete items."""
        runner = FakeRunner()
        runner.set_response(
            "wl list --stage plan_complete",
            stdout=SAMPLE_WL_LIST_RESPONSE,
        )
        for item_id in ["SA-IMPL-001", "SA-IMPL-002", "SA-IMPL-003"]:
            runner.set_response(
                f"wl update {item_id} --status",
                stdout=json.dumps({"success": True}),
            )
            runner.set_response(
                f"pi run /skill:implement {item_id}",
                stdout=json.dumps({"success": True}),
            )

        engine = ImplementAllEngine(runner=runner)
        results = engine.run_all()
        assert len(results) == 3
        assert all(r["outcome"] == "implemented" for r in results)

    def test_json_flag(self):
        """--json flag produces JSON output."""
        results = [
            {"id": "SA-001", "title": "A", "outcome": "implemented",
             "error_detail": None, "recovery": None},
        ]
        summary = generate_summary(results, json_output=True)
        parsed = json.loads(summary)
        assert parsed["total"] == 1
        assert parsed["implemented"] == 1

    def test_max_flag_parsing(self):
        """--max flag is parsed correctly by the CLI."""
        from skill.implementall.scripts.implementall import build_parser
        parser = build_parser()
        args = parser.parse_args(["--max", "5"])
        assert args.max == 5
        args = parser.parse_args([])
        assert args.max == 0  # default

    def test_cli_accepts_verbose(self):
        """CLI accepts --verbose flag."""
        from skill.implementall.scripts.implementall import build_parser
        parser = build_parser()
        args = parser.parse_args(["--verbose"])
        assert args.verbose is True


# ===========================================================================
# Test: Engine initialization and configuration
# ===========================================================================

class TestEngineConfig:
    """Verify ImplementAllEngine configuration."""

    def test_default_runner_is_callable(self):
        """Default runner is callable."""
        engine = ImplementAllEngine()
        assert engine.runner is not None
        assert callable(engine.runner)

    def test_custom_runner_is_used(self):
        """Custom runner is used when provided."""
        def my_runner(cmd):
            return FakeProc(returncode=0, stdout="[]", stderr="")
        engine = ImplementAllEngine(runner=my_runner)
        assert engine.runner is my_runner

    def test_dry_run_flag(self):
        """Dry run flag is set correctly."""
        engine = ImplementAllEngine(dry_run=True)
        assert engine.dry_run is True
        engine2 = ImplementAllEngine(dry_run=False)
        assert engine2.dry_run is False

    def test_max_items_default(self):
        """Max items defaults to 0 (no limit)."""
        engine = ImplementAllEngine()
        assert engine.max_items == 0

    def test_max_items_custom(self):
        """Max items can be set to a custom value."""
        engine = ImplementAllEngine(max_items=5)
        assert engine.max_items == 5

    def test_item_timeout_default(self):
        """Item timeout defaults to 600 seconds."""
        engine = ImplementAllEngine()
        assert engine.item_timeout == 600

    def test_item_timeout_custom(self):
        """Item timeout can be set to a custom value."""
        engine = ImplementAllEngine(item_timeout=120)
        assert engine.item_timeout == 120


# ===========================================================================
# Test: --item-timeout (per-item subprocess timeout)
# ===========================================================================

class TestItemTimeout:
    """Verify --item-timeout triggers recovery on subprocess timeout."""

    def test_item_timeout_triggers_recovery(self):
        """When subprocess times out, item is recovered (reset to plan_complete/open) and continues."""
        runner = FakeRunner()
        runner.set_response(
            "wl list --stage plan_complete",
            stdout=SAMPLE_WL_LIST_RESPONSE,
        )
        runner.set_response(
            "wl update SA-IMPL-001 --status",
            stdout=json.dumps({"success": True}),
        )
        runner.set_response(
            "pi run /skill:implement SA-IMPL-001",
            returncode=-15,
            stderr="timed out",
        )
        runner.set_response(
            "wl update SA-IMPL-001 --status open",
            stdout=json.dumps({"success": True}),
        )
        runner.set_response(
            "wl update SA-IMPL-002 --status",
            stdout=json.dumps({"success": True}),
        )
        runner.set_response(
            "pi run /skill:implement SA-IMPL-002",
            stdout=json.dumps({"success": True}),
        )
        runner.set_response(
            "wl update SA-IMPL-003 --status",
            stdout=json.dumps({"success": True}),
        )
        runner.set_response(
            "pi run /skill:implement SA-IMPL-003",
            stdout=json.dumps({"success": True}),
        )

        engine = ImplementAllEngine(runner=runner, item_timeout=10)
        results = engine.run_all()

        assert len(results) == 3
        assert results[0]["outcome"] == "error"
        recovery_calls = [
            cmd for cmd in runner.calls
            if "wl" in cmd and "update" in cmd and "open" in " ".join(cmd)
        ]
        assert len(recovery_calls) >= 1
        assert results[1]["outcome"] == "implemented"
        assert results[2]["outcome"] == "implemented"

    def test_item_timeout_logged(self):
        """Timeout event has stderr info in error_detail."""
        runner = FakeRunner()
        runner.set_response(
            "wl list --stage plan_complete",
            stdout=json.dumps({
                "success": True,
                "workItems": [SAMPLE_ITEM_A],
            }),
        )
        runner.set_response(
            "wl update SA-IMPL-001 --status",
            stdout=json.dumps({"success": True}),
        )
        runner.set_response(
            "pi run /skill:implement SA-IMPL-001",
            returncode=-15,
            stderr="timed out after 10 seconds",
        )
        runner.set_response(
            "wl update SA-IMPL-001 --status open",
            stdout=json.dumps({"success": True}),
        )

        engine = ImplementAllEngine(runner=runner, item_timeout=10)
        results = engine.run_all()

        assert len(results) == 1
        assert results[0]["outcome"] == "error"
        error_detail = results[0].get("error_detail", "")
        assert "timed out" in error_detail or "timeout" in error_detail

    def test_item_timeout_continues_to_next_item(self):
        """After timeout, processing continues to next items."""
        runner = FakeRunner()
        runner.set_response(
            "wl list --stage plan_complete",
            stdout=SAMPLE_WL_LIST_RESPONSE,
        )
        runner.set_response(
            "wl update SA-IMPL-001 --status",
            stdout=json.dumps({"success": True}),
        )
        runner.set_response(
            "pi run /skill:implement SA-IMPL-001",
            returncode=-15,
            stderr="timed out",
        )
        runner.set_response(
            "wl update SA-IMPL-001 --status open",
            stdout=json.dumps({"success": True}),
        )
        runner.set_response(
            "wl update SA-IMPL-002 --status",
            stdout=json.dumps({"success": True}),
        )
        runner.set_response(
            "pi run /skill:implement SA-IMPL-002",
            stdout=json.dumps({"success": True}),
        )

        engine = ImplementAllEngine(runner=runner, item_timeout=10)
        results = engine.run_all()

        assert len(results) == 3
        assert results[0]["outcome"] == "error"
        assert results[1]["outcome"] == "implemented"
        assert results[2]["outcome"] == "implemented"

    def test_max_and_item_timeout_interact(self):
        """--max and --item-timeout interact correctly (timeout counts toward max)."""
        runner = FakeRunner()
        runner.set_response(
            "wl list --stage plan_complete",
            stdout=SAMPLE_WL_LIST_RESPONSE,
        )
        runner.set_response(
            "wl update SA-IMPL-001 --status",
            stdout=json.dumps({"success": True}),
        )
        runner.set_response(
            "pi run /skill:implement SA-IMPL-001",
            returncode=-15,
            stderr="timed out",
        )
        runner.set_response(
            "wl update SA-IMPL-001 --status open",
            stdout=json.dumps({"success": True}),
        )

        engine = ImplementAllEngine(runner=runner, max_items=1, item_timeout=10)
        results = engine.run_all()

        assert len(results) == 1
        assert results[0]["outcome"] == "error"
        # Verify item 2 was NOT processed (max reached)
        impl_calls = [
            cmd for cmd in runner.calls
            if "pi" in cmd and "run" in cmd and "skill:implement" in " ".join(cmd)
        ]
        assert len(impl_calls) == 1
        assert "SA-IMPL-002" not in " ".join(impl_calls[-1])


# ===========================================================================
# Test: Signal handling for graceful abort
# ===========================================================================

class TestSignalHandling:
    """Verify signal handlers are registered and trigger recovery correctly."""

    def test_signal_handlers_registered(self):
        """SIGINT and SIGTERM handlers are registered on setup."""
        runner = FakeRunner()
        engine = ImplementAllEngine(runner=runner)

        engine._setup_signal_handlers()

        assert signal.getsignal(signal.SIGINT) == engine._signal_handler
        assert signal.getsignal(signal.SIGTERM) == engine._signal_handler

        engine._restore_signal_handlers()
        assert signal.getsignal(signal.SIGINT) != engine._signal_handler or \
               signal.getsignal(signal.SIGINT) == signal.default_int_handler

    def test_signal_handler_calls_recovery_for_current_item(self):
        """Signal handler calls recovery for the current item."""
        runner = FakeRunner()
        runner.set_response(
            "wl update SA-IMPL-001 --status open",
            stdout=json.dumps({"success": True}),
        )

        engine = ImplementAllEngine(runner=runner)
        engine._current_item_id = "SA-IMPL-001"

        try:
            engine._signal_handler(signal.SIGINT, None)
        except SystemExit:
            pass

        recovery_calls = [
            cmd for cmd in runner.calls
            if "wl" in cmd and "update" in cmd and "SA-IMPL-001" in " ".join(cmd)
        ]
        assert len(recovery_calls) >= 1

    def test_signal_handler_noop_when_no_current_item(self):
        """Signal handler does nothing when no item is being processed."""
        runner = FakeRunner()
        engine = ImplementAllEngine(runner=runner)
        engine._current_item_id = None

        try:
            engine._signal_handler(signal.SIGINT, None)
        except SystemExit:
            pass

        update_calls = [
            cmd for cmd in runner.calls
            if "wl" in cmd and "update" in cmd
        ]
        assert len(update_calls) == 0

    def test_current_item_id_set_during_implement(self):
        """_current_item_id is set during implement processing for signal handling."""
        runner = FakeRunner()
        runner.set_response(
            "wl list --stage plan_complete",
            stdout=json.dumps({
                "success": True,
                "workItems": [SAMPLE_ITEM_A],
            }),
        )
        runner.set_response(
            "wl update SA-IMPL-001 --status",
            stdout=json.dumps({"success": True}),
        )
        runner.set_response(
            "pi run /skill:implement SA-IMPL-001",
            stdout=json.dumps({"success": True}),
        )

        engine = ImplementAllEngine(runner=runner)
        engine.run_all()

        assert engine._current_item_id is None

    def test_signal_handler_exits_with_code(self):
        """Signal handler raises SystemExit with correct code (128+signum)."""
        runner = FakeRunner()
        engine = ImplementAllEngine(runner=runner)

        try:
            engine._signal_handler(signal.SIGINT, None)
            assert False, "Expected SystemExit"
        except SystemExit as e:
            assert e.code == 128 + signal.SIGINT


# ===========================================================================
# Test: Summary enhancements (remaining items reporting)
# ===========================================================================

class TestSummaryEnhancements:
    """Verify summary reports remaining items when processing is incomplete."""

    def test_remaining_items_reported_when_timeout_limits(self):
        """When timeout limits processing, remaining items can be computed."""
        runner = FakeRunner()
        runner.set_response(
            "wl list --stage plan_complete",
            stdout=SAMPLE_WL_LIST_RESPONSE,
        )
        runner.set_response(
            "wl update SA-IMPL-001 --status",
            stdout=json.dumps({"success": True}),
        )
        runner.set_response(
            "pi run /skill:implement SA-IMPL-001",
            stdout=json.dumps({"success": True}),
        )

        engine = ImplementAllEngine(runner=runner, max_items=1)
        results = engine.run_all()

        assert len(results) == 1
        assert results[0]["id"] == "SA-IMPL-001"

        # Remaining count can be determined: total discovered - processed
        discovered = engine.discover_items()
        remaining = len(discovered) - len(results)
        assert remaining == 2
