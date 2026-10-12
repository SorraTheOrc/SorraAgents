"""Doc-contract tests for the §9.3 external-launcher finding.

Verifies SA-0MV0G9D960078SKT (parent SA-0MUIZSXEY008NGR8): §9.3 of
``docs/dev/test-isolation.md`` must name the confirmed source of the
repository-override leak, the fix at the spawn boundary, and the evidence/fix
work items, so the finding cannot silently regress out of the documentation.

The deliverable is documentation, so these tests assert the documented
contract, in the established style of ``tests/test_worktree_isolation_docs.py``
and ``tests/test_agents_global_session_per_child.py``.
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ISOLATION_DOC = _REPO_ROOT / "docs" / "dev" / "test-isolation.md"


def _section_9_3() -> str:
    """Return the normalised text of §9.3 (until the next ``### 9.4``)."""
    content = _ISOLATION_DOC.read_text(encoding="utf-8")
    start = content.find("### 9.3")
    assert start != -1, "test-isolation.md must contain a §9.3 section"
    end = content.find("### 9.4", start)
    assert end != -1 and end > start, "§9.3 must be followed by §9.4"
    return " ".join(content[start:end].split())


class TestSection93NamesConfirmedSource:
    """§9.3 names the confirmed launcher source."""

    def test_names_build_downtime_spawn_options(self) -> None:
        assert "buildDowntimeSpawnOptions" in _section_9_3()

    def test_names_context_hub_source_file(self) -> None:
        assert "packages/herdr/src/downtime-worker.ts" in _section_9_3()

    def test_names_the_hook_signature(self) -> None:
        section = _section_9_3()
        assert "GIT_EXEC_PATH" in section
        assert "GIT_PREFIX" in section


class TestSection93DocumentsTheFix:
    """§9.3 documents the spawn-boundary fix and its cross-repo item."""

    def test_names_cross_repo_fix_item(self) -> None:
        assert "WL-0MV0TZEWZ003ZXEB" in _section_9_3()

    def test_names_the_scrub_helper(self) -> None:
        assert "scrubRepositoryOverrides" in _section_9_3()

    def test_names_shell_side_defence(self) -> None:
        section = _section_9_3()
        assert "send-to-pi.sh" in section
        assert "run-pi-agent.sh" in section


class TestSection93ReferencesEvidence:
    """§9.3 references the evidence work items and the reproducible tracer."""

    def test_references_parent_investigation(self) -> None:
        assert "SA-0MUIZSXEY008NGR8" in _section_9_3()

    def test_references_evidence_tracing_child(self) -> None:
        assert "SA-0MV0G9BF7008S01K" in _section_9_3()

    def test_references_reproducible_tracer(self) -> None:
        assert "docs/dev/trace_git_env_propagation.py" in _section_9_3()
