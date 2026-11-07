#!/usr/bin/env python3
# <!-- REFACTOR-SA-0MW6JO5UO006PCKZ
# smell: formatting
# severity: low
# description: Shebang is present but file is not executable
# -->
"""Golden-output test: grouped, ID-free, impact-first standup report.

The standup epic (SA-0MW6ER1NX0094QCG) renders every section as grouped
narrative: the five report sections are fixed, per-item sentences are
impact-first (the item's ``## Impact`` sentence when present, else the
``Title — user story`` fallback), and no work item ids ever reach the spoken
output.

Behaviour pinned here, against a fixed structured dataset and no network:
  - ``format_report()`` renders the exact golden document — grouped section
    headings in order, one bullet per item, impact-first sentences with the
    legacy fallback where ``impact`` is absent;
  - no work item id survives in any shape (bare ``WL-…``/``SA-…``, braced, or
    parenthesised inside a user story or blocker line);
  - the render is hermetic: the fixed data is injected, so no ``wl``/``git``
    subprocess (sync, fetch or commit) is ever spawned
    (``--no-sync``/``--no-commit`` equivalent).
"""
from __future__ import annotations

import importlib.util
import re
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


#: Spoken dates only — the header must never carry an ISO timestamp.
HEADER_RE = re.compile(
    r"^## Test Project Standup Report "
    r"\(\d{1,2}(?:st|nd|rd|th) [A-Z][a-z]+ \d{4}\)$"
)

#: Fixed section headings, in rendering order.
SECTION_HEADINGS = [
    "### Yesterday I completed work on...",
    "### Open critical items",
    "### Additional focus items for today",
    "### Regressions",
    "### Blockers",
]

#: Work item ids in every shape a spoken line must not carry.
BARE_ID_RE = re.compile(r"\b[A-Z]{2,}-\d[A-Z0-9]{6,}\b")
PREFIXED_ID_RE = re.compile(r"\b(?:WL|SA)-[A-Za-z0-9]+")
PARENTHESISED_ID_RE = re.compile(r"\(\s*[A-Z]{2,}-\d")

IMPACT_SENTENCE = (
    "Users now hear what each change gave them, not what was built."
)
REGRESSION_SENTENCE = (
    "Users keep the working deck setup that had briefly slipped."
)
CRITICAL_SENTENCE = (
    "Users get their queued work moving again without a restart."
)
ADDITIONAL_SENTENCE = "Users can scan the review queue at a glance."
BLOCKER_LINE = (
    "- **Worklog sync** (WL-0MUAD8U24001ZK5K) blocked by "
    "**Remote cache** (WL-0MW5HVRPJ002OOCH)"
)


def _item(iid, title, description, *, status="open", stage="plan_complete",
          priority="medium"):
    return {
        "id": iid,
        "title": title,
        "status": status,
        "stage": stage,
        "priority": priority,
        "description": description,
    }


#: One item per report section; two yesterday items prove impact-first AND the
#: legacy fallback inside the same golden document.
IMPACT_ITEM = _item(
    "SA-0MW6EUQVW008N4BA",
    "Impact-aware report rendering",
    "# Description\n\n"
    "Context paragraph.\n\n"
    "## Impact\n\n"
    f"{IMPACT_SENTENCE}\n\n"
    "## User story\n\n"
    "As the operator, I want a technical description of the change.\n",
    status="completed",
    stage="in_review",
)

LEGACY_ITEM = _item(
    "WL-0MW5HVQ7Z006SI6Q",
    "One-click deck setup",
    "# Description\n\n"
    "## User story\n\n"
    "As the operator, I want a one-click deck setup (WL-0MW5HVQ7Z006SI6Q).\n",
    status="completed",
    stage="in_review",
)

CRITICAL_ITEM = _item(
    "SA-0MW6F0C1X001AAAAA",
    "Recover the stalled dispatcher",
    f"## Impact\n\n{CRITICAL_SENTENCE}\n",
    priority="critical",
)

ADDITIONAL_ITEM = _item(
    "SA-0MW6F0C2X002BBBBB",
    "Sort the review queue",
    f"## Impact\n\n{ADDITIONAL_SENTENCE}\n",
)

REGRESSION_ITEM = _item(
    "SA-0MW6F0C3X003CCCCC",
    "Card deck setup",
    f"## Impact\n\n{REGRESSION_SENTENCE}\n",
)


def _forbidden(name):
    def _fail(*_args, **_kwargs):
        raise AssertionError(f"{name} must not run during the golden render")
    return _fail


def _fixed_report(mod):
    """Render the golden report from injected data only (no subprocesses)."""
    items = [IMPACT_ITEM, LEGACY_ITEM, CRITICAL_ITEM, ADDITIONAL_ITEM]
    mod.fetch_herdr_selection_list = lambda browse_count=None: (list(items), [])
    mod.fetch_all_completions_in_window = lambda _ws, _we: []
    mod.fetch_blockers = lambda _its: [BLOCKER_LINE]
    mod.detect_regressions = lambda _ws, _we: [REGRESSION_ITEM]
    mod.resolve_project_name = lambda worklog_dir=None: "Test Project"
    # Hermeticity: a wl sync/fetch or git commit here would be a test bug.
    mod.run_cmd = _forbidden("run_cmd")
    mod.fetch_json = _forbidden("fetch_json")

    data = mod.generate_report(
        verbose=False, browse_count=5, window_start=None, window_end=None
    )
    return mod.format_report(data, project_name="Test Project")


def _split_header(report):
    """Return (header, body); the body is date-independent, so it can be golden."""
    header, _, body = report.partition("\n")
    return header, body


def test_renders_the_exact_golden_document():
    mod = _reload_standup()
    header, body = _split_header(_fixed_report(mod))

    assert HEADER_RE.match(header) is not None, header
    assert body == GOLDEN_BODY


def test_grouped_section_headings_are_present_and_well_formed():
    mod = _reload_standup()
    report = _fixed_report(mod)
    lines = report.split("\n")

    headings = [line for line in lines if line.startswith("### ")]
    assert headings == SECTION_HEADINGS
    # Every heading opens its section: a blank line separates it from content.
    for heading in SECTION_HEADINGS:
        index = lines.index(heading)
        assert lines[index + 1] == "", f"{heading} is not followed by a blank line"


def test_no_work_item_ids_appear_anywhere():
    mod = _reload_standup()
    report = _fixed_report(mod)

    assert BARE_ID_RE.search(report) is None
    assert PREFIXED_ID_RE.search(report) is None
    assert PARENTHESISED_ID_RE.search(report) is None
    assert "WL-" not in report
    assert "SA-" not in report


def test_impact_first_sentences_with_legacy_fallback():
    mod = _reload_standup()
    report = _fixed_report(mod)

    expected_impact_lines = [
        ("Impact-aware report rendering", IMPACT_SENTENCE),
        ("Recover the stalled dispatcher", CRITICAL_SENTENCE),
        ("Sort the review queue", ADDITIONAL_SENTENCE),
        ("Card deck setup", REGRESSION_SENTENCE),
    ]
    for title, sentence in expected_impact_lines:
        assert f"  - {title} — {sentence}" in report, sentence

    # An item without ``## Impact`` keeps the established "Title — user story"
    # phrasing; the technical user story of an item WITH impact is never used.
    assert (
        "  - One-click deck setup — As the operator, I want a one-click deck setup."
        in report
    )
    assert "As the operator, I want a technical description of the change." not in report


#: Everything after the (date-dependent) header, byte for byte.
GOLDEN_BODY = (
    "\n"
    "### Yesterday I completed work on...\n"
    "\n"
    "  - Impact-aware report rendering — Users now hear what each change gave "
    "them, not what was built.\n"
    "  - One-click deck setup — As the operator, I want a one-click deck setup.\n"
    "\n"
    "### Open critical items\n"
    "\n"
    "  - Recover the stalled dispatcher — Users get their queued work moving "
    "again without a restart.\n"
    "\n"
    "### Additional focus items for today\n"
    "\n"
    "  - Sort the review queue — Users can scan the review queue at a glance.\n"
    "\n"
    "### Regressions\n"
    "\n"
    "  - Card deck setup — Users keep the working deck setup that had briefly "
    "slipped.\n"
    "\n"
    "### Blockers\n"
    "\n"
    "  - Blocked on Worklog sync, because blocked by Remote cache"
)
