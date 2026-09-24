from __future__ import annotations

import contextlib
import datetime
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pytest
from audit.scripts import audit_runner


@pytest.fixture(autouse=True)
def _free_audit_slot():
    """Neutralize the host-wide audit semaphore for deterministic unit tests.

    ``_call_pi`` acquires the real cross-process audit semaphore before
    launching the (mocked) subprocess. Under concurrent audit load the
    semaphore can saturate, making these timing-path unit tests flaky (see
    SA-0MSCDC4750019G9Y, SA-0MSCDC76A007JCJK). Replace it with a
    null-context so the mocked return paths are exercised directly.

    The real semaphore behavior is covered separately by
    ``test_audit_runner_concurrency.py``.
    """
    with mock.patch.object(
        audit_runner, "_acquire_audit_slot", return_value=contextlib.nullcontext()
    ):
        yield

class TestSharedFreshnessHelpers:
    """Unit tests for the shared ISO-8601 freshness helpers
    (SA-0MSL1Z70C007B9VZ): _parse_iso_utc and _audit_time_is_fresh.

    Both _check_audit_freshness and _get_child_audit_verdict previously
    inlined ~25 identical lines of Z-normalize → fromisoformat →
    tz-aware-ify → threshold-compare; the helpers below are the single
    implementation both call sites delegate to.
    """

    # ------------------------------------------------------------------
    # _parse_iso_utc
    # ------------------------------------------------------------------

    def test_parse_iso_utc_z_suffix_becomes_aware_utc(self):
        """'Z' suffix normalizes to +00:00 and stays tz-aware."""
        parsed = audit_runner._parse_iso_utc("2026-08-01T00:00:00.000Z")
        assert parsed is not None
        assert parsed.year == 2026 and parsed.month == 8 and parsed.day == 1
        assert parsed.tzinfo is not None
        assert parsed.utcoffset() == datetime.timedelta(0)

    def test_parse_iso_utc_naive_timestamp_gets_utc(self):
        """Naive timestamps (no offset) fall back to UTC."""
        parsed = audit_runner._parse_iso_utc("2026-08-01T00:00:00.000")
        assert parsed is not None
        assert parsed.tzinfo is not None
        assert parsed.utcoffset() == datetime.timedelta(0)

    def test_parse_iso_utc_explicit_offset_preserved(self):
        """An explicit non-UTC offset is preserved, not rewritten."""
        parsed = audit_runner._parse_iso_utc("2026-08-01T00:00:00+02:00")
        assert parsed is not None
        assert parsed.utcoffset() == datetime.timedelta(hours=2)

    def test_parse_iso_utc_invalid_returns_none(self):
        """Unparseable values return None instead of raising."""
        assert audit_runner._parse_iso_utc("not-a-date") is None
        assert audit_runner._parse_iso_utc(None) is None
        assert audit_runner._parse_iso_utc(42) is None

    # ------------------------------------------------------------------
    # _audit_time_is_fresh
    # ------------------------------------------------------------------

    def test_audit_time_is_fresh_after_buffer(self):
        """auditedAt > updatedAt + buffer → fresh."""
        audited = datetime.datetime(2026, 8, 2, tzinfo=datetime.timezone.utc)
        updated = datetime.datetime(2026, 8, 1, tzinfo=datetime.timezone.utc)
        assert audit_runner._audit_time_is_fresh(audited, updated) is True

    def test_audit_time_is_fresh_within_buffer(self):
        """auditedAt within updatedAt + buffer → not fresh (needs the
        persistence-tolerance check in the child gate)."""
        updated = datetime.datetime(2026, 8, 1, 0, 0, tzinfo=datetime.timezone.utc)
        audited = updated + datetime.timedelta(seconds=10)  # 10s < 60s buffer
        assert audit_runner._audit_time_is_fresh(audited, updated) is False

    def test_audit_time_is_fresh_before_update(self):
        """auditedAt before updatedAt → not fresh."""
        audited = datetime.datetime(2026, 8, 1, tzinfo=datetime.timezone.utc)
        updated = datetime.datetime(2026, 8, 2, tzinfo=datetime.timezone.utc)
        assert audit_runner._audit_time_is_fresh(audited, updated) is False

class TestContentFreshnessGate:
    """Tests for the content-based freshness gate (AC1-AC6).

    The gate captures a content fingerprint (git HEAD sha + work-item
    description hash + Key Files list) at audit time and stores it in the
    persisted report. Re-auditing an item whose fingerprint is unchanged
    returns the existing report in seconds instead of re-running the
    pipeline. A change in ANY fingerprint component invalidates freshness.
    Legacy audits without a fingerprint fall back to the 60s time floor.
    """

    @staticmethod
    def _proc(returncode: int, stdout: str):
        """Build a canned CompletedProcess for git subprocess calls."""
        return subprocess.CompletedProcess(
            args=[], returncode=returncode, stdout=stdout, stderr="",
        )


    _HEAD = "a" * 40
    _DESC = "## Acceptance Criteria\n- AC1: do the thing\n\n## Key Files\n- `src/a.py`"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _make_run_wl(self, audit_raw=None, audit_audited_at="2026-08-01T00:00:00.000Z",
                     work_item_desc=_DESC, work_item_updated_at="2026-07-01T00:00:00.000Z"):
        """Build a ``_run_wl`` fake returning a stored audit + work item."""
        def _fake(runner, cmd, worklog_dir=None):
            cmd_str = " ".join(cmd)
            if "audit-show" in cmd_str:
                return {
                    "success": True,
                    "audit": {
                        "auditedAt": audit_audited_at,
                        "rawOutput": audit_raw or "",
                    },
                }
            if "show" in cmd_str and "--children" not in cmd_str:
                return {
                    "success": True,
                    "workItem": {
                        "id": "TEST-1",
                        "description": work_item_desc,
                        "updatedAt": work_item_updated_at,
                    },
                }
            raise AssertionError(f"unexpected wl command: {cmd_str}")
        return _fake

    def _report_with_fingerprint(self, fingerprint, verdict="Yes"):
        """Assemble a report body carrying a fingerprint line."""
        return (
            f"Ready to close: {verdict}\n\n"
            f"Audit report for work item TEST-1\n\n"
            f"{audit_runner.AUDIT_CONTENT_FINGERPRINT_PREFIX}{fingerprint}\n\n"
            "## Summary\nAll criteria acceptable."
        )

    def _check(self, run_wl_fake, head=_HEAD):
        """Run _check_audit_freshness with mocked wl + git HEAD."""
        with (
            mock.patch.object(audit_runner, "_run_wl", side_effect=run_wl_fake),
            mock.patch.object(
                audit_runner, "_resolve_audited_head", return_value=head,
            ),
        ):
            return audit_runner._check_audit_freshness(mock.MagicMock(), "TEST-1")

    # ------------------------------------------------------------------
    # Fingerprint computation (AC2)
    # ------------------------------------------------------------------

    def test_fingerprint_changes_with_head_sha(self):
        """AC2/AC3: a different HEAD sha yields a different fingerprint."""
        with mock.patch.object(audit_runner, "_resolve_audited_head") as mock_head:
            mock_head.side_effect = ["h" * 40, "g" * 40]
            f1 = audit_runner._compute_content_fingerprint(
                mock.MagicMock(), "TEST-1", work_item={"description": self._DESC},
            )
            f2 = audit_runner._compute_content_fingerprint(
                mock.MagicMock(), "TEST-1", work_item={"description": self._DESC},
            )
        assert f1 != f2
        assert len(f1) == 64  # sha256 hex

    def test_fingerprint_changes_with_description(self):
        """AC3: a different description (ACs) yields a different fingerprint."""
        with mock.patch.object(
            audit_runner, "_resolve_audited_head", return_value=self._HEAD,
        ):
            f1 = audit_runner._compute_content_fingerprint(
                mock.MagicMock(), "TEST-1", work_item={"description": self._DESC},
            )
            f2 = audit_runner._compute_content_fingerprint(
                mock.MagicMock(), "TEST-1",
                work_item={"description": self._DESC + "\n- AC2: more"},
            )
        assert f1 != f2

    def test_fingerprint_changes_with_key_files(self):
        """AC3: a different Key Files list yields a different fingerprint."""
        with mock.patch.object(
            audit_runner, "_resolve_audited_head", return_value=self._HEAD,
        ):
            f1 = audit_runner._compute_content_fingerprint(
                mock.MagicMock(), "TEST-1", work_item={"description": self._DESC},
            )
            f2 = audit_runner._compute_content_fingerprint(
                mock.MagicMock(), "TEST-1",
                work_item={"description": self._DESC + "\n- `src/b.py`"},
            )
        assert f1 != f2

    def test_fingerprint_none_when_git_unavailable(self):
        """AC2 (fail-open): no HEAD sha → no fingerprint (pipeline re-runs)."""
        with mock.patch.object(
            audit_runner, "_resolve_audited_head", return_value=None,
        ):
            fp = audit_runner._compute_content_fingerprint(
                mock.MagicMock(), "TEST-1", work_item={"description": self._DESC},
            )
        assert fp is None

    def test_extract_fingerprint_roundtrip(self):
        """AC2: the fingerprint line is extracted back from a stored report."""
        fp = "f" * 64
        report = self._report_with_fingerprint(fp)
        assert audit_runner._extract_content_fingerprint(report) == fp

    def test_extract_fingerprint_none_for_legacy_report(self):
        """Legacy reports without the line yield None (time floor applies)."""
        assert audit_runner._extract_content_fingerprint(
            "Ready to close: Yes\n\n## Summary\nok"
        ) is None

    # ------------------------------------------------------------------
    # Working-tree state in the fingerprint (SA-0MSL1YXG7004F2BZ)
    # ------------------------------------------------------------------

    def _tree_state(self, *status_lines):
        """Compute a fingerprint given git status/diff output lines."""
        status = "\n".join(status_lines)

        def _fake_runner(cmd):
            cmd_str = " ".join(cmd)
            if cmd_str.startswith("git status"):
                return self._proc(0, status)
            if cmd_str.startswith("git diff"):
                return self._proc(0, "")  # diff name-only folded into status output
            raise AssertionError(f"unexpected cmd: {cmd_str}")

        return audit_runner._compute_content_fingerprint(
            _fake_runner, "TEST-1", work_item={"description": self._DESC},
        )

    def test_fingerprint_changes_with_uncommitted_modified_file(self):
        """AC1: an uncommitted tracked-file change invalidates the fingerprint."""
        with mock.patch.object(
            audit_runner, "_resolve_audited_head", return_value=self._HEAD,
        ):
            f_clean = self._tree_state()
            f_dirty = self._tree_state(" M src/a.py")
        assert f_clean != f_dirty

    def test_fingerprint_changes_with_untracked_file(self):
        """AC2: an added untracked file invalidates the fingerprint."""
        with mock.patch.object(
            audit_runner, "_resolve_audited_head", return_value=self._HEAD,
        ):
            f_clean = self._tree_state()
            f_untracked = self._tree_state("?? new_file.py")
        assert f_clean != f_untracked

    def test_fingerprint_committed_change_invalidates_via_head(self):
        """AC3: committing a change invalidates via the existing HEAD sha component."""
        with mock.patch.object(audit_runner, "_resolve_audited_head") as mock_head:
            mock_head.side_effect = ["h" * 40, "g" * 40]
            f1 = audit_runner._compute_content_fingerprint(
                mock.MagicMock(), "TEST-1", work_item={"description": self._DESC},
            )
            f2 = audit_runner._compute_content_fingerprint(
                mock.MagicMock(), "TEST-1", work_item={"description": self._DESC},
            )
        assert f1 != f2

    def test_fingerprint_stable_for_untouched_tree(self):
        """AC4: an untouched working tree keeps the same fingerprint."""
        with mock.patch.object(
            audit_runner, "_resolve_audited_head", return_value=self._HEAD,
        ):
            f1 = self._tree_state()
            f2 = self._tree_state()
        assert f1 == f2

    def test_fingerprint_stable_when_git_status_unavailable(self):
        """AC4 (fail-open): git status failure does not break the fingerprint.

        The working-tree component degrades to an empty marker so audits in
        environments without git (or with a broken runner) keep working;
        freshness still works off the remaining components.
        """
        def _fake_runner(cmd):
            cmd_str = " ".join(cmd)
            if cmd_str.startswith("git status"):
                return self._proc(1, "fatal: not a git repository")
            raise AssertionError(f"unexpected cmd: {cmd_str}")

        with mock.patch.object(
            audit_runner, "_resolve_audited_head", return_value=self._HEAD,
        ):
            fp = audit_runner._compute_content_fingerprint(
                _fake_runner, "TEST-1", work_item={"description": self._DESC},
            )
        assert fp is not None
        assert len(fp) == 64

    def test_fingerprint_changes_with_git_diff_output(self):
        """AC1: staged changes reported by git diff --name-only HEAD count too."""
        def _fake_runner(cmd):
            cmd_str = " ".join(cmd)
            if cmd_str.startswith("git status"):
                return self._proc(0, "")
            if cmd_str.startswith("git diff"):
                return self._proc(0, "src/a.py\nsrc/b.py")
            raise AssertionError(f"unexpected cmd: {cmd_str}")

        with mock.patch.object(
            audit_runner, "_resolve_audited_head", return_value=self._HEAD,
        ):
            f_clean = self._tree_state()
            f_staged = audit_runner._compute_content_fingerprint(
                _fake_runner, "TEST-1", work_item={"description": self._DESC},
            )
        assert f_clean != f_staged

    # ------------------------------------------------------------------
    # Freshness gate decisions (AC1, AC3)
    # ------------------------------------------------------------------

    def test_unchanged_fingerprint_skips(self):
        """AC1: unchanged fingerprint → existing report returned (skip)."""
        with mock.patch.object(
            audit_runner, "_resolve_audited_head", return_value=self._HEAD,
        ):
            fp = audit_runner._compute_content_fingerprint(
                mock.MagicMock(), "TEST-1", work_item={"description": self._DESC},
            )
        report = self._report_with_fingerprint(fp)
        run_wl = self._make_run_wl(audit_raw=report, work_item_desc=self._DESC)
        result = self._check(run_wl)
        assert result == report

    def test_head_sha_changed_reauths(self):
        """AC3: changed HEAD sha → fingerprint mismatch → re-audit (None)."""
        with mock.patch.object(
            audit_runner, "_resolve_audited_head", return_value=self._HEAD,
        ):
            fp = audit_runner._compute_content_fingerprint(
                mock.MagicMock(), "TEST-1", work_item={"description": self._DESC},
            )
        report = self._report_with_fingerprint(fp)
        run_wl = self._make_run_wl(audit_raw=report, work_item_desc=self._DESC)
        # Now the repository moved to a different HEAD
        result = self._check(run_wl, head="b" * 40)
        assert result is None

    def test_description_changed_reauths(self):
        """AC3: changed description → fingerprint mismatch → re-audit."""
        with mock.patch.object(
            audit_runner, "_resolve_audited_head", return_value=self._HEAD,
        ):
            fp = audit_runner._compute_content_fingerprint(
                mock.MagicMock(), "TEST-1", work_item={"description": self._DESC},
            )
        report = self._report_with_fingerprint(fp)
        changed_desc = self._DESC + "\n- AC2: added later"
        run_wl = self._make_run_wl(audit_raw=report, work_item_desc=changed_desc)
        result = self._check(run_wl)
        assert result is None

    def test_key_files_changed_reauths(self):
        """AC3: changed Key Files → fingerprint mismatch → re-audit."""
        with mock.patch.object(
            audit_runner, "_resolve_audited_head", return_value=self._HEAD,
        ):
            fp = audit_runner._compute_content_fingerprint(
                mock.MagicMock(), "TEST-1", work_item={"description": self._DESC},
            )
        report = self._report_with_fingerprint(fp)
        changed_desc = self._DESC + "\n- `src/other.py`"
        run_wl = self._make_run_wl(audit_raw=report, work_item_desc=changed_desc)
        result = self._check(run_wl)
        assert result is None

    def test_ready_no_verdict_not_masked_by_skip(self):
        """AC5: a stored 'Ready to close: No' verdict is returned verbatim,
        never masked by a freshness skip."""
        with mock.patch.object(
            audit_runner, "_resolve_audited_head", return_value=self._HEAD,
        ):
            fp = audit_runner._compute_content_fingerprint(
                mock.MagicMock(), "TEST-1", work_item={"description": self._DESC},
            )
        report = self._report_with_fingerprint(fp, verdict="No")
        run_wl = self._make_run_wl(audit_raw=report, work_item_desc=self._DESC)
        result = self._check(run_wl)
        assert result == report
        assert "Ready to close: No" in result

    # ------------------------------------------------------------------
    # 60s time floor interaction (AC1, AC4)
    # ------------------------------------------------------------------

    def test_legacy_audit_without_fingerprint_uses_time_floor(self):
        """AC1/AC4: audits without a fingerprint fall back to the 60s time
        gate — fresh when auditedAt > updatedAt + 60s, stale otherwise."""
        legacy_report = "Ready to close: Yes\n\n## Summary\nlegacy audit"
        # Audit 1 day after the last update → fresh by time gate
        run_wl = self._make_run_wl(
            audit_raw=legacy_report,
            audit_audited_at="2026-08-02T00:00:00.000Z",
            work_item_updated_at="2026-08-01T00:00:00.000Z",
        )
        assert self._check(run_wl) == legacy_report

        # Audit BEFORE the last update → stale by time gate
        run_wl = self._make_run_wl(
            audit_raw=legacy_report,
            audit_audited_at="2026-08-01T00:00:00.000Z",
            work_item_updated_at="2026-08-02T00:00:00.000Z",
        )
        assert self._check(run_wl) is None

    def test_legacy_audit_persistence_write_within_tolerance_is_fresh(self):
        """SA-0MTHC710X003ORZM / SA-0MSI3XH34001LLU4: legacy (fingerprint-less)
        audits whose updatedAt is the runner's own persistence write (wl
        audit-set + wl update --audit-text, ≤ 30 s after auditedAt) are
        treated as fresh even though auditedAt <= updatedAt + 60 s.

        Without this tolerance a just-persisted legacy audit would be marked
        stale by the time gate and the selection list would show \u23f3 instead
        of \u2705. Mirrors the child gate at _get_child_audit_verdict:4849."""
        legacy_report = "Ready to close: Yes\n\n## Summary\nlegacy audit"
        # auditedAt 00:00:00, updatedAt 00:00:10 (own write, within 30 s) → fresh
        run_wl = self._make_run_wl(
            audit_raw=legacy_report,
            audit_audited_at="2026-08-01T00:00:00.000Z",
            work_item_updated_at="2026-08-01T00:00:10.000Z",
        )
        assert self._check(run_wl) == legacy_report

        # Boundary: exactly 30 s is still fresh (inclusive, per timedelta check)
        run_wl = self._make_run_wl(
            audit_raw=legacy_report,
            audit_audited_at="2026-08-01T00:00:00.000Z",
            work_item_updated_at="2026-08-01T00:00:30.000Z",
        )
        assert self._check(run_wl) == legacy_report

        # Just past tolerance (31 s) and still inside the 60 s freshness buffer
        # — NOT fresh, because the gap is no longer the runner's own write.
        # (The 60 s buffer measures auditedAt > updatedAt + 60; here auditedAt
        # is BEFORE updatedAt so the time gate fails, and 31 s > 30 s so the
        # tolerance also fails.)
        run_wl = self._make_run_wl(
            audit_raw=legacy_report,
            audit_audited_at="2026-08-01T00:00:00.000Z",
            work_item_updated_at="2026-08-01T00:00:31.000Z",
        )
        assert self._check(run_wl) is None

    def test_fingerprint_gate_not_blocked_by_recent_update(self):
        """AC1: the content gate skips even when updatedAt moved after the
        audit (e.g. a comment added) — the 60s floor only applies to legacy
        audits without a fingerprint."""
        with mock.patch.object(
            audit_runner, "_resolve_audited_head", return_value=self._HEAD,
        ):
            fp = audit_runner._compute_content_fingerprint(
                mock.MagicMock(), "TEST-1", work_item={"description": self._DESC},
            )
        report = self._report_with_fingerprint(fp)
        # Item was updated AFTER the audit (updatedAt > auditedAt)
        run_wl = self._make_run_wl(
            audit_raw=report,
            audit_audited_at="2026-08-01T00:00:00.000Z",
            work_item_updated_at="2026-08-05T00:00:00.000Z",
        )
        result = self._check(run_wl)
        assert result == report

    def test_fingerprint_unavailable_reauths_fail_open(self):
        """AC1 (fail-open): when the stored audit has a fingerprint but the
        current fingerprint cannot be computed (git unavailable), the gate
        re-runs the pipeline instead of guessing."""
        with mock.patch.object(
            audit_runner, "_resolve_audited_head", return_value=self._HEAD,
        ):
            fp = audit_runner._compute_content_fingerprint(
                mock.MagicMock(), "TEST-1", work_item={"description": self._DESC},
            )
        report = self._report_with_fingerprint(fp)
        run_wl = self._make_run_wl(audit_raw=report, work_item_desc=self._DESC)
        # Git unavailable now → fingerprint computation returns None
        result = self._check(run_wl, head=None)
        assert result is None

    # ------------------------------------------------------------------
    # Report embedding (AC2)
    # ------------------------------------------------------------------

    def test_report_embeds_fingerprint_line(self):
        """AC2: the assembled report embeds the fingerprint metadata line so
        the persisted audit carries the freshness gate data."""
        report = audit_runner._assemble_issue_report(
            {"id": "TEST-1"}, [], [],
            model="Local Proxy/plan", model_source="local",
            content_fingerprint="f" * 64,
        )
        assert f"{audit_runner.AUDIT_CONTENT_FINGERPRINT_PREFIX}{'f' * 64}" in report

    def test_report_without_fingerprint_has_no_line(self):
        """Backward compatibility: no fingerprint → no metadata line."""
        report = audit_runner._assemble_issue_report(
            {"id": "TEST-1"}, [], [],
            model="Local Proxy/plan", model_source="local",
        )
        assert "Audit content fingerprint" not in report

    # ------------------------------------------------------------------
    # cmd_issue integration (AC4: --force bypass)
    # ------------------------------------------------------------------

    def test_force_bypasses_content_gate(self):
        """AC4: --force bypasses the content gate (fresh audit still
        re-runs the pipeline)."""
        updates = []

        def _make_runner():
            mock_runner = mock.MagicMock()

            def _side_effect(cmd):
                cmd_str = " ".join(cmd)
                if "audit-show" in cmd_str:
                    # Stored audit WITH a matching fingerprint
                    return SimpleNamespace(
                        returncode=0,
                        stdout=json.dumps({
                            "success": True,
                            "audit": {
                                "auditedAt": "2026-08-01T00:00:00.000Z",
                                "rawOutput": self._report_with_fingerprint("f" * 64),
                            },
                        }),
                        stderr="",
                    )
                if "update" in cmd_str:
                    updates.append(list(cmd))
                    return SimpleNamespace(
                        returncode=0,
                        stdout=json.dumps({"success": True}), stderr="",
                    )
                if "show" in cmd_str and "--children" not in cmd_str:
                    return SimpleNamespace(
                        returncode=0,
                        stdout=json.dumps({
                            "success": True,
                            "workItem": {
                                "id": "TEST-1", "status": "open",
                                "stage": "plan_complete",
                            },
                        }),
                        stderr="",
                    )
                if "--children" in cmd_str:
                    return SimpleNamespace(
                        returncode=0,
                        stdout=json.dumps({
                            "success": True,
                            "workItem": {
                                "id": "TEST-1", "status": "open",
                                "stage": "plan_complete",
                                "description": self._DESC,
                            },
                            "children": [],
                        }),
                        stderr="",
                    )
                return SimpleNamespace(
                    returncode=0, stdout=json.dumps({"success": True}), stderr="",
                )
            mock_runner.side_effect = _side_effect
            return mock_runner

        with (
            mock.patch.object(
                audit_runner, "_call_pi_and_maybe_log",
                return_value={"extracted_text": "[]"},
            ),
            mock.patch(
                "code_review.scripts.code_quality.run_code_quality",
                return_value={"success": True, "findings": [], "fixes_applied": 0},
            ),
            mock.patch.object(
                audit_runner, "_resolve_audited_head", return_value=self._HEAD,
            ),
        ):
            rc = audit_runner.cmd_issue(
                "TEST-1", persist=False, force=True, runner=_make_runner(),
            )
        assert rc == 0

    # ------------------------------------------------------------------
    # Freshness skip notice (AC2, SA-0MTFX6HMJ006QKR3): surfaces verdict
    # + auditedAt so a re-audit short-circuits without needing to read
    # the full raw report (re-audit coordination, SA-0MSQIA84B005NHWC).
    # ------------------------------------------------------------------

    def test_fresh_skip_notice_surfaces_verdict_and_timestamp(self):
        """AC2: the fast-path skip notice names the verdict and auditedAt
        of the fresh audit instead of a bare 'still fresh' line."""
        captured = []

        def _make_runner(fp: str):
            mock_runner = mock.MagicMock()

            def _side_effect(cmd):
                cmd_str = " ".join(cmd)
                if "audit-show" in cmd_str:
                    report = self._report_with_fingerprint(
                        fp, verdict="Yes",
                    )
                    return SimpleNamespace(
                        returncode=0,
                        stdout=json.dumps({
                            "success": True,
                            "audit": {
                                "auditedAt": "2026-08-01T00:00:00.000Z",
                                "rawOutput": report,
                            },
                        }),
                        stderr="",
                    )
                if "update" in cmd_str:
                    return SimpleNamespace(
                        returncode=0,
                        stdout=json.dumps({"success": True}), stderr="",
                    )
                if "show" in cmd_str and "--children" not in cmd_str:
                    return SimpleNamespace(
                        returncode=0,
                        stdout=json.dumps({
                            "success": True,
                            "workItem": {
                                "id": "TEST-1",
                                "status": "open",
                                "stage": "plan_complete",
                                "description": self._DESC,
                            },
                        }),
                        stderr="",
                    )
                return SimpleNamespace(
                    returncode=0, stdout=json.dumps({"success": True}), stderr="",
                )
            mock_runner.side_effect = _side_effect
            return mock_runner

        # The stored fingerprint must match the one the gate recomputes at
        # runtime, so derive it through the same fake runner + mocked HEAD.
        with mock.patch.object(
            audit_runner, "_resolve_audited_head", return_value=self._HEAD,
        ):
            fp = audit_runner._compute_content_fingerprint(
                _make_runner("f" * 64), "TEST-1",
                work_item={"description": self._DESC},
            )
        assert fp is not None

        with (
            mock.patch.object(
                audit_runner, "_call_pi_and_maybe_log",
                return_value={"extracted_text": "[]"},
            ),
            mock.patch.object(
                audit_runner, "_resolve_audited_head", return_value=self._HEAD,
            ),
            mock.patch(
                "builtins.print",
                side_effect=lambda *a, **k: captured.append(
                    " ".join(str(x) for x in a)
                ),
            ),
        ):
            rc = audit_runner.cmd_issue(
                "TEST-1", persist=False, force=False, runner=_make_runner(fp),
            )
        assert rc == 0
        joined = "\n".join(captured)
        assert "Skipping: audit still fresh" in joined
        assert "Ready to close: Yes" in joined
        assert "2026-08-01T00:00:00.000Z" in joined

    # ------------------------------------------------------------------
    # AC4 (SA-0MTFX6VD70097OEO): second audit at the same HEAD
    # short-circuits WITHOUT invoking the model — the re-audit-count
    # reduction target (re-audit coordination, SA-0MSQIA84B005NHWC).
    # ------------------------------------------------------------------

    def test_second_audit_same_head_short_circuits_without_model(self):
        """AC4: auditing twice at the same HEAD reuses the fresh audit; the
        second run exits 0 and never invokes the model (zero pi calls)."""
        pi_calls = []

        def _pi(**kwargs):
            pi_calls.append(kwargs)
            raise AssertionError(
                "model must not be invoked when a fresh audit exists "
                f"(call #{len(pi_calls)}: {kwargs})"
            )

        def _make_runner(fp: str):
            mock_runner = mock.MagicMock()

            def _side_effect(cmd):
                cmd_str = " ".join(cmd)
                if "audit-show" in cmd_str:
                    # Existing fresh audit: fingerprint matches the current
                    # content state (HEAD + description + Key Files + tree).
                    report = self._report_with_fingerprint(fp, verdict="Yes")
                    return SimpleNamespace(
                        returncode=0,
                        stdout=json.dumps({
                            "success": True,
                            "audit": {
                                "auditedAt": "2026-08-01T00:00:00.000Z",
                                "rawOutput": report,
                            },
                        }),
                        stderr="",
                    )
                if "update" in cmd_str:
                    return SimpleNamespace(
                        returncode=0,
                        stdout=json.dumps({"success": True}), stderr="",
                    )
                if "show" in cmd_str and "--children" not in cmd_str:
                    return SimpleNamespace(
                        returncode=0,
                        stdout=json.dumps({
                            "success": True,
                            "workItem": {
                                "id": "TEST-1",
                                "status": "open",
                                "stage": "plan_complete",
                                "description": self._DESC,
                            },
                        }),
                        stderr="",
                    )
                return SimpleNamespace(
                    returncode=0, stdout=json.dumps({"success": True}), stderr="",
                )
            mock_runner.side_effect = _side_effect
            return mock_runner

        # Deterministic fingerprint for the CURRENT state so the stored
        # report matches exactly (no 60s time-gate dependence).
        with mock.patch.object(
            audit_runner, "_resolve_audited_head", return_value=self._HEAD,
        ):
            fp = audit_runner._compute_content_fingerprint(
                _make_runner("f" * 64), "TEST-1",
                work_item={"description": self._DESC},
            )
        assert fp is not None

        with (
            mock.patch.object(
                audit_runner, "_call_pi_and_maybe_log", side_effect=_pi,
            ),
            mock.patch.object(
                audit_runner, "_resolve_audited_head", return_value=self._HEAD,
            ),
        ):
            rc = audit_runner.cmd_issue(
                "TEST-1", persist=False, force=False, runner=_make_runner(fp),
            )
        assert rc == 0
        assert pi_calls == [], "the fresh audit short-circuit must skip the model"

    def test_second_audit_force_still_invokes_model(self):
        """AC4 (guard): --force bypasses the short-circuit — the model IS
        invoked even with a matching fresh audit (re-audit target: only
        stale/forced re-audits are allowed)."""
        pi_calls = []

        def _make_runner(fp: str):
            mock_runner = mock.MagicMock()

            def _side_effect(cmd):
                cmd_str = " ".join(cmd)
                if "audit-show" in cmd_str:
                    report = self._report_with_fingerprint(fp, verdict="Yes")
                    return SimpleNamespace(
                        returncode=0,
                        stdout=json.dumps({
                            "success": True,
                            "audit": {
                                "auditedAt": "2026-08-01T00:00:00.000Z",
                                "rawOutput": report,
                            },
                        }),
                        stderr="",
                    )
                if "update" in cmd_str:
                    return SimpleNamespace(
                        returncode=0,
                        stdout=json.dumps({"success": True}), stderr="",
                    )
                if "show" in cmd_str and "--children" not in cmd_str:
                    return SimpleNamespace(
                        returncode=0,
                        stdout=json.dumps({
                            "success": True,
                            "workItem": {
                                "id": "TEST-1",
                                "status": "open",
                                "stage": "plan_complete",
                                "description": self._DESC,
                            },
                        }),
                        stderr="",
                    )
                if "--children" in cmd_str:
                    return SimpleNamespace(
                        returncode=0,
                        stdout=json.dumps({
                            "success": True,
                            "workItem": {
                                "id": "TEST-1", "status": "open",
                                "stage": "plan_complete",
                                "description": self._DESC,
                            },
                            "children": [],
                        }),
                        stderr="",
                    )
                return SimpleNamespace(
                    returncode=0, stdout=json.dumps({"success": True}), stderr="",
                )
            mock_runner.side_effect = _side_effect
            return mock_runner

        # The full pipeline needs code_quality + _call_pi to succeed; the
        # model invocation IS recorded (proving --force re-audits).
        def _pi(*args, **kwargs):
            pi_calls.append(kwargs.get("prompt", "")[:50])
            return {"extracted_text": "[]"}

        with (
            mock.patch.object(
                audit_runner, "_call_pi_and_maybe_log", side_effect=_pi,
            ),
            mock.patch(
                "code_review.scripts.code_quality.run_code_quality",
                return_value={"success": True, "findings": [], "fixes_applied": 0},
            ),
            mock.patch.object(
                audit_runner, "_resolve_audited_head", return_value=self._HEAD,
            ),
        ):
            rc = audit_runner.cmd_issue(
                "TEST-1", persist=False, force=True, runner=_make_runner("f" * 64),
            )
        assert rc == 0
        assert pi_calls, "--force must bypass the fresh-audit short-circuit"

