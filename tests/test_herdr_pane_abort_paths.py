"""Abort-path integration for the herdr pane status indicator (SA-0MTFLQEQQ0083EW1).

AC4: on skill abort/failure the pane title must be set to the red state even
when no final report is produced. Each skill's abort path delegates to the
shared ``shared.herdr_pane.mark_aborted`` helper, which is fail-open.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
_SKILLS_ROOT = REPO_ROOT / "skill"
if str(_SKILLS_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILLS_ROOT))

from audit.scripts import audit_runner
from intake.scripts import intake


def _load_implement():
    path = REPO_ROOT / "skill" / "implement" / "scripts" / "implement.py"
    spec = importlib.util.spec_from_file_location(
        "implement_under_test_herdr_abort", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class TestAbortPathsMarkPaneAttention:
    """Every abort entry point delegates to ``mark_aborted``."""

    def test_intake_abort_marks_pane(self):
        with mock.patch("shared.herdr_pane.mark_aborted") as mark:
            intake._mark_pane_aborted()
        mark.assert_called_once()

    def test_implement_abort_marks_pane(self):
        implement = _load_implement()
        with mock.patch("shared.herdr_pane.mark_aborted") as mark:
            implement._mark_pane_aborted()
        mark.assert_called_once()

    def test_audit_abort_marks_pane(self):
        with mock.patch("shared.herdr_pane.mark_aborted") as mark:
            audit_runner._mark_abort_pane_status()
        mark.assert_called_once()


class TestAbortPathsFailOpen:
    """A helper failure must never break the abort path."""

    def test_helpers_swallow_mark_aborted_errors(self):
        implement = _load_implement()
        with mock.patch(
            "shared.herdr_pane.mark_aborted", side_effect=RuntimeError("boom"),
        ):
            intake._mark_pane_aborted()
            implement._mark_pane_aborted()
            audit_runner._mark_abort_pane_status()
