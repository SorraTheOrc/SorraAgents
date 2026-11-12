
# <!-- REFACTOR-SA-0MWD28UYZ00255LX
# smell: unknown
# severity: medium
# description: `subprocess.run` without explicit `check` argument
# -->
"""Tests: contaminated launch checkout isolation / refusal (SA-0MW4ZRLSI005PHL6).

Covers the parent's AC1 (F3 suite executes in a fresh origin/dev worktree),
AC2 (abort when the launch checkout is the owning repo's main checkout and is
ahead of / divergent from origin/dev), AC4 (the contaminated-launch fixture) and
AC5 (clean-launch / worktree-launch behaviour unchanged).

All fixtures are offline and hermetic (``skill/shared/git_sandbox.py``); every
assertion exercises production code (``_launch_checkout_divergence``,
``_create_execution_worktree``, ``_audit_base_freshness`` and the
``_phase_gate`` F3 path) — no re-implementation of the guard or isolation
logic (``skill/shared/test-writing-guidelines.md``).

TRACER BULLET: the AC2/AC1 tests were red before this item's implementation
(the guard and worktree helpers did not exist) and are green afterwards; the
red→green transition is recorded on the work item.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pytest
from audit.scripts import audit_runner
from audit.tests.fixtures.contaminated_launch import (
    build_contaminated_launch_scenario,
)
from shared.git_sandbox import fixture_git_env

# Capture the real AC1/AC2 helpers before the conftest autouse fixture
# neutralises them, so this module can re-enable the real behaviour against
# its offline fixtures.
_REAL_CREATE = audit_runner._create_execution_worktree
_REAL_REMOVE = audit_runner._remove_execution_worktree
_REAL_DIVERGENCE = audit_runner._launch_checkout_divergence


@pytest.fixture(autouse=True)
def _use_real_worktree_helpers():
    """Re-enable the real AC1/AC2 helpers for this module's tests."""
    with (
        mock.patch.object(audit_runner, "_create_execution_worktree", _REAL_CREATE),
        mock.patch.object(audit_runner, "_remove_execution_worktree", _REAL_REMOVE),
        mock.patch.object(audit_runner, "_launch_checkout_divergence", _REAL_DIVERGENCE),
    ):
        yield

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _git_runner(cwd: Path):
    """Real-git runner executing every command inside *cwd* hermetically."""

    def _run(cmd):
        proc = subprocess.run(
            list(cmd), cwd=str(cwd), check=False, text=True,
            capture_output=True, env=fixture_git_env(),
        )
        return SimpleNamespace(
            returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr,
        )

    return _run


def _make_ctx(owning_root: Path, issue_id: str = "WL-CONTAM") -> audit_runner._AuditContext:
    """Minimal ``_AuditContext`` with a real-git runner targeting *owning_root*."""
    ctx = audit_runner._AuditContext(
        issue_id=issue_id, persist=False, timeout=None, parent_timeout=None,
        pi_bin="pi", model=None, model_source="local",
        runner=_git_runner(owning_root), json_mode=False, debug_log=None,
        force=True, worklog_dir=None, batch_phase2=False, green_run=None,
        audit_children=False, max_child_audits=None, run_tests=False,
        owning_root=str(owning_root), description="", work_item={},
    )
    # Mirror ``_phase_gate``'s early publication of the per-audit root so the
    # AC1/AC2 helpers resolve the audited checkout (not the framework cwd).
    ctx.per_audit_project_root = str(owning_root)
    return ctx


@pytest.fixture()
def scenario(tmp_path):
    return build_contaminated_launch_scenario(tmp_path / "contaminated")


# ---------------------------------------------------------------------------
# AC2 — launch-checkout divergence guard
# ---------------------------------------------------------------------------


class TestLaunchCheckoutDivergence:
    """``_launch_checkout_divergence`` flags a main checkout that carries
    un-pushed in-flight commits (ahead of / divergent from origin/dev)."""

    def test_clean_main_checkout_is_not_divergent(self, scenario):
        """A main checkout at origin/dev is not divergent."""
        divergent, ahead, _evidence = audit_runner._launch_checkout_divergence(
            scenario.clean, _git_runner(scenario.clean), scenario.clean,
        )
        assert divergent is False
        assert ahead == []

    def test_dirty_main_checkout_is_divergent(self, scenario):
        """The incident shape: 1 un-pushed commit + 6 behind → divergent."""
        divergent, ahead, evidence = audit_runner._launch_checkout_divergence(
            scenario.dirty, _git_runner(scenario.dirty), scenario.dirty,
        )
        assert divergent is True
        assert scenario.dirty_head in ahead
        assert "ahead of origin/dev" in evidence

    def test_worktree_launch_is_not_divergent(self, scenario):
        """AC5: a feature-worktree launch legitimately sits ahead of
        origin/dev — the guard must NOT fire there (the discriminator is the
        main-checkout path, not 'ahead')."""
        worktree = scenario.clean / ".worklog" / "worktrees" / "wl-X"
        worktree.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "worktree", "add", "--detach", str(worktree), "origin/dev"],
            cwd=str(scenario.dirty), check=True, capture_output=True,
            text=True, env=fixture_git_env(),
        )
        # Move the worktree's HEAD to the un-pushed commit so it IS ahead.
        subprocess.run(
            ["git", "reset", "-q", "--hard", scenario.dirty_head],
            cwd=str(worktree), check=True, capture_output=True, text=True,
            env=fixture_git_env(),
        )
        divergent, ahead, _evidence = audit_runner._launch_checkout_divergence(
            scenario.dirty, _git_runner(scenario.dirty), worktree,
        )
        assert divergent is False
        assert ahead == []

    def test_fail_open_when_origin_dev_unresolvable(self, tmp_path):
        """A repo with no origin/dev never blocks the audit (fail-open)."""
        from shared.git_sandbox import commit_all, init_repo

        repo = init_repo(tmp_path / "solo", sandbox_root=tmp_path)
        (repo / "f.txt").write_text("x\n", encoding="utf-8")
        commit_all(repo, "only commit")
        divergent, ahead, evidence = audit_runner._launch_checkout_divergence(
            repo, _git_runner(repo), repo,
        )
        assert divergent is False
        assert ahead == []
        assert evidence == ""


# ---------------------------------------------------------------------------
# AC1 — fresh origin/dev execution worktree
# ---------------------------------------------------------------------------


class TestExecutionWorktree:
    """``_create_execution_worktree`` builds a detached origin/dev worktree
    with submodules initialised and the shared node_modules symlinked."""

    def test_creates_detached_worktree_at_origin_dev(self, scenario):
        owning = scenario.dirty  # a clone with an origin/dev remote
        worktree, evidence = audit_runner._create_execution_worktree(
            owning, _git_runner(owning), "WL-CONTAM",
        )
        try:
            assert worktree is not None, evidence
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=str(worktree),
                capture_output=True, text=True, env=fixture_git_env(),
            ).stdout.strip()
            # The worktree is detached at origin/dev — NOT the dirty HEAD.
            assert head == scenario.origin_dev
            assert head != scenario.dirty_head
        finally:
            audit_runner._remove_execution_worktree(
                owning, _git_runner(owning), str(worktree) if worktree else None,
            )

    def test_removes_worktree_after_run(self, scenario):
        owning = scenario.dirty
        runner = _git_runner(owning)
        worktree, evidence = audit_runner._create_execution_worktree(
            owning, runner, "WL-CONTAM",
        )
        assert worktree is not None, evidence
        assert worktree.exists()
        audit_runner._remove_execution_worktree(owning, runner, str(worktree))
        assert not worktree.exists()

    def test_submodule_initialised_and_node_modules_absent(self, scenario):
        """The fresh worktree has no node_modules (git-ignored) — the AC1
        toolchain step must symlink it from the owning checkout and initialise
        the tracked submodule."""
        owning = scenario.dirty
        # Simulate the shared node_modules convention (SA-0MSGS763C006SM1B).
        (owning / "node_modules").mkdir()
        (owning / "node_modules" / "marker.txt").write_text("x\n", encoding="utf-8")
        runner = _git_runner(owning)
        worktree, evidence = audit_runner._create_execution_worktree(
            owning, runner, "WL-CONTAM",
        )
        try:
            assert worktree is not None, evidence
            assert (worktree / "node_modules").exists()
            assert (worktree / "node_modules" / "marker.txt").exists()
            assert "node_modules symlinked" in evidence
            # Submodule initialised (tce-the-rising's core/ scenario).
            assert "submodules initialised" in evidence
            assert (worktree / "core").is_dir()
            assert (worktree / "core" / "core.py").is_file()
        finally:
            audit_runner._remove_execution_worktree(
                owning, runner, str(worktree) if worktree else None,
            )

    def test_fail_open_when_origin_unresolvable(self, tmp_path):
        """No origin/dev → no worktree (callers fall back to the launch root)."""
        from shared.git_sandbox import commit_all, init_repo

        repo = init_repo(tmp_path / "solo2", sandbox_root=tmp_path)
        (repo / "f.txt").write_text("x\n", encoding="utf-8")
        commit_all(repo, "only")
        worktree, evidence = audit_runner._create_execution_worktree(
            repo, _git_runner(repo), "WL-X",
        )
        assert worktree is None
        assert "unavailable" in evidence


# ---------------------------------------------------------------------------
# AC2 belt-and-braces — _audit_base_freshness flags divergence too
# ---------------------------------------------------------------------------


class TestAuditBaseFreshnessDivergence:
    """The stale-audit-base guard now also flags ahead/divergent main
    checkouts (AC2), while the behind-only case stays covered."""

    def test_clean_checkout_is_fresh(self, scenario):
        ctx = _make_ctx(scenario.clean)
        fresh, _evidence, missing, head = audit_runner._audit_base_freshness(
            ctx, scenario.delivered_commits, "",
        )
        assert fresh is True
        assert missing == []
        assert head is not None

    def test_dirty_ahead_checkout_is_stale(self, scenario):
        """The dirty clone is behind origin/dev (delivered commits absent)
        AND ahead (an un-pushed commit) — both make it stale."""
        ctx = _make_ctx(scenario.dirty)
        fresh, evidence, missing, _head = audit_runner._audit_base_freshness(
            ctx, scenario.delivered_commits, "",
        )
        assert fresh is False
        assert scenario.dirty_head in missing
        assert "divergence" in evidence.lower()


# ---------------------------------------------------------------------------
# AC1/AC2 integration — _phase_gate aborts BEFORE F3; F3 runs in the worktree
# ---------------------------------------------------------------------------


def _phase_gate_ctx(owning: Path, issue_id: str = "WL-CONTAM"):
    """A ``_phase_gate``-ready context with the heavily-mocked runner surface."""
    ctx = _make_ctx(owning, issue_id=issue_id)
    ctx.force = True
    return ctx


class TestPhaseGateContaminatedLaunch:
    """``_phase_gate`` refuses a divergent main checkout before F3 execution
    (AC2) and otherwise runs F3 in the fresh origin/dev worktree (AC1)."""

    def test_aborts_on_divergent_main_checkout_before_f3(self, scenario, capsys):
        """AC2: the guard fires before ``_run_tests_via_test_skill`` — a
        contaminated suite never executes and no spurious test-failure child
        is triaged."""
        ctx = _phase_gate_ctx(scenario.dirty)
        with (
            mock.patch.object(
                audit_runner, "_verify_launch_context", return_value=None,
            ),
            mock.patch.object(
                audit_runner, "_resolve_owning_project_root",
                return_value=scenario.dirty,
            ),
            mock.patch.object(
                audit_runner, "TARGET_PROJECT_ROOT", scenario.dirty,
            ),
            mock.patch.object(
                audit_runner, "_run_tests_via_test_skill",
            ) as run_mock,
        ):
            rc = audit_runner._phase_gate(ctx)

        assert rc == 1
        run_mock.assert_not_called()
        captured = capsys.readouterr()
        assert "Refusing to audit" in captured.err
        assert "un-pushed in-flight work" in captured.err

    def test_runs_f3_in_execution_worktree_on_cache_miss(self, scenario):
        """AC1: on a cache miss the F3 suite executes in the fresh origin/dev
        worktree, never the launch checkout."""
        ctx = _phase_gate_ctx(scenario.clean)
        # ``run_tests=True`` bypasses the LIVE_REPO_GUARD stand-down (the
        # suite guard is active inside the test process) so the F3 call is
        # reached deterministically.
        ctx.run_tests = True
        captured: dict = {}

        def _fake_run_tests(**kwargs):
            captured["cwd"] = kwargs.get("cwd")
            return {"success": False}

        with (
            mock.patch.object(
                audit_runner, "_verify_launch_context", return_value=None,
            ),
            mock.patch.object(
                audit_runner, "_resolve_owning_project_root",
                return_value=scenario.clean,
            ),
            mock.patch.object(
                audit_runner, "TARGET_PROJECT_ROOT", scenario.clean,
            ),
            mock.patch.object(
                audit_runner, "_auto_green_run_outcome",
                return_value=(
                    None, None, audit_runner._FULL_SUITE_CACHE_MISS,
                ),
            ),
            mock.patch.object(
                audit_runner, "_run_tests_via_test_skill",
                side_effect=_fake_run_tests,
            ),
            mock.patch.object(
                audit_runner, "_write_audit_state_file",
                return_value=str(scenario.root / "state.json"),
            ),
            mock.patch.object(
                audit_runner, "_run_wl",
                return_value={"workItem": {"status": "open", "stage": "open"}},
            ),
            mock.patch.object(
                audit_runner, "_apply_terminal_lifecycle", return_value=0,
            ),
            mock.patch.object(
                audit_runner, "_check_audit_freshness", return_value=None,
            ),
        ):
            try:
                audit_runner._phase_gate(ctx)
            finally:
                # Never leave an execution worktree behind if the test fails
                # before cleanup.
                audit_runner._remove_execution_worktree(
                    scenario.clean, _git_runner(scenario.clean),
                    ctx.execution_worktree or None,
                )

        assert ctx.execution_worktree, "execution worktree was not created"
        assert captured.get("cwd") is not None
        assert Path(captured["cwd"]) == Path(ctx.execution_worktree)
        assert Path(captured["cwd"]) != scenario.clean

    def test_clean_launch_proceeds_unchanged(self, scenario):
        """AC5: a clean launch is not aborted by the divergence guard."""
        ctx = _phase_gate_ctx(scenario.clean)
        with (
            mock.patch.object(
                audit_runner, "_verify_launch_context", return_value=None,
            ),
            mock.patch.object(
                audit_runner, "_resolve_owning_project_root",
                return_value=scenario.clean,
            ),
            mock.patch.object(
                audit_runner, "TARGET_PROJECT_ROOT", scenario.clean,
            ),
            mock.patch.object(
                audit_runner, "_auto_green_run_outcome",
                return_value=(None, None, audit_runner._FULL_SUITE_CACHE_EMPTY),
            ),
            mock.patch.object(
                audit_runner, "_write_audit_state_file",
                return_value=str(scenario.root / "state.json"),
            ),
            mock.patch.object(
                audit_runner, "_run_wl",
                return_value={"workItem": {"status": "open", "stage": "open"}},
            ),
            mock.patch.object(
                audit_runner, "_check_audit_freshness", return_value=None,
            ),
        ):
            try:
                rc = audit_runner._phase_gate(ctx)
            finally:
                audit_runner._remove_execution_worktree(
                    scenario.clean, _git_runner(scenario.clean),
                    ctx.execution_worktree or None,
                )

        assert rc is None
        assert ctx.execution_worktree, "clean launch should still isolate F3"
