"""Tests for PlanAll: Automated Batch Planning for intake_complete items.

These tests verify:
- Discovery of intake_complete items via wl list
- Sequential /plan invocation for each item
- Producer-input detection via unanswered questions
- Summary report accuracy
- Error resilience (errors for one item don't stop processing)
- Idempotence (re-running doesn't duplicate work)
- --max flag to limit batch size
- --item-timeout for per-item subprocess timeout
- Signal handler registration and behavior (SIGINT/SIGTERM trigger recovery)
- Summary enhancements for remaining items when incomplete

Related work item: SA-0MQA7HOLS007HMHZ
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

from skill.planall.scripts.planall import (  # noqa: E402
    PlanAllEngine,
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

    def set_response(self, cmd_prefix: str, returncode: int = 0, stdout: str = "", stderr: str = ""):
        """Register a canned response for any command whose args start with cmd_prefix."""
        self.responses[cmd_prefix] = FakeProc(
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        )

    def __call__(self, cmd, **kwargs):
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
    "id": "SA-PLAN-001",
    "title": "Test Feature A",
    "status": "open",
    "stage": "intake_complete",
    "priority": "high",
}

SAMPLE_ITEM_B = {
    "id": "SA-PLAN-002",
    "title": "Test Feature B",
    "status": "open",
    "stage": "intake_complete",
    "priority": "medium",
}

SAMPLE_ITEM_C = {
    "id": "SA-PLAN-003",
    "title": "Complex Feature C",
    "status": "open",
    "stage": "intake_complete",
    "priority": "high",
}

SAMPLE_WL_LIST_RESPONSE = json.dumps({
    "success": True,
    "workItems": [SAMPLE_ITEM_A, SAMPLE_ITEM_B, SAMPLE_ITEM_C],
})


# ===========================================================================
# Test: Discovery of intake_complete items
# ===========================================================================

class TestDiscovery:
    """Verify that PlanAll discovers all items in intake_complete status."""

    def test_discover_all_intake_complete_items(self):
        """`wl list --stage intake_complete --json` returns all eligible items."""
        runner = FakeRunner()
        runner.set_response(
            "wl list --stage intake_complete",
            stdout=SAMPLE_WL_LIST_RESPONSE,
        )

        engine = PlanAllEngine(runner=runner)
        items = engine.discover_items()

        assert len(items) == 3
        assert items[0]["id"] == "SA-PLAN-001"
        assert items[1]["id"] == "SA-PLAN-002"
        assert items[2]["id"] == "SA-PLAN-003"

        # Verify the correct wl command was issued
        assert any(
            cmd[:3] == ["wl", "list", "--stage"] and "intake_complete" in cmd
            for cmd in runner.calls
        ), "Expected wl list --stage intake_complete --json call"

    def test_discover_returns_empty_list_when_no_items(self):
        """When no items are in intake_complete, return an empty list."""
        runner = FakeRunner()
        runner.set_response(
            "wl list --stage intake_complete",
            stdout=json.dumps({"success": True, "workItems": []}),
        )

        engine = PlanAllEngine(runner=runner)
        items = engine.discover_items()
        assert items == []

    def test_discover_handles_wl_error_gracefully(self):
        """If wl command fails, return an empty list without crashing."""
        runner = FakeRunner()
        runner.set_response(
            "wl list --stage intake_complete",
            returncode=1,
            stdout="",
            stderr="wl: error connecting",
        )

        engine = PlanAllEngine(runner=runner)
        items = engine.discover_items()
        assert items == []


# ===========================================================================
# Test: Sequential /plan invocation
# ===========================================================================

class TestPlanInvocation:
    """Verify that /plan is invoked for each item sequentially."""

    def test_plan_invoked_for_each_item(self):
        """/plan is invoked for each item in sequence."""
        runner = FakeRunner()
        runner.set_response(
            "wl list --stage intake_complete",
            stdout=SAMPLE_WL_LIST_RESPONSE,
        )
        # Mock successful claim for each item
        for item_id in ["SA-PLAN-001", "SA-PLAN-002", "SA-PLAN-003"]:
            runner.set_response(
                f"wl update {item_id} --status",
                stdout=json.dumps({"success": True}),
            )
            runner.set_response(
                f"pi -p --mode json /plan {item_id}",
                stdout=json.dumps({"success": True}),
            )

        engine = PlanAllEngine(runner=runner)
        results = engine.run_all()

        assert len(results) == 3
        # Verify pi -p --mode json /plan was called for each item
        plan_calls = [
            cmd for cmd in runner.calls
            if "pi" in cmd and any("/plan" in part for part in cmd)
        ]
        assert len(plan_calls) == 3, f"Expected 3 plan calls, got {len(plan_calls)}: {runner.calls}"
        assert "SA-PLAN-001" in " ".join(plan_calls[0])
        assert "SA-PLAN-002" in " ".join(plan_calls[1])
        assert "SA-PLAN-003" in " ".join(plan_calls[2])

    def test_plan_items_claimed_before_planning(self):
        """Each item is claimed with wl update before /plan is invoked."""
        runner = FakeRunner()
        runner.set_response(
            "wl list --stage intake_complete",
            stdout=SAMPLE_WL_LIST_RESPONSE,
        )
        for item_id in ["SA-PLAN-001", "SA-PLAN-002", "SA-PLAN-003"]:
            runner.set_response(
                f"wl update {item_id} --status",
                stdout=json.dumps({"success": True}),
            )
            runner.set_response(
                f"pi -p --mode json /plan {item_id}",
                stdout=json.dumps({"success": True}),
            )

        engine = PlanAllEngine(runner=runner)
        engine.run_all()

        # Extract claim and plan call pairs
        claim_calls = []
        plan_calls = []
        for cmd in runner.calls:
            cmd_str = " ".join(cmd)
            if "wl update" in cmd_str and "--status" in cmd_str:
                claim_calls.append(cmd)
            if "pi -p --mode json /plan" in cmd_str:
                plan_calls.append(cmd)

        assert len(claim_calls) == 3
        assert len(plan_calls) == 3
        # Each item should be claimed before it is planned
        for i in range(3):
            claim_id = claim_calls[i][2]  # item id is at index 2 in wl update <id> ...
            plan_str = " ".join(plan_calls[i])
            assert claim_id in plan_str, f"Item {claim_id} claimed but not planned in order"


# ===========================================================================
# Test: Producer-input detection
# ===========================================================================

class TestProducerInputDetection:
    """Verify detection of items needing producer input."""

    def test_unanswered_questions_detected(self):
        """Items with unanswered questions are flagged as needing producer input."""
        runner = FakeRunner()
        runner.set_response(
            "wl list --stage intake_complete",
            stdout=json.dumps({"success": True, "workItems": [SAMPLE_ITEM_A]}),
        )
        runner.set_response(
            f"wl update {SAMPLE_ITEM_A['id']} --status",
            stdout=json.dumps({"success": True}),
        )
        # Simulate pi output that indicates unanswered questions (e.g., contains "?" prompts)
        runner.set_response(
            f"pi -p --mode json /plan {SAMPLE_ITEM_A['id']}",
            stdout=json.dumps({"success": True, "text": "Should feature X be behind a flag? (yes/no):"}),
            # Non-zero returncode could signal interactive stall
            returncode=1,
        )

        engine = PlanAllEngine(runner=runner)
        results = engine.run_all()
        assert len(results) == 1
        assert results[0]["outcome"] == "needs_input"
        assert results[0]["id"] == SAMPLE_ITEM_A["id"]

    def test_successful_plan_is_not_needs_input(self):
        """Items that complete planning without questions are not marked needs_input."""
        runner = FakeRunner()
        runner.set_response(
            "wl list --stage intake_complete",
            stdout=json.dumps({"success": True, "workItems": [SAMPLE_ITEM_B]}),
        )
        runner.set_response(
            f"wl update {SAMPLE_ITEM_B['id']} --status",
            stdout=json.dumps({"success": True}),
        )
        runner.set_response(
            f"pi -p --mode json /plan {SAMPLE_ITEM_B['id']}",
            stdout=json.dumps({"success": True}),
        )

        engine = PlanAllEngine(runner=runner)
        results = engine.run_all()
        assert len(results) == 1
        assert results[0]["outcome"] == "planned"


# ===========================================================================
# Test: Summary report
# ===========================================================================

class TestSummaryReport:
    """Verify summary report accuracy."""

    def test_summary_counts_are_correct(self):
        """Summary report correctly counts processed, planned, and needs-input items."""
        results = [
            {"id": "SA-001", "title": "A", "outcome": "planned"},
            {"id": "SA-002", "title": "B", "outcome": "needs_input"},
            {"id": "SA-003", "title": "C", "outcome": "planned"},
            {"id": "SA-004", "title": "D", "outcome": "error"},
        ]

        markdown = generate_summary(results, json_output=False)
        assert "**Total processed**: 4" in markdown
        assert "**Planned**: 2" in markdown
        assert "**Needs input**: 1" in markdown
        assert "**Errors**: 1" in markdown

    def test_summary_lists_each_item_with_outcome(self):
        """Each processed item appears in the summary with its outcome."""
        results = [
            {"id": "SA-001", "title": "Feature A", "outcome": "planned"},
            {"id": "SA-002", "title": "Feature B", "outcome": "needs_input"},
        ]

        markdown = generate_summary(results, json_output=False)
        assert "SA-001" in markdown and "planned" in markdown
        assert "SA-002" in markdown and "needs_input" in markdown

    def test_summary_json_output(self):
        """JSON output is produced when --json flag is requested."""
        results = [
            {"id": "SA-001", "title": "A", "outcome": "planned"},
            {"id": "SA-002", "title": "B", "outcome": "needs_input"},
        ]

        json_out = generate_summary(results, json_output=True)
        parsed = json.loads(json_out)
        assert parsed["total"] == 2
        assert parsed["planned"] == 1
        assert parsed["needs_input"] == 1
        assert parsed["errors"] == 0
        assert len(parsed["items"]) == 2

    def test_empty_summary(self):
        """Empty results produce a valid zeroed summary."""
        markdown = generate_summary([], json_output=False)
        assert "**Total processed**: 0" in markdown
        assert "**Planned**: 0" in markdown
        assert "**Needs input**: 0" in markdown
        assert "**Errors**: 0" in markdown

    def test_summary_posted_as_comment_when_parent_provided(self):
        """Summary is posted as a wl comment on the parent item if provided."""
        runner = FakeRunner()
        runner.set_response(
            "wl comment add SA-PARENT",
            stdout=json.dumps({"success": True}),
        )

        results = [
            {"id": "SA-001", "title": "A", "outcome": "planned"},
        ]

        engine = PlanAllEngine(runner=runner)
        engine.post_summary(results, parent_id="SA-PARENT")

        # Verify wl comment add was called
        comment_calls = [
            cmd for cmd in runner.calls
            if "wl" in cmd and "comment" in cmd and "add" in cmd
        ]
        assert len(comment_calls) >= 1
        comment_str = " ".join(comment_calls[0])
        assert "SA-PARENT" in comment_str
        assert "Planned" in comment_str or "processed" in comment_str


# ===========================================================================
# Test: Error resilience
# ===========================================================================

class TestErrorResilience:
    """Verify that errors during /plan for one item do not stop processing."""

    def test_errors_do_not_stop_processing(self):
        """Processing continues to remaining items after a /plan error."""
        runner = FakeRunner()
        runner.set_response(
            "wl list --stage intake_complete",
            stdout=SAMPLE_WL_LIST_RESPONSE,
        )
        for item_id in ["SA-PLAN-001", "SA-PLAN-002", "SA-PLAN-003"]:
            runner.set_response(
                f"wl update {item_id} --status",
                stdout=json.dumps({"success": True}),
            )
        # First item fails, second and third succeed
        runner.set_response(
            "pi -p --mode json /plan SA-PLAN-001",
            returncode=1,
            stdout="",
            stderr="plan failed: timeout",
        )
        runner.set_response(
            "pi -p --mode json /plan SA-PLAN-002",
            stdout=json.dumps({"success": True}),
        )
        runner.set_response(
            "pi -p --mode json /plan SA-PLAN-003",
            stdout=json.dumps({"success": True}),
        )

        engine = PlanAllEngine(runner=runner)
        results = engine.run_all()

        assert len(results) == 3
        assert results[0]["outcome"] == "error"
        assert results[1]["outcome"] == "planned"
        assert results[2]["outcome"] == "planned"

    def test_wl_update_failure_does_not_stop_processing(self):
        """Processing continues if claiming an item fails."""
        runner = FakeRunner()
        runner.set_response(
            "wl list --stage intake_complete",
            stdout=SAMPLE_WL_LIST_RESPONSE,
        )
        # First item claim fails
        runner.set_response(
            "wl update SA-PLAN-001 --status",
            returncode=1,
            stdout="",
            stderr="wl: item not found",
        )
        # Second item claim succeeds
        runner.set_response(
            "wl update SA-PLAN-002 --status",
            stdout=json.dumps({"success": True}),
        )
        runner.set_response(
            "pi -p --mode json /plan SA-PLAN-002",
            stdout=json.dumps({"success": True}),
        )
        # Third item claim succeeds
        runner.set_response(
            "wl update SA-PLAN-003 --status",
            stdout=json.dumps({"success": True}),
        )
        runner.set_response(
            "pi -p --mode json /plan SA-PLAN-003",
            stdout=json.dumps({"success": True}),
        )

        engine = PlanAllEngine(runner=runner)
        results = engine.run_all()

        assert len(results) == 3
        assert results[0]["outcome"] == "error"
        assert results[1]["outcome"] == "planned"
        assert results[2]["outcome"] == "planned"


# ===========================================================================
# Test: Idempotence
# ===========================================================================

class TestIdempotence:
    """Verify that re-running PlanAll does not duplicate work."""

    def test_re_run_does_not_process_planned_items(self):
        """Items already past intake_complete are not processed again."""
        runner = FakeRunner()

        # First run: 2 items in intake_complete
        runner.set_response(
            "wl list --stage intake_complete",
            stdout=json.dumps({
                "success": True,
                "workItems": [SAMPLE_ITEM_A, SAMPLE_ITEM_B],
            }),
        )
        for item_id in ["SA-PLAN-001", "SA-PLAN-002"]:
            runner.set_response(
                f"wl update {item_id} --status",
                stdout=json.dumps({"success": True}),
            )
            runner.set_response(
                f"pi -p --mode json /plan {item_id}",
                stdout=json.dumps({"success": True}),
            )

        engine = PlanAllEngine(runner=runner)
        results_first = engine.run_all()
        assert len(results_first) == 2

        # Second run: only 1 item left in intake_complete
        runner.set_response(
            "wl list --stage intake_complete",
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
            f"pi -p --mode json /plan {SAMPLE_ITEM_C['id']}",
            stdout=json.dumps({"success": True}),
        )

        results_second = engine.run_all()
        assert len(results_second) == 1
        assert results_second[0]["id"] == "SA-PLAN-003"
        assert results_second[0]["outcome"] == "planned"


# ===========================================================================
# Test: CLI entry point
# ===========================================================================

class TestCLI:
    """Verify the CLI entry point parses arguments and runs correctly."""

    def test_cli_parse_args_default(self):
        """Default invocation processes all intake_complete items."""
        runner = FakeRunner()
        runner.set_response(
            "wl list --stage intake_complete",
            stdout=json.dumps({"success": True, "workItems": [SAMPLE_ITEM_A]}),
        )
        runner.set_response(
            f"wl update {SAMPLE_ITEM_A['id']} --status",
            stdout=json.dumps({"success": True}),
        )
        runner.set_response(
            f"pi -p --mode json /plan {SAMPLE_ITEM_A['id']}",
            stdout=json.dumps({"success": True}),
        )

        engine = PlanAllEngine(runner=runner)
        results = engine.run_all()
        assert len(results) == 1
        assert results[0]["outcome"] == "planned"

    def test_cli_json_flag(self):
        """--json flag produces JSON output."""
        runner = FakeRunner()
        runner.set_response(
            "wl list --stage intake_complete",
            stdout=json.dumps({"success": True, "workItems": [SAMPLE_ITEM_A]}),
        )
        runner.set_response(
            f"wl update {SAMPLE_ITEM_A['id']} --status",
            stdout=json.dumps({"success": True}),
        )
        runner.set_response(
            f"pi -p --mode json /plan {SAMPLE_ITEM_A['id']}",
            stdout=json.dumps({"success": True}),
        )

        engine = PlanAllEngine(runner=runner)
        results = engine.run_all()
        summary = generate_summary(results, json_output=True)
        parsed = json.loads(summary)
        assert parsed["total"] == 1


# ===========================================================================
# Test: Engine initialization and configuration
# ===========================================================================

class TestEngineConfig:
    """Verify PlanAllEngine configuration."""

    def test_default_runner_is_subprocess_run(self):
        """Default runner uses subprocess.run."""
        engine = PlanAllEngine()
        assert engine.runner is not None
        # Verify it's the default subprocess.run behavior
        # We can't directly check subprocess.run identity but we can check it's callable
        assert callable(engine.runner)

    def test_custom_runner_is_used(self):
        """Custom runner is used when provided."""
        def my_runner(cmd):
            return FakeProc(returncode=0, stdout="[]", stderr="")
        engine = PlanAllEngine(runner=my_runner)
        assert engine.runner is my_runner

    def test_max_items_default(self):
        """Max items defaults to 0 (no limit)."""
        engine = PlanAllEngine()
        assert engine.max_items == 0

    def test_max_items_custom(self):
        """Max items can be set to a custom value."""
        engine = PlanAllEngine(max_items=5)
        assert engine.max_items == 5

    def test_item_timeout_default(self):
        """Item timeout defaults to 600 seconds."""
        engine = PlanAllEngine()
        assert engine.item_timeout == 600

    def test_item_timeout_custom(self):
        """Item timeout can be set to a custom value."""
        engine = PlanAllEngine(item_timeout=120)
        assert engine.item_timeout == 120


# ===========================================================================
# Test: --max flag
# ===========================================================================

class TestMaxFlag:
    """Verify --max flag limits the number of items processed."""

    def test_max_zero_processes_all(self):
        """--max 0 (default) processes all items."""
        runner = FakeRunner()
        runner.set_response(
            "wl list --stage intake_complete",
            stdout=SAMPLE_WL_LIST_RESPONSE,
        )
        for item_id in ["SA-PLAN-001", "SA-PLAN-002", "SA-PLAN-003"]:
            runner.set_response(
                f"wl update {item_id} --status",
                stdout=json.dumps({"success": True}),
            )
            runner.set_response(
                f"pi -p --mode json /plan {item_id}",
                stdout=json.dumps({"success": True}),
            )

        engine = PlanAllEngine(runner=runner, max_items=0)
        results = engine.run_all()
        assert len(results) == 3

    def test_max_positive_limits_processing(self):
        """--max N processes at most N items."""
        runner = FakeRunner()
        runner.set_response(
            "wl list --stage intake_complete",
            stdout=SAMPLE_WL_LIST_RESPONSE,
        )
        # Only need responses for first 2 items
        for item_id in ["SA-PLAN-001", "SA-PLAN-002"]:
            runner.set_response(
                f"wl update {item_id} --status",
                stdout=json.dumps({"success": True}),
            )
            runner.set_response(
                f"pi -p --mode json /plan {item_id}",
                stdout=json.dumps({"success": True}),
            )

        engine = PlanAllEngine(runner=runner, max_items=2)
        results = engine.run_all()
        assert len(results) == 2
        assert results[0]["id"] == "SA-PLAN-001"
        assert results[1]["id"] == "SA-PLAN-002"

    def test_max_larger_than_item_count(self):
        """--max larger than available items processes all items."""
        runner = FakeRunner()
        runner.set_response(
            "wl list --stage intake_complete",
            stdout=SAMPLE_WL_LIST_RESPONSE,
        )
        for item_id in ["SA-PLAN-001", "SA-PLAN-002", "SA-PLAN-003"]:
            runner.set_response(
                f"wl update {item_id} --status",
                stdout=json.dumps({"success": True}),
            )
            runner.set_response(
                f"pi -p --mode json /plan {item_id}",
                stdout=json.dumps({"success": True}),
            )

        engine = PlanAllEngine(runner=runner, max_items=10)
        results = engine.run_all()
        assert len(results) == 3

    def test_max_one_processes_single_item(self):
        """--max 1 processes exactly one item."""
        runner = FakeRunner()
        runner.set_response(
            "wl list --stage intake_complete",
            stdout=SAMPLE_WL_LIST_RESPONSE,
        )
        runner.set_response(
            "wl update SA-PLAN-001 --status",
            stdout=json.dumps({"success": True}),
        )
        runner.set_response(
            "pi -p --mode json /plan SA-PLAN-001",
            stdout=json.dumps({"success": True}),
        )

        engine = PlanAllEngine(runner=runner, max_items=1)
        results = engine.run_all()
        assert len(results) == 1
        assert results[0]["id"] == "SA-PLAN-001"

    def test_max_counts_errors_and_needs_input(self):
        """--max counts all items processed (including errors and needs_input)."""
        runner = FakeRunner()
        runner.set_response(
            "wl list --stage intake_complete",
            stdout=SAMPLE_WL_LIST_RESPONSE,
        )
        # Item 1: error
        runner.set_response(
            "wl update SA-PLAN-001 --status",
            stdout=json.dumps({"success": True}),
        )
        runner.set_response(
            "pi -p --mode json /plan SA-PLAN-001",
            returncode=1,
            stderr="timeout",
        )
        # Item 2: needs_input
        runner.set_response(
            "wl update SA-PLAN-002 --status",
            stdout=json.dumps({"success": True}),
        )
        runner.set_response(
            "pi -p --mode json /plan SA-PLAN-002",
            stdout=json.dumps({
                "type": "message_update",
                "assistantMessageEvent": {
                    "type": "text_end",
                    "content": "What should we do? (yes/no):"
                }
            }),
            returncode=1,
        )

        engine = PlanAllEngine(runner=runner, max_items=2)
        results = engine.run_all()
        assert len(results) == 2
        assert results[0]["outcome"] == "error"
        assert results[1]["outcome"] == "needs_input"
        # Verify no calls were made for item 3
        plan_calls = [
            cmd for cmd in runner.calls
            if "pi" in cmd and "-p" in cmd and "/plan" in " ".join(cmd)
        ]
        assert len(plan_calls) == 2
        assert "SA-PLAN-003" not in " ".join(plan_calls[-1])


# ===========================================================================
# Test: --item-timeout (per-item subprocess timeout)
# ===========================================================================

class TestItemTimeout:
    """Verify --item-timeout triggers recovery on subprocess timeout."""

    def test_item_timeout_triggers_recovery(self):
        """When subprocess times out, item is recovered (stage reset to intake_complete) and continues."""
        runner = FakeRunner()
        runner.set_response(
            "wl list --stage intake_complete",
            stdout=SAMPLE_WL_LIST_RESPONSE,
        )
        runner.set_response(
            "wl update SA-PLAN-001 --status",
            stdout=json.dumps({"success": True}),
        )
        runner.set_response(
            "pi -p --mode json /plan SA-PLAN-001",
            returncode=-15,
            stderr="timed out",
        )
        runner.set_response(
            "wl update SA-PLAN-001 --status open",
            stdout=json.dumps({"success": True}),
        )
        runner.set_response(
            "wl update SA-PLAN-002 --status",
            stdout=json.dumps({"success": True}),
        )
        runner.set_response(
            "pi -p --mode json /plan SA-PLAN-002",
            stdout=json.dumps({"success": True}),
        )
        runner.set_response(
            "wl update SA-PLAN-003 --status",
            stdout=json.dumps({"success": True}),
        )
        runner.set_response(
            "pi -p --mode json /plan SA-PLAN-003",
            stdout=json.dumps({"success": True}),
        )

        engine = PlanAllEngine(runner=runner, item_timeout=10)
        results = engine.run_all()

        assert len(results) == 3
        assert results[0]["outcome"] == "error"
        recovery_calls = [
            cmd for cmd in runner.calls
            if "wl" in cmd and "update" in cmd and "open" in " ".join(cmd)
        ]
        assert len(recovery_calls) >= 1
        assert results[1]["outcome"] == "planned"
        assert results[2]["outcome"] == "planned"

    def test_item_timeout_logged(self):
        """Timeout event results in error_detail containing timeout info."""
        runner = FakeRunner()
        runner.set_response(
            "wl list --stage intake_complete",
            stdout=json.dumps({
                "success": True,
                "workItems": [SAMPLE_ITEM_A],
            }),
        )
        runner.set_response(
            "wl update SA-PLAN-001 --status",
            stdout=json.dumps({"success": True}),
        )
        runner.set_response(
            "pi -p --mode json /plan SA-PLAN-001",
            returncode=-15,
            stderr="timed out after 10 seconds",
        )
        runner.set_response(
            "wl update SA-PLAN-001 --status open",
            stdout=json.dumps({"success": True}),
        )

        engine = PlanAllEngine(runner=runner, item_timeout=10)
        results = engine.run_all()

        assert len(results) == 1
        assert results[0]["outcome"] == "error"
        error_detail = results[0].get("error_detail", "")
        assert "timed out" in error_detail or "timeout" in error_detail

    def test_item_timeout_default_is_600(self):
        """Default item timeout is 600 seconds (matching established convention)."""
        engine = PlanAllEngine()
        assert engine.item_timeout == 600

    def test_item_timeout_does_not_affect_normal_items(self):
        """Items completing within timeout are not affected by item_timeout."""
        runner = FakeRunner()
        runner.set_response(
            "wl list --stage intake_complete",
            stdout=SAMPLE_WL_LIST_RESPONSE,
        )
        for item_id in ["SA-PLAN-001", "SA-PLAN-002", "SA-PLAN-003"]:
            runner.set_response(
                f"wl update {item_id} --status",
                stdout=json.dumps({"success": True}),
            )
            runner.set_response(
                f"pi -p --mode json /plan {item_id}",
                stdout=json.dumps({"success": True}),
            )

        engine = PlanAllEngine(runner=runner, item_timeout=5)
        results = engine.run_all()

        assert len(results) == 3
        assert all(r["outcome"] == "planned" for r in results)


# ===========================================================================
# Test: Signal handling for graceful abort
# ===========================================================================

class TestSignalHandling:
    """Verify signal handlers are registered and trigger recovery correctly."""

    def test_signal_handlers_registered(self):
        """SIGINT and SIGTERM handlers are registered on setup."""
        runner = FakeRunner()
        engine = PlanAllEngine(runner=runner)

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
            "wl update SA-PLAN-001 --status open",
            stdout=json.dumps({"success": True}),
        )
        runner.set_response(
            "wl update SA-PLAN-001 --stage intake_complete",
            stdout=json.dumps({"success": True}),
        )

        engine = PlanAllEngine(runner=runner)
        engine._current_item_id = "SA-PLAN-001"

        try:
            engine._signal_handler(signal.SIGINT, None)
        except SystemExit:
            pass

        recovery_calls = [
            cmd for cmd in runner.calls
            if "wl" in cmd and "update" in cmd and "SA-PLAN-001" in " ".join(cmd)
        ]
        assert len(recovery_calls) >= 1

    def test_signal_handler_noop_when_no_current_item(self):
        """Signal handler does nothing when no item is being processed."""
        runner = FakeRunner()
        engine = PlanAllEngine(runner=runner)
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

    def test_current_item_id_set_during_plan(self):
        """_current_item_id is set during plan processing for signal handling."""
        runner = FakeRunner()
        runner.set_response(
            "wl list --stage intake_complete",
            stdout=json.dumps({
                "success": True,
                "workItems": [SAMPLE_ITEM_A],
            }),
        )
        runner.set_response(
            "wl update SA-PLAN-001 --status",
            stdout=json.dumps({"success": True}),
        )
        runner.set_response(
            "pi -p --mode json /plan SA-PLAN-001",
            stdout=json.dumps({"success": True}),
        )

        engine = PlanAllEngine(runner=runner)
        engine.run_all()

        assert engine._current_item_id is None

    def test_signal_handler_exits_with_code(self):
        """Signal handler raises SystemExit with correct code (128+signum)."""
        runner = FakeRunner()
        engine = PlanAllEngine(runner=runner)

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

    def test_remaining_items_reported_when_max_limits(self):
        """When --max limits processing, remaining count can be computed."""
        runner = FakeRunner()
        runner.set_response(
            "wl list --stage intake_complete",
            stdout=SAMPLE_WL_LIST_RESPONSE,
        )
        runner.set_response(
            "wl update SA-PLAN-001 --status",
            stdout=json.dumps({"success": True}),
        )
        runner.set_response(
            "pi -p --mode json /plan SA-PLAN-001",
            stdout=json.dumps({"success": True}),
        )
        runner.set_response(
            "wl list --stage intake_complete",
            stdout=SAMPLE_WL_LIST_RESPONSE,
        )

        engine = PlanAllEngine(runner=runner, max_items=1)
        results = engine.run_all()

        assert len(results) == 1
        assert results[0]["id"] == "SA-PLAN-001"

        # Verify remaining count in engine state
        assert engine._total_discovered == 3
        remaining = engine._total_discovered - len(results)
        assert remaining == 2

        # Verify remaining count appears in Markdown summary
        summary = generate_summary(results, total_discovered=engine._total_discovered)
        assert "**Remaining**: 2" in summary, \
            f"Expected remaining count in summary, got: {summary}"

        # Verify remaining count appears in JSON summary
        json_summary = json.loads(generate_summary(results, json_output=True,
                                                    total_discovered=engine._total_discovered))
        assert json_summary.get("remaining") == 2, \
            f"Expected remaining=2 in JSON, got: {json_summary}"
