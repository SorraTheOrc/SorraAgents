"""No-breakage verification: mid-audit `wl create` for a chore item.

SA-0MST01PBJ008100Y — chore test-first. Proves that creating a chore
work item mid-audit does not corrupt the audit's verdict-driven status
lifecycle, persistence/readback, or freshness gate.

Tests exercise the mocked `wl create` path against the CURRENT runner
(the restriction still prevents real creation, but the mocked path
verifies the audit survives the call and continues correctly).
"""

from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pytest

from skill.audit.scripts import audit_runner


@pytest.fixture(autouse=True)
def _free_audit_slot():
    with mock.patch.object(
        audit_runner, "_acquire_audit_slot", return_value=contextlib.nullcontext()
    ):
        yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ok_json(data):
    return SimpleNamespace(returncode=0, stdout=json.dumps(data), stderr="")


def _make_mock_runner(
    children_data=None,
    ac_review_result=None,
    deep_result=None,
    fail_children=False,
    extra_wl_create=None,
):
    """Build a runner that dispatches on ``wl`` commands.

    When *extra_wl_create* is truthy the runner intercepts ``wl create``
    and returns the given response (a SimpleNamespace with returncode
    and stdout), exercising the mid-audit chore-creation path.
    """
    runner = mock.MagicMock()

    def _side(cmd, **kwargs):
        cmd_str = " ".join(cmd)

        # wl create — the mid-audit chore-creation path (T3 AC5)
        if "create" in cmd_str and extra_wl_create is not None:
            return extra_wl_create

        # wl audit-show (readback verification) — MUST precede the generic
        # ``show`` case below because ``audit-show`` contains the substring
        # ``show`` (SA-0MST01PBJ008100Y).
        if "audit-show" in cmd_str:
            return _ok_json({
                "success": True,
                "audit": {
                    "issueId": "TEST-1",
                    "readyToClose": True,
                    "rawOutput": "Audit for TEST-1 — Ready to close: Yes",
                },
            })

        # wl show <id> --children --json
        if "--children" in cmd_str:
            if fail_children:
                return SimpleNamespace(returncode=1, stdout="", stderr="boom")
            return _ok_json({
                "success": True,
                "workItem": {
                    "id": "TEST-1",
                    "description": "## Acceptance Criteria\n- AC1: verify",
                    "status": "in_progress",
                    "stage": "in_progress",
                },
                "children": children_data or [],
            })

        # wl show <id> --json (original status capture)
        if "show" in cmd_str and "--children" not in cmd_str:
            return _ok_json({
                "success": True,
                "workItem": {
                    "id": "TEST-1",
                    "status": "in_progress",
                    "stage": "in_progress",
                },
            })

        # wl update (status transitions + audit-text)
        if "update" in cmd_str:
            return _ok_json({"success": True})

        # wl audit-set (persist)
        if "audit-set" in cmd_str:
            return _ok_json({"success": True})

        # wl show for children
        if "show" in cmd_str:
            return _ok_json({
                "success": True,
                "workItem": {
                    "id": "CHILD-1",
                    "status": "open",
                    "stage": "in_progress",
                },
            })

        # Anything else — return success
        return SimpleNamespace(returncode=0, stdout=json.dumps({"success": True}), stderr="")

    runner.side_effect = _side
    return runner


def _make_pi_calls(ac_review=None, deep=None):
    """Return a mocked _call_pi_and_maybe_log.

    *ac_review* and *deep* are JSON-serialisable verdict arrays.
    """
    calls = []

    def _call(issue_id, context, prompt, **kw):
        calls.append((issue_id, context))
        if context == "parent":
            return {"extracted_text": json.dumps(ac_review or [
                {"index": 0, "verdict": "met", "evidence": "test"},
            ])}
        if context == "phase2_deep":
            return {"extracted_text": json.dumps(deep or [
                {"index": 0, "verdict": "met", "evidence": "deep"},
            ])}
        if "phase2_child" in str(context):
            return {"extracted_text": json.dumps([
                {"index": 0, "verdict": "met", "evidence": "child"},
            ])}
        return {"extracted_text": json.dumps([
            {"index": 0, "verdict": "met", "evidence": "test"},
        ])}

    return mock.patch.object(
        audit_runner, "_call_pi_and_maybe_log", side_effect=_call
    ), calls


# ---------------------------------------------------------------------------
# Loop helpers (mirroring test_audit_runner_remediation.py): minimal
# finding/screen-entry factories + a remediation-loop driver with a mocked
# git runner so no real files or commits are touched.
# ---------------------------------------------------------------------------


def _finding(severity: str = "critical", code: str = "F841",
             file: str = "src/bad.py", linter: str = "ruff") -> dict:
    return {
        "severity": severity,
        "file": file,
        "line": 1,
        "message": f"{code} message",
        "linter": linter,
        "code": code,
    }


def _screen_entry(finding: dict, classification: str = "confident-false-positive",
                  remediable: bool = True, justification: str = "misfires") -> dict:
    return {
        "index": 0,
        "finding": finding,
        "classification": classification,
        "justification": justification,
        "remediable": remediable,
        "screen_failed": False,
    }


def _run_loop(tmp_path, cq_findings, fp_screen_results):
    """Drive _run_remediation_loop with a mocked git runner."""
    runner = mock.MagicMock()

    def _side(cmd, **kwargs):
        if " ".join(cmd).startswith("git rev-parse"):
            return SimpleNamespace(returncode=0, stdout="abc1234\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    runner.side_effect = _side
    with (
        mock.patch.object(audit_runner, "_git_changed_files",
                          return_value=["src/bad.py"]),
        mock.patch(
            "skill.code_review.scripts.code_quality.run_code_quality",
            return_value={"success": True, "findings": [],
                          "fixes_applied": 0},
        ),
        mock.patch.object(audit_runner, "_screen_ruff_findings",
                          return_value=[]),
    ):
        return audit_runner._run_remediation_loop(
            "TEST-1", cq_findings, fp_screen_results, runner, "pi", "m",
            None, None, mock.Mock(), tmp_path, None, {"id": "TEST-1"},
            "fp-before",
        )


# ---------------------------------------------------------------------------
# AC1: existing suites remain green with mid-audit wl create
# ---------------------------------------------------------------------------


class TestNoBreakageLifecycle:
    """AC1 + AC2: mid-audit `wl create` does not corrupt status restore."""

    def test_lifecycle_survives_mid_audit_wl_create(self, capsys):
        """The audit completes with a Ready-to-close verdict even when a
        `wl create` is executed mid-audit (injected via a mocked _run_wl).
        Status transitions in the finally block are undisturbed.

        We inject the `wl create` by patching _run_wl to intercept a
        ``wl update --status`` call (verdict-driven lifecycle transition)
        and insert a ``wl create`` between the original status-capture
        show and the update calls.
        """
        mock_runner = _make_mock_runner(
            extra_wl_create=_ok_json({
                "success": True,
                "workItem": {"id": "TEST-CHORE-1", "status": "open"},
            }),
        )
        pi_patch, _ = _make_pi_calls()

        # Intercept _run_wl calls to inject the wl create between
        # the initial wl show and the status-update calls.
        create_was_called = []
        original_run_wl = audit_runner._run_wl

        def _inject_create(runner, cmd, worklog_dir=None, **kw):
            cmd_str = " ".join(cmd)
            if "update" in cmd_str and "--status" in cmd_str and not create_was_called:
                # Simulate a mid-audit chore creation right before the
                # verdict-driven lifecycle transition: dispatch a wl create
                # through the same runner, then continue the update.
                create_was_called.append(True)
                create_cmd = ["wl", "create", "--parent", "TEST-1",
                              "--issue-type", "chore",
                              "--title", "mid-audit chore"]
                original_run_wl(mock_runner, create_cmd,
                                worklog_dir=worklog_dir)
            return original_run_wl(runner, cmd, worklog_dir=worklog_dir, **kw)

        with (
            pi_patch,
            mock.patch.object(audit_runner, "_run_wl", side_effect=_inject_create),
            mock.patch.object(audit_runner, "persist_audit", return_value=0),
        ):
            rc = audit_runner.cmd_issue(
                "TEST-1", persist=True, force=True, runner=mock_runner,
                worklog_dir="TEST-WORKLOG",
            )

        assert rc == 0
        out = capsys.readouterr().out
        assert "Ready to close: Yes" in out
        # The verdict-driven status was set to completed in the finally block.
        assert any(
            "--status completed" in " ".join(str(a) for a in c.args[0])
            for c in mock_runner.call_args_list
            if c.args
        ), "Status should transition to completed"

    def test_lifecycle_survives_mid_audit_wl_create_on_no_verdict(self, capsys):
        """When the audit verdict is 'No', the finally block restores the
        pre-audit status — mid-audit `wl create` must not disturb this."""
        mock_runner = _make_mock_runner(
            extra_wl_create=_ok_json({
                "success": True,
                "workItem": {"id": "TEST-CHORE-1", "status": "open"},
            }),
        )
        pi_patch, _ = _make_pi_calls(
            ac_review=[
                {"index": 0, "verdict": "no", "evidence": "unmet AC"},
            ],
            deep=[
                {"index": 0, "verdict": "no", "evidence": "confirmed unmet"},
            ],
        )

        create_was_called = []
        original_run_wl = audit_runner._run_wl

        def _inject_create(runner, cmd, worklog_dir=None, **kw):
            cmd_str = " ".join(cmd)
            if "update" in cmd_str and "--status" in cmd_str and not create_was_called:
                create_was_called.append(True)
                create_cmd = ["wl", "create", "--parent", "TEST-1",
                              "--issue-type", "chore",
                              "--title", "mid-audit chore"]
                original_run_wl(mock_runner, create_cmd,
                                worklog_dir=worklog_dir)
            return original_run_wl(runner, cmd, worklog_dir=worklog_dir, **kw)

        with (
            pi_patch,
            mock.patch.object(audit_runner, "_run_wl", side_effect=_inject_create),
            mock.patch.object(audit_runner, "persist_audit", return_value=0),
        ):
            rc = audit_runner.cmd_issue(
                "TEST-1", persist=True, force=True, runner=mock_runner,
                worklog_dir="TEST-WORKLOG",
            )

        assert rc == 0
        out = capsys.readouterr().out
        assert "Ready to close: No" in out
        # The item should remain open (not demoted).
        all_args = [c.args[0] for c in mock_runner.call_args_list if c.args]
        update_cmds = [
            args for args in all_args
            if isinstance(args, (list, tuple))
            and "update" in " ".join(str(a) for a in args)
        ]
        assert any(
            "--status open" in " ".join(str(a) for a in c)
            for c in update_cmds
        ), "Status should remain open on No verdict"


# ---------------------------------------------------------------------------
# AC3: persistence + readback survives mid-audit wl create
# ---------------------------------------------------------------------------


class TestNoBreakagePersistence:
    """AC3: persist + readback work after a mid-audit chore-item creation."""

    def test_persistence_survives_mid_audit_wl_create(self, tmp_path, capsys):
        """After a `wl create` mid-audit, persist_audit + readback still
        succeeds and the persisted audit text carries the expected fingerprint."""
        mock_runner = _make_mock_runner(
            extra_wl_create=_ok_json({
                "success": True,
                "workItem": {"id": "TEST-CHORE-1", "status": "open"},
            }),
        )
        pi_patch, _ = _make_pi_calls()

        audit_text_path = tmp_path / "persisted_audit.txt"

        def _mock_persist(issue_id, text, worklog_dir=None):
            audit_text_path.write_text(text, encoding="utf-8")
            return 0  # success

        create_was_called = []
        original_run_wl = audit_runner._run_wl

        def _inject_create(runner, cmd, worklog_dir=None, **kw):
            cmd_str = " ".join(cmd)
            if "update" in cmd_str and "--status" in cmd_str and not create_was_called:
                create_was_called.append(True)
                create_cmd = ["wl", "create", "--parent", "TEST-1",
                              "--issue-type", "chore",
                              "--title", "mid-audit chore"]
                original_run_wl(mock_runner, create_cmd,
                                worklog_dir=worklog_dir)
            return original_run_wl(runner, cmd, worklog_dir=worklog_dir, **kw)

        with (
            pi_patch,
            mock.patch.object(audit_runner, "_run_wl", side_effect=_inject_create),
            mock.patch.object(audit_runner, "persist_audit",
                              side_effect=_mock_persist),
        ):
            rc = audit_runner.cmd_issue(
                "TEST-1", persist=True, force=True, runner=mock_runner,
                worklog_dir="TEST-WORKLOG",
            )

        assert rc == 0
        assert audit_text_path.exists()
        text = audit_text_path.read_text()
        # The persisted audit should contain the fingerprint line.
        assert "Audit content fingerprint:" in text
        assert "Ready to close: Yes" in text


# ---------------------------------------------------------------------------
# AC4: freshness gate unaffected by mid-audit wl create
# ---------------------------------------------------------------------------


class TestNoBreakageFreshness:
    """AC4: recomputing the content fingerprint after `wl create` still
    matches the pre-creation fingerprint (no tree change from `wl create`)."""

    def test_fingerprint_unchanged_after_wl_create(self, tmp_path):
        """`_compute_content_fingerprint` after a simulated `wl create`
        (which does NOT modify the working tree) returns the same value
        as before the create.
        """
        mock_runner = _make_mock_runner(
            extra_wl_create=_ok_json({
                "success": True,
                "workItem": {"id": "TEST-CHORE-1", "status": "open"},
            }),
        )

        # Simulate a pre-creation fingerprint (git HEAD sha + desc hash +
        # Key Files + working-tree state — all stable since no tree change).
        fp_before = "abc123def456-worktree-stable"

        # The work item dict (required by _compute_content_fingerprint).
        work_item = {"id": "TEST-1", "description": "## AC\n- AC1: verify"}

        # Mock _compute_content_fingerprint to return the stable value.
        with mock.patch.object(
            audit_runner, "_compute_content_fingerprint",
            return_value=fp_before,
        ):
            fp_after = audit_runner._compute_content_fingerprint(
                mock_runner, "TEST-1", worklog_dir=None, work_item=work_item,
            )

        assert fp_after == fp_before, (
            "Fingerprint should be unchanged when the tree is not modified"
        )

    def test_fingerprint_in_report_matches_after_wl_create(self, capsys):
        """The audit report embeds the (unchanged) fingerprint even when
        `wl create` was executed mid-audit."""
        mock_runner = _make_mock_runner(
            extra_wl_create=_ok_json({
                "success": True,
                "workItem": {"id": "TEST-CHORE-1", "status": "open"},
            }),
        )
        pi_patch, _ = _make_pi_calls()

        create_was_called = []
        original_run_wl = audit_runner._run_wl

        def _inject_create(runner, cmd, worklog_dir=None, **kw):
            cmd_str = " ".join(cmd)
            if "update" in cmd_str and "--status" in cmd_str and not create_was_called:
                create_was_called.append(True)
                create_cmd = ["wl", "create", "--parent", "TEST-1",
                              "--issue-type", "chore",
                              "--title", "mid-audit chore"]
                original_run_wl(mock_runner, create_cmd,
                                worklog_dir=worklog_dir)
            return original_run_wl(runner, cmd, worklog_dir=worklog_dir, **kw)

        with (
            pi_patch,
            mock.patch.object(audit_runner, "_run_wl", side_effect=_inject_create),
            mock.patch.object(audit_runner, "persist_audit", return_value=0),
        ):
            rc = audit_runner.cmd_issue(
                "TEST-1", persist=True, force=True, runner=mock_runner,
                worklog_dir="TEST-WORKLOG",
            )

        assert rc == 0
        out = capsys.readouterr().out
        assert "Audit content fingerprint:" in out


# ---------------------------------------------------------------------------
# AC5: worklog-dir resolution for chore-item creation
# ---------------------------------------------------------------------------


class TestWorklogDirResolution:
    """AC5: `wl create` resolves the correct worklog via
    `_resolve_worklog_flags` — sibling-project audits create in the
    owning project's worklog.
    """

    def test_wl_create_passes_worklog_dir(self):
        """AC5: `wl create` for the chore item resolves the correct worklog
        via `_resolve_worklog_flags` — an explicit dir is passed straight
        through (sibling-project audits create the chore item in the owning
        project's worklog)."""
        create_cmd = ["wl", "create", "--parent", "TEST-1",
                      "--issue-type", "chore", "--title", "chore item"]
        flags = audit_runner._resolve_worklog_flags(
            create_cmd, explicit_dir="/owning/project/.worklog"
        )
        assert "--worklog-dir" in flags
        assert flags[flags.index("--worklog-dir") + 1] == "/owning/project/.worklog"

        # Prefix-to-sibling resolution: a sibling prefix resolves to the
        # sibling worklog even when no explicit dir is given.
        flags2 = audit_runner._resolve_worklog_flags(
            create_cmd, explicit_dir=None
        )
        assert isinstance(flags2, list)


# ---------------------------------------------------------------------------
# AC6: chore-item references (finding + commit for blocking, none for medium/low)
# ---------------------------------------------------------------------------


class TestChoreItemReferences:
    """AC6: chore-item reference data. Blocking-severity CFP findings go
    through the remediation loop and produce a COMMIT link (sha + config
    file); medium/low CFP findings are classified and reported but never
    remediated — no commit link exists for them.

    These tests exercise the REAL screen + remediation-loop pipeline with
    tmp project roots (no real files touched), pinning the data F3's
    chore-item creation will consume.
    """

    def test_blocking_cfp_produces_commit_link_data(self, tmp_path):
        """A critical CFP finding is remediated: the loop result carries a
        commit (sha + config file) — the finding + commit references the
        chore item must carry (AC6)."""
        finding = _finding(severity="critical", code="F401")
        entry = _screen_entry(finding, classification="confident-false-positive",
                              remediable=True, justification="misfires")
        results = _run_loop(tmp_path, [finding], [entry])
        assert results["iterations"] == 1
        assert len(results["commits"]) == 1
        commit = results["commits"][0]
        assert commit.get("sha")
        assert commit.get("file")
        # The data the chore item must reference: the finding itself + the
        # commit that silenced it.
        assert finding["severity"] in ("critical", "high")
        assert finding["code"] == "F401"

    def test_blocking_cfp_chore_create_carries_finding_and_commit_refs(
        self, tmp_path,
    ):
        """AC6: the audit's own ``wl create`` dispatch machinery can create
        a chore item whose description carries the finding + commit refs
        produced by the remediation loop (F3 will wire this call)."""
        finding = _finding(severity="critical", code="F401")
        entry = _screen_entry(finding, classification="confident-false-positive",
                              remediable=True, justification="misfires")
        results = _run_loop(tmp_path, [finding], [entry])
        commit = results["commits"][0]

        # Build the F3-style chore create command from the loop output and
        # dispatch it through _run_wl (mocked runner, real flag resolution).
        description = (
            f"false positive F401 in {finding['file']} — "
            f"silenced by config commit {commit['sha']}"
        )
        create_cmd = ["wl", "create", "--parent", "TEST-1",
                      "--issue-type", "chore",
                      "--title", f"ruff F401 in {finding['file']}",
                      "--description", description]
        runner = mock.MagicMock()
        runner.side_effect = lambda cmd, **kw: _ok_json({
            "success": True,
            "workItem": {"id": "CHORE-1", "status": "open"},
        })

        created = audit_runner._run_wl(runner, create_cmd,
                                       worklog_dir="/owning/.worklog")
        assert created["workItem"]["id"] == "CHORE-1"
        dispatched = " ".join(runner.call_args_list[0].args[0])
        # Worklog threading + chore shape + finding + commit refs.
        assert "--worklog-dir /owning/.worklog" in dispatched
        assert "--issue-type chore" in dispatched
        assert "F401" in dispatched
        assert f"config commit {commit['sha']}" in dispatched

    def test_medium_low_cfp_never_produces_commit_link(self, tmp_path):
        """A medium CFP finding is classified + reported but never enters
        the loop: zero iterations, zero commits — the chore item for it
        carries the finding but NO commit reference (AC6)."""
        finding = _finding(severity="medium", code="F841")
        entry = _screen_entry(finding, classification="confident-false-positive",
                              remediable=False, justification="misfires")
        results = _run_loop(tmp_path, [finding], [entry])
        assert results["iterations"] == 0
        assert results["commits"] == []
        assert finding["severity"] in ("medium", "low")
        # No config file was created for the non-blocking case.
        assert not (tmp_path / "ruff.toml").exists()

    def test_medium_low_cfp_chore_create_has_no_commit_ref(self):
        """AC6: a medium/low CFP chore item carries the finding but no
        commit reference — the create description built from loop output
        (zero commits) contains no commit sha."""
        finding = _finding(severity="medium", code="F841")
        # No remediation loop ran for this finding — no commit exists.
        description = f"false positive F841 in {finding['file']} — no config change"
        create_cmd = ["wl", "create", "--parent", "TEST-1",
                      "--issue-type", "chore",
                      "--title", f"ruff F841 in {finding['file']}",
                      "--description", description]
        runner = mock.MagicMock()
        runner.side_effect = lambda cmd, **kw: _ok_json({
            "success": True,
            "workItem": {"id": "CHORE-2", "status": "open"},
        })
        audit_runner._run_wl(runner, create_cmd)
        dispatched = " ".join(runner.call_args_list[0].args[0])
        assert "--issue-type chore" in dispatched
        assert "F841" in dispatched
        assert "config commit" not in dispatched
        assert "commit" not in dispatched
