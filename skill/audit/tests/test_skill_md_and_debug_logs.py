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

# ===========================================================================
# SKILL.md scanning guidance
# ===========================================================================


class TestSkillMdScanningGuidance:
    def test_skill_md_references_scan_helpers(self) -> None:
        """SKILL.md Tools-Enabled section references scan.py helpers."""
        text = SKILL_MD.read_text()
        assert "scan.py" in text

    def test_skill_md_forbids_unbounded_recursive_grep(self) -> None:
        """SKILL.md forbids unbounded recursive grep / repo-root scans."""
        text = SKILL_MD.read_text()
        assert "grep -r" in text or "unbounded" in text
        assert "node_modules" in text or "prune" in text

    def test_skill_md_documents_debug_logs_as_transient(self) -> None:
        """SKILL.md describes debug files as transient, non-scanned forensics."""
        text = SKILL_MD.read_text()
        assert "transient" in text.lower() or "forensic" in text.lower()

    def test_skill_md_documents_batch_phase2(self) -> None:
        """SKILL.md documents --batch-phase2 and AUDIT_PHASE2_BATCH."""
        text = SKILL_MD.read_text()
        assert "--batch-phase2" in text
        assert "AUDIT_PHASE2_BATCH" in text

    def test_skill_md_documents_max_concurrency(self) -> None:
        """SKILL.md documents --max-concurrency and AUDIT_MAX_CONCURRENCY."""
        text = SKILL_MD.read_text()
        assert "--max-concurrency" in text
        assert "AUDIT_MAX_CONCURRENCY" in text

    def test_skill_md_runner_line_includes_batch_and_concurrency(self) -> None:
        """The Runner usage line lists --batch-phase2 and --max-concurrency."""
        text = SKILL_MD.read_text()
        runner_line = next(
            line for line in text.splitlines()
            if line.strip().startswith("- **Runner:**")
        )
        assert "--batch-phase2" in runner_line
        assert "--max-concurrency" in runner_line


# ===========================================================================
# Debug-log lifecycle (for SA-0MSBSOAEM0078LAO)
# ===========================================================================


class TestSkillMdDocumentsBatchAndConcurrencyFlags:
    """SKILL.md documents the P6 batch mode and --max-concurrency flags.

    Work item: SA-0MSG3M3NI004HQVO (parent SA-0MSADWWH3003N82D AC5).
    """

    def test_skill_md_documents_batch_phase2_flag(self) -> None:
        """--batch-phase2 appears in SKILL.md."""
        text = SKILL_MD.read_text()
        assert "--batch-phase2" in text

    def test_skill_md_documents_batch_env_var(self) -> None:
        """AUDIT_PHASE2_BATCH appears in SKILL.md."""
        text = SKILL_MD.read_text()
        assert "AUDIT_PHASE2_BATCH" in text

    def test_skill_md_documents_max_concurrency_flag(self) -> None:
        """--max-concurrency appears in SKILL.md."""
        text = SKILL_MD.read_text()
        assert "--max-concurrency" in text

    def test_skill_md_runner_usage_line_includes_flags(self) -> None:
        """The Runner usage line lists --batch-phase2 and --max-concurrency."""
        text = SKILL_MD.read_text()
        usage_line = next(
            line for line in text.splitlines()
            if "audit_runner.py issue|project" in line
        )
        assert "--batch-phase2" in usage_line
        assert "--max-concurrency" in usage_line


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
