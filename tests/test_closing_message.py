"""Tests for the closing message format in AGENTS.md and skill SKILL.md files.

This test ensures that the closing message agents output to operators
correctly reflects that work has already been committed, rather than
suggesting the operator commit the work themselves.

Related work item: SA-0MQ037ZH000403K0
"""

from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parent.parent
_PI_SKILLS_DIR = Path("/home/rgardler/.pi/agent/skills")

# Files that should contain the closing message instruction.
# Pi skills files are only checked when they exist (they may not be present on CI runners).
_TARGET_FILES = [
    _REPO_ROOT / "AGENTS.md",
]
_EXTRA_FILES = []
for _path in [_PI_SKILLS_DIR / "implement" / "SKILL.md", _PI_SKILLS_DIR / "implement-single" / "SKILL.md"]:
    if _path.exists():
        _EXTRA_FILES.append(_path)
_ALL_TARGET_FILES = _TARGET_FILES + _EXTRA_FILES

_OLD_PREAMBLE = "If you want to commit this work now I suggest the following commit message:"


def _file_content(path: Path) -> str:
    assert path.exists(), f"File not found: {path}"
    return path.read_text(encoding="utf-8")


def test_old_preamble_removed_from_all_files() -> None:
    """AC-2: The old preamble text is removed from all three files."""
    for path in _ALL_TARGET_FILES:
        content = _file_content(path)
        assert _OLD_PREAMBLE not in content, (
            f"{path.name} still contains the old preamble text"
        )


def test_new_format_present_in_all_files() -> None:
    """AC-3: The closing message format includes 'Work committed to dev'."""
    for path in _ALL_TARGET_FILES:
        content = _file_content(path)
        assert "Work committed to dev" in content, (
            f"{path.name} does not contain 'Work committed to dev'"
        )
