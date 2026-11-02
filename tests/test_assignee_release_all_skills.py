
# <!-- REFACTOR-SA-0MVYZJMK2001KFM6
# smell: unknown
# severity: low
# description: Unparenthesized implicit string concatenation in collection
# -->
"""Table-driven assignee-release regression matrix for every skill terminal entry point.

A non-empty ``assignee`` is a scheduling hold; the canonical statement in
``AGENTS_GLOBAL.md`` (Assignee release invariant) is: "any code path that
claims an ``assignee`` must release it on every terminal outcome; a persistent
hold is represented by the producer assignee (``Map``) or
``needsProducerReview``, never by a lingering agent assignee.". ``wl next``
skips a held item, so an item left on a lingering *agent* assignee after a
terminal outcome is invisible to dispatch and blocks re-work.

This module is the **enforcement point** for the assignee-release invariant
(parent SA-0MVVEDK2Q008NDUH, child SA-0MVVF6GWG005VIFK): one table enumerates
every skill terminal entry point and asserts the persisted ``assignee`` is
empty after both a successful and a failure/abort outcome. Every row is
exercised against an in-memory worklog (:class:`FakeWorklogStore`) injected
through the shared ``StatusLifecycle`` runner seam, and the persisted value is
read back with ``wl show <id> --json`` through that same runner — no live
worklog writes and no mocks of the behaviour under test.

The matrix is paired with a *discovery guard*: it scans
``skill/*/scripts/*.py`` for assignee mutation and fails loudly when a
script, or an entry point inside one, is not classified by this module. A new
skill that claims an assignee therefore cannot silently reintroduce the hold
without adding a row here.

Intentional holds (producer ``Map`` holds, ``needsProducerReview`` holds, the
agent-log-review creation assignee and ``verify_context_reduction``'s assignee
restore) are pinned separately in :data:`PINNED_HOLD_ROWS`; they must survive
any future default flip unchanged.
"""

from __future__ import annotations

import ast
import contextlib
import importlib.util
import json
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from audit.scripts import audit_runner, verify_context_reduction
from intake.scripts import intake
from refactor.scripts import refactor
from shared import status_lifecycle, tree_coverage

from skill.implement.scripts import implement

_AGENT = "matrix-agent"
_WORK_ITEM_ID_RE = re.compile(r"^[A-Z][A-Z0-9]*-[A-Z0-9-]+$")
_SKILL_ROOT = REPO_ROOT / "skill"


def _load_hyphenated_skill_module(source: str, name: str):
    """Load a script from a hyphenated skill directory as a module.

    Skill directories such as ``find-related`` and ``agent-log-review`` are
    not importable package names, so the module is loaded from its file path.
    """
    path = _SKILL_ROOT / source
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # ``dataclasses`` resolves the defining module through ``sys.modules``.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


agent_log_review = _load_hyphenated_skill_module(
    "agent-log-review/scripts/agent_log_review.py", "_matrix_agent_log_review"
)
find_related = _load_hyphenated_skill_module(
    "find-related/scripts/find_related.py", "_matrix_find_related"
)
effort_and_risk_run_skill = _load_hyphenated_skill_module(
    "effort-and-risk/scripts/run_skill.py", "_matrix_effort_and_risk_run_skill"
)


# ---------------------------------------------------------------------------
# In-memory worklog: the injected runner
# ---------------------------------------------------------------------------


class FakeWorklogStore:
    """A minimal, stateful ``wl`` CLI over an in-memory work-item store.

    The ``runner`` method matches the injectable runner contract used by
    :class:`~shared.status_lifecycle.StatusLifecycle` and the skill scripts.
    It models exactly the semantics the matrix depends on:

    * ``wl update <id> --status/--stage/--assignee/--needs-producer-review``
      mutates the item (so a read-back reflects what the code actually wrote);
    * ``wl show <id> --json`` returns the persisted item (the read-back path);
    * ``wl show <id> --children --json`` returns the parent plus its children;
    * ``wl create`` creates an item with the requested assignee (intentional
      producer holds);
    * every other command succeeds with a minimal, well-formed JSON payload.

    No command ever reaches the real worklog, so exercising the production
    entry points here cannot mutate the repository's store.
    """

    def __init__(self, items: dict[str, dict[str, Any]] | None = None) -> None:
        self.items: dict[str, dict[str, Any]] = {
            item_id: self._normalise_item(item_id, dict(item))
            for item_id, item in (items or {}).items()
        }
        self.update_commands: list[list[str]] = []
        self.show_commands: list[list[str]] = []
        self.create_commands: list[list[str]] = []
        self._fail_once_status: str | None = None
        self._next_created = 1

    # -- construction helpers -------------------------------------------

    @staticmethod
    def _normalise_item(item_id: str, item: dict[str, Any]) -> dict[str, Any]:
        item.setdefault("id", item_id)
        item.setdefault("title", item_id)
        item.setdefault("status", "in-progress")
        item.setdefault("stage", "plan_complete")
        item.setdefault("assignee", "")
        item.setdefault("needsProducerReview", False)
        item.setdefault("parentId", None)
        return item

    @classmethod
    def with_item(
        cls,
        item_id: str,
        *,
        status: str = "in-progress",
        stage: str = "plan_complete",
        assignee: str = _AGENT,
        needs_producer_review: bool = False,
        parent_id: str | None = None,
    ) -> FakeWorklogStore:
        """Build a store holding one claimed item (the matrix's common seed)."""
        return cls(
            {
                item_id: {
                    "id": item_id,
                    "title": item_id,
                    "status": status,
                    "stage": stage,
                    "assignee": assignee,
                    "needsProducerReview": needs_producer_review,
                    "parentId": parent_id,
                }
            }
        )

    def add_item(self, item: dict[str, Any]) -> None:
        """Add (or replace) an item in the store."""
        item_id = str(item.get("id", ""))
        self.items[item_id] = self._normalise_item(item_id, dict(item))

    def fail_next_update(self, status: str) -> None:
        """Make the next ``wl update --status <status>`` fail once.

        Models a transient worklog write failure so the production code's
        failure-reset branch (and its assignee release) is exercised.
        """
        self._fail_once_status = status

    # -- read helpers ----------------------------------------------------

    def show(self, item_id: str) -> dict[str, Any]:
        """Read the persisted item back through ``wl show --json``."""
        proc = self.runner(["wl", "show", item_id, "--json"])
        assert proc.returncode == 0, proc.stdout
        return json.loads(proc.stdout)["workItem"]

    def assignee(self, item_id: str) -> str:
        """The persisted ``assignee`` for *item_id*, read via ``wl show``."""
        return str(self.show(item_id).get("assignee", ""))

    # -- runner ----------------------------------------------------------

    def runner(self, cmd: list[str]) -> SimpleNamespace:
        """Injectable command runner matching ``subprocess.run``'s shape."""
        command = [str(token) for token in cmd]
        item_id = self._item_id(command)

        if "update" in command and item_id is not None:
            self.update_commands.append(command)
            return self._run_update(item_id, command)
        if "show" in command and item_id is not None:
            self.show_commands.append(command)
            return self._run_show(item_id, command)
        if "create" in command:
            self.create_commands.append(command)
            return self._run_create(command)
        if "dep" in command:
            return self._ok({"success": True, "outbound": [], "inbound": []})
        if "list" in command:
            return self._ok({"success": True, "workItems": []})
        if "audit-show" in command:
            return self._ok({"success": True, "audit": None})
        if "audit-set" in command:
            return self._ok({"success": True})
        if "comment" in command:
            return self._ok({"success": True})
        return self._ok({"success": True})

    # -- runner internals ------------------------------------------------

    @staticmethod
    def _ok(payload: dict[str, Any]) -> SimpleNamespace:
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    @staticmethod
    def _fail(message: str) -> SimpleNamespace:
        payload = {"success": False, "error": message}
        return SimpleNamespace(returncode=1, stdout=json.dumps(payload), stderr="")

    @staticmethod
    def _item_id(command: list[str]) -> str | None:
        for token in command:
            if _WORK_ITEM_ID_RE.match(token):
                return token
        return None

    @staticmethod
    def _flag(command: list[str], name: str) -> str | None:
        try:
            return command[command.index(name) + 1]
        except (ValueError, IndexError):
            return None

    def _run_show(self, item_id: str, command: list[str]) -> SimpleNamespace:
        item = self.items.get(item_id)
        if item is None:
            return self._fail(f"work item {item_id} not found")
        if "--children" in command:
            children = [
                child
                for child in self.items.values()
                if child.get("parentId") == item_id
            ]
            return self._ok(
                {"success": True, "workItem": dict(item), "children": children}
            )
        return self._ok({"success": True, "workItem": dict(item), "comments": []})

    def _run_update(self, item_id: str, command: list[str]) -> SimpleNamespace:
        item = self.items.get(item_id)
        if item is None:
            return self._fail(f"work item {item_id} not found")
        requested_status = self._flag(command, "--status")
        if (
            self._fail_once_status is not None
            and requested_status == self._fail_once_status
        ):
            self._fail_once_status = None
            return self._fail("simulated transient worklog write failure")
        if requested_status is not None:
            item["status"] = requested_status
        requested_stage = self._flag(command, "--stage")
        if requested_stage is not None:
            item["stage"] = requested_stage
        requested_assignee = self._flag(command, "--assignee")
        if requested_assignee is not None:
            item["assignee"] = requested_assignee
        requested_review = self._flag(command, "--needs-producer-review")
        if requested_review is not None:
            item["needsProducerReview"] = requested_review == "yes"
        return self._ok({"success": True, "workItem": dict(item)})

    def _run_create(self, command: list[str]) -> SimpleNamespace:
        item_id = f"SA-CREATED-{self._next_created}"
        self._next_created += 1
        item = self._normalise_item(
            item_id,
            {
                "id": item_id,
                "title": self._flag(command, "--title") or item_id,
                "status": "open",
                "stage": "idea",
                "assignee": self._flag(command, "--assignee") or "",
                "parentId": self._flag(command, "--parent"),
            },
        )
        self.items[item_id] = item
        return self._ok({"success": True, "workItem": dict(item)})


# ---------------------------------------------------------------------------
# Runner injection + module-boundary patches
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _inject_store_runner(store: FakeWorklogStore):
    """Route every shared ``StatusLifecycle`` / ``run_wl`` call to *store*.

    ``StatusLifecycle`` resolves its default runner from the
    ``skill.shared.status_lifecycle`` module at call time, so patching that
    seam routes the real ``update_status``/``show`` calls (and ``run_wl``)
    through the in-memory store without replacing the class or its logic.
    """
    with patch.object(status_lifecycle, "_default_runner", store.runner):
        yield


def _fake_run_cmd(store: FakeWorklogStore) -> Callable[..., subprocess.CompletedProcess]:
    """Build an ``implement.run_cmd`` stand-in.

    ``implement.py`` shells out to ``wl`` and ``git`` through ``run_cmd``;
    the matrix routes ``wl`` to the in-memory store and answers every other
    command (git plumbing, process probes) with an empty success.
    """

    def runner(cmd: list[str], *args: Any, **kwargs: Any) -> subprocess.CompletedProcess:
        command = [str(token) for token in cmd]
        if command and command[0] == "wl":
            proc = store.runner(command)
            return subprocess.CompletedProcess(
                args=command,
                returncode=proc.returncode,
                stdout=proc.stdout,
                stderr=proc.stderr,
            )
        return subprocess.CompletedProcess(
            args=command, returncode=0, stdout="", stderr=""
        )

    return runner


# ---------------------------------------------------------------------------
# Row model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Invocation:
    """One terminal-outcome exercise of a skill entry point.

    ``run`` executes the production entry point (or the documented transition
    for prose-only skills) against *store*. ``check`` optionally asserts on
    the returned value; the caller always asserts the persisted assignee.
    """

    label: str
    target_id: str
    run: Callable[[FakeWorklogStore, Path], Any]
    check: Callable[[Any, FakeWorklogStore], None] | None = None
    seed_status: str = "in-progress"
    seed_assignee: str = _AGENT


@dataclass(frozen=True)
class ReleaseRow:
    """A skill terminal entry point plus its success and failure exercises."""

    row_id: str
    skill: str
    entry_point: str
    source: str
    entry_points: tuple[str, ...]
    success: Invocation
    failure: Invocation
    classification: str = "must-release-on-terminal"


@dataclass(frozen=True)
class PinnedHold:
    """An intentional hold that the release default must not regress."""

    name: str
    source: str
    entry_points: tuple[str, ...]
    exercise: Callable[[Path], None] = field(compare=False)
    classification: str = "intentional-hold"


@dataclass(frozen=True)
class ReleaseExercise:
    """A release-only entry point exercised through one terminal outcome.

    Such a helper only ever *clears* an assignee (it hard-codes
    ``--assignee ""``), so it cannot introduce a hold. It is still exercised
    here rather than left to the discovery guard's word, and only the outcome
    that can persist a release is asserted: a failed restore write cannot
    clear anything (the helper logs loudly and leaves manual recovery).
    """

    name: str
    source: str
    entry_points: tuple[str, ...]
    exercise: Callable[[FakeWorklogStore], None] = field(compare=False)
    classification: str = "must-release-on-terminal"
    target_id: str = "SA-MATRIXAUDIT"


@dataclass(frozen=True)
class ReadOnlyCaller:
    """A reviewed read-only caller that must never establish a claim.

    ``exercise`` runs the production entry point against the in-memory store
    seeded with a pre-existing producer ``Map`` hold. A read-only caller must
    write no ``--assignee`` at all: it cannot leave a hold behind, and the
    release-by-default must not wrongly clear a hold it never held.
    """

    name: str
    source: str
    exercise: Callable[[FakeWorklogStore, Path], None] = field(compare=False)
    classification: str = "must-not-acquire-assignee"


# ---------------------------------------------------------------------------
# Invocation helpers: intake
# ---------------------------------------------------------------------------


def _run_intake_finish(store: FakeWorklogStore, tmp_path: Path) -> dict:
    with _inject_store_runner(store), patch.object(intake, "_mark_pane_aborted"):
        return intake.cmd_finish("SA-MATRIXINTAKE")


def _run_intake_finish_failure(store: FakeWorklogStore, tmp_path: Path) -> None:
    description = tmp_path / "intake.md"
    description.write_text("draft", encoding="utf-8")

    def explode(_cmd: list[str]) -> dict:
        raise RuntimeError("simulated wl update --description-file failure")

    with (
        _inject_store_runner(store),
        patch.object(intake, "run_wl", side_effect=explode),
        patch.object(intake, "_mark_pane_aborted"),
        pytest.raises(RuntimeError, match="simulated"),
    ):
        intake.cmd_finish("SA-MATRIXINTAKE", description_file=str(description))


def _run_intake_auto_complete(store: FakeWorklogStore, tmp_path: Path) -> dict:
    with _inject_store_runner(store), patch.object(intake, "_mark_pane_aborted"):
        return intake.cmd_auto_complete("SA-MATRIXINTAKE")


def _run_intake_auto_complete_failure(store: FakeWorklogStore, tmp_path: Path) -> None:
    store.fail_next_update("open")
    with (
        _inject_store_runner(store),
        patch.object(intake, "_mark_pane_aborted"),
        pytest.raises(RuntimeError, match="simulated transient"),
    ):
        intake.cmd_auto_complete("SA-MATRIXINTAKE")


def _run_intake_abort(store: FakeWorklogStore, tmp_path: Path) -> dict:
    with _inject_store_runner(store), patch.object(intake, "_mark_pane_aborted"):
        return intake.cmd_abort("SA-MATRIXINTAKE")


def _run_intake_abort_pane_failure(
    store: FakeWorklogStore, tmp_path: Path
) -> None:
    """The abort completes (releasing the item) before a pane update blows up."""
    with (
        _inject_store_runner(store),
        patch.object(
            intake, "_mark_pane_aborted", side_effect=RuntimeError("pane down")
        ),
        pytest.raises(RuntimeError, match="pane down"),
    ):
        intake.cmd_abort("SA-MATRIXINTAKE")


# ---------------------------------------------------------------------------
# Invocation helpers: implement
# ---------------------------------------------------------------------------


def _build_result() -> dict[str, Any]:
    return {"success": True, "stdout": "", "stderr": "", "exit_code": 0}


def _tests_result() -> dict[str, Any]:
    return {
        "success": True,
        "failures": [],
        "skipped": False,
        "tooling": "suite",
        "scope": "changed",
        "notices": [],
    }


def _finish_state(tmp_path: Path, work_item_id: str) -> implement.ImplementState:
    worktree = tmp_path / "worktree"
    worktree.mkdir(exist_ok=True)
    repo_root = tmp_path / "repo-root"
    (repo_root / ".worklog" / "locks").mkdir(parents=True, exist_ok=True)
    return implement.ImplementState(
        work_item_id=work_item_id,
        worktree_path=str(worktree.resolve()),
        repo_root=str(repo_root.resolve()),
        parent_branch="dev",
    )


@contextlib.contextmanager
def _phase_finish_env(
    store: FakeWorklogStore, tmp_path: Path, *, gate_green: bool
):
    """Patch every external boundary of ``phase_finish``; keep its logic real."""
    state = _finish_state(tmp_path, "SA-MATRIXIMPL")
    tests = _tests_result()
    if not gate_green:
        tests.update(
            success=False,
            failures=[{"test": "tests/test_example.py::test_x", "stdout": "boom"}],
        )
    with contextlib.ExitStack() as stack:
        stack.enter_context(
            patch.object(implement, "_discover_worktree", return_value=state.worktree_path)
        )
        stack.enter_context(patch.object(implement, "read_state", return_value=state))
        stack.enter_context(
            patch.object(implement, "_worktree_placement_violation", return_value=None)
        )
        stack.enter_context(
            patch.object(
                implement,
                "_snapshot_main_checkout",
                return_value={"taken_at": "test", "status": {}},
            )
        )
        stack.enter_context(
            patch.object(implement, "_verify_main_checkout", return_value=[])
        )
        stack.enter_context(
            patch.object(implement, "acquire_main_checkout_lock", return_value=(7, None))
        )
        stack.enter_context(patch.object(implement, "_release_main_checkout_lock"))
        stack.enter_context(patch.object(implement, "expected_dirty_paths", return_value=()))
        stack.enter_context(
            patch.object(implement, "run_refactor", return_value={"success": True})
        )
        stack.enter_context(patch.object(implement, "run_build", return_value=_build_result()))
        stack.enter_context(
            patch.object(
                implement, "run_gate_suite", return_value=(tests, {"tree": "tree-1"})
            )
        )
        stack.enter_context(
            patch.object(
                implement,
                "stage_tested_tree",
                return_value={"success": True, "tree": "tree-1", "expected": "tree-1"},
            )
        )
        stack.enter_context(patch.object(implement, "git_commit", return_value=True))
        stack.enter_context(
            patch.object(implement, "git_get_commit_hash", return_value="abc1234")
        )
        stack.enter_context(
            patch.object(
                implement,
                "_fast_forward_local_dev_for_push",
                return_value={"method": "ff", "success": True, "own_churn": []},
            )
        )
        stack.enter_context(
            patch.object(implement, "git_push_to_dev", return_value=True)
        )
        stack.enter_context(
            patch.object(
                implement, "_assert_commit_on_origin_dev", return_value={"success": True}
            )
        )
        stack.enter_context(
            patch.object(
                implement,
                "cleanup_worktree_processes",
                return_value={"method": "test", "terminated": 0, "warning": ""},
            )
        )
        stack.enter_context(patch.object(implement, "remove_state"))
        stack.enter_context(patch.object(implement, "_remove_worktree", return_value=True))
        stack.enter_context(
            patch.object(
                implement,
                "_sync_parent_branch",
                return_value={"method": "skipped", "success": True},
            )
        )
        stack.enter_context(patch.object(implement, "_surface_postfix_prose"))
        stack.enter_context(patch.object(implement, "wl_add_comment", return_value=True))
        stack.enter_context(patch.object(implement, "run_cmd", _fake_run_cmd(store)))
        stack.enter_context(_inject_store_runner(store))
        yield


def _run_phase_finish(store: FakeWorklogStore, tmp_path: Path) -> dict:
    with _phase_finish_env(store, tmp_path, gate_green=True):
        return implement.phase_finish(
            "SA-MATRIXIMPL", json_output=True, no_refactor=True
        )


def _run_phase_finish_failure(store: FakeWorklogStore, tmp_path: Path) -> dict:
    # ``max_retry=0`` lands on the "Tests failed" abort branch, which releases
    # the claim; the JSON-mode per-attempt branch reports and lets the caller
    # loop externally instead (covered by the finish gate's own tests).
    with _phase_finish_env(store, tmp_path, gate_green=False):
        return implement.phase_finish(
            "SA-MATRIXIMPL", json_output=True, no_refactor=True, max_retry=0
        )


def _run_phase_abort(store: FakeWorklogStore, tmp_path: Path) -> dict:
    with (
        patch.object(implement, "_discover_worktree", return_value=None),
        patch.object(implement, "_resolve_main_repo_root", return_value=str(tmp_path)),
        patch.object(implement, "_restore_repo_state"),
        patch.object(implement, "_mark_pane_aborted"),
        patch.object(implement, "wl_add_comment", return_value=True),
        _inject_store_runner(store),
    ):
        return implement.phase_abort("SA-MATRIXIMPL", json_output=True)


def _safety_reset(target_id: str) -> Callable[[FakeWorklogStore, Path], None]:
    """Build an invocation of the signal/error safety reset for *target_id*."""

    def run(store: FakeWorklogStore, tmp_path: Path) -> None:
        with _inject_store_runner(store):
            implement._safety_reset_if_in_progress(target_id)

    return run


class _FakeChildLock:
    """Minimal per-child lock stand-in (already acquired, no-op release)."""

    def __init__(self) -> None:
        self.path = "/tmp/fake-child.lock"

    def acquire(self) -> SimpleNamespace:
        return SimpleNamespace(
            acquired=True,
            reentrant=False,
            reclaimed=False,
            held_by=None,
            path=self.path,
        )

    def release(self) -> None:
        return None


def _run_phase_parent_advance(store: FakeWorklogStore, tmp_path: Path) -> dict:
    parent = store.show("SA-MATRIXPARENT")
    store.add_item(
        {
            "id": "SA-MATRIXCHILD",
            "title": "child",
            "status": "completed",
            "stage": "in_review",
            "assignee": "",
            "parentId": "SA-MATRIXPARENT",
        }
    )
    child = store.show("SA-MATRIXCHILD")
    with (
        patch.object(implement, "is_code_freeze_active", return_value=False),
        patch.object(
            implement,
            "_outbound_gate",
            return_value={"blocked": False, "blockers": [], "coordination_blockers": []},
        ),
        patch.object(implement, "wl_show", return_value=parent),
        patch.object(implement, "wl_show_children", return_value=[child]),
        patch.object(implement, "wl_dep_blockers", return_value=[]),
        patch.object(implement, "wl_add_comment", return_value=True),
        _inject_store_runner(store),
    ):
        return implement.phase_parent("SA-MATRIXPARENT", json_output=True)


def _run_phase_start_failure(store: FakeWorklogStore, tmp_path: Path) -> dict:
    """A start that claims, then cannot fetch the item, releases the claim."""
    with (
        patch.object(implement, "is_code_freeze_active", return_value=False),
        patch.object(implement, "_new_child_lock", return_value=_FakeChildLock()),
        patch.object(implement, "_set_child_lock"),
        patch.object(implement, "_release_child_lock"),
        patch.object(implement, "_register_signal_handlers"),
        patch.object(
            implement,
            "_outbound_gate",
            return_value={"blocked": False, "blockers": [], "coordination_blockers": []},
        ),
        patch.object(implement, "load_implement_extension", return_value=None),
        patch.object(implement, "_surface_extension_prose"),
        patch.object(
            implement,
            "check_orphaned_stashes",
            return_value={
                "has_orphaned": False,
                "orphaned_stashes": [],
                "total_stashes": 0,
                "warning": "",
            },
        ),
        patch.object(implement, "_current_checkout_is_linked_worktree", return_value=True),
        patch.object(implement, "wl_show", return_value={}),
        patch.object(implement, "wl_add_comment", return_value=True),
        _inject_store_runner(store),
    ):
        return implement.phase_start(
            "SA-MATRIXPARENT", json_output=True, force=True
        )


def _run_finalise_stranded_child(store: FakeWorklogStore, tmp_path: Path) -> dict:
    child = store.show("SA-MATRIXCHILD")
    with (
        patch.object(implement, "wl_show", return_value=child),
        patch.object(implement, "wl_add_comment", return_value=True),
        _inject_store_runner(store),
    ):
        return implement._finalise_stranded_child(
            "SA-MATRIXCHILD",
            {
                "stranded": True,
                "commit": "abc1234",
                "evidence": "abc1234",
            },
            assignee=_AGENT,
        )


# ---------------------------------------------------------------------------
# Invocation helpers: audit
# ---------------------------------------------------------------------------


def _audit_context(
    store: FakeWorklogStore,
    *,
    item_id: str,
    verdict: str,
    original_status: str = "in-progress",
) -> audit_runner._AuditContext:
    ctx = audit_runner._AuditContext(
        issue_id=item_id,
        persist=False,
        timeout=None,
        parent_timeout=None,
        pi_bin="pi",
        model=None,
        model_source="matrix-test",
        runner=store.runner,
        json_mode=True,
        debug_log=None,
        force=True,
        worklog_dir=None,
        batch_phase2=False,
        green_run=None,
        audit_children=False,
        max_child_audits=None,
        run_tests=False,
    )
    ctx.audit_completed = True
    ctx.audit_verdict = verdict
    ctx.script_failure = None
    ctx.wi = {"id": item_id, "parentId": None}
    ctx.original_status = original_status
    ctx.original_stage = "plan_complete"
    return ctx


def _run_audit_verdict(store: FakeWorklogStore, tmp_path: Path, verdict: str) -> int:
    ctx = _audit_context(store, item_id="SA-MATRIXAUDIT", verdict=verdict)
    with patch.object(audit_runner, "_remove_debug_log"):
        return audit_runner._apply_terminal_lifecycle(ctx)


def _run_audit_yes(store: FakeWorklogStore, tmp_path: Path) -> int:
    return _run_audit_verdict(store, tmp_path, "yes")


def _run_audit_no(store: FakeWorklogStore, tmp_path: Path) -> int:
    return _run_audit_verdict(store, tmp_path, "no")


# ---------------------------------------------------------------------------
# Invocation helpers: plan (prose-only skill)
# ---------------------------------------------------------------------------


def _plan_claim(store: FakeWorklogStore, item_id: str) -> None:
    """Apply plan's documented claim (``in_progress``, no assignee)."""
    status_lifecycle.StatusLifecycle.update_status(
        item_id, "in_progress", runner=store.runner
    )


def _run_plan_complete(store: FakeWorklogStore, tmp_path: Path) -> None:
    item_id = "SA-MATRIXPLAN"
    _plan_claim(store, item_id)
    status_lifecycle.StatusLifecycle.update_status(
        item_id, "open", stage="plan_complete", runner=store.runner
    )


def _run_plan_abort(store: FakeWorklogStore, tmp_path: Path) -> None:
    item_id = "SA-MATRIXPLAN"
    _plan_claim(store, item_id)
    status_lifecycle.StatusLifecycle.update_status(
        item_id, "open", runner=store.runner
    )


# ---------------------------------------------------------------------------
# The matrix
# ---------------------------------------------------------------------------


def _check_finish_success(report: dict, store: FakeWorklogStore) -> None:
    assert report["success"] is True, report.get("message")
    persisted = store.show("SA-MATRIXIMPL")
    assert persisted["status"] == "completed"
    assert persisted["stage"] == "in_review"


def _check_finish_failure(report: dict, store: FakeWorklogStore) -> None:
    assert report["success"] is False
    assert store.show("SA-MATRIXIMPL")["status"] == "open"


def _check_phase_abort(report: dict, store: FakeWorklogStore) -> None:
    assert report["success"] is True
    assert store.show("SA-MATRIXIMPL")["status"] == "open"


def _check_parent_advance(report: dict, store: FakeWorklogStore) -> None:
    assert report["parent_advanced"] is True, report.get("message")
    persisted = store.show("SA-MATRIXPARENT")
    assert persisted["status"] == "completed"
    assert persisted["stage"] == "in_review"


def _check_phase_start_failure(report: dict, store: FakeWorklogStore) -> None:
    assert report["success"] is False
    assert store.show("SA-MATRIXPARENT")["status"] == "open"


def _check_finalised(result: dict, store: FakeWorklogStore) -> None:
    assert result["finalised"] is True, result.get("reason")
    persisted = store.show("SA-MATRIXCHILD")
    assert persisted["status"] == "completed"
    assert persisted["stage"] == "in_review"


def _check_audit_yes(rc: int, store: FakeWorklogStore) -> None:
    assert rc == 0
    persisted = store.show("SA-MATRIXAUDIT")
    assert persisted["status"] == "completed"
    assert persisted["stage"] == "in_review"
    # The persistent hold is represented by needsProducerReview, never by an
    # assignee — the audit agent's own claim is released.
    assert persisted["needsProducerReview"] is True


def _check_audit_no(rc: int, store: FakeWorklogStore) -> None:
    assert rc == 0
    persisted = store.show("SA-MATRIXAUDIT")
    assert persisted["status"] == "open"
    assert persisted["stage"] == "plan_complete"


RELEASE_ROWS: tuple[ReleaseRow, ...] = (
    ReleaseRow(
        row_id="intake-finish",
        skill="intake",
        entry_point="cmd_finish",
        source="intake/scripts/intake.py",
        entry_points=("cmd_finish",),
        success=Invocation(
            label="finish", target_id="SA-MATRIXINTAKE", run=_run_intake_finish
        ),
        failure=Invocation(
            label="finish-description-error",
            target_id="SA-MATRIXINTAKE",
            run=_run_intake_finish_failure,
        ),
    ),
    ReleaseRow(
        row_id="intake-auto-complete",
        skill="intake",
        entry_point="cmd_auto_complete",
        source="intake/scripts/intake.py",
        entry_points=("cmd_auto_complete",),
        success=Invocation(
            label="auto-complete",
            target_id="SA-MATRIXINTAKE",
            run=_run_intake_auto_complete,
        ),
        failure=Invocation(
            label="auto-complete-terminal-write-failure",
            target_id="SA-MATRIXINTAKE",
            run=_run_intake_auto_complete_failure,
        ),
    ),
    ReleaseRow(
        row_id="intake-abort",
        skill="intake",
        entry_point="cmd_abort",
        source="intake/scripts/intake.py",
        entry_points=("cmd_abort",),
        success=Invocation(
            label="abort", target_id="SA-MATRIXINTAKE", run=_run_intake_abort
        ),
        failure=Invocation(
            label="abort-pane-failure",
            target_id="SA-MATRIXINTAKE",
            run=_run_intake_abort_pane_failure,
        ),
    ),
    ReleaseRow(
        row_id="implement-finish",
        skill="implement",
        entry_point="phase_finish",
        source="implement/scripts/implement.py",
        entry_points=("phase_finish",),
        success=Invocation(
            label="finish",
            target_id="SA-MATRIXIMPL",
            run=_run_phase_finish,
            check=_check_finish_success,
        ),
        failure=Invocation(
            label="finish-gate-failure",
            target_id="SA-MATRIXIMPL",
            run=_run_phase_finish_failure,
            check=_check_finish_failure,
        ),
    ),
    ReleaseRow(
        row_id="implement-abort",
        skill="implement",
        entry_point="phase_abort",
        source="implement/scripts/implement.py",
        entry_points=("phase_abort", "_safety_reset_if_in_progress"),
        success=Invocation(
            label="abort",
            target_id="SA-MATRIXIMPL",
            run=_run_phase_abort,
            check=_check_phase_abort,
        ),
        failure=Invocation(
            label="signal-safety-reset",
            target_id="SA-MATRIXIMPL",
            run=_safety_reset("SA-MATRIXIMPL"),
        ),
    ),
    ReleaseRow(
        row_id="implement-parent",
        skill="implement",
        entry_point="phase_parent / phase_start",
        source="implement/scripts/implement.py",
        entry_points=("phase_parent", "_phase_start_locked"),
        success=Invocation(
            label="parent-advance",
            target_id="SA-MATRIXPARENT",
            run=_run_phase_parent_advance,
            check=_check_parent_advance,
        ),
        failure=Invocation(
            label="start-failure",
            target_id="SA-MATRIXPARENT",
            run=_run_phase_start_failure,
            check=_check_phase_start_failure,
        ),
    ),
    ReleaseRow(
        row_id="implement-drive-finalise",
        skill="implement",
        entry_point="phase_drive / _finalise_stranded_child",
        source="implement/scripts/implement.py",
        entry_points=(
            "phase_drive",
            "_finalise_stranded_child",
            "_release_orphaned_child_claim",
        ),
        success=Invocation(
            label="finalise-stranded-child",
            target_id="SA-MATRIXCHILD",
            run=_run_finalise_stranded_child,
            check=_check_finalised,
        ),
        failure=Invocation(
            label="drive-session-safety-reset",
            target_id="SA-MATRIXCHILD",
            run=_safety_reset("SA-MATRIXCHILD"),
        ),
    ),
    ReleaseRow(
        row_id="audit-terminal-verdict",
        skill="audit",
        entry_point="_apply_terminal_lifecycle",
        source="audit/scripts/audit_runner.py",
        entry_points=("_apply_terminal_lifecycle",),
        success=Invocation(
            label="verdict-yes",
            target_id="SA-MATRIXAUDIT",
            run=_run_audit_yes,
            check=_check_audit_yes,
        ),
        failure=Invocation(
            label="verdict-no",
            target_id="SA-MATRIXAUDIT",
            run=_run_audit_no,
            check=_check_audit_no,
        ),
    ),
    ReleaseRow(
        row_id="plan-claim-to-plan-complete",
        skill="plan",
        entry_point="documented claim -> plan_complete",
        source="plan/SKILL.md",
        entry_points=(),
        success=Invocation(
            label="plan-complete",
            target_id="SA-MATRIXPLAN",
            run=_run_plan_complete,
            # Plan's documented claim carries no assignee, so the row pins
            # that the claim never introduces a hold in the first place.
            seed_assignee="",
        ),
        failure=Invocation(
            label="plan-error-reset",
            target_id="SA-MATRIXPLAN",
            run=_run_plan_abort,
            seed_assignee="",
        ),
    ),
)


# ---------------------------------------------------------------------------
# Matrix tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("row", RELEASE_ROWS, ids=[row.row_id for row in RELEASE_ROWS])
@pytest.mark.parametrize("outcome", ("success", "failure"))
def test_terminal_outcome_releases_assignee(
    row: ReleaseRow, outcome: str, tmp_path: Path
) -> None:
    """Both terminal outcomes persist an empty assignee (read back via ``wl show``)."""
    assert row.classification == "must-release-on-terminal"
    invocation: Invocation = getattr(row, outcome)
    store = FakeWorklogStore.with_item(
        invocation.target_id,
        status=invocation.seed_status,
        assignee=invocation.seed_assignee,
    )

    # Precondition: the item really starts on the seeded hold (read through the
    # same injected runner the assertion uses), so a no-op cannot pass.
    assert store.assignee(invocation.target_id) == invocation.seed_assignee

    result = invocation.run(store, tmp_path)
    if invocation.check is not None:
        invocation.check(result, store)

    assert store.assignee(invocation.target_id) == "", (
        f"{row.row_id}/{invocation.label}: the terminal outcome left the item "
        f"on assignee {store.assignee(invocation.target_id)!r}"
    )
    assert any(
        invocation.target_id in command for command in store.update_commands
    ), f"{row.row_id}/{invocation.label}: no wl update reached the item"
    # The assertion above used ``wl show --json`` through the injected runner.
    assert any(
        "show" in command and invocation.target_id in command
        for command in store.show_commands
    )


# ---------------------------------------------------------------------------
# Pinned intentional holds
# ---------------------------------------------------------------------------


def _pin_producer_creation_assignee(tmp_path: Path) -> None:
    """agent-log-review creates recommendation items on the producer ``Map`` hold."""
    store = FakeWorklogStore.with_item(
        "SA-MATRIXHOLD", status="in-progress", assignee=_AGENT
    )
    recommendations = [
        {
            "title": "Reduce repeated tool preamble",
            "issue_type": "chore",
            "priority": "low",
            "confidence": 0.9,
            "rationale": "Repeated preamble wastes context.",
        }
    ]
    result = agent_log_review.file_recommendations(
        recommendations,
        "SA-MATRIXHOLD",
        wl_runner=store.runner,
        worklog_flags=[],
    )

    assert result["created"], result
    created_id = result["created"][0]["id"]
    assert store.assignee(created_id) == "Map"
    assert store.show(created_id)["status"] == "open"


def _pin_verify_context_reduction_restore(tmp_path: Path) -> None:
    """verify_context_reduction restores a pre-existing (producer) assignee."""
    item_id = "SA-MATRIXHOLD"
    store = FakeWorklogStore.with_item(
        item_id, status="open", stage="plan_complete", assignee="audit-agent"
    )

    def fake_wl(cmd: list[str], timeout: int = 120) -> dict:
        proc = store.runner(cmd)
        if proc.returncode != 0:
            raise RuntimeError(proc.stdout)
        return json.loads(proc.stdout)

    item = verify_context_reduction.AuditItem(
        item_id=item_id,
        pre_status="open",
        pre_stage="plan_complete",
        pre_assignee="Map",
    )
    with patch.object(verify_context_reduction, "_wl", side_effect=fake_wl):
        verify_context_reduction._restore_item_state(item)

    assert store.assignee(item_id) == "Map"


def _pin_needs_producer_review_hold(tmp_path: Path) -> None:
    """A passing top-level audit holds via needsProducerReview, not an assignee."""
    store = FakeWorklogStore.with_item(
        "SA-MATRIXAUDIT", status="in-progress", assignee=_AGENT
    )
    assert _run_audit_yes(store, tmp_path) == 0
    persisted = store.show("SA-MATRIXAUDIT")
    assert persisted["assignee"] == ""
    assert persisted["needsProducerReview"] is True


PINNED_HOLD_ROWS: tuple[PinnedHold, ...] = (
    PinnedHold(
        name="agent-log-review creation assignee (producer Map hold)",
        source="agent-log-review/scripts/agent_log_review.py",
        entry_points=("file_recommendations",),
        exercise=_pin_producer_creation_assignee,
    ),
    PinnedHold(
        name="verify_context_reduction assignee restore",
        source="audit/scripts/verify_context_reduction.py",
        entry_points=("_restore_item_state",),
        exercise=_pin_verify_context_reduction_restore,
    ),
    PinnedHold(
        name="needsProducerReview hold (audit passing verdict)",
        source="audit/scripts/audit_runner.py",
        entry_points=(),
        exercise=_pin_needs_producer_review_hold,
    ),
)


@pytest.mark.parametrize("hold", PINNED_HOLD_ROWS, ids=[hold.name for hold in PINNED_HOLD_ROWS])
def test_intentional_holds_are_preserved(hold: PinnedHold, tmp_path: Path) -> None:
    """Intentional holds must survive the release-by-default behaviour."""
    assert hold.classification == "intentional-hold"
    hold.exercise(tmp_path)


# ---------------------------------------------------------------------------
# Read-only callers: they must never establish a claim in the first place
# ---------------------------------------------------------------------------


def _exercise_find_related(store: FakeWorklogStore, tmp_path: Path) -> None:
    """Run find-related's read-only lifecycle (its work fetch fails fast)."""
    with (
        patch.object(find_related, "_default_repo_path", return_value="."),
        patch.object(find_related, "_wl_flags_for", return_value=[]),
        patch.object(find_related, "fetch_with_probe", return_value=(None, None)),
        patch.object(
            sys, "argv", ["find_related.py", "--work-item-id", "SA-MATRIXRO"]
        ),
        _inject_store_runner(store),
        pytest.raises(SystemExit),
    ):
        find_related._main()


def _exercise_refactor(store: FakeWorklogStore, tmp_path: Path) -> None:
    """Run refactor's read-only lifecycle wrapper (pipeline stubbed out)."""
    args = SimpleNamespace(
        work_item_id="SA-MATRIXRO",
        dry_run=False,
        config=None,
        parent_branch="dev",
        no_linter=True,
        no_llm=True,
    )
    with (
        patch.object(refactor, "refactor_pipeline", return_value={"success": True}),
        _inject_store_runner(store),
    ):
        refactor._build_pipeline_runner(args)()


def _exercise_effort_and_risk(store: FakeWorklogStore, tmp_path: Path) -> None:
    """Run the effort/risk wrapper (orchestrator subprocess stubbed out)."""
    proc = SimpleNamespace(returncode=0, stdout="{}", stderr="")
    with (
        patch.object(
            effort_and_risk_run_skill,
            "wl_show",
            return_value={"workItem": {"status": "open"}},
        ),
        patch.object(effort_and_risk_run_skill.subprocess, "run", return_value=proc),
        patch.object(sys, "argv", ["run_skill.py", "--issue", "SA-MATRIXRO"]),
        _inject_store_runner(store),
    ):
        effort_and_risk_run_skill.main()


def _exercise_tree_coverage(store: FakeWorklogStore, tmp_path: Path) -> None:
    """Advance the stage through tree coverage's shared lifecycle helper."""
    tree_coverage._advance_stage("SA-MATRIXRO", "plan_complete", runner=store.runner)


READ_ONLY_CALLERS: tuple[ReadOnlyCaller, ...] = (
    ReadOnlyCaller(
        name="find-related read-only analysis",
        source="find-related/scripts/find_related.py",
        exercise=_exercise_find_related,
    ),
    ReadOnlyCaller(
        name="refactor read-only analysis",
        source="refactor/scripts/refactor.py",
        exercise=_exercise_refactor,
    ),
    ReadOnlyCaller(
        name="effort-and-risk estimate wrapper",
        source="effort-and-risk/scripts/run_skill.py",
        exercise=_exercise_effort_and_risk,
    ),
    ReadOnlyCaller(
        name="tree-coverage stage advance",
        source="shared/tree_coverage.py",
        exercise=_exercise_tree_coverage,
    ),
)


@pytest.mark.parametrize(
    "case", READ_ONLY_CALLERS, ids=[case.name for case in READ_ONLY_CALLERS]
)
def test_read_only_callers_never_touch_an_assignee(
    case: ReadOnlyCaller, tmp_path: Path
) -> None:
    """A read-only caller writes no assignee and preserves a producer hold."""
    assert case.classification == "must-not-acquire-assignee"
    store = FakeWorklogStore.with_item(
        "SA-MATRIXRO", status="open", stage="plan_complete", assignee="Map"
    )
    assert store.assignee("SA-MATRIXRO") == "Map"

    case.exercise(store, tmp_path)

    assert store.update_commands, "the read-only caller issued no wl update at all"
    offending = [cmd for cmd in store.update_commands if "--assignee" in cmd]
    assert not offending, (
        f"{case.name}: a read-only caller wrote an assignee ({offending[0]}); it "
        "must neither claim nor release a hold"
    )
    assert store.assignee("SA-MATRIXRO") == "Map", (
        f"{case.name}: the read-only caller wrongly cleared the producer hold"
    )


# ---------------------------------------------------------------------------
# Release-only entry points: exercised, never assumed
# ---------------------------------------------------------------------------


def _exercise_audit_pre_audit_restore(store: FakeWorklogStore) -> None:
    """The audit failure restore persists a cleared assignee."""
    ctx = _audit_context(
        store,
        item_id="SA-MATRIXAUDIT",
        verdict="yes",
        original_status="in-progress",
    )
    audit_runner._restore_pre_audit_state_on_failure(ctx)


def _exercise_implement_orphaned_claim_release(store: FakeWorklogStore) -> None:
    """The timeout-kill release persists a cleared assignee (SA-0MVVF75ZA007XNKB).

    Called by ``phase_drive`` and ``phase_parent`` after a kill that cannot
    run the killed session's own exit hook (a drive/spawn timeout is a
    ``SIGKILL``). The helper keeps the status so the finish/finalise recovery
    still applies.

    ``_release_orphaned_child_claim`` writes through the shared
    ``StatusLifecycle`` helper, so the in-memory store's runner must be
    injected into that seam too (``_inject_store_runner``) — otherwise the
    release would target the live worklog instead of the fixture store.
    """
    with (
        _inject_store_runner(store),
        patch.object(implement, "wl_show", side_effect=store.show),
        patch.object(implement, "wl_add_comment", return_value=True),
    ):
        release = implement._release_orphaned_child_claim("SA-MATRIXIMPL")

    assert release["released"] is True, release
    assert store.show("SA-MATRIXIMPL")["status"] == "in-progress"


RELEASE_EXERCISES: tuple[ReleaseExercise, ...] = (
    ReleaseExercise(
        name="audit pre-audit-state restore (failed terminal transition)",
        source="audit/scripts/audit_runner.py",
        entry_points=("_restore_pre_audit_state_on_failure",),
        exercise=_exercise_audit_pre_audit_restore,
    ),
    ReleaseExercise(
        name="implement orphaned-claim release (timeout/signal kill)",
        source="implement/scripts/implement.py",
        entry_points=("_release_orphaned_child_claim",),
        exercise=_exercise_implement_orphaned_claim_release,
        target_id="SA-MATRIXIMPL",
    ),
)


@pytest.mark.parametrize(
    "case", RELEASE_EXERCISES, ids=[case.name for case in RELEASE_EXERCISES]
)
def test_release_only_entry_point_clears_the_assignee(case: ReleaseExercise) -> None:
    """A release-only helper must genuinely persist an empty assignee."""
    assert case.classification == "must-release-on-terminal"
    store = FakeWorklogStore.with_item(case.target_id, status="in-progress")
    assert store.assignee(case.target_id) == _AGENT

    case.exercise(store)

    assert store.assignee(case.target_id) == ""
    assert any(
        case.target_id in command for command in store.update_commands
    ), "the restore never reached the item"


# ---------------------------------------------------------------------------
# Discovery guard: no skill may mutate an assignee outside the matrix
# ---------------------------------------------------------------------------

#: Entry points that mutate an assignee but never *terminalise* an item, with
#: a one-line justification. A claim is released by its terminal counterpart,
#: which the matrix rows exercise.
NON_TERMINAL_ENTRY_POINTS: dict[str, dict[str, str]] = {
    "intake/scripts/intake.py": {
        "build_parser": (
            "argparse definition; it only forwards --assignee to cmd_start "
            "(the claim), which every intake terminal row releases"
        ),
        "main": (
            "CLI dispatcher; forwards --assignee to cmd_start (claim) and "
            "delegates terminal outcomes to cmd_finish/cmd_auto_complete/"
            "cmd_abort, all covered by matrix rows"
        ),
    },
}

#: Entry points whose terminal release is asserted by a skill-local test
#: suite rather than a matrix row, with the covering test named. The claim is
#: verified by ``test_covered_by_skill_tests_claims_are_verified``: the named
#: file must exist and reference the entry point, so a stale or invented
#: pointer fails the guard instead of quietly classifying a gap.
COVERED_BY_SKILL_TESTS: dict[str, dict[str, str]] = {
    "audit/scripts/audit_runner.py": {
        "_restore_work_item_status": (
            "audit SIGTERM/SIGINT restore, covered by "
            "skill/audit/tests/test_audit_runner_abort_recovery.py"
        ),
    },
}

#: Exact ``wl`` flags that set an assignee (prose mention does not count).
_ASSIGNEE_FLAGS = ("--assignee", "--if-assignee")


def _node_mutates_assignee(node: ast.AST) -> bool:
    """Whether *node*'s subtree sets an assignee (CLI flag or keyword)."""
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            if child.value in _ASSIGNEE_FLAGS or child.value.startswith(
                "--assignee="
            ):
                return True
        elif isinstance(child, ast.keyword) and child.arg == "assignee":
            return True
    return False


def discover_assignee_entry_points(
    skill_root: Path = _SKILL_ROOT,
) -> dict[str, set[str]]:
    """Map ``skill/*/scripts/*.py`` files to the functions that set an assignee.

    Only top-level functions and module-level statements are attributed (a
    nested helper's mutation is reported against its enclosing entry point).
    ``skill/shared`` helpers and test modules are out of scope: the guard's
    unit is a *skill script*, which is what a new skill adds.
    """
    discovered: dict[str, set[str]] = {}
    for path in sorted(skill_root.glob("*/scripts/*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        entry_points: set[str] = set()
        for statement in tree.body:
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if _node_mutates_assignee(statement):
                    entry_points.add(statement.name)
            elif _node_mutates_assignee(statement):
                entry_points.add("<module>")
        if entry_points:
            discovered[str(path.relative_to(skill_root))] = entry_points
    return discovered


def _classified_entry_points() -> dict[str, set[str]]:
    """Every entry point the matrix or a pinning test accounts for."""
    classified: dict[str, set[str]] = {}

    def add(source: str, names: tuple[str, ...]) -> None:
        classified.setdefault(source, set()).update(names)

    for row in RELEASE_ROWS:
        if row.entry_points:
            add(row.source, row.entry_points)
    for hold in PINNED_HOLD_ROWS:
        if hold.entry_points:
            add(hold.source, hold.entry_points)
    for case in RELEASE_EXERCISES:
        if case.entry_points:
            add(case.source, case.entry_points)
    for source, reasons in NON_TERMINAL_ENTRY_POINTS.items():
        add(source, tuple(reasons))
    for source, reasons in COVERED_BY_SKILL_TESTS.items():
        add(source, tuple(reasons))
    return classified


def uncovered_entry_points(
    skill_root: Path = _SKILL_ROOT,
) -> dict[str, set[str]]:
    """Return ``{file: {entry point}}`` for assignee mutators with no coverage."""
    classified = _classified_entry_points()
    uncovered: dict[str, set[str]] = {}
    for source, entry_points in discover_assignee_entry_points(skill_root).items():
        missing = entry_points - classified.get(source, set())
        if missing:
            uncovered[source] = missing
    return uncovered


def _uncovered_message(uncovered: dict[str, set[str]]) -> str:
    lines = [
        "Assignee-mutating skill entry point(s) are not covered by the "
        "assignee-release regression matrix:",
    ]
    for source, entry_points in sorted(uncovered.items()):
        for entry_point in sorted(entry_points):
            lines.append(f"  - {source}::{entry_point}")
    lines.append("")
    lines.append(
        "Add a RELEASE_ROWS row (with a success and a failure/abort "
        "exercise) or a RELEASE_EXERCISES entry in "
        "tests/test_assignee_release_all_skills.py, or list the entry "
        "point in PINNED_HOLD_ROWS if the hold is intentional (producer "
        "Map hold / needsProducerReview), or in "
        "NON_TERMINAL_ENTRY_POINTS / COVERED_BY_SKILL_TESTS with a "
        "justification (COVERED_BY_SKILL_TESTS must name an existing "
        "test file that mentions the entry point). A new skill must never "
        "reintroduce a lingering agent assignee unnoticed."
    )
    return "\n".join(lines)


def test_every_assignee_mutating_entry_point_is_classified() -> None:
    """The real skill tree has no unclassified assignee-mutating entry point."""
    uncovered = uncovered_entry_points()
    assert not uncovered, _uncovered_message(uncovered)


def test_covered_by_skill_tests_claims_are_verified() -> None:
    """A "covered elsewhere" claim must name a test that really covers it."""
    for source, claims in COVERED_BY_SKILL_TESTS.items():
        for entry_point, justification in claims.items():
            pointer = re.search(r"skill/[^\s`]+\.py", justification)
            assert pointer is not None, (
                f"{source}::{entry_point}: the justification names no covering "
                f"test file: {justification!r}"
            )
            test_path = REPO_ROOT / pointer.group(0)
            assert test_path.is_file(), (
                f"{source}::{entry_point}: named covering test "
                f"{pointer.group(0)} does not exist"
            )
            assert entry_point in test_path.read_text(encoding="utf-8"), (
                f"{source}::{entry_point}: named covering test "
                f"{pointer.group(0)} never mentions the entry point"
            )


def test_discovery_guard_flags_a_new_unlisted_skill(tmp_path: Path) -> None:
    """A new skill that mutates an assignee fails the guard loudly."""
    scripts = tmp_path / "new-skill" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "new_skill.py").write_text(
        "def finish(item_id):\n"
        "    update(item_id, status='completed', assignee='')\n",
        encoding="utf-8",
    )

    uncovered = uncovered_entry_points(tmp_path)

    assert uncovered == {"new-skill/scripts/new_skill.py": {"finish"}}
    message = _uncovered_message(uncovered)
    assert "new-skill/scripts/new_skill.py::finish" in message
    assert "Add a RELEASE_ROWS row" in message


def test_discovery_guard_ignores_scripts_without_assignee_mutation(
    tmp_path: Path,
) -> None:
    """A skill script that never touches an assignee is not flagged."""
    scripts = tmp_path / "quiet-skill" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "quiet_skill.py").write_text(
        "def finish(item_id):\n    update(item_id, status='completed')\n",
        encoding="utf-8",
    )

    assert uncovered_entry_points(tmp_path) == {}


