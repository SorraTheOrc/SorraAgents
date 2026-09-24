#!/usr/bin/env python3
"""Tests: detect_regressions() must only flag slips that happened *inside* the window.

Covers SA-0MUFNU2WM00049A9. The original predicate had an operator-precedence
bug (``and`` binds tighter than ``or``), so the ``in_window()`` gate was
bypassed and every item that had ever been completed+in_review was reported as
a regression, regardless of when it slipped. These tests pin the correct
behaviour:

  - an item completed+in_review before that slipped *inside* the window IS flagged;
  - an item completed+in_review before that slipped *outside* the window is NOT flagged;
  - an item still completed+in_review is NOT flagged;
  - an item that was never completed+in_review before is NOT flagged.
"""  # noqa: EXE001
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
_SKILLS_ROOT = REPO_ROOT / "skill"
if str(_SKILLS_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILLS_ROOT))

WINDOW_START = datetime(2026, 9, 23, 6, 0, 0)
WINDOW_END = datetime(2026, 9, 24, 6, 0, 0)

# The canonical "full snapshot" marker lets the window be treated as valid.
_BEFORE = ("completed", "in_review")


def _reload_standup():
    """Import a fresh generate_standup module from the repo skill tree."""
    spec_path = REPO_ROOT / "skill" / "standup" / "scripts" / "generate_standup.py"
    spec = importlib.util.spec_from_file_location("generate_standup", str(spec_path))
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _item(iid: str, status: str, stage: str, updated_at: str) -> dict:
    return {"id": iid, "status": status, "stage": stage, "updatedAt": updated_at}


def _run(mod, before_states: dict, current: list, window_start=WINDOW_START, window_end=WINDOW_END):
    """Drive detect_regressions with injected snapshot + current data."""
    mod.get_snapshot_before = lambda _ws: "fake-snapshot"
    mod.load_snapshot_map = lambda _h: dict(before_states)
    mod.fetch_json = lambda _cmd: (current, "")
    return mod.detect_regressions(window_start, window_end)


class TestInWindowRegression:
    def test_completed_before_slipped_inside_window_is_flagged(self):
        mod = _reload_standup()
        item = _item("B", "open", "plan_complete", "2026-09-23T12:00:00")
        out = _run(mod, {"B": _BEFORE}, [item])
        assert {r["id"] for r in out} == {"B"}

    def test_window_start_is_inclusive(self):
        mod = _reload_standup()
        item = _item("D", "open", "plan_complete", "2026-09-23T06:00:00")
        out = _run(mod, {"D": _BEFORE}, [item])
        assert {r["id"] for r in out} == {"D"}

    def test_slipped_to_blocked_is_flagged(self):
        mod = _reload_standup()
        item = _item("G", "blocked", "plan_complete", "2026-09-23T20:00:00")
        out = _run(mod, {"G": _BEFORE}, [item])
        assert {r["id"] for r in out} == {"G"}


class TestNoFalsePositives:
    def test_completed_before_slipped_outside_window_is_not_flagged(self):
        """The core bug: a slip days before the window must not be reported."""
        mod = _reload_standup()
        item = _item("C", "open", "plan_complete", "2026-09-20T10:00:00")
        out = _run(mod, {"C": _BEFORE}, [item])
        assert out == []

    def test_still_completed_in_review_is_not_flagged(self):
        mod = _reload_standup()
        item = _item("A", "completed", "in_review", "2026-09-24T02:00:00")
        out = _run(mod, {"A": _BEFORE}, [item])
        assert out == []

    def test_never_completed_before_is_not_flagged(self):
        mod = _reload_standup()
        item = _item("F", "open", "plan_complete", "2026-09-23T12:00:00")
        out = _run(mod, {"F": ("open", "plan_complete")}, [item])
        assert out == []

    def test_item_absent_from_snapshot_is_not_flagged(self):
        mod = _reload_standup()
        item = _item("H", "open", "idea", "2026-09-23T12:00:00")
        out = _run(mod, {}, [item])
        assert out == []


class TestMixedSet:
    def test_only_in_window_slips_reported(self):
        """Regression guard for the precedence bug: C (outside) must not appear."""
        mod = _reload_standup()
        current = [
            _item("A", "completed", "in_review", "2026-09-24T02:00:00"),  # unchanged
            _item("B", "open", "plan_complete", "2026-09-23T12:00:00"),  # in window -> flag
            _item("C", "open", "plan_complete", "2026-09-20T10:00:00"),  # out of window -> no
            _item("D", "open", "plan_complete", "2026-09-23T06:00:00"),  # boundary -> flag
            _item("E", "open", "plan_complete", "2026-09-23T06:00:01"),  # in window -> flag
            _item("F", "open", "plan_complete", "2026-09-23T12:00:00"),  # never completed -> no
        ]
        before = {
            "A": _BEFORE,
            "B": _BEFORE,
            "C": _BEFORE,
            "D": _BEFORE,
            "E": _BEFORE,
            "F": ("open", "plan_complete"),
        }
        out = _run(mod, before, current)
        assert {r["id"] for r in out} == {"B", "D", "E"}
