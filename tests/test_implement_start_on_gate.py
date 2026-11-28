"""Run-not-before (``startOn``) scheduling gate for parent/drive.

Work item: **LP-0MWZO40DI00701SY**.

Contract: ``implement.py``'s parent/drive orchestration must never start a
child whose ``startOn`` instant is still in the future. ``startOn`` is the
worklog claim guard's schedule (``WL-0MWMGEUT10056MUI``) — the same instant
the ``[run-not-before …]`` title marker restates — so a measurement-window
child started early can only no-op: its evidence does not exist yet.

Before the fix, ``phase_drive`` classified such a child as ``startable`` and
spawned a session ~45 h before the window closed.

The cases here pin:

1. classification: future → ``skip-not-before``, past/absent/unparseable →
   ``implement`` (fail open), and the documented precedence over
   ``skip-blocked``;
2. ``_revalidate_child_startable`` reports the instant as a soft wait;
3. ``phase_parent`` starts no session and does not advance the parent when
   the only remaining child is future-dated;
4. ``phase_drive`` spawns nothing, reports the child in its own bucket and
   sets no ``failed_child``.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests.test_implement_drive import (
    RecordingSpawner,
    ScriptedParent,
    _child_report,
)
from tests.test_implement_drive import (
    _run as _run_drive,
)
from tests.test_implement_parent_safety import ParentHarness, _child

_REPO_ROOT = Path(__file__).resolve().parent.parent
_IMPLEMENT_PY = _REPO_ROOT / "skill" / "implement" / "scripts" / "implement.py"

FUTURE_CHILD = "SA-FUTURE0001"
OPEN_SIBLING = "SA-OPEN00001"
PARENT = "SA-PARENT001"


@pytest.fixture(scope="module")
def implement_mod():
    """Load the module-under-test (skill/implement/scripts/implement.py)."""
    sys.path.insert(0, str(_REPO_ROOT))
    spec = importlib.util.spec_from_file_location(
        "implement_under_test_start_on", _IMPLEMENT_PY
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["implement_under_test_start_on"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _neutralise_ambient_drive_guard(monkeypatch):
    """Keep the module hermetic when the suite runs inside a driven session."""
    monkeypatch.delenv("IMPLEMENT_DRIVE_ACTIVE", raising=False)


def _start_on(delta: timedelta) -> str:
    """An ISO-8601 ``startOn`` relative to now (Z form, as ``wl`` emits)."""
    instant = datetime.now(timezone.utc) + delta
    return instant.isoformat().replace("+00:00", "Z")


def _scheduled_child(work_item_id: str, delta: timedelta, **kwargs) -> dict:
    child = _child(work_item_id, **kwargs)
    child["startOn"] = _start_on(delta)
    return child


# ===========================================================================
# AC: classification matrix
# ===========================================================================


class TestStartOnClassification:
    def test_future_start_on_is_not_startable(self, implement_mod):
        child = _scheduled_child(FUTURE_CHILD, timedelta(days=2))
        assert (
            implement_mod._classify_child(child)
            == implement_mod.ACTION_SKIP_NOT_BEFORE
        )

    def test_past_start_on_is_startable(self, implement_mod):
        child = _scheduled_child(FUTURE_CHILD, timedelta(days=-1))
        assert implement_mod._classify_child(child) == "implement"

    def test_absent_start_on_is_startable(self, implement_mod):
        assert implement_mod._classify_child(_child(OPEN_SIBLING)) == "implement"

    def test_unparseable_start_on_fails_open(self, implement_mod):
        """A malformed schedule must never wedge the chain."""
        child = _child(OPEN_SIBLING)
        child["startOn"] = "not-a-timestamp"
        assert implement_mod._classify_child(child) == "implement"

    def test_future_start_on_outranks_blocked(self, implement_mod):
        """Documented precedence: skip-not-before sits before skip-blocked."""
        child = _scheduled_child(
            FUTURE_CHILD, timedelta(days=2), status="blocked"
        )
        assert (
            implement_mod._classify_child(child)
            == implement_mod.ACTION_SKIP_NOT_BEFORE
        )

    def test_start_on_holds_boundary(self, implement_mod):
        """The instant itself is already reached; only later values hold."""
        now = datetime(2026, 11, 29, 21, 26, tzinfo=timezone.utc)
        assert implement_mod._start_on_holds(
            {"startOn": "2026-11-29T21:26:00Z"}, now=now
        ) is None
        assert implement_mod._start_on_holds(
            {"startOn": "2026-11-29T21:26:01Z"}, now=now
        ) is not None
        assert implement_mod._start_on_holds({}, now=now) is None

    def test_next_child_skips_future_and_picks_sibling(self, implement_mod):
        children = [
            _scheduled_child(FUTURE_CHILD, timedelta(days=2), sort_index=1),
            _child(OPEN_SIBLING, sort_index=2),
        ]
        picked = implement_mod._next_child_to_implement(children, {})
        assert picked is not None
        assert picked["id"] == OPEN_SIBLING

    def test_next_child_returns_none_when_only_child_is_future(
        self, implement_mod
    ):
        children = [_scheduled_child(FUTURE_CHILD, timedelta(days=2))]
        assert implement_mod._next_child_to_implement(children, {}) is None


# ===========================================================================
# AC: pre-spawn re-validation reports the schedule as a soft wait
# ===========================================================================


class TestRevalidateChildStartable:
    def test_future_child_is_not_startable_and_names_the_instant(
        self, implement_mod
    ):
        child = _scheduled_child(FUTURE_CHILD, timedelta(days=2))
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(implement_mod, "wl_show", lambda _wid: dict(child))
            verdict = implement_mod._revalidate_child_startable(
                FUTURE_CHILD, [child]
            )
        assert verdict["startable"] is False
        assert verdict["action"] == implement_mod.ACTION_SKIP_NOT_BEFORE
        assert verdict["not_before"]
        assert "startOn=" in verdict["reason"]
        assert "failed" not in verdict["reason"].lower()

    def test_past_child_remains_startable(self, implement_mod):
        child = _scheduled_child(FUTURE_CHILD, timedelta(days=-1))
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(implement_mod, "wl_show", lambda _wid: dict(child))
            verdict = implement_mod._revalidate_child_startable(
                FUTURE_CHILD, [child]
            )
        assert verdict["startable"] is True
        assert verdict["action"] == implement_mod.REVALIDATE_STARTABLE


# ===========================================================================
# AC: phase_parent starts nothing and does not advance on a future-only epic
# ===========================================================================


class TestParentSchedule:
    def test_future_only_child_is_not_started_and_parent_not_advanced(
        self, implement_mod
    ):
        children = [_scheduled_child(FUTURE_CHILD, timedelta(days=2))]
        harness = ParentHarness(implement_mod, children)
        report = harness.run(PARENT)

        assert report["success"] is True
        assert harness.calls["phase_start"] == []
        assert report.get("parent_advanced") is not True
        assert report.get("next_child") in (None, "")
        assert report["not_before_children"] == [FUTURE_CHILD]
        assert "run-not-before" in report["message"]
        # No lifecycle write: the parent stays open while the window runs.
        assert harness.calls["update_status"] == []

    def test_runnable_sibling_starts_while_future_child_waits(
        self, implement_mod
    ):
        children = [
            _scheduled_child(FUTURE_CHILD, timedelta(days=2), sort_index=1),
            _child(OPEN_SIBLING, sort_index=2),
        ]
        harness = ParentHarness(implement_mod, children)
        report = harness.run(PARENT)

        assert report["success"] is True
        assert harness.calls["phase_start"] == [OPEN_SIBLING]
        assert report["next_child"] == OPEN_SIBLING

    def test_past_dated_child_starts_normally(self, implement_mod):
        children = [_scheduled_child(FUTURE_CHILD, timedelta(days=-1))]
        harness = ParentHarness(implement_mod, children)
        report = harness.run(PARENT)

        assert report["success"] is True
        assert harness.calls["phase_start"] == [FUTURE_CHILD]


# ===========================================================================
# AC: phase_drive never spawns a future-dated child and does not fail it
# ===========================================================================


class TestDriveSchedule:
    def test_future_child_is_not_spawned_and_is_not_a_failure(
        self, implement_mod
    ):
        children = [_scheduled_child(FUTURE_CHILD, timedelta(days=2))]
        runner = ScriptedParent([
            _child_report(FUTURE_CHILD),
            {
                "success": True,
                "message": (
                    "No child is currently startable: scheduled children: "
                    f"{FUTURE_CHILD} (run-not-before 2026-11-30T21:26:00Z)"
                ),
                "not_before_children": [FUTURE_CHILD],
            },
        ])
        spawner = RecordingSpawner()

        report = _run_drive(
            implement_mod,
            runner,
            spawner,
            children,
            child_states={FUTURE_CHILD: dict(children[0])},
        )

        assert spawner.calls == []
        assert report["success"] is True
        assert "failed_child" not in report
        assert report.get("not_before_children") == [FUTURE_CHILD]
        assert report.get("parent_advanced") is not True

    def test_drive_continues_to_a_runnable_sibling(self, implement_mod):
        """The schedule hold is a soft wait, not the end of the chain."""
        children = [
            _scheduled_child(FUTURE_CHILD, timedelta(days=2), sort_index=1),
            _child(OPEN_SIBLING, sort_index=2),
        ]
        runner = ScriptedParent([
            _child_report(FUTURE_CHILD),
            _child_report(OPEN_SIBLING),
            {"success": True, "parent_advanced": True},
        ])
        spawner = RecordingSpawner([
            {"success": True, "returncode": 0, "reason": ""},
        ])

        report = _run_drive(
            implement_mod,
            runner,
            spawner,
            children,
            child_states={
                FUTURE_CHILD: dict(children[0]),
                OPEN_SIBLING: dict(children[1]),
            },
        )

        assert [call["id"] for call in spawner.calls] == [OPEN_SIBLING]
        assert report["success"] is True
        assert "failed_child" not in report
