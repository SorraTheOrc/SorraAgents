"""Unit tests for skill/test_cache.py — the per-repo test-run cache.

Covers the correctness contract of the cache: fresh-run persistence, cache
hits served WITHOUT spawning the underlying command, misses, TTL expiry,
git-state change invalidation, corrupt-entry degradation, force bypass,
pipeline normalization sharing, and read-only query mode.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from skill.test_cache import (
    DEFAULT_TTL_SECONDS,
    FAILED_RUN_TTL_SECONDS,
    cache_dir,
    compute_git_state,
    lookup,
    normalize_test_command,
    query_cached,
    run_cached,
    store,
    summary_lines,
)

SUCCESS_OUTPUT = (
    "Test Files  1 passed (1)\n"
    "Tests  3 passed (3)\n"
    "Duration  1.2s\n"
)


def make_runner(calls: list[str], output: str = SUCCESS_OUTPUT, exit_code: int = 0):
    """Return a runner that records each invoked command string in *calls*."""

    def runner(command: str, cwd: str | Path, timeout: int) -> SimpleNamespace:
        calls.append(command)
        return SimpleNamespace(stdout=output, stderr="", returncode=exit_code)

    return runner


def make_sequence_runner(outputs: list[str]):
    """Return a runner that yields one distinct output per call."""
    calls: list[str] = []
    index = [0]

    def runner(command: str, cwd: str | Path, timeout: int) -> SimpleNamespace:
        calls.append(command)
        output = outputs[min(index[0], len(outputs) - 1)]
        index[0] += 1
        return SimpleNamespace(stdout=output, stderr="", returncode=0)

    return runner, calls


def git(repo: Path, *args: str) -> None:
    """Run a git command inside *repo*, asserting success."""
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """A throwaway git repository with one committed file."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "cache-test@example.com")
    git(repo, "config", "user.name", "Cache Test")
    (repo / "tracked.txt").write_text("one\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    return repo


# ---------------------------------------------------------------------------
# Fresh-run persistence and hit/miss behaviour
# ---------------------------------------------------------------------------


def test_fresh_run_persists_stdout_stderr_exit_and_timestamp(
    git_repo: Path,
) -> None:
    """A fresh run must store full stdout, stderr, exit code and timestamp."""
    calls: list[str] = []
    result = run_cached(
        "npm test", cwd=str(git_repo), runner=make_runner(calls, exit_code=1)
    )
    assert result["cached"] is False
    assert result["stdout"] == SUCCESS_OUTPUT
    assert result["exit_code"] == 1
    assert result["completed_at"] > 0
    assert calls == ["npm --silent test"]  # canonicalized command executed

    # A read-only query on the same repo state must find the stored entry.
    entry = query_cached("npm test", cwd=str(git_repo))
    assert entry is not None
    assert entry["stdout"] == SUCCESS_OUTPUT
    assert entry["exit_code"] == 1
    assert entry["cached"] is True
    assert entry["completed_at"] == result["completed_at"]


def test_cache_hit_does_not_spawn_underlying_command(git_repo: Path) -> None:
    """Re-running the same command on unchanged state must NOT execute it."""
    calls: list[str] = []
    runner = make_runner(calls)
    first = run_cached("pytest", cwd=str(git_repo), runner=runner)
    second = run_cached("pytest", cwd=str(git_repo), runner=runner)
    assert first["cached"] is False
    assert second["cached"] is True
    assert second["stdout"] == SUCCESS_OUTPUT
    # The underlying command ran exactly once despite two invocations.
    assert len(calls) == 1


def test_cache_miss_triggers_fresh_run(git_repo: Path) -> None:
    """A different command (different normalized key) must execute fresh."""
    calls: list[str] = []
    runner = make_runner(calls)
    run_cached("pytest", cwd=str(git_repo), runner=runner)
    run_cached("npm test", cwd=str(git_repo), runner=runner)
    assert len(calls) == 2
    assert calls == ["pytest -q -r a --disable-warnings", "npm --silent test"]


# ---------------------------------------------------------------------------
# TTL expiry
# ---------------------------------------------------------------------------


def test_expired_ttl_triggers_fresh_run_and_replaces_entry(
    git_repo: Path,
) -> None:
    """An entry older than the TTL must be re-run and replaced."""
    runner, calls = make_sequence_runner(["run-one\n", "run-two\n"])
    run_cached("pytest", cwd=str(git_repo), runner=runner)

    # Backdate the stored entry beyond the default TTL.
    entry = query_cached("pytest", cwd=str(git_repo))
    assert entry is not None
    assert entry["stdout"] == "run-one\n"
    entry_dir = cache_dir(git_repo) / _key_for("pytest", entry["git_state"])
    meta_path = entry_dir / "metadata.json"
    meta = json.loads(meta_path.read_text())
    meta["completed_at"] = meta["completed_at"] - DEFAULT_TTL_SECONDS - 60
    meta_path.write_text(json.dumps(meta))

    result = run_cached("pytest", cwd=str(git_repo), runner=runner)
    assert result["cached"] is False
    assert len(calls) == 2  # re-executed after expiry

    # The stale entry was replaced: a fresh query returns the new run.
    fresh = query_cached("pytest", cwd=str(git_repo))
    assert fresh is not None
    assert fresh["stdout"] == "run-two\n"


# ---------------------------------------------------------------------------
# Git-state invalidation
# ---------------------------------------------------------------------------


def test_changed_git_state_triggers_fresh_run(git_repo: Path) -> None:
    """A dirty working tree must invalidate the cache entry."""
    calls: list[str] = []
    runner = make_runner(calls)
    run_cached("pytest", cwd=str(git_repo), runner=runner)

    (git_repo / "tracked.txt").write_text("two\n")  # dirty the tree
    result = run_cached("pytest", cwd=str(git_repo), runner=runner)
    assert result["cached"] is False
    assert len(calls) == 2


def test_compute_git_state_changes_when_tree_dirty(git_repo: Path) -> None:
    """compute_git_state must differ when the working tree changes."""
    clean = compute_git_state(str(git_repo))
    (git_repo / "tracked.txt").write_text("two\n")
    dirty = compute_git_state(str(git_repo))
    assert clean != dirty
    # An untracked file is also part of the state.
    (git_repo / "new_file.txt").write_text("new\n")
    assert compute_git_state(str(git_repo)) != dirty


def test_git_state_is_cwd_aware_worktree_fingerprint(git_repo: Path) -> None:
    """The fingerprint must reflect the repo containing cwd (worktree-aware).

    A subdirectory of the same repo resolves to the same state; a separate
    worktree created from the same commit has its own HEAD so its state
    differs from the main checkout's.
    """
    subdir = git_repo / "sub"
    subdir.mkdir()
    (subdir / "file.txt").write_text("x\n")
    assert compute_git_state(str(subdir)) == compute_git_state(str(git_repo))

    worktree = git_repo.parent / "worktree"
    git(git_repo, "worktree", "add", "-q", "-b", "wt-branch", str(worktree))
    try:
        # The worktree branch has its own HEAD -> different fingerprint.
        assert compute_git_state(str(worktree)) != compute_git_state(str(git_repo))
        # Dirtying the worktree must not affect the main checkout's state.
        (worktree / "tracked.txt").write_text("wt\n")
        assert compute_git_state(str(worktree)) != compute_git_state(str(git_repo))
    finally:
        git(git_repo, "worktree", "remove", "--force", str(worktree))


# ---------------------------------------------------------------------------
# Corrupt entries and force bypass
# ---------------------------------------------------------------------------


def test_corrupt_entry_degrades_to_fresh_run(git_repo: Path) -> None:
    """A corrupt/unreadable cache entry must degrade to a normal run."""
    calls: list[str] = []
    runner = make_runner(calls)
    run_cached("pytest", cwd=str(git_repo), runner=runner)

    entry = query_cached("pytest", cwd=str(git_repo))
    assert entry is not None
    entry_dir = cache_dir(git_repo) / _key_for("pytest", entry["git_state"])
    (entry_dir / "metadata.json").write_text("{not valid json")

    result = run_cached("pytest", cwd=str(git_repo), runner=runner)
    assert result["cached"] is False
    assert len(calls) == 2  # degraded to a fresh run without raising

    # Missing stdout file is also treated as corrupt.
    run_cached("npm test", cwd=str(git_repo), runner=runner)
    entry = query_cached("npm test", cwd=str(git_repo))
    assert entry is not None
    entry_dir = cache_dir(git_repo) / _key_for("npm test", entry["git_state"])
    (entry_dir / "stdout.txt").unlink()
    result = run_cached("npm test", cwd=str(git_repo), runner=runner)
    assert result["cached"] is False
    assert len(calls) == 4


def test_force_bypasses_valid_entry(git_repo: Path) -> None:
    """force=True must ignore a valid cached entry and re-run."""
    calls: list[str] = []
    runner = make_runner(calls)
    run_cached("pytest", cwd=str(git_repo), runner=runner)
    result = run_cached("pytest", cwd=str(git_repo), runner=runner, force=True)
    assert result["cached"] is False
    assert len(calls) == 2
    # The forced run refreshes the entry.
    fresh = query_cached("pytest", cwd=str(git_repo))
    assert fresh is not None
    assert fresh["stdout"] == SUCCESS_OUTPUT


def test_no_cache_bypasses_lookup_and_store(git_repo: Path) -> None:
    """no_cache=True must skip lookup AND skip storing the result."""
    calls: list[str] = []
    runner = make_runner(calls)
    run_cached("pytest", cwd=str(git_repo), runner=runner, no_cache=True)
    assert len(calls) == 1
    # Nothing was stored: a direct lookup is a miss.
    state = compute_git_state(str(git_repo))
    assert lookup("pytest", state, cwd=str(git_repo)) is None
    # A subsequent normal run is also a miss and re-executes.
    run_cached("pytest", cwd=str(git_repo), runner=runner)
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# Pipeline normalization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "variant",
    [
        "npm test 2>&1 | tail -30",
        'npm test 2>&1 | grep -E "Test Files|failed"',
        "npm test | head",
        "npm test 2>&1 | tee /tmp/test-output.log",
        "npm test",
    ],
)
def test_pipeline_normalized_variants_share_one_entry(
    git_repo: Path, variant: str
) -> None:
    """Output-filtering pipelines must share the underlying run's cache entry."""
    calls: list[str] = []
    runner = make_runner(calls)
    first = run_cached(variant, cwd=str(git_repo), runner=runner)
    assert first["cached"] is False
    assert calls == ["npm --silent test"]

    # All other variants are served from the single cached run.
    for other in [
        "npm test 2>&1 | tail -30",
        'npm test 2>&1 | grep -E "Test Files|failed"',
        "npm test | head",
        "npm test 2>&1 | tee /tmp/test-output.log",
        "npm test",
    ]:
        if other == variant:
            continue
        hit = run_cached(other, cwd=str(git_repo), runner=runner)
        assert hit["cached"] is True, f"expected cache hit for {other}"
    assert len(calls) == 1


def test_non_output_filter_pipeline_does_not_collapse(git_repo: Path) -> None:
    """Semantically different pipelines (e.g. sort) must NOT share the entry."""
    calls: list[str] = []
    runner = make_runner(calls)
    run_cached("npm test", cwd=str(git_repo), runner=runner)
    run_cached("npm test | sort", cwd=str(git_repo), runner=runner)
    assert len(calls) == 2


def test_normalize_test_command_strips_filters_and_redirects() -> None:
    """Pipeline/redirect stripping must be conservative and deterministic."""
    assert normalize_test_command("npm test 2>&1 | tail -30") == "npm --silent test"
    assert normalize_test_command('npm test 2>&1 | grep -E "Test Files|failed"') == "npm --silent test"
    assert normalize_test_command("pytest -q | head") == "pytest -q -r a --disable-warnings"
    # Unknown filters are preserved (no collapse into the plain-npm entry).
    assert normalize_test_command("npm test | sort") != "npm --silent test"
    # A bare `2>&1` redirect on the base command is stripped.
    assert normalize_test_command("npm test 2>&1") == "npm --silent test"


# ---------------------------------------------------------------------------
# Query mode
# ---------------------------------------------------------------------------


def test_query_mode_returns_summary_without_executing(git_repo: Path) -> None:
    """query_cached + summary_lines must return summary lines without running."""
    calls: list[str] = []
    runner = make_runner(calls)
    run_cached("pytest", cwd=str(git_repo), runner=runner)

    entry = query_cached("pytest", cwd=str(git_repo))
    assert entry is not None
    lines = summary_lines(entry["stdout"], entry["stderr"])
    assert any("Test Files" in line for line in lines)
    assert any("Tests" in line for line in lines)
    assert len(calls) == 1  # nothing was executed by the query

    # A custom grep pattern filters further.
    filtered = summary_lines(entry["stdout"], entry["stderr"], pattern="Test Files")
    assert filtered == ["Test Files  1 passed (1)"]


def test_query_mode_reports_miss(git_repo: Path) -> None:
    """A query for an un-cached command returns None (clear miss)."""
    assert query_cached("pytest tests/test_demo.py", cwd=str(git_repo)) is None


# ---------------------------------------------------------------------------
# Storage location
# ---------------------------------------------------------------------------


def test_cache_dir_prefers_worklog(git_repo: Path) -> None:
    """cache_dir must prefer <repo>/.worklog/cache/ when .worklog exists."""
    (git_repo / ".worklog").mkdir()
    assert cache_dir(git_repo) == git_repo / ".worklog" / "cache"


def test_cache_dir_falls_back_to_git_dir(git_repo: Path) -> None:
    """Without .worklog, cache_dir must fall back inside the git dir."""
    cache = cache_dir(git_repo)
    assert str(cache).endswith("test-cache")
    assert ".git" in str(cache)
    assert not str(cache).startswith(str(git_repo / ".worklog"))


def test_cache_entries_are_written_atomically_and_gitignored(
    git_repo: Path,
) -> None:
    """Stored entries live under the cache dir and are ignored by git."""
    calls: list[str] = []
    run_cached("pytest", cwd=str(git_repo), runner=make_runner(calls))
    cache = cache_dir(git_repo)
    assert cache.is_dir()
    # No .tmp files remain after an atomic write.
    assert list(cache.rglob("*.tmp")) == []
    # git status must not report the cache directory as untracked.
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=git_repo,
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    assert "test-cache" not in status
    assert ".worklog" not in status


def test_store_replaces_existing_entry(git_repo: Path) -> None:
    """store() must replace an existing entry for the same key."""
    state = compute_git_state(str(git_repo))
    store("pytest", state, cwd=str(git_repo), stdout="first\n", stderr="", exit_code=0)
    store("pytest", state, cwd=str(git_repo), stdout="second\n", stderr="", exit_code=1)
    entry = lookup("pytest", state, cwd=str(git_repo))
    assert entry is not None
    assert entry["stdout"] == "second\n"
    assert entry["exit_code"] == 1


def _key_for(command: str, git_state: str) -> str:
    """Compute the cache key directory name (mirrors the module internals)."""

    from skill.test_cache import cache_key

    return cache_key(normalize_test_command(command), git_state)


def test_default_ttl_is_two_hours() -> None:
    assert DEFAULT_TTL_SECONDS == 2 * 60 * 60


# ---------------------------------------------------------------------------
# Failed-run cache policy (SA-0MSJELL44009XYIL)
# ---------------------------------------------------------------------------


def _backdate_entry(git_repo: Path, command: str, seconds: float) -> None:
    """Backdate the stored entry's completed_at by *seconds* seconds."""
    entry = query_cached(command, cwd=str(git_repo))
    assert entry is not None
    entry_dir = cache_dir(git_repo) / _key_for(command, entry["git_state"])
    meta_path = entry_dir / "metadata.json"
    meta = json.loads(meta_path.read_text())
    meta["completed_at"] = meta["completed_at"] - seconds
    meta_path.write_text(json.dumps(meta))


def test_failed_run_expires_after_short_ttl(git_repo: Path) -> None:
    """A failed run must NOT be re-served for the full 2h TTL.

    A non-zero exit (e.g. a transient infra failure) expires after the short
    failed-run TTL so a later query/run re-executes fresh instead of being
    served a stale failure as if it were current (SA-0MSJELL44009XYIL).
    """
    calls: list[str] = []
    run_cached("npm test", cwd=str(git_repo), runner=make_runner(calls, exit_code=1))
    # Backdate beyond the failed-run TTL but still within the default TTL.
    _backdate_entry(git_repo, "npm test", FAILED_RUN_TTL_SECONDS + 60)

    assert query_cached("npm test", cwd=str(git_repo)) is None
    # A subsequent run re-executes (miss) rather than serving the stale failure.
    result = run_cached("npm test", cwd=str(git_repo), runner=make_runner(calls, exit_code=1))
    assert result["cached"] is False


def test_failed_run_served_within_short_ttl(git_repo: Path) -> None:
    """A recent failed run is still served (short TTL, not immediate expiry)."""
    calls: list[str] = []
    run_cached("npm test", cwd=str(git_repo), runner=make_runner(calls, exit_code=1))
    _backdate_entry(git_repo, "npm test", FAILED_RUN_TTL_SECONDS - 60)

    entry = query_cached("npm test", cwd=str(git_repo))
    assert entry is not None
    assert entry["exit_code"] == 1
    assert entry["cached"] is True


def test_green_run_keeps_full_ttl(git_repo: Path) -> None:
    """Green runs are unaffected: still served well past the failed-run TTL."""
    calls: list[str] = []
    run_cached("pytest", cwd=str(git_repo), runner=make_runner(calls))
    _backdate_entry(git_repo, "pytest", FAILED_RUN_TTL_SECONDS + 60)

    entry = query_cached("pytest", cwd=str(git_repo))
    assert entry is not None
    assert entry["exit_code"] == 0
    assert entry["cached"] is True


def test_failed_run_ttl_constant_is_short() -> None:
    """The failed-run TTL must be strictly shorter than the default TTL."""
    assert FAILED_RUN_TTL_SECONDS > 0
    assert FAILED_RUN_TTL_SECONDS < DEFAULT_TTL_SECONDS


# ---------------------------------------------------------------------------
# PATH augmentation for user-installed executables (SA-0MSUZAJPC003BS66)
# ---------------------------------------------------------------------------


def test_default_runner_prepends_local_bin_to_path(git_repo: Path) -> None:
    """_default_runner must prepend ~/.local/bin to PATH for subprocess calls.

    When the audit runner spawns suite commands in a restricted environment
    (missing ~/.local/bin on PATH), user-installed executables like pytest
    must still be found (SA-0MSUZAJPC003BS66).
    """
    from skill.test_cache import _default_runner

    captured_env: dict[str, str] = {}
    original_path = os.environ.get("PATH", "")
    local_bin = os.path.expanduser("~/.local/bin")
    # Remove ~/.local/bin from PATH to simulate the restricted environment.
    path_parts = [p for p in original_path.split(os.pathsep) if p != local_bin]
    os.environ["PATH"] = os.pathsep.join(path_parts)

    try:
        import subprocess as sp
        orig_run = sp.run

        def fake_run(cmd, **kwargs):
            captured_env["PATH"] = kwargs.get("env", {}).get("PATH", "")
            return SimpleNamespace(stdout="ok\n", stderr="", returncode=0)

        sp.run = fake_run  # type: ignore[assignment]
        try:
            result = _default_runner("pytest -q", str(git_repo), 60)
            path_value = captured_env.get("PATH", "")
            assert path_value is not None, "PATH not captured from subprocess.run call"
            # ~/.local/bin must be first on PATH.
            assert path_value.startswith(local_bin + os.pathsep), (
                f"~/.local/bin not at front of PATH: {path_value}"
            )
        finally:
            sp.run = orig_run  # type: ignore[assignment]
    finally:
        os.environ["PATH"] = original_path


def test_default_runner_skips_dup_when_local_bin_already_on_path(git_repo: Path) -> None:
    """When ~/.local/bin is already on PATH, _default_runner must not duplicate it.

    The runner checks before prepending so the path stays clean for callers
    that already have the directory configured.
    """
    from skill.test_cache import _default_runner

    captured_env: dict[str, str] = {}
    original_path = os.environ.get("PATH", "")
    # Prepend ~/.local/bin to our own PATH so it is already present.
    local_bin = os.path.expanduser("~/.local/bin")
    if local_bin not in original_path:
        os.environ["PATH"] = local_bin + os.pathsep + original_path

    try:
        import subprocess as sp
        orig_run = sp.run

        def fake_run(cmd, **kwargs):
            captured_env["PATH"] = kwargs.get("env", {}).get("PATH", "")
            return SimpleNamespace(stdout="ok\n", stderr="", returncode=0)

        sp.run = fake_run  # type: ignore[assignment]
        try:
            result = _default_runner("pytest -q", str(git_repo), 60)
            # Count occurrences — should be exactly one.
            parts = captured_env["PATH"].split(os.pathsep)
            count = parts.count(local_bin)
            assert count == 1, f"~/.local/bin appears {count} times in PATH: {captured_env['PATH']}"
        finally:
            sp.run = orig_run  # type: ignore[assignment]
    finally:
        os.environ["PATH"] = original_path
