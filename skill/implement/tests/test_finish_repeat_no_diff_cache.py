"""Regression: repeat ``implement.py finish`` on an already-pushed, clean worktree.

What this module pins (SA-0MW0V9PEP004EJYV)
--------------------------------------------

A *repeat* finish is the documented recovery path after a failed push: the
change set is already committed and pushed, and ``implement.py finish`` is
re-run so the work item reaches ``completed``/``in_review``. On a clean,
fully-committed worktree the changed-scope selector finds no changed files
and falls back to the **full suite** — historically a fresh ~16-minute run,
gated by ``implement.py``'s temporary index (``GIT_INDEX_FILE``). Two bugs
made that repeat finish fail:

* (AC1) at least one full-suite test staged into the inherited index, so the
  gate's post-suite integrity check reported ``gate evidence invalid`` and the
  item never reached its terminal status;
* (AC4) even with the offender fixed, re-running the *whole* suite is
  unnecessary: the dev-push integration gate already recorded a green
  full-scope entry for the pushed commit's **tree**
  (``fingerprint_kind="tree"``, SA-0MW7C6AYO0062GJB), and a repeat finish
  fingerprints exactly that tree.

This module pins the cache-hit contract end to end: a clean, fully-committed
worktree whose pushed-tree green entry already exists must have its full-scope
fallback **served from the cache** (no fresh execution), the would-be-commit
index must stay unchanged (tree-identical), and ``phase_finish`` must persist
the terminal transition — the convergence the repeat finish previously never
reached.

Hermetic by construction: a temp git repository (``git_sandbox`` factories) plus
the shared stubbed-finish-boundaries harness — no real worklog, no real main
checkout, no network.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
for _path in (str(REPO_ROOT), str(REPO_ROOT / "skill")):
    if _path not in sys.path:
        sys.path.append(_path)

import test_cache
from shared import git_sandbox as gs

from skill.implement.scripts import implement
from skill.implement.tests.test_finish_gate_commit_tree import (
    _finish_gate_report,
    _gate_test_attempt,
)


def _build_committed_repo(tmp_path: Path) -> Path:
    """A hermetic repo whose suite is green and whose worktree is fully committed.

    This is the *repeat-finish* state: nothing staged, nothing modified,
    ``HEAD`` is the (already-pushed) commit — so the would-be-commit tree is
    exactly ``HEAD^{tree}``.
    """
    repo = gs.init_repo(tmp_path / "repeat-finish-repo", default_branch="dev")
    (repo / ".gitignore").write_text(
        "__pycache__/\n.pytest_cache/\n.worklog/\n", encoding="utf-8"
    )
    (repo / "README.md").write_text("# Repeat-finish fixture\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_green.py").write_text(
        "def test_green():\n    assert True\n", encoding="utf-8"
    )
    gs.commit_all(repo, "seed committed fixture")
    return repo


def _would_be_commit_tree_sha(repo: Path) -> str:
    """The tree a (re-)finish gate stages on the clean worktree — HEAD's tree.

    The finish gate copies the real index, runs ``git add -A`` (a no-op on a
    clean tree) and fingerprints ``git write-tree`` of that copy. On a
    fully-committed worktree that is ``HEAD^{tree}`` — the same tree the
    dev-push integration gate already ran green and recorded
    (``fingerprint_kind="tree"``).
    """
    env = {"GIT_INDEX_FILE": str(repo.parent / "repeat-finish.index")}
    gs.run_git(repo, ["read-tree", "HEAD"], check=True, env=env)
    gs.run_git(repo, ["add", "-A"], check=True, env=env)
    return gs.run_git(repo, ["write-tree"], check=True, env=env).stdout.strip()


def _single_full_suite_command(repo: Path) -> str:
    """The one full-suite command the gate resolves for the fixture repo."""
    commands = implement._resolve_full_suite_commands(str(repo))
    assert len(commands) == 1, commands
    return commands[0]


def _record_pushed_tree_green_entry(repo: Path, tree: str) -> dict:
    """Record a real green full-scope run for *tree* (the dev-push behaviour).

    This mirrors what ``run_tests.py --scope full --gate-commit <sha>``
    records on the dev-push integration gate: a successful run whose cache
    entry is keyed on the pushed commit's **tree** (`fingerprint_kind ==
    "tree"`), exactly the fingerprint a repeat finish supplies.
    """
    entry = test_cache.run_cached(
        _single_full_suite_command(repo), cwd=str(repo), fingerprint=tree
    )
    assert entry["exit_code"] == 0, entry["stdout"]
    assert entry["fingerprint_kind"] == test_cache.FINGERPRINT_KIND_TREE, entry
    return entry


def test_repeat_finish_serves_the_pushed_tree_entry_and_reaches_terminal(
    tmp_path, monkeypatch
):
    """A repeat finish on a clean, already-pushed worktree converges.

    The full-scope fallback (no changed files) is served from the dev-push
    gate's green **tree** entry — no fresh suite execution — the would-be-commit
    index is untouched, and ``phase_finish`` persists the terminal
    ``completed``/``in_review`` transition that the historical repeat finish
    never reached (SA-0MW0V9PEP004EJYV AC3/AC4).
    """
    repo = _build_committed_repo(tmp_path)
    assert gs.run_git(repo, ["status", "--porcelain"]).stdout.strip() == "", (
        "the repeat-finish fixture must start fully committed"
    )

    tree = _would_be_commit_tree_sha(repo)
    assert tree == gs.run_git(
        repo, ["rev-parse", "HEAD^{tree}"], check=True
    ).stdout.strip()

    seeded = _record_pushed_tree_green_entry(repo, tree)

    report = _finish_gate_report(repo, tmp_path, monkeypatch)

    assert report["success"] is True, report["message"]

    # The gate converged in one attempt, served from the cache: no fresh
    # full-suite execution, and the entry reused is the pushed-tree one.
    attempt = _gate_test_attempt(report)
    assert attempt["success"] is True, attempt
    assert attempt["scope"] == "full", attempt
    assert attempt["cached"] is True, attempt
    assert attempt["cache_entry"]["fingerprint_kind"] == "tree", attempt
    assert attempt["cache_entry"]["completed_at"] == seeded["completed_at"], (
        "the served entry must be the dev-push gate's green tree entry"
    )
    assert attempt["git_fingerprint"] == tree, attempt

    # The would-be-commit index is unchanged after the suite: no test staged
    # into the inherited index (AC1's contract, checked here on the gate's
    # own evidence).
    gate = report["steps"]["would_be_commit"]
    assert gate["index_unchanged"] is True, gate

    # The terminal transition was persisted (AC3): the store read-back shows
    # completed/in_review exactly like a first-time finish.
    assert report["steps"]["status_readback"]["status"] == "completed", report
    assert report["steps"]["status_readback"]["stage"] == "in_review", report


def test_repeat_finish_without_an_entry_still_converges_via_a_fresh_run(
    tmp_path, monkeypatch
):
    """No cached entry: the full-scope fallback executes fresh and still converges.

    The AC4 reuse is an optimisation, never a prerequisite: a repeat finish
    whose pushed-tree entry is missing or outside the TTL genuinely re-runs
    the suite *and still reaches the terminal status* — the poisoning/guard
    fixes (AC1/AC2) make that fresh run safe. This pins the fallback contract
    so the optimisation cannot become a failure mode's cover.
    """
    repo = _build_committed_repo(tmp_path)
    tree = _would_be_commit_tree_sha(repo)
    assert test_cache.query_cached(
        _single_full_suite_command(repo),
        cwd=str(repo),
        fingerprint=tree,
        fingerprint_kind=test_cache.FINGERPRINT_KIND_TREE,
    ) is None, "the fixture must start without a tree entry"

    report = _finish_gate_report(repo, tmp_path, monkeypatch)

    assert report["success"] is True, report["message"]
    attempt = _gate_test_attempt(report)
    assert attempt["success"] is True, attempt
    assert attempt["scope"] == "full", attempt
    assert attempt["cached"] is False, attempt
    assert report["steps"]["would_be_commit"]["index_unchanged"] is True
    assert report["steps"]["status_readback"]["status"] == "completed", report
