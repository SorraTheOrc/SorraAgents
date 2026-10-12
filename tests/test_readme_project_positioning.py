"""Project positioning tests for the README opening paragraph.

Related work item: correct project README and GitHub description (SA-0MUZISRXD005GCXL)
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_README = _REPO_ROOT / "README.md"


def test_readme_uses_correct_project_positioning() -> None:
    """The README opening must describe the project as providing agent skills
    and workflows for software development, and mention ContextHub."""
    content = _README.read_text(encoding="utf-8")
    assert "software development" in content
    assert "ContextHub" in content


def test_readme_mentions_other_orchestration() -> None:
    """The README opening must acknowledge that skills can be used with other
    orchestration systems but are optimised for ContextHub."""
    content = _README.read_text(encoding="utf-8")
    assert "optimised" in content
    assert "ContextHub" in content


def test_readme_no_longer_uses_outdated_phrase() -> None:
    """The outdated phrase 'small automation agents' must be removed."""
    content = _README.read_text(encoding="utf-8")
    assert "small automation agents" not in content
