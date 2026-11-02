"""Docs-consistency for the assignee-release invariant (SA-0MVVF7CZ6002UDKU).

The invariant is documented **once**, in canonical wording, in
``AGENTS_GLOBAL.md`` (the "Assignee release invariant" section), and the
skill ``SKILL.md`` files and lifecycle references cross-reference that wording
rather than restate their own copies. The documents are read as text on
purpose: the contract under test *is* the wording an operator or skill author
sees.

A regression in either direction fails here:

* the canonical wording must exist in ``AGENTS_GLOBAL.md`` and spell out the
  release obligation, the sanctioned persistent holds and the opt-out;
* every skill doc must link to that section instead of restating a divergent
  copy — the divergence this item prevents;
* the deprecated ``clear_assignee_on_exit`` flag must not reappear in the
  skill docs.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

AGENTS_GLOBAL = REPO_ROOT / "AGENTS_GLOBAL.md"

#: The canonical statement, as required verbatim by the acceptance criteria.
CANONICAL_STATEMENT = (
    "any code path that claims an `assignee` must release it on every "
    "terminal outcome; a persistent hold is represented by the producer "
    "assignee (`Map`) or `needsProducerReview`, never by a lingering agent "
    "assignee."
)

#: Every document that must cross-reference the canonical section.
CROSS_REFERENCING_DOCS: tuple[Path, ...] = (
    REPO_ROOT / "skill" / "implement" / "SKILL.md",
    REPO_ROOT / "skill" / "intake" / "SKILL.md",
    REPO_ROOT / "skill" / "audit" / "SKILL.md",
    REPO_ROOT / "skill" / "plan" / "SKILL.md",
    REPO_ROOT / "docs" / "dev" / "implement-skill-reference.md",
)

_CANONICAL_SECTION_RE = re.compile(
    r"^## Assignee release invariant\s*$", re.MULTILINE
)
_CROSS_REFERENCE_RE = re.compile(
    r"AGENTS_GLOBAL\.md#assignee-release-invariant"
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_agents_global_carries_the_canonical_statement() -> None:
    """AGENTS_GLOBAL.md must state the invariant verbatim, once."""
    content = _read(AGENTS_GLOBAL)
    assert _CANONICAL_SECTION_RE.search(content), (
        "AGENTS_GLOBAL.md lacks the `## Assignee release invariant` section"
    )
    assert CANONICAL_STATEMENT in content, (
        "AGENTS_GLOBAL.md does not carry the canonical statement verbatim"
    )


def test_agents_global_documents_the_opt_out_and_its_uses() -> None:
    """The preserve_assignee_on_exit opt-out, its legitimate uses and the
    inline-comment requirement must be documented with the invariant."""
    content = _read(AGENTS_GLOBAL)
    section = content.split("## Assignee release invariant", 1)[1]
    assert "preserve_assignee_on_exit" in section
    assert "verify_context_reduction" in section
    assert "inline comment" in section


def test_every_skill_doc_cross_references_the_canonical_section() -> None:
    """Each skill doc must link to the canonical wording, not restate it."""
    for path in CROSS_REFERENCING_DOCS:
        assert path.is_file(), f"missing {path.relative_to(REPO_ROOT)}"
        assert _CROSS_REFERENCE_RE.search(_read(path)), (
            f"{path.relative_to(REPO_ROOT)} does not cross-reference "
            "AGENTS_GLOBAL.md#assignee-release-invariant"
        )


def test_no_skill_doc_restates_a_divergent_copy() -> None:
    """The pre-invariant shorthand must not reappear in the skill docs."""
    divergent_phrases = (
        "non-empty assignee = scheduling hold; clear to release",
        "clear to release)",
    )
    for path in CROSS_REFERENCING_DOCS:
        content = _read(path)
        for phrase in divergent_phrases:
            assert phrase not in content, (
                f"{path.relative_to(REPO_ROOT)} restates a divergent copy: "
                f"{phrase!r}"
            )


def test_no_deprecated_flag_in_skill_docs() -> None:
    """The deprecated clear_assignee_on_exit flag must not reappear in docs."""
    for path in CROSS_REFERENCING_DOCS:
        assert "clear_assignee_on_exit" not in _read(path), (
            f"{path.relative_to(REPO_ROOT)} still references the deprecated "
            "clear_assignee_on_exit flag"
        )
