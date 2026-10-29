"""Per-child lock integration in ``implement.py`` (SA-0MVQ5AXLF002PAQ3).

The per-work-item flock primitive lives in ``skill/shared/work_item_lock.py``;
this module covers the *integration* of that primitive into the implement
orchestration, at the single choke point ``phase_start`` (parent epic
SA-0MVOABYW00004978 — "prevent duplicate drive child sessions").

AC mapping
----------
- AC1: ``phase_start`` acquires the child lock **before** the work-item claim
  and before any worktree creation/resume; a failed acquire aborts with a
  ``skipped-held`` report (holder identity populated), no claim, no
  status/assignee mutation, no worktree create/remove and no file writes.
- AC2: the lock is held for ``phase_start``'s lifetime and released on normal
  exit, on exception/abort, and on SIGTERM; a SIGTERM between the claim and
  worktree creation releases the lock and leaves an explanatory comment
  naming the interruption (never a claimed item with neither a worktree nor a
  comment).
- AC3: ``_classify_child`` maps a held child to ``skipped-held``;
  ``phase_parent``/``phase_drive`` report ``action: "skipped-held"`` with
  ``held_by: {agent, pid, session_id, started_at}`` and stop cleanly instead
  of spawning a second session.
- AC4: driver re-entrancy is token-based: ``IMPLEMENT_CHILD_LOCK_TOKEN`` is
  propagated through ``_child_session_env``/``_invoke_implement`` and an
  acquire whose token matches the lock file is a re-entrant no-op — never a
  pid-equality heuristic.
- AC5: existing guards are untouched (covered by the existing
  ``test_implement_parent_*``/``test_implement_drive*`` suites; this module
  additionally pins that terminal children win over a stale/held lock).
- AC6: the lock path resolves to ``<worklog>/locks/child-<id>.lock`` — the
  worklog store, never the worktree.

Every test drives the real ``WorkItemLock`` against a throwaway worklog
directory (``_child_lock_dir`` is patched to it) and only stubs the external
boundaries of ``phase_start`` (``wl``, git, signals, state file) — the same
convention as ``test_phase_start_claim.py``.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
_SKILLS_ROOT = REPO_ROOT / "skill"
if str(_SKILLS_ROOT) not in sys.path:
    sys.path.append(str(_SKILLS_ROOT))

from shared.work_item_lock import ENV_LOCK_TOKEN, WorkItemLock, lock_file_path

from skill.implement.scripts import implement

ITEM_ID = "SA-CHILDLOCK"
PARENT_ID = "SA-PARENTLOCK"


# ---------------------------------------------------------------------------
# Hermetic fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _hermetic_lock_state(monkeypatch):
    """Isolate module lock globals and the ambient driver token per test.

    A driven child session exports ``IMPLEMENT_CHILD_LOCK_TOKEN`` (and a fake
    ``PI_SESSION_ID`` in the environment); inheriting either would make the
    lock self-owned in a test that means to observe contention.
    """
    monkeypatch.delenv(ENV_LOCK_TOKEN, raising=False)
    # A driven child session also exports the drive recursion guard; the drive
    # tests here exercise the held-child path, not the recursion refusal.
    monkeypatch.delenv(implement.DRIVE_RECURSION_ENV, raising=False)
    monkeypatch.setattr(implement, "_child_lock_global", None)
    monkeypatch.setattr(implement, "_cleanup_done", False)
    monkeypatch.setattr(implement, "_work_item_id_global", None)
    monkeypatch.setattr(implement, "_worktree_path_global", None)
    monkeypatch.setattr(implement, "_repo_root_global", None)
    yield
    lock = implement._child_lock_global
    if lock is not None:
        lock.release()


@pytest.fixture
def worklog_dir(tmp_path, monkeypatch):
    """A throwaway worklog store; ``_child_lock_dir`` resolves to it."""
    store = tmp_path / ".worklog"
    (store / "locks").mkdir(parents=True)
    monkeypatch.setattr(implement, "_child_lock_dir", lambda _wid: store)
    return store


def _make_lock(store: Path, agent: str = "holder", session: str = "sess-holder"):
    """A real per-child lock bound to the throwaway store."""
    return WorkItemLock(
        ITEM_ID, agent=agent, session_id=session, worklog_dir=store
    )


def _acquired(store: Path, agent: str = "holder", session: str = "sess-holder"):
    """Acquire a real child lock for ``ITEM_ID`` (caller must release)."""
    lock = _make_lock(store, agent, session)
    result = lock.acquire()
    assert result.acquired is True, result
    return lock, result


def _clean_stash_result() -> dict:
    return {
        "total_stashes": 0,
        "orphaned_stashes": [],
        "matched_stashes": [],
        "has_orphaned": False,
        "warning": None,
    }


@contextlib.contextmanager
def _patched_phase_start(
    *,
    lifecycle,
    events: list[str] | None = None,
    worktree_path: str | None = None,
    worktree_add: bool = True,
    comments: list[tuple[str, str]] | None = None,
):
    """Patch every external boundary of ``phase_start``.

    *events* records the observable order of the lock acquisition, the claim,
    the worktree creation and the lock release (AC1). The real
    ``WorkItemLock`` is used against the test's throwaway store.
    """
    events = events if events is not None else []
    comments = comments if comments is not None else []
    wt = worktree_path or str(Path("/tmp/wt").resolve())
    real_factory = implement._new_child_lock

    class _RecordingLock:
        def __init__(self, inner):
            self._inner = inner

        @property
        def token(self):
            return self._inner.token

        def acquire(self):
            events.append("lock-acquire")
            return self._inner.acquire()

        def release(self):
            events.append("lock-release")
            self._inner.release()

    def _factory(work_item_id: str):
        return _RecordingLock(real_factory(work_item_id))

    def _record_worktree_add(*_args, **_kwargs):
        events.append("worktree-add")
        return worktree_add

    configured_claim = lifecycle.update_status.side_effect

    def _record_claim(*args, **kwargs):
        events.append("claim")
        if isinstance(configured_claim, BaseException):
            raise configured_claim
        if callable(configured_claim):
            return configured_claim(*args, **kwargs)
        return None

    lifecycle.update_status.side_effect = _record_claim

    with contextlib.ExitStack() as stack:
        stack.enter_context(
            patch.object(implement, "is_code_freeze_active", return_value=False)
        )
        stack.enter_context(patch.object(implement, "_new_child_lock", _factory))
        stack.enter_context(
            patch.object(implement, "_current_checkout_is_linked_worktree", return_value=False)
        )
        stack.enter_context(
            patch.object(implement, "git_status", return_value="## dev\n")
        )
        stack.enter_context(
            patch.object(implement, "git_has_dirty_files", return_value=False)
        )
        stack.enter_context(
            patch.object(
                implement, "check_orphaned_stashes", return_value=_clean_stash_result()
            )
        )
        stack.enter_context(
            patch.object(
                implement,
                "wl_show",
                return_value={
                    "id": ITEM_ID,
                    "title": "Test item",
                    "risk": "Medium",
                    "effort": "Small",
                },
            )
        )
        stack.enter_context(
            patch.object(
                implement,
                "_check_and_evaluate_risk_effort",
                return_value={
                    "success": True,
                    "estimates_provided": True,
                    "risk": "Medium",
                    "effort": "Small",
                },
            )
        )
        stack.enter_context(
            patch.object(implement, "_get_repo_root", return_value="/tmp/repo")
        )
        stack.enter_context(
            patch.object(
                implement,
                "_sync_parent_branch",
                return_value={"method": "skipped", "success": True},
            )
        )
        stack.enter_context(patch.object(implement, "_is_worktree", return_value=False))
        stack.enter_context(
            patch.object(implement, "git_worktree_add", _record_worktree_add)
        )
        stack.enter_context(
            patch.object(implement, "_ensure_node_modules_symlink", return_value=True)
        )
        stack.enter_context(
            patch.object(implement, "_ensure_nested_node_modules_symlinks", return_value=0)
        )
        stack.enter_context(
            patch.object(implement, "_ensure_submodules", return_value=True)
        )
        stack.enter_context(patch.object(implement, "_store_signal_globals"))
        stack.enter_context(patch.object(implement, "_register_signal_handlers"))
        stack.enter_context(patch.object(implement, "write_state"))
        stack.enter_context(
            patch.object(implement, "wl_add_comment", side_effect=lambda wid, c: comments.append((wid, c)))
        )
        stack.enter_context(patch.object(implement, "StatusLifecycle", lifecycle))
        stack.enter_context(
            patch.object(implement, "worktree_path_for", return_value=wt)
        )
        yield


def _store_snapshot(store: Path) -> dict[str, bytes]:
    """Content snapshot of every file the refused start must not touch."""
    return {
        path.relative_to(store).as_posix(): path.read_bytes()
        for path in sorted(store.rglob("*"))
        if path.is_file()
    }


# ---------------------------------------------------------------------------
# AC1 - acquired before the claim, released afterwards
# ---------------------------------------------------------------------------


def test_phase_start_acquires_the_lock_before_claim_and_releases_it(worklog_dir):
    """The lock is taken before the claim/worktree and released on exit."""
    events: list[str] = []
    lifecycle = MagicMock()
    with _patched_phase_start(lifecycle=lifecycle, events=events):
        report = implement.phase_start(ITEM_ID, json_output=True)

    assert report["success"] is True, report
    assert "lock-acquire" in events and "claim" in events and "worktree-add" in events
    assert events.index("lock-acquire") < events.index("claim"), events
    assert events.index("claim") < events.index("worktree-add"), events
    assert events[-1] == "lock-release", events

    # AC6: path is <worklog>/locks/child-<id>.lock, never inside the worktree.
    path = lock_file_path(ITEM_ID, worklog_dir)
    assert path.is_file()
    assert path == worklog_dir / "locks" / f"child-{ITEM_ID}.lock"
    assert not (Path(report["worktree_path"]) / "locks").exists()

    # Released: an independent acquirer succeeds without manual file cleanup.
    probe = _make_lock(worklog_dir, agent="after-start", session="sess-after")
    result = probe.acquire()
    try:
        assert result.acquired is True
    finally:
        probe.release()


def test_held_child_reports_skipped_held_and_performs_no_mutation(worklog_dir, capsys):
    """A refused start names the holder, exits non-zero and writes nothing."""
    holder, _held = _acquired(worklog_dir, agent="other-driver", session="sess-holder")
    try:
        before = _store_snapshot(worklog_dir)
        lifecycle = MagicMock()
        events: list[str] = []
        comments: list[tuple[str, str]] = []
        with _patched_phase_start(
            lifecycle=lifecycle, events=events, comments=comments
        ):
            report = implement.phase_start(ITEM_ID, json_output=True)
        after = _store_snapshot(worklog_dir)
    finally:
        holder.release()

    # The JSON report is emitted on stdout with the held classification.
    printed = json.loads(capsys.readouterr().out)
    assert printed["success"] is False
    assert printed["action"] == "skipped-held"
    assert printed["worktree_path"] == ""
    assert printed["branch"] == ""
    held_by = printed["held_by"]
    for key in ("agent", "pid", "session_id", "started_at"):
        assert key in held_by, held_by
    assert held_by["agent"] == "other-driver"
    assert held_by["pid"] == os.getpid()
    assert held_by["session_id"] == "sess-holder"
    assert report == printed

    # No claim, no status/assignee mutation, no worktree, no comment.
    lifecycle.update_status.assert_not_called()
    assert "lock-release" in events  # the losing handle never holds; release is a no-op
    assert "claim" not in events
    assert "worktree-add" not in events
    assert comments == []

    # AC1/AC2 of the parent epic: byte-identical store (no lock-metadata rewrite).
    assert after == before


def test_phase_start_releases_the_lock_when_the_claim_fails(worklog_dir):
    """An exception path still releases the lock (never a stale holder)."""
    lifecycle = MagicMock()
    lifecycle.update_status.side_effect = RuntimeError("wl exploded")
    with _patched_phase_start(lifecycle=lifecycle):
        report = implement.phase_start(ITEM_ID, json_output=True)

    assert report["success"] is False
    assert "Failed to claim" in report["message"]
    probe = _make_lock(worklog_dir, agent="after-failure", session="sess-after")
    result = probe.acquire()
    try:
        assert result.acquired is True
    finally:
        probe.release()


# ---------------------------------------------------------------------------
# AC2 - SIGTERM release plus an explanatory comment
# ---------------------------------------------------------------------------


def test_sigterm_cleanup_releases_lock_and_explains_the_interruption(worklog_dir):
    """SIGTERM between claim and worktree creation: released + explained."""
    lock, _ = _acquired(worklog_dir, agent="implement", session="sess-term")
    implement._child_lock_global = lock
    implement._work_item_id_global = ITEM_ID
    implement._worktree_path_global = ""  # the worktree is not created yet
    implement._repo_root_global = "/tmp/repo"

    updates: list[tuple[tuple, dict]] = []
    comments: list[tuple[str, str]] = []
    with (
        patch.object(
            implement.StatusLifecycle,
            "show",
            return_value={"workItem": {"status": "in-progress"}},
        ),
        patch.object(
            implement.StatusLifecycle,
            "update_status",
            side_effect=lambda *a, **k: updates.append((a, k)),
        ),
        patch.object(
            implement,
            "wl_add_comment",
            side_effect=lambda wid, c: comments.append((wid, c)),
        ),
        patch.object(implement, "_restore_repo_state"),
        patch.object(implement, "cleanup_worktree_processes"),
        patch.object(implement, "_remove_worktree"),
    ):
        implement._handle_signal_cleanup(signal.SIGTERM)

    # The item is released back to open (never left claimed).
    assert updates, "the interrupt must reset the work item"
    assert updates[0][0] == (ITEM_ID, "open")
    assert updates[0][1].get("assignee") == ""

    # The interruption is named in a comment — never silence.
    assert comments, "a claimed item interrupted mid-start must be explained"
    item_id, text = comments[0]
    assert item_id == ITEM_ID
    assert signal.Signals(signal.SIGTERM).name in text

    # The lock is released: an independent acquirer takes it immediately.
    assert implement._child_lock_global is None
    probe = _make_lock(worklog_dir, agent="after-term", session="sess-after")
    result = probe.acquire()
    try:
        assert result.acquired is True
    finally:
        probe.release()


def test_signal_handler_runs_cleanup_then_exits():
    """The registered handler delegates to the cleanup body before exiting."""
    with (
        patch.object(implement, "_handle_signal_cleanup") as cleanup,
        patch.object(implement.os, "_exit") as exit_,
    ):
        implement._cleanup_done = False
        implement._signal_handler(signal.SIGTERM, None)

    cleanup.assert_called_once_with(signal.SIGTERM)
    exit_.assert_called_once_with(2)


# ---------------------------------------------------------------------------
# AC3 - skipped-held classification and parent/drive reports
# ---------------------------------------------------------------------------


def test_classify_child_maps_a_held_child_to_skipped_held(worklog_dir):
    """A live holder makes an otherwise startable child ``skipped-held``."""
    holder, _ = _acquired(worklog_dir)
    try:
        assert implement._classify_child(
            {"id": ITEM_ID, "status": "open"}
        ) == "skipped-held"
    finally:
        holder.release()

    # Released: the child is startable again.
    assert implement._classify_child({"id": ITEM_ID, "status": "open"}) == "implement"


def test_classify_child_keeps_terminal_precedence_over_a_stale_lock(worklog_dir):
    """A terminal child is never re-implemented, even while a lock is held.

    A live holder outranks the status-based ``in-progress`` guess (it is a
    fact about a running process); a terminal status outranks both.
    """
    holder, _ = _acquired(worklog_dir)
    try:
        assert implement._classify_child(
            {"id": ITEM_ID, "status": "completed"}
        ) == "skip-terminal"
        assert implement._classify_child(
            {"id": ITEM_ID, "status": "in-progress"}
        ) == "skipped-held"
    finally:
        holder.release()


def test_probe_does_not_treat_the_own_token_chain_as_a_holder(worklog_dir, monkeypatch):
    """AC4: token ownership, never pid equality, decides self-ownership."""
    holder, result = _acquired(worklog_dir)
    try:
        assert implement._probe_child_lock(ITEM_ID) is not None
        monkeypatch.setenv(ENV_LOCK_TOKEN, result.token)
        assert implement._probe_child_lock(ITEM_ID) is None
        monkeypatch.setenv(ENV_LOCK_TOKEN, "a-different-token")
        assert implement._probe_child_lock(ITEM_ID) is not None
    finally:
        holder.release()


def _held_child_report() -> dict:
    return {
        "success": True,
        "action": "skipped-held",
        "skipped_held": True,
        "held_by": {
            "agent": "other-driver",
            "pid": 4242,
            "session_id": "sess-other",
            "started_at": "2026-10-29T20:00:00+00:00",
        },
        "held_children": [ITEM_ID],
        "message": f"Child {ITEM_ID} is held by another implementation session.",
    }


def test_phase_parent_reports_a_held_child_without_starting_anything(worklog_dir):
    """``phase_parent`` stops cleanly on a held child — no second session."""
    child = {"id": ITEM_ID, "title": "Held child", "status": "open", "sortIndex": 1}
    holder, _ = _acquired(worklog_dir, agent="other-driver", session="sess-holder")
    try:
        with (
            patch.object(implement, "is_code_freeze_active", return_value=False),
            patch.object(implement, "wl_show", return_value={"id": PARENT_ID}),
            patch.object(implement, "wl_show_children", return_value=[child]),
            patch.object(implement, "wl_dep_blockers", return_value=[]),
            patch.object(implement, "_invoke_implement") as invoke,
            patch.object(implement, "wl_add_comment") as comment,
        ):
            report = implement.phase_parent(PARENT_ID, json_output=True)
    finally:
        holder.release()

    assert report["success"] is True, report
    assert report["action"] == "skipped-held"
    assert report["held_by"]["agent"] == "other-driver"
    assert report["held_by"]["pid"] == os.getpid()
    assert ITEM_ID in report["held_children"]
    assert report["children"][0]["action"] == "skipped-held"
    assert report["children"][0]["held_by"]["session_id"] == "sess-holder"
    assert report["children_processed"] == [
        {
            "id": ITEM_ID,
            "title": "Held child",
            "action": "skipped-held",
            "status": "open",
            "held_by": report["children"][0]["held_by"],
        }
    ]
    assert report.get("parent_advanced") is not True
    invoke.assert_not_called()
    comment.assert_not_called()


def test_phase_drive_reports_a_held_child_without_spawning(worklog_dir):
    """``phase_drive`` surfaces the held child and spawns no session."""
    calls: list[str] = []

    def parent_runner(action, work_item_id, no_refactor=False, verbose=False):
        calls.append(action)
        return _held_child_report()

    def spawner(child_id, worktree_path, timeout, env, verbose=False):
        calls.append(f"spawn:{child_id}")
        return {"success": True, "returncode": 0, "reason": ""}

    with (
        patch.object(implement, "wl_show", return_value={"id": PARENT_ID}),
        patch.object(
            implement,
            "wl_show_children",
            return_value=[{"id": ITEM_ID, "title": "Held child", "status": "open"}],
        ),
    ):
        report = implement.phase_drive(
            PARENT_ID,
            json_output=True,
            parent_runner=parent_runner,
            spawn_session=spawner,
        )

    assert calls == ["parent"], "a held child must never be spawned"
    assert report["action"] == "skipped-held"
    assert report["skipped_held"] is True
    assert report["held_by"]["agent"] == "other-driver"
    assert report["held_children"] == [ITEM_ID]
    assert report["sessions_spawned"] == 0
    assert report["parent_advanced"] is False


# ---------------------------------------------------------------------------
# AC4 - token propagation and re-entrancy
# ---------------------------------------------------------------------------


def test_child_session_env_propagates_the_lock_token(worklog_dir, monkeypatch):
    """The spawned session inherits the holder's explicit token."""
    monkeypatch.setenv(ENV_LOCK_TOKEN, "driver-token")
    env = implement._child_session_env()
    assert env[ENV_LOCK_TOKEN] == "driver-token"

    # A held lock's own token wins over a stale environment value.
    lock, result = _acquired(worklog_dir)
    try:
        monkeypatch.setenv(ENV_LOCK_TOKEN, "stale-env-token")
        implement._child_lock_global = lock
        env = implement._child_session_env(base={})
        assert env[ENV_LOCK_TOKEN] == result.token
    finally:
        implement._child_lock_global = None
        lock.release()


def test_invoke_implement_passes_the_token_to_the_subprocess(monkeypatch):
    """``_invoke_implement`` exports the token explicitly (never inherited by luck)."""
    monkeypatch.setenv(ENV_LOCK_TOKEN, "driver-token")
    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("env")
        return subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr="")

    with patch.object(implement.subprocess, "run", fake_run):
        assert implement._invoke_implement("start", ITEM_ID) == {}

    assert captured["env"] is not None
    assert captured["env"][ENV_LOCK_TOKEN] == "driver-token"
    assert ENV_LOCK_TOKEN in captured["env"]
    # The rest of the environment is preserved.
    assert "PATH" in captured["env"]


def test_phase_start_reenters_its_own_lock_by_token(worklog_dir, monkeypatch):
    """A driver-held lock does not falsely block the driven child's start."""
    holder, held = _acquired(worklog_dir, agent="drive", session="sess-drive")
    try:
        monkeypatch.setenv(ENV_LOCK_TOKEN, held.token)
        lifecycle = MagicMock()
        events: list[str] = []
        with _patched_phase_start(lifecycle=lifecycle, events=events):
            report = implement.phase_start(ITEM_ID, json_output=True)

        assert report["success"] is True, report
        assert events.index("claim") < events.index("worktree-add"), events

        # The outer holder's lock and recorded ownership are untouched by the
        # re-entrant handle's release.
        metadata = json.loads(lock_file_path(ITEM_ID, worklog_dir).read_text())
        assert metadata == held.owner
        monkeypatch.setenv(ENV_LOCK_TOKEN, "another-token")
        probe = _make_lock(worklog_dir, agent="intruder", session="sess-intruder")
        assert probe.acquire().acquired is False
    finally:
        implement._child_lock_global = None
        holder.release()
