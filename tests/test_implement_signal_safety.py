"""Signal and timeout kill paths release the assignee (SA-0MVVF75ZA007XNKB).

Parent epic: SA-0MVVEDK2Q008NDUH — *release the assignee on every terminal
outcome*. A non-empty ``assignee`` is a scheduling hold (``AGENTS_GLOBAL.md``),
so a killed session that only reclaimed the *status* would still leave the item
invisible to ``wl next`` and block re-dispatch. Three outcomes never reach a
normal ``StatusLifecycle`` exit and therefore need an explicit durable release:

1. **SIGTERM / SIGINT.** ``_signal_handler`` → ``_handle_signal_cleanup`` →
   ``_safety_reset_if_in_progress``. Asserted here **across a real process
   boundary**: a real ``implement.py start`` subprocess is killed with a real
   signal and the persisted assignee is read back from a real ``wl`` store.
2. **A timeout-killed child session.** ``drive``'s per-child timeout is a
   ``SIGKILL`` (``subprocess.run(timeout=…)``), so the killed session can never
   run an exit hook. The surviving driver persists the release with
   ``_release_orphaned_child_claim``.
3. **An unexpected exception** in ``phase_start`` / ``phase_finish`` — a
   non-classified crash must not leave a claimed item behind.

Everything here runs against a throwaway git repository with its own
initialised worklog store (prefix ``QZ``, deliberately *not* ``SA`` so the
shared resolver cannot match this repository's real store). The live
SorraAgents checkout is never touched.

Residual window (documented, not silently ignored): a process killed by
``SIGKILL`` with **no surviving supervisor** cannot write anything, so its
claim survives until an operator clears it. Every in-tree kill path now has a
surviving writer (the signal handler, or ``drive``/``phase_parent``), so the
remaining case is scoped out in the parent item together with the TTL-based
stale-hold reaper (precedent: SA-0MVKWV2V2006JC8E).
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_IMPLEMENT_PY = _REPO_ROOT / "skill" / "implement" / "scripts" / "implement.py"
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from shared.git_sandbox import fixture_git_env

from skill.implement.scripts import implement
from tests.test_assignee_release_all_skills import (
    FakeWorklogStore,
    _phase_finish_env,
)

REAL_GIT = shutil.which("git")
START_TIMEOUT = 300
READY_TIMEOUT = 120


@pytest.fixture(autouse=True)
def _neutralise_ambient_drive_guard(monkeypatch):
    """Make the module hermetic inside a driven session.

    ``implement.py finish`` runs the changed-scope suite with
    ``IMPLEMENT_DRIVE_ACTIVE=1`` exported by the driving parent; every
    ``phase_drive`` test would otherwise hit the recursion guard
    (SA-0MV8HEQ4O009Y3UB).
    """
    monkeypatch.delenv("IMPLEMENT_DRIVE_ACTIVE", raising=False)


# ---------------------------------------------------------------------------
# Throwaway project fixture (real git repo + real wl store)
# ---------------------------------------------------------------------------

requires_tooling = pytest.mark.skipif(
    REAL_GIT is None or shutil.which("wl") is None,
    reason="requires git and wl on PATH to build a real fixture project",
)


@dataclass(frozen=True)
class Project:
    """A throwaway git repository with its own initialised worklog store."""

    root: Path
    worklog_dir: Path


def _run(cmd: list[str], cwd: Path, *, timeout: int = 120) -> subprocess.CompletedProcess:
    """Run *cmd* in *cwd* with a hermetic git environment."""
    proc = subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
        env=fixture_git_env(),
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"fixture command failed ({proc.returncode}): {' '.join(cmd)}\n"
            f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )
    return proc


def _make_project(base: Path) -> Project:
    """Create a clean git repo on ``dev`` with an initialised ``QZ`` store."""
    root = base / "repo"
    root.mkdir(parents=True)

    _run([REAL_GIT, "init", "-b", "dev"], root)
    _run([REAL_GIT, "config", "user.email", "signal-safety@example.com"], root)
    _run([REAL_GIT, "config", "user.name", "signal safety test"], root)
    (root / "README.md").write_text("signal-safety fixture\n", encoding="utf-8")
    _run([REAL_GIT, "add", "README.md"], root)
    _run([REAL_GIT, "commit", "-m", "fixture: initial commit"], root)

    worklog_dir = root / ".worklog"
    _run(
        [
            "wl", "init",
            "--worklog-dir", str(worklog_dir),
            "--prefix", "QZ",
            "--project-name", "signal safety fixture",
            "--auto-export", "no",
            "--auto-sync", "no",
            "--agents-template", "skip",
            "--json",
        ],
        root,
    )
    # ``wl init`` writes AGENTS.md / .gitignore / .githooks; commit them so the
    # main-checkout dirty-tree gate sees a clean tree.
    _run([REAL_GIT, "add", "-A"], root)
    _run([REAL_GIT, "commit", "-m", "fixture: worklog init"], root)
    return Project(root=root, worklog_dir=worklog_dir)


def _create_item(project: Project, title: str, *, extra: list[str] | None = None) -> str:
    """Create one work item in the fixture store and return its id."""
    proc = _run(
        [
            "wl", "--worklog-dir", str(project.worklog_dir), "create",
            "--title", title,
            "--description", "Signal/timeout assignee-release regression fixture.",
            *(extra or []),
            "--json",
        ],
        project.root,
    )
    return json.loads(proc.stdout)["workItem"]["id"]


def _show_item(project: Project, item_id: str) -> dict:
    """Fetch the fixture work item (with comments) from the store."""
    proc = _run(
        ["wl", "--worklog-dir", str(project.worklog_dir), "show", item_id, "--json"],
        project.root,
    )
    return json.loads(proc.stdout)


# ---------------------------------------------------------------------------
# 1. Real SIGTERM / SIGINT killed subprocess
# ---------------------------------------------------------------------------

#: A ``git`` shim that blocks ``git worktree add`` — the first long-running step
#: *after* ``phase_start`` claimed the item and registered the signal handlers —
#: until the test releases it, so the signal provably arrives while the claim
#: is live. Every other ``git`` invocation passes straight through.
_GIT_SHIM = '''#!/usr/bin/env python3
"""Test-only git shim (SA-0MVVF75ZA007XNKB): hold ``worktree add`` open."""
import os
import sys
import time

args = sys.argv[1:]
if args[:2] == ["worktree", "add"]:
    with open(os.environ["SIGNAL_TEST_READY_FILE"], "w", encoding="utf-8") as fh:
        fh.write("blocked\\n")
    release = os.environ["SIGNAL_TEST_RELEASE_FILE"]
    deadline = time.time() + 60
    while time.time() < deadline:
        if os.path.exists(release):
            # The run was interrupted: never create the worktree.
            os._exit(1)
        time.sleep(0.01)
    os._exit(1)

real_git = os.environ["SIGNAL_TEST_REAL_GIT"]
os.execv(real_git, [real_git, *args])
'''


def _spawn_start(project: Project, item_id: str, shim_dir: Path, ready: Path, release: Path):
    """Spawn a real ``implement.py start <id> --json`` with the gated shim."""
    env = fixture_git_env()
    env.pop("IMPLEMENT_CHILD_LOCK_TOKEN", None)
    env.pop("IMPLEMENT_DRIVE_ACTIVE", None)
    env.pop("PYTEST_CURRENT_TEST", None)
    env["PATH"] = f"{shim_dir}{os.pathsep}{env.get('PATH', '')}"
    env["SIGNAL_TEST_READY_FILE"] = str(ready)
    env["SIGNAL_TEST_RELEASE_FILE"] = str(release)
    env["SIGNAL_TEST_REAL_GIT"] = str(REAL_GIT)
    return subprocess.Popen(
        [sys.executable, str(_IMPLEMENT_PY), "start", item_id, "--json"],
        cwd=str(project.root),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _wait_for_ready(path: Path, proc: subprocess.Popen, timeout: int = READY_TIMEOUT) -> None:
    """Wait (no fixed sleep) until *proc* signals it reached the gated step."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        if proc.poll() is not None:
            stdout, stderr = proc.communicate()
            pytest.fail(
                f"implement.py start exited ({proc.returncode}) before reaching "
                f"the gated worktree step\nstdout:\n{stdout}\n"
                f"stderr:\n{stderr[-4000:]}"
            )
        time.sleep(0.02)
    proc.kill()
    stdout, stderr = proc.communicate()
    pytest.fail(
        f"implement.py start never reached the gated step within {timeout}s\n"
        f"stdout:\n{stdout}\nstderr:\n{stderr[-4000:]}"
    )


@requires_tooling
@pytest.mark.parametrize(
    "signum", (signal.SIGTERM, signal.SIGINT), ids=("SIGTERM", "SIGINT")
)
def test_signal_kill_releases_the_assignee(signum, tmp_path: Path) -> None:
    """A real signal-killed ``start`` leaves status ``open`` and assignee ``""``.

    The item is seeded on a **stale agent hold** (``assignee="implement"``) to
    model the highest-frequency real-world shape: a previous session died
    holding the claim. ``phase_start``'s claim is status-only, so the hold is
    still live when the signal arrives — only the safety reset can release it.
    """
    project = _make_project(tmp_path)
    item_id = _create_item(
        project,
        "Signal-killed start fixture",
        extra=["--assignee", "implement", "--stage", "intake_complete"],
    )

    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    shim = shim_dir / "git"
    shim.write_text(_GIT_SHIM, encoding="utf-8")
    shim.chmod(0o755)
    ready = tmp_path / "ready"
    release = tmp_path / "release"

    proc = _spawn_start(project, item_id, shim_dir, ready, release)
    try:
        _wait_for_ready(ready, proc)

        # Precondition: the claim really is live and still holds the assignee,
        # so a no-op reset cannot pass the assertions below.
        claimed = _show_item(project, item_id)["workItem"]
        assert claimed["status"] == "in-progress"
        assert claimed["assignee"] == "implement"

        proc.send_signal(signum)
        try:
            proc.communicate(timeout=START_TIMEOUT)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
            pytest.fail(
                f"implement.py start survived {signal.Signals(signum).name}\n"
                f"stdout:\n{stdout}\nstderr:\n{stderr[-4000:]}"
            )
    finally:
        release.touch()  # let the gated shim exit

    assert proc.returncode == 2, (
        f"{signal.Signals(signum).name} must exit via the deterministic "
        f"cleanup path (os._exit(2)); got {proc.returncode}"
    )

    persisted = _show_item(project, item_id)["workItem"]
    assert persisted["status"] == "open", persisted
    assert persisted["assignee"] == "", (
        f"{signal.Signals(signum).name} left the item on assignee "
        f"{persisted['assignee']!r}: the safety reset did not release the claim"
    )
    assert not (project.root / ".worklog" / "worktrees").exists() or not any(
        (project.root / ".worklog" / "worktrees").iterdir()
    ), "a killed start must not leave a worktree behind"


# ---------------------------------------------------------------------------
# 2. Timeout-killed child session inside phase_drive
# ---------------------------------------------------------------------------

#: A fake ``pi`` binary that never finishes, so the drive timeout bound really
#: kills it (a real ``subprocess.TimeoutExpired`` → real ``SIGKILL``).
_SLEEPING_PI = '''#!/usr/bin/env python3
import time
time.sleep(120)
'''


@requires_tooling
def test_timeout_killed_child_releases_the_assignee(
    tmp_path: Path, monkeypatch
) -> None:
    """``drive``'s timeout kill leaves the child on ``assignee == ""``.

    The child is seeded ``in-progress`` holding ``implement`` — exactly the
    state a killed ``finish`` leaves behind, since a ``SIGKILL`` cannot run the
    killed session's own exit hook. The surviving driver must persist the
    release itself.
    """
    project = _make_project(tmp_path)
    parent_id = _create_item(project, "Timeout-kill parent fixture")
    child_id = _create_item(
        project,
        "Timeout-killed child fixture",
        extra=["--parent", parent_id],
    )
    _run(
        [
            "wl", "--worklog-dir", str(project.worklog_dir), "update", child_id,
            "--status", "in-progress",
            "--stage", "in_progress",
            "--assignee", "implement",
            "--json",
        ],
        project.root,
    )

    child_worktree = project.root / ".worklog" / "worktrees" / f"wl-{child_id}-x"
    child_worktree.mkdir(parents=True)

    fake_pi = tmp_path / "fake-pi"
    fake_pi.write_text(_SLEEPING_PI, encoding="utf-8")
    fake_pi.chmod(0o755)

    monkeypatch.setenv("IMPLEMENT_DRIVE_PI_BIN", str(fake_pi))
    monkeypatch.chdir(project.root)  # so the real ``wl`` resolves the fixture store

    reports = [
        {
            "phase": "parent",
            "success": True,
            "next_child": child_id,
            "worktree_path": str(child_worktree),
            "children_processed": [],
        },
        {"phase": "parent", "success": True, "parent_advanced": True},
    ]

    def parent_runner(action, work_item_id, no_refactor=False, verbose=False):
        return reports.pop(0)

    report = implement.phase_drive(
        parent_id,
        json_output=False,
        parent_runner=parent_runner,
        child_timeout=1,
        commit_evidence=lambda _cid: None,
    )

    # The failure itself is never masked: the chain still stops.
    assert report["success"] is False
    assert report["failed_child"] == child_id
    entry = report["children_driven"][0]
    assert entry["timed_out"] is True, entry
    assert entry["result"] == "failed", entry
    assert entry["claim_release"]["released"] is True, entry

    persisted = _show_item(project, child_id)["workItem"]
    assert persisted["assignee"] == "", (
        "the timeout kill left the child on a scheduling hold: "
        f"{persisted['assignee']!r}"
    )
    assert persisted["status"] == "in-progress", (
        "the release must not disturb the status: the existing "
        f"finish/finalise recovery still applies (got {persisted['status']!r})"
    )
    comments = " ".join(
        comment["comment"] for comment in _show_item(project, child_id)["comments"]
    )
    assert "Claim released" in comments, comments


# ---------------------------------------------------------------------------
# 3. Unexpected exceptions in phase_start / phase_finish
# ---------------------------------------------------------------------------


class _FakeChildLock:
    """Minimal per-child lock stand-in (already acquired, no-op release)."""

    def __init__(self) -> None:
        self.path = "/tmp/fake-child.lock"

    def acquire(self):
        return mock.Mock(
            acquired=True, reentrant=False, reclaimed=False, held_by=None,
            path=self.path,
        )

    def release(self) -> None:
        return None


def test_phase_start_unexpected_exception_releases_the_claim() -> None:
    """An unclassified crash during ``start`` releases the claim it wrote."""
    updates: list[dict] = []

    def _update(work_item_id, status, stage=None, assignee=None, **kwargs):
        updates.append(
            {"id": work_item_id, "status": status, "assignee": assignee}
        )
        return {"success": True}

    with (
        mock.patch.object(implement, "is_code_freeze_active", return_value=False),
        mock.patch.object(implement, "_new_child_lock", return_value=_FakeChildLock()),
        mock.patch.object(implement, "_set_child_lock"),
        mock.patch.object(implement, "_release_child_lock"),
        mock.patch.object(implement, "_register_signal_handlers"),
        mock.patch.object(
            implement, "_phase_start_locked", side_effect=OSError("disk full")
        ),
        mock.patch.object(
            implement.StatusLifecycle,
            "show",
            return_value={"workItem": {"status": "in-progress", "assignee": "implement"}},
        ),
        mock.patch.object(
            implement.StatusLifecycle, "update_status", side_effect=_update
        ),
    ):
        report = implement.phase_start("SA-SIGSTART", json_output=True)

    assert report["success"] is False
    assert "OSError" in report["unexpected_error"]
    assert updates[-1] == {"id": "SA-SIGSTART", "status": "open", "assignee": ""}, (
        "an unexpected start failure must release the claim it wrote"
    )


def test_phase_finish_unexpected_exception_releases_the_assignee(
    tmp_path: Path,
) -> None:
    """A crash inside ``finish``'s critical section releases the claim.

    ``StatusLifecycle.__exit__`` restores the *original* status on an
    exception — ``in-progress`` for a finishing item — so without the explicit
    release the claim would survive the crash on its scheduling hold.
    """
    store = FakeWorklogStore.with_item(
        "SA-MATRIXIMPL", status="in-progress", assignee="implement"
    )
    assert store.assignee("SA-MATRIXIMPL") == "implement"

    with (
        _phase_finish_env(store, tmp_path, gate_green=True),
        mock.patch.object(
            implement, "stage_tested_tree", side_effect=OSError("disk full")
        ),
    ):
        report = implement.phase_finish(
            "SA-MATRIXIMPL", json_output=True, no_refactor=True
        )

    assert report["success"] is False
    assert "OSError" in report["unexpected_error"]
    persisted = store.show("SA-MATRIXIMPL")
    assert persisted["assignee"] == "", (
        "an unexpected finish failure left the item on a scheduling hold"
    )
    assert persisted["status"] == "open", persisted


# ---------------------------------------------------------------------------
# 4. phase_parent: a timed-out finish subprocess releases the child claim
# ---------------------------------------------------------------------------


def test_invoke_implement_timeout_returns_none() -> None:
    """A finish subprocess that overruns its bound reports a failure, not a crash."""
    with (
        mock.patch.object(implement, "_get_repo_root", return_value="/tmp"),
        mock.patch.object(implement, "_child_lock_token", return_value=""),
        mock.patch.object(
            implement,
            "subprocess",
            mock.Mock(
                run=mock.Mock(
                    side_effect=subprocess.TimeoutExpired(cmd="finish", timeout=600)
                )
            ),
        ),
    ):
        assert implement._invoke_implement("finish", "SA-SIGCHILD") is None


def test_phase_parent_releases_a_child_claim_after_a_finish_timeout() -> None:
    """``phase_parent`` releases the claim its killed ``finish`` left behind."""
    parent_id = "SA-SIGPARENT"
    child_id = "SA-SIGCHILD"
    updates: list[dict] = []
    child = {
        "id": child_id,
        "title": "child",
        "status": "in-progress",
        "stage": "in_progress",
        "assignee": "implement",
        "parentId": parent_id,
    }

    def _show(work_item_id):
        if work_item_id == parent_id:
            return {"id": parent_id, "status": "in-progress", "children": []}
        if work_item_id == child_id:
            return dict(child)
        return {}

    def _update(work_item_id, status, stage=None, assignee=None, **kwargs):
        updates.append({"id": work_item_id, "status": status, "assignee": assignee})
        return {"success": True}

    with (
        mock.patch.object(implement, "is_code_freeze_active", return_value=False),
        mock.patch.object(
            implement,
            "_outbound_gate",
            return_value={"blocked": False, "blockers": [], "coordination_blockers": []},
        ),
        mock.patch.object(implement, "wl_show", side_effect=_show),
        mock.patch.object(implement, "wl_show_children", return_value=[dict(child)]),
        mock.patch.object(implement, "wl_dep_blockers", return_value=[]),
        mock.patch.object(implement, "wl_add_comment", return_value=True),
        mock.patch.object(implement, "_probe_child_lock", return_value=None),
        mock.patch.object(implement, "_discover_worktree", return_value="/tmp/wt"),
        mock.patch.object(implement, "_has_worktree_changes", return_value=True),
        mock.patch.object(implement, "_invoke_implement", return_value=None),
        mock.patch.object(
            implement.StatusLifecycle,
            "update_status",
            side_effect=_update,
            create=True,
        ),
    ):
        report = implement.phase_parent(parent_id, json_output=True)

    assert report["success"] is False
    assert report["finishing_failed"] is True
    assert report["next_child"] == child_id
    assert report["orphaned_claim_release"]["released"] is True
    assert {
        "id": child_id,
        "status": "in-progress",
        "assignee": "",
    } in updates, updates
