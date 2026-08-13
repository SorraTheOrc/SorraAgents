#!/usr/bin/env python3
"""Tests for cwd-independent wl invocation in audit_runner.py.

Covers the status-lifecycle step and all wl routing from a non-project cwd:

  - ``_run_wl`` injects a resolved ``--worklog-dir`` flag into every wl
    command when the runner's cwd is not the target project's root.
  - The status lifecycle (capture original status, set in_progress, restore)
    completes without RuntimeError from a non-project cwd.
  - A failing wl command whose error is a JSON error field on stdout (with
    empty stderr) raises a RuntimeError containing the real error text.
  - An explicitly provided ``--worklog-dir`` value overrides auto-resolution.
"""  # noqa: EXE001
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

# ---------------------------------------------------------------------------
# Ensure repo root is on sys.path so the audit_runner module is importable.
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from skill.audit.scripts import audit_runner

# ===========================================================================
# Helpers
# ===========================================================================


def _make_wl_success_proc() -> SimpleNamespace:
    """Build a canned success response for a wl subcommand."""
    return SimpleNamespace(
        returncode=0,
        stdout=json.dumps({"success": True}),
        stderr="",
    )


def _make_wl_failure_proc(returncode: int = 1,
                          stdout: str = "",
                          stderr: str = "") -> SimpleNamespace:
    """Build a canned failure response for a wl subcommand."""
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _make_recording_runner(recorded: list[list[str]]):
    """Build a fake runner that records every command and returns success."""
    def fake_runner(cmd):
        recorded.append(list(cmd))
        return _make_wl_success_proc()

    return fake_runner


def _make_sibling_projects(tmp_path: Path, prefix: str = "OSL") -> tuple[Path, mock.patch]:
    """Create a tmp projects dir with a sibling target project.

    Layout::

        <tmp>/projects/
            SorraAgents/.worklog/config.yaml      (prefix: SA)
            open_source_llm/.worklog/config.yaml  (prefix: OSL)

    Returns ``(target_worklog_dir, patcher)`` where *target_worklog_dir* is the
    target project's ``.worklog`` directory and *patcher* is a ``mock.patch``
    on the shared ``skill.shared.status_lifecycle.SIBLING_SCAN_ROOT`` constant
    (call ``patcher.start()`` to apply). The sibling scan base is patched (not
    ``TARGET_PROJECT_ROOT``) because the scan must resolve sibling projects
    relative to the framework repo root's parent, independent of the
    cwd-derived target root (SA-0MSG48MEI0083K82). Since the prefix-to-sibling
    scan was promoted into the shared module (SA-0MSG57UNY009DE51), the patch
    targets the shared constant, not ``audit_runner.SIBLING_SCAN_ROOT``.
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
    # Patch the SHARED SIBLING_SCAN_ROOT so the sibling scan finds the target
    # project (the shared module owns the scan; audit_runner delegates).
    patcher = mock.patch(
        "skill.shared.status_lifecycle.SIBLING_SCAN_ROOT", projects
    )
    return target, patcher


# ===========================================================================
# _run_wl flag injection (F1 AC1)
# ===========================================================================


class TestRunWlWorklogDirInjection:
    """Fake-runner tests: wl commands carry --worklog-dir from a non-project cwd."""

    def test_injects_worklog_dir_flag_pointing_at_target_worklog(self, tmp_path):
        """AC1: every wl command from a non-project cwd includes --worklog-dir
        pointing at the target project's .worklog (via prefix sibling scan).
        """
        target, patcher = _make_sibling_projects(tmp_path, prefix="OSL")
        recorded: list[list[str]] = []

        with patcher:
            audit_runner._run_wl(
                _make_recording_runner(recorded),
                ["wl", "show", "OSL-0MSABC7SB001NVUN", "--json"],
            )

        assert recorded, "fake runner should have received the command"
        cmd = recorded[0]
        assert cmd[0] == "wl"
        assert cmd[1] == "--worklog-dir"
        assert cmd[2] == str(target)
        assert "show" in cmd
        assert "OSL-0MSABC7SB001NVUN" in cmd

    def test_injects_worklog_dir_flag_for_update_command(self, tmp_path):
        """AC1: the lifecycle update command (set in_progress) also carries
        the --worklog-dir flag.
        """
        target, patcher = _make_sibling_projects(tmp_path, prefix="OSL")
        recorded: list[list[str]] = []

        with patcher:
            audit_runner._run_wl(
                _make_recording_runner(recorded),
                ["wl", "update", "OSL-0MSABC7SB001NVUN", "--status", "in_progress", "--json"],
            )

        cmd = recorded[0]
        assert cmd[1] == "--worklog-dir"
        assert cmd[2] == str(target)
        assert "--status" in cmd and "in_progress" in cmd

    def test_explicit_worklog_dir_overrides_auto_resolution(self, tmp_path):
        """AC4: an explicitly provided --worklog-dir value overrides any
        auto-resolution (sibling scan / cwd-chain).
        """
        _target, patcher = _make_sibling_projects(tmp_path, prefix="OSL")
        recorded: list[list[str]] = []

        with patcher:
            audit_runner._run_wl(
                _make_recording_runner(recorded),
                ["wl", "show", "OSL-0MSABC7SB001NVUN", "--json"],
                worklog_dir="/explicit/.worklog",
            )

        cmd = recorded[0]
        assert cmd[1] == "--worklog-dir"
        assert cmd[2] == "/explicit/.worklog"


# ===========================================================================
# Child-audit subprocess threading (F2 AC4)
# ===========================================================================


class TestChildAuditWorklogDirThreading:
    """The recursive child-audit subprocess passes resolved worklog flags."""

    def test_child_audit_subprocess_carries_resolved_worklog_dir(self, tmp_path):
        """AC4: when a child audit is auto-triggered, the spawned runner command
        includes the resolved --worklog-dir flags.

        Launched from the owning project root (the launch-context guard makes
        non-owning launches fatal — LP-0MSQ32HNR007AI6B).
        """
        target, patcher = _make_sibling_projects(tmp_path, prefix="OSL")
        recorded: list[list[str]] = []
        spawned: list[list[str]] = []

        def fake_runner(cmd):
            recorded.append(list(cmd))
            cmd_str = " ".join(cmd)
            if "audit-show" in cmd_str:
                # No prior audit -> full audit runs (freshness gate falls through).
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({"success": True, "audit": None}),
                    stderr="",
                )
            if "show" in cmd_str and "--children" not in cmd_str:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({
                        "success": True,
                        "workItem": {"id": "OSL-1", "status": "open",
                                     "stage": "in_review"},
                    }),
                    stderr="",
                )
            if "--children" in cmd_str:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({
                        "success": True,
                        "workItem": {"id": "OSL-1", "description": "",
                                     "status": "open", "stage": "in_review"},
                        "children": [
                            {"id": "OSL-2", "title": "Child", "status": "open",
                             "stage": "plan_complete", "description": ""},
                        ],
                    }),
                    stderr="",
                )
            if "update" in cmd_str:
                return _make_wl_success_proc()
            return _make_wl_success_proc()

        def fake_subprocess_run(cmd, *args, **kwargs):
            spawned.append(list(cmd))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with (
            patcher,
            mock.patch.object(
                audit_runner, "TARGET_PROJECT_ROOT", target.parent
            ),
            mock.patch("skill.code_review.scripts.code_quality.run_code_quality",
                       return_value={"success": True, "findings": [],
                                    "fixes_applied": 0}),
            mock.patch.object(audit_runner.subprocess, "run", fake_subprocess_run),
        ):
            audit_runner.cmd_issue(
                "OSL-1",
                persist=True,
                force=True,
                runner=fake_runner,
                timeout=30,
                audit_children=True,  # Cascade is opt-in (SA-0MSKB6V5Q007YDHE)
            )

        # The child-audit spawn is the audit_runner.py invocation (not the
        # persist_audit wl audit-set subprocess).
        runner_spawns = [c for c in spawned if any("audit_runner.py" in a for a in c)]
        assert runner_spawns, "expected a child-audit subprocess to be spawned"
        child_cmd = runner_spawns[0]
        assert "--worklog-dir" in child_cmd
        idx = child_cmd.index("--worklog-dir")
        assert child_cmd[idx + 1] == str(target)


class TestSiblingScanBaseCwdIndependence:
    """Regression tests for SA-0MSG48MEI0083K82.

    The prefix-to-project sibling scan must resolve sibling projects relative
    to the framework repo root's parent (``SIBLING_SCAN_ROOT``), NOT the
    import-time cwd-derived ``TARGET_PROJECT_ROOT.parent``. These tests
    simulate launching the runner from a non-project cwd (the skill install
    dir): ``TARGET_PROJECT_ROOT`` points at a wrong root that does NOT contain
    the target project, yet the scan must still inject the correct
    ``--worklog-dir`` for both the parent and its children.
    """

    def test_scan_base_is_framework_repo_parent(self):
        """AC2: the scan base is derived from the shared framework root's
        parent — cwd-independent and worktree-safe (SA-0MSG57UNY009DE51) —
        not from the import-time cwd-derived TARGET_PROJECT_ROOT.
        """
        from skill.shared.status_lifecycle import REPO_ROOT as SHARED_REPO_ROOT

        assert audit_runner.SIBLING_SCAN_ROOT == SHARED_REPO_ROOT.parent

    def test_resolves_from_non_project_cwd_when_target_root_is_wrong(self, tmp_path):
        """AC1/AC4: a wrong (cwd-derived) TARGET_PROJECT_ROOT must not break
        resolution when SIBLING_SCAN_ROOT holds the sibling projects.
        """
        target, patcher = _make_sibling_projects(tmp_path, prefix="OSL")
        # Simulate the skill-install-dir launch: the cwd's git root is NOT the
        # audited project and does not contain the sibling projects either.
        wrong_root = tmp_path / "skill-install-dir"
        wrong_root.mkdir()

        with patcher, mock.patch.object(audit_runner, "TARGET_PROJECT_ROOT",
                                        wrong_root):
            parent_flags = audit_runner._resolve_worklog_flags(
                ["wl", "show", "OSL-0MSABC7SB001NVUN", "--json"])
            child_flags = audit_runner._resolve_worklog_flags(
                ["wl", "update", "OSL-2", "--status", "in_progress", "--json"])

        assert parent_flags == ["--worklog-dir", str(target)]
        assert child_flags == ["--worklog-dir", str(target)]

    def test_run_wl_injects_dir_from_non_project_cwd(self, tmp_path):
        """AC3: every wl command (incl. child persistence) from a non-project
        cwd carries the resolved --worklog-dir for parent and children.
        """
        target, patcher = _make_sibling_projects(tmp_path, prefix="OSL")
        wrong_root = tmp_path / "skill-install-dir"
        wrong_root.mkdir()
        recorded: list[list[str]] = []

        with patcher, mock.patch.object(audit_runner, "TARGET_PROJECT_ROOT",
                                        wrong_root):
            audit_runner._run_wl(
                _make_recording_runner(recorded),
                ["wl", "show", "OSL-1", "--children", "--json"],
            )
            audit_runner._run_wl(
                _make_recording_runner(recorded),
                ["wl", "audit-set", "OSL-2", "--json"],
            )

        assert len(recorded) == 2
        for cmd in recorded:
            assert cmd[1] == "--worklog-dir"
            assert cmd[2] == str(target)


# ===========================================================================
# Status lifecycle from a non-project cwd (F1 AC2)
# ===========================================================================


class TestStatusLifecycleFromOwningProjectRoot:
    """Status lifecycle completes from the owning project root.

    The launch-context guard (LP-0MSQ32HNR007AI6B) makes launches from a
    non-owning cwd fatal, so the lifecycle is exercised from the owning
    project root — the wl commands still carry the resolved ``--worklog-dir``
    pointing at the owning project's store.
    """

    def _make_lifecycle_runner(self, recorded: list[list[str]]):
        """Fake runner handling the wl calls made by cmd_issue with an empty
        description and no children (no pi calls), recording every command.
        """
        def fake_runner(cmd):
            recorded.append(list(cmd))
            cmd_str = " ".join(cmd)

            # wl show <id> --json (capture original status)
            if "show" in cmd_str and "--children" not in cmd_str:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({
                        "success": True,
                        "workItem": {"id": "OSL-1", "status": "open"},
                    }),
                    stderr="",
                )
            # wl update <id> --status ... --json (in_progress + restore)
            if "update" in cmd_str:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({"success": True}),
                    stderr="",
                )
            # wl show <id> --children --json
            if "--children" in cmd_str:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({
                        "success": True,
                        "workItem": {"id": "OSL-1", "description": "", "status": "open"},
                        "children": [],
                    }),
                    stderr="",
                )
            return _make_wl_success_proc()

        return fake_runner

    def test_lifecycle_completes_and_commands_carry_worklog_dir(self, tmp_path):
        """AC2: capture -> in_progress -> terminal transition completes without
        RuntimeError from the owning project root, and every wl command
        carries --worklog-dir pointing at the target project's store.
        """
        target, patcher = _make_sibling_projects(tmp_path, prefix="OSL")
        recorded: list[list[str]] = []

        with (
            patcher,
            mock.patch.object(
                audit_runner, "TARGET_PROJECT_ROOT", target.parent
            ),
            mock.patch(
                "skill.code_review.scripts.code_quality.run_code_quality",
                return_value={"success": True, "findings": [], "fixes_applied": 0},
            ),
        ):
            rc = audit_runner.cmd_issue(
                "OSL-1",
                persist=False,
                force=True,  # Skip freshness gate
                runner=self._make_lifecycle_runner(recorded),
            )

        assert rc == 0
        wl_commands = [c for c in recorded if c and c[0] == "wl"]
        assert wl_commands, "expected wl commands to be recorded"
        for cmd in wl_commands:
            assert cmd[1] == "--worklog-dir", f"missing flag in {cmd}"
            assert cmd[2] == str(target), f"wrong target in {cmd}"

        # Lifecycle steps present: capture (wl show), set in_progress, and a
        # terminal transition (verdict-driven on current dev).
        statuses = [c[c.index("--status") + 1] for c in wl_commands
                    if "--status" in c]
        assert "in_progress" in statuses
        assert any(s in ("completed", "open") for s in statuses)


# ===========================================================================
# Error diagnostics (F1 AC3)
# ===========================================================================


class TestWlErrorDiagnostics:
    """Failing wl commands surface real error text instead of '(empty stderr)'."""

    def test_stdout_json_error_is_surfaced(self, tmp_path):
        """AC3: non-zero exit with error JSON on stdout and empty stderr raises
        a RuntimeError whose message contains the real error text.
        """
        _target, patcher = _make_sibling_projects(tmp_path, prefix="OSL")

        def failing_runner(cmd):
            return _make_wl_failure_proc(
                returncode=1,
                stdout=json.dumps({
                    "success": False,
                    "error": "Work item not found: OSL-0MSABC7SB001NVUN",
                }),
                stderr="",
            )

        with (
            patcher,
            mock.patch("builtins.print"),  # quiet test output
        ):
            try:
                audit_runner._run_wl(
                    failing_runner,
                    ["wl", "show", "OSL-0MSABC7SB001NVUN", "--json"],
                )
                raise AssertionError("expected RuntimeError")
            except RuntimeError as exc:
                message = str(exc)
                assert "Work item not found" in message
                assert "OSL-0MSABC7SB001NVUN" in message
                assert "(empty stderr)" not in message

    def test_plain_stdout_error_is_surfaced(self, tmp_path):
        """AC3: non-zero exit with plain-text error on stdout (empty stderr)
        surfaces the stdout text.
        """
        _target, patcher = _make_sibling_projects(tmp_path, prefix="OSL")

        def failing_runner(cmd):
            return _make_wl_failure_proc(
                returncode=1,
                stdout="Work item not found: OSL-0MSABC7SB001NVUN",
                stderr="",
            )

        with patcher:
            try:
                audit_runner._run_wl(
                    failing_runner,
                    ["wl", "show", "OSL-0MSABC7SB001NVUN", "--json"],
                )
                raise AssertionError("expected RuntimeError")
            except RuntimeError as exc:
                message = str(exc)
                assert "Work item not found" in message
                assert "(empty stderr)" not in message
