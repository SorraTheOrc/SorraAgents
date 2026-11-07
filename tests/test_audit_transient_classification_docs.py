"""Doc-contract tests for the audit transient-classification contract.

WL-0MW2F2T1I001ZNH7 (C2) requires the framework docs to be the single
source of truth for the transient classification of verdict-assembly /
verdict-persistence failures:

- ``docs/dev/audit-skill-reference.md`` documents the line format (the
  ``Ready to close:`` first line is preserved, the machine-readable
  ``Audit classification: transient (<cause>)`` line, the cause vocabulary,
  the ``wl audit-set --summary`` mirror and the absence of a ``wl``
  schema/CLI change).
- ``docs/dev/ship-skill-reference.md`` documents the release audit gate's
  recognition of the classification token and the runner-infra descriptors,
  and that a transient-classified item is reported as transient/remediable
  (re-audit recommended) — never with the genuine-verdict reason.

These tests pin the *documentation contract* the audit reads (the audit sees
the repository, not the work-item comments). The behavioural contracts are
covered next to the code: ``tests/test_audit_runner_persist.py``,
``tests/test_audit_runner_core.py`` and
``tests/unit/test-check-audit-gate.mjs`` — this module does not re-implement
them.
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent

_AUDIT_DOC = _REPO_ROOT / "docs" / "dev" / "audit-skill-reference.md"
_SHIP_DOC = _REPO_ROOT / "docs" / "dev" / "ship-skill-reference.md"


def _normalised(path: Path) -> str:
    """Collapse whitespace so assertions survive line rewrapping."""
    assert path.exists(), f"reference doc not found at {path}"
    return " ".join(path.read_text(encoding="utf-8").split())


def test_audit_doc_pins_classification_line_format():
    """The audit reference documents the classification line and its place."""
    doc = _normalised(_AUDIT_DOC)
    assert "Audit classification: transient (<cause>)" in doc
    assert "Ready to close: <Yes|No>" in doc
    assert "verdict_persistence_failure" in doc
    for cause in (
        "timeout", "provider_error", "unparseable_output", "concurrency_cap",
    ):
        assert cause in doc
    # The summary mirror contract (a summary-only consumer can distinguish).
    assert "Audit result persisted; transient" in doc
    assert "summary" in doc
    assert "--summary" in doc


def test_audit_doc_pins_no_wl_schema_change():
    """D1: the classification is content-scoped, never a new wl field."""
    doc = _normalised(_AUDIT_DOC)
    assert "No new" in doc
    assert "wl" in doc
    assert "schema" in doc
    assert "content" in doc


def test_ship_doc_pins_gate_recognition_and_reason():
    """The ship gate reference documents the transient recognition tokens
    and the transient (never genuine-verdict) reason."""
    doc = _normalised(_SHIP_DOC)
    assert "transient (verdict_(assembly|persistence)_failure)" in doc
    assert "Pi model output could not be parsed" in doc
    assert "Audit concurrency limit reached" in doc
    assert "re-audit recommended" in doc
    assert "Audit verdict: not ready to close" in doc


def test_ship_doc_keeps_genuine_not_ready_blocking():
    """A genuine not-ready verdict still blocks — the transient markers are
    additive and never weaken the genuine-verdict path."""
    doc = _normalised(_SHIP_DOC)
    assert 'Genuine "not ready to close"' in doc
    assert "blocks immediately" in doc
