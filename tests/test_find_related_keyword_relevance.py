"""v4 keyword-relevance tests for the find-related skill (SA-0MUG46NJE003JOCD).

Covers the signal-weighting changes applied on top of the v3 frequency
ordering:

  - **Title boost**: a title token outweighs a single body occurrence
    (``TITLE_WEIGHT``), so the item's compressed subject leads the list.
  - **Project-generic suppression**: scaffold terms ("work", "item",
    "report", "review", "skill", "related", "find") are dropped so a capped
    ``MAX_SEARCH_KEYWORDS`` sweep is not spent on corpus-generic words.
  - **Determinism preserved**: identical input yields an identical keyword
    list; ties remain alphabetical.
  - **Runtime bound preserved**: the search fan-out stays capped at
    ``MAX_SEARCH_KEYWORDS``.

Every test asserts observable behaviour through the module's public API
(``extract_keywords`` / ``search_and_dedup``); none greps source or
re-implements production logic.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
_SKILLS_ROOT = REPO_ROOT / "skill"
for _p in (str(REPO_ROOT), str(_SKILLS_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_SCRIPT_PATH = REPO_ROOT / "skill" / "find-related" / "scripts" / "find_related.py"
_spec = importlib.util.spec_from_file_location("find_related", _SCRIPT_PATH)
mod = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(mod)


# ---------------------------------------------------------------------------
# Title weighting (title tokens weight higher than body tokens)
# ---------------------------------------------------------------------------


class TestTitleWeighting:
    def test_title_token_outranks_equally_frequent_body_token(self):
        """A title token weighs more than a single body occurrence."""
        keywords = mod.extract_keywords("caching", "caching strategy")
        assert keywords[0] == "caching", keywords
        assert "strategy" in keywords

    def test_title_token_outranks_more_frequent_body_token(self):
        """A once-in-title token outranks a body token repeated twice.

        ``TITLE_WEIGHT`` (3) means the title occurrence scores 3 body
        occurrences, so it must lead a body token seen twice.
        """
        keywords = mod.extract_keywords("caching", "strategy strategy caching")
        assert keywords[0] == "caching", keywords
        assert keywords.index("caching") < keywords.index("strategy")

    def test_body_frequency_still_matters(self):
        """Repeated body terms still rank above one-off body terms (v3)."""
        keywords = mod.extract_keywords("", "delta delta alpha beta")
        assert keywords[0] == "delta", keywords
        assert set(keywords[1:]) == {"alpha", "beta"}

    def test_equal_title_and_body_weights_are_alphabetical(self):
        """Equal-weight keywords remain alphabetically ordered (determinism)."""
        keywords = mod.extract_keywords("beta alpha", "")
        assert keywords == ["alpha", "beta"], keywords

    def test_title_weight_constant_is_sane(self):
        """TITLE_WEIGHT is a positive, modest multiplier."""
        assert hasattr(mod, "TITLE_WEIGHT")
        assert isinstance(mod.TITLE_WEIGHT, int)
        assert 1 < mod.TITLE_WEIGHT < 100, (
            "TITLE_WEIGHT must boost the title without letting a single "
            "generic title word swamp repeated body signal"
        )


# ---------------------------------------------------------------------------
# Project-generic suppression (expanded stop-word list)
# ---------------------------------------------------------------------------


class TestProjectGenericStopWords:
    def test_project_scaffold_terms_are_dropped(self):
        """Named generic terms never become keywords, even as the title."""
        added = ["work", "item", "report", "review", "skill", "related",
                 "find"]
        for word in added:
            keywords = mod.extract_keywords(word, f"{word} extraction")
            assert word not in keywords, (
                f"Project-generic term '{word}' must not be a keyword"
            )
            assert "extraction" in keywords

    def test_project_stop_words_constant_exposed(self):
        """The project-generic list is a discoverable constant."""
        assert hasattr(mod, "PROJECT_STOP_WORDS")
        assert isinstance(mod.PROJECT_STOP_WORDS, set)
        for word in ("work", "item", "report", "review", "skill", "related",
                     "find"):
            assert word in mod.PROJECT_STOP_WORDS

    def test_generic_repetition_does_not_displace_domain_term(self):
        """A repeated generic body word must not crowd out a domain term.

        Before v4, ten occurrences of "work"/"item"/"report" ranked above a
        one-off domain term and consumed capped search slots.
        """
        generic = "work item report review skill related find " * 10
        keywords = mod.extract_keywords("", f"{generic}postgresql deadlock")
        assert "postgresql" in keywords
        assert "deadlock" in keywords
        for generic_word in ("work", "item", "report", "review", "skill",
                             "related", "find"):
            assert generic_word not in keywords
        # The domain terms sit at the front of the list (highest weight).
        assert set(keywords[:2]) == {"postgresql", "deadlock"}

    def test_generic_title_word_does_not_outrank_domain_body_term(self):
        """AC3 risk mitigation: a generic title word cannot dominate.

        "review" in the title is suppressed, so the distinctive body term
        leads even though the generic word appears in the title.
        """
        keywords = mod.extract_keywords(
            "Review report", "postgresql migration deadlock"
        )
        assert keywords[0] in {"deadlock", "migration", "postgresql"}
        assert "review" not in keywords
        assert "report" not in keywords


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_identical_input_identical_keywords(self):
        """Identical input returns an identical keyword list."""
        title = "Optimize keyword extraction"
        desc = "extraction extraction keyword pipeline keyword ranking"
        assert mod.extract_keywords(title, desc) == mod.extract_keywords(
            title, desc
        )

    def test_tie_order_is_alphabetical_regardless_of_input_order(self):
        """Equal-weight ties are broken alphabetically, not by input order."""
        k1 = mod.extract_keywords("", "gamma beta alpha gamma beta alpha")
        k2 = mod.extract_keywords("", "alpha beta gamma alpha beta gamma")
        assert k1 == k2 == ["alpha", "beta", "gamma"]


# ---------------------------------------------------------------------------
# Runtime bound (search fan-out cap) unchanged by v4
# ---------------------------------------------------------------------------


class TestSearchFanOutCap:
    def test_top_k_cap_still_applies(self):
        """v4 must not raise the per-keyword subprocess cap."""
        calls: list[str] = []

        def fake_search(keyword, use_semantic=False, worklog_flags=None):
            calls.append(keyword)
            return []

        keywords = [f"kw{i:03d}" for i in range(300)]
        with mock.patch.object(mod, "run_wl_search", side_effect=fake_search):
            mod.search_and_dedup(keywords, use_semantic=True)
        assert len(calls) == mod.MAX_SEARCH_KEYWORDS
        assert calls == keywords[: mod.MAX_SEARCH_KEYWORDS]


# ---------------------------------------------------------------------------
# Result ranking (v4, SA-0MUG46NJE003JOCD)
#
# search_and_dedup ranking was changed to surface genuinely-related items:
# primary = descending number of distinct searched keywords matched;
# secondary = ascending best per-keyword score (more negative = better, wl
# returns best-first with an ascending score field); tertiary = id. This
# fixed an inverted sort (old code ranked by *descending* score, surfacing
# the worst matches first) and makes multi-keyword matches win.
# ---------------------------------------------------------------------------


class TestResultRanking:
    def _search_once(self, monkeypatch, mapping):
        """Patch run_wl_search so each keyword returns mapping[keyword]."""
        def fake_search(keyword, use_semantic=False, worklog_flags=None):
            return mapping.get(keyword, [])

        monkeypatch.setattr(mod, "run_wl_search", fake_search)

    def test_multi_keyword_match_outranks_single_match(self, monkeypatch):
        """Items matched by several searched keywords rank above single hits.

        The v4 primary ranking signal: an item surfaced by multiple
        distinctive keywords is far more likely to be genuinely related than
        an item hit by one corpus-generic keyword (AC1 reproduction on
        SA-0MNCDAQ8W008KOG9).
        """
        self._search_once(monkeypatch, {
            "keyword": [{"id": "REL-A", "score": -0.4}, {"id": "REL-B", "score": -0.9}],
            "extraction": [{"id": "REL-B", "score": -0.8}],
            "idempotency": [{"id": "REL-B", "score": -0.7}],
        })
        results = mod.search_and_dedup(["keyword", "extraction", "idempotency"])
        # REL-B matched by all three keywords -> first; REL-A matched by one.
        assert [r["id"] for r in results] == ["REL-B", "REL-A"]

    def test_score_tiebreak_is_ascending(self, monkeypatch):
        """Equal match counts are broken by ascending (most negative) score.

        wl search returns best-first with a more-negative = better score
        (negated BM25/hybrid rank), so the old descending sort was inverted.
        """
        self._search_once(monkeypatch, {
            "alpha": [{"id": "A", "score": -0.1}, {"id": "B", "score": -2.0}],
        })
        results = mod.search_and_dedup(["alpha"])
        assert [r["id"] for r in results] == ["B", "A"]

    def test_exclude_id_never_occupies_a_slot(self, monkeypatch):
        """The analyzed item itself is skipped during aggregation, so it
        cannot consume one of the capped result slots."""
        self._search_once(monkeypatch, {
            "keyword": [
                {"id": "SELF", "score": -0.05},
                {"id": "REL-1", "score": -0.1},
            ],
        })
        results = mod.search_and_dedup(["keyword"], exclude_id="SELF")
        assert [r["id"] for r in results] == ["REL-1"]

    def test_unscored_items_sort_last(self, monkeypatch):
        """Items without a score field rank below scored ones."""
        self._search_once(monkeypatch, {
            "alpha": [{"id": "NO-SCORE"}, {"id": "SCORED", "score": -3.0}],
        })
        results = mod.search_and_dedup(["alpha"])
        assert [r["id"] for r in results] == ["SCORED", "NO-SCORE"]

    def test_deterministic_ties_by_id(self, monkeypatch):
        """Identical counts/scores across runs sort identically (by id)."""
        items = [{"id": "REL-B", "score": -1.0}, {"id": "REL-A", "score": -1.0}]
        self._search_once(monkeypatch, {"alpha": items})
        first = mod.search_and_dedup(["alpha"])
        second = mod.search_and_dedup(["alpha"])
        assert [r["id"] for r in first] == [r["id"] for r in second]
