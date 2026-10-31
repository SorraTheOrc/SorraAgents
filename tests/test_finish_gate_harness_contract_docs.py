"""Doc- and code-contract tests for the ``phase_finish`` harness contract.

``phase_finish`` runs its test gate against the *would-be-commit tree*
(``run_gate_suite``, SA-0MVQHVGFR006W8LG) and stages the change set in the
real index through ``stage_tested_tree`` once the gate is green. A harness
that drives ``phase_finish`` from a bare temp directory must therefore stub
both seams, and any **fixture** git commands it runs must use a hermetic
environment (``shared.git_sandbox.fixture_git_env``) because the gate exports
its temporary index as ``GIT_INDEX_FILE``.

The failing state this guards against (SA-0MVVX85KW001ZHE7): four harnesses
stubbed only ``run_tests``, so the real temp-index staging ran against a
non-git directory (``RuntimeError``) — and, once the harvest worked, fixture
``git add`` calls wrote into the *gate's* index and invalidated the verdict
with a "gate evidence invalid" notice.

The deliverable is the documented contract (the code change landed with
SA-0MVTE8XAJ002IUDR), so these tests assert the documentation states it and
that the four harnesses still honour it — the established doc-contract style
of ``tests/test_worktree_isolation_docs.py``.
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_REFERENCE_DOC = _REPO_ROOT / "docs" / "dev" / "implement-skill-reference.md"

_HARNESSES = (
    "skill/implement/tests/test_finish_main_checkout_guard.py",
    "skill/implement/tests/test_finish_main_checkout_lock.py",
    "skill/implement/tests/test_finish_no_diff_status.py",
    "tests/test_implement_finish_local_parent_branch.py",
)

_SECTION_HEADING = "#### Harness contract for `phase_finish`"


def _read(path: Path) -> str:
    assert path.exists(), f"File not found: {path}"
    return path.read_text(encoding="utf-8")


def _harness_section() -> str:
    """The documented harness-contract section, normalised to one line."""
    content = _read(_REFERENCE_DOC)
    start = content.find(_SECTION_HEADING)
    assert start != -1, (
        f"{_REFERENCE_DOC.name} must document {_SECTION_HEADING!r} — the "
        "harness contract for the would-be-commit gate boundary"
    )
    # The section runs until the next markdown heading of any level.
    rest = content[start + len(_SECTION_HEADING):]
    end = len(rest)
    for candidate in ("\n#", "\n##", "\n###", "\n####"):
        position = rest.find(candidate)
        if position != -1:
            end = min(end, position)
    return " ".join(rest[:end].split())


class TestDocumentedHarnessContract:
    """The reference doc states both stub seams and the hermetic env rule."""

    def test_documents_run_gate_suite_stub(self) -> None:
        section = _harness_section()
        assert "implement.run_gate_suite" in section
        assert "index_unchanged=True" in section

    def test_documents_stage_tested_tree_stub(self) -> None:
        section = _harness_section()
        assert "implement.stage_tested_tree" in section
        assert '"success": True' in section

    def test_documents_hermetic_fixture_environment(self) -> None:
        section = _harness_section()
        assert "shared.git_sandbox.fixture_git_env()" in section
        assert "GIT_INDEX_FILE" in section

    def test_names_the_gate_evidence_failure_mode(self) -> None:
        section = _harness_section()
        assert "gate evidence invalid" in section


class TestHarnessesHonourTheContract:
    """Every ``phase_finish`` harness stubs both seams (no stale boundary)."""

    def test_each_harness_stubs_both_seams(self) -> None:
        for relative in _HARNESSES:
            source = _read(_REPO_ROOT / relative)
            missing = [
                seam
                for seam in ("run_gate_suite", "stage_tested_tree")
                if seam not in source
            ]
            assert not missing, f"{relative} must stub {missing}"

    def test_fixture_git_module_uses_hermetic_env(self) -> None:
        source = _read(_REPO_ROOT / "tests/test_implement_finish_local_parent_branch.py")
        assert "fixture_git_env" in source, (
            "the local-parent harness runs fixture git commands; it must use "
            "shared.git_sandbox.fixture_git_env() so an ambient GIT_INDEX_FILE "
            "exported by the finish gate cannot redirect them"
        )
