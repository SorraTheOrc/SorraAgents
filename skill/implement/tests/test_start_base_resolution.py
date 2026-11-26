
# <!-- REFACTOR-SA-0MWI3MWFR009NQ3J
# smell: simplification
# severity: medium
# description: Use a single `with` statement with multiple contexts instead of nested `with` statements
# -->

# <!-- REFACTOR-SA-0MWI3MUWU0099KF5
# smell: ruff_specific
# severity: medium
# description: Unpacked variable `base` is never used
# -->
"""Regression tests: ``start`` never bases a worktree on an unpushed ``dev``.

WL-0MWFKDH78002T2CR AC3/AC4. ``implement.py finish`` used to fast-forward the
main checkout's local ``dev`` to the about-to-be-pushed tip *before* the push
succeeded, then restore it on failure. A concurrent ``start`` inside that
window resolved ``dev`` to the unpushed commit and created its worktree/branch
from it — permanently grafting another item's commit onto the new branch.

``_resolve_start_base`` closes the start side of the race: local ``dev`` must
be a **known pushed tip** (equal to the fetched ``refs/remotes/origin/dev``,
or an ancestor of it) before ``phase_start`` will fork a worktree from it.
Positive evidence of unpushed commits — local ``dev`` ahead of, or diverged
from, ``origin/dev`` — **refuses** the start (fail-closed: no worktree, item
released to ``open``). Only an unresolvable remote tip (fetch failure) falls
back to the historical fail-open local-``dev`` base, mirroring the existing
refresh's offline contract.

The helper-level tests run against real ``git`` repositories
(``skill.shared.git_sandbox``); the phase-level test drives
``phase_start``'s worktree-creation step with the work-item store stubbed.
"""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) in sys.path:
    sys.path.remove(str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT))

from skill.implement.scripts import implement
from skill.shared import git_sandbox as gs


def _rev(cwd: Path, ref: str = "HEAD") -> str:
    return gs.run_git(cwd, ["rev-parse", ref], check=True).stdout.strip()


def _commit(repo: Path, filename: str, content: str, msg: str) -> str:
    path = repo / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    gs.run_git(repo, ["add", filename], check=True)
    gs.run_git(repo, ["commit", "-q", "-m", msg], check=True)
    return _rev(repo)


def _repo_on_dev(tmp_path: Path) -> tuple[Path, Path, str]:
    """A bare origin and a ``dev`` main checkout, both at one commit."""
    remote = gs.init_bare_remote(tmp_path / "origin.git")
    main = gs.init_repo(tmp_path / "main", default_branch="dev")
    (main / ".gitignore").write_text(".worklog/\n", encoding="utf-8")
    base = _commit(main, "README.md", "seed\n", "seed")
    gs.add_remote(main, "origin", str(remote))
    gs.run_git(main, ["push", "-q", "origin", "dev"], check=True)
    return remote, main, base


# ---------------------------------------------------------------------------
# _resolve_start_base — pushed-tip verification (AC3)
# ---------------------------------------------------------------------------


def test_base_is_local_dev_when_in_sync(tmp_path: Path):
    """local ``dev`` == the fetched ``origin/dev``: safe, base stays `dev`."""
    _remote, main, base = _repo_on_dev(tmp_path)

    result = implement._resolve_start_base(str(main), "dev")

    assert result["method"] == "local_dev", result
    assert result["base"] == "dev", result
    assert result["reason"] == "in_sync", result
    assert result["local_tip"] == base == result["remote_tip"], result


def test_base_forks_from_origin_when_behind_remote(tmp_path: Path):
    """local ``dev`` behind ``origin/dev``: fork the published tip, not the stale local ref.

    A stale local base would leave the worktree behind published commits; the
    published tip includes them, so AC3 (worktree already carries the fix)
    holds without a manual rebase.
    """
    remote, main, _base = _repo_on_dev(tmp_path)
    # A sibling lands on origin/dev; local dev stays behind.
    _commit(main, "sibling.txt", "s\n", "sibling")
    gs.run_git(main, ["push", "-q", "origin", "dev"], check=True)
    gs.run_git(main, ["reset", "-q", "--hard", "HEAD~1"], check=True)
    assert _rev(main, "dev") != _rev(remote, "dev")

    result = implement._resolve_start_base(str(main), "dev")

    assert result["method"] == "origin_fork", result
    assert result["base"] == "refs/remotes/origin/dev", result
    assert result["reason"] == "behind_remote", result
    assert result["behind"] >= 1, result


def test_base_forks_from_origin_when_local_has_unpushed_commits(tmp_path: Path):
    """local ``dev`` ahead of ``origin/dev``: fork the published tip (graft-safe).

    This is the exact finish-window shape: local ``dev`` carries a commit the
    remote does not have.  Forking from ``origin/dev`` never grafts that commit
    onto the new branch, so the helper proposes the published tip and reports
    the ahead/behind counts — the local ref is left untouched.
    """
    remote, main, _base = _repo_on_dev(tmp_path)
    _commit(main, "foreign.txt", "f\n", "foreign unpushed commit")
    local_before = _rev(main, "dev")
    assert local_before != _rev(remote, "dev")

    result = implement._resolve_start_base(str(main), "dev")

    assert result["method"] == "origin_fork", result
    assert result["base"] == "refs/remotes/origin/dev", result
    assert result["reason"] == "ahead_only", result
    assert result["ahead"] >= 1, result
    assert _rev(main, "dev") == local_before  # never force-updated


def test_base_forks_from_origin_when_diverged(tmp_path: Path):
    """local ``dev`` diverged from ``origin/dev``: fork the published tip."""
    _remote, main, _base = _repo_on_dev(tmp_path)
    # Sibling lands on origin/dev, then our local dev is rewritten onto the
    # pre-sibling commit with a different history: diverged, not ahead/behind.
    _commit(main, "sibling.txt", "s\n", "sibling")
    gs.run_git(main, ["push", "-q", "origin", "dev"], check=True)
    gs.run_git(main, ["reset", "-q", "--hard", "HEAD~1"], check=True)
    _commit(main, "local.txt", "l\n", "local rewrite")
    assert implement._resolve_full_commit(str(main), "origin/dev") != implement._resolve_full_commit(
        str(main), "dev"
    )

    result = implement._resolve_start_base(str(main), "dev")

    assert result["method"] == "origin_fork", result
    assert result["base"] == "refs/remotes/origin/dev", result
    assert result["reason"] == "diverged", result


def test_base_fails_open_when_the_fetch_fails(tmp_path: Path, monkeypatch):
    """An unresolvable remote tip cannot be *proven* unsafe: fail open.

    Mirrors the parent-refresh's offline contract: start is never blocked by
    an unreachable remote, so the historical local-``dev`` base is retained
    with a loud warning.
    """
    _remote, main, _base = _repo_on_dev(tmp_path)

    import subprocess

    real_run_cmd = implement.run_cmd

    def fake_run_cmd(cmd, **kwargs):
        if (
            isinstance(cmd, (list, tuple))
            and len(cmd) >= 3
            and cmd[0] == "git"
            and cmd[1] == "fetch"
        ):
            return subprocess.CompletedProcess(
                args=cmd, returncode=128, stdout="", stderr="fatal: offline"
            )
        return real_run_cmd(cmd, **kwargs)

    monkeypatch.setattr(implement, "run_cmd", fake_run_cmd)

    result = implement._resolve_start_base(str(main), "dev")

    assert result["method"] == "local_dev", result
    assert result["base"] == "dev", result
    assert result["reason"] == "fetch_failed", result
    assert "warning" in result


def test_refresh_synced_short_circuits_without_an_extra_fetch(
    tmp_path: Path, monkeypatch
):
    """When the Step-6.5 refresh already synced, no second fetch runs.

    ``_sync_parent_branch``'s ``synced`` result means local ``dev`` == the
    just-fetched ``origin/dev``; re-fetching (and re-verifying) here would be
    pure redundancy on the happy path.
    """
    _remote, main, _base = _repo_on_dev(tmp_path)
    calls: list[list[str]] = []

    def recording(cmd, **kwargs):
        if isinstance(cmd, (list, tuple)) and cmd[:2] == ["git", "fetch"]:
            calls.append(list(cmd))
        return gs.run_git(main, cmd, check=False) if cmd else None

    monkeypatch.setattr(implement, "run_cmd", recording)

    result = implement._resolve_start_base(
        str(main), "dev", refresh_result={"method": "synced", "success": True}
    )

    assert result["method"] == "local_dev", result
    assert result["reason"] == "refresh_synced", result
    assert calls == [], f"a synced refresh must not trigger another fetch: {calls}"


# ---------------------------------------------------------------------------
# phase_start — a refused base aborts the start (AC3/AC4)
# ---------------------------------------------------------------------------


def _clean_stash_result() -> dict:
    return {
        "total_stashes": 0,
        "orphaned_stashes": [],
        "matched_stashes": [],
        "has_orphaned": False,
        "warning": None,
    }


@contextlib.contextmanager
def _phase_start_harness(*, work_item, repo_root: str):
    """Stub every external boundary of ``phase_start`` except the base check.

    The main-checkout pre-check, the claim, the stash gate, the refresh,
    node_modules symlinks and state writes are stubbed; ``_resolve_start_base``
    and ``git_worktree_add`` run for real against the fixture repositories so
    the AC4 assertion ("the new branch's base is the pushed tip, never an
    unpushed sibling commit") is observable.
    """
    with contextlib.ExitStack() as stack:
        stack.enter_context(
            patch.object(implement, "is_code_freeze_active", return_value=False)
        )
        stack.enter_context(patch.object(implement, "git_status", return_value="## dev\n"))
        stack.enter_context(
            patch.object(
                implement,
                "_current_checkout_is_linked_worktree",
                return_value=False,
            )
        )
        stack.enter_context(
            patch.object(implement, "git_has_dirty_files", return_value=False)
        )
        stack.enter_context(
            patch.object(
                implement, "check_orphaned_stashes", return_value=_clean_stash_result()
            )
        )
        stack.enter_context(patch.object(implement, "wl_show", return_value=work_item))
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
        stack.enter_context(patch.object(implement, "_get_repo_root", return_value=repo_root))
        stack.enter_context(
            patch.object(
                implement,
                "_sync_parent_branch_locked",
                return_value={"method": "skipped", "success": True, "reason": "test"},
            )
        )
        stack.enter_context(patch.object(implement, "_is_worktree", return_value=False))
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
        stack.enter_context(patch.object(implement, "write_state"))
        stack.enter_context(
            patch.object(implement, "warn_self_referential_upstream", return_value={"status": "ok"})
        )
        update_status = MagicMock()
        stack.enter_context(
            patch.object(implement.StatusLifecycle, "update_status", update_status)
        )
        add_comment = MagicMock(return_value=True)
        stack.enter_context(patch.object(implement, "wl_add_comment", add_comment))
        stack.enter_context(patch.object(implement, "_surface_extension_prose"))
        yield {"update_status": update_status, "wl_add_comment": add_comment}


_WORK_ITEM = {
    "id": "WL-TESTBASE",
    "title": "base resolution",
    "risk": "Medium",
    "effort": "Small",
}


def test_phase_start_forks_from_origin_when_local_dev_has_unpushed_commits(
    tmp_path: Path,
):
    """Local ``dev`` parked on an unpushed commit: fork ``origin/dev`` (never refuse).

    Reproduces the WL-0MWFKDH78002T2CR finish-window shape, but the approved
    policy is to fork the published tip rather than refuse: the worktree is
    created from ``refs/remotes/origin/dev`` so the foreign commit can never
    become the branch base, the item stays claimed, and the recorded base ref
    names the published tip.
    """
    _remote, main, _base = _repo_on_dev(tmp_path)
    _commit(main, "foreign.txt", "f\n", "unpushed foreign commit")
    origin_tip = _rev(main, "origin/dev")

    created: list[tuple] = []
    comments: list[str] = []

    def recording_worktree_add(branch, path, base):
        created.append((branch, Path(path), base))
        gs.run_git(
            main,
            ["worktree", "add", "--track", "-b", branch, str(path), base],
            check=True,
        )
        return True

    def recording_add_comment(_work_item_id, comment, *args, **kwargs):
        comments.append(comment)
        return True

    with _phase_start_harness(
        work_item=_WORK_ITEM, repo_root=str(main)
    ) as mocks, patch.object(
        implement, "git_worktree_add", side_effect=recording_worktree_add
    ):
        mocks["wl_add_comment"].side_effect = recording_add_comment
        report = implement.phase_start(
            "WL-TESTBASE", json_output=True, no_refactor=True
        )

    assert report["success"] is True, report
    assert not report.get("worktree_refused"), report
    assert len(created) == 1, created
    branch, wt_path, base_ref = created[0]
    assert base_ref == "refs/remotes/origin/dev", base_ref
    assert report["steps"]["worktree_base"]["ref"] == "refs/remotes/origin/dev"
    # The new branch is based on the published tip, never the foreign commit.
    assert _rev(main, f"refs/heads/{branch}") == origin_tip
    assert _rev(wt_path) == origin_tip

    # A successful origin-fork start keeps the claim — it is not released.
    open_calls = [
        c
        for c in mocks["update_status"].call_args_list
        if len(c.args) > 1 and c.args[1] == "open"
    ]
    assert not open_calls, (
        "a successful origin-fork start must not release the claim; "
        f"status updates: {mocks['update_status'].call_args_list}"
    )
    assert any("refs/remotes/origin/dev" in c for c in comments), comments
    # Clean up the real worktree created by this test.
    gs.run_git(main, ["worktree", "remove", "--force", str(wt_path)], check=True)


def test_phase_start_bases_a_worktree_on_the_pushed_tip(tmp_path: Path):
    """Healthy local ``dev`` (a known pushed tip): the worktree forks from it.

    AC4 positive companion: with ``dev`` == ``origin/dev``, the created
    branch's base is exactly the pushed tip — never a foreign commit.
    """
    _remote, main, base = _repo_on_dev(tmp_path)

    created: list[tuple] = []

    def recording_worktree_add(branch, path, base_ref):
        created.append((branch, Path(path), base_ref))
        # a real registration, so the observable base is the created branch's
        # HEAD and upstream — not just the recorded argument
        gs.run_git(main, ["worktree", "add", "--track", "-b", branch, str(path), base_ref], check=True)
        return True

    with _phase_start_harness(work_item=_WORK_ITEM, repo_root=str(main)), \
         patch.object(
             implement, "git_worktree_add", side_effect=recording_worktree_add
         ):
        report = implement.phase_start(
            "WL-TESTBASE", json_output=True, no_refactor=True
        )

    assert report["success"] is True, report["message"]
    assert len(created) == 1, created
    branch, wt_path, base_ref = created[0]
    assert branch.startswith("wl-WL-TESTBASE-"), branch
    assert base_ref == "dev", base_ref
    # The branch's base is the pushed tip (== origin/dev), never a foreign
    # unpushed commit: there is none, and the recorded base is `dev` which the
    # helper verified equals the fetched remote tip.
    assert _rev(main, f"refs/heads/{branch}") == base, (
        f"the new branch must be based on the pushed tip {base}; "
        f"got {_rev(main, f'refs/heads/{branch}')}"
    )
    assert _rev(wt_path) == base
    # Clean up the real worktree created by this test.
    gs.run_git(main, ["worktree", "remove", "--force", str(wt_path)], check=True)
