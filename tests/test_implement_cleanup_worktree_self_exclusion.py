"""Regression tests: ``implement.py finish`` no longer SIGTERMs itself.

Root cause (SA-0MVJC5KNT0044L2B, discovered-from: SA-0MVF1NKZQ009P3ZG):
``phase_finish`` ran ``cleanup_worktree_processes`` *before* the dev push,
*from inside* the worktree (cwd), so the cleanup signalled the invoking
finish process — directly via ``wl cleanup-worktree``'s self-registration /
process-group kill, and via the pgrep fallback that excluded only
``os.getpid()`` while the wrapper shell the finish was launched from carries
the worktree path in its command line. The finish died mid-workflow, was
safety-reset to ``open`` and the commit was stranded local-only. Hit 3/3
finish invocations in the audit-perf epic.

Coverage:

- AC2 — the invoking process, its process group and its ancestry (wrapper
  shell / exec'd wrapper) are never signalled: ``wl cleanup-worktree`` is
  spawned from a cwd outside the worktree in its own session, and the pgrep
  fallback excludes the invoker's whole process tree via
  ``_pid_is_invoker_related``.
- AC3 — a genuine stray process (own session, unrelated ancestry) in the
  worktree is still reaped by the pgrep fallback.
- AC1 — the finish-time cleanup now runs *after* the push, so a misbehaving
  cleanup cannot pre-empt or strand the push in the first place; a
  structural guard locks the ordering.

The tests use real processes (same-group child, own-session stray) and
hermetic git sandboxes; the live SorraAgents checkout is never touched.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

import pytest

# ---------------------------------------------------------------------------
# Paths / module loading
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
_IMPLEMENT_PY = _REPO_ROOT / "skill" / "implement" / "scripts" / "implement.py"


@pytest.fixture(scope="module")
def implement_mod():
    """Load implement.py as a module for direct helper testing."""
    sys.path.insert(0, str(_REPO_ROOT))
    spec = importlib.util.spec_from_file_location(
        "implement_under_test_cleanup_self_exclusion", _IMPLEMENT_PY
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["implement_under_test_cleanup_self_exclusion"] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_worktree(tmp_path: Path) -> Path:
    """Build a fake managed worktree dir under the canonical layout."""
    wt = tmp_path / ".worklog" / "worktrees" / "wl-SA-TEST0001-slug"
    wt.mkdir(parents=True)
    return wt


def _spawn_same_group_sleeper() -> subprocess.Popen:
    """A child of the test process sharing its process group (the
    wrapper-shell situation: finish's wrapper shares its PGID)."""
    proc = subprocess.Popen(["sleep", "600"])
    assert os.getpgid(proc.pid) == os.getpgrp(), (
        "test precondition: child must share the test's process group"
    )
    return proc


def _spawn_unrelated_stray(worktree_path: Path) -> int:
    """Start a long-lived process UNRELATED to the test process.

    Double-forks so the final process is daemonised: it runs in its own
    session/group and is reparented to init once its intermediate parent
    exits — neither in the invoker's process group nor in its ancestry
    (deterministic; avoids util-linux ``setsid`` fork semantics). Its
    command line contains *worktree_path*, so a real ``pgrep -f <path>``
    matches it, and the final PID is reported back over a pipe.
    """
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:  # intermediate child
        os.close(read_fd)
        try:
            os.setsid()
        except OSError:
            pass
        pid2 = os.fork()
        if pid2 > 0:
            os._exit(0)  # intermediate exits; grandchild reparents to init
        # grandchild: own session, ppid -> init, cmdline contains the path
        os.write(
            write_fd, str(os.getpid()).encode() + b"\n"
        )
        os.close(write_fd)
        os.execvp(
            "python3",
            [
                "python3",
                "-c",
                "import time; time.sleep(600)",
                str(worktree_path),
            ],
        )
        os._exit(1)
    os.close(write_fd)
    os.waitpid(pid, 0)
    data = os.read(read_fd, 64).decode().strip()
    os.close(read_fd)
    return int(data)


def _wait_unrelated(implement_mod, pid: int, timeout: float = 5.0) -> None:
    """Wait until *pid* is fully reparented away from the test process."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not implement_mod._pid_is_invoker_related(pid):
            return
        time.sleep(0.05)
    raise AssertionError(
        f"stray {pid} never became unrelated to the invoker (reparenting failed)"
    )


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# _pid_is_invoker_related — the self/group/ancestry exclusion primitive
# ---------------------------------------------------------------------------


class TestPidIsInvokerRelated:
    def test_self_is_related(self, implement_mod):
        assert implement_mod._pid_is_invoker_related(os.getpid()) is True

    def test_same_process_group_is_related(self, implement_mod):
        child = _spawn_same_group_sleeper()
        try:
            assert implement_mod._pid_is_invoker_related(child.pid) is True
        finally:
            child.terminate()
            child.wait(timeout=10)

    def test_descendant_with_own_session_is_related_via_ancestry(
        self, implement_mod
    ):
        # A direct child of ours that escaped into its own session is still a
        # descendant — the ancestry walk (not just the PGID check) must catch
        # it (this is how git_push_to_dev's start_new_session subprocess
        # behaves).
        child = subprocess.Popen(
            ["python3", "-c", "import os; os.setsid(); import time; time.sleep(600)"]
        )
        try:
            # Wait for the child to call setsid() (Popen returns before the
            # child execs), so the own-session precondition is deterministic.
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                try:
                    if os.getpgid(child.pid) == child.pid:
                        break
                except OSError:
                    break
                time.sleep(0.05)
            assert child.poll() is None
            assert os.getpgid(child.pid) == child.pid, (
                "test precondition: child must have its own session/group"
            )
            assert os.getpgid(child.pid) != os.getpgrp()
            assert implement_mod._pid_is_invoker_related(child.pid) is True
        finally:
            child.terminate()
            child.wait(timeout=10)

    def test_unrelated_stray_is_not_related(self, implement_mod, tmp_path):
        wt = _make_worktree(tmp_path)
        stray = _spawn_unrelated_stray(wt)
        try:
            _wait_unrelated(implement_mod, stray)
            assert _alive(stray)
            assert implement_mod._pid_is_invoker_related(stray) is False
        finally:
            os.kill(stray, 9)
            for _ in range(100):
                if not _alive(stray):
                    break
                time.sleep(0.05)


# ---------------------------------------------------------------------------
# wl cleanup-worktree invocation — spawned from OUTSIDE the worktree, own
# session, and the kill count parses the real JSON key (AC2 guards 1 & 2)
# ---------------------------------------------------------------------------


class TestWlCleanupWorktreeInvocation:
    def test_wl_spawned_from_outside_worktree_in_own_session(
        self, implement_mod, tmp_path, monkeypatch
    ):
        wt = _make_worktree(tmp_path)
        real_run_cmd = implement_mod.run_cmd
        calls: list[tuple[list[str], dict]] = []

        def fake_run_cmd(cmd, **kwargs):
            if cmd[:2] == ["wl", "cleanup-worktree"]:
                call = (list(cmd), dict(kwargs))
                calls.append(call)
                return subprocess.CompletedProcess(
                    cmd,
                    0,
                    stdout=json.dumps({"success": True, "processesKilled": 3}),
                    stderr="",
                )
            # Delegate every other command (e.g. the git probes inside
            # _main_checkout_for) to the real runner.
            return real_run_cmd(cmd, **kwargs)

        monkeypatch.setattr(implement_mod, "run_cmd", fake_run_cmd)

        result = implement_mod.cleanup_worktree_processes(str(wt))

        assert result["method"] == "wl_cleanup_worktree"
        assert result["terminated"] == 3

        _, kwargs = calls[0]
        # Guard 1: cwd must be OUTSIDE the worktree (so the wl CLI does not
        # auto-register the invoking tree's own PID under it).
        cwd = kwargs.get("cwd")
        assert cwd is not None, "wl cleanup-worktree must set cwd"
        cwd_path = Path(cwd).resolve()
        assert cwd_path != Path(str(wt)).resolve()
        assert "worktrees" not in cwd_path.parts, (
            f"wl cwd {cwd} must not be inside a managed worktree"
        )
        # Guard 2: the wl subprocess runs in its own session so a
        # register-then-kill inside it can never reach the caller's group.
        assert kwargs.get("start_new_session") is True

    def test_wl_parse_tolerates_legacy_terminated_key(
        self, implement_mod, tmp_path, monkeypatch
    ):
        wt = _make_worktree(tmp_path)
        real_run_cmd = implement_mod.run_cmd

        def fake_run_cmd(cmd, **kwargs):
            if cmd[:2] == ["wl", "cleanup-worktree"]:
                return subprocess.CompletedProcess(
                    cmd,
                    0,
                    stdout=json.dumps({"success": True, "terminated": 2}),
                    stderr="",
                )
            return real_run_cmd(cmd, **kwargs)

        monkeypatch.setattr(implement_mod, "run_cmd", fake_run_cmd)
        result = implement_mod.cleanup_worktree_processes(str(wt))
        assert result["method"] == "wl_cleanup_worktree"
        assert result["terminated"] == 2


# ---------------------------------------------------------------------------
# _main_checkout_for — a cwd guaranteed to be outside the worktree
# ---------------------------------------------------------------------------


class TestMainCheckoutFor:
    def test_git_derived_main_checkout(self, implement_mod, tmp_path):
        main = tmp_path / "main"
        main.mkdir()
        subprocess.run(["git", "init", "-q", str(main)], check=True)
        wt = main / ".worklog" / "worktrees" / "wl-SA-TEST0001-slug"
        wt.mkdir(parents=True)
        assert implement_mod._main_checkout_for(str(wt)) == str(main.resolve())

    def test_layout_fallback(self, implement_mod, tmp_path):
        wt = _make_worktree(tmp_path)
        # tmp_path is not a git repository -> canonical-layout fallback
        assert implement_mod._main_checkout_for(str(wt)) == str(
            tmp_path.resolve()
        )

    def test_non_worktree_path_falls_back_to_tempdir(self, implement_mod, tmp_path):
        assert (
            implement_mod._main_checkout_for(str(tmp_path / "elsewhere"))
            == tempfile.gettempdir()
        )


# ---------------------------------------------------------------------------
# pgrep fallback — excludes the invoker's tree, reaps genuine strays
# ---------------------------------------------------------------------------


class TestPgrepFallback:
    def test_excludes_self_group_and_wrapper_kills_stray_only(
        self, implement_mod, tmp_path, monkeypatch
    ):
        wt = _make_worktree(tmp_path)
        real_run_cmd = implement_mod.run_cmd
        group_child = _spawn_same_group_sleeper()
        stray = _spawn_unrelated_stray(wt)
        try:
            _wait_unrelated(implement_mod, stray)
            pgrep_out = (
                f"{os.getpid()}\n{group_child.pid}\n{stray}\n"
            )

            def fake_run_cmd(cmd, **kwargs):
                if cmd[:2] == ["wl", "cleanup-worktree"]:
                    return subprocess.CompletedProcess(
                        cmd, 1, stdout="", stderr="command not found"
                    )
                if cmd[0] == "pgrep":
                    return subprocess.CompletedProcess(
                        cmd, 0, stdout=pgrep_out, stderr=""
                    )
                return real_run_cmd(cmd, **kwargs)

            monkeypatch.setattr(implement_mod, "run_cmd", fake_run_cmd)
            monkeypatch.setattr(implement_mod.time, "sleep", lambda _s: None)

            with mock.patch.object(implement_mod.os, "kill") as mock_kill:
                result = implement_mod.cleanup_worktree_processes(str(wt))

            assert result["method"] == "pgrep_fallback"
            killed_pids = {call.args[0] for call in mock_kill.call_args_list}
            # Only the genuine stray is targeted — NOT os.getpid(), NOT the
            # same-process-group sibling (surrogate for the wrapper shell the
            # finish was launched from).
            assert killed_pids == {stray}, killed_pids
            assert result["terminated"] == 1
            # The same-group sibling is genuinely untouched.
            assert group_child.poll() is None
            # The invoker (this process) is still alive and functional.
            assert _alive(os.getpid())
        finally:
            group_child.terminate()
            group_child.wait(timeout=10)
            if _alive(stray):
                os.kill(stray, 9)

    def test_reaps_real_stray_in_worktree(
        self, implement_mod, tmp_path, monkeypatch
    ):
        """AC3: a real unrelated stray whose cmdline references the worktree
        is genuinely killed by the pgrep fallback, while the invoking
        process (the concurrent 'finish') survives."""
        wt = _make_worktree(tmp_path)
        real_run_cmd = implement_mod.run_cmd
        stray = _spawn_unrelated_stray(wt)
        try:
            _wait_unrelated(implement_mod, stray)
            assert _alive(stray)

            def fake_run_cmd(cmd, **kwargs):
                if cmd[:2] == ["wl", "cleanup-worktree"]:
                    return subprocess.CompletedProcess(
                        cmd, 1, stdout="", stderr="command not found"
                    )
                if cmd[0] == "pgrep":
                    # Run the REAL pgrep against the real worktree path.
                    return subprocess.run(
                        cmd, capture_output=True, text=True, check=False
                    )
                return real_run_cmd(cmd, **kwargs)

            monkeypatch.setattr(implement_mod, "run_cmd", fake_run_cmd)
            monkeypatch.setattr(implement_mod.time, "sleep", lambda _s: None)

            result = implement_mod.cleanup_worktree_processes(str(wt))

            assert result["method"] == "pgrep_fallback"
            assert result["terminated"] >= 1, result
            # The real stray is really dead now.
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if not _alive(stray):
                    break
                time.sleep(0.2)
            assert not _alive(stray), "stray was not reaped"
            # The 'finish' (invoker) survives and keeps working.
            assert _alive(os.getpid())
        finally:
            if _alive(stray):
                os.kill(stray, 9)


# ---------------------------------------------------------------------------
# AC1 — finish-time cleanup runs after the push (structural guard)
# ---------------------------------------------------------------------------


class TestFinishOrdering:
    SOURCE = _IMPLEMENT_PY.read_text()

    def test_cleanup_runs_after_push_in_phase_finish(self):
        """The worktree-process cleanup must run post-push, so a misbehaving
        cleanup can never pre-empt the dev push (SA-0MVJC5KNT0044L2B)."""
        finish_start = self.SOURCE.index("def phase_finish(")
        finish_end = self.SOURCE.index("def phase_abort(", finish_start)
        body = self.SOURCE[finish_start:finish_end]
        push_idx = body.index('report["steps"]["push"]')
        cleanup_idx = body.index("cleanup_worktree_processes(worktree_path)")
        assert cleanup_idx > push_idx, (
            "phase_finish must clean up worktree processes AFTER the push; "
            "got cleanup before push"
        )
