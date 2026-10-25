"""Install/sync documentation tests for the author-identity gate.

Covers SA-0MVFOUW2B007WAZ0 AC4 (install/sync reference) and the AC1
actionable-remediation gap. The gate's *behaviour* is pinned by
``tests/test_author_gate_allowlist.py`` driving the real exported
``checkAuthorIdentity``; this module pins the documentation that turns a
refusal into a fix: the gate rules, the `syncAllowedAuthors` allow-list, the
one-off `--allow-foreign-author` escape hatch, and the pre-flight identity
guard wired by `scripts/install_pi.sh`.
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SYNC_DOC = REPO_ROOT / "docs" / "dev" / "worklog-sync.md"
INSTALL_SCRIPT = REPO_ROOT / "scripts" / "install_pi.sh"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _flat(text: str) -> str:
    """Collapse whitespace so assertions survive Markdown line wrapping."""
    return " ".join(text.split())


class TestSyncDocAuthorIdentityGate:
    """AC4: the sync reference documents the gate and every escape route."""

    def test_documents_the_gate_and_its_configuration(self):
        text = _text(SYNC_DOC)
        assert "## Author-identity gate" in text
        assert "syncAllowedAuthors" in text
        assert ".worklog/config.yaml" in text

    def test_documents_accepted_identities_and_the_project_allowlist(self):
        text = _text(SYNC_DOC)
        assert "user.email" in text
        assert "pi@local" in text
        assert "SA-0MVGSB63F00803HK" in text

    def test_documents_empty_email_and_exact_email_constraints(self):
        text = _text(SYNC_DOC).lower()
        assert "empty author email is always refused" in text
        assert "exact-email" in text
        assert "wildcard" in text
        assert "t@t.com" in text

    def test_documents_the_escape_hatch_and_its_limits(self):
        flat = _flat(_text(SYNC_DOC))
        assert "wl sync --allow-foreign-author" in flat
        # The escape hatch is one-off and never bypasses the empty-email gate.
        assert "never bypasses the empty-email gate" in flat
        assert "does not modify the allow-list" in flat

    def test_documents_remediation_and_the_pre_flight_guard(self):
        flat = _flat(_text(SYNC_DOC))
        assert "git config --local user.email" in flat
        assert "scripts/check_git_identity.py" in flat
        # Already-merged data is not re-gated (the incident convergence case).
        assert "last-synced-ref" in flat

    def test_references_regression_coverage_and_traceability(self):
        text = _text(SYNC_DOC)
        assert "tests/test_author_gate_allowlist.py" in text
        for work_item in (
            "SA-0MVB57M02008QPJS",
            "SA-0MVGSB63F00803HK",
            "SA-0MUJ2VMF7000UHCX",
        ):
            assert work_item in text


class TestInstallReferenceDocumentsIdentityRequirement:
    """AC4: the install reference states the `user.email` requirement."""

    def test_install_script_documents_the_gate_requirement_and_remedies(self):
        text = _text(INSTALL_SCRIPT)
        assert "check_git_identity.py" in text
        assert "user.email" in text
        assert "syncAllowedAuthors" in text
        assert "--allow-foreign-author" in text
        assert "docs/dev/worklog-sync.md" in text
