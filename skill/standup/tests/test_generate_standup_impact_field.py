#!/usr/bin/env python3
# <!-- REFACTOR-SA-0MW6F7IQR004B6AM
# smell: formatting
# severity: low
# description: Shebang is present but file is not executable
# -->
"""Tests: ``impact`` extraction and structured report data (SA-0MW6ERJ1A001YYJ0).

The standup epic (SA-0MW6ER1NX0094QCG) adds an explicit ``## Impact`` section
to work items: one sentence describing the user-experience impact of a change.
``generate_standup.py`` must parse that section into a dedicated ``impact``
field and expose it as structured item data in ``--json`` output, so the
invoking agent can render impact-first sentences.

Behaviour pinned here:
  - ``extract_impact()`` returns the section's sentence, trimmed of markdown
    emphasis, bullets and surrounding whitespace;
  - the section ends at the next heading of the same/higher level, so a later
    ``## Acceptance Criteria`` never bleeds in;
  - a missing, empty or malformed section (``None``/empty description
    included) yields ``""`` without raising;
  - ``normalize_item()`` always carries an ``impact`` key;
  - ``generate_report()`` exposes structured item data (with ``impact``) for
    yesterday, critical, additional and regression sections while keeping the
    existing formatted-line fields (JSON schema superset).
"""
from __future__ import annotations

import importlib.util
import json
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


# ── extract_impact() ───────────────────────────────────────────────────

class TestExtractImpactPresent:
    def test_parses_impact_section(self):
        mod = _reload_standup()
        desc = (
            "# Description\n\n"
            "Some context.\n\n"
            "## Impact\n\n"
            "Users can now set up a deck in one click.\n\n"
            "## Acceptance Criteria\n\n"
            "1. Something else.\n"
        )
        assert mod.extract_impact(desc) == "Users can now set up a deck in one click."

    def test_strips_markdown_emphasis_and_bullet(self):
        mod = _reload_standup()
        desc = "## Impact\n\n- **Users see results 3x faster.**\n"
        assert mod.extract_impact(desc) == "Users see results 3x faster."

    def test_strips_inline_bold_markers(self):
        mod = _reload_standup()
        desc = "## Impact\n\nUsers can now **share** reports with one click.\n"
        assert mod.extract_impact(desc) == "Users can now share reports with one click."

    def test_accepts_trailing_colon_and_lowercase_heading(self):
        mod = _reload_standup()
        assert mod.extract_impact("## impact:\n\nFaster setup.\n") == "Faster setup."

    def test_joins_wrapped_sentence(self):
        mod = _reload_standup()
        desc = "## Impact\n\nUsers can now set up a deck\nin a single click.\n"
        assert mod.extract_impact(desc) == "Users can now set up a deck in a single click."

    def test_section_ends_at_next_heading(self):
        """A following heading's body must not bleed into the impact sentence."""
        mod = _reload_standup()
        desc = (
            "## Impact\n\n"
            "Faster feedback loops.\n\n"
            "### Notes\n\n"
            "Implementation detail that must not be included.\n"
        )
        assert mod.extract_impact(desc) == "Faster feedback loops."


class TestExtractImpactAbsentOrMalformed:
    def test_missing_section_returns_empty(self):
        mod = _reload_standup()
        assert mod.extract_impact("# Description\n\nNo impact here.\n") == ""

    def test_empty_section_returns_empty(self):
        mod = _reload_standup()
        assert mod.extract_impact("## Impact\n\n## Acceptance Criteria\n\n1. x\n") == ""

    def test_heading_only_returns_empty(self):
        mod = _reload_standup()
        assert mod.extract_impact("## Impact\n") == ""

    def test_none_description_returns_empty(self):
        mod = _reload_standup()
        assert mod.extract_impact(None) == ""

    def test_empty_description_returns_empty(self):
        mod = _reload_standup()
        assert mod.extract_impact("") == ""

    def test_non_impact_heading_is_not_matched(self):
        """``## Impactful changes`` is a different heading, not the Impact section."""
        mod = _reload_standup()
        assert mod.extract_impact("## Impactful changes\n\nSomething.\n") == ""


# ── normalize_item() ───────────────────────────────────────────────────

class TestNormalizeItemImpact:
    def test_impact_populated_from_description(self):
        mod = _reload_standup()
        item = mod.normalize_item(
            {"id": "SA-1", "title": "T", "description": "## Impact\n\nFaster.\n"}
        )
        assert item["impact"] == "Faster."

    def test_impact_key_present_when_description_missing(self):
        mod = _reload_standup()
        assert mod.normalize_item({"id": "SA-1", "title": "T"})["impact"] == ""

    def test_impact_key_present_for_non_dict_description(self):
        mod = _reload_standup()
        item = mod.normalize_item({"id": "SA-1", "title": "T", "description": None})
        assert item["impact"] == ""


# ── generate_report() structured data ─────────────────────────────────

def _normalized(mod, iid, title, status, stage, priority, impact_text):
    return mod.normalize_item(
        {
            "id": iid,
            "title": title,
            "status": status,
            "stage": stage,
            "priority": priority,
            "description": f"## Impact\n\n{impact_text}\n",
        }
    )


def _report_data(mod, items, regressions):
    """Drive generate_report with injected selection/regression data."""
    mod.fetch_herdr_selection_list = lambda browse_count=None: (list(items), [])
    mod.fetch_all_completions_in_window = lambda _ws, _we: []
    mod.fetch_blockers = lambda _its: []
    mod.detect_regressions = lambda _ws, _we: list(regressions)
    mod.resolve_project_name = lambda worklog_dir=None: "TestProject"
    return mod.generate_report(
        verbose=False, browse_count=5, window_start=None, window_end=None
    )


class TestReportStructuredImpact:
    def test_sections_carry_structured_impact(self):
        mod = _reload_standup()
        items = [
            _normalized(mod, "SA-1", "Yesterday thing", "completed", "in_review",
                        "high", "Users finished the yesterday thing faster."),
            _normalized(mod, "SA-2", "Critical thing", "open", "plan_complete",
                        "critical", "Users unblock the critical thing."),
            _normalized(mod, "SA-3", "Additional thing", "open", "plan_complete",
                        "medium", "Users focus on the additional thing."),
        ]
        regression = _normalized(mod, "SA-4", "Regressed thing", "open",
                                 "plan_complete", "high",
                                 "Users recover the regressed thing.")
        data = _report_data(mod, items, [regression])

        for key, iid, impact in (
            ("yesterday_items", "SA-1", "Users finished the yesterday thing faster."),
            ("today_critical_items", "SA-2", "Users unblock the critical thing."),
            ("today_additional_items", "SA-3", "Users focus on the additional thing."),
            ("regressions_items", "SA-4", "Users recover the regressed thing."),
        ):
            section = data[key]
            assert [entry["id"] for entry in section] == [iid], key
            assert section[0]["impact"] == impact, key
            assert section[0]["title"], key

    def test_json_output_is_a_schema_superset(self):
        """Existing formatted fields remain; the new structured arrays serialise."""
        mod = _reload_standup()
        items = [
            _normalized(mod, "SA-1", "Yesterday thing", "completed", "in_review",
                        "high", "Users finished faster."),
        ]
        data = _report_data(mod, items, [])
        encoded = json.dumps(data)
        decoded = json.loads(encoded)

        assert decoded["yesterday_items"][0]["impact"] == "Users finished faster."
        # Legacy formatted lines are untouched.
        assert decoded["yesterday_tts"] == data["yesterday_tts"]
        assert decoded["yesterday_count"] == 1
