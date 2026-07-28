#!/usr/bin/env python3
"""Tests for StatusLifecycle integration in effort-and-risk run_skill.py.

Validates that run_skill.py uses the shared StatusLifecycle context manager
for automatic status management (in_progress on entry, completed on exit,
restore on failure).

StatusLifecycle itself is tested separately in ``skill/shared/test_status_lifecycle.py``.
These tests focus on the integration point: the source code structure of
``run_skill.py`` and ``SKILL.md``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "skill" / "effort-and-risk" / "scripts"


# ===========================================================================
# Tests: run_skill.py uses StatusLifecycle
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

    def test_uses_context_manager(self):
        """run_skill.py wraps logic in a 'with StatusLifecycle()' block."""
        assert "with StatusLifecycle(" in self.SOURCE, (
            "run_skill.py should wrap its main logic in a "
            "`with StatusLifecycle(...):` block"
        )

    def test_no_ad_hoc_wl_update_in_source(self):
        """run_skill.py should NOT contain ad-hoc 'wl update' calls.

        wl commands are now managed by StatusLifecycle.
        Only 'wl show' is still used directly (for fetching issue data).
        """
        # Find all subprocess.run calls that contain 'wl'
        lines = self.SOURCE.split("\n")
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if "subprocess.run" in stripped and "wl" in stripped:
                # Allow only 'wl show', not 'wl update'
                assert "wl show" in stripped, (
                    f"Line {i}: subprocess.run with 'wl' should only be 'wl show', "
                    f"not other wl commands: {stripped.strip()}"
                )

    def test_context_manager_uses_issue_id(self):
        """The StatusLifecycle context manager is invoked with the issue ID."""
        assert "with StatusLifecycle(issue_id)" in self.SOURCE or \
               "with StatusLifecycle(args.issue)" in self.SOURCE, (
            "StatusLifecycle should be instantiated with the issue/work item ID"
        )


# ===========================================================================
# Tests: SKILL.md documentation hygiene
# ===========================================================================


class TestSkillDocHygiene:
    """Tests that SKILL.md no longer documents ad-hoc wl update --status commands."""

    SKILL_MD = (REPO_ROOT / "skill" / "effort-and-risk" / "SKILL.md").read_text()

    def test_no_ad_hoc_status_commands(self):
        """SKILL.md should not document ad-hoc 'wl update --status' commands."""
        assert "wl update --status in_progress" not in self.SKILL_MD, (
            "SKILL.md should not document ad-hoc 'wl update --status in_progress'. "
            "Status is managed by StatusLifecycle automatically."
        )
        assert "wl update --status open" not in self.SKILL_MD, (
            "SKILL.md should not document ad-hoc 'wl update --status open'. "
            "Status is managed by StatusLifecycle automatically."
        )

    def test_has_status_lifecycle_reference(self):
        """SKILL.md should reference StatusLifecycle instead of ad-hoc commands."""
        assert "StatusLifecycle" in self.SKILL_MD, (
            "SKILL.md should reference StatusLifecycle for status management."
        )

    def test_mentions_auto_status_management(self):
        """SKILL.md should describe automatic status management."""
        has_auto = any([
            "automatically" in self.SKILL_MD.lower(),
            "managed automatically" in self.SKILL_MD.lower(),
            "automatic" in self.SKILL_MD.lower(),
        ])
        assert has_auto, (
            "SKILL.md should describe that status is managed automatically."
        )


if __name__ == "__main__":
    pytest.main(sys.argv)
