"""Doc-contract tests for the pytest interpreter resolution reference.

SA-0MVK154IE007OS3D AC5 requires §1b of ``docs/dev/test-skill-reference.md``
to document the interpreter-resolution contract, and AC4 requires the field
verification (parity against a real ContentCreator checkout) to be recorded
there rather than only in a work-item comment — the audit reads the repository,
not the worklog comments.

These tests pin that documented contract so the precedence list cannot drift
back to the historical bare-``pytest``-first rule and the measured parity
evidence cannot silently disappear from the docs. Behaviour of the resolver
itself is covered by ``tests/test_pytest_path_resolution.py``; this file only
asserts the documentation contract, in the established style of
``tests/test_test_isolation_launcher_docs.py``.
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_REFERENCE_DOC = _REPO_ROOT / "docs" / "dev" / "test-skill-reference.md"


def _normalised(text: str) -> str:
    """Collapse all whitespace so assertions survive line rewrapping."""
    return " ".join(text.split())


def _section_1b() -> str:
    """Return the normalised text of §1b (until the next ``### `` heading)."""
    content = _REFERENCE_DOC.read_text(encoding="utf-8")
    start = content.find("### 1b. pytest interpreter resolution")
    assert start != -1, "test-skill-reference.md must contain the §1b section"
    end = content.find("\n### ", start + 1)
    assert end != -1 and end > start, "§1b must be followed by another section"
    return _normalised(content[start:end])


def _field_verification() -> str:
    """Return the normalised text of the §1b field-verification subsection."""
    section = _section_1b()
    start = section.find("#### Field verification")
    assert start != -1, "§1b must contain a '#### Field verification' subsection"
    return section[start:]


class TestDocumentedResolutionOrder:
    """§1b documents the fixed precedence and its probing contract."""

    def test_documents_both_explicit_overrides(self) -> None:
        section = _section_1b()
        assert "PYTEST_BIN" in section
        assert "TEST_PYTHON" in section

    def test_places_runner_interpreter_before_path_pytest(self) -> None:
        section = _section_1b()
        sys_executable = section.find("sys.executable -m pytest")
        path_pytest = section.find("`pytest` on PATH")
        assert sys_executable != -1, "§1b must document the sys.executable form"
        assert path_pytest != -1, "§1b must document the PATH pytest fallback"
        assert sys_executable < path_pytest, (
            "the runner's interpreter must be documented before the PATH "
            "pytest fallback"
        )

    def test_documents_that_candidates_are_probed(self) -> None:
        assert "probed" in _section_1b()


class TestRecordedFieldVerification:
    """§1b records the measured ContentCreator parity (AC4)."""

    def test_records_both_runners_and_the_shared_totals(self) -> None:
        field = _field_verification()
        assert "./scripts/run_tests.sh" in field
        assert "run_tests.py --type full --force --json" in field
        assert "3286 passed, 2 skipped" in field

    def test_records_the_interpreter_used(self) -> None:
        assert "/usr/bin/python3" in _field_verification()

    def test_records_the_reproduced_failure_condition(self) -> None:
        field = _field_verification()
        assert "ModuleNotFoundError: No module named 'numpy'" in field
        assert "resolve_pytest_command()" in field
