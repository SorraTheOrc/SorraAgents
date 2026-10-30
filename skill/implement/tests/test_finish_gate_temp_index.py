"""F6: temp-index staging in the finish gate (SA-0MVQHVGFR006W8LG).

What this module pins
---------------------
``phase_finish``'s test step must validate the exact tree the commit will
produce. The change set is staged into a **temporary copy** of the index,
``GIT_INDEX_FILE`` is exported for the suite, and the real index is only
touched once the gate is green — at which point ``git write-tree`` of the real
index must equal the tree the suite ran against (parent AC2).

The reproduction and abort contracts live with their own items
(``test_finish_gate_commit_tree.py`` — F1/F2, ``test_finish_gate_abort.py`` —
F5). This module covers what only the staging implementation can be asked:

* the gate records the tree it ran against and the temporary index it used;
* the **real** index is staged only after a green gate, and the committed tree
  is proven equal to the tested tree before the commit is attempted;
* a tree that drifted after the gate (a build step or a concurrent writer) is
  refused — the change set is unstaged again and no commit is created;
* the temporary index is removed and the previous ``GIT_INDEX_FILE`` restored
  on exit, with the real index byte-identical throughout.

Hermetic by construction: a temp git repository (``git_sandbox`` factories)
plus the F1 module's stubbed finish boundaries (build, commit, push, worktree
removal, the ``wl``/``StatusLifecycle`` store). The gate's **test step is
real** — a genuine suite runs against the staged tree.
"""

from __future__ import annotations

import os
import signal
import sys
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[3]
for _path in (str(REPO_ROOT), str(REPO_ROOT / "skill")):
    if _path not in sys.path:
        sys.path.append(_path)

from shared import git_sandbox as gs

from skill.implement.scripts import implement

# The F1 harness: the stubbed finish boundaries and the gate-report accessors.
from skill.implement.tests.test_finish_gate_commit_tree import (
    _finish_gate_report,
    _stubbed_finish_boundaries,
)

#: The single test the clean fixture suite carries.
CLEAN_TEST_NAME = "test_clean_gate_suite"
CLEAN_TEST_REL = f"tests/{CLEAN_TEST_NAME}.py"


def _build_clean_repo(tmp_path: Path) -> Path:
    """A hermetic repo whose suite is green, plus a clean change set.

    The seed commit carries the suite and a README; the change set then edits
    the README, so the gate's staged tree differs from HEAD in exactly one
    file — which makes the recorded ``staged_paths`` assertion unambiguous.
    """
    repo = gs.init_repo(tmp_path / "temp-index-repo", default_branch="dev")
    (repo / ".gitignore").write_text(
        "__pycache__/\n.pytest_cache/\n.worklog/\n", encoding="utf-8"
    )
    (repo / "README.md").write_text("# Temp-index fixture\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / CLEAN_TEST_REL).write_text(
        f"def {CLEAN_TEST_NAME}():\n    assert True\n", encoding="utf-8"
    )
    gs.commit_all(repo, "seed temp-index fixture")

    (repo / "README.md").write_text(
        "# Temp-index fixture\n\nChange set.\n", encoding="utf-8"
    )
    return repo


def _real_index_bytes(repo: Path) -> bytes:
    """The real index file's bytes (the F5 abort contract's strongest pin)."""
    git_dir = Path(
        gs.run_git(repo, ["rev-parse", "--absolute-git-dir"], check=True).stdout.strip()
    )
    return (git_dir / "index").read_bytes()


# ---------------------------------------------------------------------------
# AC2 — a green gate records the tested tree and stages exactly that tree
# ---------------------------------------------------------------------------


def test_green_gate_records_and_stages_the_tested_tree(tmp_path, monkeypatch):
    """A green gate records the would-be-commit tree and commits that tree.

    The gate's report names the tree the suite ran against and the change set
    it tested; the temporary index is gone once the gate returns; and the real
    index that the commit will use holds exactly that tree — the write-tree
    equality the parent AC requires (``staged_paths`` proves which change set
    was tested, not merely a tree hash).
    """
    repo = _build_clean_repo(tmp_path)

    report = _finish_gate_report(repo, tmp_path, monkeypatch)

    assert report["success"] is True, report.get("message")
    gate = report["steps"]["would_be_commit"]
    assert gate["tree"], gate
    assert gate["staged_paths"] == ["README.md"], gate
    assert not Path(gate["index_path"]).exists(), (
        "the temporary index must be removed once the gate returns"
    )

    assertion = report["steps"]["would_be_commit_assertion"]
    assert assertion["success"] is True, assertion
    assert assertion["verified"] is True, assertion
    assert assertion["tree"] == gate["tree"], assertion

    committed_tree = gs.run_git(repo, ["write-tree"], check=True).stdout.strip()
    assert committed_tree == gate["tree"], (
        "the real index must hold the tree the suite ran against"
    )


# ---------------------------------------------------------------------------
# AC2 — the write-tree equality assertion refuses a tree that drifted
# ---------------------------------------------------------------------------


def test_stage_tested_tree_refuses_a_tree_that_drifted_after_the_gate(tmp_path):
    """A tree that changed after the gate is refused, with no residual staging.

    A build step or a concurrent writer can modify files between the gate and
    the commit. Staging that tree and committing it would publish a tree the
    suite never tested, so ``stage_tested_tree`` unstages again, creates no
    commit, and returns an actionable message naming both trees.
    """
    repo = _build_clean_repo(tmp_path)
    head_before = gs.run_git(repo, ["rev-parse", "HEAD"], check=True).stdout.strip()

    with implement.would_be_commit_index(str(repo)) as gate_index:
        tested_tree = gate_index.tree

    (repo / "README.md").write_text("# Drifted\n", encoding="utf-8")

    result = implement.stage_tested_tree(str(repo), tested_tree)

    assert result["success"] is False, result
    assert result["expected"] == tested_tree, result
    assert result["tree"] != tested_tree, result
    assert "does not match the tree" in result["message"], result
    assert gs.run_git(repo, ["diff", "--cached", "--quiet"]).returncode == 0, (
        "the refused change set must be unstaged again"
    )
    assert (
        gs.run_git(repo, ["rev-parse", "HEAD"], check=True).stdout.strip()
        == head_before
    )


def test_stage_tested_tree_accepts_the_tested_tree(tmp_path):
    """An unchanged tree stages exactly the tested change set."""
    repo = _build_clean_repo(tmp_path)

    with implement.would_be_commit_index(str(repo)) as gate_index:
        tested_tree = gate_index.tree

    result = implement.stage_tested_tree(str(repo), tested_tree)

    assert result["success"] is True, result
    assert result["verified"] is True, result
    assert result["tree"] == tested_tree, result
    assert (
        gs.run_git(repo, ["diff", "--cached", "--name-only"], check=True).stdout.split()
        == ["README.md"]
    )


def test_finish_aborts_without_committing_when_the_tree_assertion_fails(
    tmp_path, monkeypatch
):
    """Finish returns a red report and never calls ``git_commit`` on drift.

    The assertion runs inside ``phase_finish``'s lifecycle block, so its
    failure must surface as a report (not an unhandled crash) and must abort
    before the commit — the F5 no-partial-commit contract.
    """
    repo = _build_clean_repo(tmp_path)
    monkeypatch.delenv("IMPLEMENT_TEST_COMMAND", raising=False)

    with _stubbed_finish_boundaries(repo, tmp_path):
        with patch.object(
            implement,
            "stage_tested_tree",
            return_value={
                "success": False,
                "expected": "expected-tree",
                "tree": "drifted-tree",
                "message": "The commit's tree does not match the tree the finish gate tested.",
            },
        ), patch.object(implement, "git_commit") as commit:
            report = implement.phase_finish(
                "SA-TEST", json_output=True, no_refactor=True
            )

    assert report["success"] is False, report
    assert "does not match the tree" in report["message"], report["message"]
    commit.assert_not_called()


# ---------------------------------------------------------------------------
# AC3/F5 — the temporary index and the environment are restored on exit
# ---------------------------------------------------------------------------


def test_temporary_index_is_removed_and_previous_environment_restored(tmp_path):
    """Inside the context the suite sees the temp index; on exit both are gone.

    The real index is never written by the staging context manager, so its
    bytes are identical before and after — the abort contract's strongest
    invariant, asserted directly on the index file.
    """
    repo = _build_clean_repo(tmp_path)
    real_index_before = _real_index_bytes(repo)

    with implement.would_be_commit_index(str(repo)) as gate_index:
        assert os.environ["GIT_INDEX_FILE"] == gate_index.index_path
        assert Path(gate_index.index_path).exists()
        assert (
            Path(gate_index.index_path).resolve()
            != Path(gate_index.real_index).resolve()
        )

    assert Path(gate_index.index_path).exists() is False
    assert _real_index_bytes(repo) == real_index_before


def test_temporary_index_restores_a_preset_git_index_file(tmp_path, monkeypatch):
    """A pre-existing ``GIT_INDEX_FILE`` is restored, not blindly unset."""
    repo = _build_clean_repo(tmp_path)
    monkeypatch.setenv("GIT_INDEX_FILE", "/tmp/previous-gate.index")

    with implement.would_be_commit_index(str(repo)) as gate_index:
        assert os.environ["GIT_INDEX_FILE"] == gate_index.index_path

    assert os.environ["GIT_INDEX_FILE"] == "/tmp/previous-gate.index"
    assert not Path(gate_index.index_path).exists()


def test_signal_cleanup_removes_a_live_temporary_index(tmp_path):
    """The signal handler removes the live temp index (SIGTERM/SIGINT path).

    A crash/kill during the suite must not leave an index-shaped artefact in
    the git directory; the handler shares its removal helper with the context
    manager's ``finally`` so both paths converge.
    """
    repo = _build_clean_repo(tmp_path)

    with implement.would_be_commit_index(str(repo)) as gate_index:
        assert Path(gate_index.index_path).exists()
        with patch.object(implement, "_worktree_path_global", None), patch.object(
            implement, "_repo_root_global", None
        ), patch.object(
            implement, "_safety_reset_if_in_progress"
        ), patch.object(
            implement, "cleanup_worktree_processes"
        ), patch.object(
            implement, "_remove_worktree"
        ), patch.object(
            implement, "_restore_repo_state"
        ):
            implement._handle_signal_cleanup(signal.SIGTERM)

        assert not Path(gate_index.index_path).exists(), (
            "the signal handler must remove the gate's live temporary index"
        )

    assert implement._gate_index_global is None


def test_fixture_git_env_drops_an_ambient_git_index_file(monkeypatch):
    """Hermetic fixtures never inherit the gate's exported temporary index.

    The suite runs with ``GIT_INDEX_FILE`` exported, and that variable
    deliberately survives the repository-override scrub (F3). Fixture git
    commands route through :func:`shared.git_sandbox.fixture_git_env`, which
    drops it — otherwise a fixture's ``git init``/``add``/``commit`` would
    write into the gate's index when the suite itself runs under the gate.
    """
    from shared.git_sandbox import fixture_git_env

    monkeypatch.setenv("GIT_INDEX_FILE", "/tmp/ambient-gate.index")
    monkeypatch.setenv("GIT_DIR", "/tmp/leaked/.git")

    env = fixture_git_env()

    assert "GIT_INDEX_FILE" not in env
    assert "GIT_DIR" not in env
    # Even a caller-supplied base cannot smuggle the gate's index through:
    # fixture tests target a specific index via ``run_git(..., env=...)``,
    # which is applied *after* this helper.
    assert "GIT_INDEX_FILE" not in fixture_git_env(
        {"GIT_INDEX_FILE": "/tmp/fixture.index", "PATH": "/usr/bin"}
    )


def test_run_cmd_drops_an_ambient_git_index_file(monkeypatch):
    """``run_cmd`` ignores an ambient ``GIT_INDEX_FILE`` unless asked to keep it.

    While the gate exports the temp index for the suite, the gate's own git
    commands must still act on the real index; suite runners opt in explicitly
    with ``keep_git_index=True`` so child git commands see the staged set.
    """
    monkeypatch.setenv("GIT_INDEX_FILE", "/tmp/ambient-gate.index")
    print_env = ["sh", "-c", 'printf %s "${GIT_INDEX_FILE:-}"']

    dropped = implement.run_cmd(print_env)
    kept = implement.run_cmd(print_env, keep_git_index=True)

    assert dropped.stdout == ""
    assert kept.stdout == "/tmp/ambient-gate.index"


def test_temporary_index_is_removed_when_staging_fails(tmp_path, monkeypatch):
    """A staging failure raises without leaking a temp index or env value."""
    repo = _build_clean_repo(tmp_path)
    monkeypatch.delenv("GIT_INDEX_FILE", raising=False)
    real_plumbing = implement._run_git_plumbing

    def _failing_add(cwd, args, **kwargs):
        if args == ["add", "-A"]:
            return real_plumbing(cwd, ["rev-parse", "--verify", "no-such-ref"])
        return real_plumbing(cwd, args, **kwargs)

    with patch.object(implement, "_run_git_plumbing", side_effect=_failing_add):
        try:
            with implement.would_be_commit_index(str(repo)):
                raise AssertionError("staging failure must not yield a gate index")
        except RuntimeError as exc:
            assert "Cannot stage the change set" in str(exc)

    assert "GIT_INDEX_FILE" not in os.environ
    git_dir = Path(
        gs.run_git(repo, ["rev-parse", "--absolute-git-dir"], check=True).stdout.strip()
    )
    leftovers = [
        path.name
        for path in git_dir.iterdir()
        if implement.GATE_INDEX_PREFIX in path.name
    ]
    assert leftovers == [], leftovers


# ---------------------------------------------------------------------------
# AC2 — a suite that stages into the inherited index invalidates the verdict
# ---------------------------------------------------------------------------

POISONING_TEST_NAME = "test_stages_into_the_inherited_index"
_POISONING_SUITE_SOURCE = f'''"""Hostile fixture: stages a new file into the inherited index.

A raw ``git add`` inherits ``GIT_INDEX_FILE``, so this rewrites the finish
gate's temporary index instead of the fixture's own — the hazard the gate's
post-suite integrity check names.
"""

import subprocess
from pathlib import Path


def {POISONING_TEST_NAME}():
    Path("poison.md").write_text("poison\\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], check=True)
'''


def _build_poisoning_repo(tmp_path: Path) -> Path:
    """A hermetic repo whose suite stages into whatever index it inherits."""
    repo = gs.init_repo(tmp_path / "poisoning-repo", default_branch="dev")
    (repo / ".gitignore").write_text(
        "__pycache__/\n.pytest_cache/\n.worklog/\n", encoding="utf-8"
    )
    (repo / "README.md").write_text("# Gate fixture\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / f"{POISONING_TEST_NAME}.py").write_text(
        _POISONING_SUITE_SOURCE, encoding="utf-8"
    )
    gs.commit_all(repo, "seed poisoning fixture")

    (repo / "README.md").write_text(
        "# Gate fixture\n\nChange set.\n", encoding="utf-8"
    )
    return repo


def test_suite_that_stages_into_the_inherited_index_fails_the_gate(
    tmp_path, monkeypatch
):
    """The gate refuses a verdict whose tested tree changed under it.

    A green pytest result is not enough: if the suite rewrote the temporary
    index, the verdict cannot be attributed to the would-be-commit tree. The
    gate reports red with an explanatory notice and leaves the real index
    untouched.
    """
    repo = _build_poisoning_repo(tmp_path)
    head_before = gs.run_git(repo, ["rev-parse", "HEAD"], check=True).stdout.strip()

    report = _finish_gate_report(repo, tmp_path, monkeypatch)

    assert report["success"] is False, report
    gate = report["steps"]["would_be_commit"]
    assert gate["index_unchanged"] is False, gate
    assert "gate evidence invalid" in report["message"], report["message"]
    assert gs.run_git(repo, ["diff", "--cached", "--quiet"]).returncode == 0
    assert (
        gs.run_git(repo, ["rev-parse", "HEAD"], check=True).stdout.strip()
        == head_before
    )


# ---------------------------------------------------------------------------
# Fail-soft — a cwd outside a git work tree has no commit tree to verify
# ---------------------------------------------------------------------------
#
# Regression pin (SA-0MVVKJGKP003QVNM): the would-be-commit staging made every
# ``phase_finish`` call whose worktree is not a git repository raise before the
# suite ran, breaking the stubbed-harness finish tests (lifecycle, lock and
# main-checkout-guard contracts) whose worktrees are plain temp directories.
# A directory outside a git work tree has no commit tree to attribute a
# verdict to, so the gate runs the suite plainly and records why the tree
# check was unavailable instead of refusing to run at all.


def test_gate_runs_plainly_outside_a_git_worktree(tmp_path, monkeypatch):
    """The suite still runs when there is no repository to stage a tree in.

    The gate must not raise before the suite: it runs the plain command and
    records ``available: false`` plus a stable reason, so a caller can tell an
    unverifiable tree from a verified one.
    """
    plain = tmp_path / "plain-directory"
    plain.mkdir()
    green = {
        "success": True,
        "failures": [],
        "scope": "changed",
        "notices": [],
    }
    ran: list[str] = []

    def _fake_run_tests(cwd, **kwargs):
        ran.append(cwd)
        return green

    monkeypatch.setattr(implement, "run_tests", _fake_run_tests)

    result, summary = implement.run_gate_suite(str(plain), scope="changed")

    assert result is green
    assert ran == [str(plain)], "the suite must still run outside a repository"
    assert summary["available"] is False, summary
    assert summary["reason"] == "not_a_git_worktree", summary
    assert summary["notice"], summary
    assert not summary.get("tree"), summary
    assert not list(plain.iterdir()), "no temporary index may be left behind"


def test_gate_still_verifies_the_tree_inside_a_git_worktree(tmp_path, monkeypatch):
    """A real repository keeps the tree verification (the fail-soft is narrow)."""
    repo = _build_clean_repo(tmp_path)
    green = {
        "success": True,
        "failures": [],
        "scope": "changed",
        "notices": [],
    }
    monkeypatch.setattr(implement, "run_tests", lambda cwd, **kwargs: green)

    result, summary = implement.run_gate_suite(str(repo), scope="changed")

    assert result is green
    assert summary.get("available", True) is not False, summary
    assert summary["tree"], summary
    assert summary["staged_paths"] == ["README.md"], summary
    assert not Path(summary["index_path"]).exists(), summary
