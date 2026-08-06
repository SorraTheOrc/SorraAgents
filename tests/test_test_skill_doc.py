"""Doc hygiene tests for the test skill.

Mirrors tests/test_implement_skill_doc_hygiene.py and
tests/test_audit_skill_doc.py patterns.
"""
import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SKILL_MD = _REPO_ROOT / "skill" / "test" / "SKILL.md"


def _skill_content() -> str:
    assert _SKILL_MD.exists(), f"test skill doc not found at {_SKILL_MD}"
    return _SKILL_MD.read_text(encoding="utf-8")


def test_test_skill_doc_exists_with_frontmatter_name() -> None:
    """SKILL.md must exist and declare frontmatter name: test."""
    content = _skill_content()
    assert content.startswith("---")
    assert re.search(r"^name:\s*test\s*$", content, re.MULTILINE)


def test_test_skill_doc_has_triggering_description() -> None:
    """The frontmatter description must trigger on test-suite queries."""
    content = _skill_content()
    assert re.search(r"^description:\s*", content, re.MULTILINE)
    assert "run all tests" in content.lower() or "test suite" in content.lower()


def test_test_skill_doc_documents_quiet_pytest_contract() -> None:
    """SKILL.md must document the quiet pytest command contract."""
    content = _skill_content()
    assert "pytest -q -r a --disable-warnings" in content


def test_test_skill_doc_documents_quiet_npm_contract() -> None:
    """SKILL.md must document the quiet npm --silent contract."""
    content = _skill_content()
    assert "npm --silent" in content


def test_test_skill_doc_has_no_bats_suite_reference() -> None:
    """SKILL.md must not reference the removed bats suite.

    The only bats suite (tests/install-worklog-plugin.bats) was deleted and
    the runner is pytest + Node only (SA-0MSHZ08O8002PYN5).
    """
    content = _skill_content()
    assert "bats" not in content.lower()


def test_test_skill_doc_has_no_dangling_any_asset_line() -> None:
    content = _skill_content()
    assert not re.search(r"^\s*-\s+any\s*$", content, re.MULTILINE)


def test_test_skill_doc_has_no_legacy_opencode_paths() -> None:
    """The doc must not reference legacy opencode paths."""
    content = _skill_content()
    assert "opencode" not in content.lower()
    assert ".command/" not in content


def test_test_skill_doc_references_triage_check_or_create() -> None:
    """The doc must reference the triage check_or_create.py helper."""
    content = _skill_content()
    assert "check_or_create.py" in content


def test_test_skill_doc_references_usefulness_evaluation() -> None:
    """The doc must document code-path usefulness evaluation."""
    content = _skill_content()
    assert "evaluate_usefulness.py" in content or "usefulness" in content.lower()


def test_test_skill_doc_references_precreated_followup_item() -> None:
    """The doc must reference the pre-created follow-up wiring item."""
    content = _skill_content()
    assert "SA-0MSAC1IAS007I3K8" in content
