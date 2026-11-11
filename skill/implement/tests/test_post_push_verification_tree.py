"""Post-push verification must read the tree-keyed gate verdict.

Regression coverage for SA-0MWAXC5TD00461FN.

The dev-push full-suite gate binds its verdict to the pushed commit's **tree**
(``git rev-parse <commit>^{tree}``) and records it with
``fingerprint_kind="tree"`` (SA-0MW7C6BMZ00931JG). The post-push verification in
``implement.py finish`` used to query the cache only by the loose
``HEAD + dirty`` worktree fingerprint, so the gate's tree-keyed entry was never
visible: **every** successful push was reported as ``verdict=missing`` and the
terminal transition was refused, auto-filing a spurious
``[test-failure] post-push full-suite verification: no valid full-suite cache
entry`` item.

These tests exercise the real ``_resolve_post_push_verification`` against a
hermetic repository and the real per-repo cache; only the suite-command
resolver is stubbed (the real resolver needs the test skill's project layout,
which is irrelevant to the cache-key bug).
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

#: One canonical full-suite command; the exact string does not matter, only
#: that the producer (gate) and consumer (verification) resolve the same one.
COMMAND = "pytest -q -r a --disable-warnings"


def _make_repo(tmp_path: Path) -> Path:
    """A hermetic dev-branch repository with a per-repo ``.worklog`` cache."""
    repo = gs.init_repo(tmp_path / "repo", default_branch="dev")
    (repo / ".gitignore").write_text(".worklog/\n", encoding="utf-8")
    (repo / "README.md").write_text("# fixture\n", encoding="utf-8")
    gs.commit_all(repo, "seed")
    # Created after the commit (gitignored) so the working tree stays clean.
    (repo / ".worklog").mkdir()
    return repo


def _head_commit(repo: Path) -> str:
    return gs.run_git(repo, ["rev-parse", "HEAD"], check=True).stdout.strip()


def _head_tree(repo: Path) -> str:
    return gs.run_git(repo, ["rev-parse", "HEAD^{tree}"], check=True).stdout.strip()


def _store_tree_entry(
    repo: Path,
    command: str,
    tree: str,
    *,
    exit_code: int = 0,
    stdout: str = "1 passed\n",
    stderr: str = "",
) -> None:
    """Record a full-suite verdict keyed on *tree*, exactly as the gate does."""
    test_cache.store(
        command,
        tree,
        cwd=str(repo),
        stdout=stdout,
        stderr=stderr,
        exit_code=exit_code,
        scope="full",
        test_type="full",
        fingerprint_kind=test_cache.FINGERPRINT_KIND_TREE,
    )


def _stub_commands(monkeypatch) -> None:
    monkeypatch.setattr(
        implement, "_resolve_full_suite_commands", lambda _cwd: [COMMAND]
    )


def test_tree_keyed_full_suite_entry_satisfies_post_push_verification(
    tmp_path: Path, monkeypatch
) -> None:
    """The pushed tree's gate entry is found and yields a green verdict.

    Bug premise pinned first: the tree-keyed entry is *invisible* to the loose
    worktree query (a different fingerprint and a different provenance kind),
    which is exactly why the old verification reported ``missing``.
    """
    repo = _make_repo(tmp_path)
    commit = _head_commit(repo)
    tree = _head_tree(repo)
    _store_tree_entry(repo, COMMAND, tree)
    _stub_commands(monkeypatch)

    assert test_cache.query_cached(COMMAND, cwd=str(repo)) is None, (
        "premise: a tree-keyed gate entry is not loose-worktree evidence"
    )

    verification = implement._resolve_post_push_verification(
        str(repo), str(repo), commit
    )

    assert verification["verdict"] == implement.POST_PUSH_VERDICT_GREEN
    assert verification["success"] is True
    assert verification["executed"] is False
    assert verification["tree"] == tree
    entry = verification["entries"][0]
    assert entry["verdict"] == implement.POST_PUSH_VERDICT_GREEN
    assert entry["fingerprint_kind"] == "tree"
    assert entry["cache_root"] == str(repo)


def test_entry_for_a_different_tree_does_not_satisfy_verification(
    tmp_path: Path, monkeypatch
) -> None:
    """A verdict recorded for another tree is never served (fail-closed)."""
    repo = _make_repo(tmp_path)
    commit = _head_commit(repo)
    _store_tree_entry(repo, COMMAND, "f" * 40)
    _stub_commands(monkeypatch)

    verification = implement._resolve_post_push_verification(
        str(repo), str(repo), commit
    )

    assert verification["verdict"] == implement.POST_PUSH_VERDICT_MISSING
    assert verification["entries"][0]["verdict"] == (
        implement.POST_PUSH_VERDICT_MISSING
    )


def test_red_tree_keyed_entry_refuses_the_transition(
    tmp_path: Path, monkeypatch
) -> None:
    """A red tree-keyed verdict is reported red, not green/missing."""
    repo = _make_repo(tmp_path)
    commit = _head_commit(repo)
    tree = _head_tree(repo)
    _store_tree_entry(
        repo,
        COMMAND,
        tree,
        exit_code=1,
        stdout="FAILED tests/test_demo.py::test_boom - AssertionError\n",
    )
    _stub_commands(monkeypatch)

    verification = implement._resolve_post_push_verification(
        str(repo), str(repo), commit
    )

    assert verification["verdict"] == implement.POST_PUSH_VERDICT_RED
    assert verification["entries"][0]["exit_code"] == 1


def test_legacy_loose_entry_is_still_evidence_without_a_commit(
    tmp_path: Path, monkeypatch
) -> None:
    """Without a commit the historical loose-worktree query still applies."""
    repo = _make_repo(tmp_path)
    loose_state = test_cache.compute_git_state(str(repo), strict=True)
    test_cache.store(
        COMMAND,
        loose_state,
        cwd=str(repo),
        stdout="1 passed\n",
        stderr="",
        exit_code=0,
        scope="full",
        test_type="full",
        fingerprint_kind=test_cache.FINGERPRINT_KIND_WORKTREE,
    )
    _stub_commands(monkeypatch)

    verification = implement._resolve_post_push_verification(
        str(repo), str(repo)
    )

    assert verification["verdict"] == implement.POST_PUSH_VERDICT_GREEN
    assert verification["tree"] is None


def test_full_suite_cache_present_recognises_the_tree_keyed_entry(
    tmp_path: Path, monkeypatch
) -> None:
    """The dev-push gate evidence path also reads the tree-keyed entry.

    Companion regression for ``_full_suite_cache_present``: its worktree-only
    query would report ``no-evidence`` after a successful push even though the
    (fixed) post-push verification finds a green tree entry.
    """
    repo = _make_repo(tmp_path)
    commit = _head_commit(repo)
    tree = _head_tree(repo)
    _store_tree_entry(repo, COMMAND, tree)
    _stub_commands(monkeypatch)

    assert implement._full_suite_cache_present(str(repo), commit) is True
    assert implement._full_suite_cache_present(str(repo)) is False, (
        "the loose-only query must not fabricate evidence"
    )
