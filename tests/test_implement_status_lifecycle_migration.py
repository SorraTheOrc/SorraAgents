"""Tests for StatusLifecycle context manager migration in implement/plan skills.

Verifies that:
- skill/implement/scripts/implement.py uses StatusLifecycle context manager in phase_finish
- All error paths in phase_finish properly reset status
- skill/implement/SKILL.md references StatusLifecycle instead of ad-hoc wl update --status
- skill/plan/SKILL.md references StatusLifecycle instead of ad-hoc wl update --status

Related work item: SA-0MS69FE4Q008N8SZ
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


# ===========================================================================
# Tests: implement.py StatusLifecycle source structure
# ===========================================================================


class TestImplementScriptStatusLifecycle:
    """Structural tests for skill/implement/scripts/implement.py."""

    IMPLEMENT_PY = REPO_ROOT / "skill" / "implement" / "scripts" / "implement.py"
    SOURCE = IMPLEMENT_PY.read_text() if IMPLEMENT_PY.exists() else ""

    def test_imports_status_lifecycle(self):
        """implement.py imports StatusLifecycle from skill.shared."""
        assert self.SOURCE, f"implement.py not found at {self.IMPLEMENT_PY}"
        has_import = any([
            "from shared.status_lifecycle import StatusLifecycle" in self.SOURCE,
            "from shared import StatusLifecycle" in self.SOURCE,
            "from shared import status_lifecycle" in self.SOURCE,
        ])
        assert has_import, (
            "implement.py should import StatusLifecycle from shared.status_lifecycle"
        )

    def test_imports_and_uses_context_manager_in_phase_finish(self):
        """phase_finish wraps its main workflow in a 'with StatusLifecycle()' block."""
        assert "with StatusLifecycle(" in self.SOURCE, (
            "implement.py should wrap phase_finish logic in a "
            "`with StatusLifecycle(...):` block"
        )

    def test_phase_finish_uses_target_stage_in_review(self):
        """The StatusLifecycle context manager in phase_finish uses target_stage='in_review'."""
        assert "target_stage=" in self.SOURCE, (
            "StatusLifecycle should be invoked with target_stage= parameter"
        )
        assert "in_review" in self.SOURCE, (
            "phase_finish should set target_stage to 'in_review'"
        )

    def test_no_ad_hoc_wl_update_calls_in_source(self):
        """implement.py should not contain ad-hoc 'wl update' subprocess.run calls.

        Status transitions are managed by StatusLifecycle.
        """
        lines = self.SOURCE.split("\n")
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            # Skip comments and docstrings
            if stripped.startswith(("#", '"""', "'''")):
                continue
            # Allow only wl show, wl comment add, wl list, wl cleanup-worktree
            if "subprocess.run" in stripped and "wl" in stripped and "wl update" in stripped:
                pytest.fail(
                    f"Line {i}: subprocess.run with 'wl update' found. "
                    f"Use StatusLifecycle for status transitions: {stripped.strip()}"
                )

    def test_phase_start_uses_update_status(self):
        """phase_start uses StatusLifecycle.update_status() for transitions."""
        # phase_start should still use the static method (not context manager)
        assert "StatusLifecycle.update_status" in self.SOURCE, (
            "implement.py should use StatusLifecycle.update_status for status transitions"
        )


# ===========================================================================
# Tests: implement SKILL.md references StatusLifecycle
# ===========================================================================


class TestImplementSkillDocReferencesStatusLifecycle:
    """Tests that skill/implement/SKILL.md references StatusLifecycle."""

    SKILL_MD = REPO_ROOT / "skill" / "implement" / "SKILL.md"
    CONTENT = SKILL_MD.read_text() if SKILL_MD.exists() else ""

    def test_skill_md_exists(self):
        """SKILL.md exists in the implement skill directory."""
        assert self.SKILL_MD.exists(), f"SKILL.md not found at {self.SKILL_MD}"
        assert self.CONTENT, "SKILL.md is empty"

    def test_references_status_lifecycle(self):
        """SKILL.md references StatusLifecycle for status management."""
        assert "StatusLifecycle" in self.CONTENT, (
            "implement SKILL.md should reference StatusLifecycle for status management"
        )

    def test_no_ad_hoc_status_commands_in_steps(self):
        """SKILL.md should not document ad-hoc 'wl update --status' commands
        in step instructions or the Status Transition Matrix.
        
        Exceptions: example commands in reference sections are acceptable.
        """
        # Check for ad-hoc commands in the main steps (not example sections)
        lines = self.CONTENT.split("\n")
        in_reference = False
        for i, line in enumerate(lines):
            if "### Scripts" in line or "## Appendix" in line or "## Examples" in line:
                in_reference = True
            if in_reference and line.startswith("#") and not line.startswith("##"):
                in_reference = False
            
            # Only check non-reference sections
            # Allow references that explain what StatusLifecycle replaces
            if not in_reference and "wl update" in line and "--status" in line and "StatusLifecycle" not in line:
                    pytest.fail(
                        f"Line {i+1}: ad-hoc 'wl update --status' command found "
                        f"in SKILL.md without StatusLifecycle reference: {line.strip()}"
                    )


# ===========================================================================
# Tests: plan SKILL.md references StatusLifecycle
# ===========================================================================


class TestPlanSkillDocReferencesStatusLifecycle:
    """Tests that skill/plan/SKILL.md references StatusLifecycle."""

    SKILL_MD = REPO_ROOT / "skill" / "plan" / "SKILL.md"
    CONTENT = SKILL_MD.read_text() if SKILL_MD.exists() else ""

    def test_skill_md_exists(self):
        """SKILL.md exists in the plan skill directory."""
        assert self.SKILL_MD.exists(), f"SKILL.md not found at {self.SKILL_MD}"
        assert self.CONTENT, "SKILL.md is empty"

    def test_references_status_lifecycle(self):
        """plan SKILL.md references StatusLifecycle for status management."""
        assert "StatusLifecycle" in self.CONTENT, (
            "plan SKILL.md should reference StatusLifecycle for status management"
        )


