"""Regression tests: finish never attributes, or acts on, another checkout.

**Regression slice** for SA-0MVXVC03O0034A7V (parent SA-0MVWMUBP10041CSM
AC2/AC3). The pinning assertions were ``xfail(strict=True)`` on the pre-fix
tree and are unmarked now that the hardening landed. The companions pin
invariants the hardening must not relax (a deletion is never allow-listed, a
refused fast-forward records no churn, a foreign spelling of a dirty path never
satisfies the own-churn allow-list), so a companion failure means "the fix
weakened the guard", never "the harness broke".

## The defect this file pins (2026-10-31 forensics)

The AI_Hell finish ``AH-0MVVNKP1B00701HR``
(``.worklog/drive-logs/finish-AH-0MVVNKP1B00701HR-2.log``) reported:

* ``main_checkout_guard.repo_root = /home/rgardler/projects/AI_Hell``,
  ``baseline_paths: 0`` — its own main checkout was **clean** at snapshot time
  and both verifies passed;
* ``main_checkout_guard.own_churn`` **and**
  ``local_dev_fast_forward.own_churn`` listing seven paths of *other* work
  items (``docs/Game Design Document.md``, five ``src/powerups/*``,
  ``src/vfx/droneFeel.docs.test.ts``);
* ``local_dev_fast_forward`` ``method: advanced``, ``pre_advance_tip`` ==
  ``remote_tip`` == ``b009ab6da`` and ``advanced_to`` == ``6263702f0``.

So ``own_churn`` is exactly ``git diff --name-only b009ab6da 6263702f0`` — the
file list of the commit the run advanced ``dev`` to, not of anything observed
dirty in the finished checkout. Two independent facts make that list foreign to
the finish run: the checkout was clean (``baseline_paths: 0``), and the pushed
commit was the *rebased replacement* ``b009ab6da`` (the push failed
non-fast-forward and recovered), i.e. ``6263702f0`` was never what finish
delivered. Whatever produced ``6263702f0`` in that worktree, the contract this
file pins is the invariant behind the report: **``own_churn`` ⊆ paths observed
dirty in the finished checkout**, and paths it cannot observe are surfaced as a
``not_observed`` diagnostic, never allow-listed.

The second half is reachability: ``_resolve_main_repo_root`` trusted any
``state.repo_root`` that was not the worktree or inside it, so a state file
pointing at a sibling checkout made finish snapshot, fast-forward and push
*that* repository. The real-git sibling test below drives the production
``phase_finish`` path (only build/gate/commit/push/removal are stubbed) with a
sibling checkout whose ``origin/dev`` is ahead, and asserts the sibling's HEAD,
tracked files and status are untouched.

Every assertion here is on observable behaviour of production code against real
``git`` repositories (``skill.shared.git_sandbox``) — the guard logic is never
re-implemented, and the cases below are distinct from
``test_repo_root_resolution.py`` (which owns the stale-state rejection) and from
``test_finish_main_checkout_guard.py`` (which owns the failure-path verify
points).
"""

from __future__ import annotations

import contextlib
import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) in sys.path:
    sys.path.remove(str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT))

from skill.implement.scripts import implement
from skill.shared import git_sandbox

LOG_NAME = "implement.scripts.implement"

WORK_ITEM = "SA-TESTXCHECK01"

#: The seven paths the 2026-10-31 AI_Hell finish mis-attributed (abbreviated to
#: the two shapes that matter: a docs path and a powerups source path).
_FOREIGN_PATHS = (
    "docs/Game Design Document.md",
    "src/powerups/types.ts",
)


def _write(repo: Path, relpath: str, content: str) -> Path:
    path = repo / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _git(repo: Path, *args: str) -> str:
    return git_sandbox.run_git(repo, list(args), check=True).stdout


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").strip()


def _status(repo: Path) -> str:
    return _git(repo, "status", "--porcelain")


@pytest.fixture()
def main_repo(tmp_path: Path) -> Path:
    """A real main checkout on ``dev`` with a clean working tree."""
    repo = git_sandbox.init_repo(tmp_path / "main-checkout")
    _write(repo, ".gitignore", ".worklog/\n")
    _write(repo, "docs/changed.md", "v1\n")
    _write(repo, "docs/unchanged.md", "u1\n")
    git_sandbox.commit_all(repo, "seed")
    return repo


def _advance(repo: Path) -> tuple[str, str]:
    """Add one commit on top of ``HEAD``; return ``(before, after)``."""
    before = _head(repo)
    _write(repo, "docs/changed.md", "v2\n")
    _write(repo, "docs/added.md", "a1\n")
    after = git_sandbox.commit_all(repo, "advance")
    return before, after


# ---------------------------------------------------------------------------
# own_churn ⊆ observed churn (AC: the 2026-10-31 invariant)
# ---------------------------------------------------------------------------


def test_own_churn_excludes_paths_not_observed_dirty(main_repo: Path):
    """A commit range is not churn: only paths observed dirty are own churn.

    The 2026-10-31 report listed seven paths while ``baseline_paths`` was 0 and
    the working tree clean. The finished checkout after the fast-forward has
    nothing dirty, so nothing in that diff range belongs in ``own_churn``.
    """
    before, after = _advance(main_repo)
    assert _status(main_repo) == ""

    assert implement._record_finish_own_churn(str(main_repo), before, after) == []


def test_fast_forward_surfaces_not_observed_churn_as_diagnostic(tmp_path: Path):
    """The production fast-forward drops unobserved paths and reports them.

    ``_fast_forward_local_dev_for_push`` is the only caller of
    ``_record_finish_own_churn``, so the report step is where an operator sees
    the dropped paths: ``not_observed`` is visible, never allow-listed.

    The advance is created in a linked worktree on a feature branch — exactly as
    ``finish`` produces it — so local ``dev`` still equals ``origin/dev`` and the
    fast-forward really advances the main checkout.
    """
    remote = git_sandbox.init_bare_remote(tmp_path / "origin.git")
    repo = git_sandbox.init_repo(tmp_path / "main-checkout")
    _write(repo, ".gitignore", ".worklog/\n")
    _write(repo, "docs/changed.md", "v1\n")
    git_sandbox.commit_all(repo, "seed")
    git_sandbox.add_remote(repo, "origin", str(remote))
    _git(repo, "push", "--quiet", "origin", "dev")

    worktree = git_sandbox.add_worktree(repo, tmp_path / "wt", "wl-test-ff")
    _write(worktree, "docs/changed.md", "v2\n")
    _write(worktree, "docs/added.md", "a1\n")
    tip = git_sandbox.commit_all(worktree, "advance")

    result = implement._fast_forward_local_dev_for_push(str(repo), tip)

    # The harness really advanced the checkout (guards against a false pass).
    assert result["method"] == "advanced", result
    assert _head(repo) == tip

    assert result["own_churn"] == []
    not_observed = result.get("not_observed") or []
    assert any("docs/changed.md" in entry for entry in not_observed), result
    assert any("docs/added.md" in entry for entry in not_observed), result


def test_own_churn_refuses_a_root_that_is_not_the_repository(
    main_repo: Path, caplog
):
    """Paths outside the finished repo are never returned as own churn.

    A ``repo_root`` below the repository root yields diff paths relative to the
    repository (``docs/…``), which do not resolve inside ``repo_root`` — the
    helper refuses instead of attributing files it does not own.
    """
    before, after = _advance(main_repo)
    subdirectory = main_repo / "docs"
    assert subdirectory.is_dir()

    with caplog.at_level(logging.WARNING, logger=LOG_NAME):
        own_churn = implement._record_finish_own_churn(
            str(subdirectory), before, after
        )

    assert own_churn == []
    assert "refus" in caplog.text.lower()


# ---------------------------------------------------------------------------
# Foreign paths are violations, never allow-listed (unmarked companions)
# ---------------------------------------------------------------------------


def test_guard_never_allow_lists_a_foreign_spelling_of_a_dirty_path(tmp_path: Path):
    """An absolute/escaping entry in ``own_churn`` cannot hide a real dirty path.

    The guard compares normalised checkout-relative status paths, so a foreign
    spelling of the same path (absolute, or a ``..`` escape) must leave the path
    offending — proving the allow-list can never be satisfied by a path from
    another checkout.
    """
    repo = git_sandbox.init_repo(tmp_path / "main-checkout")
    _write(repo, "src/powerups/types.ts", "export const types = 1;\n")
    git_sandbox.commit_all(repo, "seed")
    _write(repo, "src/powerups/types.ts", "export const types = 2;\n")

    guard: dict = {"verifies": [], "verdict": "pass"}
    violations = implement._verify_main_checkout(
        guard,
        {"taken_at": "T0", "status": {}},
        repo_root=str(repo),
        worktree_path=str(tmp_path / "wt"),
        own_churn=[
            str(repo / "src/powerups/types.ts"),  # absolute path
            "../main-checkout/src/powerups/types.ts",  # `..` escape
            "src/powerups/other.ts",  # another checkout's path
        ],
        expected_dirty=(),
        stage="pre_push",
    )

    assert violations == ["src/powerups/types.ts [ M]"]
    assert guard["verdict"] == "fail"
    assert guard["verifies"][-1]["offending"] == ["src/powerups/types.ts [ M]"]


def test_guard_still_never_allow_lists_a_deletion(tmp_path: Path):
    """A deletion stays offending even when the checkout lists it as own churn.

    Unmarked companion to the 2026-10-31 invariant: constraining *when*
    ``own_churn`` is recorded must not turn a foreign deletion into expected
    churn (a fast-forward cannot delete a file relative to the tracked tree).
    """
    repo = git_sandbox.init_repo(tmp_path / "main-checkout")
    _write(repo, "packages/shared/src/database.ts", "export const x = 1;\n")
    git_sandbox.commit_all(repo, "seed")

    baseline = implement._snapshot_main_checkout(str(repo))
    assert baseline["status"] == {}

    (repo / "packages/shared/src/database.ts").unlink()
    current = implement._snapshot_main_checkout(str(repo))
    violations = implement._main_checkout_guard_violations(
        baseline,
        current,
        own_churn=["packages/shared/src/database.ts"],
    )
    assert violations == ["packages/shared/src/database.ts [ D]"]


def test_refused_fast_forward_records_no_own_churn(tmp_path: Path):
    """A fast-forward that never advanced the checkout contributes no churn.

    Unmarked companion: churn is only attributable when the advance happened.
    """
    repo = git_sandbox.init_repo(tmp_path / "main-checkout")
    _write(repo, "docs/changed.md", "v1\n")
    git_sandbox.commit_all(repo, "seed")

    result = implement._fast_forward_local_dev_for_push(str(repo), "0" * 40)

    assert result["method"] == "refused", result
    assert result["advanced"] is False
    assert result["own_churn"] == []


# ---------------------------------------------------------------------------
# A worktree-scoped finish never acts on a sibling checkout (AC2)
# ---------------------------------------------------------------------------


def _sibling_checkout_with_remote_ahead(tmp_path: Path) -> tuple[Path, Path, str]:
    """A sibling repository whose ``origin/dev`` is one commit ahead.

    That is what makes the reachability defect observable: a finish that
    escapes into this checkout fast-forwards it (mutating its HEAD and tracked
    files) even though it belongs to a different repository and never pushed
    anything here.
    """
    remote = git_sandbox.init_bare_remote(tmp_path / "sibling-origin.git")
    sibling = git_sandbox.init_repo(tmp_path / "sibling-checkout")
    _write(sibling, ".gitignore", ".worklog/\n")
    _write(sibling, "src/powerups/types.ts", "export const types = 1;\n")
    _write(sibling, "src/powerups/legacy.ts", "export const legacy = 1;\n")
    seed = git_sandbox.commit_all(sibling, "seed")
    git_sandbox.add_remote(sibling, "origin", str(remote))
    _git(sibling, "push", "--quiet", "origin", "dev")

    # A concurrent session lands another work item's change on origin/dev.
    _write(sibling, "src/powerups/types.ts", "export const types = 2;\n")
    (sibling / "src/powerups/legacy.ts").unlink()
    landing = git_sandbox.commit_all(sibling, "concurrent landing")
    _git(sibling, "push", "--quiet", "origin", "dev")
    # Rewind the local branch so origin/dev is ahead by exactly that commit.
    _git(sibling, "reset", "--hard", seed)
    return sibling, remote, landing


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


#: The tree the stubbed gate "ran" — any stable sha-like token works because
#: staging and commit are stubbed too.
_GATE_TREE = "0" * 40


@contextlib.contextmanager
def _finish_harness(worktree: Path):
    """Patch ``phase_finish``'s external boundaries but **not** the subject.

    ``_resolve_main_repo_root``, ``_snapshot_main_checkout``,
    ``_verify_main_checkout``, ``_fast_forward_local_dev_for_push`` and
    ``_record_finish_own_churn`` all run for real against the fixture repos —
    they are what this file pins. Build, gate, commit, push, ancestry, worktree
    removal and ``wl`` writes are stubbed (they have dedicated coverage).
    """
    with contextlib.ExitStack() as stack:
        stack.enter_context(
            patch.object(
                implement, "_discover_worktree", return_value=str(worktree)
            )
        )
        stack.enter_context(
            patch.object(
                implement, "_worktree_placement_violation", return_value=None
            )
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
                        "tree": _GATE_TREE,
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
                    "tree": _GATE_TREE,
                    "expected": _GATE_TREE,
                },
            )
        )
        stack.enter_context(patch.object(implement, "git_commit", return_value=True))
        stack.enter_context(
            patch.object(implement, "git_get_commit_hash", return_value="0" * 40)
        )
        stack.enter_context(patch.object(implement, "_assert_push_subject"))
        stack.enter_context(
            patch.object(implement, "git_push_to_dev", return_value=True)
        )
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
                "cleanup_worktree_processes",
                return_value={
                    "method": "wl_cleanup_worktree",
                    "terminated": 0,
                    "warning": "",
                },
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
            patch.object(implement, "wl_add_comment", return_value=True)
        )
        stack.enter_context(patch.object(implement, "_surface_postfix_prose"))
        stack.enter_context(
            patch.object(
                implement,
                "_assert_terminal_status",
                return_value={
                    "success": True,
                    "status": "completed",
                    "stage": "in_review",
                },
            )
        )
        stack.enter_context(
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
        yield


def test_finish_never_acts_on_a_sibling_checkout(tmp_path, caplog, monkeypatch):
    """A stated ``repo_root`` in another repository is rejected, loudly.

    The worktree's own repository stays the subject of finish: the guard's
    ``repo_root`` is the main checkout derived from the worktree, the mismatch
    is logged at WARNING naming both roots, and the sibling checkout is left
    exactly as it was (same HEAD, tracked files intact, clean status) instead of
    being fast-forwarded to its ``origin/dev``.
    """
    monkeypatch.delenv("IMPLEMENT_DRIVE_ACTIVE", raising=False)
    monkeypatch.delenv("IMPLEMENT_KEEP_WORKTREE", raising=False)

    main = git_sandbox.init_repo(tmp_path / "main-checkout")
    _write(main, ".gitignore", ".worklog/\n")
    _write(main, "README.md", "main checkout\n")
    git_sandbox.commit_all(main, "seed")
    worktree = git_sandbox.add_worktree(
        main, tmp_path / "wt", f"wl-{WORK_ITEM}-test"
    )

    sibling, _remote, landing = _sibling_checkout_with_remote_ahead(tmp_path)
    sibling_head = _head(sibling)
    sibling_status = _status(sibling)
    types_content = (sibling / "src/powerups/types.ts").read_text(encoding="utf-8")

    implement.write_state(
        implement.ImplementState(
            work_item_id=WORK_ITEM,
            worktree_path=str(worktree),
            repo_root=str(sibling),  # the foreign root the defect trusted
            parent_branch="dev",
        ),
        str(worktree),
    )

    with (
        caplog.at_level(logging.WARNING, logger=LOG_NAME),
        _finish_harness(worktree),
    ):
        report = implement.phase_finish(
            WORK_ITEM, json_output=True, no_refactor=True
        )

    assert report["success"] is True, report["message"]

    # The guard observed the worktree's own repository, never the sibling.
    guard_root = Path(report["steps"]["main_checkout_guard"]["repo_root"])
    assert guard_root.resolve() == main.resolve()
    assert guard_root.resolve() != sibling.resolve()

    # The mismatch is loud: both roots are named in the log.
    assert str(sibling) in caplog.text
    assert str(main) in caplog.text

    # The sibling checkout is untouched: no fast-forward, no deletion.
    assert _head(sibling) == sibling_head
    assert _head(sibling) != landing
    assert _status(sibling) == sibling_status == ""
    assert (sibling / "src/powerups/legacy.ts").exists()
    assert (
        sibling / "src/powerups/types.ts"
    ).read_text(encoding="utf-8") == types_content
