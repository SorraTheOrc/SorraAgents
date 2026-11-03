"""Stale ``@worklog/shared`` resolution — detector and ``finish`` gate.

Parent epic **SA-0MVMZBJGT00718ZO** ("implement.py worktrees resolve
``@worklog/shared`` to the main checkout's stale ``dist``"). This module is
the **F1 test-first slice** (SA-0MVPI3JSP009BZ55); the behaviour under test is
implemented by the sibling slices:

- **SA-0MVPI400Y001FOM1** — the worktree-local link farm (pinned by
  ``test_node_modules_link_farm.py``);
- **SA-0MVPI41SR008JYXL** — extract the resolution check into a shared helper
  and make ``finish`` hard-fail when the worktree's diff touches
  ``packages/shared/src/**`` while ``@worklog/shared`` still resolves to the
  main checkout.

The contract this module pins (single source of truth for both slices):

``_shared_resolution_check(worktree_path, repo_root) -> dict``
    ``{"applicable", "ok", "package", "expected_root", "resolved_root",
    "stale_dist"}``. ``applicable`` is ``False`` for a repo with no workspace
    ``@worklog/shared`` package (degradation). When applicable,
    ``expected_root`` is the worktree's own ``packages/shared`` and ``ok`` is
    true only when the resolved package root is that same real path.

``_stale_shared_resolution(worktree_path, repo_root, parent_branch="dev")
-> str | None``
    Returns an actionable refusal message when — and only when — the
    worktree's change set (uncommitted files *and* commits ahead of the
    parent branch's merge-base) touches ``packages/shared/src/**`` **and**
    the resolution check fails. The message names the stale
    ``<main-checkout>/packages/shared/dist`` path and the remediation
    (re-run ``implement.py start``). Returns ``None`` (no-op) otherwise:
    shared source unchanged, resolution worktree-local, or no workspace
    shared package at all.

``phase_finish``
    Runs the check after the build and **before** the test gate, records it
    as ``report["steps"]["shared_resolution"]`` and refuses with
    ``success: False`` (no gate run, no commit, item released to ``open``)
    when ``_stale_shared_resolution`` returns a message.

Why this matters
----------------
``finish`` runs build → gate → commit from the worktree. With stale
resolution the gate validates the main checkout's ``dist`` instead of the
worktree's source: a correct change false-reds (WL-0MVK6S4HM005GBCO) and a
broken one can false-green. The gate must never silently pass on the wrong
code, so detection is **fail-closed** once the diff touches shared source
while the link step stays best-effort.

Tracer-bullet convention
------------------------
Expectations the sibling slices do not provide yet are marked
``xfail(strict=True)``; their implementing change sets remove the markers.
Two ``finish`` tests are **unmarked companions** that pass today and must
stay green — they are the guard against the failure mode of a *blanket*
hard-fail:

- ``test_finish_proceeds_when_shared_source_is_unchanged_despite_stale_resolution``
- ``test_finish_proceeds_for_a_repo_without_a_workspace_shared_package``

Hermetic by construction: every repository is a throwaway one created under
``tmp_path`` with the ``skill/shared/git_sandbox.py`` factories (a main
checkout plus a real linked worktree). ``phase_finish``'s external
boundaries — build, gate, would-be-commit staging, commit, push, the
``wl``/lifecycle store — are stubbed; the git fixture and the resolution
under test are real.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
for _path in (str(REPO_ROOT), str(REPO_ROOT / "skill")):
    if _path not in sys.path:
        sys.path.append(_path)

from shared import git_sandbox as gs

from skill.implement.scripts import implement

ITEM_ID = "SA-TEST"
SHARED_NAME = "@worklog/shared"
SHARED_REL = Path("packages") / "shared"
SHARED_SRC_REL = SHARED_REL / "src" / "index.js"
SHARED_DIST_REL = SHARED_REL / "dist"

_XFAIL_F3 = "F3 SA-0MVPI41SR008JYXL lands the stale-resolution gate"


# ---------------------------------------------------------------------------
# Fixtures: a real main checkout + a real linked worktree
# ---------------------------------------------------------------------------


def _populate_workspace(main: Path, *, workspace_shared: bool = True) -> None:
    """Seed the workspace checkout's source tree (no ``node_modules`` yet)."""
    main.mkdir(parents=True, exist_ok=True)
    (main / ".gitignore").write_text(
        "node_modules\n**/dist/\n.worklog/\n__pycache__/\n", encoding="utf-8"
    )
    (main / "README.md").write_text("# Fixture workspace\n", encoding="utf-8")
    (main / "package.json").write_text(
        json.dumps(
            {
                "name": "fixture-workspace",
                "private": True,
                "workspaces": ["packages/*"],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    if not workspace_shared:
        return
    shared = main / SHARED_REL
    (shared / "src").mkdir(parents=True)
    (shared / "dist").mkdir(parents=True)
    (shared / "package.json").write_text(
        json.dumps({"name": SHARED_NAME, "main": "./dist/index.js"}) + "\n",
        encoding="utf-8",
    )
    (shared / "src" / "index.js").write_text(
        "export const VERSION = 'main-src';\n", encoding="utf-8"
    )
    (shared / "dist" / "index.js").write_text(
        "exports.VERSION = 'main-stale-dist';\n", encoding="utf-8"
    )


def _populate_node_modules(main: Path, *, workspace_shared: bool = True) -> None:
    """Create the main checkout's installed ``node_modules`` tree."""
    nm = main / "node_modules"
    (nm / "left-pad").mkdir(parents=True)
    (nm / "left-pad" / "index.js").write_text(
        "module.exports = 'left-pad';\n", encoding="utf-8"
    )
    (nm / ".bin").mkdir()
    (nm / ".bin" / "left-pad").write_text("#!/bin/sh\n", encoding="utf-8")
    if workspace_shared:
        (nm / "@worklog").mkdir()
        # The reference layout: a workspace install leaves a relative link
        # back to the main checkout's package.
        (nm / "@worklog" / "shared").symlink_to("../../packages/shared")


def _workspace_repo(
    tmp_path: Path, *, workspace_shared: bool = True
) -> tuple[Path, Path]:
    """Return ``(main, worktree)`` — a real checkout plus a linked worktree."""
    main = gs.init_repo(tmp_path / "main", default_branch="dev", sandbox_root=tmp_path)
    _populate_workspace(main, workspace_shared=workspace_shared)
    _populate_node_modules(main, workspace_shared=workspace_shared)
    gs.commit_all(main, "seed workspace checkout")
    wt = gs.add_worktree(main, tmp_path / "wt", "wl-fixture-item", base="dev")
    return main, wt


def _install_stale_symlink(wt: Path, main: Path) -> None:
    """Reproduce today's ``start`` behaviour: a whole-directory symlink."""
    nm = wt / "node_modules"
    if nm.is_symlink() or nm.is_file():
        nm.unlink()
    elif nm.is_dir():
        shutil.rmtree(nm)
    os.symlink(str(main / "node_modules"), str(nm), target_is_directory=True)


def _append_shared_source(wt: Path) -> None:
    """The item's change set: a modification under ``packages/shared/src/``."""
    src = wt / SHARED_SRC_REL
    src.write_text(
        src.read_text(encoding="utf-8") + "export const FIXED = true;\n",
        encoding="utf-8",
    )


def _stale_dist(main: Path) -> str:
    return str(main / SHARED_DIST_REL)


def _real(path: Path) -> Path:
    return Path(os.path.realpath(str(path)))


# ---------------------------------------------------------------------------
# Resolution check: stale / worktree-local / inapplicable
# ---------------------------------------------------------------------------


@pytest.mark.xfail(strict=True, reason=_XFAIL_F3)
def test_resolution_check_reports_stale_through_a_directory_symlink(tmp_path):
    """A whole-directory symlink resolves ``@worklog/shared`` to the main copy."""
    main, wt = _workspace_repo(tmp_path)
    _install_stale_symlink(wt, main)

    check = implement._shared_resolution_check(str(wt), str(main))

    assert check["applicable"] is True
    assert check["ok"] is False
    assert check["package"] == SHARED_NAME
    assert _real(Path(check["resolved_root"])) == _real(main / SHARED_REL)
    assert _real(Path(check["expected_root"])) == _real(wt / SHARED_REL)
    assert _real(Path(check["stale_dist"])) == _real(main / SHARED_DIST_REL)


@pytest.mark.xfail(strict=True, reason=_XFAIL_F3)
def test_resolution_check_reports_worktree_local_with_the_link_farm(tmp_path):
    """The link farm makes the resolved package root the worktree's own copy."""
    main, wt = _workspace_repo(tmp_path)
    assert implement._ensure_node_modules_symlink(str(wt), str(main)) is True

    check = implement._shared_resolution_check(str(wt), str(main))

    assert check["applicable"] is True
    assert check["ok"] is True
    assert _real(Path(check["resolved_root"])) == _real(wt / SHARED_REL)
    assert check["stale_dist"] in (None, "")


@pytest.mark.xfail(strict=True, reason=_XFAIL_F3)
def test_resolution_check_reports_stale_when_node_modules_is_missing(tmp_path):
    """No ``node_modules`` at all is stale, not "not applicable"."""
    main, wt = _workspace_repo(tmp_path)

    check = implement._shared_resolution_check(str(wt), str(main))

    assert check["applicable"] is True
    assert check["ok"] is False
    assert check["resolved_root"] is None
    assert _real(Path(check["stale_dist"])) == _real(main / SHARED_DIST_REL)


@pytest.mark.xfail(strict=True, reason=_XFAIL_F3)
def test_resolution_check_is_not_applicable_without_a_workspace_shared_package(
    tmp_path,
):
    """No workspace shared package → the check is a no-op (degradation)."""
    main, wt = _workspace_repo(tmp_path, workspace_shared=False)

    check = implement._shared_resolution_check(str(wt), str(main))

    assert check["applicable"] is False


# ---------------------------------------------------------------------------
# Staleness detector: fires only on shared-source change AND stale resolution
# ---------------------------------------------------------------------------


@pytest.mark.xfail(strict=True, reason=_XFAIL_F3)
def test_staleness_refusal_names_the_stale_dist_and_the_remediation(tmp_path):
    """Shared source changed + stale resolution → actionable refusal message."""
    main, wt = _workspace_repo(tmp_path)
    _install_stale_symlink(wt, main)
    _append_shared_source(wt)

    message = implement._stale_shared_resolution(str(wt), str(main))

    assert message, "expected a hard-fail message for stale shared resolution"
    assert _stale_dist(main) in message
    assert "implement.py start" in message


@pytest.mark.xfail(strict=True, reason=_XFAIL_F3)
def test_staleness_detector_no_ops_with_a_worktree_local_farm(tmp_path):
    """Shared source changed but resolution is worktree-local → no refusal."""
    main, wt = _workspace_repo(tmp_path)
    assert implement._ensure_node_modules_symlink(str(wt), str(main)) is True
    _append_shared_source(wt)

    assert implement._stale_shared_resolution(str(wt), str(main)) is None


@pytest.mark.xfail(strict=True, reason=_XFAIL_F3)
def test_staleness_detector_no_ops_when_shared_source_is_unchanged(tmp_path):
    """Stale resolution alone is not a failure: nothing shared changed."""
    main, wt = _workspace_repo(tmp_path)
    _install_stale_symlink(wt, main)
    (wt / "README.md").write_text("# Unrelated change\n", encoding="utf-8")

    assert implement._stale_shared_resolution(str(wt), str(main)) is None


@pytest.mark.xfail(strict=True, reason=_XFAIL_F3)
def test_staleness_detector_ignores_non_src_shared_files(tmp_path):
    """Only ``packages/shared/src/**`` triggers detection, not any shared edit."""
    main, wt = _workspace_repo(tmp_path)
    _install_stale_symlink(wt, main)
    (wt / SHARED_REL / "README.md").write_text("# Shared docs\n", encoding="utf-8")

    assert implement._stale_shared_resolution(str(wt), str(main)) is None


@pytest.mark.xfail(strict=True, reason=_XFAIL_F3)
def test_staleness_detector_sees_committed_shared_source_changes(tmp_path):
    """A committed (clean-tree) shared-source change is still detected."""
    main, wt = _workspace_repo(tmp_path)
    _install_stale_symlink(wt, main)
    _append_shared_source(wt)
    gs.commit_all(wt, "change shared source")
    status = gs.run_git(wt, ["status", "--porcelain=v1"]).stdout.strip()
    assert status == "", f"fixture must be clean to exercise the committed path: {status}"

    message = implement._stale_shared_resolution(str(wt), str(main))

    assert message
    assert _stale_dist(main) in message


@pytest.mark.xfail(strict=True, reason=_XFAIL_F3)
def test_staleness_detector_no_ops_without_a_workspace_shared_package(tmp_path):
    """Degradation: repos without a workspace shared package are never refused."""
    main, wt = _workspace_repo(tmp_path, workspace_shared=False)
    _install_stale_symlink(wt, main)
    (wt / "README.md").write_text("# Unrelated change\n", encoding="utf-8")

    assert implement._stale_shared_resolution(str(wt), str(main)) is None


# ---------------------------------------------------------------------------
# finish gate integration
# ---------------------------------------------------------------------------


def _build_result() -> dict:
    return {
        "success": True,
        "stdout": "stubbed build",
        "stderr": "",
        "exit_code": 0,
        "skipped": False,
    }


def _green_gate() -> dict:
    return {
        "success": True,
        "failures": [],
        "notices": [],
        "notice_codes": [],
        "skipped": False,
        "tooling": "suite",
        "scope": "changed",
    }


def _gate_tree() -> dict:
    return {"tree": "abc1234", "scope": "changed", "cached": False, "available": True}


def _cleanup_result() -> dict:
    return {"method": "wl_cleanup_worktree", "terminated": 0, "warning": ""}


@contextlib.contextmanager
def _finish_harness(wt: Path, main: Path):
    """Drive ``phase_finish`` with its external boundaries stubbed.

    The git fixture and the resolution under test are real; build, the gate,
    would-be-commit staging, commit, push, worktree removal and the
    ``wl``/lifecycle store are stubbed so a refusal can be attributed to the
    staleness check alone (no suite runs, nothing is pushed).
    """
    state = implement.ImplementState(
        work_item_id=ITEM_ID,
        worktree_path=str(wt),
        repo_root=str(main),
        parent_branch="dev",
    )
    comments: list[str] = []

    with contextlib.ExitStack() as stack:
        stack.enter_context(
            patch.object(implement, "_discover_worktree", return_value=str(wt))
        )
        stack.enter_context(patch.object(implement, "read_state", return_value=state))
        stack.enter_context(
            patch.object(implement, "_worktree_placement_violation", return_value=None)
        )
        stack.enter_context(
            patch.object(implement, "run_build", return_value=_build_result())
        )
        gate = stack.enter_context(
            patch.object(
                implement, "run_gate_suite", return_value=(_green_gate(), _gate_tree())
            )
        )
        stack.enter_context(
            patch.object(
                implement,
                "stage_tested_tree",
                return_value={"success": True, "tree": "abc1234"},
            )
        )
        commit = stack.enter_context(
            patch.object(implement, "git_commit", return_value=True)
        )
        stack.enter_context(
            patch.object(implement, "git_get_commit_hash", return_value="abc1234")
        )
        stack.enter_context(
            patch.object(
                implement,
                "_fast_forward_local_dev_for_push",
                return_value={"method": "stubbed", "success": True, "own_churn": []},
            )
        )
        stack.enter_context(
            patch.object(
                implement, "_assert_commit_on_origin_dev", return_value={"success": True}
            )
        )
        stack.enter_context(patch.object(implement, "_assert_push_subject"))
        stack.enter_context(
            patch.object(implement, "git_push_to_dev", return_value=True)
        )
        stack.enter_context(
            patch.object(
                implement, "cleanup_worktree_processes", return_value=_cleanup_result()
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
        stack.enter_context(
            patch.object(
                implement,
                "wl_add_comment",
                side_effect=lambda wid, text: comments.append(text) or True,
            )
        )
        stack.enter_context(patch.object(implement, "_surface_postfix_prose"))
        lifecycle = stack.enter_context(
            patch.object(implement.StatusLifecycle, "update_status", MagicMock())
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
        yield SimpleNamespace(
            gate=gate, commit=commit, comments=comments, lifecycle=lifecycle
        )


@pytest.mark.xfail(strict=True, reason=_XFAIL_F3)
def test_finish_refuses_when_shared_source_changed_and_resolution_is_stale(
    tmp_path,
):
    """AC5: the gate can never silently pass on the main checkout's ``dist``.

    Shared source changed + stale resolution → non-zero finish result naming
    the stale ``<main>/packages/shared/dist`` path and the remediation, with
    no test gate run and no commit.
    """
    main, wt = _workspace_repo(tmp_path)
    _install_stale_symlink(wt, main)
    _append_shared_source(wt)

    with _finish_harness(wt, main) as harness:
        report = implement.phase_finish(ITEM_ID, json_output=True, no_refactor=True)

    assert report["success"] is False, report
    message = report["message"]
    assert _stale_dist(main) in message, message
    assert "implement.py start" in message, message

    # The gate never validated the wrong code, and nothing was committed.
    harness.gate.assert_not_called()
    harness.commit.assert_not_called()
    assert report["steps"]["shared_resolution"]["ok"] is False
    # The operator sees the same actionable message on the work item.
    assert any(_stale_dist(main) in text for text in harness.comments), harness.comments
    # The item is released, not left claimed in_progress.
    assert any(
        call.args[:2] == (ITEM_ID, "open")
        for call in harness.lifecycle.call_args_list
    ), harness.lifecycle.call_args_list


@pytest.mark.xfail(strict=True, reason=_XFAIL_F3)
def test_finish_proceeds_with_a_worktree_local_farm(tmp_path):
    """A worktree-local resolution passes the check and finishes normally."""
    main, wt = _workspace_repo(tmp_path)
    assert implement._ensure_node_modules_symlink(str(wt), str(main)) is True
    _append_shared_source(wt)

    with _finish_harness(wt, main) as harness:
        report = implement.phase_finish(ITEM_ID, json_output=True, no_refactor=True)

    assert report["success"] is True, report.get("message")
    harness.gate.assert_called_once()
    harness.commit.assert_called_once()
    assert report["steps"]["shared_resolution"]["ok"] is True


def test_finish_proceeds_when_shared_source_is_unchanged_despite_stale_resolution(
    tmp_path,
):
    """Companion: stale resolution without a shared change is not a failure.

    Detection is conditional on the diff touching ``packages/shared/src/**``
    — a blanket hard-fail would block every item in a worktree created before
    the fix landed. Green today and must stay green after F3.
    """
    main, wt = _workspace_repo(tmp_path)
    _install_stale_symlink(wt, main)
    (wt / "README.md").write_text("# Unrelated change\n", encoding="utf-8")

    with _finish_harness(wt, main) as harness:
        report = implement.phase_finish(ITEM_ID, json_output=True, no_refactor=True)

    assert report["success"] is True, report.get("message")
    harness.gate.assert_called_once()
    harness.commit.assert_called_once()


def test_finish_proceeds_for_a_repo_without_a_workspace_shared_package(tmp_path):
    """Companion: the degradation path keeps finishing normally."""
    main, wt = _workspace_repo(tmp_path, workspace_shared=False)
    _install_stale_symlink(wt, main)
    (wt / "README.md").write_text("# Unrelated change\n", encoding="utf-8")

    with _finish_harness(wt, main) as harness:
        report = implement.phase_finish(ITEM_ID, json_output=True, no_refactor=True)

    assert report["success"] is True, report.get("message")
    harness.gate.assert_called_once()
    harness.commit.assert_called_once()
