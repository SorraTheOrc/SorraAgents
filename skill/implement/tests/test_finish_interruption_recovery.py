"""Regression tests: recoverable interruption and idempotent re-run.

Parent **SA-0MVWFPI31000HP3H** (Only mark as complete when fully complete),
child **SA-0MVXUSJJY009HMQ0** (parent AC3 — recoverable interruption).

The contract these tests pin: an implement run interrupted *after* its work
was committed/pushed but *before* its final lifecycle action must

1. leave the item non-terminal (``open``) with the held assignee cleared —
   never stuck in ``completed``/``in_review`` for work that did not finish
   (AC1) — and the worktree either removed or deliberately retained, never
   stranded (AC3);
2. record one comment naming the retained state: the published (or local)
   commit, the branch, the worktree's fate and the command that resumes
   (AC2/AC6);
3. converge when ``finish`` is re-run: the ordinary no-diff/no-op-push path
   completes the lifecycle, and the completion comment plus the interruption
   record are deduplicated on a stable marker + commit hash, so a recovery
   never stacks a duplicate completion comment or a second terminal
   transition in one run (AC4).

Coverage:

- ``_handle_signal_cleanup`` (the SIGTERM/SIGINT path) during a finish whose
  commit reached ``dev``: release + retained-state comment, with the tree
  removed on the plain path and retained under the driven-session deferral
  (AC1/AC2/AC3);
- the ``phase_finish`` failure exit after a successful push: the item is
  ``open`` with an empty assignee and the interruption is recorded exactly
  once however many times it is retried (AC1/AC2/AC6);
- a lost terminal lifecycle write, then a re-run: convergence to
  ``completed``/``in_review`` with one completion comment, one interruption
  record and one terminal transition in the converging run (AC4).

The real ``phase_finish`` state machine is driven with only its external
boundaries patched (git, the ``wl``/``StatusLifecycle`` store, the build and
gate runners, the push, worktree removal, the read-only post-push
verification), following the stub/runner style of
``test_finish_no_diff_status.py``: the modelled store records writes, reads
comments back, and can drop the terminal write once to simulate the lost
lifecycle write that strands a half-finished run.
"""

from __future__ import annotations

import contextlib
import signal
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) in sys.path:
    sys.path.remove(str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT))

from skill.implement.scripts import implement
from skill.shared import git_sandbox as gs

#: The synthetic work item every test drives.
_ITEM_ID = "SA-INTERRUPT1"
#: Short commit hash the fake git/commit boundaries report (the completion
#: comment and the interruption record both name it).
_COMMIT = "abc1234"
#: Full commit hash the fake ``git rev-parse`` resolves to.
_FAKE_COMMIT = "1" * 40


# ---------------------------------------------------------------------------
# Hermetic boundaries
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_interruption_state(monkeypatch):
    """Keep the module-level interruption state and deferral env per test.

    ``IMPLEMENT_DRIVE_ACTIVE`` is set for a driven child session (this suite
    can run inside one), and the module globals are process-wide: both would
    silently change the deferral verdict of ``_handle_signal_cleanup``.
    """
    monkeypatch.delenv(implement.DRIVE_RECURSION_ENV, raising=False)
    monkeypatch.delenv(implement.KEEP_WORKTREE_ENV, raising=False)
    monkeypatch.setattr(implement, "_finish_progress_global", {}, raising=False)
    monkeypatch.setattr(implement, "_keep_worktree_global", False, raising=False)
    monkeypatch.setattr(implement, "_child_lock_global", None, raising=False)
    yield


def _fake_run_cmd(cmd: list[str], *args, **kwargs) -> subprocess.CompletedProcess:
    """Stand-in for ``run_cmd``: every git sub-command "succeeds".

    ``rev-parse`` resolves to :data:`_FAKE_COMMIT` so the main-checkout
    snapshot/verify and any ancestry resolution behave like a healthy
    repository instead of tripping a fail-closed assertion.
    """
    stdout = f"{_FAKE_COMMIT}\n" if len(cmd) > 1 and cmd[1] == "rev-parse" else ""
    if len(cmd) > 1 and cmd[1] == "merge-base":
        # A freshly-committed feature branch is not yet on origin/dev:
        # the pre-push already-landed check (SA-0MW22SEMV00230O5) must see
        # an unpushed commit and proceed to the push.
        return subprocess.CompletedProcess(
            args=cmd, returncode=1, stdout="", stderr=""
        )
    return subprocess.CompletedProcess(
        args=cmd, returncode=0, stdout=stdout, stderr=""
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
    return {"method": "wl_cleanup_worktree", "terminated": 0, "warning": ""}


def _green_verification() -> dict:
    return {
        "verdict": implement.POST_PUSH_VERDICT_GREEN,
        "success": True,
        "executed": False,
        "reason": "",
        "entries": [],
    }


# ---------------------------------------------------------------------------
# Harness: the real phase_finish over a modelled store
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _finish_harness(tmp_path: Path):
    """Drive the real ``phase_finish`` against a modelled work-item store.

    Only external boundaries are patched. The store models ``wl show`` /
    ``wl update`` and the comments, so lifecycle writes, reads and records are
    observable; ``ctx["drop_terminal_write"]`` drops the next
    ``completed``/``in_review`` write once, simulating the lost lifecycle write
    that leaves a half-finished run stranded (the interruption this item is
    about).

    Yields:
        ``SimpleNamespace`` with ``run()`` (invoke ``phase_finish``),
        ``item`` (the live store item), ``comments`` (recorded comment dicts),
        ``writes`` (applied lifecycle writes) and ``ctx`` (mutable switches).
    """
    worktree = str((tmp_path / "worktree").resolve())
    Path(worktree).mkdir(parents=True, exist_ok=True)
    repo_root = str((tmp_path / "main-checkout").resolve())
    Path(repo_root).mkdir(parents=True, exist_ok=True)

    state = implement.ImplementState(
        work_item_id=_ITEM_ID,
        worktree_path=worktree,
        repo_root=repo_root,
        parent_branch="dev",
    )
    item: dict = {
        "id": _ITEM_ID,
        "status": "in-progress",
        "stage": "intake_complete",
        "assignee": "implement",
        "parentId": "",
        "needsProducerReview": False,
        "auditResult": None,
        "updatedAt": datetime.now(timezone.utc).isoformat(),
    }
    comments: list[dict] = []
    writes: list[dict] = []
    ctx: dict = {"drop_terminal_write": False}

    def _update_status(work_item_id, *args, **kwargs):
        """Simulated ``wl update`` — applies the write (unless dropped)."""
        status = args[0] if args else kwargs.get("status")
        if status == "completed" and ctx["drop_terminal_write"]:
            # The lost lifecycle write: the transition never reaches the store.
            ctx["drop_terminal_write"] = False
            return {"success": True}
        if status is not None:
            item["status"] = str(status)
        stage = kwargs.get("stage")
        if stage is not None:
            item["stage"] = str(stage)
        if kwargs.get("assignee") is not None:
            item["assignee"] = str(kwargs["assignee"])
        writes.append(
            {
                "status": item["status"],
                "stage": item["stage"],
                "assignee": item["assignee"],
            }
        )
        return {"success": True}

    def _show(work_item_id, **kwargs):
        """Simulated ``wl show`` — the item plus its recorded comments."""
        return {
            "workItem": dict(item),
            "comments": [dict(comment) for comment in comments],
        }

    def _add_comment(work_item_id, comment):
        comments.append({"comment": comment, "author": "implement"})
        return True

    def _run():
        return implement.phase_finish(
            _ITEM_ID, json_output=True, no_refactor=True
        )

    with contextlib.ExitStack() as stack:
        stack.enter_context(
            patch.object(implement, "_discover_worktree", return_value=worktree)
        )
        stack.enter_context(patch.object(implement, "read_state", return_value=state))
        stack.enter_context(
            patch.object(implement, "_worktree_placement_violation", return_value=None)
        )
        stack.enter_context(
            patch.object(implement, "run_build", return_value=_build_result())
        )
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
        stack.enter_context(patch.object(implement, "git_commit", return_value=True))
        stack.enter_context(
            patch.object(implement, "git_get_commit_hash", return_value=_COMMIT)
        )
        stack.enter_context(
            patch.object(implement, "git_push_to_dev", return_value=True)
        )
        stack.enter_context(patch.object(implement, "_assert_push_subject"))
        stack.enter_context(
            patch.object(implement, "run_cmd", side_effect=_fake_run_cmd)
        )
        stack.enter_context(
            patch.object(
                implement, "cleanup_worktree_processes", return_value=_cleanup_result()
            )
        )
        stack.enter_context(patch.object(implement, "remove_state"))
        stack.enter_context(
            patch.object(implement, "_remove_worktree", return_value=True)
        )
        stack.enter_context(
            patch.object(
                implement,
                "_fast_forward_local_dev_for_push",
                return_value={"success": True, "own_churn": [], "method": "stub"},
            )
        )
        stack.enter_context(
            patch.object(
                implement,
                "_assert_commit_on_origin_dev",
                return_value={"success": True, "target_ref": "refs/remotes/origin/dev"},
            )
        )
        stack.enter_context(
            patch.object(
                implement,
                "_resolve_post_push_verification",
                return_value=_green_verification(),
            )
        )
        # The refusal path routes a red/missing verdict to the triage helper,
        # which would create real work items; these tests never rely on it.
        stack.enter_context(
            patch.object(
                implement,
                "_route_post_push_failure_to_triage",
                return_value={"success": True},
            )
        )
        stack.enter_context(
            patch.object(
                implement,
                "_sync_parent_branch",
                return_value={"method": "skipped", "success": True},
            )
        )
        stack.enter_context(patch.object(implement, "wl_add_comment", _add_comment))
        stack.enter_context(patch.object(implement, "_surface_postfix_prose"))
        stack.enter_context(
            patch.object(
                implement, "acquire_main_checkout_lock", return_value=(999, None)
            )
        )
        stack.enter_context(
            patch.object(implement, "_release_main_checkout_lock")
        )
        stack.enter_context(
            patch.object(
                implement.StatusLifecycle,
                "update_status",
                MagicMock(side_effect=_update_status),
            )
        )
        stack.enter_context(
            patch.object(
                implement.StatusLifecycle, "show", MagicMock(side_effect=_show)
            )
        )
        yield SimpleNamespace(
            run=_run, item=item, comments=comments, writes=writes, ctx=ctx
        )


def _comment_bodies(comments: list[dict]) -> list[str]:
    return [str(entry.get("comment", "")) for entry in comments]


def _with_prefix(comments: list[dict], prefix: str) -> list[str]:
    return [body for body in _comment_bodies(comments) if body.startswith(prefix)]


# ---------------------------------------------------------------------------
# AC1/AC2/AC3 — SIGTERM/SIGINT during a finish after the push
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _signal_after_push(worktree: Path, push_commit: str = _COMMIT):
    """Install the signal-handler globals for an interrupted finish.

    Restores every module-level global it writes, so a test's interruption
    context never leaks into the next test's signal cleanup.
    """
    previous = (
        implement._work_item_id_global,
        implement._worktree_path_global,
        implement._repo_root_global,
        implement._child_lock_global,
    )
    implement._work_item_id_global = _ITEM_ID
    implement._worktree_path_global = str(worktree)
    implement._repo_root_global = "/tmp/repo-root"
    implement._child_lock_global = None
    implement._finish_progress_global.update(
        {
            "work_item_id": _ITEM_ID,
            "worktree_path": str(worktree),
            "branch": "wl-SA-interrupt1",
            "commit": push_commit,
            "pushed": True,
        }
    )
    updates: list[tuple[tuple, dict]] = []
    comments: list[tuple[str, str]] = []
    removed: list[str] = []

    try:
        with (
            patch.object(
                implement.StatusLifecycle,
                "show",
                return_value={
                    "workItem": {"status": "in-progress", "assignee": "implement"},
                    "comments": [],
                },
            ),
            patch.object(
                implement.StatusLifecycle,
                "update_status",
                side_effect=lambda *a, **k: updates.append((a, k)),
            ),
            patch.object(
                implement,
                "wl_add_comment",
                side_effect=lambda wid, body: comments.append((wid, body)),
            ),
            patch.object(implement, "_restore_repo_state"),
            patch.object(implement, "cleanup_worktree_processes"),
            patch.object(
                implement,
                "_remove_worktree",
                side_effect=lambda path, **_kwargs: removed.append(path) or True,
            ),
            patch.object(implement, "_release_child_lock"),
        ):
            yield {
                "updates": updates,
                "comments": comments,
                "removed": removed,
            }
    finally:
        (
            implement._work_item_id_global,
            implement._worktree_path_global,
            implement._repo_root_global,
            implement._child_lock_global,
        ) = previous


def test_sigterm_after_push_releases_the_item_and_names_the_retained_state(
    tmp_path,
):
    """AC1/AC2/AC3: released to ``open``, assignee cleared, state named.

    The item is never ``completed``/``in_review`` (the terminal transition
    never ran), the claim is released, the worktree is not stranded, and the
    comment names the pushed commit, the branch and the resume command.
    """
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    with _signal_after_push(worktree) as mocks:
        implement._handle_signal_cleanup(signal.SIGTERM)

    # Status reset first, with the assignee released (the scheduling hold).
    assert mocks["updates"], "the interrupt must release the work item"
    assert mocks["updates"][0][0] == (_ITEM_ID, "open")
    assert mocks["updates"][0][1].get("assignee") == ""

    # The tree is removed on the plain (non-deferred) path: not stranded.
    assert mocks["removed"] == [str(worktree)]

    # One record naming the retained state, the published commit and the
    # command that resumes.
    assert len(mocks["comments"]) == 1
    item_id, body = mocks["comments"][0]
    assert item_id == _ITEM_ID
    assert body.startswith(implement.INTERRUPTION_COMMENT_MARKER)
    assert "SIGTERM" in body
    assert "assignee cleared" in body
    assert _COMMIT in body and "pushed to dev" in body
    assert "wl-SA-interrupt1" in body
    # The plain path removes the tree, so the record says so and the resume
    # recreates it before finishing.
    assert "Worktree: removed" in body
    assert f"implement.py start {_ITEM_ID}" in body
    assert f"implement.py finish {_ITEM_ID}" in body
    assert "not in_review" in body


def test_sigterm_under_driven_deferral_retains_the_tree_and_says_so(
    tmp_path, monkeypatch
):
    """AC3: a driven session's tree is retained (deferral) and reported.

    The item is still released; the difference is only worktree ownership, and
    the record states the tree survives so the resume can continue on it.
    """
    monkeypatch.setenv(implement.DRIVE_RECURSION_ENV, "1")
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    with _signal_after_push(worktree) as mocks:
        implement._handle_signal_cleanup(signal.SIGINT)

    assert mocks["updates"][0][0] == (_ITEM_ID, "open")
    assert mocks["updates"][0][1].get("assignee") == ""
    assert mocks["removed"] == [], "the driven session's tree must be retained"

    body = mocks["comments"][0][1]
    assert "SIGINT" in body
    assert "Worktree: retained at" in body
    assert _COMMIT in body
    assert f"implement.py finish {_ITEM_ID}" in body
    assert "implement.py start" not in body, (
        "a retained tree resumes on the tree itself — no recreate step"
    )


def test_signal_after_push_does_not_duplicate_the_record(tmp_path):
    """AC2/AC4: a repeat signal (or a re-run) never stacks the record."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    with _signal_after_push(worktree) as mocks:
        implement._handle_signal_cleanup(signal.SIGTERM)
        # A second cleanup pass sees the same (still recorded) context; the
        # comment store must deduplicate on the marker plus commit.
        with patch.object(
            implement.StatusLifecycle,
            "show",
            return_value={
                "workItem": {"status": "in-progress", "assignee": "implement"},
                "comments": [
                    {"comment": mocks["comments"][0][1]},
                ],
            },
        ):
            implement._handle_signal_cleanup(signal.SIGTERM)

    assert len(mocks["comments"]) == 1


# ---------------------------------------------------------------------------
# AC1/AC2/AC6 — a finish failure after the push
# ---------------------------------------------------------------------------


def test_finish_failure_after_push_records_the_state_once(tmp_path):
    """AC1/AC2/AC6: open + empty assignee, one retained-state record.

    The interruption is simulated after the push (the parent-branch sync
    raises), i.e. exactly the window AC1 names. Running the interrupted finish
    twice leaves one record: the deduplication key is the marker plus commit.
    """
    with _finish_harness(tmp_path) as harness, patch.object(
        implement,
        "_sync_parent_branch",
        side_effect=RuntimeError("simulated interruption after push"),
    ):
        first = harness.run()
        second = harness.run()

    assert first["success"] is False
    assert "simulated interruption after push" in first["message"]

    # Non-terminal, claim released: never completed/in_review for unfinished
    # work (AC1).
    assert harness.item["status"] == "open"
    assert harness.item["assignee"] == ""
    assert not [
        write
        for write in harness.writes
        if write["status"] == "completed" and write["stage"] == "in_review"
    ], "no terminal transition may be written by an interrupted run"

    # One record, naming the pushed commit, the branch and the resume command
    # (AC2/AC6) — the second attempt deduplicates instead of stacking one.
    records = _with_prefix(harness.comments, implement.INTERRUPTION_COMMENT_MARKER)
    assert len(records) == 1, records
    assert _COMMIT in records[0]
    assert "pushed to dev" in records[0]
    assert "on branch" in records[0]
    assert f"implement.py finish {_ITEM_ID}" in records[0]
    # The run never posted its completion comment (the interruption precedes it).
    assert not _with_prefix(harness.comments, implement.COMPLETION_COMMENT_MARKER)
    # The report carries the record as structured evidence too.
    assert first["steps"]["interruption_comment"]["recorded"] is True
    assert second["steps"]["interruption_comment"]["deduplicated"] is True


# ---------------------------------------------------------------------------
# AC4 — re-running finish converges without duplicating anything
# ---------------------------------------------------------------------------


def test_rerun_after_a_lost_terminal_write_converges_without_duplicates(
    tmp_path,
):
    """AC4: the re-run completes the lifecycle exactly once.

    Run 1 posts its completion comment, then its terminal lifecycle write is
    lost: the read-back fails loudly, the item is released to ``open`` with no
    assignee and the retained state is recorded. Run 2 converges to
    ``completed`` / ``in_review`` — reusing the ordinary push/no-diff path —
    without a duplicate completion comment, a duplicate interruption record or
    a second terminal transition.
    """
    with _finish_harness(tmp_path) as harness:
        harness.ctx["drop_terminal_write"] = True
        first = harness.run()

        assert first["success"] is False
        assert "does not hold the terminal transition" in first["message"]
        assert harness.item["status"] == "open"
        assert harness.item["assignee"] == ""
        # The completion comment ran before the lost write; the interruption
        # record documents the retained state.
        assert len(
            _with_prefix(harness.comments, implement.COMPLETION_COMMENT_MARKER)
        ) == 1
        assert len(
            _with_prefix(harness.comments, implement.INTERRUPTION_COMMENT_MARKER)
        ) == 1
        assert not [
            write
            for write in harness.writes
            if write["status"] == "completed"
        ], "the first run's terminal write was lost, not applied"

        writes_before_rerun = len(harness.writes)
        second = harness.run()

    assert second["success"] is True, second["message"]
    # The converged run completes the lifecycle…
    assert harness.item["status"] == "completed"
    assert harness.item["stage"] == "in_review"
    assert harness.item["assignee"] == ""
    # …exactly once, and without re-posting either record.
    assert second["steps"]["completion_comment"]["posted"] is False
    assert second["steps"]["completion_comment"]["deduplicated"] is True
    assert len(
        _with_prefix(harness.comments, implement.COMPLETION_COMMENT_MARKER)
    ) == 1
    assert len(
        _with_prefix(harness.comments, implement.INTERRUPTION_COMMENT_MARKER)
    ) == 1
    rerun_writes = harness.writes[writes_before_rerun:]
    assert [
        write
        for write in rerun_writes
        if write["status"] == "completed" and write["stage"] == "in_review"
    ], "the re-run must perform the terminal transition"
    assert len(
        [
            write
            for write in rerun_writes
            if write["status"] == "completed" and write["stage"] == "in_review"
        ]
    ) == 1, "the re-run must not duplicate the terminal transition"


def test_rerun_after_a_push_only_interruption_converges(tmp_path):
    """AC4: a failure before the completion comment also converges.

    The interrupted run released the item; the re-run reaches the terminal
    state and posts the (single) completion comment, so recovery never depends
    on the interruption having got as far as the comment.
    """
    with _finish_harness(tmp_path) as harness:
        with patch.object(
            implement,
            "_sync_parent_branch",
            side_effect=RuntimeError("simulated interruption after push"),
        ):
            first = harness.run()
        assert first["success"] is False
        assert not _with_prefix(
            harness.comments, implement.COMPLETION_COMMENT_MARKER
        )

        second = harness.run()

    assert second["success"] is True, second["message"]
    assert harness.item["status"] == "completed"
    assert harness.item["stage"] == "in_review"
    assert harness.item["assignee"] == ""
    assert second["steps"]["completion_comment"]["posted"] is True
    # One completion comment, plus the interruption record from the first run.
    assert len(
        _with_prefix(harness.comments, implement.COMPLETION_COMMENT_MARKER)
    ) == 1
    assert len(
        _with_prefix(harness.comments, implement.INTERRUPTION_COMMENT_MARKER)
    ) == 1


# ---------------------------------------------------------------------------
# AC1/AC3/AC4 — an interrupted-push re-run attributes the prior-run commit,
# driven through the REAL ``git_commit`` boundary (not a truthy stub)
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    return gs.run_git(repo, args, check=True).stdout


def _sandbox_with_unpushed_item_commit(tmp_path: Path) -> tuple[Path, Path, str]:
    """Build the interrupted-push state: clean worktree, commit ahead of origin.

    Models the live 2026-11-09 incident (SA-0MW74IKJE008JXDQ): the item commit
    lives on the worktree branch **and** on local ``dev`` but was never pushed
    to ``origin/dev``. The worktree is therefore clean, and a re-run of
    ``finish`` creates no new commit — it must attribute the prior-run commit
    instead of refusing (AC1/AC4 i).

    Returns:
        ``(main_checkout, worktree, short_commit_hash)``.
    """
    origin = gs.init_bare_remote(tmp_path / "origin.git")
    main = gs.init_repo(tmp_path / "main")
    (main / "README.md").write_text("base\n", encoding="utf-8")
    (main / ".gitignore").write_text(".implement_state.json\n", encoding="utf-8")
    gs.commit_all(main, "base")
    gs.add_remote(main, "origin", str(origin))
    gs.run_git(main, ["push", "-q", "origin", "dev"], check=True)

    # The prior run's commit: on local dev, never on origin/dev (the push was
    # interrupted). The message references the item id, so it is attributable.
    (main / "feature.txt").write_text("work\n", encoding="utf-8")
    full = gs.commit_all(main, f"{_ITEM_ID}: Implementation complete")

    # The worktree forks from dev (HEAD == the item commit) and is clean.
    worktree = gs.add_worktree(
        main, tmp_path / "wt" / "wl-item", f"wl-{_ITEM_ID}-recovery", base="dev"
    )
    short = _git(main, "rev-parse", "--short", full).strip()
    return main, worktree, short


@contextlib.contextmanager
def _real_git_finish_harness(worktree: Path, main: Path):
    """Drive ``phase_finish`` over real git with ``git_commit`` unstubbed.

    Unlike :func:`_finish_harness`, ``git_commit``, ``git_get_commit_hash`` and
    the attribution helper run for real against the fixture repository, so the
    interrupted-push re-run genuinely crosses the commit boundary (AC4 iii) and
    a truthy stub can no longer mask it. Only the expensive, network and
    lifecycle boundaries are patched.
    """
    state = implement.ImplementState(
        work_item_id=_ITEM_ID,
        worktree_path=str(worktree),
        repo_root=str(main),
        parent_branch="dev",
    )
    # A modelled store: the lifecycle helper reflects the item's live state, so
    # its exception-path restore is honest (restoring a genuinely non-terminal
    # item) and the success-path read-back sees the terminal transition.
    item: dict = {
        "status": "in-progress",
        "stage": "plan_complete",
        "assignee": "implement",
    }
    comments: list[dict] = []
    progress: list[dict] = []
    updates: list[tuple[tuple, dict]] = []

    def _update_status(work_item_id, *args, **kwargs):
        updates.append((args, kwargs))
        status = args[0] if args else kwargs.get("status")
        if status is not None:
            item["status"] = str(status)
        stage = kwargs.get("stage")
        if stage is not None:
            item["stage"] = str(stage)
        if kwargs.get("assignee") is not None:
            item["assignee"] = str(kwargs["assignee"])
        return {"success": True}

    def _add_comment(work_item_id, body):
        comments.append({"comment": str(body), "author": "implement"})
        return True

    def _show(work_item_id, **kwargs):
        return {
            "workItem": dict(item),
            "comments": [dict(entry) for entry in comments],
        }

    push = MagicMock(return_value=True)
    real_set_progress = implement._set_finish_progress

    def _capture_progress(**fields):
        progress.append(dict(fields))
        real_set_progress(**fields)

    with contextlib.ExitStack() as stack:
        stack.enter_context(
            patch.object(
                implement, "_discover_worktree", return_value=str(worktree)
            )
        )
        stack.enter_context(patch.object(implement, "read_state", return_value=state))
        stack.enter_context(
            patch.object(implement, "_worktree_placement_violation", return_value=None)
        )
        stack.enter_context(
            patch.object(implement, "run_build", return_value=_build_result())
        )
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
        stack.enter_context(
            patch.object(
                implement,
                "_fast_forward_local_dev_for_push",
                return_value={"success": True, "own_churn": [], "method": "stub"},
            )
        )
        stack.enter_context(patch.object(implement, "_assert_push_subject"))
        stack.enter_context(patch.object(implement, "git_push_to_dev", push))
        stack.enter_context(
            patch.object(
                implement,
                "_assert_commit_on_origin_dev",
                return_value={"verified": True},
            )
        )
        stack.enter_context(
            patch.object(
                implement,
                "_resolve_post_push_verification",
                return_value=_green_verification(),
            )
        )
        stack.enter_context(
            patch.object(
                implement, "cleanup_worktree_processes", return_value=_cleanup_result()
            )
        )
        stack.enter_context(patch.object(implement, "remove_state"))
        stack.enter_context(
            patch.object(implement, "_remove_worktree", return_value=True)
        )
        stack.enter_context(
            patch.object(
                implement,
                "_sync_parent_branch",
                return_value={"method": "skipped", "success": True},
            )
        )
        stack.enter_context(
            patch.object(implement, "wl_add_comment", side_effect=_add_comment)
        )
        stack.enter_context(patch.object(implement, "_surface_postfix_prose"))
        stack.enter_context(
            patch.object(
                implement, "_set_finish_progress", side_effect=_capture_progress
            )
        )
        stack.enter_context(
            patch.object(
                implement.StatusLifecycle,
                "update_status",
                MagicMock(side_effect=_update_status),
            )
        )
        stack.enter_context(
            patch.object(
                implement.StatusLifecycle, "show", MagicMock(side_effect=_show)
            )
        )
        yield SimpleNamespace(
            run=lambda: implement.phase_finish(
                _ITEM_ID, json_output=True, no_refactor=True
            ),
            comments=comments,
            progress=progress,
            updates=updates,
            push=push,
        )


def test_interrupted_push_rerun_attributes_the_prior_commit(
    tmp_path, monkeypatch
):
    """AC1/AC4 i+iii: a clean re-run publishes and finalises the prior commit.

    The worktree is clean and one commit ahead of ``origin/dev``; the real
    ``git_commit`` therefore creates nothing (``CommitOutcome.committed`` is
    False). ``finish`` must attribute that prior-run commit, name it in the
    report, the completion comment, the retained-state record and the push,
    and converge to ``completed`` / ``in_review``.
    """
    monkeypatch.delenv(implement.DRIVE_RECURSION_ENV, raising=False)
    monkeypatch.delenv(implement.KEEP_WORKTREE_ENV, raising=False)

    main, worktree, short = _sandbox_with_unpushed_item_commit(tmp_path)

    # Pre-condition (guards against a vacuous test): clean tree, one commit
    # ahead of origin/dev, absent from origin/dev, message references the item.
    assert _git(worktree, "status", "--porcelain").strip() == ""
    assert _git(worktree, "rev-list", "--count", "origin/dev..HEAD").strip() == "1"
    assert short not in _git(worktree, "rev-parse", "origin/dev")

    with _real_git_finish_harness(worktree, main) as harness:
        report = harness.run()

    assert report["success"] is True, report["message"]

    commit_step = report["steps"]["commit"]
    assert commit_step["created"] is False, "the re-run must not create a commit"
    assert commit_step["attributed"] == "prior_run"
    assert commit_step["hash"] == short
    assert report["hash"] == short
    assert short in report["message"]

    # The push path published the attributed commit (AC1).
    assert harness.push.call_args.args[2] == short

    # The completion comment names the prior-run commit (AC1).
    completion = _with_prefix(
        harness.comments, implement.COMPLETION_COMMENT_MARKER
    )
    assert len(completion) == 1, completion
    assert f"Commit: {short}" in completion[0]
    assert "No changes" not in completion[0]

    # The retained-state interruption record names it too (AC1).
    assert any(entry.get("commit") == short for entry in harness.progress)

    # It converged to the terminal state.
    assert any(
        kwargs.get("status") == "completed"
        and kwargs.get("stage") == "in_review"
        for _args, kwargs in harness.updates
    )
    assert report["steps"]["status_readback"]["success"] is True


def test_rerun_with_no_attributable_commit_still_refuses(tmp_path, monkeypatch):
    """AC2/AC4 ii: a genuinely work-less worktree is still refused.

    A clean worktree whose only commit ahead of ``origin/dev`` does **not**
    reference the item is the ambient-branch false positive. ``finish`` must
    refuse with ``no_commit_no_signal``, push nothing, post no completion
    comment and write no terminal transition — proving the attribution cannot
    be widened into claiming another item's commit.
    """
    monkeypatch.delenv(implement.DRIVE_RECURSION_ENV, raising=False)
    monkeypatch.delenv(implement.KEEP_WORKTREE_ENV, raising=False)

    origin = gs.init_bare_remote(tmp_path / "origin.git")
    main = gs.init_repo(tmp_path / "main")
    (main / "README.md").write_text("base\n", encoding="utf-8")
    (main / ".gitignore").write_text(".implement_state.json\n", encoding="utf-8")
    gs.commit_all(main, "base")
    gs.add_remote(main, "origin", str(origin))
    gs.run_git(main, ["push", "-q", "origin", "dev"], check=True)

    # A commit ahead of origin/dev that references a DIFFERENT work item.
    (main / "foreign.txt").write_text("someone else's work\n", encoding="utf-8")
    gs.commit_all(main, "SA-OTHER9999: implementation")
    worktree = gs.add_worktree(
        main, tmp_path / "wt" / "wl-item", f"wl-{_ITEM_ID}-recovery", base="dev"
    )

    with _real_git_finish_harness(worktree, main) as harness:
        report = harness.run()

    assert report["success"] is False, report
    assert report["steps"]["commit"]["reason"] == "no_commit_no_signal"
    assert report["steps"]["commit"]["hash"] == ""
    # Nothing was published and no completion comment was posted.
    harness.push.assert_not_called()
    assert not _with_prefix(
        harness.comments, implement.COMPLETION_COMMENT_MARKER
    )
    # No terminal transition; the item was released to open.
    assert not any(
        kwargs.get("status") == "completed" for _args, kwargs in harness.updates
    )
    # ``_update_status`` binds the id to its own parameter, so the remaining
    # positional args start at index 0 (``("open",)`` for the reset).
    assert any(args and args[0] == "open" for args, _kwargs in harness.updates)
    refusal = [
        body
        for body in _comment_bodies(harness.comments)
        if "no_commit_no_signal" in body
    ]
    assert refusal, harness.comments
    # AC3: the wording no longer claims "nothing was committed or pushed".
    assert "nothing was committed or pushed" not in refusal[0]
