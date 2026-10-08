"""Regression tests for the driven-child worktree-placement guard.

Incident (repo ``tce-main-street``, epic ``MS-0MUXW90YE009L4DQ``): a driven
child was spawned with ``cwd`` = its worktree, but prefixed every ``bash`` call
with ``cd <main-checkout> && ...`` and wrote all its files into the main
checkout. ``cwd`` alone does not confine an agent. The parent/``drive`` phase
silently proceeded because it never checked where the child's work landed.

Contract (SA-0MUY9PSRS003V5CM, AC3/AC4):

- ``_child_main_checkout_violation`` detects a non-terminal child whose
  worktree is clean at the parent HEAD while the main checkout is dirty
  outside ``.worklog/``, and names the offending main-checkout paths.
- The guard is precise: a legitimate in-worktree child (worktree has changes),
  a clean main checkout, or ``.worklog/``-only dirt all return ``None``.
- ``phase_parent`` fails closed (``success: False`` + ``placement_violation``)
  instead of returning ``next_child``, so ``phase_drive`` stops.
- ``_invoke_implement(parse_on_failure=True)`` surfaces the parsed failure
  report so the actionable message reaches ``phase_drive``.
- ``_child_session_env`` injects ``IMPLEMENT_WORKTREE_PATH`` for the child.

The real-git fixtures are routed through ``skill/shared/git_sandbox.py`` so a
leaked repository override (``GIT_DIR``/``GIT_WORK_TREE``/``GIT_CONFIG*``)
cannot redirect the git subprocesses away from ``tmp_path``.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest import mock

import pytest
from shared import git_sandbox as gs

_REPO_ROOT = Path(__file__).resolve().parent.parent
_IMPLEMENT_PY = _REPO_ROOT / "skill" / "implement" / "scripts" / "implement.py"
PARENT = "SA-PARENT01"
CHILD = "SA-CHILD01"


@pytest.fixture(scope="module")
def implement_mod():
    """Load the module-under-test (skill/implement/scripts/implement.py)."""
    sys.path.insert(0, str(_REPO_ROOT))
    spec = importlib.util.spec_from_file_location(
        "implement_under_test_placement", _IMPLEMENT_PY
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["implement_under_test_placement"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def worktree_env(tmp_path):
    """A main checkout on ``dev`` plus a linked worktree for :data:`CHILD`."""
    repo_root = gs.init_repo(tmp_path / "main_repo", default_branch="main")
    (repo_root / "README.md").write_text("# Test\n", encoding="utf-8")
    gs.commit_all(repo_root, "Initial commit")
    gs.run_git(repo_root, ["branch", "dev"], check=True)

    worktree_dir = (tmp_path / "wt").resolve()
    gs.add_worktree(repo_root, worktree_dir, f"wl-{CHILD}", base="dev")
    return {"repo_root": repo_root.resolve(), "worktree_dir": worktree_dir}


@pytest.fixture
def scrubbed_env():
    """Scrub repository-overriding git variables for the duration of a test."""
    with mock.patch.dict(os.environ, gs.scrub_repository_overrides(), clear=True):
        yield


# ---------------------------------------------------------------------------
# Unit: _main_checkout_offending_paths
# ---------------------------------------------------------------------------


class TestMainCheckoutOffendingPaths:
    def test_lists_non_worklog_dirty_paths(self, implement_mod, worktree_env,
                                           scrubbed_env):
        repo_root = worktree_env["repo_root"]
        (repo_root / "src_offender.py").write_text("x = 1\n", encoding="utf-8")
        worklog = repo_root / ".worklog"
        worklog.mkdir(exist_ok=True)
        (worklog / "worklog-data.jsonl").write_text("{}\n", encoding="utf-8")

        paths = implement_mod._main_checkout_offending_paths(str(repo_root))

        assert "src_offender.py" in paths
        assert not any(p.startswith(".worklog/") for p in paths)

    def test_clean_checkout_has_no_offending_paths(self, implement_mod,
                                                   worktree_env, scrubbed_env):
        repo_root = worktree_env["repo_root"]
        assert implement_mod._main_checkout_offending_paths(str(repo_root)) == []


# ---------------------------------------------------------------------------
# Unit: _child_main_checkout_violation
# ---------------------------------------------------------------------------


class TestChildMainCheckoutViolation:
    def test_detects_child_write_in_main_checkout(self, implement_mod,
                                                  worktree_env, scrubbed_env):
        """AC3: main dirty + child worktree clean at parent HEAD → violation."""
        repo_root = worktree_env["repo_root"]
        worktree_dir = worktree_env["worktree_dir"]
        (repo_root / "child_wrote_here.py").write_text("x = 1\n", encoding="utf-8")

        violation = implement_mod._child_main_checkout_violation(
            CHILD, str(worktree_dir)
        )

        assert violation is not None
        assert "main checkout" in violation
        assert "worktree" in violation
        assert "child_wrote_here.py" in violation
        assert "cwd" in violation

    def test_legitimate_in_worktree_child_unaffected(self, implement_mod,
                                                     worktree_env, scrubbed_env):
        """AC4: work in the worktree → no violation even with main dirt."""
        repo_root = worktree_env["repo_root"]
        worktree_dir = worktree_env["worktree_dir"]
        (repo_root / "unrelated.py").write_text("x = 1\n", encoding="utf-8")
        (worktree_dir / "child_code.py").write_text("y = 2\n", encoding="utf-8")

        violation = implement_mod._child_main_checkout_violation(
            CHILD, str(worktree_dir)
        )

        assert violation is None

    def test_clean_main_checkout_has_no_violation(self, implement_mod,
                                                  worktree_env, scrubbed_env):
        worktree_dir = worktree_env["worktree_dir"]
        assert implement_mod._child_main_checkout_violation(
            CHILD, str(worktree_dir)
        ) is None

    def test_worklog_only_dirt_has_no_violation(self, implement_mod,
                                                worktree_env, scrubbed_env):
        repo_root = worktree_env["repo_root"]
        worktree_dir = worktree_env["worktree_dir"]
        worklog = repo_root / ".worklog"
        worklog.mkdir(exist_ok=True)
        (worklog / "worklog-data.jsonl").write_text("{}\n", encoding="utf-8")

        assert implement_mod._child_main_checkout_violation(
            CHILD, str(worktree_dir)
        ) is None


# ---------------------------------------------------------------------------
# Integration: phase_parent fails closed
# ---------------------------------------------------------------------------


def _run_phase_parent(mod, children, worktree_path, **kwargs):
    with (
        mock.patch.object(mod, "wl_show",
                          return_value={"id": PARENT, "status": "in-progress"}),
        mock.patch.object(mod, "wl_show_children", return_value=children),
        mock.patch.object(mod, "wl_dep_blockers", return_value=[]),
        mock.patch.object(mod, "_discover_worktree",
                          return_value=worktree_path),
        mock.patch.object(mod, "is_code_freeze_active", return_value=False),
        mock.patch.object(mod, "wl_add_comment", return_value=True),
        mock.patch.object(mod.StatusLifecycle, "update_status",
                          return_value={"success": True}),
    ):
        return mod.phase_parent(PARENT, json_output=True, **kwargs)


def _child(status="in-progress", title="Child"):
    return {"id": CHILD, "title": title, "status": status, "stage": "in_progress"}


class TestPhaseParentPlacementGuard:
    def test_fails_closed_when_child_wrote_to_main(
        self, implement_mod, worktree_env, scrubbed_env,
    ):
        """AC3/AC4: a child that wrote to the main checkout fails the phase."""
        repo_root = worktree_env["repo_root"]
        worktree_dir = worktree_env["worktree_dir"]
        (repo_root / "leaked_to_main.py").write_text("x = 1\n", encoding="utf-8")

        report = _run_phase_parent(
            implement_mod, [_child()], str(worktree_dir)
        )

        assert report["success"] is False
        assert report["placement_violation"] is True
        assert report["next_child"] == CHILD
        assert "leaked_to_main.py" in report["message"]
        assert "main checkout" in report["message"]

    def test_legitimate_in_worktree_child_is_not_flagged(
        self, implement_mod, worktree_env, scrubbed_env,
    ):
        """AC4: a child whose work is in its worktree is unaffected.

        The worktree has changes, so the loop attempts ``finish`` (stubbed);
        the placement guard must not fire.
        """
        worktree_dir = worktree_env["worktree_dir"]
        (worktree_dir / "child_code.py").write_text("y = 1\n", encoding="utf-8")

        with mock.patch.object(
            implement_mod, "_invoke_implement", return_value={"success": True},
        ):
            report = _run_phase_parent(
                implement_mod, [_child()], str(worktree_dir)
            )

        assert report.get("placement_violation") is not True


# ---------------------------------------------------------------------------
# _invoke_implement(parse_on_failure=True) surfaces the failure report
# ---------------------------------------------------------------------------


class TestInvokeImplementParseOnFailure:
    def test_returns_parsed_report_on_nonzero_exit(self, implement_mod):
        payload = {
            "phase": "parent",
            "success": False,
            "message": "Child SA-CHILD01 produced changes in the main checkout",
            "placement_violation": True,
        }

        class _R:
            returncode = 1
            stdout = json.dumps(payload)
            stderr = ""

        with mock.patch.object(implement_mod.subprocess, "run",
                               return_value=_R()):
            result = implement_mod._invoke_implement(
                "parent", PARENT, parse_on_failure=True,
            )

        assert result == payload

    def test_returns_none_on_nonzero_exit_without_parse(self, implement_mod):
        class _R:
            returncode = 1
            stdout = json.dumps({"success": False})
            stderr = ""

        with mock.patch.object(implement_mod.subprocess, "run",
                               return_value=_R()):
            result = implement_mod._invoke_implement("parent", PARENT)

        assert result is None


# ---------------------------------------------------------------------------
# _child_session_env injects the worktree root
# ---------------------------------------------------------------------------


class TestChildSessionEnvWorktree:
    def test_injects_worktree_path(self, implement_mod):
        env = implement_mod._child_session_env(
            {"PATH": "/bin"}, worktree_path="/wt/SA-CHILD01",
        )
        assert env[implement_mod.WORKTREE_ENV] == "/wt/SA-CHILD01"
        assert env[implement_mod.DRIVE_RECURSION_ENV] == "1"

    def test_omits_worktree_path_when_not_given(self, implement_mod):
        env = implement_mod._child_session_env({"PATH": "/bin"})
        assert implement_mod.WORKTREE_ENV not in env


# ---------------------------------------------------------------------------
# End-to-end spawner: the child environment carries the worktree root
# ---------------------------------------------------------------------------


class TestDefaultSpawnerCarriesWorktree:
    def test_spawn_uses_injected_worktree_env(self, implement_mod, tmp_path):
        captured: dict = {}

        def fake_run(cmd, cwd=None, env=None, capture_output=None, text=None,
                     timeout=None, check=None):
            captured.update(cmd=cmd, cwd=cwd, env=env)

            class _R:
                returncode = 0
                stdout = ""
                stderr = ""

            return _R()

        env = implement_mod._child_session_env(
            {"PATH": "/bin"}, worktree_path=str(tmp_path),
        )
        with mock.patch.object(implement_mod, "_resolve_pi_bin",
                               return_value="/usr/bin/pi"), \
                mock.patch.object(implement_mod.subprocess, "run",
                                  side_effect=fake_run):
            result = implement_mod._default_session_spawner(
                CHILD, str(tmp_path), 120, env,
            )

        assert result["success"] is True
        assert captured["env"][implement_mod.WORKTREE_ENV] == str(tmp_path)
        assert captured["cwd"] == str(tmp_path)
