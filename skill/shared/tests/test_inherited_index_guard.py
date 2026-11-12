"""Behavioural tests for the inherited-``GIT_INDEX_FILE`` guard.

Contract under test (``skill/shared/inherited_index_guard.py`` plus the
autouse wiring in the repo-root ``conftest.py``), SA-0MVWQ4FZ4003S0GE:

- without an inherited index (or with one that does not exist) there is
  nothing to watch: ``IndexWatch.capture`` returns ``None``;
- a fixture that runs its git commands through the hermetic helpers
  (``shared.git_sandbox.run_git`` / ``fixture_git_env``) leaves the inherited
  index untouched, so ``poll()`` reports nothing — the reference remediation
  in the guard's message stays correct;
- a fixture that inherits ``GIT_INDEX_FILE`` and stages in its own repository
  rewrites the gate's index, so ``poll()`` returns an actionable report naming
  the index, the changed tree and the hermetic-helper remediation;
- the report is emitted **once**: after reporting, the watch re-baselines, so
  later tests in the same session are not blamed for the first test's write;
- an index rewrite that stages the same entries (a git stat-cache refresh) is
  not a violation — the guard compares the staged tree, not the bytes;
- end to end, the real ``conftest.py`` wiring fails the offending test by name
  in a simulated gate run (``GIT_INDEX_FILE`` exported to the nested suite) and
  leaves the index byte-identical when the suite is hermetic.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
for _path in (str(REPO_ROOT), str(REPO_ROOT / "skill")):
    if _path not in sys.path:
        sys.path.append(_path)

from shared import git_sandbox as gs
from shared import inherited_index_guard as guard

GUARD_MODULE = REPO_ROOT / "skill" / "shared" / "inherited_index_guard.py"
SANDBOX_MODULE = REPO_ROOT / "skill" / "shared" / "git_sandbox.py"
LIVE_REPO_GUARD_MODULE = REPO_ROOT / "skill" / "shared" / "live_repo_guard.py"
ROOT_CONFTEST = REPO_ROOT / "conftest.py"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _project_with_inherited_index(tmp_path: Path) -> tuple[Path, Path]:
    """A checkout with a gate-style temporary index staged from ``HEAD``.

    Mirrors ``implement.py``'s ``would_be_commit_index``: the index lives inside
    the repository's git directory and its staged tree equals the committed
    tree, so a fixture staging into it changes the tree observably.
    """
    repo = gs.init_repo(tmp_path / "proj", default_branch="dev", sandbox_root=tmp_path)
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    gs.commit_all(repo, "seed")
    index = repo / ".git" / "gate.index"
    env = {"GIT_INDEX_FILE": str(index)}
    gs.run_git(repo, ["read-tree", "HEAD"], check=True, env=env)
    gs.run_git(repo, ["add", "-A"], check=True, env=env)
    return repo, index


def _fixture_repo(tmp_path: Path, name: str = "fixture") -> Path:
    """An independent throwaway repository (the fixture under test)."""
    repo = gs.init_repo(tmp_path / name, default_branch="dev", sandbox_root=tmp_path)
    (repo / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    return repo


def _staged_tree(repo: Path, index: Path) -> str | None:
    """The gate's own evidence: ``git write-tree`` of *index* inside *repo*."""
    proc = gs.run_git(repo, ["write-tree"], env={"GIT_INDEX_FILE": str(index)})
    return proc.stdout.strip() if proc.returncode == 0 else None


def _copy_guard_wiring(project: Path) -> None:
    """Copy the real conftest + guard modules into *project* unchanged.

    Copying the production files (rather than reimplementing their wiring)
    keeps the simulated gate run honest: the assertions are about the shipped
    conftest behaviour, not about a test-local re-creation of it.
    """
    (project / "skill" / "shared").mkdir(parents=True, exist_ok=True)
    (project / "tests").mkdir(parents=True, exist_ok=True)
    (project / "conftest.py").write_text(
        ROOT_CONFTEST.read_text(encoding="utf-8"), encoding="utf-8"
    )
    for source in (GUARD_MODULE, SANDBOX_MODULE, LIVE_REPO_GUARD_MODULE):
        (project / "skill" / "shared" / source.name).write_text(
            source.read_text(encoding="utf-8"), encoding="utf-8"
        )
    (project / "tests" / "conftest.py").write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "\n"
        "ROOT = Path(__file__).resolve().parents[1]\n"
        'sys.path.insert(0, str(ROOT / "skill"))\n',
        encoding="utf-8",
    )


POISONING_SUITE = '''\
"""Synthetic suite: one hermetic fixture, one that stages into the inherited
index (the exact pattern the guard must attribute)."""

import subprocess

from shared.git_sandbox import init_repo, run_git


def _fixture_repo(tmp_path, name):
    repo = init_repo(tmp_path / name, default_branch="dev", sandbox_root=tmp_path)
    (repo / "tracked.txt").write_text("tracked\\n", encoding="utf-8")
    return repo


def test_hermetic_fixture_stages_into_its_own_index(tmp_path):
    repo = _fixture_repo(tmp_path, "hermetic")
    run_git(repo, ["add", "-A"], check=True)
    staged = run_git(repo, ["diff", "--cached", "--name-only"], check=True)
    assert staged.stdout.split() == ["tracked.txt"]


def test_raw_fixture_stages_into_the_inherited_index(tmp_path):
    repo = _fixture_repo(tmp_path, "raw")
    (repo / "raw.txt").write_text("raw\\n", encoding="utf-8")
    # No env= → inherits the gate's GIT_INDEX_FILE (the hazard under test).
    subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True)
'''


def _gate_env(project: Path, index: Path) -> dict[str, str]:
    """Environment for a simulated gate run of the suite in *project*."""
    env = {key: value for key, value in os.environ.items() if key != "GIT_INDEX_FILE"}
    env["GIT_INDEX_FILE"] = str(index)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _build_simulated_project(tmp_path: Path) -> tuple[Path, Path]:
    """A git checkout with the real guard wiring and the synthetic suite."""
    project = tmp_path / "project"
    _copy_guard_wiring(project)
    (project / "tests" / "test_suite.py").write_text(POISONING_SUITE, encoding="utf-8")
    repo = gs.init_repo(project, default_branch="dev", sandbox_root=tmp_path)
    (repo / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    gs.commit_all(repo, "seed simulated gate project")
    index = repo / ".git" / "gate.index"
    env = {"GIT_INDEX_FILE": str(index)}
    gs.run_git(repo, ["read-tree", "HEAD"], check=True, env=env)
    gs.run_git(repo, ["add", "-A"], check=True, env=env)
    return project, index


def _run_suite(project: Path, index: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *args],
        cwd=str(project),
        env=_gate_env(project, index),
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


# ---------------------------------------------------------------------------
# Unit behaviour — capture
# ---------------------------------------------------------------------------


class TestCapture:
    def test_no_inherited_index_means_nothing_to_watch(self, tmp_path):
        """Without ``GIT_INDEX_FILE`` the guard is inert."""
        repo, _index = _project_with_inherited_index(tmp_path)
        assert guard.IndexWatch.capture(repo, env={}) is None

    def test_missing_index_file_means_nothing_to_watch(self, tmp_path):
        """An inherited path that is not a file cannot be shadowed."""
        repo, _index = _project_with_inherited_index(tmp_path)
        env = {"GIT_INDEX_FILE": str(tmp_path / "absent.index")}
        assert guard.IndexWatch.capture(repo, env=env) is None

    def test_capture_records_the_staged_tree(self, tmp_path):
        """The captured baseline is the index's staged tree (the gate's evidence)."""
        repo, index = _project_with_inherited_index(tmp_path)
        watch = guard.IndexWatch.capture(repo, env={"GIT_INDEX_FILE": str(index)})

        head_tree = gs.run_git(repo, ["rev-parse", "HEAD^{tree}"], check=True)
        assert watch is not None
        assert watch.snapshot.tree == head_tree.stdout.strip()


# ---------------------------------------------------------------------------
# Unit behaviour — poll
# ---------------------------------------------------------------------------


class TestHermeticFixturesStaySilent:
    def test_hermetic_helper_leaves_the_inherited_index_untouched(self, tmp_path):
        """``run_git`` drops the inherited index, so the guard reports nothing."""
        repo, index = _project_with_inherited_index(tmp_path)
        watch = guard.IndexWatch.capture(repo, env={"GIT_INDEX_FILE": str(index)})
        fixture = _fixture_repo(tmp_path)

        gs.run_git(fixture, ["add", "-A"], check=True)
        gs.run_git(fixture, ["commit", "-q", "-m", "fixture"], check=True)

        assert watch is not None
        assert watch.poll() is None
        assert (fixture / ".git" / "index").is_file()

    def test_rewrite_that_stages_the_same_tree_is_not_a_violation(self, tmp_path):
        """A stat-cache refresh rewrites bytes but not the staged tree."""
        repo, index = _project_with_inherited_index(tmp_path)
        watch = guard.IndexWatch.capture(repo, env={"GIT_INDEX_FILE": str(index)})
        assert watch is not None

        # Rebuild the same staged tree: content may differ, `write-tree` may not.
        env = {"GIT_INDEX_FILE": str(index)}
        gs.run_git(repo, ["read-tree", "HEAD"], check=True, env=env)
        gs.run_git(repo, ["add", "-A"], check=True, env=env)

        assert watch.poll() is None


class TestPoisonedIndexIsReported:
    def test_fixture_staging_into_the_inherited_index_is_reported(self, tmp_path):
        """A raw ``git add`` in a fixture repo rewrites the watch's index."""
        repo, index = _project_with_inherited_index(tmp_path)
        watch = guard.IndexWatch.capture(repo, env={"GIT_INDEX_FILE": str(index)})
        fixture = _fixture_repo(tmp_path)
        (fixture / "raw.txt").write_text("raw\n", encoding="utf-8")

        subprocess.run(
            ["git", "add", "-A"],
            cwd=str(fixture),
            env={**os.environ, "GIT_INDEX_FILE": str(index)},
            check=True,
            capture_output=True,
        )

        report = watch.poll() if watch is not None else "not armed"
        assert guard.VIOLATION_HEADLINE in report
        assert str(index) in report
        assert "run_git" in report
        assert "fixture_git_env" in report

    def test_report_is_emitted_once_and_then_rebaselined(self, tmp_path):
        """Only the test that wrote the index is blamed, not every later test."""
        repo, index = _project_with_inherited_index(tmp_path)
        watch = guard.IndexWatch.capture(repo, env={"GIT_INDEX_FILE": str(index)})
        fixture = _fixture_repo(tmp_path)
        (fixture / "raw.txt").write_text("raw\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", "-A"],
            cwd=str(fixture),
            env={**os.environ, "GIT_INDEX_FILE": str(index)},
            check=True,
            capture_output=True,
        )

        assert watch is not None
        assert watch.poll() is not None
        assert watch.poll() is None

    def test_unreadable_inherited_index_is_reported(self, tmp_path):
        """A corrupt inherited index is a violation, never silent (AC2).

        ``git write-tree`` fails on an unreadable index, so ``poll()`` must
        fall back to reporting (naming the offender) rather than returning
        ``None`` — the observed failure mode where the gate could not attribute
        its verdict and reported it anonymously.
        """
        repo, index = _project_with_inherited_index(tmp_path)
        watch = guard.IndexWatch.capture(repo, env={"GIT_INDEX_FILE": str(index)})
        assert watch is not None

        # Truncate/corrupt the inherited index so ``git write-tree`` fails.
        index.write_bytes(b"DIRC\x00\x00\x00\x02corrupt-index")

        report = watch.poll()
        assert report is not None
        assert guard.VIOLATION_HEADLINE in report
        assert "unreadable" in report

    def test_deleted_inherited_index_is_reported(self, tmp_path):
        """A deleted inherited index is a violation, never silent (AC2).

        ``git write-tree`` on a missing index yields the empty tree (it does
        not fail), so ``poll()`` must not treat the deletion as benign stat
        churn — the empty tree differs from the captured HEAD tree, so the
        violation falls through to the report (naming the offender).
        """
        repo, index = _project_with_inherited_index(tmp_path)
        watch = guard.IndexWatch.capture(repo, env={"GIT_INDEX_FILE": str(index)})
        assert watch is not None
        assert watch.snapshot.digest is not None

        index.unlink()

        report = watch.poll()
        assert report is not None
        assert guard.VIOLATION_HEADLINE in report
        # ``git write-tree`` on a missing index yields the empty tree, which
        # differs from the captured HEAD tree — the violation is still named.
        assert str(index) in report

    def test_index_unreadable_at_capture_then_deleted_is_reported(self, tmp_path):
        """An index unreadable at session start is not excused by ``None`` digests.

        The narrow silent path (AC2): when capture recorded ``digest=None``
        (the index already could not be read) and a test then deletes the
        file, ``poll()`` must not treat ``None == None`` as stat churn — it
        must report. Before this guard was hardened the offender stayed
        anonymous and the finish gate failed with unattributable evidence.
        """
        repo, index = _project_with_inherited_index(tmp_path)
        index.chmod(0)
        try:
            watch = guard.IndexWatch.capture(
                repo, env={"GIT_INDEX_FILE": str(index)}
            )
        finally:
            index.chmod(0o644)
        assert watch is not None
        # Capture could not read the index: the digest baseline is None — the
        # exact state that used to short-circuit the stat-churn check.
        assert watch.snapshot.digest is None

        index.unlink()

        report = watch.poll()
        assert report is not None
        assert guard.VIOLATION_HEADLINE in report
        assert "unreadable" in report


# ---------------------------------------------------------------------------
# End to end — the real conftest wiring in a simulated gate run
# ---------------------------------------------------------------------------


class TestConftestWiring:
    def test_hermetic_suite_stays_green_and_leaves_the_index_unchanged(self, tmp_path):
        """A hermetic fixture neither trips the guard nor moves the gate's evidence.

        The assertion is on the **staged tree**, which is exactly what the
        finish gate compares: a git stat-cache refresh (``git status`` inside
        the live-repo guard's own snapshot) rewrites the index *file* without
        changing the entries staged in it, so byte equality would be a false
        failure — the same reasoning behind the guard's tree comparison.
        """
        project, index = _build_simulated_project(tmp_path)
        before = _staged_tree(project, index)

        result = _run_suite(
            project, index, "tests/test_suite.py::test_hermetic_fixture_stages_into_its_own_index"
        )

        assert result.returncode == 0, result.stdout + result.stderr
        assert guard.VIOLATION_HEADLINE not in result.stdout
        assert _staged_tree(project, index) == before
        assert index.is_file()

    def test_poisoning_test_is_failed_by_name(self, tmp_path):
        """The guard attributes the write to the offending test, not the gate."""
        project, index = _build_simulated_project(tmp_path)
        before = _staged_tree(project, index)

        result = _run_suite(
            project, index, "tests/test_suite.py::test_raw_fixture_stages_into_the_inherited_index"
        )
        output = result.stdout + result.stderr

        assert result.returncode != 0, output
        assert guard.VIOLATION_HEADLINE in output
        assert "offending test:" in output
        assert "test_raw_fixture_stages_into_the_inherited_index" in output
        assert "run_git" in output
        # The gate's evidence really did move (unreadable or different tree).
        assert _staged_tree(project, index) != before
