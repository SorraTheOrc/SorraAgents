"""Tests for the tool-output budget guidance (SA-0MWJYHKDX006HS36).

Asserts the observable deliverables of the first rollout slice: the canonical
shared doc exists, documents the five core points, and carries the required
sections and traceability. The reference-inventory classes land with the
rollout feature (SA-0MWJYHX6D008ZYE6).
"""
from __future__ import annotations

from pathlib import Path

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
