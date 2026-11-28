"""Deterministic tests for the tree-first red-diagnostics cache lookup.

Covers SA-0MWL8PX1Y004H3A8: ``_cached_red_failures`` must consult the
tree-keyed dev-push gate verdict (``git rev-parse HEAD^{tree}``) before the
legacy loose-worktree key, so a red gate verdict at the audited ``HEAD`` is
visible to the audit's red diagnostics instead of forcing a re-execution.

No real suite is executed and no live git state is used: ``_resolve_audited_tree``
and ``query_cached`` are stubbed, except for the different-tree case which
primes a temporary cache with the real store/lookup helpers (so the exclusion
is proved against the production keying, not a hand-rolled simulation).
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[3]
SKILL_ROOT = Path(__file__).resolve().parents[2]
for _p in (REPO_ROOT, SKILL_ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from audit.scripts import audit_runner
from test_cache import (
    FINGERPRINT_KIND_TREE,
    FINGERPRINT_KIND_WORKTREE,
    query_cached,
    store,
)

_COMMAND = "pytest -q"
_TREE = "treeA"
_OTHER_TREE = "treeB"


def _red_entry(*, scope: str = "full", stdout: str = "FAILED tests/x.py::t") -> dict:
    """A minimal red cache entry as ``query_cached`` returns it."""
    return {
        "exit_code": 1,
        "stdout": stdout,
        "stderr": "",
        "scope": scope,
        "completed_at": 1000.0,
    }


def _failure() -> dict:
    """A minimal parsed failure record as ``parse_failures`` returns it."""
    return {"test_name": "tests/x.py::t", "path": "tests/x.py"}


# ── AC1/AC6: tree-keyed red hit is parsed with provenance ──────────────────


def test_tree_keyed_red_hit_yields_failures_with_provenance():
    """A red tree-keyed entry at the audited HEAD yields failure records with
    ``fingerprint_kind="tree"`` and the ``tree`` sha (AC1, AC6)."""
    calls: list[tuple[str | None, str]] = []

    def fake_tree_first(command, *, cwd=None, ttl=None, fingerprint=None,
                        fingerprint_kind=FINGERPRINT_KIND_WORKTREE):
        calls.append((fingerprint, fingerprint_kind))
        if fingerprint == _TREE and fingerprint_kind == FINGERPRINT_KIND_TREE:
            return _red_entry()
        return None

    with mock.patch.object(
        audit_runner, "full_suite_commands", return_value=[_COMMAND],
    ), mock.patch.object(
        audit_runner, "_resolve_audited_tree", return_value=_TREE,
    ), mock.patch.object(
        audit_runner, "query_cached", side_effect=fake_tree_first,
    ), mock.patch.object(
        audit_runner, "parse_failures", return_value=[_failure()],
    ):
        failures = audit_runner._cached_red_failures("/tmp/project")

    assert len(failures) == 1
    assert failures[0]["fingerprint_kind"] == FINGERPRINT_KIND_TREE
    assert failures[0]["tree"] == _TREE
    assert failures[0]["suite_command"] == _COMMAND
    # The tree query was attempted first, and the tree hit means the loose
    # fallback was never consulted.
    assert calls == [(_TREE, FINGERPRINT_KIND_TREE)]


# ── AC2: legacy loose-key red entry still parses ───────────────────────────


def test_legacy_loose_key_red_entry_still_parses():
    """A legacy worktree-keyed red entry (no tree entry) parses on the loose
    fallback exactly as today, tagged ``fingerprint_kind="worktree"`` (AC2)."""
    calls: list[tuple[str | None, str]] = []

    def fake(command, *, cwd=None, ttl=None, fingerprint=None,
             fingerprint_kind=FINGERPRINT_KIND_WORKTREE):
        calls.append((fingerprint, fingerprint_kind))
        # Tree-keyed lookup misses; the legacy loose lookup holds the verdict.
        if fingerprint is not None:
            return None
        return _red_entry()

    with mock.patch.object(
        audit_runner, "full_suite_commands", return_value=[_COMMAND],
    ), mock.patch.object(
        audit_runner, "_resolve_audited_tree", return_value=_TREE,
    ), mock.patch.object(
        audit_runner, "query_cached", side_effect=fake,
    ), mock.patch.object(
        audit_runner, "parse_failures", return_value=[_failure()],
    ):
        failures = audit_runner._cached_red_failures("/tmp/project")

    # Tree consulted first, then the loose query — in that order.
    assert calls == [
        (_TREE, FINGERPRINT_KIND_TREE),
        (None, FINGERPRINT_KIND_WORKTREE),
    ]
    assert len(failures) == 1
    assert failures[0]["fingerprint_kind"] == FINGERPRINT_KIND_WORKTREE
    assert "tree" not in failures[0]


# ── AC1: a tree hit (even green) is authoritative over the loose key ────────


def test_green_tree_entry_blocks_loose_red_fallback():
    """A green tree-keyed entry is authoritative: the loose red entry is not
    consulted, so no red diagnostics are fabricated for a passed tree."""
    calls: list[tuple[str | None, str]] = []

    def fake(command, *, cwd=None, ttl=None, fingerprint=None,
             fingerprint_kind=FINGERPRINT_KIND_WORKTREE):
        calls.append((fingerprint, fingerprint_kind))
        if fingerprint == _TREE and fingerprint_kind == FINGERPRINT_KIND_TREE:
            return {"exit_code": 0, "stdout": "", "stderr": "", "scope": "full"}
        # A loose red entry would be served here if the fallback ran.
        return _red_entry()

    with mock.patch.object(
        audit_runner, "full_suite_commands", return_value=[_COMMAND],
    ), mock.patch.object(
        audit_runner, "_resolve_audited_tree", return_value=_TREE,
    ), mock.patch.object(
        audit_runner, "query_cached", side_effect=fake,
    ), mock.patch.object(
        audit_runner, "parse_failures", return_value=[_failure()],
    ):
        failures = audit_runner._cached_red_failures("/tmp/project")

    assert calls == [(_TREE, FINGERPRINT_KIND_TREE)]
    assert failures == []


# ── AC3: a red entry for a *different* tree is ignored ─────────────────────


def test_different_tree_red_entry_is_ignored(tmp_path):
    """A red tree-keyed entry recorded for another tree is never attributed to
    the audited HEAD (AC3) — proved against the real cache keying."""
    # A .worklog dir makes cache_dir() resolve inside tmp_path (isolated).
    (tmp_path / ".worklog").mkdir()
    store(
        _COMMAND,
        _OTHER_TREE,
        cwd=str(tmp_path),
        stdout="FAILED tests/x.py::t",
        stderr="",
        exit_code=1,
        scope="full",
        fingerprint_kind=FINGERPRINT_KIND_TREE,
    )
    # Sanity: the primed entry is a real, queryable red tree-keyed hit.
    primed = query_cached(
        _COMMAND,
        cwd=str(tmp_path),
        fingerprint=_OTHER_TREE,
        fingerprint_kind=FINGERPRINT_KIND_TREE,
    )
    assert primed is not None and primed["exit_code"] == 1

    with mock.patch.object(
        audit_runner, "full_suite_commands", return_value=[_COMMAND],
    ), mock.patch.object(
        audit_runner, "_resolve_audited_tree", return_value=_TREE,
    ), mock.patch.object(
        audit_runner, "query_cached", wraps=query_cached,
    ), mock.patch.object(
        audit_runner, "parse_failures", return_value=[_failure()],
    ):
        failures = audit_runner._cached_red_failures(tmp_path)

    assert failures == [], (
        "a red verdict for a different tree must never be attributed to the "
        "audited HEAD"
    )


# ── AC4: an uncomputable tree falls back to the loose query only ────────────


def test_uncomputable_tree_falls_back_to_loose_only():
    """When the audited tree cannot be resolved, only the loose query runs
    (no tree query, no crash, no fabricated failure) (AC4)."""
    calls: list[tuple[str | None, str]] = []

    def fake(command, *, cwd=None, ttl=None, fingerprint=None,
             fingerprint_kind=FINGERPRINT_KIND_WORKTREE):
        calls.append((fingerprint, fingerprint_kind))
        return _red_entry()

    with mock.patch.object(
        audit_runner, "full_suite_commands", return_value=[_COMMAND],
    ), mock.patch.object(
        audit_runner, "_resolve_audited_tree", return_value=None,
    ), mock.patch.object(
        audit_runner, "query_cached", side_effect=fake,
    ), mock.patch.object(
        audit_runner, "parse_failures", return_value=[_failure()],
    ):
        failures = audit_runner._cached_red_failures("/tmp/project")

    assert calls == [(None, FINGERPRINT_KIND_WORKTREE)]
    assert len(failures) == 1
    assert failures[0]["fingerprint_kind"] == FINGERPRINT_KIND_WORKTREE


def test_resolve_audited_tree_is_fail_soft_on_non_repo(tmp_path):
    """``_resolve_audited_tree`` returns ``None`` (never raises) when git
    cannot resolve a tree (AC4)."""
    assert audit_runner._resolve_audited_tree(str(tmp_path)) is None


# ── AC5: a single lookup path through ``_cached_red_evaluation`` ────────────


def test_cached_red_evaluation_reuses_cached_red_failures(tmp_path):
    """``_cached_red_evaluation`` obtains its failures from
    ``_cached_red_failures`` — there is no second cache query (AC5)."""
    sentinel = [{"test_name": "t", "path": "p"}]
    with mock.patch.object(
        audit_runner, "_cached_red_failures", return_value=sentinel,
    ) as failures_mock, mock.patch.object(
        audit_runner, "_evaluate_red_suite", return_value={"outcome": "blocked"},
    ) as evaluate_mock:
        result = audit_runner._cached_red_evaluation(
            tmp_path,
            strict=False,
            touched_files=[],
            key_files=[],
            baseline_commit=None,
        )

    assert result == {"outcome": "blocked"}
    failures_mock.assert_called_once_with(tmp_path)
    assert evaluate_mock.call_args.args[0] is sentinel


# ── AC7: changed-scope entries are not red diagnostics ─────────────────────


def test_changed_scope_tree_entry_is_not_red_diagnostics():
    """A changed-scope (partial) tree-keyed entry stays non-evidence, matching
    the classifier's SA-0MT6CEO700058ZEN rule (AC7)."""
    def fake(command, *, cwd=None, ttl=None, fingerprint=None,
             fingerprint_kind=FINGERPRINT_KIND_WORKTREE):
        if fingerprint == _TREE and fingerprint_kind == FINGERPRINT_KIND_TREE:
            return _red_entry(scope="changed")
        return None

    with mock.patch.object(
        audit_runner, "full_suite_commands", return_value=[_COMMAND],
    ), mock.patch.object(
        audit_runner, "_resolve_audited_tree", return_value=_TREE,
    ), mock.patch.object(
        audit_runner, "query_cached", side_effect=fake,
    ), mock.patch.object(
        audit_runner, "parse_failures", return_value=[_failure()],
    ):
        failures = audit_runner._cached_red_failures("/tmp/project")

    assert failures == []


# ── AC8: query_cached's worktree-only default is unchanged ─────────────────


def test_query_cached_default_fingerprint_kind_unchanged():
    """``query_cached`` keeps its worktree-only defaults for every other
    caller (AC8)."""
    sig = inspect.signature(query_cached)
    assert sig.parameters["fingerprint"].default is None
    assert (
        sig.parameters["fingerprint_kind"].default == FINGERPRINT_KIND_WORKTREE
    )
