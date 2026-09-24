"""Tests for persist_audit critical→high priority lowering.

Covers (SA-0MSBRMXS800625RR):

- AC1: When ``persist_audit()`` persists a report containing
  ``Ready to close: Yes`` for a work item whose priority is ``critical``,
  the priority is lowered to ``high`` *before* the audit is persisted.
- AC2: When the report says ``Ready to close: No``, priority is NOT changed.
- AC3: When priority is already ``high`` or lower, no change is made
  (idempotent for non-critical items).
- AC4: The priority change is applied via ``wl update --priority high``
  before the ``wl audit-set`` call.
- AC5: Both parent-level and child-level audit paths are covered (the
  single ``persist_audit()`` function handles both).
- Best-effort: a failed priority fetch/update never blocks audit
  persistence (the audit still persists; rc stays 0).
- ``worklog_dir`` is respected for both the priority fetch and the
  priority update commands.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

# Ensure repo root is on sys.path so the persist_audit module is importable.
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from audit.scripts.persist_audit import persist_audit

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _proc(returncode: int = 0, stdout: str = "", stderr: str = "") -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _strip_worklog_flags(cmd: list[str]) -> list[str]:
    """Drop an injected ``--worklog-dir <value>`` pair (position 1).

    Since SA-0MSKQERKH002IBLG every wl command built by persist_audit
    carries the resolved ``--worklog-dir`` flag (from the shared
    prefix-to-sibling scan / cwd-chain); assertions on the wl subcommand
    tokens must ignore it.
    """
    cmd = list(cmd)
    if len(cmd) >= 3 and cmd[1] == "--worklog-dir":
        del cmd[1:3]
    return cmd


def _make_priority_runner(recorded: list[list[str]], priority: str = "critical",
                          show_rc: int = 0, prio_update_rc: int = 0):
    """Build a recording runner simulating wl calls made by persist_audit.

    * ``wl show <id> --json`` returns a workItem with the configured
      ``priority`` (and a stage, so the stage-preservation path is sane).
    * ``wl update <id> --priority high --json`` returns ``prio_update_rc``.
    * ``wl audit-set`` / ``wl update --audit-text`` return success.
    """
    def fake_runner(cmd, **kwargs):
        recorded.append(list(cmd))
        cmd_str = " ".join(cmd)
        if "audit-set" in cmd_str:
            return _proc(stdout='{"success": true}')
        if "--audit-text" in cmd_str:
            return _proc(stdout='{"success": true}')
        if "--priority" in cmd_str:
            return _proc(returncode=prio_update_rc, stdout='{"success": true}')
        if "show" in cmd_str:
            return _proc(
                returncode=show_rc,
                stdout=json.dumps({
                    "success": True,
                    "workItem": {
                        "id": cmd[2],
                        "priority": priority,
                        "stage": "in_progress",
                    },
                }),
            )
        return _proc(stdout='{"success": true}')

    return fake_runner


# ---------------------------------------------------------------------------
# AC1/AC4: Ready to close: Yes + critical → lowered to high before audit-set
# ---------------------------------------------------------------------------

class TestLowerCriticalPriorityWhenReadyToClose:
    """AC1/AC4: priority lowered before the audit is persisted."""

    def test_ready_yes_critical_lowers_priority(self):
        """A report saying 'Ready to close: Yes' for a critical item must
        trigger ``wl update <id> --priority high --json``."""
        calls: list[list[str]] = []
        runner = _make_priority_runner(calls, priority="critical")

        rc = persist_audit(
            "SA-TEST",
            "Audit of SA-TEST\nReady to close: Yes\nAll ACs met.",
            runner=runner,
        )

        assert rc == 0
        prio_calls = [c for c in calls if "--priority" in c]
        assert len(prio_calls) == 1
        assert _strip_worklog_flags(prio_calls[0])[:4] == ["wl", "update", "SA-TEST", "--priority"]
        assert _strip_worklog_flags(prio_calls[0])[4] == "high"
        assert "--json" in prio_calls[0]

    def test_priority_update_happens_before_audit_set(self):
        """AC4: the ``wl update --priority high`` call must precede the
        ``wl audit-set`` call."""
        calls: list[list[str]] = []
        runner = _make_priority_runner(calls, priority="critical")

        rc = persist_audit(
            "SA-TEST",
            "Audit of SA-TEST\nReady to close: Yes\nAll ACs met.",
            runner=runner,
        )

        assert rc == 0
        prio_idx = next(i for i, c in enumerate(calls) if "--priority" in c)
        audit_set_idx = next(i for i, c in enumerate(calls) if "audit-set" in c)
        assert prio_idx < audit_set_idx


# ---------------------------------------------------------------------------
# AC2: Ready to close: No → priority NOT changed
# ---------------------------------------------------------------------------

class TestKeepPriorityWhenNotReadyToClose:
    """AC2: a 'Ready to close: No' verdict must not change priority."""

    def test_ready_no_keeps_priority(self):
        calls: list[list[str]] = []
        runner = _make_priority_runner(calls, priority="critical")

        rc = persist_audit(
            "SA-TEST",
            "Audit of SA-TEST\nReady to close: No\nStill work to do.",
            runner=runner,
        )

        assert rc == 0
        assert not any("--priority" in c for c in calls)


# ---------------------------------------------------------------------------
# AC3: already high/lower → no change (idempotent)
# ---------------------------------------------------------------------------

class TestIdempotentForNonCritical:
    """AC3: non-critical items must not be touched."""

    def test_already_high_is_unchanged(self):
        calls: list[list[str]] = []
        runner = _make_priority_runner(calls, priority="high")

        rc = persist_audit(
            "SA-TEST",
            "Audit of SA-TEST\nReady to close: Yes\nAll ACs met.",
            runner=runner,
        )

        assert rc == 0
        assert not any("--priority" in c for c in calls)

    def test_medium_is_unchanged(self):
        calls: list[list[str]] = []
        runner = _make_priority_runner(calls, priority="medium")

        rc = persist_audit(
            "SA-TEST",
            "Audit of SA-TEST\nReady to close: Yes\nAll ACs met.",
            runner=runner,
        )

        assert rc == 0
        assert not any("--priority" in c for c in calls)


# ---------------------------------------------------------------------------
# AC5: child-level audit path uses the same persist_audit entry point
# ---------------------------------------------------------------------------

class TestChildAuditPath:
    """AC5: child audits flow through the same persist_audit function."""

    def test_child_audit_lowers_critical_priority(self):
        calls: list[list[str]] = []
        runner = _make_priority_runner(calls, priority="critical")

        rc = persist_audit(
            "SA-CHILD",
            "Audit of SA-CHILD\nReady to close: Yes\nAll ACs met.",
            runner=runner,
        )

        assert rc == 0
        prio_calls = [c for c in calls if "--priority" in c]
        assert len(prio_calls) == 1
        stripped = _strip_worklog_flags(prio_calls[0])
        assert stripped[:4] == ["wl", "update", "SA-CHILD", "--priority"]
        assert stripped[4] == "high"


# ---------------------------------------------------------------------------
# Best-effort: failures must not block audit persistence
# ---------------------------------------------------------------------------

class TestBestEffortPriorityAdjustment:
    """A failed priority fetch/update must never block the audit."""

    def test_priority_update_failure_still_persists(self, capsys):
        """AC constraint: if ``wl update --priority high`` fails, the audit
        must still persist (rc 0) and a warning is printed."""
        calls: list[list[str]] = []
        runner = _make_priority_runner(calls, priority="critical",
                                       prio_update_rc=1)

        rc = persist_audit(
            "SA-TEST",
            "Audit of SA-TEST\nReady to close: Yes\nAll ACs met.",
            runner=runner,
        )

        assert rc == 0  # audit persisted despite the priority-update failure
        assert any("audit-set" in c for c in calls)
        err = capsys.readouterr().err
        assert "priority" in err.lower()

    def test_priority_fetch_failure_still_persists(self, capsys):
        """If ``wl show`` (priority fetch) fails, no update is attempted but
        the audit still persists (rc 0)."""
        calls: list[list[str]] = []
        runner = _make_priority_runner(calls, priority="critical", show_rc=1)

        rc = persist_audit(
            "SA-TEST",
            "Audit of SA-TEST\nReady to close: Yes\nAll ACs met.",
            runner=runner,
        )

        assert rc == 0
        assert not any("--priority" in c for c in calls)
        assert any("audit-set" in c for c in calls)
        err = capsys.readouterr().err
        assert err  # a diagnostic was printed


# ---------------------------------------------------------------------------
# worklog_dir is respected for both priority fetch and update
# ---------------------------------------------------------------------------

class TestWorklogDirRespected:
    """The priority fetch/update commands must carry ``--worklog-dir``."""

    def test_priority_fetch_and_update_carry_worklog_dir(self):
        calls: list[list[str]] = []
        runner = _make_priority_runner(calls, priority="critical")

        rc = persist_audit(
            "SA-TEST",
            "Audit of SA-TEST\nReady to close: Yes\nAll ACs met.",
            runner=runner,
            worklog_dir="/tmp/proj/.worklog",
        )

        assert rc == 0
        prio_calls = [c for c in calls if "--priority" in c]
        assert len(prio_calls) == 1
        assert prio_calls[0][1:3] == ["--worklog-dir", "/tmp/proj/.worklog"]
        # The priority fetch (wl show) also carries --worklog-dir.
        show_calls = [c for c in calls if "show" in c and "audit-set" not in " ".join(c)]
        assert show_calls, "expected a priority-fetch wl show call"
        assert all(c[1:3] == ["--worklog-dir", "/tmp/proj/.worklog"]
                   for c in show_calls)
