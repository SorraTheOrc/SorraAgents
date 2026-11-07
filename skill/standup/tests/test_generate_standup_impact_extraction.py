"""Tests: edge cases of ``## Impact`` extraction (SA-0MW6EUQ16003QBVL).

``generate_standup.py`` reads an item's explicit ``## Impact`` sentence into a
structured ``impact`` field for the standup report. The basic contract —
present, absent, empty, heading-only, ``None``/empty description, ``**``
emphasis, bullet prefixes, wrapped sentences — is already pinned by
``test_generate_standup_impact_field.py`` (SA-0MW6ERJ1A001YYJ0), and the
rendering fallback by ``test_generate_standup_impact_render.py``
(SA-0MW6EUQVW008N4BA). This file covers only the **gaps** those leave open, so
no behaviour is asserted twice.

Behaviour pinned here:
  - heading variants: ``# Impact``, ``### Impact`` terminated by ``##``, and
    any following heading terminating the section *without* leaking heading
    markup into the sentence;
  - line endings and whitespace: CRLF descriptions, indented headings with
    trailing spaces, and a whitespace-only body yielding ``""``;
  - body shapes: the first paragraph only (a later paragraph is excluded), a
    numbered-list prefix stripped, and a body on the line right after the
    heading;
  - section selection: the first ``## Impact`` wins, word-boundary prefixes
    (``## Impacts``, ``## Impactful changes``) never match;
  - type safety: every non-string description yields ``""`` without raising;
  - markdown emphasis: single-asterisk and single-underscore emphasis are
    trimmed while snake_case identifiers survive;
  - the pipeline contract: ``normalize_item`` carries the extraction into the
    ``impact`` field and leaves ``""`` (fallback) when there is no section.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
_SKILLS_ROOT = REPO_ROOT / "skill"
if str(_SKILLS_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILLS_ROOT))


def _reload_standup():
    """Import a fresh generate_standup module from the repo skill tree."""
    spec_path = REPO_ROOT / "skill" / "standup" / "scripts" / "generate_standup.py"
    spec = importlib.util.spec_from_file_location("generate_standup", str(spec_path))
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# ── heading variants ───────────────────────────────────────────────────

class TestImpactHeadingVariants:
    def test_top_level_heading_is_recognised(self):
        """``# Impact`` (level 1) ends any section, not just ``##`` headings."""
        mod = _reload_standup()
        desc = "# Description\n\nSome context.\n\n# Impact\n\nUsers gain speed.\n"
        assert mod.extract_impact(desc) == "Users gain speed."

    def test_level_three_section_ends_at_shallower_heading(self):
        mod = _reload_standup()
        desc = "### Impact\n\nUsers gain speed.\n\n## Acceptance Criteria\n\n1. nope.\n"
        assert mod.extract_impact(desc) == "Users gain speed."

    def test_subheading_terminates_section_without_leaking_markup(self):
        """A deeper heading straight after the sentence must not bleed in.

        The ``####`` markers of a following sub-heading are markdown noise: a
        spoken line must never contain them (AC3 of SA-0MW6ER1NX0094QCG).
        """
        mod = _reload_standup()
        desc = "## Impact\n\nUsers gain speed.\n#### Detail\n\nImplementation note.\n"
        assert mod.extract_impact(desc) == "Users gain speed."

    def test_heading_as_only_body_content_yields_empty(self):
        mod = _reload_standup()
        assert mod.extract_impact("## Impact\n### Notes\n") == ""


# ── line endings and whitespace ────────────────────────────────────────

class TestImpactWhitespace:
    def test_crlf_description(self):
        mod = _reload_standup()
        desc = "## Impact\r\n\r\nUsers gain speed.\r\n\r\n## Notes\r\n\r\nx\r\n"
        assert mod.extract_impact(desc) == "Users gain speed."

    def test_indented_heading_with_trailing_spaces(self):
        mod = _reload_standup()
        assert mod.extract_impact("   ## Impact   \n\nUsers gain speed.\n") == (
            "Users gain speed."
        )

    def test_whitespace_only_body_returns_empty(self):
        mod = _reload_standup()
        assert mod.extract_impact("## Impact\n\n   \n\t\n## Notes\n\nx\n") == ""


# ── body shapes ────────────────────────────────────────────────────────

class TestImpactBodyShapes:
    def test_first_paragraph_only(self):
        """Only the first paragraph is the sentence; later prose is not read."""
        mod = _reload_standup()
        desc = "## Impact\n\nUsers gain speed.\n\nA second paragraph.\n"
        assert mod.extract_impact(desc) == "Users gain speed."

    def test_numbered_list_prefix_is_stripped(self):
        mod = _reload_standup()
        assert mod.extract_impact("## Impact\n\n1. Users gain speed.\n") == (
            "Users gain speed."
        )

    def test_body_on_line_immediately_after_heading(self):
        mod = _reload_standup()
        assert mod.extract_impact("## Impact\nUsers gain speed.\n") == (
            "Users gain speed."
        )


# ── section selection ──────────────────────────────────────────────────

class TestImpactSectionSelection:
    def test_first_section_wins(self):
        mod = _reload_standup()
        desc = "## Impact\n\nFirst sentence.\n\n## Impact\n\nSecond sentence.\n"
        assert mod.extract_impact(desc) == "First sentence."

    def test_word_boundary_prefix_is_not_matched(self):
        mod = _reload_standup()
        for desc in (
            "## Impacts\n\nSomething else.\n",
            "## Impactful changes\n\nSomething else.\n",
        ):
            assert mod.extract_impact(desc) == "", desc


# ── type safety ────────────────────────────────────────────────────────

class TestImpactNonStringDescriptions:
    def test_every_non_string_description_yields_empty(self):
        mod = _reload_standup()
        for value in (
            None,
            "",
            "   \n\t\n",
            5,
            ["## Impact", "Users gain speed."],
            {"impact": "Users gain speed."},
            b"## Impact\n\nUsers gain speed.\n",
        ):
            assert mod.extract_impact(value) == "", repr(value)


# ── markdown emphasis ──────────────────────────────────────────────────

class TestImpactMarkdownEmphasis:
    def test_single_asterisk_emphasis_is_stripped(self):
        mod = _reload_standup()
        desc = "## Impact\n\nUsers gain *noticeably* faster setup.\n"
        assert mod.extract_impact(desc) == "Users gain noticeably faster setup."

    def test_single_underscore_emphasis_is_stripped(self):
        mod = _reload_standup()
        desc = "## Impact\n\nUsers gain _noticeably_ faster setup.\n"
        assert mod.extract_impact(desc) == "Users gain noticeably faster setup."

    def test_snake_case_identifier_is_preserved(self):
        """``deck_setup`` is an identifier, not emphasis — it must survive."""
        mod = _reload_standup()
        desc = "## Impact\n\nUsers can run deck_setup in one click.\n"
        assert mod.extract_impact(desc) == "Users can run deck_setup in one click."


# ── pipeline contract ──────────────────────────────────────────────────

class TestImpactPipeline:
    def test_normalize_item_carries_edge_case_extraction(self):
        mod = _reload_standup()
        description = (
            "### Impact\r\n\r\n"
            "Users gain *noticeably* faster setup.\r\n\r\n"
            "## Acceptance Criteria\r\n\r\n1. x\r\n"
        )
        item = mod.normalize_item(
            {"id": "SA-1", "title": "T", "description": description}
        )
        assert item["impact"] == "Users gain noticeably faster setup."

    def test_item_without_section_keeps_empty_impact_for_fallback(self):
        mod = _reload_standup()
        item = mod.normalize_item(
            {"id": "SA-1", "title": "T", "description": "# Description\n\nNo section.\n"}
        )
        assert item["impact"] == ""
        assert item["id"] == "SA-1"
        assert item["title"] == "T"
