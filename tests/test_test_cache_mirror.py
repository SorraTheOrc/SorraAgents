"""Cache grafting: an isolated run's entries reach the main checkout.

The audit skill's F3 execution worktree has no ``.worklog`` of its own, so its
cache entries live under ``<git-dir>/test-cache`` and are removed with the
worktree. ``mirror_cache_entries`` grafts each missing entry into the main
checkout's ``.worklog/cache`` so read-only consumers (later audits, post-push
verification) serve the verdict instead of re-executing the suite
(WL-0MVSPZ1FH000RWET AC2/AC3).

These are unit tests of the graft's observable contract: entries the
destination lacks are copied, an existing entry is never overwritten, and a
same-cache mirror is a no-op.
"""
from __future__ import annotations

import json

import test_cache


def _make_cache(root):
    """Create a worklog-backed cache root and return its cache directory."""
    (root / ".worklog").mkdir(parents=True, exist_ok=True)
    return root / ".worklog" / "cache"


def _write_entry(cache_dir, name, exit_code=0):
    """Write a minimal cache entry directory under *cache_dir*."""
    entry = cache_dir / name
    entry.mkdir(parents=True)
    (entry / "metadata.json").write_text(
        json.dumps({"version": 1, "exit_code": exit_code})
    )
    (entry / "stdout.txt").write_text(f"output for {name}")
    (entry / "stderr.txt").write_text("")
    return entry


def test_mirrors_missing_entries_into_destination(tmp_path):
    """A destination that lacks an entry receives a byte-identical copy."""
    source_cache = _make_cache(tmp_path / "worktree")
    destination_cache = _make_cache(tmp_path / "main")
    _write_entry(source_cache, "entry-a")
    _write_entry(source_cache, "entry-b")

    mirrored = test_cache.mirror_cache_entries(
        tmp_path / "worktree", tmp_path / "main"
    )

    assert sorted(mirrored) == ["entry-a", "entry-b"]
    for name in ("entry-a", "entry-b"):
        assert (destination_cache / name / "metadata.json").is_file()
        assert (destination_cache / name / "stdout.txt").read_text() == (
            f"output for {name}"
        )


def test_never_overwrites_an_existing_entry(tmp_path):
    """An entry already present in the destination is left untouched."""
    source_cache = _make_cache(tmp_path / "worktree")
    destination_cache = _make_cache(tmp_path / "main")
    _write_entry(source_cache, "shared-key", exit_code=1)
    existing = _write_entry(destination_cache, "shared-key", exit_code=0)

    mirrored = test_cache.mirror_cache_entries(
        tmp_path / "worktree", tmp_path / "main"
    )

    assert mirrored == []
    meta = json.loads((existing / "metadata.json").read_text())
    assert meta["exit_code"] == 0, "the destination's verdict must win"


def test_same_cache_is_a_noop(tmp_path):
    """Mirroring a root into its own cache does nothing."""
    _make_cache(tmp_path / "repo")
    _write_entry(tmp_path / "repo" / ".worklog" / "cache", "only-entry")

    assert test_cache.mirror_cache_entries(tmp_path / "repo", tmp_path / "repo") == []


def test_missing_source_cache_is_a_noop(tmp_path):
    """A source without a cache directory yields nothing, never raises."""
    _make_cache(tmp_path / "main")
    assert (
        test_cache.mirror_cache_entries(tmp_path / "absent", tmp_path / "main")
        == []
    )


def test_mirrored_entry_is_queryable_from_the_destination(tmp_path):
    """A grafted entry is served by a read-only query in the destination.

    The key is content-addressed (command + git state), so the entry a source
    run recorded under a state is visible to a ``query_cached`` performed for
    that same state from the destination checkout.
    """
    source = tmp_path / "worktree"
    destination = tmp_path / "main"
    _make_cache(source)
    _make_cache(destination)
    command = "pytest -q"
    state = "deadbeef" * 8
    test_cache.store(
        command,
        state,
        cwd=source,
        stdout="3 passed",
        stderr="",
        exit_code=0,
    )

    test_cache.mirror_cache_entries(source, destination)

    hit = test_cache.query_cached(
        command,
        cwd=destination,
        fingerprint=state,
        fingerprint_kind=test_cache.FINGERPRINT_KIND_WORKTREE,
    )
    assert hit is not None
    assert "3 passed" in hit["stdout"]
