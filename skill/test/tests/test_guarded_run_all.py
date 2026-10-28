
# <!-- REFACTOR-SA-0MVPTU9HG00146UV
# smell: ruff_specific
# severity: medium
# description: Unpacked variable `gs` is never used
# -->
"""Unit tests for ``run_tests.guarded_run_all`` — guard-ownership contract.

Covers SA-0MV0EERBB007QA4V (child of SA-0MUH1MJL6003767Q): the canonical
guard-owning programmatic API. ``guarded_run_all`` is the single source of
truth for outer live-repo guard ownership; the CLI (``main``) and
``implement.py`` delegate to it.

Contract under test:

- When ``LIVE_REPO_GUARD_ACTIVE`` is unset the function OWNS the checkout:
  it snapshots before, sets the marker for the duration of ``run_all`` (so
  the inner conftest guard stands down), clears it in a ``finally`` block,
  and fails the run on a detected mutation.
- When the marker is already set an outer guard owns the checkout: the
  function stands down — it does not snapshot, does not touch the marker,
  and does not run the mutation detector.
- The result is the ``run_all`` dict shape, plus ``live_repo_mutation`` when
  the owning guard detected a mutation, and ``live_repo_excluded`` when
  working-tree churn was attributed to a concurrent session and therefore
  excluded from the verdict (SA-0MUIJAHXS003M7BG AC3).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_RUNNER_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_RUNNER_DIR) not in sys.path:
    sys.path.insert(0, str(_RUNNER_DIR))

import run_tests
from run_tests import guarded_run_all

MARKER_ENV = "LIVE_REPO_GUARD_ACTIVE"


def _fake_result(**overrides):
    base = {
        "success": True,
        "suites": {},
        "failures": [],
        "notices": [],
        "scope": "full",
        "type": "full",
        "resolved_scopes": ["full"],
    }
    base.update(overrides)
    return base


@pytest.fixture(autouse=True)
def _clean_marker():
    """Ensure the session recursion marker is restored after each test.

    The repo-root conftest arms the guard for the whole session, so
    ``LIVE_REPO_GUARD_ACTIVE`` is set before these tests run. The fixture must
    NOT pop it at setup: tests below call ``monkeypatch.delenv`` themselves,
    and popping it first makes monkeypatch record the marker as absent, so its
    teardown deletes it even after this fixture restores it — leaking an unset
    marker into later tests (which then re-arm the guard and report the cache
    write of an unrelated ``tmp_path`` repo as a mutation). Capturing the value
    and restoring it in ``finally`` keeps the session invariant intact
    (SA-0MUY6U05F008RG86).
    """
    original = os.environ.get(MARKER_ENV)
    yield
    if original is None:
        os.environ.pop(MARKER_ENV, None)
    else:
        os.environ[MARKER_ENV] = original


class TestGuardOwnership:
    def test_arms_guard_and_clears_marker_when_unset(self, monkeypatch):
        """Marker unset → own the checkout: snapshot, set marker, run, clear."""
        observed: dict[str, object] = {}

        def fake_snapshot(root):
            observed["snapshot_root"] = root
            return {"git": True, "refs": {}}

        def fake_run_all(**kwargs):
            observed["marker_during_run"] = os.environ.get(MARKER_ENV)
            observed["run_all_kwargs"] = kwargs
            return _fake_result()

        def fake_detect(root, snapshot_before):
            observed["detect_called"] = (root, snapshot_before)
            return {"mutation": None, "excluded": []}

        monkeypatch.delenv(MARKER_ENV, raising=False)
        monkeypatch.setattr(run_tests, "snapshot_guarded_roots", fake_snapshot)
        monkeypatch.setattr(run_tests, "run_all", fake_run_all)
        monkeypatch.setattr(run_tests, "_detect_live_repo_mutation", fake_detect)

        result = guarded_run_all(REPO_ROOT, timeout=42, scope="changed")

        assert observed["marker_during_run"] == "1"
        assert MARKER_ENV not in os.environ
        assert observed["snapshot_root"] == REPO_ROOT.resolve()
        assert observed["detect_called"][1] == {"git": True, "refs": {}}
        assert result["success"] is True
        assert "live_repo_mutation" not in result
        # Run-all receives the resolved project root, not the raw argument.
        assert observed["run_all_kwargs"]["cwd"] == REPO_ROOT.resolve()
        assert observed["run_all_kwargs"]["timeout"] == 42
        assert observed["run_all_kwargs"]["scope"] == "changed"

    def test_clears_marker_even_when_run_all_raises(self, monkeypatch):
        """The marker is cleared in a ``finally`` even on an exception."""
        monkeypatch.delenv(MARKER_ENV, raising=False)
        monkeypatch.setattr(
            run_tests, "snapshot_guarded_roots", lambda root: {"git": True}
        )

        def boom(**kwargs):
            raise RuntimeError("suite exploded")

        monkeypatch.setattr(run_tests, "run_all", boom)
        monkeypatch.setattr(
            run_tests,
            "_detect_live_repo_mutation",
            lambda root, snapshot: {"mutation": None, "excluded": []},
        )

        with pytest.raises(RuntimeError):
            guarded_run_all(REPO_ROOT)
        assert MARKER_ENV not in os.environ

    def test_stands_down_when_marker_preset(self, monkeypatch):
        """Marker preset → an outer guard owns; do not snapshot or detect."""
        monkeypatch.setenv(MARKER_ENV, "1")
        observed: dict[str, object] = {}

        def fake_snapshot(root):  # pragma: no cover - must not be called
            observed["snapshot_called"] = True
            return {"git": True}

        def fake_run_all(**kwargs):
            observed["marker_during_run"] = os.environ.get(MARKER_ENV)
            return _fake_result(scope="full")

        def fake_detect(root, snapshot_before):  # pragma: no cover
            observed["detect_called"] = True
            return {"mutation": "should not happen", "excluded": []}

        monkeypatch.setattr(run_tests, "snapshot_guarded_roots", fake_snapshot)
        monkeypatch.setattr(run_tests, "run_all", fake_run_all)
        monkeypatch.setattr(run_tests, "_detect_live_repo_mutation", fake_detect)

        result = guarded_run_all(REPO_ROOT)

        assert observed["marker_during_run"] == "1"
        assert "snapshot_called" not in observed
        assert "detect_called" not in observed
        # The outer guard's marker is preserved, not cleared.
        assert os.environ.get(MARKER_ENV) == "1"
        assert "live_repo_mutation" not in result
        assert result["success"] is True

    def test_mutation_is_merged_and_fails_the_run(self, monkeypatch):
        """A detected mutation marks the result failed with the diff attached."""
        monkeypatch.delenv(MARKER_ENV, raising=False)
        monkeypatch.setattr(
            run_tests, "snapshot_guarded_roots", lambda root: {"git": True}
        )
        monkeypatch.setattr(run_tests, "run_all", lambda **kwargs: _fake_result())
        monkeypatch.setattr(
            run_tests,
            "_detect_live_repo_mutation",
            lambda root, snapshot_before: {
                "mutation": "live-repo mutation detected: refs moved",
                "excluded": [],
            },
        )

        result = guarded_run_all(REPO_ROOT)

        assert result["success"] is False
        assert result["live_repo_mutation"] == "live-repo mutation detected: refs moved"
        # The original run_all shape is preserved (not replaced).
        assert result["suites"] == {}
        assert MARKER_ENV not in os.environ

    def test_forwards_all_run_all_kwargs(self, monkeypatch):
        """Every kwarg is forwarded to run_all unchanged."""
        monkeypatch.delenv(MARKER_ENV, raising=False)
        monkeypatch.setattr(
            run_tests, "snapshot_guarded_roots", lambda root: {"git": True}
        )
        monkeypatch.setattr(
            run_tests,
            "_detect_live_repo_mutation",
            lambda r, s: {"mutation": None, "excluded": []},
        )
        captured: dict[str, object] = {}

        def fake_run_all(**kwargs):
            captured.update(kwargs)
            return _fake_result()

        monkeypatch.setattr(run_tests, "run_all", fake_run_all)

        guarded_run_all(
            REPO_ROOT,
            timeout=99,
            suites=("pytest",),
            use_cache=False,
            force=True,
            no_cache=True,
            scope="changed",
            base_ref="origin/main",
            commands=["pytest tests/x.py"],
            test_type="unit",
            group="audit",
        )

        assert captured["suites"] == ("pytest",)
        assert captured["timeout"] == 99
        assert captured["use_cache"] is False
        assert captured["force"] is True
        assert captured["no_cache"] is True
        assert captured["scope"] == "changed"
        assert captured["base_ref"] == "origin/main"
        assert captured["commands"] == ["pytest tests/x.py"]
        assert captured["test_type"] == "unit"
        assert captured["group"] == "audit"


# ---------------------------------------------------------------------------
# Marker-restore regression (SA-0MUY6U05F008RG86)
# ---------------------------------------------------------------------------
# The two tests below are a deliberate ordered pair. The first deletes the
# session marker via ``monkeypatch`` (exactly what the ownership tests above
# do); the second asserts the marker is present again. If ``_clean_marker``
# popped the marker at setup, monkeypatch would record the absent value and
# delete it on teardown — leaving later modules with an unset marker, which
# makes ``guarded_run_all`` re-arm the guard and misreport an unrelated
# ``tmp_path`` repo's cache write as a live-repo mutation.


def test_marker_deleted_for_the_duration_of_a_test(monkeypatch):
    monkeypatch.delenv(MARKER_ENV, raising=False)
    assert MARKER_ENV not in os.environ


def test_marker_restored_after_the_previous_test():
    # The repo-root conftest arms the guard for the whole session, so the
    # marker must survive the previous test's monkeypatch teardown.
    assert os.environ.get(MARKER_ENV) == "1"


# ---------------------------------------------------------------------------
# WL-0MV5MYBKJ0033P8U — concurrent dev / remote-tracking ref movement is not a
# live-repo mutation (shared helper fix propagates to the outer guard)
# ---------------------------------------------------------------------------


def _advance_refs(repo, *refs):
    """Move each *ref* to a fresh tree-identical commit (only refs differ)."""
    from shared import git_sandbox as gs

    for ref in refs:
        parent = gs.run_git(repo, ["rev-parse", "HEAD"]).stdout.strip()
        tree = gs.run_git(repo, ["rev-parse", "HEAD^{tree}"]).stdout.strip()
        fresh = gs.run_git(
            repo, ["commit-tree", tree, "-p", parent, "-m", "advance"], check=True
        ).stdout.strip()
        gs.run_git(repo, ["update-ref", ref, fresh], check=True)


def test_outer_guard_detection_ignores_only_dev_and_origin_ref_moves(tmp_path):
    """The outer guard's mutation detector inherits the shared helper fix.

    Snapshots that differ ONLY in ``refs/heads/dev`` and ``refs/remotes/origin/*``
    (concurrent sibling finish fast-forward / fetch-push) must not produce a
    ``live_repo_mutation`` — the shared ``git_sandbox`` helper excludes those
    namespaces, so there is no consumer-side drift (AC4)."""
    from run_tests import _detect_live_repo_mutation
    from shared import git_sandbox as gs

    repo = gs.init_repo(tmp_path / "main", default_branch="dev")
    (repo / "README.md").write_text("fixture\n", encoding="utf-8")
    gs.commit_all(repo, "init")
    origin = gs.init_bare_remote(tmp_path / "origin.git")
    gs.add_remote(repo, "origin", str(origin))
    gs.run_git(repo, ["push", "-u", "origin", "dev"], check=True)
    gs.run_git(
        repo,
        ["update-ref", "refs/remotes/origin/feature-x", "HEAD"],
        check=True,
    )

    before = gs.snapshot_guarded_roots(repo)
    _advance_refs(
        repo,
        "refs/heads/dev",
        "refs/remotes/origin/dev",
        "refs/remotes/origin/feature-x",
    )

    verdict = _detect_live_repo_mutation(repo, before)

    assert verdict["mutation"] is None, verdict["mutation"]
    assert verdict["excluded"] == []


def test_outer_guard_detection_still_fires_on_new_non_excluded_branch(tmp_path):
    """A genuine ref addition outside the excluded namespaces still fails."""
    from run_tests import _detect_live_repo_mutation
    from shared import git_sandbox as gs

    repo = gs.init_repo(tmp_path / "adv", default_branch="dev")
    (repo / "README.md").write_text("fixture\n", encoding="utf-8")
    gs.commit_all(repo, "init")
    before = gs.snapshot_guarded_roots(repo)
    gs.run_git(repo, ["branch", "feature-x"], check=True)

    verdict = _detect_live_repo_mutation(repo, before)

    assert verdict["mutation"] is not None
    assert "feature-x" in verdict["mutation"]


# ---------------------------------------------------------------------------
# WL-0MVPE2B8E005O0OS — a guarded run started in a linked worktree must also
# watch the MAIN CHECKOUT
# ---------------------------------------------------------------------------


def _main_with_linked_worktree(tmp_path: Path):
    """A ``dev`` checkout with a tracked file plus a linked ``wl-*`` worktree."""
    from shared import git_sandbox as gs

    main = gs.init_repo(tmp_path / "main", default_branch="dev")
    (main / ".gitignore").write_text(".worklog/\n", encoding="utf-8")
    (main / "keep.txt").write_text("keep\n", encoding="utf-8")
    gs.commit_all(main, "init")
    worktree = gs.add_worktree(
        main, main / ".worklog" / "worktrees" / "wl-agent", "wl-agent"
    )
    return gs, main, worktree


def _mutating_scope_command(tmp_path: Path, victim: Path) -> str:
    """A shell-safe 'test command' that unlinks *victim* — the offending child."""
    script = tmp_path / "delete_tracked.py"
    script.write_text(
        "from pathlib import Path\n" f"Path({str(victim)!r}).unlink()\n",
        encoding="utf-8",
    )
    return f"{sys.executable} {script}"


def test_guarded_run_from_worktree_fails_on_main_checkout_deletion(
    tmp_path, monkeypatch
):
    """E2E: a guarded scope run inside a linked worktree deletes a main-checkout
    tracked file → the run FAILS and the message names the mutated path.

    The fixture reproduces the incident layout: the guarded root is a real
    linked worktree under ``.worklog/worktrees/wl-agent`` and the offending
    deletion happens in the *main* checkout, exactly the "main checkout mutated
    by a worktree-scoped run" defect class (docs/dev/worktree-isolation.md).
    """
    from shared.process_semaphore import nested_semaphore_dir

    gs, main, worktree = _main_with_linked_worktree(tmp_path)
    victim = main / "keep.txt"

    # A nested run must not contend for the host-wide "test" slot this suite
    # holds, and no outer guard may own the checkout (that would stand down
    # the authority under test).
    monkeypatch.setenv("PI_SEMAPHORE_DIR", str(nested_semaphore_dir(tmp_path)))
    monkeypatch.delenv(MARKER_ENV, raising=False)

    result = guarded_run_all(
        worktree,
        timeout=120,
        suites=("all",),
        commands=[_mutating_scope_command(tmp_path, victim)],
        scope="full",
        use_cache=False,
        no_cache=True,
    )

    assert not victim.exists(), "fixture invariant: the child really deleted the file"
    assert result["success"] is False, result
    mutation = result.get("live_repo_mutation")
    assert mutation, "the worktree-scoped run must not report a clean checkout"
    assert "keep.txt" in mutation, mutation
    assert str(main.resolve()) in mutation, mutation


def test_guarded_run_fast_forward_of_main_checkout_dev_is_expected_churn(
    tmp_path, monkeypatch
):
    """Characterisation: finish's own Step-5 fast-forward in the main checkout is
    NEVER a guard failure.

    A concurrent `implement.py finish` fast-forwards the main checkout's ``dev``
    to the commit it just pushed (and its ``fetch`` moves ``origin/dev``) while
    another worktree-scoped run is in flight. That is expected infrastructure
    churn, so the newly covered main checkout must not turn it into a failure.
    """
    from shared.process_semaphore import nested_semaphore_dir

    gs, main, worktree = _main_with_linked_worktree(tmp_path)
    origin = gs.init_bare_remote(tmp_path / "origin.git")
    gs.add_remote(main, "origin", str(origin))
    gs.run_git(main, ["push", "-u", "origin", "dev"], check=True)

    script = tmp_path / "fast_forward_dev.py"
    script.write_text(
        "import subprocess, sys\n"
        f"from pathlib import Path\n"
        f"main = Path({str(main)!r})\n"
        "(main / 'shared.txt').write_text('new\\n', encoding='utf-8')\n"
        "subprocess.run(['git', '-C', str(main), 'add', '-A'], check=True)\n"
        "subprocess.run(['git', '-C', str(main), 'commit', '-qm', 'advance dev'], check=True)\n"
        "subprocess.run(['git', '-C', str(main), 'update-ref',\n"
        "                'refs/remotes/origin/dev', 'HEAD'], check=True)\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("PI_SEMAPHORE_DIR", str(nested_semaphore_dir(tmp_path)))
    monkeypatch.delenv(MARKER_ENV, raising=False)

    result = guarded_run_all(
        worktree,
        timeout=120,
        suites=("all",),
        commands=[f"{sys.executable} {script}"],
        scope="full",
        use_cache=False,
        no_cache=True,
    )

    assert result["success"] is True, result.get("live_repo_mutation")
    assert "live_repo_mutation" not in result


# ---------------------------------------------------------------------------
# SA-0MUIJAHXS003M7BG — working-tree churn attributed to a concurrent session
# ---------------------------------------------------------------------------
# The outer guard inherits the shared ``git_sandbox`` attribution rule (no
# consumer-side drift): a working-tree path that was already dirty at snapshot
# start and is clean at run end was resolved by a concurrent session, so it
# must not fail the run — but it is reported under ``live_repo_excluded`` so
# the exclusion is never silent. A path that becomes dirty from a clean start
# is still a mutation.


def _attribution_repo(tmp_path: Path) -> Path:
    """A ``dev`` checkout with one tracked file and a pre-existing dirty edit."""
    from shared import git_sandbox as gs

    repo = gs.init_repo(tmp_path / "main", default_branch="dev")
    (repo / ".gitignore").write_text(".worklog/\n", encoding="utf-8")
    (repo / "README.md").write_text("fixture\n", encoding="utf-8")
    gs.commit_all(repo, "init")
    (repo / "README.md").write_text("sibling edit\n", encoding="utf-8")
    return repo


def _write_command(tmp_path: Path, name: str, body: str) -> str:
    """Write a small Python 'suite command' script and return its invocation."""
    script = tmp_path / name
    script.write_text(body, encoding="utf-8")
    return f"{sys.executable} {script}"


def test_outer_guard_attributes_a_concurrently_resolved_path(tmp_path):
    """The verdict dict carries the note instead of a mutation (AC1/AC3)."""
    from run_tests import _detect_live_repo_mutation
    from shared import git_sandbox as gs

    repo = _attribution_repo(tmp_path)
    before = gs.snapshot_guarded_roots(repo)

    # The sibling session commits the work it had already dirtied; ``dev``
    # moves with it, which the concurrency exclusion already covers.
    gs.run_git(repo, ["commit", "-am", "sibling work"], check=True)

    verdict = _detect_live_repo_mutation(repo, before)

    assert verdict["mutation"] is None, verdict["mutation"]
    notes = verdict["excluded"]
    assert len(notes) == 1, notes
    assert gs.EXCLUDED_CHURN_NOTE_PREFIX in notes[0]
    assert "README.md" in notes[0]


def test_guarded_run_reports_excluded_churn_without_failing(tmp_path, monkeypatch):
    """E2E (AC1/AC3): a sibling commit of pre-existing dirt keeps the run green.

    The suite command is the concurrent actor: it commits a file that was
    already dirty before the run started. The run must succeed, must not report
    a ``live_repo_mutation``, and must carry the exclusion note.
    """
    from shared import git_sandbox as gs
    from shared.process_semaphore import nested_semaphore_dir

    repo = _attribution_repo(tmp_path)
    sibling_commit = (
        "import subprocess\n"
        "import sys\n"
        f"repo = {str(repo)!r}\n"
        "subprocess.run(['git', '-C', repo, '-c', 'user.name=Sibling',\n"
        "                '-c', 'user.email=sibling@example.invalid',\n"
        "                'commit', '-am', 'sibling work'], check=True)\n"
        "sys.exit(0)\n"
    )

    monkeypatch.setenv("PI_SEMAPHORE_DIR", str(nested_semaphore_dir(tmp_path)))
    monkeypatch.delenv(MARKER_ENV, raising=False)

    result = guarded_run_all(
        repo,
        timeout=120,
        suites=("all",),
        commands=[_write_command(tmp_path, "sibling_commit.py", sibling_commit)],
        scope="full",
        use_cache=False,
        no_cache=True,
    )

    assert result["success"] is True, result.get("live_repo_mutation")
    assert "live_repo_mutation" not in result
    notes = result.get("live_repo_excluded")
    assert notes, result
    assert any("README.md" in note for note in notes), notes
    assert any(gs.EXCLUDED_CHURN_NOTE_PREFIX in note for note in notes), notes


def test_guarded_run_still_fails_on_a_newly_dirty_tracked_path(tmp_path, monkeypatch):
    """E2E (AC2): deleting a path that was clean at run start still fails."""
    from shared import git_sandbox as gs
    from shared.process_semaphore import nested_semaphore_dir

    repo = _attribution_repo(tmp_path)
    gs.run_git(repo, ["checkout", "--", "README.md"], check=True)  # clean start
    victim = repo / "README.md"
    delete_command = (
        "import sys\n"
        f"from pathlib import Path\nPath({str(victim)!r}).unlink()\n"
        "sys.exit(0)\n"
    )

    monkeypatch.setenv("PI_SEMAPHORE_DIR", str(nested_semaphore_dir(tmp_path)))
    monkeypatch.delenv(MARKER_ENV, raising=False)

    result = guarded_run_all(
        repo,
        timeout=120,
        suites=("all",),
        commands=[_write_command(tmp_path, "delete_tracked.py", delete_command)],
        scope="full",
        use_cache=False,
        no_cache=True,
    )

    assert not victim.exists(), "fixture invariant: the child really deleted the file"
    assert result["success"] is False, result
    mutation = result.get("live_repo_mutation")
    assert mutation and "README.md" in mutation, mutation
    assert "live_repo_excluded" not in result, result
