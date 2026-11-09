"""Build-originated composition churn and composed-engine provenance.

SA-0MVZE164P0048306: ``phase_finish`` runs build → test → commit, and a build
such as this repo's ``npm run build`` (``engine:sync`` first) can advance the
engine submodule and rewrite/stage the pin plus the gitlink when the tracked
engine branch tip has moved. Left alone, ``git_commit``'s blanket ``git add
-A`` commits that bump — an engine change the item never originated — and the
gate tests a composition the commit does not record.

What is pinned here
-------------------
* **AC1 — build-originated churn is never committed.** A source-unchanged
  item whose build advances the engine pin+gitlink produces a commit (or no
  commit) that does not contain either path, and the submodule checkout is
  back at the recorded composition.
* **AC2 — the exclusion is conditional on origin.** An item whose own change
  set *before the build* already carries the pin/gitlink keeps committing
  them: the restoration only ever applies to paths that were clean pre-build.
* **AC3 — tested composition equals committed composition.** A fixture suite
  that asserts the submodule checkout equals the recorded gitlink passes only
  because the restore runs *before* the gate; the committed tree then records
  exactly the composition the suite ran against.
* **AC4 — every verdict records the composed engine SHA.** The gate attempt,
  the ``build_churn`` step and the completion comment all carry the resolved
  submodule commit vs the gitlink ``HEAD`` records.
* **AC5 — the cache key still sees the pin.** ``compute_git_state`` is not
  relaxed: a different pin content yields a different state fingerprint.
* **No-op for other repos / mixed-path safety / fail-closed restore.** A repo
  without submodules behaves byte-for-byte as before; a path the item had
  already modified (and the build touched further) is never restored; an
  unrestorable gitlink refuses finish with an actionable message instead of
  committing a composition the item did not originate.

Hermetic by construction: temp git repositories (``git_sandbox`` factories,
including a real local submodule) with the finish boundaries stubbed — the
**build, the gate, the real index staging and the real commit** run against
the fixture repository; push, worktree removal and the ``wl``/lifecycle store
are stubbed in the style of ``test_finish_gate_commit_tree.py``.
"""

from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parents[3]
for _path in (str(REPO_ROOT), str(REPO_ROOT / "skill")):
    if _path not in sys.path:
        sys.path.append(_path)

import test_cache
from shared import git_sandbox as gs

from skill.implement.scripts import implement

PIN_REL = Path("configs") / "engine-pin.json"
CORE_REL = "core"

GREEN_TEST_NAME = "test_fixture_suite_is_green"
COMPOSITION_TEST_NAME = "test_composed_engine_matches_recorded_gitlink"

_GREEN_TEST_SOURCE = '''"""Fixture suite: green by construction."""


def {test_name}():
    assert True
'''

_COMPOSITION_TEST_SOURCE = '''"""Composition parity: the checkout must equal the recorded gitlink.

The suite the *finish gate* runs. It only passes when the ``core`` submodule
checkout is at the commit ``HEAD`` records — the pre-build composition — so a
green AC3 verdict proves the restore ran before the suite.
"""

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def {test_name}():
    checkout = subprocess.run(
        ["git", "-C", str(ROOT / "core"), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    recorded = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD:core"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert checkout == recorded, (
        "submodule checkout " + checkout + " != recorded gitlink " + recorded
    )
'''


def _write_pin(repo: Path, sha: str) -> None:
    """Write ``configs/engine-pin.json`` for *sha* (the engine-sync format)."""
    pin = repo / PIN_REL
    pin.parent.mkdir(parents=True, exist_ok=True)
    pin.write_text(json.dumps({"sha": sha}) + "\n", encoding="utf-8")


def _advance_engine(game: Path, engine_v2: str) -> None:
    """Mirror ``engine:sync``: move ``core`` to the engine tip, pin and stage.

    The game-repo ``engine-sync.mjs`` contract (CG-0MVO268500071BPI): the
    submodule is moved with ``git submodule update --remote``, the pin is
    rewritten to the new HEAD and *both* the pin and the gitlink are staged
    — the exact shape ``git_commit``'s ``git add -A`` used to commit.
    """
    gs.run_git(game / CORE_REL, ["checkout", "-q", "--detach", engine_v2], check=True)
    _write_pin(game, engine_v2)
    gs.run_git(game, ["add", CORE_REL, str(PIN_REL)], check=True)


def _build_composition_fixture(
    tmp_path: Path, composition_guard: bool = False
) -> tuple[Path, str, str]:
    """A game repo composing an engine submodule, seeded at engine v1.

    Returns ``(game, engine_v1, engine_v2)``. The seed commit records the
    gitlink and the pin at v1; a tracked README and a green pytest suite make
    the finish gate meaningful, and the fixture's engine can then advance to
    v2 (the tracked-branch tip an ``engine:sync`` would pull).

    Args:
        tmp_path: Sandbox root for both fixture repositories.
        composition_guard: Seed the composition-parity suite (which fails
            unless the submodule checkout equals the recorded gitlink) instead
            of the trivially green one — the AC3 detector.
    """
    engine = gs.init_repo(tmp_path / "engine", default_branch="dev")
    (engine / "lib.txt").write_text("v1\n", encoding="utf-8")
    gs.commit_all(engine, "engine v1")
    engine_v1 = gs.run_git(engine, ["rev-parse", "HEAD"], check=True).stdout.strip()
    (engine / "lib.txt").write_text("v1\nv2\n", encoding="utf-8")
    gs.commit_all(engine, "engine v2")
    engine_v2 = gs.run_git(engine, ["rev-parse", "HEAD"], check=True).stdout.strip()

    game = gs.init_repo(tmp_path / "game", default_branch="dev")
    (game / ".gitignore").write_text(
        "__pycache__/\n.pytest_cache/\n.worklog/\n", encoding="utf-8"
    )
    gs.run_git(
        game,
        ["-c", "protocol.file.allow=always", "submodule", "add", "-q", "-b",
         "dev", "../engine", CORE_REL],
        check=True,
    )
    gs.run_git(game / CORE_REL, ["checkout", "-q", "--detach", engine_v1], check=True)
    _write_pin(game, engine_v1)
    (game / "README.md").write_text("# Game\n", encoding="utf-8")
    (game / "tests").mkdir()
    if composition_guard:
        (game / "tests" / f"{COMPOSITION_TEST_NAME}.py").write_text(
            _COMPOSITION_TEST_SOURCE.format(test_name=COMPOSITION_TEST_NAME),
            encoding="utf-8",
        )
    else:
        (game / "tests" / f"{GREEN_TEST_NAME}.py").write_text(
            _GREEN_TEST_SOURCE.format(test_name=GREEN_TEST_NAME), encoding="utf-8"
        )
    gs.commit_all(game, "seed game at engine v1")
    assert (
        gs.run_git(game, ["rev-parse", f"HEAD:{CORE_REL}"], check=True).stdout.strip()
        == engine_v1
    )
    return game, engine_v1, engine_v2


# ---------------------------------------------------------------------------
# finish harness — real build staging, real gate, real commit; stubbed edges
# ---------------------------------------------------------------------------


def _build_result() -> dict:
    return {
        "success": True,
        "stdout": "stubbed build",
        "stderr": "",
        "exit_code": 0,
        "skipped": False,
    }


def _cleanup_result() -> dict:
    return {"method": "wl_cleanup_worktree", "terminated": 0, "warning": ""}


@contextlib.contextmanager
def _finish_harness(repo: Path, tmp_path: Path, build=None, build_outcome=None):
    """Drive ``phase_finish`` with every external boundary stubbed.

    Unlike the F1 harness, the **build** (a project-supplied callable that
    performs the fixture's engine churn), the **finish gate**, the real-index
    staging (``stage_tested_tree``) and the real **``git_commit``** all run for
    real — the assertions in this module are about exactly those steps.

    Args:
        repo: The fixture worktree root.
        tmp_path: Sandbox root (hosts the fake main checkout).
        build: Side effect the stubbed build performs (the engine churn).
        build_outcome: The ``run_build`` result to report; defaults to a
            successful build. A failing outcome drives the build-failure
            branch without changing anything else.
    """
    main_checkout = tmp_path / "main-checkout"
    (main_checkout / ".worklog").mkdir(parents=True, exist_ok=True)
    state = implement.ImplementState(
        work_item_id="SA-TEST",
        worktree_path=str(repo),
        repo_root=str(main_checkout),
        parent_branch="dev",
    )

    def _run_build(cwd):
        if build is not None:
            build(cwd)
        if build_outcome is not None:
            return dict(build_outcome)
        return _build_result()

    with contextlib.ExitStack() as stack:
        stack.enter_context(
            patch.object(implement, "_discover_worktree", return_value=str(repo))
        )
        stack.enter_context(
            patch.object(implement, "read_state", return_value=state)
        )
        stack.enter_context(
            patch.object(
                implement, "_worktree_placement_violation", return_value=None
            )
        )
        stack.enter_context(patch.object(implement, "run_build", side_effect=_run_build))
        stack.enter_context(
            patch.object(
                implement,
                "_fast_forward_local_dev_for_push",
                return_value={
                    "method": "stubbed",
                    "success": True,
                    "own_churn": [],
                },
            )
        )
        stack.enter_context(
            patch.object(
                implement, "_assert_commit_on_origin_dev", return_value={"success": True}
            )
        )
        stack.enter_context(patch.object(implement, "git_push_to_dev", return_value=True))
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
        stack.enter_context(patch.object(implement, "wl_add_comment", return_value=True))
        stack.enter_context(patch.object(implement, "_surface_postfix_prose"))
        # The read-only post-push verification (SA-0MVXUSJ2P009T97H) models
        # the pre-push gate's cached green full-suite entry; its own
        # verdict/refusal semantics live in
        # ``test_finish_finalisation_ordering.py``.
        stack.enter_context(
            patch.object(
                implement,
                "query_cached",
                return_value={
                    "stdout": "1 passed",
                    "stderr": "",
                    "exit_code": 0,
                    "completed_at": 1.0,
                    "command": implement.PYTEST_CMD,
                    "git_state": "stub-git-state",
                    "fingerprint_kind": "worktree",
                    "scope": "full",
                    "test_type": "full",
                    "cached": True,
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


def _committed_paths(repo: Path) -> list[str]:
    """Paths the last commit changed, sorted (empty for a root commit)."""
    proc = gs.run_git(repo, ["diff", "--name-only", "--no-renames", "HEAD~1", "HEAD"])
    return sorted(line for line in proc.stdout.splitlines() if line)



def _submodule_head(game: Path) -> str:
    return gs.run_git(game / CORE_REL, ["rev-parse", "HEAD"], check=True).stdout.strip()


def _gitlink(game: Path) -> str:
    return gs.run_git(game, ["rev-parse", f"HEAD:{CORE_REL}"], check=True).stdout.strip()


def _status_lines(game: Path) -> list[str]:
    proc = gs.run_git(game, ["status", "--porcelain=v1"])
    return [line for line in proc.stdout.splitlines() if line]


# ---------------------------------------------------------------------------
# AC1 — a source-unchanged item never commits build-originated churn
# ---------------------------------------------------------------------------


def test_build_originated_pin_and_gitlink_are_not_committed(
    tmp_path, monkeypatch
):
    """A source-unchanged item whose build advances the engine commits nothing.

    The item has no change set at all; the build stages the pin + gitlink at
    the advanced engine (the historical tce-main-street shape). Finish must
    restore both to the pre-build composition, so the would-be-commit tree is
    the seeded tree: ``git_commit`` finds nothing to commit and the item's
    commit does not contain the pin or the gitlink.

    A no-change item is finalised deliberately with ``--verification-only``
    (SA-0MW74IJWI009NRZ1 AC1/AC3); without it finish would refuse the no-commit
    terminal write.
    """
    monkeypatch.delenv("IMPLEMENT_TEST_COMMAND", raising=False)
    game, engine_v1, engine_v2 = _build_composition_fixture(tmp_path)

    seeds = gs.run_git(game, ["rev-list", "--count", "HEAD"], check=True).stdout.strip()

    with _finish_harness(game, tmp_path, build=lambda cwd: _advance_engine(game, engine_v2)):
        report = implement.phase_finish(
            "SA-TEST",
            json_output=True,
            no_refactor=True,
            verification_only=True,
        )

    assert report["success"] is True, report.get("message")
    churn = report["steps"]["build_churn"]
    assert churn["success"] is True, churn
    assert sorted(churn["excluded_from_commit"]) == [
        str(PIN_REL),
        CORE_REL,
    ], churn
    assert len(churn["restored"]) == 2, churn

    # The tested composition equals the committed one: the checkout is back at
    # the recorded gitlink and the tree is exactly the seeded tree.
    assert _submodule_head(game) == engine_v1, _submodule_head(game)
    assert _submodule_head(game) == _gitlink(game)
    assert _status_lines(game) == [], _status_lines(game)
    assert gs.run_git(game, ["rev-list", "--count", "HEAD"], check=True).stdout.strip() == seeds
    # No commit carries the build-originated composition churn: the pin still
    # records engine v1 in the working tree *and* in the committed tree.
    assert json.loads((game / PIN_REL).read_text())["sha"] == engine_v1
    assert (
        gs.run_git(game, ["show", f"HEAD:{PIN_REL}"], check=True).stdout
        == (game / PIN_REL).read_text()
    )
    assert _committed_paths(game) == []


# ---------------------------------------------------------------------------
# AC2 — an item that genuinely changes composition still commits it
# ---------------------------------------------------------------------------


def test_item_originated_composition_change_is_committed(tmp_path, monkeypatch):
    """A pin/gitlink in the item's own change set before the build is preserved.

    The exclusion is conditional on origin (never an unconditional ignore
    list): the fixture's change set advances the engine and rewrites the pin
    *before* finish runs, and the build does not touch them again, so they are
    ``restored: []`` and land in the commit with the recorded composition.
    """
    monkeypatch.delenv("IMPLEMENT_TEST_COMMAND", raising=False)
    game, _engine_v1, engine_v2 = _build_composition_fixture(tmp_path)

    # The item's own change set: compose the newer engine, staged like an agent
    # edit, before finish (and before any build) runs.
    _advance_engine(game, engine_v2)
    assert implement._index_gitlinks(str(game)) == {CORE_REL: engine_v2}

    with _finish_harness(game, tmp_path, build=None):
        report = implement.phase_finish("SA-TEST", json_output=True, no_refactor=True)

    assert report["success"] is True, report.get("message")
    churn = report["steps"]["build_churn"]
    assert churn["success"] is True, churn
    assert churn["build_originated"] == [], churn
    assert churn["restored"] == [], churn
    assert churn["excluded_from_commit"] == [], churn

    attempt = report["steps"]["tests"][-1]
    assert attempt["success"] is True, attempt
    composed = attempt["composed_engine"]["submodules"][0]
    assert composed["commit"] == engine_v2, composed
    assert composed["recorded_gitlink"] == engine_v2, composed
    assert composed["matches_recorded"] is True, composed

    # The commit records the item's composition: pin + gitlink travel together.
    assert _committed_paths(game) == [str(PIN_REL), CORE_REL], _committed_paths(game)
    assert _submodule_head(game) == engine_v2
    assert _gitlink(game) == engine_v2


# ---------------------------------------------------------------------------
# AC3 — the tested composition equals the committed composition
# ---------------------------------------------------------------------------


def test_tested_composition_equals_committed_composition(tmp_path, monkeypatch):
    """The suite runs against the pre-build composition, which the commit keeps.

    The fixture's own gate suite asserts ``git -C core rev-parse HEAD`` equals
    ``git rev-parse HEAD:core``. With the build having advanced the checkout,
    that suite only passes because the restore runs before the gate; the
    committed tree — the README change plus *no* pin/gitlink change — is
    exactly the tree the suite validated.
    """
    monkeypatch.delenv("IMPLEMENT_TEST_COMMAND", raising=False)
    game, engine_v1, engine_v2 = _build_composition_fixture(
        tmp_path, composition_guard=True
    )
    (game / "README.md").write_text("# Game\n\nChange set.\n", encoding="utf-8")

    with _finish_harness(game, tmp_path, build=lambda cwd: _advance_engine(game, engine_v2)):
        report = implement.phase_finish("SA-TEST", json_output=True, no_refactor=True)

    assert report["success"] is True, report.get("message")
    attempt = report["steps"]["tests"][-1]
    assert attempt["success"] is True, attempt
    assert any(
        COMPOSITION_TEST_NAME in str(failure) for failure in attempt["failures"]
    ) is False, attempt["failures"]

    composed = attempt["composed_engine"]["submodules"][0]
    assert composed["commit"] == engine_v1, composed
    assert composed["recorded_gitlink"] == engine_v1, composed
    assert composed["source"] == "would-be-commit tree", composed
    assert composed["matches_recorded"] is True, composed

    # The commit carries the README change and nothing else: the composed
    # engine (the composition the suite ran against) equals the committed one.
    assert _committed_paths(game) == ["README.md"], _committed_paths(game)
    assert _submodule_head(game) == engine_v1
    assert _gitlink(game) == engine_v1


# ---------------------------------------------------------------------------
# AC4 — every verdict records the composed engine SHA, in report and comment
# ---------------------------------------------------------------------------


def test_verdict_and_comment_record_the_composed_engine_sha(
    tmp_path, monkeypatch
):
    """The gate attempt and the completion comment name the composed engine.

    Alongside the existing ``tree_fingerprint``/``composed`` provenance, a
    verdict states which resolved submodule commit the suite ran against and
    whether it matches the gitlink ``HEAD`` records (AC4).
    """
    monkeypatch.delenv("IMPLEMENT_TEST_COMMAND", raising=False)
    game, engine_v1, _v2 = _build_composition_fixture(tmp_path)
    (game / "README.md").write_text("# Game\n\nChange set.\n", encoding="utf-8")

    with _finish_harness(
        game, tmp_path, build=lambda cwd: _advance_engine(game, _v2)
    ), patch.object(implement, "wl_add_comment") as comment:
        report = implement.phase_finish("SA-TEST", json_output=True, no_refactor=True)

    assert report["success"] is True, report.get("message")
    attempt = report["steps"]["tests"][-1]
    composed = attempt["composed_engine"]
    assert composed["composition"] == "submodules", composed
    assert composed["submodules"][0] == {
        "submodule": CORE_REL,
        "commit": engine_v1,
        "recorded_gitlink": engine_v1,
        "source": "would-be-commit tree",
        "matches_recorded": True,
    }, composed

    texts = [
        str(call.args[1]) for call in comment.call_args_list if len(call.args) > 1
    ]
    completion = [text for text in texts if "Implementation complete" in text]
    assert completion, texts
    assert (
        f"- Composed engine: {CORE_REL}@{engine_v1} "
        f"(recorded gitlink {engine_v1} in would-be-commit tree, "
        "match: true)" in completion[-1]
    ), completion[-1]


# ---------------------------------------------------------------------------
# AC5 — compute_git_state is not relaxed: the pin still fingerprints the state
# ---------------------------------------------------------------------------


def test_compute_git_state_still_sees_the_pin_content(tmp_path):
    """A different pin content yields a different loose git-state fingerprint.

    Remedy B (excluding the pin/gitlink from the cache key) stays rejected:
    the finish gate's cache key continues to reflect the composed engine, so a
    green entry recorded against one engine can never be served for another.
    """
    repo = gs.init_repo(tmp_path / "plain", default_branch="dev")
    _write_pin(repo, "a" * 40)
    gs.commit_all(repo, "pin at A")

    pin_a_state = test_cache.compute_git_state(str(repo))
    _write_pin(repo, "b" * 40)
    pin_b_state = test_cache.compute_git_state(str(repo))

    assert pin_a_state != pin_b_state, (
        "the cache key must still include the engine pin content"
    )


# ---------------------------------------------------------------------------
# Mixed-path safety and fail-closed restore
# ---------------------------------------------------------------------------


def test_path_modified_by_the_item_and_the_build_is_never_restored(
    tmp_path, monkeypatch
):
    """A path the item already had dirty stays dirty even when the build rewrites it.

    Restoring it would discard the item's own work, so ``item_modified_during_build``
    records it and the commit carries the build's final content — the item's
    forward progress is never silently rolled back.
    """
    monkeypatch.delenv("IMPLEMENT_TEST_COMMAND", raising=False)
    game, engine_v1, _engine_v2 = _build_composition_fixture(tmp_path)
    (game / "README.md").write_text("# Game\n\nItem work.\n", encoding="utf-8")

    def _build_overwrites_item_file(cwd):
        # A build that rewrites an already-dirty tracked file (e.g. a
        # formatter) without touching the composition.
        (Path(cwd) / "README.md").write_text(
            "# Game\n\nItem work (build-formatted).\n", encoding="utf-8"
        )

    with _finish_harness(game, tmp_path, build=_build_overwrites_item_file):
        report = implement.phase_finish("SA-TEST", json_output=True, no_refactor=True)

    assert report["success"] is True, report.get("message")
    churn = report["steps"]["build_churn"]
    assert churn["item_modified_during_build"] == ["README.md"], churn
    assert churn["restored"] == [], churn

    assert _committed_paths(game) == ["README.md"], _committed_paths(game)
    assert (game / "README.md").read_text(encoding="utf-8").startswith(
        "# Game\n\nItem work (build-formatted)."
    )
    # The composition was never part of this churn.
    assert _submodule_head(game) == engine_v1
    assert _gitlink(game) == engine_v1


def test_unrestorable_gitlink_fails_finish_closed(tmp_path, monkeypatch):
    """A gitlink that cannot be restored refuses finish instead of committing churn.

    The build dirties the submodule's own working tree (tracked-file edit): the
    restore checkout refuses rather than discard it, so ``restore_failed`` is
    non-empty and finish aborts — the item is reset to ``open`` and the
    actionable refusal names the path, the git error and both remedies.
    """
    monkeypatch.delenv("IMPLEMENT_TEST_COMMAND", raising=False)
    game, _engine_v1, _v2 = _build_composition_fixture(tmp_path)

    def _build_dirties_submodule(cwd):
        (Path(cwd) / CORE_REL / "lib.txt").write_text(
            "v1\nbuild dirt (never to be discarded)\n", encoding="utf-8"
        )

    with _finish_harness(game, tmp_path, build=_build_dirties_submodule):
        report = implement.phase_finish("SA-TEST", json_output=True, no_refactor=True)
        update_status = implement.StatusLifecycle.update_status

    assert report["success"] is False, report
    message = report["message"]
    assert "build-originated churn could not be excluded" in message, message
    assert CORE_REL in message, message
    churn = report["steps"]["build_churn"]
    assert churn["success"] is False, churn
    assert churn["reason"] == "restore_failed", churn
    assert any(entry["path"] == CORE_REL for entry in churn["restore_failed"]), churn
    # The refuse happened before any commit; the submodule dirt is untouched
    # and the item's own work could not have been discarded.
    assert _committed_paths(game) == []
    assert "never to be discarded" in (
        game / CORE_REL / "lib.txt"
    ).read_text(encoding="utf-8")
    update_status.assert_any_call("SA-TEST", "open", assignee="")


# ---------------------------------------------------------------------------
# No-op for repositories without a submodule composition
# ---------------------------------------------------------------------------


def test_repo_without_submodules_is_unchanged(tmp_path, monkeypatch):
    """No submodule composition: the churn stage is a recorded no-op.

    A plain repository (no gitlinks, a build that produces no tracked churn)
    keeps the exact old behaviour — the new step exists, reports empty
    ``restored``/``excluded_from_commit`` and ``composition: none``, and the
    item's own change still commits normally.
    """
    monkeypatch.delenv("IMPLEMENT_TEST_COMMAND", raising=False)
    repo = gs.init_repo(tmp_path / "plain-repo", default_branch="dev")
    (repo / ".gitignore").write_text(
        "__pycache__/\n.pytest_cache/\n.worklog/\n", encoding="utf-8"
    )
    (repo / "README.md").write_text("# Plain\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / f"{GREEN_TEST_NAME}.py").write_text(
        _GREEN_TEST_SOURCE.format(test_name=GREEN_TEST_NAME), encoding="utf-8"
    )
    gs.commit_all(repo, "seed plain repo")
    (repo / "README.md").write_text("# Plain\n\nChange set.\n", encoding="utf-8")

    with _finish_harness(repo, tmp_path, build=None):
        report = implement.phase_finish("SA-TEST", json_output=True, no_refactor=True)

    assert report["success"] is True, report.get("message")
    churn = report["steps"]["build_churn"]
    assert churn["success"] is True, churn
    assert churn["build_originated"] == [], churn
    assert churn["restored"] == [], churn
    assert churn["excluded_from_commit"] == [], churn
    assert churn["composed_engine"] == {"submodules": [], "composition": "none"}, churn
    assert _committed_paths(repo) == ["README.md"]
