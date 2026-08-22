"""v3 tests for the find-related skill (SA-0MNCDAQ8W008KOG9).

Covers the speed/value fixes shipped by the v3 review:

  - Keyword feedback-loop break: the automated report section no longer
    feeds keyword extraction (AC4), keywords are frequency-ranked with
    numeric-only tokens dropped, and search keywords are capped at
    MAX_SEARCH_KEYWORDS (AC3b).
  - Worklog search fan-out bounded: only the top-k frequency-ranked
    keywords are queried, one subprocess each (AC3a) — 523 spawns on the
    polluted review item collapse to 8 with recall preserved.
  - Repo scan noise removal: `.worklog` (sidecar full report + stale
    worktree clones) is excluded from repo matching (AC4).
  - update_description section-boundary fix: `###` sub-headings inside the
    report are no longer treated as section ends, so re-runs stay
    idempotent and never stack duplicate sub-blocks (AC4).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
_SKILLS_ROOT = REPO_ROOT / "skill"
if str(_SKILLS_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILLS_ROOT))

import importlib.util

_SCRIPT_PATH = REPO_ROOT / "skill" / "find-related" / "scripts" / "find_related.py"
_spec = importlib.util.spec_from_file_location("find_related", _SCRIPT_PATH)
mod = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(mod)

REPORT_HEADING = "## Related work (automated report)"


def _report_section(contents: str) -> str:
    """A full automated-report section with *contents* after the heading."""
    return f"\n{REPORT_HEADING}\n\n{contents}\n"


# ---------------------------------------------------------------------------
# Keyword feedback-loop break (AC3b / AC4)
# ---------------------------------------------------------------------------


class TestExtractKeywordsSkipsReportSection:
    def test_report_words_never_become_keywords(self):
        """Tokens that only exist in the automated report section must not
        leak into keyword extraction (feedback loop).

        The polluted description on SA-0MNCDAQ8W008KOG9 grew to 523 keywords
        because report words ("matched", "repository", "worktrees", item ids)
        were re-extracted as search keywords on every run.
        """
        desc = (
            "## Summary\n"
            "Analyze the find-related skill code path for speedups.\n"
            + _report_section(
                "### Related work items\n"
                "- **SA-0ML7Q2XI11OO3S5I** – Add command scheduling (completed)\n"
                "### Repository file matches\n"
                "- `.worklog/tmp/find-related-full-SKILL-1.md` — matched: 10s, "
                "acceptance, worktrees, matched, repository (+488 more)"
            )
        )
        keywords = mod.extract_keywords("Review find related skill", desc)
        # Front-matter words are retained
        assert "analyze" in keywords
        assert "speedups" in keywords
        # Report-only tokens (exact tokenized forms) must NOT be keywords
        for leaked in ["matched", "repository", "worktrees", "acceptance",
                       "scheduling", "completed", "0ml7q2xi11oo3s5i"]:
            assert leaked not in keywords, (
                f"Report-only token '{leaked}' leaked into keywords"
            )

    def test_sections_after_report_still_extracted(self):
        """Text in a later non-report section is preserved for keywords."""
        desc = (
            "## Summary\nFront matter.\n"
            + _report_section("### Related work items\n- **X** – old\n")
            + "## Implementation notes\nIterate on the benchmark results.\n"
        )
        keywords = mod.extract_keywords("", desc)
        assert "front" in keywords
        assert "iterate" in keywords
        assert "benchmark" in keywords
        # The report heading's own word must not leak either
        assert "related" not in keywords

    def test_no_report_section_unchanged(self):
        """A description without the automated section extracts as before."""
        keywords = mod.extract_keywords("", "Plain description text here")
        assert "plain" in keywords
        assert "description" in keywords
        assert "text" in keywords


class TestKeywordRankingAndNoise:
    def test_frequency_ordering_puts_descriptive_core_first(self):
        """Top keywords are the repeated descriptive core, not alphabetical junk.

        The v3 batched semantic query feeds only the top MAX_SEARCH_KEYWORDS
        terms, so the ordering must surface the item's true subject first.
        """
        title = "Optimize search speed"
        desc = (
            "optimize the search speed of the skill. optimize search terms. "
            "optimize the keyword search pipeline and speed it up. "
            "speed matters for the search."
        )
        keywords = mod.extract_keywords(title, desc)
        # The most frequent token leads; 'optimize'/'speed' tie at 4 and sort
        # alphabetically (deterministic). 'pipeline' (freq 1) sorts last.
        assert keywords[:3] == ["search", "optimize", "speed"], keywords[:6]
        assert keywords.index("pipeline") > 2

    def test_numeric_only_tokens_dropped(self):
        """Pure numbers (years/counts) carry no search signal and are removed."""
        keywords = mod.extract_keywords(
            "", "Measure 2026 and 452 files for 969 records in total"
        )
        for numeric in ["2026", "452", "969"]:
            assert numeric not in keywords, f"Numeric token {numeric} kept"
        assert "measure" in keywords

    def test_stable_deterministic_ties(self):
        """Equal-frequency keywords sort alphabetically (deterministic)."""
        k1 = mod.extract_keywords("", "alpha beta gamma alpha beta gamma")
        k2 = mod.extract_keywords("", "gamma beta alpha gamma beta alpha")
        assert k1 == k2


class TestMaxSearchKeywordsConstant:
    def test_constant_exists_and_sane(self):
        """MAX_SEARCH_KEYWORDS bounds the worklog-search fan-out."""
        assert hasattr(mod, "MAX_SEARCH_KEYWORDS")
        assert mod.MAX_SEARCH_KEYWORDS >= 1
        assert mod.MAX_SEARCH_KEYWORDS < 100, (
            "Cap must be small enough to bound subprocess spawns"
        )


# ---------------------------------------------------------------------------
# Worklog search capping (AC3a)
# ---------------------------------------------------------------------------


class TestSearchAndDedupTopK:
    def test_searches_only_top_k_keywords(self):
        """Only MAX_SEARCH_KEYWORDS frequency-ranked keywords are queried.

        The v2 fan-out spawned one `wl search` subprocess per extracted
        keyword (523 on the polluted review item). v3 caps the spawn count.
        """
        kws = [f"kw{i:03d}" for i in range(300)]
        calls: list[str] = []

        def fake_search(keyword, use_semantic=False, worklog_flags=None):
            calls.append(keyword)
            return []

        with mock.patch.object(mod, "run_wl_search", side_effect=fake_search):
            mod.search_and_dedup(kws, use_semantic=True)
        assert len(calls) == mod.MAX_SEARCH_KEYWORDS
        assert calls == kws[: mod.MAX_SEARCH_KEYWORDS]

    def test_semantic_flag_passed_through(self):
        """Hybrid --semantic ranking is used for each queried keyword."""
        seen_semantic: list[bool] = []

        def fake_search(keyword, use_semantic=False, worklog_flags=None):
            seen_semantic.append(use_semantic)
            return [{"id": "REL-001", "score": -0.5}]

        with mock.patch.object(mod, "run_wl_search", side_effect=fake_search):
            mod.search_and_dedup(["alpha", "beta"], use_semantic=True)
        assert seen_semantic == [True, True]

    def test_no_search_without_keywords(self):
        with mock.patch.object(mod, "run_wl_search") as fake:
            results = mod.search_and_dedup([], use_semantic=True)
        assert results == []
        fake.assert_not_called()

    def test_lexical_fallback_caps_spawns(self):
        """Without semantic, per-keyword spawns are equally capped."""
        kws = [f"kw{i:03d}" for i in range(300)]
        calls: list[str] = []

        def fake_search(keyword, use_semantic=False, worklog_flags=None):
            calls.append(keyword)
            return []

        with mock.patch.object(mod, "run_wl_search", side_effect=fake_search):
            mod.search_and_dedup(kws, use_semantic=False)
        assert len(calls) == mod.MAX_SEARCH_KEYWORDS, (
            "Lexical fallback must not spawn one subprocess per keyword "
            "when the list is pathological"
        )

    def test_lexical_fallback_still_dedups_and_ranks(self):
        """The existing per-keyword dedup/rank semantics are unchanged."""
        def fake_search(keyword, use_semantic=False, worklog_flags=None):
            if keyword in ("alpha", "beta"):
                return [{"id": "REL-001", "title": "Dup", "score": -0.1}]
            return [{"id": "REL-002", "title": "Other", "score": -0.9}]

        with mock.patch.object(mod, "run_wl_search", side_effect=fake_search):
            results = mod.search_and_dedup(["alpha", "beta", "gamma"],
                                           use_semantic=False)
        ids = [r["id"] for r in results]
        assert ids == ["REL-001", "REL-002"], "Dedup + descending score order"


# ---------------------------------------------------------------------------
# Repo scan noise removal (AC4)
# ---------------------------------------------------------------------------


class TestSearchRepoWorklogExclusions:
    def _make_repo(self, tmp_path: Path) -> Path:
        """A repo with a real src file plus .worklog noise (sidecar + worktrees)."""
        real = tmp_path / "src" / "guide.md"
        real.parent.mkdir(parents=True)
        real.write_text("optimize keyword extraction feed", encoding="utf-8")
        # sidecar full report — self-referential, must never match
        sidecar = tmp_path / ".worklog" / "tmp" / "find-related-full-SKILL-1.md"
        sidecar.parent.mkdir(parents=True)
        sidecar.write_text("optimize keyword extraction feed", encoding="utf-8")
        # stale worktree clone
        wt = tmp_path / ".worklog" / "worktrees" / "wl-SA-0MS0001" / "skill" / "x.md"
        wt.parent.mkdir(parents=True)
        wt.write_text("optimize keyword extraction feed", encoding="utf-8")
        return tmp_path

    def test_worklog_files_excluded_from_matches(self, tmp_path):
        repo = self._make_repo(tmp_path)
        matches = mod.search_repo(str(repo), ["optimize", "keyword", "extraction"])
        files = [m["file"] for m in matches]
        assert "src/guide.md" in files
        assert not any(f.startswith(".worklog") for f in files), (
            f".worklog files matched: {files}"
        )

    def test_sidecar_full_report_never_matches(self, tmp_path):
        repo = self._make_repo(tmp_path)
        matches = mod.search_repo(str(repo), ["optimize", "keyword"])
        for m in matches:
            assert "find-related-full" not in m["file"], (
                "Self-referential sidecar report must never rank as a match"
            )

    def test_stale_worktree_clones_never_match(self, tmp_path):
        repo = self._make_repo(tmp_path)
        matches = mod.search_repo(str(repo), ["optimize"])
        for m in matches:
            assert "worktrees" not in m["file"], (
                "Stale worktree clones must never rank as matches"
            )


# ---------------------------------------------------------------------------
# update_description section-boundary fix (AC4)
# ---------------------------------------------------------------------------


class TestUpdateDescriptionSubHeadingBoundary:
    def test_replaces_section_with_subheadings_cleanly(self):
        """A prior-run section with ### sub-headings is fully replaced:
        stale sub-blocks must not survive, and must not stack duplicates.
        """
        old = (
            "## Summary\nIntro.\n"
            + _report_section(
                "### Related work items\n- **OLD-1** – stale\n"
                "### Repository file matches\n- `.worklog/old.md` — matched: x"
            )
            + "## Tail\nStill here.\n"
        )
        new_report = _report_section(
            "### Related work items\n- **NEW-1** – fresh\n"
        )
        out = mod.update_description(old, new_report)
        assert out.count(REPORT_HEADING) == 1
        assert out.count("### Related work items") == 1
        assert "OLD-1" not in out and "Repository file matches" not in out
        assert "NEW-1" in out
        assert "Intro." in out and "## Tail" in out

    def test_rerun_in_place_does_not_stack_subblocks(self):
        """Two successive updates yield exactly one report + one sub-block."""
        desc = "## Summary\nIntro.\n"
        report = _report_section(
            "### Related work items\n- **REL-001** – related\n"
        )
        first = mod.update_description(desc, report)
        second = mod.update_description(first, report)
        assert second.count(REPORT_HEADING) == 1
        assert second.count("### Related work items") == 1
        assert second.count("REL-001") == 1

    def test_heading_at_start_of_description(self):
        """Report heading at position 0 (no leading newline) is handled."""
        old = (REPORT_HEADING + "\n\n### Related work items\n- **OLD**\n\n"
               "## Tail\n")
        new_report = _report_section("### Related work items\n- **NEW**\n")
        out = mod.update_description(old, new_report)
        assert out.count(REPORT_HEADING) == 1
        assert "OLD" not in out and "NEW" in out
        assert "## Tail" in out