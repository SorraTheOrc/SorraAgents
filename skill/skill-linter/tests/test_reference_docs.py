"""Doc-contract tests for ``docs/dev/skill-linter-reference.md``.

Work item: SA-0MWGAZMJX008CRLD — "Write skill-linter reference documentation"
(child of SA-0MWFP1E9P0027EOE).

The reference doc is the canonical statement of the linter's rule set, report
schema, exit codes, ``--apply`` safety contract and cron helper. Those facts
also live in ``scripts/lint_skills.py``; these tests keep the two in step by
reading the **code** and asserting the doc documents it:

- the detector thresholds, exit codes, fix classes and cron schedule constants
  are named in the doc with their implemented values;
- every CLI flag the parser accepts is documented;
- every key of the emitted JSON report (payload, finding entry, span, summary)
  appears in the documented schema;
- the workflow-comment rule labels and the work-item ID pattern are documented;
- the cron contract states that the line is printed, not installed, and that
  the schedule must not be claimed active;
- ``README.md`` links the reference (the docs-index half of AC6).

The doc is prose, so the assertions target stable phrases and identifiers
rather than layout. Adding a rule, flag, key or constant without documenting it
turns these tests red.
"""

from __future__ import annotations

import contextlib
import io
import re
from pathlib import Path

import lint_skills

REPO_ROOT = Path(__file__).resolve().parents[3]
REFERENCE = REPO_ROOT / "docs" / "dev" / "skill-linter-reference.md"
README = REPO_ROOT / "README.md"

DUP_BLOCK = (
    "The linter scans SKILL.md files for repeated blocks of prose.",
    "It normalises whitespace before comparing candidate windows.",
    "Every finding reports the file path and the exact line span.",
)


def _doc_text() -> str:
    return REFERENCE.read_text(encoding="utf-8")


def _flat_text() -> str:
    """The reference lowercased with whitespace collapsed.

    Prose wraps, so a phrase assertion must not depend on where a line breaks.
    """
    return re.sub(r"\s+", " ", _doc_text()).lower()


def _sample_report() -> dict:
    """Build a report through the production path, for schema assertion."""
    text = "\n".join(
        [
            "# Alpha skill",
            "",
            "## Usage",
            "",
            *DUP_BLOCK,
            "",
            "Tracking note for SA-0MWGAYPMX002Z0VC.",
            "",
            *DUP_BLOCK,
            "",
        ]
    )
    documents = {"skill/alpha/SKILL.md": text}
    findings = lint_skills.scan_documents(documents)
    return lint_skills.build_report(findings, documents, root=REPO_ROOT)


def _cli_option_strings() -> set[str]:
    """Every option the CLI accepts, read from its public ``--help`` output."""
    buffer = io.StringIO()
    with contextlib.suppress(SystemExit):
        with contextlib.redirect_stdout(buffer):
            lint_skills.main(["--help"])
    return set(re.findall(r"(?<![\w-])(--[a-z][a-z-]+)", buffer.getvalue()))


class TestReferenceExists:
    def test_reference_doc_exists_and_is_substantial(self):
        assert REFERENCE.is_file(), (
            "docs/dev/skill-linter-reference.md must exist: it is the canonical "
            "contract the linter tests and SKILL.md cite"
        )
        assert REFERENCE.stat().st_size > 3000, (
            "the reference must document the rule set, report schema, exit "
            "codes, --apply contract and cron helper in full"
        )

    def test_reference_carries_a_title_and_its_work_item(self):
        text = _doc_text()
        assert text.startswith("# Skill-linter reference")
        # Traceability: the doc names the item that owns it and its parent.
        assert "SA-0MWGAZMJX008CRLD" in text
        assert "SA-0MWFP1E9P0027EOE" in text


class TestDocumentedConstantsMatchCode:
    def test_detector_thresholds_are_named_with_their_values(self):
        text = _doc_text()
        for name, value in (
            ("MIN_DUPLICATE_LINES", lint_skills.MIN_DUPLICATE_LINES),
            ("MIN_DUPLICATE_CHARS", lint_skills.MIN_DUPLICATE_CHARS),
            ("NEAR_DUPLICATE_RATIO", lint_skills.NEAR_DUPLICATE_RATIO),
        ):
            assert name in text, f"reference must name {name}"
            assert str(value) in text, f"reference must document {name}={value}"

    def test_exit_codes_are_named_with_their_values(self):
        text = _doc_text()
        for name, value in (
            ("EXIT_CLEAN", lint_skills.EXIT_CLEAN),
            ("EXIT_FINDINGS", lint_skills.EXIT_FINDINGS),
            ("EXIT_ERROR", lint_skills.EXIT_ERROR),
        ):
            assert name in text, f"reference must name {name}"
            assert str(value) in text, f"reference must document {name}={value}"

    def test_fix_classes_and_schema_version_are_named(self):
        text = _doc_text()
        assert "FIX_SAFE" in text
        assert "FIX_RECOMMENDATION" in text
        assert f'"{lint_skills.FIX_SAFE}"' in text
        assert f'"{lint_skills.FIX_RECOMMENDATION}"' in text
        assert "REPORT_SCHEMA_VERSION" in text
        assert str(lint_skills.REPORT_SCHEMA_VERSION) in text

    def test_scan_scope_and_cron_constants_are_named(self):
        text = _doc_text()
        assert "DEFAULT_SKILL_GLOB" in text
        assert lint_skills.DEFAULT_SKILL_GLOB in text
        assert "DEFAULT_ROOT_DOCUMENTS" in text
        for name in lint_skills.DEFAULT_ROOT_DOCUMENTS:
            assert name in text, f"reference must document root document {name}"
        assert "SCAN_DOCUMENT_NAMES" in text
        for name in sorted(lint_skills.SCAN_DOCUMENT_NAMES):
            assert name in text, f"reference must document scanned name {name}"
        assert "CRON_SCHEDULE_WEEKLY" in text
        assert lint_skills.CRON_SCHEDULE_WEEKLY in text

    def test_allow_list_default_is_documented_as_empty(self):
        text = _doc_text()
        assert "DEFAULT_ALLOW_IDS" in text
        assert lint_skills.DEFAULT_ALLOW_IDS == ()
        assert "no work-item id is essential" in _flat_text()
        assert "--allow-id" in text

    def test_work_item_id_pattern_is_documented_verbatim(self):
        assert lint_skills.WORK_ITEM_ID_RE.pattern in _doc_text()

    def test_every_workflow_comment_rule_label_is_documented(self):
        text = _doc_text()
        for label, _pattern in lint_skills.WORKFLOW_COMMENT_RULES:
            assert label in text, f"reference must document the {label!r} rule"


class TestDocumentedCliMatchesParser:
    def test_every_parser_option_is_documented(self):
        text = _doc_text()
        undocumented = [option for option in _cli_option_strings() if option not in text]
        assert not undocumented, (
            "CLI flags missing from the reference: " + ", ".join(undocumented)
        )


class TestDocumentedSchemaMatchesReport:
    def test_payload_keys_are_documented(self):
        text = _doc_text()
        payload = _sample_report()
        for key in payload:
            assert key in text, f"report payload key {key!r} is undocumented"

    def test_finding_entry_and_span_keys_are_documented(self):
        text = _doc_text()
        entry = _sample_report()["findings"][0]
        for key in entry:
            assert key in text, f"finding entry key {key!r} is undocumented"
        for key in entry["spans"][0]:
            assert key in text, f"span key {key!r} is undocumented"

    def test_summary_keys_are_documented(self):
        text = _doc_text()
        for key in _sample_report()["summary"]:
            assert key in text, f"summary key {key!r} is undocumented"

    def test_rendered_json_example_carries_the_schema_version(self):
        text = _doc_text()
        assert f'"schema_version": {lint_skills.REPORT_SCHEMA_VERSION}' in text

    def test_all_finding_kinds_are_documented(self):
        text = _doc_text()
        for kind in (
            lint_skills.KIND_DUPLICATE,
            lint_skills.KIND_NEAR_DUPLICATE,
            lint_skills.KIND_NONESSENTIAL_ID,
            lint_skills.KIND_WORKFLOW_COMMENT,
        ):
            assert kind in text, f"finding kind {kind!r} is undocumented"


class TestDocumentedSafetyContracts:
    def test_apply_contract_states_safe_class_only_and_read_only_default(self):
        lowered = _flat_text()
        assert "dry-run by default" in lowered
        assert "safe-fix" in lowered or "safe fix" in lowered
        assert "recommendation" in lowered
        assert any(
            phrase in lowered
            for phrase in (
                "never applied automatically",
                "never modified automatically",
                "never auto-applied",
            )
        )
        assert "operator approval" in lowered

    def test_cron_contract_states_printed_not_installed(self):
        text = _doc_text()
        lowered = _flat_text()
        assert "--install-cron" in text
        assert "weekly" in lowered
        assert "printed only; not installed" in lowered
        assert "crontab -e" in lowered
        assert ".worklog/skill-linter-cron.log" in text
        # Never claim the schedule is already active.
        assert "not claim" in lowered or "must not claim" in lowered

    def test_reference_is_linked_from_the_docs_index(self):
        assert "docs/dev/skill-linter-reference.md" in README.read_text(encoding="utf-8"), (
            "README.md must link the reference so the doc is discoverable from "
            "the repository's docs index"
        )
