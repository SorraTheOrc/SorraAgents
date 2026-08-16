#!/usr/bin/env python3
"""Tests for audit SKILL.md scanning guidance and debug-log lifecycle.

Work item: SA-0MSBR0E8Y0022Z4V (parent SA-0MSAEJCP7002LTIM).

Two groups:

1. **SKILL.md guidance (AC5):** the Tools-Enabled section references the
   bounded scan.py helpers and forbids unbounded recursive grep.

2. **Debug-log lifecycle (extended scope, for SA-0MSBSOAEM0078LAO):**
   - ``_default_debug_log_path`` resolves outside ``.worklog/`` and outside
     the repo tree; an explicit ``--debug-log`` override still wins.
   - Kept debug entries preserve full ``raw_stdout``/``raw_stderr`` content
     (no truncation).
   - Successful audit runs leave no debug file; failed runs retain it.
   - ``cleanup_debug_logs.py`` removes files older than retention, keeps
     recent ones, and does nothing in dry-run mode.

All tests run offline.
"""  # noqa: EXE001
from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from skill.audit.scripts import audit_runner

SKILL_MD = REPO_ROOT / "skill" / "audit" / "SKILL.md"
SKILL_REF = REPO_ROOT / "docs" / "dev" / "audit-skill-reference.md"


def _skill_docs() -> str:
    """Return SKILL.md + reference-doc content (F5 relocated detail to docs)."""
    parts = [SKILL_MD.read_text()]
    if SKILL_REF.exists():
        parts.append(SKILL_REF.read_text())
    return "\n".join(parts)

# ===========================================================================
# SKILL.md scanning guidance
# ===========================================================================


class TestSkillMdScanningGuidance:
    def test_skill_md_references_scan_helpers(self) -> None:
        """SKILL.md Tools-Enabled section references scan.py helpers."""
        text = _skill_docs()
        assert "scan.py" in text

    def test_skill_md_forbids_unbounded_recursive_grep(self) -> None:
        """SKILL.md forbids unbounded recursive grep / repo-root scans."""
        text = _skill_docs()
        assert "grep -r" in text or "unbounded" in text
        assert "node_modules" in text or "prune" in text

    def test_skill_md_documents_debug_logs_as_transient(self) -> None:
        """SKILL.md describes debug files as transient, non-scanned forensics."""
        text = _skill_docs()
        assert "transient" in text.lower() or "forensic" in text.lower()

    def test_skill_md_documents_batch_phase2(self) -> None:
        """SKILL.md documents --batch-phase2 and AUDIT_PHASE2_BATCH."""
        text = _skill_docs()
        assert "--batch-phase2" in text
        assert "AUDIT_PHASE2_BATCH" in text

    def test_skill_md_documents_max_concurrency(self) -> None:
        """SKILL.md documents --max-concurrency and AUDIT_MAX_CONCURRENCY."""
        text = _skill_docs()
        assert "--max-concurrency" in text
        assert "AUDIT_MAX_CONCURRENCY" in text

    def test_skill_md_runner_line_includes_batch_and_concurrency(self) -> None:
        """The Runner usage line lists --batch-phase2 and --max-concurrency."""
        text = _skill_docs()
        runner_line = next(
            line for line in text.splitlines()
            if line.strip().startswith("- **Runner:**")
        )
        assert "--batch-phase2" in runner_line
        assert "--max-concurrency" in runner_line


# ===========================================================================
# SKILL.md path resolvability (SA-0MSL1ZBY0007PIVD)
# ===========================================================================


class TestSkillMdPathResolvability:
    """Every repo path referenced in SKILL.md resolves to a real file.

    Catches doc-drift like the stale ``./scripts/failure_notice.py``
    reference (the module lives one level up in ``skill/scripts/``). Only
    concrete repo-relative paths are checked: flags, env vars, commands
    (``/skill:test``), placeholders (``<id>``), home paths (``~``), and
    absolute paths (``/llama/...``) are out of scope.
    """

    SKILL_DIR = REPO_ROOT / "skill" / "audit"

    def _path_tokens(self):
        """Backtick-quoted tokens + markdown-link targets from SKILL.md."""
        text = SKILL_MD.read_text()
        tokens = set(re.findall(r"`([^`]+)`", text))
        tokens |= set(re.findall(r"\]\(([^)]+)\)", text))
        return sorted(t.strip() for t in tokens)

    def _resolve(self, token):
        """Resolve a token to a repo path, or None when out of scope."""
        if token.startswith(("~", "/", "<")):
            return None  # home / absolute / placeholder
        if token.startswith(("./", "../", "evidence/")):
            return self.SKILL_DIR / token
        if token.startswith(("skill/", "docs/", "tests/")):
            return REPO_ROOT / token
        return None  # bare flag / command / env var / filename mention

    def test_every_referenced_path_resolves(self):
        """AC1: every repo path referenced in SKILL.md exists on disk."""
        missing = []
        for token in self._path_tokens():
            path = self._resolve(token)
            if path is not None and not path.exists():
                missing.append(f"{token} -> {path}")
        assert missing == [], \
            "SKILL.md references paths that do not exist:\n" + "\n".join(missing)

    def test_failure_notice_points_at_shared_module(self):
        """The failure-notice banner references ../scripts/ (one level up),
        never the stale ./scripts/failure_notice.py (the module lives in
        the shared scripts dir, above the audit scripts)."""
        text = SKILL_MD.read_text()
        assert "../scripts/failure_notice.py" in text
        assert not re.search(r"`\./scripts/failure_notice\.py`", text)


# ===========================================================================
# Context reduction documentation (SA-0MSRVNMFW005LWZL)
# ===========================================================================


class TestSkillMdContextReductionDoc:
    """SKILL.md documents the context-reduction flags AND the rationale.

    Work item: SA-0MSRVNMFW005LWZL (gap remediation after the 2026-08-13
    re-audit rated AC5 partial — SKILL.md documented the "what" (flags) but
    not the "why" (prompts are self-contained); the rationale had been
    trimmed by later SKILL.md edits). The restored text must state both the
    mechanism and the self-contained invariant in its own words.
    """

    def test_skill_md_documents_flags_in_context_reduction(self) -> None:
        """SKILL.md scripts section names both flags for _call_pi calls."""
        text = SKILL_MD.read_text()
        assert "--no-context-files" in text
        assert "--no-skills" in text

    def test_skill_md_documents_why_self_contained(self) -> None:
        """SKILL.md states the rationale: prompts are fully self-contained."""
        text = SKILL_MD.read_text()
        assert "self-contained" in text
        assert "never depend" in text or "invariant" in text

    def test_skill_md_evidence_dir_referenced(self) -> None:
        """SKILL.md points to the recorded in-scope AC2/AC3 evidence."""
        text = SKILL_MD.read_text()
        assert "evidence/" in text


# ===========================================================================
# Monitored Run Execution guidance (for SA-0MSL51XSF0086KM5)
# ===========================================================================


class TestSkillMdMonitoredRunGuidance:
    """SKILL.md documents the launch → monitor → abort workflow for long audits.

    Work item: SA-0MSL51XSF0086KM5 (test-first verification contract, child
    SA-0MSL6DQVN0036IGM). Audits can legitimately run for hours, so SKILL.md
    must define an agent-side execution contract: a detached launch with a
    180-minute hard budget, output captured to a unique log, a 3-minute
    progress-monitoring cadence, and a defined abort + mitigation procedure.
    Guidance-only change — these tests assert the documented markers exist.
    """

    @staticmethod
    def _section() -> str:
        """Return the Monitored Run Execution section (reference doc preferred;
        F5 relocated the full detail to docs/dev/audit-skill-reference.md)."""
        candidates = []
        if SKILL_REF.exists():
            candidates.append(SKILL_REF.read_text())
        candidates.append(SKILL_MD.read_text())
        for text in candidates:
            start = text.find("## Monitored Run Execution")
            if start != -1:
                end = text.find("\n## ", start + 1)
                return text[start : end if end != -1 else len(text)]
        return ""

    def test_skill_md_has_monitored_run_execution_section(self) -> None:
        """SKILL.md contains a Monitored Run Execution section heading."""
        assert "## Monitored Run Execution" in _skill_docs()

    def test_launch_captures_pre_audit_status_and_stage(self) -> None:
        """Launch documents capturing the pre-audit status/stage via wl show."""
        section = self._section()
        assert "wl show <id> --json" in section
        assert "pre-audit" in section.lower()
        assert "status" in section
        assert "stage" in section

    def test_launch_is_detached_with_unique_log(self) -> None:
        """Launch uses nohup/disown and a unique audit_run_ log path."""
        section = self._section()
        assert "nohup" in section
        assert "disown" in section
        assert "audit_run_" in section
        assert "~/.audit_debug/" in section

    def test_launch_enforces_180_minute_hard_budget(self) -> None:
        """Launch enforces the 10800s (180-minute) outer budget."""
        section = self._section()
        assert "10800" in section
        assert "180-minute" in section

    def test_monitor_reports_every_3_minutes_with_alive_check(self) -> None:
        """Monitor specifies the 3-minute cadence and kill -0 alive check."""
        section = self._section()
        assert "every 3 minutes" in section
        assert "kill -0" in section

    def test_monitor_tails_log_for_phase_markers(self) -> None:
        """Monitor tails the log for the runner's phase/timing markers."""
        section = self._section()
        assert "tail -50" in section
        assert "Phase 1 passed: running Phase 2 deep code analysis" in section
        assert "Per-call timing:" in section

    def test_monitor_confirms_log_growth(self) -> None:
        """Monitor treats a stopped-growing log as a stall signal."""
        section = self._section()
        assert "growth" in section.lower() or "growing" in section.lower()

    def test_abort_defines_stall_trigger(self) -> None:
        """Abort defines the >=10 minute no-output stall trigger."""
        section = self._section()
        assert "10 minutes" in section

    def test_abort_defines_repeated_failure_trigger(self) -> None:
        """Abort defines the >=3 consecutive Pi-call-failure trigger."""
        section = self._section()
        assert "3 consecutive" in section
        assert "Warning: Pi call failed" in section

    def test_abort_restores_pre_audit_state_and_clears_assignee(self) -> None:
        """Abort restores pre-audit status/stage and clears the assignee."""
        section = self._section()
        assert "restore" in section.lower()
        assert "assignee" in section.lower()

    def test_abort_kills_process_tree_and_appends_failure_notice(self) -> None:
        """Abort kills the process tree and appends a failure notice."""
        section = self._section()
        assert "process tree" in section
        assert "failure notice" in section.lower()

    def test_abort_failure_notice_includes_progress_summary(self) -> None:
        """Failure notice mandates elapsed time, last phase marker, and trigger.

        AC3 (SA-0MSL6E6KD006KJ0K): the progress-summary fields must appear in
        SKILL.md itself, not only in the reference doc. The reference doc is
        preferred by ``_section()``, so read SKILL.md directly to catch a
        regression where the summary omits the required fields.
        """
        text = SKILL_MD.read_text()
        start = text.find("## Monitored Run Execution")
        assert start != -1
        end = text.find("\n## ", start + 1)
        section = text[start : end if end != -1 else len(text)]
        assert "failure notice" in section.lower()
        assert "elapsed time" in section.lower()
        assert "phase marker" in section.lower()
        assert "trigger" in section.lower()

    def test_abort_never_fabricates_or_overrides_a_verdict(self) -> None:
        """Abort never persists a fabricated report or overrides a verdict."""
        section = self._section()
        assert "fabricat" in section.lower()
        assert "override" in section.lower()

    def test_abort_reports_outcome_to_operator(self) -> None:
        """Abort requires reporting the outcome to the operator.

        AC4 (SA-0MSL6E6KD006KJ0K): the operator-report step must appear in
        SKILL.md itself, not only in the reference doc. The reference doc is
        preferred by ``_section()``, so read SKILL.md directly to catch a
        regression where the summary omits the report step.
        """
        text = SKILL_MD.read_text()
        start = text.find("## Monitored Run Execution")
        assert start != -1
        end = text.find("\n## ", start + 1)
        section = text[start : end if end != -1 else len(text)]
        assert "report" in section.lower()
        assert "operator" in section.lower()


# ===========================================================================
# Debug-log lifecycle (for SA-0MSBSOAEM0078LAO)
# ===========================================================================


class TestSkillMdDocumentsBatchAndConcurrencyFlags:
    """SKILL.md documents the P6 batch mode and --max-concurrency flags.

    Work item: SA-0MSG3M3NI004HQVO (parent SA-0MSADWWH3003N82D AC5).
    """

    def test_skill_md_documents_batch_phase2_flag(self) -> None:
        """--batch-phase2 appears in SKILL.md."""
        text = _skill_docs()
        assert "--batch-phase2" in text

    def test_skill_md_documents_batch_env_var(self) -> None:
        """AUDIT_PHASE2_BATCH appears in SKILL.md."""
        text = _skill_docs()
        assert "AUDIT_PHASE2_BATCH" in text

    def test_skill_md_documents_max_concurrency_flag(self) -> None:
        """--max-concurrency appears in SKILL.md."""
        text = _skill_docs()
        assert "--max-concurrency" in text

    def test_skill_md_runner_usage_line_includes_flags(self) -> None:
        """The Runner usage line lists --batch-phase2 and --max-concurrency."""
        text = _skill_docs()
        usage_line = next(
            line for line in text.splitlines()
            if "audit_runner.py issue|project" in line
        )
        assert "--batch-phase2" in usage_line
        assert "--max-concurrency" in usage_line


class TestSkillMdReadOnlyException:
    """SKILL.md + reference doc document the justified READ-ONLY exception
    (SA-0MST01Q4W005G495 AC1-AC3)."""

    def test_exception_scope_documented(self) -> None:
        """The exception scope (config write, local commit, chore creation,
        code-quality re-run) is documented; never close/delete/push."""
        text = _skill_docs()
        assert "READ-ONLY exception" in text
        assert "per-file-ignores" in text
        assert "committed locally (no push)" in text
        assert "chore" in text.lower()
        assert "Do NOT close or delete work items" in text

    def test_no_create_rule_relaxation_documented(self) -> None:
        """The no-create rule states the config-fix chore exception and its
        preconditions (model confidence + no-breakage verification)."""
        text = _skill_docs()
        assert "ONLY relaxation of the no-create rule" in text
        assert "confident-false-positive" in text
        assert "no-breakage verification" in text

    def test_reference_doc_documents_false_positive_screen(self) -> None:
        """The reference doc documents the screen classifications and
        caution-first degradation."""
        text = _skill_docs()
        assert "False-positive screen" in text
        assert "confident-false-positive" in text
        assert "caution-first" in text
        assert "uncertain" in text

    def test_reference_doc_documents_remediation_loop_and_env_var(self) -> None:
        """The reference doc documents the remediation loop and the env var
        with its default (3)."""
        text = _skill_docs()
        assert "Remediation loop" in text
        assert "AUDIT_REMEDIATION_MAX_ITERATIONS" in text
        assert "default `3`" in text
        assert "remediation loop exhausted" in text

    def test_reference_doc_documents_chore_creation(self) -> None:
        """The reference doc documents chore creation for config fixes and
        medium/low tracking (worklog resolution, fail-safe)."""
        text = _skill_docs()
        assert "Chore work-item creation" in text
        assert "candidate false positive — producer decision required" in text
        assert "--worklog-dir" in text
        assert "chore_failures" in text


class TestDebugLogLocation:
    def test_default_path_outside_worklog_and_repo(self) -> None:
        """_default_debug_log_path resolves outside .worklog/ and repo tree."""
        with mock.patch.object(audit_runner, "TARGET_PROJECT_ROOT",
                               Path("/tmp/fake-repo")), mock.patch("pathlib.Path.home",
                        return_value=Path("/home/fakeuser")):
            p = audit_runner._default_debug_log_path("SA-1", "parent")
        p = Path(p)
        assert ".worklog" not in p.parts
        assert str(p).startswith("/home/fakeuser")
        assert p.name == "audit_debug_SA-1.jsonl"

    def test_explicit_debug_log_override_wins(self) -> None:
        """An explicit --debug-log path still wins over the default."""
        # The default helper is monkeypatched by tests; the runner passes an
        # explicit debug_log through to _call_pi_and_maybe_log. Verify the
        # explicit path is used when provided.
        explicit = "/tmp/explicit-debug/audit_debug_SA-1.jsonl"
        with mock.patch.object(
            audit_runner, "_call_pi", return_value={
                "verdict": "met", "evidence": "ok",
                "raw_stdout": "", "raw_stderr": "", "elapsed_seconds": 1.0,
            }
        ), mock.patch.object(audit_runner, "_write_debug_log") as mock_write:
            audit_runner._call_pi_and_maybe_log(
                "SA-1", "parent", "prompt", debug_log=explicit,
            )
        assert mock_write.called
        written_path = mock_write.call_args[0][0]
        assert str(written_path) == explicit


class TestDebugLogContentFidelity:
    def test_kept_entries_preserve_full_content(self) -> None:
        """Debug entries keep raw_stdout/raw_stderr unchanged (no truncation)."""
        big_stdout = "x" * 500_000
        big_stderr = "y" * 50_000
        entry = {
            "issue_id": "SA-1", "context": "parent", "reason": "parse_failure",
            "raw_stdout": big_stdout, "raw_stderr": big_stderr,
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "audit_debug_SA-1.jsonl"
            audit_runner._write_debug_log(path, entry)
            written = json.loads(path.read_text())
        assert written["raw_stdout"] == big_stdout
        assert written["raw_stderr"] == big_stderr
        assert len(written["raw_stdout"]) == 500_000
        assert len(written["raw_stderr"]) == 50_000


class TestRunCompletionCleanup:
    """Successful audit runs remove their debug file; failed runs retain it.

    SA-0MSBSOAEM0078LAO AC3 (verified by SA-0MSLSHK9600667FO post-audit
    remediation): ``_remove_debug_log`` is dead code unless wired into the
    cmd_issue/cmd_project finally paths. These tests exercise the wiring
    directly (the runner is unit-tested elsewhere with mocked _call_pi).
    """

    def test_remove_debug_log_removes_default_and_explicit(self) -> None:
        """_remove_debug_log unlinks the default path and an explicit path."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            explicit = tmp_p / "explicit.jsonl"
            explicit.write_text('{"a": 1}\n')
            default = tmp_p / "audit_debug_SA-1.jsonl"
            default.write_text('{"b": 2}\n')
            with mock.patch.object(audit_runner, "_default_debug_log_path",
                                   return_value=default):
                # Explicit path wins when provided.
                audit_runner._remove_debug_log(str(explicit), "SA-1")
                assert not explicit.exists()
                assert default.exists()
                # Without an explicit path, the default is removed.
                audit_runner._remove_debug_log(None, "SA-1")
                assert not default.exists()

    def test_remove_debug_log_ignores_missing_file(self) -> None:
        """Removing a non-existent debug file is a silent no-op."""
        with tempfile.TemporaryDirectory() as tmp:
            audit_runner._remove_debug_log(str(Path(tmp) / "nope.jsonl"), "SA-1")
        # No exception raised; nothing to assert beyond reaching here.

    @staticmethod
    def _fake_run_wl(runner, cmd, worklog_dir=None):
        """Return plausible JSON for any wl command issued by cmd_issue/"cmd_project"."""
        cmd_str = " ".join(cmd)
        if "show" in cmd_str and "--children" not in cmd_str:
            return {"success": True, "workItem": {"id": "SA-X", "status": "open", "stage": "plan_complete"}}
        if "--children" in cmd_str:
            return {"success": True, "workItem": {"id": "SA-X", "description": "", "status": "open"}, "children": []}
        if "update" in cmd_str or "list" in cmd_str:
            return {"success": True}
        return {"success": True}

    def test_cmd_issue_removes_debug_log_on_success(self) -> None:
        """cmd_issue deletes the debug file when the audit completes."""
        with tempfile.TemporaryDirectory() as tmp:
            debug_path = Path(tmp) / "audit_debug_SA-X.jsonl"
            debug_path.write_text('{"raw_stdout": "x"}\n')
            with mock.patch.object(audit_runner, "_call_pi", return_value={
                "verdict": "no", "evidence": "gap", "raw_stdout": "",
                "raw_stderr": "", "elapsed_seconds": 1.0,
            }), mock.patch.object(audit_runner, "_run_wl",
                                  side_effect=self._fake_run_wl), \
                 mock.patch("skill.code_review.scripts.code_quality.run_code_quality",
                            return_value={"success": True, "findings": [], "fixes_applied": 0}), \
                 mock.patch.object(audit_runner, "_remove_debug_log") as mock_remove, \
                 mock.patch.object(audit_runner, "_default_debug_log_path",
                                   return_value=debug_path):
                audit_runner.cmd_issue(
                    "SA-X", persist=False, force=True, debug_log=str(debug_path),
                )
            # Cleanup invoked on the success path (even for a 'No' verdict).
            assert mock_remove.called
            args = mock_remove.call_args[0]
            assert args[0] == str(debug_path)
            assert args[1] == "SA-X"

    def test_cmd_project_removes_debug_log_on_success(self) -> None:
        """cmd_project deletes the debug file when the audit completes."""
        with tempfile.TemporaryDirectory() as tmp:
            debug_path = Path(tmp) / "audit_debug_project.jsonl"
            debug_path.write_text('{"raw_stdout": "x"}\n')
            with mock.patch.object(audit_runner, "_call_pi", return_value={
                "extracted_text": '{"summary": "s", "recommendation": "r"}',
                "raw_stdout": "", "raw_stderr": "", "elapsed_seconds": 1.0,
            }), mock.patch.object(audit_runner, "_run_wl",
                                  side_effect=self._fake_run_wl), \
                 mock.patch.object(audit_runner, "_remove_debug_log") as mock_remove, \
                 mock.patch.object(audit_runner, "_default_debug_log_path",
                                   return_value=debug_path):
                audit_runner.cmd_project(debug_log=str(debug_path))
            assert mock_remove.called
            args = mock_remove.call_args[0]
            assert args[0] == str(debug_path)
            assert args[1] == "project"

    def test_cmd_issue_retains_debug_log_on_failure(self) -> None:
        """cmd_issue keeps the debug file when a script failure occurs (SA-0MSBSOAEM0078LAO AC3)."""
        with tempfile.TemporaryDirectory() as tmp:
            debug_path = Path(tmp) / "audit_debug_SA-X.jsonl"
            debug_path.write_text('{"raw_stdout": "x"}\n')
            original_content = debug_path.read_text()
            # Provide an AC in the description so Phase 1 calls _call_pi
            # (which raises, triggering script_failure → debug log retained).
            work_with_ac = {"success": True,
                            "workItem": {"id": "SA-X",
                                         "status": "open",
                                         "stage": "plan_complete",
                                         "description": "Acceptance Criteria:\n- Verify X works"}}
            def _run_wl_with_ac(runner, cmd, worklog_dir=None):
                cmd_str = " ".join(cmd)
                if "show" in cmd_str:  # both --json and --children --json
                    return work_with_ac
                if "update" in cmd_str or "list" in cmd_str:
                    return {"success": True}
                return {"success": True}
            with mock.patch.object(audit_runner, "_call_pi",
                                   side_effect=RuntimeError("provider timeout")), \
                 mock.patch.object(audit_runner, "_run_wl",
                                   side_effect=_run_wl_with_ac), \
                 mock.patch("skill.code_review.scripts.code_quality.run_code_quality",
                            return_value={"success": True, "findings": [], "fixes_applied": 0}), \
                 mock.patch.object(audit_runner, "_default_debug_log_path",
                                   return_value=debug_path):
                audit_runner.cmd_issue("SA-X", persist=False, force=True,
                                       debug_log=str(debug_path))
            # Debug file retained for forensics (may contain additional entries).
            assert debug_path.exists()
            # The original content must still be present (file was NOT deleted).
            assert original_content in debug_path.read_text()

    def test_cmd_project_retains_debug_log_on_failure(self) -> None:
        """cmd_project keeps the debug file when _call_pi raises (failure-path retention)."""
        with tempfile.TemporaryDirectory() as tmp:
            debug_path = Path(tmp) / "audit_debug_project.jsonl"
            debug_path.write_text('{"raw_stdout": "x"}\n')
            original_content = debug_path.read_text()
            with mock.patch.object(audit_runner, "_call_pi",
                                   side_effect=RuntimeError("pi unavailable")), \
                 mock.patch.object(audit_runner, "_run_wl",
                                   side_effect=self._fake_run_wl), \
                 mock.patch.object(audit_runner, "_default_debug_log_path",
                                   return_value=debug_path):
                audit_runner.cmd_project(debug_log=str(debug_path))
            # Debug file retained for forensics.
            assert debug_path.exists()
            assert debug_path.read_text() == original_content


# ===========================================================================
# cleanup_debug_logs.py
# ===========================================================================


class TestCleanupDebugLogs:
    CLEANUP_SCRIPT = REPO_ROOT / "skill" / "audit" / "scripts" / "cleanup_debug_logs.py"

    def _make_debug_dir(self, old_days: int = 20) -> Path:
        root = Path(tempfile.mkdtemp(prefix="debug-sweep-"))
        old = root / "audit_debug_OLD.jsonl"
        old.write_text('{"raw_stdout": "old"}\n')
        # backdate mtime beyond retention
        cutoff = time.time() - old_days * 86400 - 3600
        os_utime = __import__("os").utime
        os_utime(old, (cutoff, cutoff))
        fresh = root / "audit_debug_NEW.jsonl"
        fresh.write_text('{"raw_stdout": "new"}\n')
        return root

    def test_cleanup_dry_run_makes_no_changes(self) -> None:
        """Default dry-run: no files removed."""
        root = self._make_debug_dir()
        proc = subprocess.run(
            [sys.executable, str(self.CLEANUP_SCRIPT), "--dir", str(root)],
            capture_output=True, text=True, timeout=60,
            check=False,
        )
        assert proc.returncode == 0
        assert (root / "audit_debug_OLD.jsonl").exists()
        assert (root / "audit_debug_NEW.jsonl").exists()

    def test_cleanup_apply_removes_old_keeps_recent(self) -> None:
        """--apply removes files older than retention, keeps recent ones."""
        root = self._make_debug_dir()
        proc = subprocess.run(
            [sys.executable, str(self.CLEANUP_SCRIPT),
             "--dir", str(root), "--apply", "--older-than", "14"],
            capture_output=True, text=True, timeout=60,
            check=False,
        )
        assert proc.returncode == 0
        assert not (root / "audit_debug_OLD.jsonl").exists()
        assert (root / "audit_debug_NEW.jsonl").exists()

    def test_cleanup_respects_retention(self) -> None:
        """With a long retention, even old files are kept."""
        root = self._make_debug_dir(old_days=3)  # only 3 days old
        proc = subprocess.run(
            [sys.executable, str(self.CLEANUP_SCRIPT),
             "--dir", str(root), "--apply", "--older-than", "14"],
            capture_output=True, text=True, timeout=60,
            check=False,
        )
        assert proc.returncode == 0
        assert (root / "audit_debug_OLD.jsonl").exists()
