"""Docs-vs-source consistency for the stale-worktree-debris filter.

WL-0MWQLHDOS006CAIH. An operator (or agent) who hits the placement guard reads
`docs/dev/worktree-isolation.md` § "The placement guard" and
`docs/dev/implement-skill-reference.md` § "Stale worktree debris is never
evidence" to predict *which* sibling worktrees refuse a clean child, and
`skill/implement/SKILL.md` step 3 to find the prune remedy. A helper renamed in
the source, a dropped mention of the fail-closed rule, or a test reference that
no longer resolves would silently leave that guidance wrong, so this file checks
it in both directions:

* every guard symbol the documents name still exists in
  `skill/implement/scripts/implement.py`, and
* the documented test references resolve to real files, classes and functions.

The documents are read as text on purpose: the contract under test *is* the
wording an operator sees.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) in sys.path:
    sys.path.remove(str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT))

from skill.implement.scripts import implement

REFERENCE_DOC = REPO_ROOT / "docs" / "dev" / "implement-skill-reference.md"
WORKTREE_DOC = REPO_ROOT / "docs" / "dev" / "worktree-isolation.md"
SKILL_MD = REPO_ROOT / "skill" / "implement" / "SKILL.md"

#: Helpers the reference document names, and which must therefore still exist.
GUARD_SYMBOLS: tuple[str, ...] = (
    "_is_live_foreign_surface",
    "_worktree_path_to_item_id",
    "_worktree_owner_is_terminal",
)

#: The documents may name a test file by bare name (they live in these dirs).
TEST_DIRS: tuple[Path, ...] = (
    REPO_ROOT / "skill" / "implement" / "tests",
    REPO_ROOT / "tests",
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _section(path: Path, heading: str) -> str:
    """The markdown of *heading*'s section, up to the next same-level heading."""
    lines = _read(path).splitlines()
    level = len(heading) - len(heading.lstrip("#"))
    start = next(
        index for index, line in enumerate(lines) if line.startswith(heading)
    )
    end = next(
        (
            index
            for index, line in enumerate(lines[start + 1 :], start=start + 1)
            if re.match(rf"^#{{1,{level}}} ", line)
        ),
        len(lines),
    )
    return "\n".join(lines[start:end])


@pytest.fixture(scope="module")
def debris_section() -> str:
    return _section(REFERENCE_DOC, "## Stale worktree debris is never evidence")


@pytest.fixture(scope="module")
def placement_section() -> str:
    return _section(WORKTREE_DOC, "## The placement guard")


@pytest.mark.parametrize("symbol", GUARD_SYMBOLS)
def test_documented_helpers_exist_in_the_source(
    symbol: str, debris_section: str
) -> None:
    """A helper the reference names must exist under that exact name."""
    assert symbol in debris_section, (
        f"the debris section does not document {symbol!r}, so an operator "
        "cannot find the rule that decides what refuses a clean child"
    )
    assert hasattr(implement, symbol), (
        f"the debris section documents {symbol!r}, which no longer exists in "
        "skill/implement/scripts/implement.py"
    )


def test_fail_closed_rule_is_documented(debris_section: str) -> None:
    """Unresolved ownership keeping the surface must be stated explicitly."""
    assert "fail-closed" in debris_section, (
        "the debris section must state that unresolved ownership stays "
        "fail-closed — otherwise the filter reads as a blanket weakening"
    )


def test_both_debris_classes_and_the_log_line_are_documented(
    debris_section: str,
) -> None:
    """The two skipped classes and the operator-facing log line are named."""
    # Reflowed prose: the phrase may wrap across lines in the document.
    lowered = " ".join(debris_section.split()).lower()
    assert "outside the repository's worktree root" in lowered
    assert "terminal" in lowered
    assert "LOG.info" in debris_section or "log line" in lowered, (
        "the section must say that a skipped surface is reported by one "
        "informational log line"
    )


def test_placement_guard_section_names_the_filter_and_links_the_remedy(
    placement_section: str,
) -> None:
    """The canonical placement-gate page is updated, not just the reference."""
    assert "_is_live_foreign_surface" in placement_section, (
        "the placement-guard section of docs/dev/worktree-isolation.md must "
        "name the live-surface filter"
    )
    assert "#stale-worktree-debris-the-prune-remedy" in placement_section, (
        "the placement-guard section must link the prune remedy"
    )
    # The linked heading must exist, or the anchor is dead.
    assert "### Stale worktree debris: the prune remedy" in _read(WORKTREE_DOC)


def test_skill_md_hygiene_step_offers_the_remedy() -> None:
    """SKILL.md's stash/worktree hygiene step points at the debris report."""
    content = _read(SKILL_MD)
    assert "stale worktree debris" in content.lower(), (
        "skill/implement/SKILL.md step 3 must mention the debris report"
    )
    assert "git worktree remove" in content and "git worktree prune" in content, (
        "SKILL.md must print the exact prune remedy an operator runs"
    )


def test_documented_test_references_resolve() -> None:
    """Every test file and test class the debris section names must exist."""
    section = _section(REFERENCE_DOC, "## Stale worktree debris is never evidence")
    files = [
        REPO_ROOT / name
        for name in re.findall(r"((?:skill|tests)/[\w/\.-]+\.py)", section)
    ]
    assert files, (
        "the debris section references no test file: it must name the "
        "regression pins for the behaviour it documents"
    )
    contents = []
    for path in files:
        assert path.is_file(), (
            f"the debris section references {path.relative_to(REPO_ROOT)}, "
            "which does not exist"
        )
        contents.append(_read(path))
    joined = "\n".join(contents)
    for class_name in re.findall(r"`(Test\w+)`", section):
        assert re.search(rf"^class {re.escape(class_name)}\b", joined, re.MULTILINE), (
            f"the debris section names {class_name}, which no referenced test "
            "file defines"
        )
