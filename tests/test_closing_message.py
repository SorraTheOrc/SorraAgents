"""Tests for the closing message format in AGENTS_GLOBAL.md and skill SKILL.md files.

This test ensures that the closing message agents output to operators
correctly reflects that work has already been committed, rather than
suggesting the operator commit the work themselves.

The closing-message instruction lives in AGENTS_GLOBAL.md (the global
agent guidance installed to ~/.pi/agent/AGENTS.md) and in the canonical
skill/implement/SKILL.md. Both are checked in the repository checkout so the
test is hermetic and worktree-local — it never depends on where (or whether)
the global skill symlink is installed on the running machine.

Related work item: SA-0MQ037ZH000403K0
"""

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent

# Files that should contain the closing message instruction. The canonical
# repository copies are authoritative; the installed global skill is a
# symlink to this repository's skill/ directory.
_TARGET_FILES = [
    _REPO_ROOT / "AGENTS_GLOBAL.md",
    _REPO_ROOT / "skill" / "implement" / "SKILL.md",
]
_ALL_TARGET_FILES = list(_TARGET_FILES)

_OLD_PREAMBLE = "If you want to commit this work now I suggest the following commit message:"


def _normalise_whitespace(content: str) -> str:
    """Collapse Markdown source whitespace (incl. line wraps) to single spaces.

    The conventions below are prose; a hard line wrap in the Markdown source
    (e.g. ``Work\n  committed to dev``) must not make a literal-substring
    check report the convention as missing (SA-0MWEHR3K6006KO62).
    """
    return re.sub(r"\s+", " ", content).strip()


def _contains_phrase(content: str, phrase: str) -> bool:
    """Return True if *phrase* appears, ignoring Markdown source wrapping."""
    return phrase in _normalise_whitespace(content)


def _file_content(path: Path) -> str:
    assert path.exists(), f"File not found: {path}"
    return path.read_text(encoding="utf-8")


def test_phrase_detection_rejects_genuinely_missing_phrase() -> None:
    """Guard the guard: normalisation must not make an absent phrase pass."""
    assert not _contains_phrase("nothing relevant here", "Work committed to dev")
    assert _contains_phrase("Work\n  committed to dev", "Work committed to dev")


def test_old_preamble_removed_from_all_files() -> None:
    """AC-2: The old preamble text is removed from all three files."""
    for path in _ALL_TARGET_FILES:
        content = _file_content(path)
        assert not _contains_phrase(content, _OLD_PREAMBLE), (
            f"{path.name} still contains the old preamble text"
        )


def test_new_format_present_in_all_files() -> None:
    """AC-3: The closing message format includes 'Work committed to dev'."""
    for path in _ALL_TARGET_FILES:
        content = _file_content(path)
        assert _contains_phrase(content, "Work committed to dev"), (
            f"{path.name} does not contain 'Work committed to dev'"
        )
