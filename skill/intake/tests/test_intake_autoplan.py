"""Tests for the intake closing autoplan pre-check.

Covers ``intake.py autoplan`` (the closing step that runs after the
effort/risk estimate and before the finishing step) against the acceptance
criteria of SA-0MWXJWZ3T0062HBI:

  AC2 — decision ``skip`` advances the stage to ``plan_complete`` with
        ``status=open`` and the assignee released.
  AC3 — decision ``plan`` / ``error`` / unknown estimates leave the stage at
        ``intake_complete`` (fail-safe, never skip silently).
  AC4 — idempotence: an item already at ``plan_complete`` (or later) is never
        regressed and the decision comment is not duplicated.
  AC5 — single-sourced thresholds: intake consumes the canonical
        ``plan.plan_helpers.plan_if_needed`` rather than reimplementing it.

All tests use ``replace``-style mocks on the shared ``wl`` subprocess call and
on the canonical helper, so no live worklog store is required.
"""

from __future__ import annotations

import json
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
_SKILLS_ROOT = REPO_ROOT / "skill"
if str(_SKILLS_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILLS_ROOT))

from intake.scripts import intake

ITEM_ID = "TEST-123"

CANONICAL_PLAN_HELPERS = REPO_ROOT / "skill" / "plan" / "plan_helpers.py"

INTAKE_MD = REPO_ROOT / "skill" / "intake" / "SKILL.md"
INTAKE_REF = REPO_ROOT / "docs" / "dev" / "intake-skill-reference.md"


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _proc(cmd, payload=None, returncode=0, stderr=""):
    """Build a CompletedProcess carrying a JSON payload."""
    return subprocess.CompletedProcess(
        cmd, returncode, json.dumps(payload if payload is not None else {"success": True}), stderr
    )


class FakeWl:
    """Minimal stateful ``wl`` stand-in (show / comment list / comment add / update)."""

    def __init__(self, stage: str = "intake_complete", comments: list[dict] | None = None):
        self.stage = stage
        self.comments = list(comments or [])
        self.commands: list[list[str]] = []
        self.show_fails = False

    def __call__(self, cmd, *args, **kwargs):
        cmd = list(cmd)
        self.commands.append(cmd)
        if "show" in cmd:
            if self.show_fails:
                return _proc(cmd, {"success": False, "error": "wl show failed"}, returncode=1)
            return _proc(
                cmd,
                {
                    "success": True,
                    "workItem": {"id": ITEM_ID, "status": "open", "stage": self.stage},
                },
            )
        if "comment" in cmd and "add" in cmd:
            body = cmd[cmd.index("--comment") + 1]
            self.comments.append({"comment": body})
            return _proc(cmd, {"success": True})
        if "comment" in cmd and "list" in cmd:
            return _proc(cmd, {"success": True, "comments": list(self.comments)})
        if "update" in cmd:
            if "--stage" in cmd:
                self.stage = cmd[cmd.index("--stage") + 1]
            return _proc(cmd, {"success": True})
        return _proc(cmd, {"success": True})

    # -- assertions helpers ------------------------------------------------
    def updates(self) -> list[list[str]]:
        return [c for c in self.commands if "update" in c]

    def comment_adds(self) -> list[list[str]]:
        return [c for c in self.commands if "comment" in c and "add" in c]


@contextmanager
def fake_wl(fake: FakeWl):
    """Patch the shared wl subprocess path and keep worklog resolution hermetic."""
    with (
        mock.patch("shared.status_lifecycle.subprocess.run", side_effect=fake),
        mock.patch("shared.status_lifecycle.worklog_dir_flag", return_value=[]),
        mock.patch(
            "shared.status_lifecycle._find_worklog_dir_by_prefix", return_value=None
        ),
    ):
        yield fake


@pytest.fixture
def decision():
    """Patch the canonical helper; returns the patch object for assertions."""
    with mock.patch.object(intake, "plan_if_needed") as patched:
        yield patched


# ---------------------------------------------------------------------------
# AC2 — skip advances to plan_complete
# ---------------------------------------------------------------------------


class TestSkipAdvancesStage:
    def test_skip_advances_to_plan_complete_with_open_status(self, decision):
        fake = FakeWl(stage="intake_complete")
        decision.return_value = {"decision": "skip", "effort": "Small", "risk": "Low"}

        with fake_wl(fake):
            result = intake.cmd_autoplan(ITEM_ID)

        assert result["success"] is True
        assert result["action"] == "advanced"
        assert result["decision"] == "skip"
        assert result["stage"] == "plan_complete"

        updates = fake.updates()
        assert len(updates) == 1, f"expected exactly one stage write, got {updates}"
        update = updates[0]
        assert "--status" in update and update[update.index("--status") + 1] == "open"
        assert "--stage" in update and update[update.index("--stage") + 1] == "plan_complete"

    def test_skip_releases_the_assignee(self, decision):
        fake = FakeWl(stage="intake_complete")
        decision.return_value = {"decision": "skip", "effort": "Extra Small", "risk": "Low"}

        with fake_wl(fake):
            intake.cmd_autoplan(ITEM_ID)

        update = fake.updates()[0]
        assert "--assignee" in update
        assert update[update.index("--assignee") + 1] == ""

    def test_skip_records_the_decision_as_a_comment(self, decision):
        fake = FakeWl(stage="intake_complete")
        decision.return_value = {"decision": "skip", "effort": "Small", "risk": "Low"}

        with fake_wl(fake):
            intake.cmd_autoplan(ITEM_ID)

        adds = fake.comment_adds()
        assert len(adds) == 1
        body = adds[0][adds[0].index("--comment") + 1]
        assert "Decision: skip" in body
        assert "plan_complete" in body
        assert "Autoplan Pre-check" in body

    def test_skip_from_idea_stage_still_advances(self, decision):
        """A stage before intake_complete is not a reason to withhold the skip."""
        fake = FakeWl(stage="idea")
        decision.return_value = {"decision": "skip", "effort": "Small", "risk": "Low"}

        with fake_wl(fake):
            result = intake.cmd_autoplan(ITEM_ID)

        assert result["action"] == "advanced"
        assert result["stage"] == "plan_complete"


# ---------------------------------------------------------------------------
# AC3 — plan / unknown / error fail safe
# ---------------------------------------------------------------------------


class TestFailSafePaths:
    def test_plan_decision_leaves_stage_at_intake_complete(self, decision):
        fake = FakeWl(stage="intake_complete")
        decision.return_value = {"decision": "plan", "effort": "Small", "risk": "Medium"}

        with fake_wl(fake):
            result = intake.cmd_autoplan(ITEM_ID)

        assert result["action"] == "unchanged"
        assert result["decision"] == "plan"
        assert result["stage"] == "intake_complete"
        assert fake.updates() == []

    def test_unknown_estimates_leave_stage_at_intake_complete(self, decision):
        fake = FakeWl(stage="intake_complete")
        decision.return_value = {"decision": "plan", "effort": "", "risk": ""}

        with fake_wl(fake):
            result = intake.cmd_autoplan(ITEM_ID)

        assert result["action"] == "unchanged"
        assert result["stage"] == "intake_complete"
        assert result["effort"] == "" and result["risk"] == ""
        assert fake.updates() == []

    def test_error_decision_leaves_stage_at_intake_complete(self, decision):
        fake = FakeWl(stage="intake_complete")
        decision.return_value = {
            "decision": "error",
            "effort": "",
            "risk": "",
            "error": "could not fetch work item",
        }

        with fake_wl(fake):
            result = intake.cmd_autoplan(ITEM_ID)

        assert result["action"] == "unchanged"
        assert result["decision"] == "error"
        assert result["stage"] == "intake_complete"
        assert fake.updates() == []

    def test_helper_exception_is_treated_as_error(self, decision):
        fake = FakeWl(stage="intake_complete")
        decision.side_effect = RuntimeError("boom")

        with fake_wl(fake):
            result = intake.cmd_autoplan(ITEM_ID)

        assert result["decision"] == "error"
        assert result["stage"] == "intake_complete"
        assert fake.updates() == []

    def test_missing_canonical_helper_degrades_to_error(self):
        """An install without the plan helper must never skip planning."""
        fake = FakeWl(stage="intake_complete")

        with fake_wl(fake), mock.patch.object(intake, "plan_if_needed", None):
            result = intake.cmd_autoplan(ITEM_ID)

        assert result["decision"] == "error"
        assert result["stage"] == "intake_complete"
        assert fake.updates() == []

    def test_fetch_failure_leaves_the_item_untouched(self, decision):
        fake = FakeWl(stage="intake_complete")
        fake.show_fails = True

        with fake_wl(fake):
            result = intake.cmd_autoplan(ITEM_ID)

        assert result["decision"] == "error"
        assert result["action"] == "unchanged"
        assert fake.updates() == []
        decision.assert_not_called()

    def test_unrecognised_stage_is_never_mutated(self, decision):
        fake = FakeWl(stage="mystery_stage")

        with fake_wl(fake):
            result = intake.cmd_autoplan(ITEM_ID)

        assert result["action"] == "no_op"
        assert result["stage"] == "mystery_stage"
        assert fake.updates() == []
        decision.assert_not_called()


# ---------------------------------------------------------------------------
# AC4 — idempotence / no regression
# ---------------------------------------------------------------------------


class TestIdempotence:
    @pytest.mark.parametrize("stage", ["plan_complete", "in_review", "done"])
    def test_stage_at_or_past_plan_complete_is_a_no_op(self, decision, stage):
        fake = FakeWl(stage=stage)

        with fake_wl(fake):
            result = intake.cmd_autoplan(ITEM_ID)

        assert result["action"] == "no_op"
        assert result["stage"] == stage
        assert fake.updates() == []
        assert fake.comment_adds() == []
        decision.assert_not_called()

    def test_rerun_after_skip_does_not_regress_or_duplicate(self, decision):
        fake = FakeWl(stage="intake_complete")
        decision.return_value = {"decision": "skip", "effort": "Small", "risk": "Low"}

        with fake_wl(fake):
            first = intake.cmd_autoplan(ITEM_ID)
            second = intake.cmd_autoplan(ITEM_ID)

        assert first["action"] == "advanced"
        assert second["action"] == "no_op"
        assert len(fake.updates()) == 1
        assert len(fake.comment_adds()) == 1
        assert fake.stage == "plan_complete"

    def test_rerun_on_plan_path_does_not_duplicate_the_comment(self, decision):
        fake = FakeWl(stage="intake_complete")
        decision.return_value = {"decision": "plan", "effort": "Small", "risk": "Medium"}

        with fake_wl(fake):
            intake.cmd_autoplan(ITEM_ID)
            intake.cmd_autoplan(ITEM_ID)

        assert len(fake.comment_adds()) == 1
        assert fake.updates() == []

    def test_comment_failure_does_not_lose_the_stage_advance(self, decision):
        fake = FakeWl(stage="intake_complete")
        decision.return_value = {"decision": "skip", "effort": "Small", "risk": "Low"}

        def failing_comments(cmd, *args, **kwargs):
            if "comment" in cmd and "add" in cmd:
                return _proc(cmd, {"success": False, "error": "comment boom"}, returncode=1)
            return fake(cmd)

        with (
            mock.patch("shared.status_lifecycle.subprocess.run", side_effect=failing_comments),
            mock.patch("shared.status_lifecycle.worklog_dir_flag", return_value=[]),
            mock.patch(
                "shared.status_lifecycle._find_worklog_dir_by_prefix", return_value=None
            ),
        ):
            result = intake.cmd_autoplan(ITEM_ID)

        assert result["action"] == "advanced"
        assert len(fake.updates()) == 1


# ---------------------------------------------------------------------------
# AC5 — single-sourced threshold logic
# ---------------------------------------------------------------------------


class TestSingleSourcedHelper:
    def test_intake_uses_the_canonical_plan_helper(self):
        """No duplicated threshold logic: the canonical module is loaded."""
        assert intake.plan_if_needed is not None
        assert intake.plan_if_needed is intake._PLAN_HELPERS.plan_if_needed
        assert intake._PLAN_HELPERS is sys.modules[intake._PLAN_HELPER_MODULE_NAME]

    def test_canonical_helper_is_the_plan_skill_module(self):
        """The loaded module is the plan skill's own file, not a copy."""
        import inspect

        source = Path(inspect.getsourcefile(intake.plan_if_needed)).resolve()
        assert source == CANONICAL_PLAN_HELPERS.resolve()

    def test_skip_thresholds_come_from_the_canonical_helper(self):
        """The threshold constants are the plan skill's (never re-declared)."""
        assert intake._PLAN_HELPERS.DEFAULT_AUTOPLAN_EFFORT_SKIP == frozenset(
            {"Extra Small", "Small"}
        )
        assert intake._PLAN_HELPERS.DEFAULT_AUTOPLAN_RISK_SKIP == frozenset({"Low"})

    def test_intake_does_not_redefine_thresholds(self):
        source = (REPO_ROOT / "skill" / "intake" / "scripts" / "intake.py").read_text()
        # No local threshold constants: the sets come from the canonical helper.
        assert "DEFAULT_AUTOPLAN_EFFORT_SKIP:" not in source
        assert "DEFAULT_AUTOPLAN_RISK_SKIP:" not in source
        assert "frozenset({" not in source


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


class TestCliWiring:
    def test_autoplan_subcommand_is_registered(self):
        args = intake.build_parser().parse_args(["autoplan", ITEM_ID])
        assert args.command == "autoplan"
        assert args.item_id == ITEM_ID

    def test_main_dispatches_autoplan(self, decision):
        fake = FakeWl(stage="intake_complete")
        decision.return_value = {"decision": "skip", "effort": "Small", "risk": "Low"}

        with fake_wl(fake):
            assert intake.main(["autoplan", ITEM_ID]) == 0

        assert fake.stage == "plan_complete"


# ---------------------------------------------------------------------------
# AC7 — documentation
# ---------------------------------------------------------------------------


class TestDocumentation:
    def test_skill_md_documents_the_closing_precheck_step(self):
        content = INTAKE_MD.read_text()
        assert "autoplan" in content.lower()
        assert "plan_complete" in content

    def test_reference_documents_fail_safe_and_idempotence(self):
        content = INTAKE_REF.read_text().lower()
        assert "autoplan" in content
        assert "fail-safe" in content
        assert "idempot" in content
