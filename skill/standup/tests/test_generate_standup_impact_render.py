"""Tests: impact-first, TTS-friendly report rendering (SA-0MW6EUQVW008N4BA).

The standup epic (SA-0MW6ER1NX0094QCG) requires every completed item to be
described by one user-experience sentence: the item's ``## Impact`` sentence
when present, else the existing ``Title — user story`` phrasing. The rendered
report must stay first-person and TTS-friendly — spoken-form dates, no work
item IDs, no backticks, no underscores — and within the ~2-minute read budget
(one sentence per item).

Behaviour pinned here:
  - ``_format_item_tts()`` prefers the structured ``impact`` field and falls
    back to ``Title — user story`` when it is empty;
  - it also reads the ``## Impact`` section from a raw description when the
    item has not been through ``normalize_item()``;
  - multi-sentence impacts are trimmed to their first sentence (read budget);
  - spoken lines carry no item IDs, backticks or underscores, from the title,
    the impact sentence, the user story or a blocker line;
  - ``format_report()`` emits the impact-first lines in every section, keeps
    spoken-form dates and first-person wording, and sanitises legacy
    (non-TTS) item lines it is given for back-compat.
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

#: Rendered reports must never leak a work item id in any of these shapes.
ID_PATTERN = re.compile(r"\b[A-Z]{2,}-\d[A-Z0-9]{6,}\b")
SPOKEN_DATE_PATTERN = re.compile(
    r"\d{1,2}(?:st|nd|rd|th) (?:January|February|March|April|May|June|July|August|"
    r"September|October|November|December) \d{4}"
)


def _reload_standup():
    """Import a fresh generate_standup module from the repo skill tree."""
    spec_path = REPO_ROOT / "skill" / "standup" / "scripts" / "generate_standup.py"
    spec = importlib.util.spec_from_file_location("generate_standup", str(spec_path))
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _raw_item(iid, title, description, status="completed", stage="in_review",
              priority="high"):
    return {
        "id": iid,
        "title": title,
        "status": status,
        "stage": stage,
        "priority": priority,
        "description": description,
    }


def _impact_description(impact, user_story="As the operator, I want the old phrasing."):
    return (
        "# Description\n\n"
        "Context paragraph.\n\n"
        f"## Impact\n\n{impact}\n\n"
        "## Acceptance Criteria\n\n"
        "1. Something measurable.\n\n"
        f"## User story\n\n{user_story}\n"
    )


# ── _format_item_tts(): impact-first with fallback ─────────────────────

class TestImpactFirstRendering:
    def test_prefers_structured_impact_over_user_story(self):
        mod = _reload_standup()
        item = mod.normalize_item(
            _raw_item(
                "SA-0MW6EUQVW008N4BA",
                "Impact-aware report rendering",
                _impact_description(
                    "Users now hear what each change gave them, not what was built."
                ),
            )
        )
        line = mod._format_item_tts(item)

        assert "Users now hear what each change gave them, not what was built." in line
        assert "As the operator" not in line

    def test_reads_impact_from_description_for_raw_items(self):
        """Un-normalised items still get impact-first rendering."""
        mod = _reload_standup()
        item = _raw_item(
            "SA-0MW6EUQVW008N4BA",
            "Impact-aware report rendering",
            _impact_description("Users see the benefit at a glance."),
        )
        assert "impact" not in item

        line = mod._format_item_tts(item)

        assert "Users see the benefit at a glance." in line

    def test_falls_back_to_title_and_user_story_without_impact(self):
        mod = _reload_standup()
        item = mod.normalize_item(
            _raw_item(
                "SA-0MW5HVQ7Z006SI6Q",
                "One-click deck setup",
                "# Description\n\n## User story\n\n"
                "As the operator, I want a one-click deck setup.\n",
            )
        )
        assert item["impact"] == ""

        line = mod._format_item_tts(item)

        assert line == "  - One-click deck setup — As the operator, I want a one-click deck setup."

    def test_empty_impact_key_falls_back(self):
        mod = _reload_standup()
        item = {
            "id": "SA-0MW5HVQ7Z006SI6Q",
            "title": "Deck setup",
            "impact": "",
            "description": "- As the operator, I want deck setup to be quick.",
        }
        line = mod._format_item_tts(item)
        assert line == "  - Deck setup — As the operator, I want deck setup to be quick."

    def test_multi_sentence_impact_is_trimmed_to_first_sentence(self):
        """One sentence per item keeps the report inside the read budget."""
        mod = _reload_standup()
        item = mod.normalize_item(
            _raw_item(
                "SA-0MW6EUQVW008N4BA",
                "Read budget",
                _impact_description(
                    "Users hear one sentence per item. A second sentence would "
                    "push the report past two minutes."
                ),
            )
        )
        line = mod._format_item_tts(item)

        assert "Users hear one sentence per item." in line
        assert "A second sentence" not in line


# ── Spoken lines are free of IDs, backticks and underscores ───────────

class TestSpokenLinesAreClean:
    def test_title_and_impact_are_sanitised(self):
        mod = _reload_standup()
        item = mod.normalize_item(
            _raw_item(
                "SA-0MW6EUQVW008N4BA",
                "Fix `deck_setup` bug",
                _impact_description(
                    "Users can run `generate_standup` without a crash (SA-0MW6EUQVW008N4BA)."
                ),
            )
        )
        line = mod._format_item_tts(item)

        assert "SA-0MW6EUQVW008N4BA" not in line
        assert "`" not in line
        assert "_" not in line
        assert "deck setup bug" in line
        assert "generate standup" in line

    def test_blocker_line_is_spoken_form_without_ids(self):
        mod = _reload_standup()
        raw = (
            "- **Card deck setup** (WL-0MW5HVQ7Z006SI6Q) blocked by "
            "**Worklog sync** (WL-0MUAD8U24001ZK5K)"
        )
        line = mod._format_blocker_tts(raw)

        assert line == (
            "  - Blocked on Card deck setup, because blocked by Worklog sync"
        )
        assert "WL-" not in line
        assert "*" not in line
        assert "_" not in line


# ── format_report(): assembled report stays impact-first and TTS-friendly ──

def _report_data(mod, items, regressions=()):
    """Drive generate_report with injected selection/regression data."""
    mod.fetch_herdr_selection_list = lambda browse_count=None: (list(items), [])
    mod.fetch_all_completions_in_window = lambda _ws, _we: []
    mod.fetch_blockers = lambda _its: []
    mod.detect_regressions = lambda _ws, _we: list(regressions)
    mod.resolve_project_name = lambda worklog_dir=None: "TestProject"
    return mod.generate_report(
        verbose=False, browse_count=5, window_start=None, window_end=None
    )


class TestFormattedReportRendering:
    def test_every_section_is_impact_first_and_clean(self):
        mod = _reload_standup()
        items = [
            _raw_item(
                "SA-1",
                "Faster `deck_setup`",
                _impact_description("Users set up a deck in one click."),
            ),
            _raw_item(
                "SA-2",
                "Critical thing",
                _impact_description("Users unblock the critical thing."),
                status="open",
                stage="plan_complete",
                priority="critical",
            ),
            _raw_item(
                "SA-3",
                "Additional thing",
                _impact_description("Users focus on the additional thing."),
                status="open",
                stage="plan_complete",
                priority="medium",
            ),
        ]
        regression = _raw_item(
            "SA-4",
            "Regressed thing",
            _impact_description("Users recover the regressed thing."),
            status="open",
            stage="plan_complete",
        )
        data = _report_data(mod, items, [regression])
        report = mod.format_report(data)

        for sentence in (
            "Users set up a deck in one click.",
            "Users unblock the critical thing.",
            "Users focus on the additional thing.",
            "Users recover the regressed thing.",
        ):
            assert sentence in report, sentence

        assert ID_PATTERN.search(report) is None
        assert "`" not in report
        assert "_" not in report

    def test_report_stays_first_person_with_spoken_dates(self):
        mod = _reload_standup()
        item = _raw_item(
            "SA-1",
            "Faster deck setup",
            _impact_description("Users set up a deck in one click."),
        )
        report = mod.format_report(_report_data(mod, [item]))

        assert "Standup Report (" in report
        assert SPOKEN_DATE_PATTERN.search(report.split("\n")[0]) is not None
        assert not re.search(r"\d{4}-\d{2}-\d{2}", report.split("\n")[0])
        for phrase in (
            "Yesterday I completed work on",
            "Open critical items",
            "Additional focus items for today",
            "Regressions",
            "Blockers",
        ):
            assert phrase in report, phrase

    def test_legacy_item_lines_are_sanitised(self):
        """Back-compat fallback lines must not leak ids or markdown either."""
        mod = _reload_standup()
        data = {
            "project_name": "TestProject",
            "yesterday": ["- **Deck setup** (SA-0MW5HVQ7Z006SI6Q) — `deck_setup` work"],
            "today_critical": [],
            "today_additional": [],
            "regressions": [],
            "blockers": [],
        }
        report = mod.format_report(data)

        yesterday_section = report.split("### Open critical items")[0]
        assert "SA-0MW5HVQ7Z006SI6Q" not in report
        assert "`" not in report
        assert "_" not in report
        assert "Deck setup" in yesterday_section
