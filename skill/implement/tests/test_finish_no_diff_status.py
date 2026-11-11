
# <!-- REFACTOR-SA-0MW9417IK002J6OM
# smell: unknown
# severity: medium
# description: `__enter__` methods in classes like `FakeGitBoundary` usually return `self` at runtime
# -->
"""Regression tests: ``phase_finish`` terminal status on the no-op-push path.

The no-diff/no-op-push path (a verification-only item whose worktree branch
has no diff from ``origin/dev``) must persist the terminal transition
(``status=completed``, ``stage=in_review``) exactly like the push-with-diff
path — and, once the store read-back assertion lands, ``finish`` must fail
loudly instead of reporting success when the store does not hold the
terminal transition (OSL-0MVME1PI5005WU58 / OSL-0MVNZPQD4004567J,
OSL-0MVNZPQJZ000EE83).

The tests exercise ``phase_finish`` with its external boundaries (git, the
``wl``/``StatusLifecycle`` store, build/test runners, worktree removal)
patched, following the stub/runner style of
``test_phase_start_claim.py``: observable assertions only — the recorded
lifecycle calls, the volunteered store read-back in the report, and the
returned report.

Harness (SA-0MW74IGEK003IQ2J)
-----------------------------

This file also hosts the reusable test harness that later slices of the
finish commit-vs-no-op work build on:

* :class:`FakeGitBoundary` — an outcome-aware fake for ``run_cmd`` that
  drives the **real** ``git_commit`` gate in either ``"commit"`` mode (a
  staged diff exists, ``git commit`` runs, :attr:`commit_invoked` is
  ``True``) or ``"clean"`` mode (no staged diff, ``git commit`` never runs,
  :attr:`commit_invoked` is ``False``).  This lets a test pin a real commit
  against a no-op finish (AC1).
* :func:`assert_report_wording` — asserts the completion-comment / report
  wording: the created hash on the commit path, "no changes / no commit" on
  the no-op path (AC3).
* ``conftest.build_child_worktree_fixture`` — builds a child dict plus a
  matching ``.implement_state.json`` worktree file for ``_classify_child`` /
  ``phase_parent`` fixture slices (AC4).
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) in sys.path:
    sys.path.remove(str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT))

from skill.implement.scripts import implement

#: Stable full commit hash the fake git resolves every revision to.
#: ``phase_finish`` fast-forwards local ``dev`` before the push and then
#: asserts that the pushed commit is an ancestor of ``refs/remotes/origin/dev``
#: (SA-0MVPZG7A0000N66U). Both paths resolve their refs with
#: ``git rev-parse --verify``; modelling a published commit keeps those paths
#: exercised for real instead of tripping the fail-closed assertion, whose own
#: behaviour is covered by
#: ``tests/test_implement_finish_local_dev_verified_sync.py``.
_FAKE_COMMIT = "1" * 40

#: Commit hash returned when ``git_get_commit_hash`` is called on a real commit.
_FAKE_COMMITTED_HASH = "abc1234def5678"

#: Commit hash returned when ``git_get_commit_hash`` is called on a clean tree
#: (no commit was created — ``git_get_commit_hash`` returns the existing HEAD).
_FAKE_CLEAN_HASH = "9" * 40


class FakeGitBoundary:
    """Outcome-aware fake for the ``run_cmd`` boundary in finish tests.

    Replaces the ad-hoc ``_fake_run_cmd`` function with a class that can
    simulate two modes of the ``git_commit`` function:

    * **"commit"** — ``git diff --cached --quiet`` returns a non-zero exit
      code (changes present), so ``git commit`` is invoked.  The caller can
      read :attr:`commit_invoked` to verify a real commit was created.
    * **"clean"** — ``git diff --cached --quiet`` returns 0 (no staged diff),
      so ``git commit`` is never invoked.  The caller reads
      :attr:`commit_invoked` to confirm the no-op path.

    Both modes still resolve ``git rev-parse`` to :data:`_FAKE_COMMIT`
    (required by the fast-forward / ancestry-assertion paths) and still
    succeed ``git push`` / ``git add`` / ``git commit`` so the test doesn't
    trip on unrelated stubs.

    Args:
        outcome: One of ``"commit"`` (real commit) or ``"clean"`` (no-op).

    Attributes:
        commit_invoked: ``True`` iff a ``git commit`` call was detected.
        run_cmd_calls: Ordered list of ``cmd`` lists passed to
            :meth:`_run_cmd` (for diagnostic assertions).
    """

    def __init__(self, outcome: str = "commit") -> None:
        if outcome not in ("commit", "clean"):
            raise ValueError(f"outcome must be 'commit' or 'clean', got {outcome!r}")
        self.outcome = outcome
        self.commit_invoked = False
        self.run_cmd_calls: list[list[str]] = []

    # ------------------------------------------------------------------
    # Context-manager protocol
    # ------------------------------------------------------------------
    def __enter__(self) -> FakeGitBoundary:
        self._patch = patch.object(implement, "run_cmd", side_effect=self._run_cmd)
        self._patch.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._patch.stop()

    # ------------------------------------------------------------------
    # Internal runner
    # ------------------------------------------------------------------
    def _run_cmd(self, cmd: list[str], *args: object, **kwargs: object) -> subprocess.CompletedProcess:  # type: ignore[return]
        """Central ``run_cmd`` fake that routes by command name."""
        self.run_cmd_calls.append(list(cmd))
        cmd_name = cmd[1] if len(cmd) > 1 else ""

        # -- git rev-parse: every revision resolves to _FAKE_COMMIT --
        if cmd_name == "rev-parse":
            return subprocess.CompletedProcess(
                args=cmd, returncode=0,
                stdout=f"{_FAKE_COMMIT}\n",
                stderr="",
            )

        # -- git diff --cached --quiet: gate for whether to commit --
        if "diff" in cmd and "--cached" in cmd and "--quiet" in cmd:
            rc = 1 if self.outcome == "commit" else 0
            return subprocess.CompletedProcess(
                args=cmd, returncode=rc,
                stdout="", stderr="",
            )

        # -- git add -A: always succeeds (side-effect only) --
        if cmd_name == "add":
            return subprocess.CompletedProcess(
                args=cmd, returncode=0,
                stdout="", stderr="",
            )

        # -- git commit: record invocation and succeed --
        if cmd_name == "commit":
            self.commit_invoked = True
            return subprocess.CompletedProcess(
                args=cmd, returncode=0,
                stdout=f"{_FAKE_COMMITTED_HASH}\n",
                stderr="",
            )

        # -- git push: always succeeds (stubbed) --
        if cmd_name == "push":
            return subprocess.CompletedProcess(
                args=cmd, returncode=0,
                stdout="", stderr="",
            )

        # -- default: succeed with empty output --
        return subprocess.CompletedProcess(
            args=cmd, returncode=0,
            stdout="", stderr="",
        )


# ----------------------------------------------------------------------
# AC3 — report / comment wording helper
# ----------------------------------------------------------------------

def assert_report_wording(
    report: dict[str, Any],
    *,
    commit_hash: str | None = None,
) -> None:
    """Assert that the finish report / completion comment wording matches.

    Used by later slices to verify the commit hash is present on the commit
    path and absent ("no changes / no commit") on the no-op path.

    Args:
        report: The ``phase_finish`` return dict.
        commit_hash: If provided, assert the created commit hash appears in
            ``report["hash"]``, ``report["message"]`` and
            ``report["steps"]["commit"]["hash"]``.  If ``None``, assert the
            report says no commit was created — an explicit "no changes" /
            "no commit" wording, or a ``created: False`` marker on the commit
            step (the wording the later production slice will add).
    """
    assert report["success"] is True, report

    commit_step = report.get("steps", {}).get("commit", {})
    if commit_hash is not None:
        # Commit path: the created hash must be recorded and surfaced.
        assert report.get("hash") == commit_hash, (
            f"report['hash'] expected {commit_hash!r}, "
            f"got {report.get('hash')!r}"
        )
        assert commit_hash in report["message"], (
            f"report['message'] should contain commit hash {commit_hash!r}: "
            f"{report['message']!r}"
        )
        assert commit_step.get("hash") == commit_hash, (
            f"report['steps']['commit']['hash'] expected {commit_hash!r}, "
            f"got {commit_step.get('hash')!r}"
        )
    else:
        # No-op path: the report must say no commit was created. Later
        # slices make production record this; the helper accepts either
        # explicit "no changes / no commit" wording or a ``created: False``
        # marker on the commit step.
        wording = json.dumps(report).lower()
        assert (
            "no changes" in wording
            or "no commit" in wording
            or commit_step.get("created") is False
        ), (
            "no-op finish report should say no commit was created; "
            f"commit step: {commit_step}, message: {report.get('message')!r}"
        )


def _build_result() -> dict:
    return {
        "success": True,
        "stdout": "no build",
        "stderr": "",
        "exit_code": 0,
        "skipped": True,
    }


def _tests_result() -> dict:
    return {
        "success": True,
        "failures": [],
        "skipped": False,
        "tooling": "suite",
        "scope": "changed",
        "notices": [],
    }


def _cleanup_result() -> dict:
    return {
        "method": "wl_cleanup_worktree",
        "terminated": 0,
        "warning": "",
    }


@contextlib.contextmanager
def _mocked_phase_finish(
    *,
    update_status=None,
    store_show=None,
    tmp_path=None,
    outcome="commit",
    state_verification_only: bool = False,
):
    """Patch every external boundary of ``phase_finish``.

    The real ``StatusLifecycle`` class is kept (so the context manager's
    enter/exit transitions actually run) with only its ``update_status`` and
    ``show`` boundaries patched; the remaining git/worktree/build/test
    boundaries are stubbed.

    Args:
        update_status: A ``MagicMock`` installed as
            ``StatusLifecycle.update_status`` (instance and class calls land
            on the same mock).
        store_show: The dict returned by ``StatusLifecycle.show`` — what the
            store holds when the read-back assertion runs (defaults to a
            terminal store: ``completed``/``in_review``).
        tmp_path: A pytest ``tmp_path`` to use as the worktree directory.
        outcome: The :class:`FakeGitBoundary` outcome — ``"commit"`` (a real
            commit is created; the default) or ``"clean"`` (nothing to
            commit).
        state_verification_only: Record the verification-only opt-in in the
            run's state file (the ``start --verification-only`` path) rather
            than passing the CLI flag to ``phase_finish``.

    Yields:
        A dict of mocks; ``boundary`` is the :class:`FakeGitBoundary` so a
        test can assert whether ``git commit`` was invoked, and
        ``add_comment`` records every comment the phase posts.
    """
    worktree = str((tmp_path or Path("/tmp/wt-finish-no-diff")).resolve())
    Path(worktree).mkdir(parents=True, exist_ok=True)
    state = implement.ImplementState(
        work_item_id="OSL-TEST",
        worktree_path=worktree,
        repo_root="/tmp/repo-root",
        parent_branch="dev",
        verification_only=state_verification_only,
    )
    if update_status is None:
        update_status = MagicMock()
    if store_show is None:
        store_show = {"workItem": {"status": "completed", "stage": "in_review"}}
    show = MagicMock(return_value=store_show)

    # The fake git boundary replaces ``run_cmd`` and drives the *real*
    # ``git_commit`` gate, so the harness can distinguish a created commit
    # from a no-op (AC1). ``git_get_commit_hash`` is patched to reflect the
    # boundary's outcome: a freshly-created hash on the commit path, the
    # pre-existing HEAD on the clean path (the ambient hash the false
    # ``in_review`` regression mistook for the item's commit).
    boundary = FakeGitBoundary(outcome=outcome)

    def _commit_hash(_cwd: str) -> str:
        return _FAKE_COMMITTED_HASH if boundary.commit_invoked else _FAKE_CLEAN_HASH

    with contextlib.ExitStack() as stack:
        stack.enter_context(
            patch.object(implement, "_discover_worktree", return_value=worktree)
        )
        stack.enter_context(patch.object(implement, "read_state", return_value=state))
        stack.enter_context(
            patch.object(implement, "_worktree_placement_violation", return_value=None)
        )
        stack.enter_context(patch.object(implement, "run_build", return_value=_build_result()))
        # ``phase_finish`` runs its gate against the **would-be-commit tree**
        # through ``run_gate_suite`` (temp-index staging,
        # SA-0MVQHVGFR006W8LG) and, once green, stages the change set in the
        # real index through ``stage_tested_tree``. Both act on a real git
        # repository; this harness drives ``phase_finish`` from a bare temp
        # directory, so both boundaries are stubbed green (the gate's own
        # behaviour is covered by ``test_finish_gate_*.py``).
        stack.enter_context(
            patch.object(
                implement,
                "run_gate_suite",
                return_value=(
                    _tests_result(),
                    {
                        "tree": "abc1234",
                        "scope": "changed",
                        "cached": False,
                        "index_unchanged": True,
                    },
                ),
            )
        )
        stack.enter_context(
            patch.object(
                implement,
                "stage_tested_tree",
                return_value={
                    "success": True,
                    "tree": "abc1234",
                    "expected": "abc1234",
                },
            )
        )
        stack.enter_context(patch.object(implement, "run_tests", return_value=_tests_result()))
        stack.enter_context(
            patch.object(implement, "git_get_commit_hash", side_effect=_commit_hash)
        )
        stack.enter_context(
            patch.object(implement, "git_push_to_dev", return_value=True)
        )
        # The dev push now runs from the worktree and its subject guard reads
        # the real git HEAD (SA-0MVUTXJ4300301ZN); this harness uses a
        # placeholder, non-git worktree, so the guard is stubbed like the push
        # itself. The guard has dedicated coverage in
        # ``skill/implement/tests/test_implement_push_from_worktree.py``.
        stack.enter_context(patch.object(implement, "_assert_push_subject"))
        # The outcome-aware fake git boundary drives the real ``git_commit``
        # (AC1); it replaces the previous per-test ``run_cmd`` stub so a test
        # can express "a commit happened" vs "nothing to commit" without new
        # stubbing.
        stack.enter_context(boundary)
        stack.enter_context(
            patch.object(implement, "cleanup_worktree_processes", return_value=_cleanup_result())
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
        add_comment = MagicMock(return_value=True)
        stack.enter_context(
            patch.object(implement, "wl_add_comment", add_comment)
        )
        stack.enter_context(patch.object(implement, "_surface_postfix_prose"))
        stack.enter_context(
            patch.object(implement.StatusLifecycle, "update_status", update_status)
        )
        stack.enter_context(patch.object(implement.StatusLifecycle, "show", show))
        yield {
            "update_status": update_status,
            "show": show,
            "boundary": boundary,
            "add_comment": add_comment,
        }


def _terminal_update(update_status: MagicMock) -> tuple | None:
    """Return the ``(id, status)`` pair of the terminal ``completed`` write.

    The real ``StatusLifecycle.__exit__`` calls
    ``self.update_status(id, status="completed", stage="in_review")``
    (keyword ``status``), while the failure-path reset is positional
    (``update_status(id, "open")``) — scan for the keywords to stay
    independent of calling convention.
    """
    for call in update_status.call_args_list:
        if call.kwargs.get("status") == "completed" and call.kwargs.get("stage") == "in_review":
            return (call.args[0], "completed")
    return None


def _reset_update(update_status: MagicMock) -> tuple | None:
    """Return the ``(id, status)`` pair of the failure-path reset to ``open``."""
    for call in update_status.call_args_list:
        if len(call.args) >= 2 and call.args[1] == "open":
            return (call.args[0], "open")
    return None


def test_finish_no_diff_path_persists_terminal_status(tmp_path):
    """A verification-only, no-commit finish reaches the terminal transition.

    ``git_commit`` sees an empty staged diff (no commit created), the push
    is a no-op success, yet the store must hold ``completed``/``in_review``
    afterwards — the behaviour pinned for OSL-0MVME1PI5005WU58 AC3, preserved
    behind the explicit ``--verification-only`` opt-in (SA-0MW74IJWI009NRZ1
    AC3).

    The ``clean`` outcome drives the *real* ``git_commit`` gate, so this test
    genuinely pins the commit-less path: :attr:`FakeGitBoundary.commit_invoked`
    is ``False``. Unlike the old bug it must **never** report the ambient HEAD
    (``_FAKE_CLEAN_HASH``) as the item's commit (AC2): the report says no
    changes / no commit and carries an empty hash.
    """
    lifecycle = MagicMock()
    with _mocked_phase_finish(
        update_status=lifecycle, tmp_path=tmp_path, outcome="clean"
    ) as mocks:
        report = implement.phase_finish(
            "OSL-TEST",
            json_output=True,
            no_refactor=True,
            verification_only=True,
        )

    assert report["success"] is True, report["message"]

    # A clean tree: ``git commit`` must not have run, and the report must not
    # misattribute the ambient HEAD as the item's commit.
    assert mocks["boundary"].commit_invoked is False
    assert report["hash"] == ""
    assert report["steps"]["commit"]["created"] is False
    assert report["steps"]["commit"]["reason"] == "no_changes"
    assert_report_wording(report, commit_hash=None)

    # The real StatusLifecycle.__exit__ issued the terminal write.
    assert _terminal_update(mocks["update_status"]) == ("OSL-TEST", "completed")

    # The store read-back (the fail-loud guard) saw the terminal state.
    assert report["steps"]["status_readback"]["success"] is True
    assert report["steps"]["status_readback"]["status"] == "completed"
    assert report["steps"]["status_readback"]["stage"] == "in_review"

    # The completion comment says no changes and never names a commit.
    completion = _completion_comments(mocks["add_comment"])
    assert completion
    assert "No changes" in completion[-1]
    assert "Commit:" not in completion[-1]


def test_finish_verification_only_state_flag_finalises(tmp_path):
    """The ``start --verification-only`` state flag is the finish signal.

    The state file is the single source of truth ``drive`` also reads, so a
    no-commit finish records the opt-in once (at ``start``) and finalises
    without the CLI flag being repeated (AC3).
    """
    lifecycle = MagicMock()
    with _mocked_phase_finish(
        update_status=lifecycle,
        tmp_path=tmp_path,
        outcome="clean",
        state_verification_only=True,
    ) as mocks:
        report = implement.phase_finish("OSL-TEST", json_output=True, no_refactor=True)

    assert report["success"] is True, report["message"]
    assert mocks["boundary"].commit_invoked is False
    assert report["steps"]["commit"]["reason"] == "no_changes"
    assert _terminal_update(mocks["update_status"]) == ("OSL-TEST", "completed")
    assert report["steps"]["status_readback"]["success"] is True


def test_finish_no_commit_without_signal_refuses_terminal_write(tmp_path):
    """Default refusal (AC1): no commit and no signal → no terminal write.

    A no-op finish used to read the ambient ``dev`` HEAD (another item's
    commit), push a no-op and issue the terminal ``completed``/``in_review``
    write. Without ``--verification-only`` it must instead fail loudly with a
    structured ``no_commit_no_signal`` reason, leave the item ``open``, post a
    refusal comment (never a completion comment) and name the retained
    worktree.
    """
    lifecycle = MagicMock()
    with _mocked_phase_finish(
        update_status=lifecycle, tmp_path=tmp_path, outcome="clean"
    ) as mocks:
        report = implement.phase_finish("OSL-TEST", json_output=True, no_refactor=True)

    assert report["success"] is False, report
    assert mocks["boundary"].commit_invoked is False

    # Structured, machine-readable refusal reason (never prose parsing).
    assert report["steps"]["commit"]["reason"] == "no_commit_no_signal"
    assert report["steps"]["commit"]["created"] is False
    assert report["steps"]["commit"]["hash"] == ""
    assert report["steps"]["commit"]["worktree_path"] == report["worktree_path"]

    # No terminal write was issued; the item was released to `open`.
    assert _terminal_update(mocks["update_status"]) is None
    assert _reset_update(mocks["update_status"]) == ("OSL-TEST", "open")

    # The failure is loud and actionable: names the retained worktree and the
    # exact opt-in that would finalise the item deliberately.
    assert "--verification-only" in report["message"]
    assert report["worktree_path"] in report["message"]

    # No "Implementation complete" comment is posted; the refusal is recorded.
    completion = _completion_comments(mocks["add_comment"])
    assert completion == []
    refusal = [
        call.args[1]
        for call in mocks["add_comment"].call_args_list
        if len(call.args) > 1 and "no_commit_no_signal" in call.args[1]
    ]
    assert refusal, mocks["add_comment"].call_args_list


# ----------------------------------------------------------------------
# Helper: completion comments posted during a phase_finish run
# ----------------------------------------------------------------------

def _completion_comments(add_comment: MagicMock) -> list[str]:
    """Return every "Implementation complete" comment the phase posted."""
    return [
        str(call.args[1])
        for call in add_comment.call_args_list
        if len(call.args) > 1 and "Implementation complete" in str(call.args[1])
    ]


def test_finish_push_with_diff_path_persists_terminal_status(tmp_path):
    """The push-with-diff path is held to the same guarantee (companion).

    When a real commit was created and pushed, the store read-back must
    equally confirm the terminal transition — the no-op path is not treated
    differently (OSL-0MVME1PI5005WU58 AC5).

    The ``commit`` outcome exercises the commit-creating branch of the real
    ``git_commit`` gate: ``git commit`` runs (``commit_invoked`` is ``True``)
    and the created hash is surfaced through :func:`assert_report_wording`
    (AC1/AC3).
    """
    lifecycle = MagicMock()
    with _mocked_phase_finish(
        update_status=lifecycle, tmp_path=tmp_path, outcome="commit"
    ) as mocks:
        report = implement.phase_finish("OSL-TEST", json_output=True, no_refactor=True)

    assert report["success"] is True, report["message"]
    assert mocks["boundary"].commit_invoked is True
    assert report["hash"] == _FAKE_COMMITTED_HASH
    assert_report_wording(report, commit_hash=_FAKE_COMMITTED_HASH)
    assert _terminal_update(mocks["update_status"]) == ("OSL-TEST", "completed")
    assert report["steps"]["status_readback"]["success"] is True


def test_finish_fails_loudly_when_terminal_status_missing_from_store(tmp_path):
    """Finish never reports success while the store is not terminal.

    If the terminal write is lost or overwritten (the recorded symptom on
    OSL-0MVMB44C9008IXZX: finish reported success while the item was still
    ``open``), the read-back assertion must fail the phase with an actionable
    message and release the item to ``open`` — a silent lifecycle lie is a
    hard failure, never a green report.
    """
    lifecycle = MagicMock()
    non_terminal_store = {
        "workItem": {"status": "open", "stage": "intake_complete"}
    }
    with _mocked_phase_finish(
        update_status=lifecycle, store_show=non_terminal_store, tmp_path=tmp_path
    ) as mocks:
        report = implement.phase_finish("OSL-TEST", json_output=True, no_refactor=True)

    assert report["success"] is False, (
        "finish must fail loudly when the store does not hold the terminal "
        f"transition; got: {report}"
    )
    assert "does not hold the terminal transition" in report["message"]
    # The item is released to open (StatusLifecycle's reset), not left claimed.
    assert _reset_update(mocks["update_status"]) == ("OSL-TEST", "open")
    # The reset also releases the assignee hold.
    resets = [
        c for c in mocks["update_status"].call_args_list
        if len(c.args) >= 2 and c.args[1] == "open"
    ]
    assert resets and resets[-1].kwargs.get("assignee") == ""


def test_finish_success_clears_assignee(tmp_path):
    """finish releases the assignee hold with the terminal completion.

    A non-empty assignee is a scheduling hold; a successful implement run
    must clear it together with the ``completed``/``in_review`` transition so
    the reviewed item is never left held (assignee-hold convention).
    """
    lifecycle = MagicMock()
    with _mocked_phase_finish(
        update_status=lifecycle, tmp_path=tmp_path
    ) as mocks:
        report = implement.phase_finish("OSL-TEST", json_output=True, no_refactor=True)

    assert report["success"] is True, report["message"]

    terminal_calls = [
        c for c in mocks["update_status"].call_args_list
        if c.kwargs.get("status") == "completed"
        and c.kwargs.get("stage") == "in_review"
    ]
    assert terminal_calls, "finish must issue the terminal transition"
    assert terminal_calls[-1].kwargs.get("assignee") == ""


# ----------------------------------------------------------------------
# AC1 — the fake git boundary distinguishes commit from no-op
# ----------------------------------------------------------------------

def test_fake_git_boundary_commit_mode_invokes_commit():
    """``commit`` outcome: staged diff present, a commit is created and named.

    AC5(i)/AC1: the structured outcome reports ``committed=True`` and records
    the newly created hash, so finish can attribute the commit to this item.
    """
    boundary = FakeGitBoundary(outcome="commit")
    with boundary:
        outcome = implement.git_commit("/tmp/does-not-matter", "msg")

    assert bool(outcome) is True
    assert outcome.committed is True
    assert outcome.reason == "committed"
    # ``git rev-parse`` resolves every revision to ``_FAKE_COMMIT`` here.
    assert outcome.hash == _FAKE_COMMIT
    assert boundary.commit_invoked is True
    assert any(cmd[1] == "commit" for cmd in boundary.run_cmd_calls)


def test_fake_git_boundary_clean_mode_skips_commit():
    """``clean`` outcome: no staged diff, no commit, and ``committed=False``.

    AC5(i)/AC1: the operation still *succeeds* (``bool(outcome) is True``) but
    the structured outcome says no commit was created and carries no hash —
    the distinction the old bare-``True`` return could not express.
    """
    boundary = FakeGitBoundary(outcome="clean")
    with boundary:
        outcome = implement.git_commit("/tmp/does-not-matter", "msg")

    assert bool(outcome) is True
    assert outcome.committed is False
    assert outcome.hash == ""
    assert outcome.reason == "no_changes"
    assert boundary.commit_invoked is False
    assert not any(cmd[1] == "commit" for cmd in boundary.run_cmd_calls)


def test_fake_git_boundary_rejects_unknown_outcome():
    """An unknown outcome is a harness misconfiguration, not a silent no-op."""
    with pytest.raises(ValueError, match="'commit' or 'clean'"):
        FakeGitBoundary(outcome="bogus")


# ----------------------------------------------------------------------
# AC3 — report / completion-comment wording helper
# ----------------------------------------------------------------------

def _synthetic_report(**overrides: Any) -> dict[str, Any]:
    report = {
        "success": True,
        "hash": _FAKE_COMMITTED_HASH,
        "message": f"Implementation complete. Commit {_FAKE_COMMITTED_HASH} pushed to dev.",
        "steps": {"commit": {"hash": _FAKE_COMMITTED_HASH, "message": "msg"}},
    }
    report.update(overrides)
    return report


def test_assert_report_wording_accepts_commit_hash():
    """The commit path: the created hash is recorded and surfaced."""
    assert_report_wording(_synthetic_report(), commit_hash=_FAKE_COMMITTED_HASH)


def test_assert_report_wording_accepts_no_commit_wording():
    """The no-op path: an explicit "no commit" wording is accepted."""
    no_op = _synthetic_report(
        hash=_FAKE_CLEAN_HASH,
        message="Implementation complete. No changes — no commit was created.",
        steps={"commit": {"hash": _FAKE_CLEAN_HASH, "created": False}},
    )
    assert_report_wording(no_op, commit_hash=None)


def test_assert_report_wording_rejects_mismatched_commit_hash():
    """A wrong hash on the commit path fails loudly."""
    with pytest.raises(AssertionError, match="report\\['hash'\\]"):
        assert_report_wording(_synthetic_report(), commit_hash="deadbeef")


def test_assert_report_wording_rejects_commit_report_on_no_commit_path():
    """A report with no "no commit" wording fails the no-op assertion."""
    with pytest.raises(AssertionError, match="should say no commit was created"):
        assert_report_wording(_synthetic_report(), commit_hash=None)


# ----------------------------------------------------------------------
# AC4 — reusable child / worktree fixture
# ----------------------------------------------------------------------

def test_child_worktree_fixture_writes_state_and_classifies_open_child(
    child_worktree_fixture,
):
    """The fixture writes a real state file and classifies an open child."""
    child, state_path = child_worktree_fixture()

    assert state_path is not None and state_path.exists()
    state = implement.read_state(str(state_path.parent))
    assert state is not None
    assert state.work_item_id == child["id"]

    # Exercise the fixture against current ``_classify_child`` behaviour: an
    # open child with no live lock is startable.
    assert implement._classify_child(child) == "implement"


def test_child_worktree_fixture_classifies_terminal_child(child_worktree_fixture):
    """A terminal child fixture classifies as ``skip-terminal`` (current behaviour)."""
    child, _ = child_worktree_fixture(status="completed", stage="in_review")

    assert implement._classify_child(child) == "skip-terminal"


def test_child_worktree_fixture_can_omit_state_file(child_worktree_fixture):
    """``write_state_file=False`` yields no state file (for lock-only fixtures)."""
    child, state_path = child_worktree_fixture(write_state_file=False)

    assert state_path is None
    assert child["status"] == "open"
