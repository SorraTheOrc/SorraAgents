"""Tests for cwd-independent wl invocation in persist_audit (SA-0MSKQERKH002IBLG).

The audit runner's READ path resolves ``--worklog-dir`` via the shared
prefix-to-sibling scan (SA-0MSG48MEI0083K82); the PERSIST path must resolve
the worklog store exactly the same way. Covers:

- AC2 (standalone CLI): ``persist_audit.py --issue-id <id>`` invoked from a
  non-project cwd WITHOUT ``--worklog-dir`` persists successfully to the
  item's own store; an explicit ``--worklog-dir`` still overrides.
- AC3 (runner path): ``persist_audit()`` from a non-owning cwd (worklog_dir
  None) targets the item's own store for every wl command — ``audit-set``,
  the priority-lowering ``wl show`` / ``wl update --priority`` calls
  (SA-0MSBRMXS800625RR), the stage fetch, and the ``--audit-text`` update —
  for parent AND child audits (``_persist_child_audit``).
- AC5 (precedence / fallback): explicit ``--worklog-dir`` beats
  auto-resolution; when nothing resolves, no flag is injected (wl resolves
  from cwd), preserving the ``_fail`` hook / return-code contract.

All tests use a fake runner and a patched ``SIBLING_SCAN_ROOT`` simulating a
non-project cwd — no real pi/wl calls (mirrors test_audit_runner_worklog_dir.py).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

# Ensure repo root is on sys.path so the modules are importable.
REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from skill.audit.scripts import audit_runner
from skill.audit.scripts.persist_audit import main as persist_main
from skill.audit.scripts.persist_audit import persist_audit

# ===========================================================================
# Helpers
# ===========================================================================


def _proc(returncode: int = 0, stdout: str = "", stderr: str = "") -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _make_sibling_projects(tmp_path: Path, prefix: str = "OSL") -> tuple[Path, mock.patch]:
    """Create a tmp projects dir with a sibling target project.

    Layout::

        <tmp>/projects/
            SorraAgents/.worklog/config.yaml      (prefix: SA)
            open_source_llm/.worklog/config.yaml  (prefix: OSL)

    Returns ``(target_worklog_dir, patcher)``; the patcher swaps the SHARED
    ``SIBLING_SCAN_ROOT`` (the scan base is derived from the shared module's
    own location, cwd-independent — SA-0MSG48MEI0083K82).
    """
    projects = tmp_path / "projects"
    framework = projects / "SorraAgents" / ".worklog"
    framework.mkdir(parents=True)
    (framework / "config.yaml").write_text(
        "projectName: Sorra Agents\nprefix: SA\n", encoding="utf-8"
    )
    target = projects / "open_source_llm" / ".worklog"
    target.mkdir(parents=True)
    (target / "config.yaml").write_text(
        f"projectName: Open Source LLM\nprefix: {prefix}\n", encoding="utf-8"
    )
    patcher = mock.patch(
        "skill.shared.status_lifecycle.SIBLING_SCAN_ROOT", projects
    )
    return target, patcher


def _make_recording_runner(recorded: list[list[str]], priority: str = "high",
                           stage: str = "plan_complete"):
    """Fake runner recording every command; answers persist_audit's wl calls.

    ``wl show`` returns a workItem carrying *priority* (so the priority
    lowering path is exercised when the report says 'Ready to close: Yes')
    and *stage* (so the stage-preserving ``--audit-text`` update works).
    """
    def fake_runner(cmd, **kwargs):
        recorded.append(list(cmd))
        cmd_str = " ".join(cmd)
        if "audit-set" in cmd_str:
            return _proc(stdout='{"success": true}')
        if "--audit-text" in cmd_str:
            return _proc(stdout='{"success": true}')
        if "--priority" in cmd_str:
            return _proc(stdout='{"success": true}')
        if "show" in cmd_str:
            return _proc(stdout=json.dumps({
                "success": True,
                "workItem": {"id": cmd[-3] if len(cmd) > 3 else "X",
                             "priority": priority, "stage": stage},
            }))
        return _proc(stdout='{"success": true}')

    return fake_runner


def _worklog_flags_of(cmd: list[str]) -> list[str]:
    """Extract the ``--worklog-dir <value>`` pair if injected at position 1."""
    if len(cmd) >= 3 and cmd[1] == "--worklog-dir":
        return ["--worklog-dir", cmd[2]]
    return []


# ===========================================================================
# AC2/AC3: auto-resolution from a non-project cwd
# ===========================================================================


class TestAutoResolutionFromNonProjectCwd:
    """persist_audit resolves the store via the shared prefix-to-sibling scan."""

    def test_all_wl_commands_carry_resolved_worklog_dir(self, tmp_path):
        """Every wl command — priority fetch/update, audit-set, stage fetch,
        --audit-text update — carries --worklog-dir pointing at the item's
        own store (simulated non-project cwd, worklog_dir None)."""
        target, patcher = _make_sibling_projects(tmp_path, prefix="OSL")
        recorded: list[list[str]] = []
        report = (
            "Audit of OSL-0MSABC7SB001NVUN\n"
            "Ready to close: Yes\n"
            "All ACs met.\n"
        )

        with patcher:
            rc = persist_audit(
                "OSL-0MSABC7SB001NVUN", report,
                runner=_make_recording_runner(recorded, priority="critical"),
            )

        assert rc == 0
        assert recorded, "expected persist_audit wl commands to be recorded"
        for cmd in recorded:
            assert cmd[0] == "wl", cmd
            assert _worklog_flags_of(cmd) == ["--worklog-dir", str(target)], cmd
        subcommands = [" ".join(c) for c in recorded]
        assert any("audit-set" in c for c in subcommands)
        assert any("--priority" in c for c in subcommands)  # critical → high
        assert any("--audit-text" in c for c in subcommands)

    def test_no_ready_to_close_skips_priority_calls_but_resolves_rest(self, tmp_path):
        """A 'Ready to close: No' report skips the priority-lowering calls;
        the remaining wl commands still carry the resolved store."""
        target, patcher = _make_sibling_projects(tmp_path, prefix="OSL")
        recorded: list[list[str]] = []
        report = (
            "Audit of OSL-0MSABC7SB001NVUN\n"
            "Ready to close: No\n"
            "Work remains.\n"
        )

        with patcher:
            rc = persist_audit(
                "OSL-0MSABC7SB001NVUN", report,
                runner=_make_recording_runner(recorded, priority="critical"),
            )

        assert rc == 0
        assert not any("--priority" in " ".join(c) for c in recorded)
        for cmd in recorded:
            assert _worklog_flags_of(cmd) == ["--worklog-dir", str(target)]

    def test_explicit_worklog_dir_overrides_auto_resolution(self, tmp_path):
        """AC2/AC5: an explicit worklog_dir beats the sibling-scan target."""
        target, patcher = _make_sibling_projects(tmp_path, prefix="OSL")
        recorded: list[list[str]] = []
        report = (
            "Audit of OSL-0MSABC7SB001NVUN\n"
            "Ready to close: Yes\n"
            "All ACs met.\n"
        )

        with patcher:
            rc = persist_audit(
                "OSL-0MSABC7SB001NVUN", report,
                runner=_make_recording_runner(recorded, priority="critical"),
                worklog_dir="/explicit/.worklog",
            )

        assert rc == 0
        assert recorded
        for cmd in recorded:
            assert _worklog_flags_of(cmd) == ["--worklog-dir", "/explicit/.worklog"], cmd
        # The sibling target must never leak into an explicitly-dir'd run.
        assert not any(str(target) in " ".join(c) for c in recorded)

    def test_no_flag_when_nothing_resolves(self, tmp_path, monkeypatch):
        """AC5 fallback: when the prefix scan finds no sibling and the cwd
        chain is suppressed (simulating an initialized worklog cwd / unknown
        prefix), NO flag is injected — wl resolves from cwd (existing
        behaviour preserved)."""
        recorded: list[list[str]] = []
        report = (
            "Audit of XX-UNKNOWN123\n"
            "Ready to close: No\n"
        )

        with mock.patch(
            "skill.shared.status_lifecycle.SIBLING_SCAN_ROOT",
            tmp_path / "empty-projects",
        ), mock.patch(
            "skill.shared.status_lifecycle.worklog_dir_flag", return_value=[]
        ):
            rc = persist_audit(
                "XX-UNKNOWN123", report,
                runner=_make_recording_runner(recorded),
            )

        assert rc == 0
        assert recorded
        for cmd in recorded:
            assert _worklog_flags_of(cmd) == [], cmd
        assert any("audit-set" in " ".join(c) for c in recorded)


# ===========================================================================
# AC2: standalone CLI (main / build_parser)
# ===========================================================================


class TestStandaloneCLI:
    """persist_audit.py --issue-id <id> from a non-project cwd persists
    successfully to the item's own store."""

    def test_cli_auto_resolves_store_from_non_project_cwd(self, tmp_path):
        """The CLI (no --worklog-dir) resolves the store via the shared
        prefix-to-sibling scan; every wl command carries the resolved flag."""
        target, patcher = _make_sibling_projects(tmp_path, prefix="OSL")
        recorded: list[list[str]] = []

        def fake_subprocess_run(cmd, *args, **kwargs):
            recorded.append(list(cmd))
            cmd_str = " ".join(cmd)
            if "show" in cmd_str:
                return _proc(stdout=json.dumps({
                    "success": True,
                    "workItem": {"id": cmd[-3], "priority": "high",
                                 "stage": "plan_complete"},
                }))
            return _proc(stdout='{"success": true}')

        with patcher, mock.patch(
            "skill.audit.scripts.persist_audit.subprocess.run",
            fake_subprocess_run,
        ):
            rc = persist_main([
                "--issue-id", "OSL-0MSABC7SB001NVUN",
                "--report", "Audit of OSL-0MSABC7SB001NVUN\nReady to close: Yes",
            ])

        assert rc == 0
        assert recorded
        for cmd in recorded:
            assert _worklog_flags_of(cmd) == ["--worklog-dir", str(target)], cmd

    def test_cli_explicit_worklog_dir_overrides(self, tmp_path):
        """AC2: an explicit --worklog-dir CLI flag overrides auto-resolution."""
        _target, patcher = _make_sibling_projects(tmp_path, prefix="OSL")
        recorded: list[list[str]] = []

        def fake_subprocess_run(cmd, *args, **kwargs):
            recorded.append(list(cmd))
            return _proc(stdout='{"success": true}')

        with patcher, mock.patch(
            "skill.audit.scripts.persist_audit.subprocess.run",
            fake_subprocess_run,
        ):
            rc = persist_main([
                "--issue-id", "OSL-0MSABC7SB001NVUN",
                "--report", "Audit of OSL-0MSABC7SB001NVUN\nReady to close: No",
                "--worklog-dir", "/explicit/.worklog",
            ])

        assert rc == 0
        assert recorded
        for cmd in recorded:
            assert _worklog_flags_of(cmd) == ["--worklog-dir", "/explicit/.worklog"], cmd


# ===========================================================================
# AC3: child-audit persistence (runner path)
# ===========================================================================


class TestChildAuditPersistence:
    """_persist_child_audit from a non-project cwd (worklog_dir None) targets
    the child's own store."""

    def test_child_persist_commands_carry_resolved_worklog_dir(self, tmp_path):
        """The child-audit persist path resolves the store via the shared
        prefix-to-sibling scan for the child's own id."""
        target, patcher = _make_sibling_projects(tmp_path, prefix="OSL")
        recorded: list[list[str]] = []
        ac_results = [{"text": "AC1", "verdict": "met", "evidence": "file.py:1"}]

        def fake_subprocess_run(cmd, *args, **kwargs):
            recorded.append(list(cmd))
            cmd_str = " ".join(cmd)
            if "show" in cmd_str:
                return _proc(stdout=json.dumps({
                    "success": True,
                    "workItem": {"id": cmd[-3], "priority": "high",
                                 "stage": "plan_complete"},
                }))
            return _proc(stdout='{"success": true}')

        with patcher, mock.patch(
            "skill.audit.scripts.persist_audit.subprocess.run",
            fake_subprocess_run,
        ):
            rc, report = audit_runner._persist_child_audit(
                "OSL-2", "Child item", "open", "plan_complete",
                ac_results, worklog_dir=None,
            )

        assert rc == 0
        assert "OSL-2" in report  # identity guard satisfiable
        assert recorded
        for cmd in recorded:
            assert _worklog_flags_of(cmd) == ["--worklog-dir", str(target)], cmd
        assert any("audit-set" in " ".join(c) for c in recorded)

    def test_child_persist_explicit_dir_override(self, tmp_path):
        """A runner passing an explicit worklog_dir down to the child persist
        path keeps it (highest precedence)."""
        _target, patcher = _make_sibling_projects(tmp_path, prefix="OSL")
        recorded: list[list[str]] = []
        ac_results = [{"text": "AC1", "verdict": "met", "evidence": "file.py:1"}]

        def fake_subprocess_run(cmd, *args, **kwargs):
            recorded.append(list(cmd))
            return _proc(stdout='{"success": true}')

        with patcher, mock.patch(
            "skill.audit.scripts.persist_audit.subprocess.run",
            fake_subprocess_run,
        ):
            rc, _report = audit_runner._persist_child_audit(
                "OSL-2", "Child item", "open", "plan_complete",
                ac_results, worklog_dir="/explicit/.worklog",
            )

        assert rc == 0
        assert recorded
        for cmd in recorded:
            assert _worklog_flags_of(cmd) == ["--worklog-dir", "/explicit/.worklog"], cmd
