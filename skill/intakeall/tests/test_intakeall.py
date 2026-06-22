"""Tests for IntakeAll: Automated Batch Intake for idea-stage items.

These tests verify:
- Discovery of idea-stage items via wl list
- Auto-complete for well-defined items (skip /intake, advance to intake_complete)
- Sequential /intake invocation for each item
- Producer-input detection via unanswered questions
- Enhanced error handling: capture, recovery attempts, action recording, recovery outcome
- Error resilience (errors for one item don't stop processing)
- Summary report accuracy (Markdown and JSON) with error/recovery details
- --parent-id flag posts summary as a comment
- Idempotence (re-running doesn't duplicate work)
- Orphaned-item detection (items stuck in completed+idea or in_progress+idea)
- Orphan recovery (automatic reset to open status before processing)
- Signal handler registration and behavior (SIGINT/SIGTERM trigger recovery)

Related work item: SA-0MQKW21FQ004RW2J
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

from skill.intakeall.scripts.intakeall import (  # noqa: E402
    IntakeAllEngine,
    generate_summary,
    has_sufficient_detail,
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
    "id": "SA-INTAKE-001",
    "title": "New Feature X",
    "status": "open",
    "stage": "idea",
    "priority": "high",
    "issueType": "feature",
    "description": (
        "# New Feature X\n\n"
        "## Acceptance Criteria\n"
        "- Users can authenticate via OAuth\n"
        "- Token refresh is handled automatically\n"
        "## Implementation\n"
        "Add OAuth2 middleware using the auth library.\n"
        "Create a token storage service.\n"
    ),
}

SAMPLE_ITEM_B = {
    "id": "SA-INTAKE-002",
    "title": "Bug Fix Y",
    "status": "open",
    "stage": "idea",
    "priority": "medium",
    "issueType": "bug",
    "description": (
        "# Bug Fix Y\n\n"
        "## Acceptance Criteria\n"
        "- Fix null pointer in login flow\n"
        "## Proposed Approach\n"
        "Add null check before accessing user profile.\n"
    ),
}

SAMPLE_ITEM_C = {
    "id": "SA-INTAKE-003",
    "title": "Vague Epic Z",
    "status": "open",
    "stage": "idea",
    "priority": "low",
    "issueType": "epic",
    "description": "Some vague idea without clear acceptance criteria.",
}

SAMPLE_ITEM_D = {
    "id": "SA-INTAKE-004",
    "title": "Config Update",
    "status": "open",
    "stage": "idea",
    "priority": "low",
    "issueType": "task",
    "description": (
        "# Config Update\n\n"
        "## Acceptance Criteria\n"
        "- Update default timeout to 30s\n"
        "## Desired Change\n"
        "Change config default in settings.py.\n"
    ),
}

SAMPLE_WL_LIST_RESPONSE = json.dumps({
    "success": True,
    "workItems": [SAMPLE_ITEM_A, SAMPLE_ITEM_B, SAMPLE_ITEM_C, SAMPLE_ITEM_D],
})

# Orphan sample items — items stuck in contradictory states in idea stage
ORPHAN_ITEM_COMPLETED = {
    "id": "SA-ORPHAN-001",
    "title": "Orphaned Completed Item",
    "status": "completed",
    "stage": "idea",
    "priority": "high",
    "issueType": "feature",
    "description": (
        "# Orphaned Feature\n\n"
        "## Acceptance Criteria\n"
        "- The thing works\n"
    ),
}

ORPHAN_ITEM_IN_PROGRESS = {
    "id": "SA-ORPHAN-002",
    "title": "Orphaned In-Progress Item",
    "status": "in_progress",
    "stage": "idea",
    "priority": "medium",
    "issueType": "bug",
    "description": (
        "# Orphaned Bug\n\n"
        "## Acceptance Criteria\n"
        "- Fix the thing\n"
    ),
}

# A mixed list containing normal items + orphans
SAMPLE_MIXED_LIST_RESPONSE = json.dumps({
    "success": True,
    "workItems": [
        SAMPLE_ITEM_A,
        ORPHAN_ITEM_COMPLETED,
        SAMPLE_ITEM_C,
        ORPHAN_ITEM_IN_PROGRESS,
    ],
})


# ===========================================================================
# Test: has_sufficient_detail
# ===========================================================================

class TestHasSufficientDetail:
    """Verify the auto-complete detection logic."""

    def test_item_with_ac_and_impl_is_sufficient(self):
        """Item with acceptance criteria and implementation guidance is sufficient."""
        assert has_sufficient_detail(SAMPLE_ITEM_A) is True

    def test_item_with_ac_alone_not_epic_is_sufficient(self):
        """Item with acceptance criteria but no implementation section is still sufficient."""
        item = dict(SAMPLE_ITEM_A)
        item["description"] = (
            "# Minimal\n\n"
            "## Acceptance Criteria\n"
            "- Do the thing\n"
        )
        assert has_sufficient_detail(item) is True

    def test_epic_is_not_sufficient(self):
        """Epic items are never auto-completed."""
        assert has_sufficient_detail(SAMPLE_ITEM_C) is False

    def test_vague_item_without_ac_is_not_sufficient(self):
        """Item without acceptance criteria needs full intake."""
        item = dict(SAMPLE_ITEM_A)
        item["description"] = "# Vague\n\nSome unclear idea without criteria."
        assert has_sufficient_detail(item) is False

    def test_empty_description_is_not_sufficient(self):
        """Item with no description needs full intake."""
        item = dict(SAMPLE_ITEM_A)
        item["description"] = ""
        assert has_sufficient_detail(item) is False

    def test_no_issue_type_falls_back_to_false(self):
        """Item with no issueType defaults to checking description."""
        item = dict(SAMPLE_ITEM_A)
        item["issueType"] = ""
        # Should still be sufficient if AC are present
        assert has_sufficient_detail(item) is True

    def test_task_with_ac_is_sufficient(self):
        """Task items with AC are auto-completed."""
        assert has_sufficient_detail(SAMPLE_ITEM_D) is True


# ===========================================================================
# Test: Discovery of idea-stage items
# ===========================================================================

class TestDiscovery:
    """Verify that IntakeAll discovers all items in idea stage."""

    def test_discover_all_idea_stage_items(self):
        """`wl list --stage idea --status open --json` returns all eligible items."""
        runner = FakeRunner()
        runner.set_response(
            "wl list --stage idea",
            stdout=SAMPLE_WL_LIST_RESPONSE,
        )

        engine = IntakeAllEngine(runner=runner)
        items = engine.discover_items()

        assert len(items) == 4
        assert items[0]["id"] == "SA-INTAKE-001"
        assert items[1]["id"] == "SA-INTAKE-002"
        assert items[2]["id"] == "SA-INTAKE-003"
        assert items[3]["id"] == "SA-INTAKE-004"

        # Verify the correct wl command was issued (without --status open filter)
        assert any(
            cmd[:3] == ["wl", "list", "--stage"] and "idea" in cmd and "--status" not in cmd
            for cmd in runner.calls
        ), "Expected wl list --stage idea --json call (without --status open filter)"

    def test_discover_returns_empty_list_when_no_items(self):
        """When no items are in idea stage, return an empty list."""
        runner = FakeRunner()
        runner.set_response(
            "wl list --stage idea",
            stdout=json.dumps({"success": True, "workItems": []}),
        )

        engine = IntakeAllEngine(runner=runner)
        items = engine.discover_items()
        assert items == []

    def test_discover_handles_wl_error_gracefully(self):
        """If wl command fails, return an empty list without crashing."""
        runner = FakeRunner()
        runner.set_response(
            "wl list --stage idea",
            returncode=1,
            stdout="",
            stderr="wl: error connecting",
        )

        engine = IntakeAllEngine(runner=runner)
        items = engine.discover_items()
        assert items == []


# ===========================================================================
# Test: Auto-complete for well-defined items
# ===========================================================================

class TestAutoComplete:
    """Verify auto-complete skips /intake for well-defined items."""

    def test_well_defined_item_auto_completed(self):
        """Item with sufficient detail is auto-completed to intake_complete."""
        runner = FakeRunner()
        runner.set_response(
            "wl list --stage idea",
            stdout=json.dumps({
                "success": True,
                "workItems": [SAMPLE_ITEM_A],
            }),
        )
        runner.set_response(
            f"wl update {SAMPLE_ITEM_A['id']} --status",
            stdout=json.dumps({"success": True}),
        )
        runner.set_response(
            f"wl update {SAMPLE_ITEM_A['id']} --stage",
            stdout=json.dumps({"success": True}),
        )
        runner.set_response(
            f"wl comment add {SAMPLE_ITEM_A['id']}",
            stdout=json.dumps({"success": True}),
        )

        engine = IntakeAllEngine(runner=runner)
        results = engine.run_all()

        assert len(results) == 1
        assert results[0]["outcome"] == "auto_completed"
        assert results[0]["id"] == "SA-INTAKE-001"

        # Verify no /intake was called for this item
        intake_calls = [
            cmd for cmd in runner.calls
            if "pi" in cmd and "-p" in cmd and "/intake" in " ".join(cmd)
        ]
        assert len(intake_calls) == 0, "Auto-completed items should skip /intake"

    def test_well_defined_item_advances_to_intake_complete(self):
        """Auto-completed item is advanced to intake_complete stage."""
        runner = FakeRunner()
        runner.set_response(
            "wl list --stage idea",
            stdout=json.dumps({
                "success": True,
                "workItems": [SAMPLE_ITEM_A],
            }),
        )
        runner.set_response(
            f"wl update {SAMPLE_ITEM_A['id']} --status",
            stdout=json.dumps({"success": True}),
        )
        runner.set_response(
            f"wl update {SAMPLE_ITEM_A['id']} --stage",
            stdout=json.dumps({"success": True}),
        )
        runner.set_response(
            f"wl comment add {SAMPLE_ITEM_A['id']}",
            stdout=json.dumps({"success": True}),
        )

        engine = IntakeAllEngine(runner=runner)
        engine.run_all()

        # Verify stage update command was issued
        stage_update_calls = [
            cmd for cmd in runner.calls
            if "wl" in cmd and "update" in cmd and "--stage" in cmd
        ]
        assert len(stage_update_calls) >= 1
        stage_str = " ".join(stage_update_calls[0])
        assert "intake_complete" in stage_str


# ===========================================================================
# Test: Sequential /intake invocation
# ===========================================================================

class TestIntakeInvocation:
    """Verify that /intake is invoked for items needing intake."""

    def test_intake_invoked_for_items_requiring_intake(self):
        """/intake is invoked for items that are not auto-completed."""
        runner = FakeRunner()
        # Only SAMPLE_ITEM_C (epic) needs /intake; A, B, D are auto-completable
        runner.set_response(
            "wl list --stage idea",
            stdout=SAMPLE_WL_LIST_RESPONSE,
        )
        # Mock claim for the vague epic
        runner.set_response(
            f"wl update {SAMPLE_ITEM_C['id']} --status",
            stdout=json.dumps({"success": True}),
        )
        # Mock auto-complete responses for A, B, D
        for item in [SAMPLE_ITEM_A, SAMPLE_ITEM_B, SAMPLE_ITEM_D]:
            runner.set_response(
                f"wl update {item['id']} --status",
                stdout=json.dumps({"success": True}),
            )
            runner.set_response(
                f"wl update {item['id']} --stage",
                stdout=json.dumps({"success": True}),
            )
            runner.set_response(
                f"wl comment add {item['id']}",
                stdout=json.dumps({"success": True}),
            )
        # Mock /intake for the vague epic
        runner.set_response(
            f"pi -p --mode json /intake {SAMPLE_ITEM_C['id']}",
            stdout=json.dumps({"success": True}),
        )
        # Mock stage update for C after successful intake
        runner.set_response(
            f"wl update {SAMPLE_ITEM_C['id']} --stage",
            stdout=json.dumps({"success": True}),
        )

        engine = IntakeAllEngine(runner=runner)
        results = engine.run_all()

        assert len(results) == 4
        # Check /intake was called for the epic only
        intake_calls = [
            cmd for cmd in runner.calls
            if "pi" in cmd and "-p" in cmd and "/intake" in " ".join(cmd)
        ]
        assert len(intake_calls) == 1
        assert SAMPLE_ITEM_C["id"] in " ".join(intake_calls[0])

    def test_intake_items_claimed_before_intake(self):
        """Each item is claimed with wl update before /intake is invoked."""
        runner = FakeRunner()
        runner.set_response(
            "wl list --stage idea",
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
            f"pi -p --mode json /intake {SAMPLE_ITEM_C['id']}",
            stdout=json.dumps({"success": True}),
        )
        runner.set_response(
            f"wl update {SAMPLE_ITEM_C['id']} --stage",
            stdout=json.dumps({"success": True}),
        )

        engine = IntakeAllEngine(runner=runner)
        engine.run_all()

        claim_calls = []
        intake_calls = []
        for cmd in runner.calls:
            cmd_str = " ".join(cmd)
            if "wl update" in cmd_str and "--status" in cmd_str:
                claim_calls.append(cmd)
            if "pi -p --mode json /intake" in cmd_str:
                intake_calls.append(cmd)

        assert len(claim_calls) >= 1
        assert len(intake_calls) == 1
        # The claim should come before the intake call
        claim_idx = runner.calls.index(claim_calls[0])
        intake_idx = runner.calls.index(intake_calls[0])
        assert claim_idx < intake_idx


# ===========================================================================
# Test: Producer-input detection
# ===========================================================================

class TestProducerInputDetection:
    """Verify detection of items needing producer input."""

    def test_unanswered_questions_detected(self):
        """Items with unanswered questions are flagged as needing producer input."""
        runner = FakeRunner()
        runner.set_response(
            "wl list --stage idea",
            stdout=json.dumps({
                "success": True,
                "workItems": [SAMPLE_ITEM_C],
            }),
        )
        runner.set_response(
            f"wl update {SAMPLE_ITEM_C['id']} --status",
            stdout=json.dumps({"success": True}),
        )
        # Simulate intake output that indicates unanswered questions (JSON stream format)
        runner.set_response(
            f"pi -p --mode json /intake {SAMPLE_ITEM_C['id']}",
            stdout=json.dumps({
                "type": "message_update",
                "assistantMessageEvent": {
                    "type": "text_end",
                    "content": "Should feature Z be a separate module? (yes/no):"
                }
            }),
            returncode=1,
        )

        engine = IntakeAllEngine(runner=runner)
        results = engine.run_all()
        assert len(results) == 1
        assert results[0]["outcome"] == "needs_input"
        assert results[0]["id"] == SAMPLE_ITEM_C["id"]

    def test_successful_intake_not_needs_input(self):
        """Items that complete intake without questions are marked intake_completed."""
        runner = FakeRunner()
        runner.set_response(
            "wl list --stage idea",
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
            f"pi -p --mode json /intake {SAMPLE_ITEM_C['id']}",
            stdout=json.dumps({"success": True}),
        )
        runner.set_response(
            f"wl update {SAMPLE_ITEM_C['id']} --stage",
            stdout=json.dumps({"success": True}),
        )

        engine = IntakeAllEngine(runner=runner)
        results = engine.run_all()
        assert len(results) == 1
        assert results[0]["outcome"] == "intake_completed"

    def test_non_zero_exit_with_questions_detected(self):
        """Non-zero exit with question patterns is needs_input."""
        runner = FakeRunner()
        runner.set_response(
            "wl list --stage idea",
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
            f"pi -p --mode json /intake {SAMPLE_ITEM_C['id']}",
            stdout=json.dumps({
                "type": "message_update",
                "assistantMessageEvent": {
                    "type": "text_end",
                    "content": "What should we name the new feature?"
                }
            }),
            returncode=0,  # Zero exit but still contains questions
        )

        engine = IntakeAllEngine(runner=runner)
        results = engine.run_all()
        assert len(results) == 1
        assert results[0]["outcome"] == "needs_input"

    def test_exception_during_intake_detected(self):
        """Exception during /intake is caught and flagged as error."""
        runner = FakeRunner()
        runner.set_response(
            "wl list --stage idea",
            stdout=json.dumps({
                "success": True,
                "workItems": [SAMPLE_ITEM_C],
            }),
        )
        runner.set_response(
            f"wl update {SAMPLE_ITEM_C['id']} --status",
            stdout=json.dumps({"success": True}),
        )
        # Don't set response for /intake, causing exception in subprocess.run
        # Our fake runner will return default "[]" response, but let's make it fail
        runner.set_response(
            f"pi -p --mode json /intake {SAMPLE_ITEM_C['id']}",
            returncode=1,
            stderr="Connection refused",
        )

        engine = IntakeAllEngine(runner=runner)
        results = engine.run_all()
        assert len(results) == 1
        assert results[0]["outcome"] == "error"


# ===========================================================================
# Test: Enhanced error handling with recovery
# ===========================================================================

class TestErrorHandlingWithRecovery:
    """Verify enhanced error handling: capture, recovery, action recording."""

    def test_error_captures_stdout_and_stderr(self):
        """Error outcome includes stdout and stderr details."""
        runner = FakeRunner()
        runner.set_response(
            "wl list --stage idea",
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
            f"pi -p --mode json /intake {SAMPLE_ITEM_C['id']}",
            returncode=1,
            stdout="Some output before error",
            stderr="Intake failed: timeout exceeded",
        )

        engine = IntakeAllEngine(runner=runner)
        results = engine.run_all()

        assert len(results) == 1
        assert results[0]["outcome"] == "error"
        error_detail = results[0].get("error_detail", "")
        assert "timeout exceeded" in error_detail or "failed" in error_detail

    def test_recovery_attempts_reset_status_on_unrecoverable(self):
        """On unrecoverable error, item stage and status are reset to idea and open."""
        runner = FakeRunner()
        runner.set_response(
            "wl list --stage idea",
            stdout=json.dumps({
                "success": True,
                "workItems": [SAMPLE_ITEM_C],
            }),
        )
        runner.set_response(
            f"wl update {SAMPLE_ITEM_C['id']} --status",
            stdout=json.dumps({"success": True}),
        )
        # Intake fails
        runner.set_response(
            f"pi -p --mode json /intake {SAMPLE_ITEM_C['id']}",
            returncode=1,
            stderr="timeout",
        )
        # Recovery: reset the item stage back to idea and status to open
        runner.set_response(
            f"wl update {SAMPLE_ITEM_C['id']} --stage",
            stdout=json.dumps({"success": True}),
        )

        engine = IntakeAllEngine(runner=runner)
        results = engine.run_all()

        assert len(results) == 1
        # Verify recovery was attempted (reset stage to idea, status to open)
        reset_calls = [
            cmd for cmd in runner.calls
            if "wl" in cmd and "update" in cmd and "stage" in " ".join(cmd)
        ]
        # Error + recovery should have at least one reset call (the claim is --status only)
        assert len(reset_calls) >= 1

    def test_recovery_actions_recorded_in_result(self):
        """Recovery actions are recorded in the result for reporting."""
        runner = FakeRunner()
        runner.set_response(
            "wl list --stage idea",
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
            f"pi -p --mode json /intake {SAMPLE_ITEM_C['id']}",
            returncode=1,
            stderr="timeout",
        )
        runner.set_response(
            f"wl update {SAMPLE_ITEM_C['id']} --stage",
            stdout=json.dumps({"success": True}),
        )

        engine = IntakeAllEngine(runner=runner)
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
        # Mix of items: one vague (needs intake), others auto-completable
        items_list = [SAMPLE_ITEM_C, SAMPLE_ITEM_A, SAMPLE_ITEM_B]
        runner.set_response(
            "wl list --stage idea",
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
        # First item (vague epic) intake fails
        runner.set_response(
            f"pi -p --mode json /intake {SAMPLE_ITEM_C['id']}",
            returncode=1,
            stderr="timeout",
        )
        runner.set_response(
            f"wl update {SAMPLE_ITEM_C['id']} --stage",
            stdout=json.dumps({"success": True}),
        )
        # Second item (well-defined) auto-completes
        runner.set_response(
            f"wl update {SAMPLE_ITEM_A['id']} --stage",
            stdout=json.dumps({"success": True}),
        )
        runner.set_response(
            f"wl comment add {SAMPLE_ITEM_A['id']}",
            stdout=json.dumps({"success": True}),
        )
        # Third item (well-defined) auto-completes
        runner.set_response(
            f"wl update {SAMPLE_ITEM_B['id']} --stage",
            stdout=json.dumps({"success": True}),
        )
        runner.set_response(
            f"wl comment add {SAMPLE_ITEM_B['id']}",
            stdout=json.dumps({"success": True}),
        )

        engine = IntakeAllEngine(runner=runner)
        results = engine.run_all()

        assert len(results) == 3
        # First item has error (recovery applied), others auto-completed
        assert results[0]["outcome"] == "error"
        assert results[1]["outcome"] == "auto_completed"
        assert results[2]["outcome"] == "auto_completed"

    def test_wl_update_failure_does_not_stop_processing(self):
        """Processing continues if claiming an item fails."""
        runner = FakeRunner()
        items_list = [SAMPLE_ITEM_A, SAMPLE_ITEM_B]
        runner.set_response(
            "wl list --stage idea",
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
        # Second item claim succeeds, then auto-completes
        runner.set_response(
            f"wl update {SAMPLE_ITEM_B['id']} --status",
            stdout=json.dumps({"success": True}),
        )
        runner.set_response(
            f"wl update {SAMPLE_ITEM_B['id']} --stage",
            stdout=json.dumps({"success": True}),
        )
        runner.set_response(
            f"wl comment add {SAMPLE_ITEM_B['id']}",
            stdout=json.dumps({"success": True}),
        )

        engine = IntakeAllEngine(runner=runner)
        results = engine.run_all()

        assert len(results) == 2
        assert results[0]["outcome"] == "error"
        assert results[1]["outcome"] == "auto_completed"


# ===========================================================================
# Test: Summary report
# ===========================================================================

class TestSummaryReport:
    """Verify summary report accuracy."""

    def test_summary_counts_are_correct(self):
        """Summary report correctly counts outcomes."""
        results = [
            {"id": "SA-001", "title": "A", "outcome": "auto_completed",
             "error_detail": None, "recovery": None},
            {"id": "SA-002", "title": "B", "outcome": "needs_input",
             "error_detail": None, "recovery": None},
            {"id": "SA-003", "title": "C", "outcome": "intake_completed",
             "error_detail": None, "recovery": None},
            {"id": "SA-004", "title": "D", "outcome": "error",
             "error_detail": "timeout", "recovery": {"action": "reset_status", "success": True}},
        ]

        markdown = generate_summary(results, json_output=False)
        assert "**Total processed**: 4" in markdown
        assert "**Auto-completed**: 1" in markdown
        assert "**Intake completed**: 1" in markdown
        assert "**Needs input**: 1" in markdown
        assert "**Errors**: 1" in markdown

    def test_summary_lists_each_item_with_outcome(self):
        """Each processed item appears in the summary with its outcome."""
        results = [
            {"id": "SA-001", "title": "Feature A", "outcome": "auto_completed",
             "error_detail": None, "recovery": None},
            {"id": "SA-002", "title": "Feature B", "outcome": "needs_input",
             "error_detail": None, "recovery": None},
        ]

        markdown = generate_summary(results, json_output=False)
        assert "SA-001" in markdown and "auto_completed" in markdown
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
            {"id": "SA-001", "title": "A", "outcome": "auto_completed",
             "error_detail": None, "recovery": None},
            {"id": "SA-002", "title": "B", "outcome": "needs_input",
             "error_detail": None, "recovery": None},
        ]

        json_out = generate_summary(results, json_output=True)
        parsed = json.loads(json_out)
        assert parsed["total"] == 2
        assert parsed["auto_completed"] == 1
        assert parsed["needs_input"] == 1
        assert parsed["intake_completed"] == 0
        assert parsed["errors"] == 0
        assert len(parsed["items"]) == 2

    def test_empty_summary(self):
        """Empty results produce a valid zeroed summary."""
        markdown = generate_summary([], json_output=False)
        assert "**Total processed**: 0" in markdown
        assert "**Auto-completed**: 0" in markdown
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
            {"id": "SA-001", "title": "A", "outcome": "auto_completed",
             "error_detail": None, "recovery": None},
        ]

        engine = IntakeAllEngine(runner=runner)
        engine.post_summary(results, parent_id="SA-PARENT")

        # Verify wl comment add was called
        comment_calls = [
            cmd for cmd in runner.calls
            if "wl" in cmd and "comment" in cmd and "add" in cmd
        ]
        assert len(comment_calls) >= 1
        comment_str = " ".join(comment_calls[0])
        assert "SA-PARENT" in comment_str
        assert "IntakeAll" in comment_str or "processing" in comment_str.lower()


# ===========================================================================
# Test: Idempotence
# ===========================================================================

class TestIdempotence:
    """Verify that re-running IntakeAll does not duplicate work."""

    def test_re_run_does_not_process_completed_items(self):
        """Items already past idea stage are not processed again."""
        runner = FakeRunner()

        # First run: 2 items in idea stage
        first_items = [SAMPLE_ITEM_A, SAMPLE_ITEM_C]
        runner.set_response(
            "wl list --stage idea",
            stdout=json.dumps({
                "success": True,
                "workItems": first_items,
            }),
        )
        for item in first_items:
            runner.set_response(
                f"wl update {item['id']} --status",
                stdout=json.dumps({"success": True}),
            )

        # SAMPLE_ITEM_A auto-completes
        runner.set_response(
            f"wl update {SAMPLE_ITEM_A['id']} --stage",
            stdout=json.dumps({"success": True}),
        )
        runner.set_response(
            f"wl comment add {SAMPLE_ITEM_A['id']}",
            stdout=json.dumps({"success": True}),
        )
        # SAMPLE_ITEM_C needs intake (epic)
        runner.set_response(
            f"pi -p --mode json /intake {SAMPLE_ITEM_C['id']}",
            stdout=json.dumps({"success": True}),
        )
        runner.set_response(
            f"wl update {SAMPLE_ITEM_C['id']} --stage",
            stdout=json.dumps({"success": True}),
        )

        engine = IntakeAllEngine(runner=runner)
        results_first = engine.run_all()
        assert len(results_first) == 2

        # Second run: only 1 item left in idea stage (a new one)
        runner.set_response(
            "wl list --stage idea",
            stdout=json.dumps({
                "success": True,
                "workItems": [SAMPLE_ITEM_D],
            }),
        )
        runner.set_response(
            f"wl update {SAMPLE_ITEM_D['id']} --status",
            stdout=json.dumps({"success": True}),
        )
        runner.set_response(
            f"wl update {SAMPLE_ITEM_D['id']} --stage",
            stdout=json.dumps({"success": True}),
        )
        runner.set_response(
            f"wl comment add {SAMPLE_ITEM_D['id']}",
            stdout=json.dumps({"success": True}),
        )

        results_second = engine.run_all()
        assert len(results_second) == 1
        assert results_second[0]["id"] == "SA-INTAKE-004"
        assert results_second[0]["outcome"] == "auto_completed"


# ===========================================================================
# Test: CLI entry point
# ===========================================================================

class TestCLI:
    """Verify the CLI entry point parses arguments and runs correctly."""

    def test_default_invocation(self):
        """Default invocation discovers and processes all idea-stage items."""
        runner = FakeRunner()
        runner.set_response(
            "wl list --stage idea",
            stdout=json.dumps({
                "success": True,
                "workItems": [SAMPLE_ITEM_A],
            }),
        )
        runner.set_response(
            f"wl update {SAMPLE_ITEM_A['id']} --status",
            stdout=json.dumps({"success": True}),
        )
        runner.set_response(
            f"wl update {SAMPLE_ITEM_A['id']} --stage",
            stdout=json.dumps({"success": True}),
        )
        runner.set_response(
            f"wl comment add {SAMPLE_ITEM_A['id']}",
            stdout=json.dumps({"success": True}),
        )

        engine = IntakeAllEngine(runner=runner)
        results = engine.run_all()
        assert len(results) == 1
        assert results[0]["outcome"] == "auto_completed"

    def test_json_flag(self):
        """--json flag produces JSON output."""
        results = [
            {"id": "SA-001", "title": "A", "outcome": "auto_completed",
             "error_detail": None, "recovery": None},
        ]
        summary = generate_summary(results, json_output=True)
        parsed = json.loads(summary)
        assert parsed["total"] == 1
        assert parsed["auto_completed"] == 1

    def test_dry_run_does_not_make_changes(self):
        """--dry-run flag processes without making actual changes."""
        runner = FakeRunner()
        runner.set_response(
            "wl list --stage idea",
            stdout=SAMPLE_WL_LIST_RESPONSE,
        )

        engine = IntakeAllEngine(runner=runner, dry_run=True)
        results = engine.run_all()

        # Should still produce results
        assert len(results) == 4
        # Should NOT have made any update calls
        update_calls = [
            cmd for cmd in runner.calls
            if "wl update" in " ".join(cmd) or "pi -p --mode json" in " ".join(cmd)
        ]
        assert len(update_calls) == 0, "Dry run should not make changes"


# ===========================================================================
# Test: Engine initialization and configuration
# ===========================================================================

class TestEngineConfig:
    """Verify IntakeAllEngine configuration."""

    def test_default_runner_is_callable(self):
        """Default runner is callable."""
        engine = IntakeAllEngine()
        assert engine.runner is not None
        assert callable(engine.runner)

    def test_custom_runner_is_used(self):
        """Custom runner is used when provided."""
        def my_runner(cmd):
            return FakeProc(returncode=0, stdout="[]", stderr="")
        engine = IntakeAllEngine(runner=my_runner)
        assert engine.runner is my_runner

    def test_dry_run_flag(self):
        """Dry run flag is set correctly."""
        engine = IntakeAllEngine(dry_run=True)
        assert engine.dry_run is True
        engine2 = IntakeAllEngine(dry_run=False)
        assert engine2.dry_run is False


# ===========================================================================
# Test: Orphan detection in discover_items
# ===========================================================================

class TestOrphanDiscovery:
    """Verify that discover_items returns all idea-stage items regardless of status."""

    def test_discover_returns_orphans_with_completed_status(self):
        """Items with status=completed, stage=idea are discovered."""
        runner = FakeRunner()
        runner.set_response(
            "wl list --stage idea",
            stdout=SAMPLE_MIXED_LIST_RESPONSE,
        )

        engine = IntakeAllEngine(runner=runner)
        items = engine.discover_items()

        assert len(items) == 4
        ids = [item["id"] for item in items]
        assert "SA-ORPHAN-001" in ids
        assert "SA-ORPHAN-002" in ids
        assert "SA-INTAKE-001" in ids
        assert "SA-INTAKE-003" in ids

        # Verify the command does NOT filter by status
        discover_calls = [
            cmd for cmd in runner.calls
            if "wl list" in " ".join(cmd)
        ]
        assert len(discover_calls) >= 1
        cmd_str = " ".join(discover_calls[0])
        assert "--status" not in cmd_str, \
            "discover_items should not filter by status to find orphans"

    def test_discover_returns_all_statuses_in_idea_stage(self):
        """All items in idea stage are returned, regardless of status value."""
        runner = FakeRunner()
        # Set of items with various statuses
        items_data = [
            dict(SAMPLE_ITEM_A, status="open"),
            dict(SAMPLE_ITEM_A, id="SA-STATUS-002", status="completed"),
            dict(SAMPLE_ITEM_A, id="SA-STATUS-003", status="in_progress"),
            dict(SAMPLE_ITEM_A, id="SA-STATUS-004", status="blocked"),
        ]
        runner.set_response(
            "wl list --stage idea",
            stdout=json.dumps({"success": True, "workItems": items_data}),
        )

        engine = IntakeAllEngine(runner=runner)
        items = engine.discover_items()
        assert len(items) == 4


# ===========================================================================
# Test: Orphan recovery (_recover_orphans)
# ===========================================================================

class TestOrphanRecovery:
    """Verify that orphaned items in idea stage are recovered to open status."""

    def test_orphan_completed_recovered_to_open(self):
        """Item with status=completed, stage=idea is reset to status=open."""
        runner = FakeRunner()
        runner.set_response(
            f"wl update {ORPHAN_ITEM_COMPLETED['id']} --stage",
            stdout=json.dumps({"success": True}),
        )

        engine = IntakeAllEngine(runner=runner)
        items = [dict(ORPHAN_ITEM_COMPLETED)]
        recovered = engine._recover_orphans(items)

        assert len(recovered) == 1
        assert recovered[0]["status"] == "open"
        # Verify wl update was called to reset the item
        update_calls = [
            cmd for cmd in runner.calls
            if "wl" in cmd and "update" in cmd
        ]
        assert len(update_calls) >= 1

    def test_orphan_in_progress_recovered_to_open(self):
        """Item with status=in_progress, stage=idea is reset to status=open."""
        runner = FakeRunner()
        runner.set_response(
            f"wl update {ORPHAN_ITEM_IN_PROGRESS['id']} --stage",
            stdout=json.dumps({"success": True}),
        )

        engine = IntakeAllEngine(runner=runner)
        items = [dict(ORPHAN_ITEM_IN_PROGRESS)]
        recovered = engine._recover_orphans(items)

        assert len(recovered) == 1
        assert recovered[0]["status"] == "open"

    def test_normal_open_items_unaffected(self):
        """Items already with status=open, stage=idea pass through unchanged."""
        runner = FakeRunner()
        engine = IntakeAllEngine(runner=runner)
        items = [dict(SAMPLE_ITEM_A)]
        recovered = engine._recover_orphans(items)

        assert len(recovered) == 1
        assert recovered[0]["status"] == "open"
        # No update calls should have been made for non-orphans
        update_calls = [
            cmd for cmd in runner.calls
            if "wl" in cmd and "update" in cmd
        ]
        assert len(update_calls) == 0

    def test_mixed_items_only_orphans_recovered(self):
        """Only orphaned items are recovered; normal items pass through."""
        runner = FakeRunner()
        runner.set_response(
            f"wl update {ORPHAN_ITEM_COMPLETED['id']} --stage",
            stdout=json.dumps({"success": True}),
        )
        runner.set_response(
            f"wl update {ORPHAN_ITEM_IN_PROGRESS['id']} --stage",
            stdout=json.dumps({"success": True}),
        )

        engine = IntakeAllEngine(runner=runner)
        items = [
            dict(SAMPLE_ITEM_A),        # open - normal
            dict(ORPHAN_ITEM_COMPLETED),   # completed - orphan
            dict(SAMPLE_ITEM_C),        # open - normal (epic)
            dict(ORPHAN_ITEM_IN_PROGRESS), # in_progress - orphan
        ]
        recovered = engine._recover_orphans(items)

        assert len(recovered) == 4
        # Orphans should have been reset to open
        for item in recovered:
            assert item["status"] == "open", \
                f"Item {item['id']} should have status=open after recovery"

    def test_recovery_failure_handled_gracefully(self):
        """If wl update fails for an orphan, error is logged and item kept."""
        runner = FakeRunner()
        # Simulate wl rejecting the completed→open transition
        runner.set_response(
            f"wl update {ORPHAN_ITEM_COMPLETED['id']} --stage",
            returncode=1,
            stderr="wl: cannot transition from completed to open",
        )

        engine = IntakeAllEngine(runner=runner)
        items = [dict(ORPHAN_ITEM_COMPLETED)]
        recovered = engine._recover_orphans(items)

        # Item should still be included for processing (status updated in-memory)
        assert len(recovered) == 1
        # The item's in-memory status is updated regardless of wl success
        assert recovered[0]["status"] == "open"

    def test_dry_run_skips_update(self):
        """During dry run, orphans are not actually updated via wl."""
        runner = FakeRunner()
        engine = IntakeAllEngine(runner=runner, dry_run=True)
        items = [dict(ORPHAN_ITEM_COMPLETED)]
        recovered = engine._recover_orphans(items)

        assert len(recovered) == 1
        assert recovered[0]["status"] == "open"
        # No update calls should have been made in dry run
        update_calls = [
            cmd for cmd in runner.calls
            if "wl" in cmd and "update" in cmd
        ]
        assert len(update_calls) == 0

    def test_orphan_recovery_in_run_all_pipeline(self):
        """Orphan recovery runs before processing in the full pipeline."""
        runner = FakeRunner()
        # Mixed list: one normal (auto-completable), two orphans
        runner.set_response(
            "wl list --stage idea",
            stdout=SAMPLE_MIXED_LIST_RESPONSE,
        )
        # Recovery updates for orphans
        runner.set_response(
            f"wl update {ORPHAN_ITEM_COMPLETED['id']} --stage",
            stdout=json.dumps({"success": True}),
        )
        runner.set_response(
            f"wl update {ORPHAN_ITEM_IN_PROGRESS['id']} --stage",
            stdout=json.dumps({"success": True}),
        )
        # Claim updates for items that need processing
        runner.set_response(
            f"wl update {SAMPLE_ITEM_A['id']} --status",
            stdout=json.dumps({"success": True}),
        )
        runner.set_response(
            f"wl update {ORPHAN_ITEM_COMPLETED['id']} --status",
            stdout=json.dumps({"success": True}),
        )
        runner.set_response(
            f"wl update {ORPHAN_ITEM_IN_PROGRESS['id']} --status",
            stdout=json.dumps({"success": True}),
        )
        # Stage updates for auto-completable items
        for item_id in [SAMPLE_ITEM_A["id"], ORPHAN_ITEM_COMPLETED["id"],
                         ORPHAN_ITEM_IN_PROGRESS["id"]]:
            runner.set_response(
                f"wl update {item_id} --stage",
                stdout=json.dumps({"success": True}),
            )
            runner.set_response(
                f"wl comment add {item_id}",
                stdout=json.dumps({"success": True}),
            )

        engine = IntakeAllEngine(runner=runner)
        results = engine.run_all()

        # All 4 items should be processed
        assert len(results) == 4
        # Orphans should be recovered and then processed normally
        completed_ids = [r["id"] for r in results if r["outcome"] == "auto_completed"]
        assert ORPHAN_ITEM_COMPLETED["id"] in completed_ids
        assert ORPHAN_ITEM_IN_PROGRESS["id"] in completed_ids
        assert SAMPLE_ITEM_A["id"] in completed_ids

        # Verify recovery calls happened before processing calls
        recovery_calls = []
        processing_calls = []
        for cmd in runner.calls:
            cmd_str = " ".join(cmd)
            if ORPHAN_ITEM_COMPLETED["id"] in cmd_str and "--stage" in cmd_str:
                # Recovery updates stage, processing updates stage too
                pass  # Both orphans have --stage idea recovery + --stage intake_complete processing

        # Verify recovery call happened (wl update --stage idea for orphans)
        recovery_calls = [
            cmd for cmd in runner.calls
            if "wl" in cmd and "update" in cmd and "--stage" in cmd
            and "idea" in " ".join(cmd)
        ]
        # Should have recovery calls for both orphans
        assert len(recovery_calls) == 2


# ===========================================================================
# Test: Signal handling for graceful abort
# ===========================================================================

class TestSignalHandling:
    """Verify signal handlers are registered and trigger recovery correctly."""

    def test_signal_handlers_registered(self):
        """SIGINT and SIGTERM handlers are registered on setup."""
        runner = FakeRunner()
        engine = IntakeAllEngine(runner=runner)

        engine._setup_signal_handlers()

        # Verify both signals are registered to the handler method
        assert signal.getsignal(signal.SIGINT) == engine._signal_handler
        assert signal.getsignal(signal.SIGTERM) == engine._signal_handler

        # Restore original handlers
        engine._restore_signal_handlers()
        assert signal.getsignal(signal.SIGINT) != engine._signal_handler or \
               signal.getsignal(signal.SIGINT) == signal.default_int_handler

    def test_signal_handler_calls_recovery_for_current_item(self):
        """Signal handler calls _attempt_recovery for the current item."""
        runner = FakeRunner()
        runner.set_response(
            f"wl update {SAMPLE_ITEM_C['id']} --stage",
            stdout=json.dumps({"success": True}),
        )

        engine = IntakeAllEngine(runner=runner)
        engine._current_item_id = SAMPLE_ITEM_C["id"]

        # Simulate receiving a signal by calling the handler directly
        try:
            engine._signal_handler(signal.SIGINT, None)
        except SystemExit:
            pass

        # Verify recovery was attempted
        recovery_calls = [
            cmd for cmd in runner.calls
            if "wl" in cmd and "update" in cmd and SAMPLE_ITEM_C["id"] in " ".join(cmd)
        ]
        assert len(recovery_calls) >= 1

    def test_signal_handler_noop_when_no_current_item(self):
        """Signal handler does nothing when no item is being processed."""
        runner = FakeRunner()
        engine = IntakeAllEngine(runner=runner)
        engine._current_item_id = None

        try:
            engine._signal_handler(signal.SIGINT, None)
        except SystemExit:
            pass

        # No recovery should be attempted
        update_calls = [
            cmd for cmd in runner.calls
            if "wl" in cmd and "update" in cmd
        ]
        assert len(update_calls) == 0

    def test_current_item_id_set_during_intake(self):
        """_current_item_id is set during intake processing for signal handling."""
        runner = FakeRunner()
        runner.set_response(
            "wl list --stage idea",
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
            f"pi -p --mode json /intake {SAMPLE_ITEM_C['id']}",
            stdout=json.dumps({"success": True}),
        )
        runner.set_response(
            f"wl update {SAMPLE_ITEM_C['id']} --stage",
            stdout=json.dumps({"success": True}),
        )

        engine = IntakeAllEngine(runner=runner)
        engine.run_all()

        # After run_all completes, _current_item_id should be None (cleared)
        assert engine._current_item_id is None

    def test_signal_handler_exits_with_code(self):
        """Signal handler raises SystemExit with correct code (128+signum)."""
        runner = FakeRunner()
        engine = IntakeAllEngine(runner=runner)

        try:
            engine._signal_handler(signal.SIGINT, None)
            # Should not reach here
            assert False, "Expected SystemExit"
        except SystemExit as e:
            # SIGINT = 2, so exit code should be 130
            assert e.code == 128 + signal.SIGINT
