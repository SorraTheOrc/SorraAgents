"""``do-not-delegate`` guard in ``implement.py`` parent recursion
(SA-0MUWNKUIU007N876).

An item tagged ``do-not-delegate`` must never be claimed, started or given a
worktree by *automatic* parent recursion (``implement.py parent`` /
``implement.py drive``): ``wl`` itself refuses to hand such an item to an
automated agent without ``--force``, so an automatic implement selection is
the same delegation. Naming the child explicitly (``implement.py start
<child>`` / ``finish`` / ``drive <child>``) remains the documented override.

AC mapping
----------
- AC1: ``_classify_child`` returns ``skip-do-not-delegate`` for a tagged,
  non-terminal child (case- and whitespace-insensitive, ``None``/missing
  ``tags``-safe); ``_next_child_to_implement`` skips it exactly like a
  terminal child.
- AC2: ``phase_parent`` neither claims nor worktrees a guarded child; it
  records ``action: "skipped-do-not-delegate"`` in ``children_processed``,
  lists the ids in ``guarded_children`` and still starts the next startable
  sibling.
- AC3: when only guarded children remain the parent is not advanced, no
  ``next_child`` is reported, the message names the children and the tag (not
  the dependency-blocked wording), and a re-run is idempotent.
- AC4: ``phase_drive`` spawns no session for a guarded child and surfaces
  ``guarded_children`` plus an explicit ``do-not-delegate`` message.
- AC5: only automatic selection is gated — the classification of terminal,
  in-progress, held and startable children is unchanged, so an explicit
  ``start``/``finish``/``drive`` on a guarded item behaves exactly as before.

The shapes pinned here are the ones that broke: a guarded child with the
*lowest* ``sortIndex`` (so it sorts first) alongside open siblings, an
all-guarded parent (must not advance), and ``drive`` (must not spawn).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest import mock

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_IMPLEMENT_PY = _REPO_ROOT / "skill" / "implement" / "scripts" / "implement.py"

PARENT = "SA-PARENTDND"
GUARDED = "SA-DND-GUARDED"
SIBLING = "SA-DND-SIBLING"
GUARDED_TWO = "SA-DND-GUARDED2"

#: The report action asserted by AC2 (``children_processed`` entry).
SKIPPED_ACTION = "skipped-do-not-delegate"
#: The classification asserted by AC1 (``_classify_child`` return value).
SKIP_CLASS = "skip-do-not-delegate"
#: The tag that guards a child against automatic delegation.
TAG = "do-not-delegate"


@pytest.fixture(scope="module")
def implement_mod():
    """Load the module-under-test (skill/implement/scripts/implement.py)."""
    sys.path.insert(0, str(_REPO_ROOT))
    spec = importlib.util.spec_from_file_location(
        "implement_under_test_do_not_delegate", _IMPLEMENT_PY
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["implement_under_test_do_not_delegate"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _no_live_holder(implement_mod):
    """Keep classification hermetic: never probe the real worklog lock store.

    ``_classify_child`` probes the child's per-work-item lock; a stray lock in
    the host checkout would otherwise turn a guarded child into
    ``skipped-held`` and mask the classification under test.
    """
    with mock.patch.object(implement_mod, "_probe_child_lock", return_value=None):
        yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _child(
    work_item_id: str,
    status: str = "open",
    sort_index: int = 1000,
    tags: object = None,
    *,
    include_tags: bool = True,
    deleted_by: str = "",
    assignee: str = "",
) -> dict:
    """Build a ``wl show --children`` child dict.

    ``include_tags=False`` omits the ``tags`` key entirely — the shape a work
    item without any tag can arrive in.
    """
    child: dict = {
        "id": work_item_id,
        "title": f"Child {work_item_id}",
        "status": status,
        "stage": "intake_complete",
        "assignee": assignee,
        "priority": "high",
        "sortIndex": sort_index,
        "deletedBy": deleted_by,
    }
    if include_tags:
        child["tags"] = tags
    return child


class ParentHarness:
    """Canned ``wl``/``phase_start`` plumbing for ``phase_parent`` scenarios."""

    def __init__(self, mod, children, *, blockers_map=None):
        self.mod = mod
        self.children = children
        self.blockers_map = blockers_map or {}
        self.calls = {"phase_start": [], "update_status": [], "comments": []}

    def _fake_phase_start(self, child_id, **kwargs):
        self.calls["phase_start"].append(child_id)
        return {
            "success": True,
            "work_item_id": child_id,
            "worktree_path": f"/wt/{child_id}",
            "branch": f"wl-{child_id}",
            "message": "Worktree created",
        }

    def _fake_update_status(
        self, work_item_id, status, stage=None, assignee=None, **kwargs
    ):
        self.calls["update_status"].append((work_item_id, status, stage, assignee))
        return {"success": True, "workItem": {"id": work_item_id, "status": status}}

    def _fake_add_comment(self, work_item_id, comment):
        self.calls["comments"].append((work_item_id, comment))
        return True

    def _fake_dep_blockers(self, child_id, **_):
        return list(self.blockers_map.get(child_id, []))

    def _fake_invoke(self, action, child_id, **kwargs):
        if action == "start":
            return self._fake_phase_start(child_id, **kwargs)
        if action == "finish":
            return {"success": True, "work_item_id": child_id}
        raise AssertionError(f"unexpected action {action}")

    def run(self, parent_id: str = PARENT) -> dict:
        with (
            mock.patch.object(
                self.mod, "wl_show",
                return_value={"id": parent_id, "title": "P", "status": "open"},
            ),
            mock.patch.object(
                self.mod, "wl_show_children", return_value=self.children
            ),
            mock.patch.object(
                self.mod, "wl_dep_blockers",
                side_effect=self._fake_dep_blockers,
            ),
            mock.patch.object(
                self.mod, "phase_start", side_effect=self._fake_phase_start
            ),
            mock.patch.object(
                self.mod, "_invoke_implement", side_effect=self._fake_invoke
            ),
            mock.patch.object(self.mod, "_discover_worktree", return_value=None),
            mock.patch.object(self.mod, "_has_worktree_changes", return_value=False),
            mock.patch.object(
                self.mod.StatusLifecycle, "update_status",
                side_effect=self._fake_update_status,
            ),
            mock.patch.object(
                self.mod, "wl_add_comment", side_effect=self._fake_add_comment
            ),
            mock.patch.object(
                self.mod, "is_code_freeze_active", return_value=False
            ),
        ):
            return self.mod.phase_parent(parent_id, json_output=True)


def _processed(report: dict, child_id: str) -> dict:
    """Return the ``children_processed`` entry for *child_id*."""
    for entry in report.get("children_processed") or []:
        if entry.get("id") == child_id:
            return entry
    raise AssertionError(
        f"no children_processed entry for {child_id}: "
        f"{report.get('children_processed')}"
    )


# ===========================================================================
# AC1 — guarded classification
# ===========================================================================


class TestClassifyChild:
    """The classifier must expose a dedicated guarded action."""

    def test_guarded_child_classifies_as_skip_do_not_delegate(self, implement_mod):
        child = _child(GUARDED, tags=[TAG])
        assert implement_mod._classify_child(child) == SKIP_CLASS

    def test_exports_the_classification_and_report_constants(self, implement_mod):
        """The action strings are module-level constants (not magic values)."""
        assert implement_mod.DO_NOT_DELEGATE_TAG == TAG
        assert implement_mod.ACTION_SKIP_DO_NOT_DELEGATE == SKIP_CLASS
        assert implement_mod.ACTION_SKIPPED_DO_NOT_DELEGATE == SKIPPED_ACTION

    def test_unguarded_child_is_still_implementable(self, implement_mod):
        assert implement_mod._classify_child(
            _child(GUARDED, tags=["other-tag"])
        ) == "implement"
        assert implement_mod._classify_child(
            _child(GUARDED, tags=[])
        ) == "implement"

    @pytest.mark.parametrize(
        "tags",
        [
            ["Do-Not-Delegate"],
            ["  do-not-delegate  "],
            ["DO-NOT-DELEGATE"],
            ["\tdo-not-delegate\n"],
            ["some-tag", " Do-Not-Delegate ", "another"],
        ],
    )
    def test_tag_matching_is_case_and_whitespace_insensitive(
        self, implement_mod, tags
    ):
        assert implement_mod._classify_child(
            _child(GUARDED, tags=tags)
        ) == SKIP_CLASS

    def test_missing_or_none_tags_never_raise(self, implement_mod):
        """A child dict without ``tags`` (or with ``tags: None``) must not
        raise — it is simply not guarded."""
        assert implement_mod._classify_child(
            _child(GUARDED, include_tags=False)
        ) == "implement"
        assert implement_mod._classify_child(
            _child(GUARDED, tags=None)
        ) == "implement"

    def test_non_list_tags_value_is_tolerated(self, implement_mod):
        """A scalar ``tags`` value (defensive: some producers emit a string)
        is treated as a single tag rather than iterated character-wise."""
        assert implement_mod._classify_child(
            _child(GUARDED, tags=TAG)
        ) == SKIP_CLASS
        assert implement_mod._classify_child(
            _child(GUARDED, tags=42)
        ) == "implement"

    def test_terminal_guarded_child_keeps_skip_terminal(self, implement_mod):
        """A terminal child is never re-reported as guarded (risk: the tag is
        present on many completed items) — ``skip-terminal`` still wins."""
        for status in ("completed", "in_review", "done", "deleted"):
            assert implement_mod._classify_child(
                _child(GUARDED, status=status, tags=[TAG])
            ) == "skip-terminal"
        assert implement_mod._classify_child(
            _child(GUARDED, status="open", tags=[TAG], deleted_by="plan")
        ) == "skip-terminal"

    def test_in_progress_guarded_child_keeps_skip_in_progress(self, implement_mod):
        """No behaviour change for an in-progress child: it stays
        ``skip-in-progress`` so an operator's manual work is still finished,
        never re-started."""
        for status in ("in_progress", "in-progress"):
            assert implement_mod._classify_child(
                _child(GUARDED, status=status, tags=[TAG])
            ) == "skip-in-progress"

    def test_held_guarded_child_keeps_skipped_held(self, implement_mod):
        """A live holder is a fact about a running process and outranks the
        tag, so contention is still reported as ``skipped-held``."""
        with mock.patch.object(
            implement_mod, "_probe_child_lock",
            return_value={"agent": "other", "pid": 1},
        ):
            assert implement_mod._classify_child(
                _child(GUARDED, tags=[TAG])
            ) == "skipped-held"


class TestNextChildToImplement:
    """``_next_child_to_implement`` must skip guarded children."""

    def test_guarded_child_with_lowest_sort_index_is_skipped(self, implement_mod):
        """The exact observed failure shape: the guarded child sorts first."""
        children = [
            _child(GUARDED, sort_index=1, tags=[TAG]),
            _child(SIBLING, sort_index=2, tags=[]),
        ]
        nxt = implement_mod._next_child_to_implement(children)
        assert nxt is not None
        assert nxt["id"] == SIBLING

    def test_all_children_guarded_returns_none(self, implement_mod):
        children = [
            _child(GUARDED, sort_index=1, tags=[TAG]),
            _child(GUARDED_TWO, sort_index=2, tags=[" " + TAG]),
        ]
        assert implement_mod._next_child_to_implement(children) is None

    def test_guarded_child_does_not_unblock_a_dependent(self, implement_mod):
        """A guarded child is not terminal, so a sibling depending on it stays
        blocked (guarded ≠ terminal)."""
        children = [
            _child(GUARDED, sort_index=1, tags=[TAG]),
            _child(SIBLING, sort_index=2, tags=[]),
        ]
        blockers = {SIBLING: [{"id": GUARDED, "direction": "depends-on"}]}
        assert implement_mod._next_child_to_implement(children, blockers) is None


# ===========================================================================
# AC2 — the parent never starts a guarded child
# ===========================================================================


class TestParentNeverStartsGuardedChild:
    def test_guarded_child_is_reported_and_the_sibling_starts(self, implement_mod):
        """Guarded child with the lowest ``sortIndex`` plus an open sibling:
        the sibling is started, the guarded child is reported, and it is
        neither claimed nor given a worktree."""
        children = [
            _child(GUARDED, sort_index=1, tags=[TAG]),
            _child(SIBLING, sort_index=2, tags=["unrelated"]),
        ]
        harness = ParentHarness(implement_mod, children)
        report = harness.run()

        assert report["success"] is True
        assert report.get("next_child") == SIBLING
        assert harness.calls["phase_start"] == [SIBLING]
        # No claim for the guarded child (no status mutation naming it).
        assert all(entry[0] != GUARDED for entry in harness.calls["update_status"])
        # Distinct action in children_processed + additive report field.
        assert _processed(report, GUARDED)["action"] == SKIPPED_ACTION
        assert report.get("guarded_children") == [GUARDED]
        # Tagged child is never confused with a dependency-blocked one.
        assert GUARDED not in (report.get("blocked_children") or [])
        # Parent is not advanced — the guarded child is still non-terminal.
        assert not report.get("parent_advanced")

    def test_guarded_child_reported_even_when_nothing_is_startable(
        self, implement_mod
    ):
        """A guarded child must never be silently dropped from the report."""
        children = [
            _child(GUARDED, sort_index=1, tags=[TAG]),
            _child(SIBLING, sort_index=2, status="blocked"),
        ]
        harness = ParentHarness(
            implement_mod,
            children,
            blockers_map={SIBLING: [{"id": "SA-EXTERNAL", "direction": "depends-on"}]},
        )
        report = harness.run()

        assert harness.calls["phase_start"] == []
        assert _processed(report, GUARDED)["action"] == SKIPPED_ACTION
        assert report.get("guarded_children") == [GUARDED]
        assert report.get("blocked_children") == [SIBLING]


# ===========================================================================
# AC3 — no premature advance, distinct waiting report, idempotent re-run
# ===========================================================================


class TestAllGuardedParent:
    def _all_guarded(self) -> list[dict]:
        return [
            _child(GUARDED, sort_index=1, tags=[TAG]),
            _child(GUARDED_TWO, sort_index=2, tags=["Do-Not-Delegate"]),
        ]

    def test_parent_is_not_advanced_when_only_guarded_children_remain(
        self, implement_mod
    ):
        harness = ParentHarness(implement_mod, self._all_guarded())
        report = harness.run()

        assert report["success"] is True
        assert not report.get("parent_advanced")
        assert report.get("next_child") is None
        assert harness.calls["phase_start"] == []
        assert harness.calls["update_status"] == []
        assert report.get("guarded_children") == [GUARDED, GUARDED_TWO]

    def test_waiting_message_names_the_children_and_the_tag(self, implement_mod):
        harness = ParentHarness(implement_mod, self._all_guarded())
        report = harness.run()

        message = report["message"]
        assert GUARDED in message and GUARDED_TWO in message
        assert TAG in message
        # Not the dependency-blocked / held wording.
        assert "blocked by non-terminal siblings" not in message
        assert "another agent holds" not in message

    def test_re_run_is_idempotent(self, implement_mod):
        """No claim, no worktree, no status change, same report."""
        harness = ParentHarness(implement_mod, self._all_guarded())
        first = harness.run()
        second = harness.run()

        assert first["guarded_children"] == second["guarded_children"]
        assert first["message"] == second["message"]
        assert harness.calls["phase_start"] == []
        assert harness.calls["update_status"] == []
        # Nothing about a guarded child is written to the store.
        assert harness.calls["comments"] == []

    def test_guarded_children_do_not_count_as_terminal(self, implement_mod):
        """A terminal sibling plus a guarded child: the parent waits, it does
        not treat the guarded child as done and advance."""
        children = [
            _child("SA-DND-DONE", status="completed", sort_index=1),
            _child(GUARDED, sort_index=2, tags=[TAG]),
        ]
        harness = ParentHarness(implement_mod, children)
        report = harness.run()

        assert not report.get("parent_advanced")
        assert harness.calls["update_status"] == []
        assert report.get("guarded_children") == [GUARDED]


# ===========================================================================
# AC4 — drive parity: no session, guarded report surfaced
# ===========================================================================


class _SpawnRecorder:
    """Records child-session spawns without starting a real Pi session."""

    def __init__(self):
        self.spawned: list[str] = []

    def __call__(self, child_id, worktree_path, timeout, env):
        self.spawned.append(child_id)
        return {"success": True, "returncode": 0, "stdout": "", "stderr": ""}


def _drive(implement_mod, parent_report: dict, spawner=None):
    """Run ``phase_drive`` with a scripted ``parent`` runner."""
    spawner = spawner or _SpawnRecorder()
    calls: list[tuple] = []

    def runner(action, work_item_id, no_refactor=False, verbose=False):
        calls.append((action, work_item_id, parent_report))
        return parent_report

    with (
        mock.patch.object(
            implement_mod, "wl_show",
            return_value={"id": PARENT, "title": "P", "status": "open"},
        ),
        mock.patch.object(
            implement_mod, "wl_show_children",
            return_value=[
                _child(GUARDED, sort_index=1, tags=[TAG]),
                _child(SIBLING, sort_index=2, tags=[]),
            ],
        ),
        mock.patch.object(implement_mod, "wl_add_comment", return_value=True),
    ):
        report = implement_mod.phase_drive(
            PARENT, json_output=True, parent_runner=runner, spawn_session=spawner,
        )
    return report, spawner, calls


class TestDriveParity:
    def test_no_session_is_spawned_for_a_guarded_child(self, implement_mod):
        """The parent reports only guarded children → drive spawns nothing."""
        parent_report = {
            "phase": "parent",
            "work_item_id": PARENT,
            "success": True,
            "parent_advanced": False,
            "next_child": None,
            "guarded_children": [GUARDED],
            "children_processed": [
                {"id": GUARDED, "action": SKIPPED_ACTION, "status": "open"},
            ],
            "message": (
                f"No child of {PARENT} is currently startable: {GUARDED} is "
                f"guarded by the `{TAG}` tag."
            ),
        }
        report, spawner, _ = _drive(implement_mod, parent_report)

        assert spawner.spawned == []
        assert report["sessions_spawned"] == 0
        assert report.get("guarded_children") == [GUARDED]
        assert TAG in report["message"]
        # Never a misleading "blocked or held" stop.
        assert "blocked or held" not in report["message"]

    def test_guarded_set_is_surfaced_even_without_a_parent_message(
        self, implement_mod
    ):
        """Defensive: if the parent report carries ``guarded_children`` but no
        message, drive still states the guard explicitly."""
        parent_report = {
            "phase": "parent",
            "work_item_id": PARENT,
            "success": True,
            "parent_advanced": False,
            "next_child": None,
            "guarded_children": [GUARDED],
            "children_processed": [],
            "message": "",
        }
        report, spawner, _ = _drive(implement_mod, parent_report)

        assert spawner.spawned == []
        assert report.get("guarded_children") == [GUARDED]
        assert TAG in report["message"]
        assert GUARDED in report["message"]

    def test_startable_sibling_is_still_driven(self, implement_mod):
        """A guarded child never starves a startable sibling in drive."""
        parent_report = {
            "phase": "parent",
            "work_item_id": PARENT,
            "success": True,
            "parent_advanced": False,
            "next_child": SIBLING,
            "worktree_path": f"/wt/{SIBLING}",
            "branch": f"wl-{SIBLING}",
            "guarded_children": [GUARDED],
            "children_processed": [
                {"id": GUARDED, "action": SKIPPED_ACTION, "status": "open"},
            ],
            "message": f"Started child {SIBLING}.",
        }
        # Second parent call reports the epic complete so the loop ends.
        reports = [parent_report]
        spawner = _SpawnRecorder()

        def runner(action, work_item_id, no_refactor=False, verbose=False):
            if reports:
                return reports.pop(0)
            return {
                "phase": "parent",
                "work_item_id": PARENT,
                "success": True,
                "parent_advanced": True,
                "message": "all terminal",
            }

        with (
            mock.patch.object(
                implement_mod, "wl_show",
                return_value={"id": PARENT, "title": "P", "status": "open"},
            ),
            mock.patch.object(
                implement_mod, "wl_show_children",
                return_value=[_child(GUARDED, tags=[TAG])],
            ),
            mock.patch.object(implement_mod, "wl_add_comment", return_value=True),
        ):
            report = implement_mod.phase_drive(
                PARENT, json_output=True, parent_runner=runner,
                spawn_session=spawner,
            )

        assert spawner.spawned == [SIBLING]
        assert report.get("parent_advanced") is True


# ===========================================================================
# AC5 — explicit override unchanged (classification of other states)
# ===========================================================================


class TestExplicitOverrideUnaffected:
    def test_guarded_tag_does_not_gate_explicit_start(self, implement_mod):
        """``phase_start`` (explicit invocation) has no do-not-delegate guard:
        the tag is not consulted on the explicit path at all."""
        source = Path(_IMPLEMENT_PY).read_text(encoding="utf-8")
        start_src = source.split("def phase_start(", 1)[1].split(
            "\ndef phase_finish(", 1
        )[0]
        assert "DO_NOT_DELEGATE" not in start_src
        assert TAG not in start_src

    def test_guarded_child_is_startable_when_named_directly(self, implement_mod):
        """The classifier gate lives in parent recursion only — the child dict
        itself is otherwise ordinary (open, unclaimed)."""
        child = _child(GUARDED, tags=[TAG])
        assert child["status"] == "open"
        assert implement_mod._is_terminal_child(child) is False
