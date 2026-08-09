"""Tests for status preservation in effort-and-risk run_skill.py.

run_skill.py must NOT flip the work item to ``status=completed``. Per the
documented lifecycle (AGENTS.md), ``status=completed`` means formally closed
post-release; an item at ``intake_complete``/``plan_complete`` stays ``open``
while being estimated.

The script captures the pre-run status and restores it deterministically
(see SA-0MS93J0ZC007IO8V). StatusLifecycle itself is tested separately in
``skill/shared/test_status_lifecycle.py``; these tests focus on the
integration point in ``run_skill.py`` and ``SKILL.md``.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "skill" / "effort-and-risk" / "scripts"

# Ensure the skill package is importable when importing run_skill.py
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _load_run_skill():
    """Import run_skill.py as a module (its directory name has a hyphen)."""
    spec = importlib.util.spec_from_file_location(
        "run_skill_under_test", SCRIPTS_DIR / "run_skill.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# ===========================================================================
# Tests: run_skill.py source structure
# ===========================================================================


class TestRunSkillSourceStructure:
    """Structural tests for run_skill.py - no mocking needed."""

    SOURCE = (SCRIPTS_DIR / "run_skill.py").read_text()

    def test_imports_status_lifecycle(self):
        """run_skill.py imports StatusLifecycle from skill.shared."""
        assert "StatusLifecycle" in self.SOURCE, (
            "run_skill.py should import StatusLifecycle from skill.shared"
        )

    def test_imports_from_skill_shared(self):
        """run_skill.py imports from skill.shared.status_lifecycle module."""
        has_import = any([
            "from skill.shared.status_lifecycle import" in self.SOURCE,
            "from skill.shared import status_lifecycle" in self.SOURCE,
            "import skill.shared.status_lifecycle" in self.SOURCE,
        ])
        assert has_import, (
            "run_skill.py should import from skill.shared.status_lifecycle"
        )

    def test_does_not_use_context_manager(self):
        """run_skill.py must NOT wrap its flow in `with StatusLifecycle(...)`.

        The context manager's success path sets ``status=completed``, which
        violates the documented stage/status rules for intake/planning items.
        """
        assert "with StatusLifecycle(" not in self.SOURCE, (
            "run_skill.py must not use the StatusLifecycle context manager "
            "(its success exit flips status to completed)"
        )

    def test_captures_original_status(self):
        """run_skill.py captures the pre-run status for deterministic restore."""
        assert "original_status" in self.SOURCE, (
            "run_skill.py should capture the pre-run status (original_status)"
        )

    def test_restores_original_status(self):
        """run_skill.py restores the original status via the shared helper."""
        assert "StatusLifecycle.update_status" in self.SOURCE, (
            "run_skill.py should restore the pre-run status via "
            "StatusLifecycle.update_status"
        )

    def test_no_raw_wl_subprocess_in_source(self):
        """run_skill.py routes wl commands through the shared run_wl helper.

        Raw ``subprocess.run`` must only be used for the orchestrator
        subprocess (python3), never for ``wl``.
        """
        lines = self.SOURCE.split("\n")
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if "subprocess.run" in stripped and "wl" in stripped:
                raise AssertionError(
                    f"Line {i}: subprocess.run with 'wl' should be routed "
                    f"through the shared run_wl helper: {stripped.strip()}"
                )

    def test_wl_show_uses_shared_runner(self):
        """run_skill.py uses the shared run_wl helper for wl show."""
        assert "run_wl" in self.SOURCE, (
            "run_skill.py should use the shared run_wl helper for wl commands"
        )


# ===========================================================================
# Tests: SKILL.md documentation hygiene
# ===========================================================================


class TestSkillDocHygiene:
    """Tests that SKILL.md documents status preservation, not completion."""

    SKILL_MD = (REPO_ROOT / "skill" / "effort-and-risk" / "SKILL.md").read_text()

    def test_no_ad_hoc_status_commands(self):
        """SKILL.md should not document ad-hoc 'wl update --status' commands."""
        assert "wl update --status in_progress" not in self.SKILL_MD, (
            "SKILL.md should not document ad-hoc 'wl update --status in_progress'. "
            "Status transitions go through StatusLifecycle helpers."
        )
        assert "wl update --status open" not in self.SKILL_MD, (
            "SKILL.md should not document ad-hoc 'wl update --status open'. "
            "Status transitions go through StatusLifecycle helpers."
        )

    def test_has_status_lifecycle_reference(self):
        """SKILL.md should reference StatusLifecycle for status management."""
        assert "StatusLifecycle" in self.SKILL_MD, (
            "SKILL.md should reference StatusLifecycle for status management."
        )

    def test_documents_status_is_not_changed(self):
        """SKILL.md should state that status is preserved, not set to completed."""
        assert "does not" in self.SKILL_MD or "NOT" in self.SKILL_MD, (
            "SKILL.md should document that status is not modified by run_skill.py."
        )
        assert "completed" not in self.SKILL_MD.lower().split("status is managed")[-1][:400], (
            "SKILL.md should not claim the status transitions to completed."
        )


# ===========================================================================
# Tests: run_skill.py behavior (status preservation)
# ===========================================================================


class TestRunSkillStatusBehavior:
    """Behavioral test: effort-and-risk leaves status=open at intake_complete."""

    ISSUE = "TEST-123"

    @staticmethod
    def _fake_run(cmd, *args, **kwargs):
        """Dispatch on the command: wl (show/update) or the orchestrator."""
        if cmd and cmd[0] == "wl":
            if "show" in cmd:
                payload = {
                    "success": True,
                    "workItem": {
                        "id": TestRunSkillStatusBehavior.ISSUE,
                        "status": "open",
                        "stage": "intake_complete",
                    },
                    "children": [],
                }
                return subprocess.CompletedProcess(
                    cmd, 0, json.dumps(payload), ""
                )
            if "update" in cmd:
                return subprocess.CompletedProcess(
                    cmd, 0, json.dumps({"success": True}), ""
                )
            return subprocess.CompletedProcess(cmd, 0, json.dumps({"success": True}), "")
        if cmd and cmd[0] == "python3":
            # The orchestrator subprocess
            return subprocess.CompletedProcess(
                cmd,
                0,
                json.dumps({"ok": True, "comment_result": {"success": True}}),
                "",
            )
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def test_status_preserved_open_after_run(self):
        """After a successful run, the item's status is restored to open.

        No wl call may set status=completed.
        """
        run_skill = _load_run_skill()
        fake_stdin = mock.MagicMock()
        fake_stdin.isatty.return_value = True

        with (
            mock.patch("subprocess.run", side_effect=self._fake_run) as m,
            mock.patch.object(sys, "stdin", fake_stdin),
            mock.patch.object(sys, "argv", ["run_skill.py", "--issue", self.ISSUE]),
        ):
            run_skill.main()

        wl_calls = [
            c.args[0]
            for c in m.call_args_list
            if c.args and c.args[0] and c.args[0][0] == "wl"
        ]
        assert wl_calls, "expected at least one wl command"

        # The only wl update is the restore to the original status (open).
        # (Commands may carry a leading --worklog-dir when run from inside a
        # git worktree, so match the "update" token position-independently.)
        updates = [c for c in wl_calls if "update" in c]
        assert len(updates) == 1, f"expected exactly one wl update, got {updates}"
        assert "--status" in updates[0]
        assert updates[0][updates[0].index("--status") + 1] == "open"

        # No wl call may request status=completed
        assert "completed" not in [tok for c in wl_calls for tok in c], (
            "run_skill.py must never set status=completed"
        )

    def test_status_restored_on_failure(self):
        """Even when the orchestrator fails, the original status is restored."""
        run_skill = _load_run_skill()
        fake_stdin = mock.MagicMock()
        fake_stdin.isatty.return_value = True

        def fake_run(cmd, *args, **kwargs):
            if cmd and cmd[0] == "wl":
                if "show" in cmd:
                    payload = {
                        "success": True,
                        "workItem": {
                            "id": TestRunSkillStatusBehavior.ISSUE,
                            "status": "plan_complete_open",
                            "stage": "plan_complete",
                        },
                        "children": [],
                    }
                    return subprocess.CompletedProcess(cmd, 0, json.dumps(payload), "")
                if "update" in cmd:
                    return subprocess.CompletedProcess(
                        cmd, 0, json.dumps({"success": True}), ""
                    )
            if cmd and cmd[0] == "python3":
                # Orchestrator fails
                return subprocess.CompletedProcess(cmd, 3, "", "boom")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with (
            mock.patch("subprocess.run", side_effect=fake_run) as m,
            mock.patch.object(sys, "stdin", fake_stdin),
            mock.patch.object(sys, "argv", ["run_skill.py", "--issue", self.ISSUE]),
            pytest.raises(SystemExit) as exc_info,
        ):
            run_skill.main()

        assert exc_info.value.code == 3

        wl_calls = [
            c.args[0]
            for c in m.call_args_list
            if c.args and c.args[0] and c.args[0][0] == "wl"
        ]
        updates = [c for c in wl_calls if "update" in c]
        assert updates, "expected a restore update even on failure"
        assert updates[0][updates[0].index("--status") + 1] == "plan_complete_open"
        assert "completed" not in [tok for c in wl_calls for tok in c]


if __name__ == "__main__":
    pytest.main(sys.argv)
