"""Plain-leaf ``finish`` deferral: keep the cwd, report it, clean it up later.

The driven half of the deferral contract (``IMPLEMENT_DRIVE_ACTIVE=1``) is
delivered and pinned by ``test_finish_deferred_worktree_removal.py``
(SA-0MVTE8XAJ002IUDR). This file pins the **plain-leaf half**
(SA-0MVTHJ3PJ000IVSK): a session that opted in with ``finish
--keep-worktree`` keeps a usable cwd after ``finish`` returns, and the
deferral is loud enough that the follow-up cleanup always happens.

Contract pinned here:

- **AC2 — cleanup is deterministic and named.** The retained path and the
  exact reviewed follow-up action (``implement.py cleanup <work-item-id>``)
  appear in the completion comment, the human output and the JSON report
  (``steps.worktree_cleanup``); finish still reaches its terminal state.
- **AC3 — the default flow is unchanged.** Without the switches, finish runs
  ``cleanup_worktree_processes`` + ``_remove_worktree``, reports
  ``worktree_removed: true`` and no follow-up command.
- **AC4 — interruption safety.** Re-running ``phase_finish`` on a retained
  worktree duplicates no state and removes nothing; ``phase_start`` resumes
  onto the retained worktree instead of failing on ``git worktree add``; and
  the named ``implement.py cleanup <id>`` action removes it behind the shared
  safety gates.

The finish tests drive ``phase_finish`` with its external boundaries (git,
the ``wl`` store, build/test runners, process cleanup and worktree removal)
patched — the same patched-boundary style as ``test_finish_no_diff_status.py``
— and assert observable behaviour only. The resume/cleanup tests use a **real**
temporary git repository and a **real** linked worktree (the fixture pattern
of ``test_repo_root_resolution.py``), because their subject is what git
actually does with a retained tree.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) in sys.path:
    sys.path.remove(str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT))

from shared.git_sandbox import fixture_git_env

from skill.implement.scripts import implement

#: Stable full commit hash the fake git resolves every revision to (the
#: pre-push fast-forward and the post-push ancestry assertion both
#: ``rev-parse``).
_FAKE_COMMIT = "1" * 40

ITEM_ID = "SA-PLAINLEAF1"
TITLE = "Plain flow deferral regression"

#: The follow-up action the plain-leaf session must be told to run.
_EXPECTED_CLEANUP_COMMAND = (
    f"python3 skill/implement/scripts/implement.py cleanup {ITEM_ID}"
)

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "Implement Tests",
    "GIT_AUTHOR_EMAIL": "tests@example.invalid",
    "GIT_COMMITTER_NAME": "Implement Tests",
    "GIT_COMMITTER_EMAIL": "tests@example.invalid",
}


def _fake_run_cmd(cmd: list[str], *args, **kwargs) -> subprocess.CompletedProcess:
    """Stand-in for ``run_cmd``: every git sub-command "succeeds"."""
    stdout = f"{_FAKE_COMMIT}\n" if len(cmd) > 1 and cmd[1] == "rev-parse" else ""
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


def _gate_tree() -> dict:
    """The would-be-commit summary ``run_gate_suite`` records on the report."""
    return {
        "tree": "abc1234",
        "scope": "changed",
        "cached": False,
        "index_unchanged": True,
    }


def _cleanup_result() -> dict:
    return {"method": "wl_cleanup_worktree", "terminated": 0, "warning": ""}


@pytest.fixture(autouse=True)
def _clean_deferral_env(monkeypatch):
    """Neutralise ambient deferral switches inherited from the host.

    The full suite may run inside a driven child session
    (``IMPLEMENT_DRIVE_ACTIVE=1``); removing it keeps the plain-leaf tests
    hermetic. Tests that need a switch set it explicitly.
    """
    monkeypatch.delenv(implement.DRIVE_RECURSION_ENV, raising=False)
    monkeypatch.delenv(implement.KEEP_WORKTREE_ENV, raising=False)


@contextlib.contextmanager
def _mocked_phase_finish(tmp_path: Path):
    """Patch every external boundary of ``phase_finish``.

    ``remove_state`` is left real (it only unlinks the state file) so the
    tests observe whether the retained state file survives; the process
    cleanup and worktree removal are mocked so the fixture tree is never
    destroyed and their calls can be counted.
    """
    worktree = str(tmp_path)
    state = implement.ImplementState(
        work_item_id=ITEM_ID,
        worktree_path=worktree,
        repo_root="/tmp/repo-root",
        parent_branch="dev",
    )
    state_file = tmp_path / implement.STATE_FILE_NAME
    state_file.write_text("{}")
    comments: list[str] = []
    update_status = MagicMock()

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
                return_value=(_tests_result(), _gate_tree()),
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
            patch.object(implement, "run_tests", return_value=_tests_result())
        )
        stack.enter_context(patch.object(implement, "git_commit", return_value=True))
        stack.enter_context(
            patch.object(implement, "git_get_commit_hash", return_value="abc1234")
        )
        stack.enter_context(
            patch.object(implement, "git_push_to_dev", return_value=True)
        )
        stack.enter_context(patch.object(implement, "run_cmd", side_effect=_fake_run_cmd))
        cleanup = stack.enter_context(
            patch.object(
                implement, "cleanup_worktree_processes", return_value=_cleanup_result()
            )
        )
        removal = stack.enter_context(
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
            patch.object(
                implement,
                "wl_add_comment",
                side_effect=lambda wid, text: comments.append(text) or True,
            )
        )
        stack.enter_context(patch.object(implement, "_surface_postfix_prose"))
        stack.enter_context(
            patch.object(implement.StatusLifecycle, "update_status", update_status)
        )
        stack.enter_context(
            patch.object(
                implement.StatusLifecycle,
                "show",
                MagicMock(
                    return_value={
                        "workItem": {"status": "completed", "stage": "in_review"}
                    }
                ),
            )
        )
        yield {
            "worktree": tmp_path,
            "state_file": state_file,
            "cleanup": cleanup,
            "removal": removal,
            "comments": comments,
            "update_status": update_status,
        }


def _run_finish(tmp_path: Path, *, json_output: bool, keep_worktree: bool = True):
    """Run one ``phase_finish`` against the patched-boundary harness."""
    with _mocked_phase_finish(tmp_path) as mocks:
        report = implement.phase_finish(
            ITEM_ID,
            json_output=json_output,
            no_refactor=True,
            keep_worktree=keep_worktree,
        )
    return report, mocks


# ---------------------------------------------------------------------------
# AC2 — the retained worktree and its cleanup action are never silent
# ---------------------------------------------------------------------------


def test_plain_keep_worktree_human_output_names_the_cleanup_action(
    tmp_path, capsys
):
    """The plain-leaf human output names the retained tree and its remedy.

    A session reading only the operator-facing output must be able to finish
    the job: path, reason and the exact ``cleanup`` command.
    """
    report, _ = _run_finish(tmp_path, json_output=False)

    assert report["success"] is True, report["message"]
    out = capsys.readouterr().out
    assert str(tmp_path) in out
    assert "keep-worktree-flag" in out
    assert _EXPECTED_CLEANUP_COMMAND in out


def test_plain_keep_worktree_json_report_carries_the_cleanup_command(
    tmp_path, capsys
):
    """The JSON report (returned and printed) carries the follow-up action."""
    report, _ = _run_finish(tmp_path, json_output=True)

    assert report["success"] is True, report["message"]
    cleanup_step = report["steps"]["worktree_cleanup"]
    assert cleanup_step == {
        "deferred": True,
        "path": str(tmp_path),
        "reason": "keep-worktree-flag",
        "command": _EXPECTED_CLEANUP_COMMAND,
    }
    # The removal record keeps its established shape (driven tests rely on it).
    assert report["steps"]["worktree_removal"]["deferred"] is True
    assert report["steps"]["worktree_removed"] is False
    assert _EXPECTED_CLEANUP_COMMAND in capsys.readouterr().out


def test_plain_keep_worktree_completion_comment_names_the_cleanup_action(
    tmp_path,
):
    """The work item itself records the retained tree and the cleanup command."""
    _, mocks = _run_finish(tmp_path, json_output=True)

    completion = "\n".join(mocks["comments"])
    assert "Worktree retained" in completion
    assert _EXPECTED_CLEANUP_COMMAND in completion
    # The manual fallback stays available next to the reviewed action.
    assert "git worktree remove" in completion
    assert "git worktree prune" in completion


def test_plain_keep_worktree_finish_reaches_terminal_state(tmp_path):
    """Deferral must not disturb the push/terminal-status machinery."""
    report, mocks = _run_finish(tmp_path, json_output=True)

    assert report["success"] is True, report["message"]
    assert report["steps"]["push"]["success"] is True
    assert report["steps"]["status_readback"]["success"] is True
    assert report["steps"]["status_readback"]["status"] == "completed"
    assert report["steps"]["status_readback"]["stage"] == "in_review"

    terminal = [
        call
        for call in mocks["update_status"].call_args_list
        if call.kwargs.get("status") == "completed"
        and call.kwargs.get("stage") == "in_review"
    ]
    assert terminal, "the plain deferred path must issue the terminal write"


# ---------------------------------------------------------------------------
# AC3 — the default flow still removes the tree, and says so
# ---------------------------------------------------------------------------


def test_default_plain_finish_removes_and_reports_no_follow_up(tmp_path):
    """No switch → cleanup + removal run, and no cleanup command is reported.

    The removal semantics themselves are pinned by
    ``test_finish_deferred_worktree_removal.py::test_default_finish_still_removes_the_worktree``;
    this guards the new ``worktree_cleanup`` surface on the non-deferred path.
    """
    report, mocks = _run_finish(tmp_path, json_output=True, keep_worktree=False)

    assert report["success"] is True, report["message"]
    assert mocks["cleanup"].call_count == 1
    assert mocks["removal"].call_count == 1
    assert report["steps"]["worktree_removed"] is True

    cleanup_step = report["steps"]["worktree_cleanup"]
    assert cleanup_step["deferred"] is False
    assert cleanup_step["command"] == ""


# ---------------------------------------------------------------------------
# AC4 — interruption safety: re-run, resume, clean up
# ---------------------------------------------------------------------------


def test_rerunning_finish_on_the_retained_worktree_is_idempotent(tmp_path):
    """A repeat finish removes nothing and leaves exactly one state file."""
    with _mocked_phase_finish(tmp_path) as mocks:
        first = implement.phase_finish(
            ITEM_ID, json_output=True, no_refactor=True, keep_worktree=True
        )
        second = implement.phase_finish(
            ITEM_ID, json_output=True, no_refactor=True, keep_worktree=True
        )

    assert first["success"] is True, first["message"]
    assert second["success"] is True, second["message"]
    assert mocks["cleanup"].call_count == 0
    assert mocks["removal"].call_count == 0

    assert tmp_path.is_dir()
    state_files = [p for p in tmp_path.iterdir() if p.name == implement.STATE_FILE_NAME]
    assert len(state_files) == 1, (
        "a re-run must reuse the retained state, not duplicate or lose it"
    )

    reasons = [
        run["steps"]["worktree_removal"]["reason"] for run in (first, second)
    ]
    assert reasons == ["keep-worktree-flag", "keep-worktree-flag"]
    for run in (first, second):
        assert run["steps"]["worktree_cleanup"]["command"] == (
            _EXPECTED_CLEANUP_COMMAND
        )


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    """Run a real git command in *cwd* with a hermetic environment/identity."""
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        env={**fixture_git_env(), **_GIT_ENV},
    )


def _worktree_path(main: Path) -> Path:
    """The canonical worktree path ``phase_start`` computes for this item."""
    slug = implement.slug_from_title(TITLE)
    return main / implement.DEFAULT_WORKTREE_DIR / f"wl-{ITEM_ID}-{slug}"


@pytest.fixture()
def main_repo(tmp_path: Path) -> Path:
    """A real git repo on ``dev`` whose worktree bookkeeping is gitignored."""
    main = tmp_path / "main-checkout"
    main.mkdir()
    _git(main, "init", "--quiet")
    _git(main, "checkout", "--quiet", "-b", "dev")
    (main / ".gitignore").write_text(".worklog/\n.implement_state.json\n")
    (main / "README.md").write_text("main checkout\n")
    _git(main, "add", ".gitignore", "README.md")
    _git(main, "commit", "--quiet", "-m", "init")
    return main


@pytest.fixture()
def retained_worktree(main_repo: Path) -> tuple[Path, Path]:
    """The main repo plus the retained worktree a deferred finish left behind."""
    worktree = _worktree_path(main_repo)
    worktree.parent.mkdir(parents=True)
    _git(
        main_repo,
        "worktree",
        "add",
        "--quiet",
        "-b",
        f"wl-{ITEM_ID}-{implement.slug_from_title(TITLE)}",
        str(worktree),
        "dev",
    )
    state = implement.ImplementState(
        work_item_id=ITEM_ID,
        worktree_path=str(worktree),
        repo_root=str(main_repo),
        parent_branch="dev",
    )
    implement.write_state(state, str(worktree))
    return main_repo, worktree


@contextlib.contextmanager
def _mocked_phase_start():
    """Patch ``phase_start``'s external boundaries (not worktree creation).

    ``git_worktree_add`` is stubbed to fail so a wrong path is loud: the
    resume branch must reuse the existing tree and never call it.
    """
    lifecycle = MagicMock()
    with contextlib.ExitStack() as stack:
        stack.enter_context(
            patch.object(implement, "is_code_freeze_active", return_value=False)
        )
        stack.enter_context(
            patch.object(
                implement,
                "wl_show",
                return_value={
                    "id": ITEM_ID,
                    "title": TITLE,
                    "description": "## Key Files\n\n- `README.md`\n",
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
            patch.object(
                implement,
                "_sync_parent_branch",
                return_value={"method": "skipped", "success": True},
            )
        )
        add = stack.enter_context(
            patch.object(implement, "git_worktree_add", return_value=False)
        )
        stack.enter_context(patch.object(implement, "_register_signal_handlers"))
        stack.enter_context(
            patch.object(implement, "wl_add_comment", return_value=True)
        )
        stack.enter_context(patch.object(implement, "StatusLifecycle", lifecycle))
        yield {"add": add, "lifecycle": lifecycle}


def test_phase_start_resumes_on_the_retained_worktree(
    retained_worktree, monkeypatch
):
    """A second start resumes the retained tree instead of failing to add it."""
    main, worktree = retained_worktree
    monkeypatch.chdir(main)
    monkeypatch.setattr(implement, "_repo_root_global", None)

    with _mocked_phase_start() as mocks:
        report = implement.phase_start(ITEM_ID, json_output=True)

    assert report["success"] is True, report
    assert report["resumed"] is True
    assert mocks["add"].call_count == 0, (
        "a retained worktree must be reused, never re-added"
    )
    assert worktree.is_dir()

    state = json.loads((worktree / implement.STATE_FILE_NAME).read_text())
    assert state["work_item_id"] == ITEM_ID
    assert Path(state["repo_root"]).resolve() == main.resolve()


def test_cleanup_action_removes_the_retained_worktree(retained_worktree):
    """The reported ``cleanup`` action is the step that finally removes it."""
    main, worktree = retained_worktree

    with contextlib.ExitStack() as stack:
        stack.enter_context(
            patch.object(
                implement,
                "wl_show",
                return_value={"id": ITEM_ID, "status": "completed"},
            )
        )
        stack.enter_context(
            patch.object(
                implement,
                "cleanup_worktree_processes",
                return_value=_cleanup_result(),
            )
        )
        report = implement.phase_cleanup(
            ITEM_ID, json_output=True, worktree_path=str(worktree)
        )

    assert report["success"] is True, report
    assert report["removed"] is True, report
    assert report["removed_state"] is True, report
    assert not worktree.exists(), "the retained tree must be gone"
    assert (worktree / implement.STATE_FILE_NAME).exists() is False

    listed = _git(main, "worktree", "list", "--porcelain").stdout
    assert str(worktree) not in listed, "the worktree must be deregistered"
