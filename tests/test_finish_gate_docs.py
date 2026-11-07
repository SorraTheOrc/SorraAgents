"""Doc-contract tests for the finish gate and the cache fingerprint reference.

SA-0MVQHVKUH009VQMD (F9) requires the framework docs to be the single source
of truth for the finish gate's tree-bound verdicts and for the cache
fingerprint contract that binds them:

- ``docs/dev/implement-skill-reference.md`` documents the gate semantics
  (the suite runs against the tree the commit will produce), the verdict
  provenance fields, the abort behaviour, the single-execution guarantee and
  the absence of a default bypass.
- ``docs/dev/test-skill-reference.md`` documents the tree-SHA fingerprint
  (``compute_tree_state`` / ``git write-tree``), the explicit-fingerprint
  override on ``run_cached``/``lookup``/``store``, the ``GIT_INDEX_FILE`` env
  pass-through and the legacy-entry miss.

These tests pin the *documentation contract* the audit reads (the audit sees
the repository, not the work-item comments). The behavioural contracts
themselves are covered where the code lives: ``tests/test_test_cache.py``,
``tests/test_git_index_file_env.py`` and
``skill/implement/tests/test_finish_gate_*.py`` — this module does not
re-implement them.

Two assertions are genuine doc↔code cross-checks rather than prose greps:
the provenance keys are read from the gate's own attempt record, and the
fingerprint-kind names are read from ``test_cache``'s exported constants, so
the docs cannot drift from the implementation silently.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
for _path in (str(_REPO_ROOT), str(_REPO_ROOT / "skill")):
    if _path not in sys.path:
        sys.path.append(_path)

from test_cache import FINGERPRINT_KIND_TREE, FINGERPRINT_KIND_WORKTREE

from skill.implement.scripts import implement

_IMPLEMENT_DOC = _REPO_ROOT / "docs" / "dev" / "implement-skill-reference.md"
_TEST_SKILL_DOC = _REPO_ROOT / "docs" / "dev" / "test-skill-reference.md"


def _normalised(path: Path) -> str:
    """Collapse whitespace so assertions survive line rewrapping."""
    assert path.exists(), f"reference doc not found at {path}"
    return " ".join(path.read_text(encoding="utf-8").split())


def _section(document: str, start_marker: str, *, stop_marker: str) -> str:
    """Return *document* from *start_marker* until the next *stop_marker*."""
    start = document.find(start_marker)
    assert start != -1, f"document must contain {start_marker!r}"
    end = document.find(stop_marker, start + len(start_marker))
    assert end != -1 and end > start, (
        f"{start_marker!r} must be followed by another {stop_marker!r} section"
    )
    return document[start:end]


def _finish_gate_section() -> str:
    return _section(
        _normalised(_IMPLEMENT_DOC),
        "### Finish gate: testing the tree the commit produces",
        stop_marker="## Push timeout",
    )


def _cache_fingerprint_section() -> str:
    return _section(
        _normalised(_TEST_SKILL_DOC),
        "#### Cache fingerprint contract",
        stop_marker="### 0.5. Concurrency bounding",
    )


class TestFinishGateSemantics:
    """The finish gate's semantics: it tests the commit's tree, not the
    loose working tree."""

    def test_documents_the_would_be_commit_tree(self) -> None:
        section = _finish_gate_section()
        assert "would-be-commit tree" in section
        assert "`git write-tree`" in section
        assert "`GIT_INDEX_FILE`" in section
        # The staging/verification entry points an implementer must know.
        assert "`run_gate_suite()`" in section
        assert "`stage_tested_tree()`" in section
        assert "report[\"steps\"][\"would_be_commit\"]" in section

    def test_documents_verdict_provenance_fields_from_the_gate_record(self) -> None:
        # The keys the gate actually records — not a hand-copied list.
        record = implement._gate_attempt_record(
            1,
            {
                "success": True,
                "failures": [],
                "skipped": False,
                "tooling": "suite",
                "scope": "changed",
                "tree": "/tmp/worktree",
                "git_fingerprint": "0" * 40,
                "cached": True,
                "cache_entry": {
                    "command": "pytest -q",
                    "completed_at": 1000.0,
                    "scope": "changed",
                    "fingerprint_kind": FINGERPRINT_KIND_TREE,
                },
            },
        )
        section = _finish_gate_section()
        for key in record:
            assert f"`{key}`" in section, (
                f"the verdict-provenance table must document the {key!r} field"
            )
        # The provenance names the tested tree, its fingerprint and reuse.
        assert "would-be-commit tree" in section
        assert "`fingerprint_kind=\"tree\"`" in section

    def test_documents_abort_behaviour(self) -> None:
        section = _finish_gate_section()
        assert "Abort behaviour: a failed gate leaves no trace" in section
        assert "`HEAD` is unchanged" in section
        assert "No residual staging" in section
        assert "removed on every exit path" in section
        assert "`gate evidence invalid`" in section

    def test_documents_single_execution_guarantee(self) -> None:
        section = _finish_gate_section()
        assert "Single-execution guarantee" in section
        assert "one" in section and "suite execution per gate verdict" in section
        assert "cache-served" in section and "served, not produced" in section

    def test_documents_the_absence_of_a_default_bypass(self) -> None:
        section = _finish_gate_section()
        assert "No default bypass" in section
        assert "no environment-variable or default bypass" in section
        # The backstop that makes a human `skip` safe to allow.
        assert "pre-push hook" in section
        assert "full" in section and "suite" in section

    def test_documents_the_cache_binding(self) -> None:
        section = _finish_gate_section()
        assert "`run_tests(..., fingerprint=...)`" in section
        assert "`fingerprint_kind=\"tree\"`" in section
        # A legacy or foreign entry must miss, not be served.
        assert "miss" in section


class TestCacheFingerprintContract:
    """The cache fingerprint contract: tree SHA, explicit override, env
    pass-through and the legacy-entry miss."""

    def test_documents_the_tree_fingerprint(self) -> None:
        section = _cache_fingerprint_section()
        assert "`compute_tree_state(" in section
        assert "`git write-tree`" in section
        assert "`compute_git_state(" in section

    def test_documents_the_provenance_kinds_from_the_exported_constants(self) -> None:
        section = _cache_fingerprint_section()
        assert FINGERPRINT_KIND_WORKTREE == "worktree"
        assert FINGERPRINT_KIND_TREE == "tree"
        for name, value in (
            ("FINGERPRINT_KIND_WORKTREE", FINGERPRINT_KIND_WORKTREE),
            ("FINGERPRINT_KIND_TREE", FINGERPRINT_KIND_TREE),
        ):
            assert f"`{name}` (`\"{value}\"`)" in section, (
                f"the docs must name {name} with its actual value {value!r}"
            )
        assert "`fingerprint_kind`" in section

    def test_documents_the_explicit_fingerprint_override(self) -> None:
        section = _cache_fingerprint_section()
        assert "Explicit-fingerprint override" in section
        assert "`run_cached(..., fingerprint=" in section
        assert "`lookup(..., fingerprint_kind=...)`" in section
        assert "`store(..., fingerprint_kind=...)`" in section
        # The override must be documented as authoritative, not recomputed.
        assert "as-is" in section

    def test_documents_the_git_index_file_pass_through(self) -> None:
        section = _cache_fingerprint_section()
        assert "`GIT_INDEX_FILE` pass-through" in section
        assert "`scrub_repository_overrides()`" in section
        assert "`subprocess_env_with_scrubbed_overrides()`" in section
        assert "`REPOSITORY_OVERRIDE_ENV_VARS`" in section
        assert "`tests/test_git_index_file_env.py`" in section

    def test_documents_the_legacy_entry_miss(self) -> None:
        section = _cache_fingerprint_section()
        assert "Legacy and foreign entries miss" in section
        assert "legacy" in section and "tree request always misses it" in section
        assert "`query_cached()`" in section
