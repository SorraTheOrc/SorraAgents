"""SKILL.md renders a clustered, agent-authored standup report.

The standup epic (SA-0MW6ER1NX0094QCG) moves the narrative out of the
deterministic script and into the invoking agent: the script emits structured
JSON, the agent clusters each section's items into free-form categories chosen
at report time and writes one spoken paragraph per category
(SA-0MW6EUVGM009RMD2).

The document *is* the deliverable here, so the behaviour pinned is the
rendering contract it hands the agent:

  - the agent renders from ``--json`` structured data — the script stays the
    deterministic data layer (no LLM inside the script);
  - each section is clustered **independently**, with free-form labels and no
    fixed taxonomy, one paragraph per category and one sentence per item;
  - the sentence is the item's ``## Impact`` sentence when present, else the
    established ``Title — user story`` fallback;
  - the report stays first-person, TTS-friendly, ID-free and within the
    ~2-minute budget;
  - the worked example shows the grouped-paragraph format (not the old static
    bullet template) and contains no work item ids.
"""
from __future__ import annotations

import re
from pathlib import Path

SKILL_MD = Path(__file__).resolve().parents[1] / "SKILL.md"

#: Work item ids in the shapes a spoken report must never carry.
BARE_ID_RE = re.compile(r"\b[A-Z]{2,}-\d[A-Z0-9]{6,}\b")
PREFIXED_ID_RE = re.compile(r"\b(?:WL|SA)-[A-Za-z0-9]+")
PARENTHESISED_ID_RE = re.compile(r"\(\s*[A-Z]{2,}-\d")

SPOKEN_DATE_RE = re.compile(
    r"\d{1,2}(?:st|nd|rd|th) (?:January|February|March|April|May|June|July|"
    r"August|September|October|November|December) \d{4}"
)
ISO_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")

REPORT_HEADINGS = [
    "### Yesterday I completed work on...",
    "### Open critical items",
    "### Additional focus items for today",
    "### Regressions",
    "### Blockers",
]


def _skill_text() -> str:
    assert SKILL_MD.is_file(), f"missing standup SKILL.md at {SKILL_MD}"
    return SKILL_MD.read_text(encoding="utf-8")


def _level_two_section(name: str) -> str:
    """Return the body of the ``## <name>`` section.

    Headings inside fenced code blocks are ignored: the documented templates
    intentionally contain a ``## <project_name> Standup Report`` header.
    """
    lines = _skill_text().split("\n")
    start: int | None = None
    in_fence = False
    body: list[str] = []
    for index, line in enumerate(lines):
        if line.startswith("```"):
            in_fence = not in_fence
        if start is None:
            if not in_fence and line.strip() == f"## {name}":
                start = index
            continue
        if not in_fence and line.startswith("## "):
            break
        body.append(line)
    assert start is not None, f"SKILL.md has no '## {name}' section"
    return "\n".join(body)


def _example_markdown() -> str:
    """Return the worked example's fenced markdown report."""
    section = _level_two_section("Example")
    match = re.search(r"```markdown\n(.*?)```", section, re.DOTALL)
    assert match, "the Example section must contain a fenced markdown report"
    return match.group(1)


def _example_sections() -> dict[str, list[str]]:
    """Split the example report into ``{section: paragraphs}``."""
    sections: dict[str, list[str]] = {}
    current: str | None = None
    buffer: list[str] = []

    def flush() -> None:
        if current is None:
            return
        paragraphs = [
            " ".join(block.split()).strip()
            for block in "\n".join(buffer).split("\n\n")
            if block.strip()
        ]
        sections[current] = paragraphs

    for line in _example_markdown().split("\n"):
        if line.startswith("### "):
            flush()
            current = line.strip()
            buffer = []
        elif current is not None:
            buffer.append(line)
    flush()
    return sections


# ---------------------------------------------------------------------------
# Rendering contract handed to the invoking agent
# ---------------------------------------------------------------------------


def test_agent_renders_from_the_deterministic_json_data_layer():
    """AC5: the script fetches structured data; the agent clusters and renders."""
    rendering = " ".join(_level_two_section("Report Rendering").split()).lower()

    assert "--json" in rendering
    assert "rendered by you, the invoking agent" in rendering
    assert "script is the deterministic data layer" in rendering
    for field in (
        "yesterday_items",
        "today_critical_items",
        "today_additional_items",
        "regressions_items",
    ):
        assert field in rendering, f"structured field {field} is not documented"


def test_clustering_is_per_section_with_free_form_labels():
    """AC1: independent per-section clustering, no fixed taxonomy."""
    rendering = " ".join(_level_two_section("Report Rendering").split()).lower()

    assert "cluster each section independently" in rendering
    assert "each section is clustered **on its own**".lower() in rendering
    assert "never pick one taxonomy for the whole report" in rendering
    assert "free-form, chosen at report time" in rendering
    assert "no fixed taxonomy" in rendering


def test_one_paragraph_per_category_and_one_sentence_per_item():
    """AC2: grouped paragraphs, exactly one spoken sentence per item."""
    rendering = " ".join(_level_two_section("Report Rendering").split()).lower()

    assert "one paragraph per category" in rendering
    assert "one sentence per item" in rendering
    assert "no bullets" in rendering


def test_impact_sentence_first_with_title_user_story_fallback():
    """AC3: ``## Impact`` when present, else ``Title — user story``."""
    rendering = " ".join(_level_two_section("Report Rendering").split())

    assert "`## Impact`" in rendering
    assert "`Title — user story`" in rendering
    assert "fall" in rendering.lower()


def test_speech_rules_are_restated():
    """AC4: no ids, first person, spoken dates, ~2-minute budget."""
    rendering = " ".join(_level_two_section("Report Rendering").split()).lower()

    assert "no work item ids anywhere" in rendering
    assert "first person" in rendering
    assert "spoken dates" in rendering
    assert "under 2 minutes" in rendering


def test_section_order_and_empty_section_lines_are_preserved():
    """The five fixed sections survive the removal of the static template."""
    rendering = _level_two_section("Report Rendering")
    fence = re.search(r"```markdown\n(.*?)```", rendering, re.DOTALL)
    assert fence, "the section order must be shown as a fenced template"
    headings = [line.strip() for line in fence.group(1).split("\n")
                if line.startswith("### ")]
    assert headings == REPORT_HEADINGS
    assert "No immediate blockers." in rendering


def test_static_bullet_template_is_gone():
    """The old per-item bullet template must not linger in the guidance."""
    text = _skill_text()

    assert "Work item description — primary use case" not in text
    assert "## Report Structure" not in text


# ---------------------------------------------------------------------------
# Worked example: grouped paragraphs, no ids
# ---------------------------------------------------------------------------


def test_example_uses_the_grouped_paragraph_format():
    """AC2/AC6: each section is prose paragraphs, not a bullet list."""
    markdown = _example_markdown()

    for line in markdown.split("\n"):
        assert not line.lstrip().startswith(("- ", "* ", "+ ")), (
            f"example must not render bullets: {line!r}"
        )

    sections = _example_sections()
    assert list(sections) == REPORT_HEADINGS
    # Yesterday clusters independently into two categories...
    assert len(sections["### Yesterday I completed work on..."]) == 2
    # ...and today's focus into its own, different clusters.
    assert len(sections["### Additional focus items for today"]) == 3
    assert len(sections["### Open critical items"]) == 1
    for heading in REPORT_HEADINGS:
        assert sections[heading], f"{heading} has no rendered content"


def test_example_paragraphs_open_with_a_spoken_category_label():
    """Every category paragraph opens with a spoken label, not a heading."""
    sections = _example_sections()

    for heading in (
        "### Yesterday I completed work on...",
        "### Open critical items",
        "### Additional focus items for today",
    ):
        for paragraph in sections[heading]:
            assert paragraph.lower().startswith("on "), (
                f"{heading} paragraph lacks a spoken category label: {paragraph!r}"
            )
            assert "**" not in paragraph, (
                f"{heading} paragraph uses markdown emphasis: {paragraph!r}"
            )


def test_example_contains_no_work_item_ids_and_uses_spoken_dates():
    """AC4: ids never reach the report; the header date is spoken-form."""
    markdown = _example_markdown()
    header = markdown.split("\n", 1)[0]

    assert BARE_ID_RE.search(markdown) is None
    assert PREFIXED_ID_RE.search(markdown) is None
    assert PARENTHESISED_ID_RE.search(markdown) is None
    assert "`" not in markdown
    assert ISO_DATE_RE.search(markdown) is None
    assert SPOKEN_DATE_RE.search(header), header


def test_example_stays_first_person():
    """AC4: the narrative speaks as "I" throughout the grouped prose."""
    sections = _example_sections()

    for heading in (
        "### Yesterday I completed work on...",
        "### Open critical items",
        "### Additional focus items for today",
    ):
        prose = " ".join(sections[heading])
        assert re.search(r"\bI\b", prose), f"{heading} is not first-person"
    assert not re.search(r"\bwe\b", _example_markdown(), re.IGNORECASE)
