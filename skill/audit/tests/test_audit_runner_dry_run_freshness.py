"""Dry-run (``--do-not-persist``) freshness contract tests.

Work item: SA-0MTJ0KO6L004GIZK.

A ``--do-not-persist`` audit is a dry run for the *report*: the full
``rawOutput`` markdown must not be stored. However, a ``Ready to close: Yes``
verdict must still refresh the audit freshness signal (``auditedAt`` /
``auditResult``) so Herdr/DOWNTIME stop re-queuing the item as a stale-audit
candidate. A non-Yes verdict must not bump ``auditedAt`` (fail-closed).

Covered here:

* ``persist_audit_freshness`` builds an atomic ``wl audit-set --ready-to-close
  yes`` command WITHOUT ``--raw-output``/``--audit-file`` and forwards the
  content fingerprint.
* ``_apply_terminal_lifecycle`` on a dry-run Yes refreshes freshness as the
  last write (``auditedAt == updatedAt``, ``rawOutput`` stays null).
* ``_apply_terminal_lifecycle`` on a dry-run No does not touch the stored
  audit.
* The ``--do-not-persist`` help on both ``issue`` and ``batch`` documents the
  contract, and SKILL.md carries the same wording.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from audit.scripts import audit_runner, persist_audit

WORKTREE_SKILL_MD = Path(__file__).resolve().parents[1] / "SKILL.md"


def _proc(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


# ---------------------------------------------------------------------------
# persist_audit_freshness unit tests
# ---------------------------------------------------------------------------


class TestPersistAuditFreshness:
    """The helper that refreshes freshness without storing the full report."""

    def test_builds_audit_set_without_raw_output(self):
        """AC1/AC4(a): no --raw-output/--audit-file; fingerprint forwarded."""
        calls: list[list[str]] = []

        def runner(cmd, **_kwargs):
            calls.append(list(cmd))
            return _proc(0, json.dumps({"success": True}))

        rc = persist_audit.persist_audit_freshness(
            "TEST-1",
            runner=runner,
            fingerprint="a" * 64,
        )

        assert rc == 0
        assert len(calls) == 1
        cmd = calls[0]
        cmd_str = " ".join(cmd)
        assert "audit-set" in cmd_str
        assert "--ready-to-close" in cmd
        assert cmd[cmd.index("--ready-to-close") + 1] == "yes"
        assert "--raw-output" not in cmd
        assert "--audit-file" not in cmd
        assert cmd[cmd.index("--fingerprint") + 1] == "a" * 64
        assert cmd[-1] == "--json"

    def test_omits_fingerprint_when_absent(self):
        """No fingerprint available → command has no --fingerprint."""
        calls: list[list[str]] = []

        def runner(cmd, **_kwargs):
            calls.append(list(cmd))
            return _proc(0, json.dumps({"success": True}))

        rc = persist_audit.persist_audit_freshness(
            "TEST-1", runner=runner, fingerprint=None,
        )

        assert rc == 0
        assert "--fingerprint" not in calls[0]

    def test_fail_closed_on_nonzero_exit(self):
        """A failed wl audit-set is surfaced (non-zero), never a silent pass."""

        def runner(_cmd, **_kwargs):
            return _proc(2, "", "wl exploded")

        rc = persist_audit.persist_audit_freshness("TEST-1", runner=runner)
        assert rc == 2

    def test_fail_closed_on_success_false(self):
        """An explicit ``success: false`` payload is a failure."""

        def runner(_cmd, **_kwargs):
            return _proc(0, json.dumps({"success": False, "error": "nope"}))

        rc = persist_audit.persist_audit_freshness("TEST-1", runner=runner)
        assert rc == 1


# ---------------------------------------------------------------------------
# Stateful wl double modelling the audit_results + updatedAt contract
# ---------------------------------------------------------------------------


class _DryRunWlStore:
    """Models the parts of wl the lifecycle + refresh touch.

    ``wl update --status/--stage`` bumps ``updatedAt`` only when a tracked
    status/stage value actually changes (matching worklog's content-change
    rule). ``wl audit-set`` atomically sets ``auditedAt`` and
    ``updatedAt = auditedAt`` and stores ``rawOutput`` from the flag (null
    when omitted).
    """

    def __init__(self, *, status: str = "completed", stage: str = "in_review"):
        self.status = status
        self.stage = stage
        self.updated_at = "2020-01-01T00:00:00.000Z"
        self.audited_at: str | None = "2020-01-01T00:00:00.000Z"
        self.raw_output: str | None = "old stored report"
        self.ready_to_close: bool | None = False
        self.now = "2026-09-26T12:00:00.000Z"
        self.calls: list[list[str]] = []
        self.audit_set_calls: list[list[str]] = []

    @staticmethod
    def _value(cmd: list[str], flag: str) -> str | None:
        return cmd[cmd.index(flag) + 1] if flag in cmd else None

    def __call__(self, cmd, **_kwargs):
        self.calls.append(list(cmd))
        cs = " ".join(cmd)

        if "audit-set" in cs:
            self.audit_set_calls.append(list(cmd))
            self.ready_to_close = self._value(cmd, "--ready-to-close") == "yes"
            self.raw_output = self._value(cmd, "--raw-output")
            self.audited_at = self.now
            self.updated_at = self.now
            return _proc(0, json.dumps({"success": True}))

        if "audit-show" in cs:
            audit = None
            if self.audited_at is not None:
                audit = {
                    "auditedAt": self.audited_at,
                    "rawOutput": self.raw_output,
                    "readyToClose": self.ready_to_close,
                }
            return _proc(0, json.dumps({"success": True, "audit": audit}))

        if "update" in cs:
            new_status = self._value(cmd, "--status") or self.status
            new_stage = self._value(cmd, "--stage")
            content_changed = new_status != self.status or (
                new_stage is not None and new_stage != self.stage
            )
            self.status = new_status
            if new_stage is not None:
                self.stage = new_stage
            if content_changed:
                self.updated_at = "2026-09-26T11:59:00.000Z"
            return _proc(0, json.dumps({"success": True}))

        if "show" in cs and "--children" not in cs:
            return _proc(0, json.dumps({
                "success": True,
                "workItem": {
                    "id": "TEST-1",
                    "status": self.status,
                    "stage": self.stage,
                    "parentId": None,
                },
            }))

        return _proc(0, json.dumps({"success": True}))


def _make_ctx(store: _DryRunWlStore, **overrides) -> audit_runner._AuditContext:
    defaults = {
        "issue_id": "TEST-1",
        "persist": False,
        "timeout": None,
        "parent_timeout": None,
        "pi_bin": "pi",
        "model": None,
        "model_source": "default",
        "runner": store,
        "json_mode": False,
        "debug_log": None,
        "force": False,
        "worklog_dir": None,
        "batch_phase2": False,
        "green_run": None,
        "audit_children": False,
        "max_child_audits": None,
        "run_tests": False,
    }
    defaults.update(overrides)
    return audit_runner._AuditContext(**defaults)


class TestDryRunLifecycleFreshness:
    """``_apply_terminal_lifecycle`` refreshes freshness on a dry-run Yes."""

    def test_yes_dry_run_refreshes_audited_at_and_keeps_raw_output_null(self):
        """AC1/AC4(a): auditedAt == updatedAt, auditResult Yes, rawOutput null."""
        store = _DryRunWlStore(status="completed", stage="in_review")
        ctx = _make_ctx(
            store,
            persist=False,
            audit_verdict="yes",
            audit_completed=True,
            content_fingerprint="f" * 64,
            original_status="completed",
            original_stage="in_review",
        )

        rc = audit_runner._apply_terminal_lifecycle(ctx)

        assert rc == 0
        assert store.audit_set_calls, "expected a freshness refresh"
        cmd = store.audit_set_calls[-1]
        assert "--raw-output" not in cmd and "--audit-file" not in cmd
        assert store.raw_output is None
        assert store.ready_to_close is True
        assert store.audited_at == store.updated_at == store.now
        assert cmd[cmd.index("--fingerprint") + 1] == "f" * 64

    def test_yes_dry_run_refreshes_after_transition(self):
        """AC1: refresh is the LAST write even when the item transitions.

        Starting from ``open``/``plan_complete`` the terminal update bumps
        ``updatedAt``; the fidelity of the fix depends on the audit-set
        happening afterwards so ``auditedAt == updatedAt`` still holds.
        """
        store = _DryRunWlStore(status="open", stage="plan_complete")
        ctx = _make_ctx(
            store,
            persist=False,
            audit_verdict="yes",
            audit_completed=True,
            content_fingerprint="f" * 64,
            original_status="open",
            original_stage="plan_complete",
        )

        rc = audit_runner._apply_terminal_lifecycle(ctx)

        assert rc == 0
        # Transition applied AND freshness refreshed afterwards.
        assert store.status == "completed" and store.stage == "in_review"
        assert store.audited_at == store.updated_at == store.now

    def test_no_dry_run_does_not_bump_audited_at(self):
        """AC1/AC4(b): a No verdict is fail-closed — stored audit untouched."""
        store = _DryRunWlStore(status="completed", stage="in_review")
        before = (store.audited_at, store.raw_output, store.ready_to_close)
        ctx = _make_ctx(
            store,
            persist=False,
            audit_verdict="no",
            audit_completed=True,
            content_fingerprint="f" * 64,
            original_status="completed",
            original_stage="in_review",
        )

        rc = audit_runner._apply_terminal_lifecycle(ctx)

        assert rc == 0
        assert store.audit_set_calls == []
        # ``updatedAt`` may legitimately move for the demotion, but the stored
        # audit (auditedAt / rawOutput / verdict) must NOT be touched.
        assert (store.audited_at, store.raw_output, store.ready_to_close) == before

    def test_persist_true_does_not_refresh_from_lifecycle(self):
        """AC3: the normal persist path owns persistence — no extra audit-set.

        With ``persist=True`` the freshness is set by ``persist_audit`` in
        ``_phase_report``; the lifecycle must not issue a second (rawOutput-less)
        audit-set that would clobber the full report's fingerprint.
        """
        store = _DryRunWlStore(status="completed", stage="in_review")
        ctx = _make_ctx(
            store,
            persist=True,
            audit_verdict="yes",
            audit_completed=True,
            content_fingerprint="f" * 64,
            original_status="completed",
            original_stage="in_review",
        )

        rc = audit_runner._apply_terminal_lifecycle(ctx)

        assert rc == 0
        assert store.audit_set_calls == []

    def test_refresh_failure_is_surfaced(self):
        """A failed refresh exits non-zero rather than claiming freshness."""
        class _FailingStore(_DryRunWlStore):
            def __call__(self, cmd, **kwargs):
                if "audit-set" in " ".join(cmd):
                    self.audit_set_calls.append(list(cmd))
                    return _proc(3, "", "audit-set failed")
                return super().__call__(cmd, **kwargs)

        store = _FailingStore(status="completed", stage="in_review")
        ctx = _make_ctx(
            store,
            persist=False,
            audit_verdict="yes",
            audit_completed=True,
            original_status="completed",
            original_stage="in_review",
        )

        rc = audit_runner._apply_terminal_lifecycle(ctx)
        assert rc == 3


# ---------------------------------------------------------------------------
# Help text + SKILL.md documentation (AC2 / AC4(c))
# ---------------------------------------------------------------------------


def _flag_help(parser: argparse.ArgumentParser, command: str, flag: str) -> str:
    """Return the help string for *flag* on subparser *command*."""
    for action in parser._subparsers._group_actions:  # type: ignore[attr-defined]
        sub = action.choices.get(command)  # type: ignore[union-attr]
        if sub is None:
            continue
        for sub_action in sub._actions:
            if flag in sub_action.option_strings:
                return sub_action.help or ""
    raise AssertionError(f"flag {flag!r} not found on {command!r}")


class TestDryRunHelpText:
    """AC2/AC4(c): the flag help documents the freshness contract."""

    def test_issue_help_mentions_freshness_and_dry_run(self):
        parser = audit_runner.build_parser()
        help_text = _flag_help(parser, "issue", "--do-not-persist").lower()
        assert "dry run" in help_text
        assert "fresh" in help_text
        assert "auditedat" in help_text
        assert "full report" in help_text or "full audit report" in help_text

    def test_batch_help_mentions_freshness_and_dry_run(self):
        parser = audit_runner.build_parser()
        help_text = _flag_help(parser, "batch", "--do-not-persist").lower()
        assert "dry run" in help_text
        assert "fresh" in help_text
        assert "auditedat" in help_text
        assert "raw" not in help_text or "freshness" in help_text

    def test_skill_md_documents_dry_run_freshness(self):
        text = WORKTREE_SKILL_MD.read_text()
        assert "Dry-run freshness" in text
        assert "auditedAt" in text
        assert "rawOutput" in text
