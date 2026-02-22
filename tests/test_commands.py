"""Tests for ampa.engine.commands — stage-to-action mapping and command building.

Covers:
- Shell command building for each action (intake, plan, implement)
- Stage-to-action mapping
"""

from __future__ import annotations

from ampa.engine.commands import (
    build_dispatch_command,
    stage_to_action,
)


# ---------------------------------------------------------------------------
# Shell command builder tests
# ---------------------------------------------------------------------------


class TestStageToAction:
    def test_idea(self):
        assert stage_to_action("idea") == "intake"

    def test_intake_complete(self):
        assert stage_to_action("intake_complete") == "plan"

    def test_plan_complete(self):
        assert stage_to_action("plan_complete") == "implement"

    def test_unknown_stage(self):
        assert stage_to_action("in_review") is None

    def test_empty_stage(self):
        assert stage_to_action("") is None


class TestBuildDispatchCommand:
    def test_intake_command(self):
        cmd = build_dispatch_command("WL-42", "intake")
        assert cmd == 'opencode run "/intake WL-42 do not ask questions"'

    def test_plan_command(self):
        cmd = build_dispatch_command("WL-42", "plan")
        assert cmd == 'opencode run "/plan WL-42"'

    def test_implement_command(self):
        cmd = build_dispatch_command("WL-42", "implement")
        assert cmd == 'opencode run "work on WL-42 using the implement skill"'

    def test_unknown_action(self):
        cmd = build_dispatch_command("WL-42", "unknown")
        assert cmd is None

    def test_special_characters_in_id(self):
        cmd = build_dispatch_command("SA-0MLX8FNGJ0IYN1LN", "implement")
        assert "SA-0MLX8FNGJ0IYN1LN" in cmd
        assert (
            cmd
            == 'opencode run "work on SA-0MLX8FNGJ0IYN1LN using the implement skill"'
        )
