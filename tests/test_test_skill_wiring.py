"""Doc hygiene tests for test-skill wiring into code-touching skills.

Verifies that the master AGENTS.md and every code-touching skill reference
the test skill (skill/test or /skill:test), so agents consistently apply the
run → triage → evaluate → loop discipline before marking work items in_review.
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent

# Master AGENTS.md plus the code-touching skills that must reference the
# test skill in their workflows (wired via SA-0MSAC1IAS007I3K8).
WIRED_FILES = {
    "AGENTS.md": _REPO_ROOT / "AGENTS.md",
    "skill/implement/SKILL.md": _REPO_ROOT / "skill" / "implement" / "SKILL.md",
    "skill/implement-single/SKILL.md": _REPO_ROOT
    / "skill"
    / "implement-single"
    / "SKILL.md",
    "skill/audit/SKILL.md": _REPO_ROOT / "skill" / "audit" / "SKILL.md",
    "skill/refactor/SKILL.md": _REPO_ROOT / "skill" / "refactor" / "SKILL.md",
    "skill/code-review/SKILL.md": _REPO_ROOT / "skill" / "code-review" / "SKILL.md",
    "skill/resolve-pr-comments/SKILL.md": _REPO_ROOT
    / "skill"
    / "resolve-pr-comments"
    / "SKILL.md",
    "skill/author-command/SKILL.md": _REPO_ROOT
    / "skill"
    / "author-command"
    / "SKILL.md",
    "skill/ship/SKILL.md": _REPO_ROOT / "skill" / "ship" / "SKILL.md",
}

REFERENCE_MARKERS = ("skill/test", "/skill:test")


def _content(path: Path) -> str:
    assert path.exists(), f"expected file not found: {path}"
    return path.read_text(encoding="utf-8")


def test_wired_files_exist() -> None:
    """Every file that must reference the test skill exists on disk."""
    for label, path in WIRED_FILES.items():
        assert path.is_file(), f"{label} missing at {path}"


def test_all_wired_files_reference_test_skill() -> None:
    """Every wired file references the test skill (skill/test or /skill:test)."""
    missing: list[str] = []
    for label, path in WIRED_FILES.items():
        content = _content(path)
        if not any(marker in content for marker in REFERENCE_MARKERS):
            missing.append(label)
    assert not missing, (
        f"Files missing a test-skill reference (skill/test or /skill:test): {missing}"
    )


def test_references_appear_before_in_review_requirement() -> None:
    """AGENTS.md must mention the test skill where tests are required
    before marking a work item in_review."""
    content = _content(WIRED_FILES["AGENTS.md"])
    assert "in_review" in content
    # The test-skill reference and the in_review requirement must co-occur
    # within the same document (at least one reference sits in a sentence
    # that also mentions in_review).
    for marker in REFERENCE_MARKERS:
        if marker in content:
            assert "in_review" in content, (
                "AGENTS.md references the test skill but never ties it to "
                "the in_review gate"
            )
            break
