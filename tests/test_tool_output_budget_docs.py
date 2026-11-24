"""Tests for the tool-output budget guidance.

Asserts the observable deliverables of the rollout in two halves:

- the document-content half (SA-0MWJYHKDX006HS36) — the canonical shared doc
  exists, documents the five core points, and carries the required sections and
  traceability;
- the reference-inventory half (SA-0MWJYHX6D008ZYE6) — every listed skill,
  `AGENTS_GLOBAL.md` and the four dev skill-reference docs reference the shared
  doc individually, and no skill/reference doc recommends a bare `cat` over a
  generated artefact.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.skill_inventory import tracked_skill_names

REPO_ROOT = Path(__file__).resolve().parent.parent

SHARED_DOC = REPO_ROOT / "skill" / "shared" / "tool-output-budget.md"

#: The parent work item that owns this rollout.
PARENT_ID = "SA-0MWJWJ1HV000EBKP"

#: Evidence session that produced the measured output-budget data.
EVIDENCE_SESSION = "AH-0MWH4F375000X08G"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class TestSharedDocExists:
    """AC1 — the canonical shared doc exists."""

    def test_shared_doc_exists(self):
        assert SHARED_DOC.exists(), (
            "skill/shared/tool-output-budget.md must exist"
        )


class TestDocumentContent:
    """AC1+AC2 — the doc documents all five points and required sections."""

    def _read_doc(self):
        return _text(SHARED_DOC)

    # --- five core points ---

    def test_documentation_covers_jq_over_cat_for_artefacts(self):
        text = self._read_doc()
        assert "jq" in text.lower()
        # Must advocate jq over cat for generated artefacts.
        assert "cat" in text.lower()
        assert "generated" in text.lower() or "artifact" in text.lower() or "artefact" in text.lower()

    def test_documentation_covers_rg_m_caps(self):
        text = self._read_doc()
        assert "rg" in text.lower()
        assert "-m" in text
        # Must warn against wide first-pass context.
        assert "-A 20" in text or "first pass" in text.lower()

    def test_documentation_covers_read_with_offset_limit(self):
        text = self._read_doc()
        assert "read" in text.lower()
        assert "offset" in text.lower() or "limit" in text.lower()
        # Should mention locating a symbol first.
        assert "rg -n" in text or "locate" in text.lower() or "definition" in text.lower()

    def test_documentation_covers_no_refetching(self):
        text = self._read_doc()
        # Must warn against re-fetching context already held.
        assert "refetch" in text.lower() or "re-read" in text.lower() or "already" in text.lower() or "hold" in text.lower()

    def test_documentation_covers_kb_rule_of_thumb(self):
        text = self._read_doc()
        assert "10 KB" in text or "10kb" in text.lower()

    # --- required sections ---

    def test_contains_pattern_section(self):
        text = self._read_doc()
        assert "pattern" in text.lower()

    def test_contains_worked_examples(self):
        text = self._read_doc()
        # Each worked example pairs a WASTEFUL call with a SIZE-PROVEN one.
        wasteful = text.count("WASTEFUL")
        proven = text.count("SIZE-PROVEN")
        assert wasteful >= 3, (
            f"expected at least 3 worked examples (WASTEFUL pairs), "
            f"found {wasteful}"
        )
        assert proven >= 3, (
            f"expected at least 3 SIZE-PROVEN replacements, "
            f"found {proven}"
        )


class TestWorkItemTraceability:
    """AC3 — the doc names the parent work item and evidence session."""

    def test_names_parent_work_item(self):
        text = _text(SHARED_DOC)
        assert PARENT_ID in text

    def test_names_evidence_session(self):
        text = _text(SHARED_DOC)
        assert EVIDENCE_SESSION in text

    def test_states_single_source_of_truth(self):
        text = _text(SHARED_DOC)
        assert "single source of truth" in text.lower() or "single source of truth" in text.lower()


#: The 21 skills that ship a `SKILL.md` and must reference the shared doc
#: (parent SA-0MWJWJ1HV000EBKP Requirement 2). `context-audit` ships no
#: `SKILL.md`; `pane-triage` does not read/search as its primary loop — both
#: deliberately excluded (recorded in the parent Appendix).
REQUIRED_SKILLS = (
    "implement",
    "audit",
    "plan",
    "test",
    "refactor",
    "find-related",
    "resolve-pr-comments",
    "intake",
    "agent-log-review",
    "cleanup",
    "code-review",
    "ship",
    "triage",
    "machine-hygiene",
    "report",
    "standup",
    "test-churn",
    "effort-and-risk",
    "author-command",
    "interview",
    "mutation-test",
)

#: Dev reference docs holding relocated detail that describe reading context.
REQUIRED_REFERENCE_DOCS = (
    "implement-skill-reference.md",
    "audit-skill-reference.md",
    "plan-skill-reference.md",
    "ship-skill-reference.md",
)

GLOBAL_AGENTS = REPO_ROOT / "AGENTS_GLOBAL.md"


def _skill_md(skill: str) -> Path:
    return REPO_ROOT / "skill" / skill / "SKILL.md"


def _reference_doc(name: str) -> Path:
    return REPO_ROOT / "docs" / "dev" / name


class TestSkillInventoryReferences:
    """AC1/AC4 — every listed skill references the shared doc, one by one."""

    @pytest.mark.parametrize("skill", REQUIRED_SKILLS)
    def test_listed_skill_is_tracked(self, skill: str):
        tracked = tracked_skill_names(REPO_ROOT)
        if tracked is None:
            pytest.skip("git unavailable; cannot resolve tracked skill dirs")
        assert skill in tracked, f"skill/{skill} is not tracked by git"

    @pytest.mark.parametrize("skill", REQUIRED_SKILLS)
    def test_skill_md_references_shared_doc(self, skill: str):
        path = _skill_md(skill)
        assert path.exists(), f"skill/{skill}/SKILL.md must exist"
        assert "tool-output-budget" in _text(path), (
            f"skill/{skill}/SKILL.md must reference "
            "../shared/tool-output-budget.md"
        )


class TestGlobalAgentsReference:
    """AC2 — one global rule line carries the jq rule and the ~10 KB rule."""

    def test_global_agents_references_shared_doc(self):
        assert "tool-output-budget" in _text(GLOBAL_AGENTS), (
            "AGENTS_GLOBAL.md must reference the shared tool-output budget doc"
        )

    def test_rule_states_jq_over_cat_and_ten_kb_expectation(self):
        blocks = [
            block
            for block in _text(GLOBAL_AGENTS).split("\n\n")
            if "tool-output-budget" in block
        ]
        assert blocks, "no AGENTS_GLOBAL.md block references the shared doc"
        rule = "\n".join(blocks)
        assert "jq" in rule, "the rule line must state the jq-over-cat rule"
        assert "cat" in rule
        assert "10 KB" in rule, "the rule line must state the ~10 KB expectation"


class TestDevReferenceDocReferences:
    """AC3 — the four dev reference docs reference the shared doc."""

    @pytest.mark.parametrize("doc", REQUIRED_REFERENCE_DOCS)
    def test_reference_doc_references_shared_doc(self, doc: str):
        path = _reference_doc(doc)
        assert path.exists(), f"docs/dev/{doc} must exist"
        assert "tool-output-budget" in _text(path), (
            f"docs/dev/{doc} must reference "
            "skill/shared/tool-output-budget.md"
        )

    def test_no_find_related_reference_doc_created(self):
        # Deliberate scope decision: find-related/SKILL.md is untrimmed, so no
        # relocated-detail doc exists or should be manufactured (parent Appendix Q1).
        path = _reference_doc("find-related-skill-reference.md")
        assert not path.exists(), (
            "docs/dev/find-related-skill-reference.md must not be created"
        )


class TestSharedDocNamesPatterns:
    """AC4 — the shared doc names the jq, rg -m and read --offset patterns."""

    def test_names_jq_pattern(self):
        assert "jq" in _text(SHARED_DOC)

    def test_names_rg_m_cap_pattern(self):
        assert "rg -m" in _text(SHARED_DOC)

    def test_names_read_offset_pattern(self):
        assert "--offset" in _text(SHARED_DOC)


#: The static-context document that must cross-link the dynamic guidance.
CONTEXT_BUDGET_DOC = REPO_ROOT / "docs" / "dev" / "context-budget.md"

#: Heading that opens the static-vs-dynamic section (SA-0MWJYI6F7009CBKA).
STATIC_DYNAMIC_HEADING = "## Static vs dynamic context"


def _static_dynamic_section(path: Path) -> str:
    """Return just the static-vs-dynamic section of *path*.

    Isolating the section keeps the assertions about the 83%/10% evidence and
    the complementary wording from being satisfied by unrelated prose
    elsewhere in the document.
    """
    text = _text(path)
    start = text.find(STATIC_DYNAMIC_HEADING)
    if start < 0:
        return ""
    end = text.find("\n## ", start + len(STATIC_DYNAMIC_HEADING))
    return text[start:] if end < 0 else text[start:end]


class TestStaticDynamicCrossLink:
    """AC1–AC3 — docs/dev/context-budget.md splits static from dynamic."""

    def test_section_exists(self):
        assert _static_dynamic_section(CONTEXT_BUDGET_DOC), (
            "docs/dev/context-budget.md must carry a "
            f"'{STATIC_DYNAMIC_HEADING}' section"
        )

    def test_section_scopes_the_committed_budget_to_static_startup(self):
        section = _static_dynamic_section(CONTEXT_BUDGET_DOC).lower()
        assert "static startup" in section
        assert "runtime" in section

    def test_section_links_the_shared_tool_output_budget_doc(self):
        section = _static_dynamic_section(CONTEXT_BUDGET_DOC)
        assert "skill/shared/tool-output-budget.md" in section

    def test_section_names_the_measured_83_percent_evidence(self):
        section = _static_dynamic_section(CONTEXT_BUDGET_DOC)
        assert "83%" in section
        assert "10%" in section
        assert "409 KB" in section

    def test_section_states_complementary_not_substitutes(self):
        section = _static_dynamic_section(CONTEXT_BUDGET_DOC).lower()
        assert "complementary" in section
        assert "not substitutes" in section


#: A bare `cat` over a generated artefact offered as a recommendation.
#: Heredocs (`cat <<'JSON'`) and redirections are writes, not reads, so they
#: are excluded by requiring a plain path token after `cat`.
_BARE_CAT_ARTEFACT = re.compile(
    r"^\s*(?:[-*>]\s*)?cat\s+[^\s<|>]*\.(?:json|jsonl|lock|log)\b"
)

#: Lines explicitly labelled as the anti-pattern are illustrative, not advice.
_ANTI_PATTERN_MARKERS = ("WASTEFUL", "Never", "never")


def _bare_cat_offenders(path: Path) -> list[str]:
    offenders = []
    for line in _text(path).splitlines():
        if not _BARE_CAT_ARTEFACT.match(line):
            continue
        if any(marker in line for marker in _ANTI_PATTERN_MARKERS):
            continue
        offenders.append(f"{path.relative_to(REPO_ROOT)}: {line.strip()}")
    return offenders


class TestNoBareCatArtefactAdvice:
    """AC5 — no skill/reference doc recommends `cat <artefact>.json`."""

    def test_no_skill_md_recommends_bare_cat_of_artefact(self):
        offenders: list[str] = []
        tracked = tracked_skill_names(REPO_ROOT)
        for path in sorted((REPO_ROOT / "skill").glob("*/SKILL.md")):
            if tracked is not None and path.parent.name not in tracked:
                continue
            offenders.extend(_bare_cat_offenders(path))
        assert not offenders, (
            "skill docs recommend a bare `cat` over a generated artefact:\n"
            + "\n".join(offenders)
        )

    def test_no_dev_reference_doc_recommends_bare_cat_of_artefact(self):
        offenders: list[str] = []
        for path in sorted((REPO_ROOT / "docs" / "dev").glob("*-skill-reference.md")):
            offenders.extend(_bare_cat_offenders(path))
        assert not offenders, (
            "dev reference docs recommend a bare `cat` over a generated "
            "artefact:\n" + "\n".join(offenders)
        )
