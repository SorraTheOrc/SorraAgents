"""Tests for the Phase 1 merge gate (SA-0MT456M27001LRTL).

The gate guarantees, at the very start of audit Phase 1 (before the
code-quality scan, children-stage check, or surface AC assessment), that
the work item being audited is integrated into its owning repository's
``dev`` branch. It resolves the item's integration evidence generically
(owning repo via worklog prefix-to-sibling; commits + ``wl-<id>-*`` branch
from the item itself — never a hardcoded commit or repo), verifies
ancestor/merge status against ``origin/dev``, integrates when missing
(fetch, integrate, build, test, push dev), and fails the audit closed
("Ready to close: No" + needs-producer-review) when integration cannot
complete — never proceeding past Phase 1 with unmerged work.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from audit.scripts import audit_runner
from audit.tests.wl_helpers import make_stateful_runner

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ctx(owning_root: str | None = "OWNING",
              runner=None,
              description: str = "",
              work_item: dict | None = None,
              issue_id: str = "WL-ABC123",
              **over) -> audit_runner._AuditContext:
    """Build a minimal _AuditContext for merge-gate unit tests."""
    if runner is None:
        runner = lambda cmd: SimpleNamespace(
            returncode=0, stdout="", stderr="",
        )
    return audit_runner._AuditContext(
        issue_id=issue_id, persist=False, timeout=None,
        parent_timeout=None, pi_bin="pi", model=None,
        model_source="local", runner=runner, json_mode=False,
        debug_log=None, force=True, worklog_dir=None, batch_phase2=False,
        green_run=None, audit_children=False, max_child_audits=None,
        run_tests=False, owning_root=owning_root, description=description,
        work_item=work_item or {}, **over,
    )


def _git_proc(rc: int, stdout: str = "", stderr: str = "") -> SimpleNamespace:
    return SimpleNamespace(returncode=rc, stdout=stdout, stderr=stderr)


def _git_runner(cwd: Path):
    """Real-git runner executing every command inside *cwd*."""
    def _run(cmd):
        return subprocess.run(list(cmd), cwd=str(cwd), check=False,
                              text=True, capture_output=True)
    return _run


def _make_real_repo(tmp_path: Path, prefix: str = "WL",
                    issue_id: str = "WL-0MSI4TAT70058921",
                    second_dev_commit: bool = True,
                    feature_branch: bool = True) -> tuple[Path, Path, dict]:
    """Create an owning repo with origin/dev plus a wl-<id> feature branch.

    Layout::

        <tmp>/projects/ctxhub/.worklog/config.yaml   (prefix)
        <tmp>/projects/ctxhub/src/main.py            (marker)
        <tmp>/origin.git/                            (bare remote "origin")

    Branches:
      - ``dev`` — first commit on the default branch, optionally a second
        dev commit, pushed to origin/dev.
      - ``wl-<issue_id>-rename-tab`` — one commit NOT pushed (unmerged).

    Returns ``(worklog_dir, owning_root, shas)`` where ``shas`` maps
    ``dev_default`` / ``dev_head`` / ``feature_head`` / ``feature_parent``.
    """
    projects = tmp_path / "projects"
    owning = projects / "ctxhub"
    owning.mkdir(parents=True)
    (owning / "src").mkdir()
    (owning / "src" / "main.py").write_text("print('hi')\n", encoding="utf-8")
    (owning / ".gitignore").write_text(".worklog/\n", encoding="utf-8")
    wl_dir = owning / ".worklog"
    wl_dir.mkdir(parents=True)
    (wl_dir / "config.yaml").write_text(
        f"projectName: ContextHub\nprefix: {prefix}\n", encoding="utf-8"
    )
    origin = tmp_path / "origin.git"
    origin.mkdir()

    def _git(*args: str, cwd: Path) -> str:
        proc = subprocess.run(["git", *args], cwd=str(cwd), check=True,
                              capture_output=True, text=True)
        return proc.stdout.strip()

    shas: dict[str, str] = {}
    _git("init", "--bare", cwd=origin)
    _git("init", cwd=owning)
    _git("config", "user.email", "t@t.com", cwd=owning)
    _git("config", "user.name", "T", cwd=owning)
    _git("remote", "add", "origin", str(origin), cwd=owning)
    (owning / "src" / "main.py").write_text("print('dev1')\n", encoding="utf-8")
    _git("add", "-A", cwd=owning)
    _git("commit", "-m", "dev first", cwd=owning)
    shas["dev_default"] = _git("rev-parse", "HEAD", cwd=owning)
    _git("branch", "-M", "dev", cwd=owning)
    if second_dev_commit:
        (owning / "src" / "main.py").write_text(
            "print('dev2')\n", encoding="utf-8"
        )
        _git("add", "-A", cwd=owning)
        _git("commit", "-m", "dev second: rename tab", cwd=owning)
    shas["dev_head"] = _git("rev-parse", "HEAD", cwd=owning)
    _git("push", "origin", "dev", cwd=owning)

    if feature_branch:
        _git("checkout", "-b", f"wl-{issue_id}-rename-tab", shas["dev_head"],
             cwd=owning)
        shas["feature_parent"] = _git("rev-parse", "HEAD", cwd=owning)
        (owning / "src" / "tab.py").write_text(
            "tab = 'Worklog'\n", encoding="utf-8"
        )
        _git("add", "-A", cwd=owning)
        _git("commit", "-m", "rename podcast tab to Worklog", cwd=owning)
        shas["feature_head"] = _git("rev-parse", "HEAD", cwd=owning)
        _git("checkout", "dev", cwd=owning)
    return wl_dir, owning, shas


def _ctx_with_runner(cwd: Path, issue_id: str,
                     description: str = "",
                     work_item: dict | None = None,
                     owning_root: str | None = None) -> audit_runner._AuditContext:
    """A context whose runner executes real git against *cwd*."""
    return _make_ctx(
        owning_root=owning_root or str(cwd),
        runner=_git_runner(cwd),
        description=description,
        work_item=work_item,
    )


# ---------------------------------------------------------------------------
# _resolve_item_integration_evidence — generic resolution (AC1/AC4)
# ---------------------------------------------------------------------------

class TestResolveItemIntegrationEvidence:
    """The gate resolves the AUDITED item's evidence (commits + branch) from
    the item itself — never a hardcoded commit or repo (AC1/AC4)."""

    def test_feature_branch_resolved_from_owning_repo_refs(self):
        """A branch matching wl-<issue_id>-* in the owning repo is resolved
        with its HEAD sha."""
        ctx = _make_ctx(
            runner=lambda cmd: _git_proc(
                0, "wl-WL-ABC123-rename-tab deadbeefdeadbeefdeadbeefdeadbeefdeadbeef\n"
            )
        )
        commits, branch = audit_runner._resolve_item_integration_evidence(ctx)
        assert branch == "wl-WL-ABC123-rename-tab"
        assert "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef" in commits

    def test_garbage_for_each_ref_output_ignored(self):
        """Mocked/garbage git output (e.g. a wl JSON success blob) is never
        treated as a branch or commit — parsing is defensive."""
        ctx = _make_ctx(
            runner=lambda cmd: _git_proc(
                0, '{"success": true}\nnot-a-branch 12345\n'
            )
        )
        commits, branch = audit_runner._resolve_item_integration_evidence(ctx)
        assert commits == []
        assert branch == ""

    def test_commits_resolved_from_description_and_comments(self):
        """Commit shas in the item's description and comments are resolved."""
        ctx = _make_ctx(
            description=(
                "Implemented in commit 4f1f0452abc — see"
                " https://example/18729266def\n"
            ),
            work_item={
                "comments": [
                    {"comment": "push produced c661f3c5aabb onto dev"},
                    {"comment": "no shas here"},
                ]
            },
        )
        commits, branch = audit_runner._resolve_item_integration_evidence(ctx)
        assert "4f1f0452abc" in commits
        assert "18729266def" in commits
        assert "c661f3c5aabb" in commits
        assert len(commits) == 3
        assert branch == ""

    def test_motivating_example_reachable_generically(self):
        """AC4: the WL-0MSI4TAT70058921 rename work is reachable through the
        generic resolution — its feature branch plus its recorded commits —
        with no hardcoded reference in the gate itself."""
        branch = "wl-WL-0MSI4TAT70058921-rename-podcast-editing-tab-to-worklog"
        head = "c661f3c5fedcba9876543210fedcba9876543210"
        ctx = _make_ctx(
            issue_id="WL-0MSI4TAT70058921",
            runner=lambda cmd: _git_proc(0, f"{branch} {head}\n"),
            description="Rename 'Podcast Editing' tab to 'Worklog'",
            work_item={"comments": [
                {"comment": "resolved commit 4f1f0452abc123 cherry-picked"},
            ]},
        )
        commits, resolved_branch = audit_runner._resolve_item_integration_evidence(ctx)
        assert resolved_branch == branch
        assert head in commits
        assert "4f1f0452abc123" in commits


# ---------------------------------------------------------------------------
# _verify_merged_in_dev — ancestor check against origin/dev (AC1)
# ---------------------------------------------------------------------------

class TestVerifyMergedInDev:
    """The verification runs git merge-base --is-ancestor <candidate>
    origin/dev and records command + result as audit evidence (AC1)."""

    def test_merged_when_commit_ancestor_of_origin_dev(self, tmp_path):
        _wl, owning, shas = _make_real_repo(tmp_path)
        ctx = _ctx_with_runner(owning, "WL-X")
        merged, evidence, baseline = audit_runner._verify_merged_in_dev(
            ctx, [shas["dev_head"]], ""
        )
        assert merged is True
        assert baseline is True
        assert "merge-base --is-ancestor" in evidence
        assert "-> yes" in evidence

    def test_not_merged_when_feature_branch_not_in_dev(self, tmp_path):
        _wl, owning, shas = _make_real_repo(tmp_path)
        branch = "wl-WL-0MSI4TAT70058921-rename-tab"
        ctx = _ctx_with_runner(owning, "WL-0MSI4TAT70058921")
        merged, evidence, baseline = audit_runner._verify_merged_in_dev(
            ctx, [shas["feature_head"]], branch
        )
        assert merged is False
        assert baseline is True
        assert "-> no" in evidence
        assert branch in evidence

    def test_no_baseline_neither_origin_dev_nor_local_dev(self, tmp_path):
        """A repo with no dev ref at all has no integration target — the
        gate must treat it as non-blocking (baseline_ok False).

        The remote, the remote-tracking ref AND the local branch must all be
        dev-free: a plain ``git fetch origin dev`` would otherwise re-create
        ``refs/remotes/origin/dev`` from the remote and the ancestor check
        would pass against the freshly-fetched ref.
        """
        _wl, owning, shas = _make_real_repo(
            tmp_path, second_dev_commit=False, feature_branch=False
        )
        # Remove dev everywhere: local branch (from a temp checkout), the
        # remote branch, and the remote-tracking ref.
        subprocess.run(["git", "checkout", "-q", "-b", "tmp-no-dev"],
                       cwd=str(owning), check=True, capture_output=True)
        subprocess.run(["git", "branch", "-D", "dev"], cwd=str(owning),
                       check=True, capture_output=True)
        subprocess.run(["git", "push", "-q", "origin", "--delete", "dev"],
                       cwd=str(owning), check=True, capture_output=True)
        subprocess.run(["git", "update-ref", "-d", "refs/remotes/origin/dev"],
                       cwd=str(owning), check=False, capture_output=True)
        ctx = _ctx_with_runner(owning, "WL-X")
        merged, evidence, baseline = audit_runner._verify_merged_in_dev(
            ctx, [shas["dev_default"]], ""
        )
        assert merged is False
        assert baseline is False
        assert "No dev baseline" in evidence

    def test_no_candidates_returns_not_verified_without_fetch(self, tmp_path):
        """No commits/branch resolved → cannot verify; recorded as such.
        Uses a repo with no origin remote to prove no fetch was attempted."""
        _wl, owning, _shas = _make_real_repo(tmp_path, feature_branch=False)
        calls: list[list[str]] = []
        def _runner(cmd):
            calls.append(list(cmd))
            return _git_runner(owning)(cmd)
        ctx = _make_ctx(owning_root=str(owning), runner=_runner)
        merged, evidence, baseline = audit_runner._verify_merged_in_dev(
            ctx, [], ""
        )
        assert merged is False
        assert baseline is False
        assert "no commits/branch resolved" in evidence
        assert not any("fetch" in str(c) for c in calls), \
            "fetch must not run when there are no candidates"


# ---------------------------------------------------------------------------
# _phase_merge_gate — gate decision logic
# ---------------------------------------------------------------------------

class TestPhaseMergeGate:
    """Gate outcomes: merged / no-evidence / no-baseline proceed;
    integration success proceeds; integration failure blocks (AC1–AC3)."""

    def test_merged_passes(self):
        ctx = _make_ctx()
        with (
            mock.patch.object(
                audit_runner, "_resolve_item_integration_evidence",
                return_value=(["deadbeef"], "wl-WL-ABC123-x"),
            ),
            mock.patch.object(
                audit_runner, "_verify_merged_in_dev",
                return_value=(True, "merge-base ... -> yes", True),
            ),
        ):
            rc = audit_runner._phase_merge_gate(ctx)
        assert rc is None
        assert ctx.merge_gate_merged is True
        assert ctx.merge_gate_blocker == ""

    def test_no_evidence_passes(self):
        ctx = _make_ctx()
        with (
            mock.patch.object(
                audit_runner, "_resolve_item_integration_evidence",
                return_value=([], ""),
            ),
            mock.patch.object(
                audit_runner, "_verify_merged_in_dev",
                return_value=(False, "no commits/branch resolved", False),
            ),
        ):
            rc = audit_runner._phase_merge_gate(ctx)
        assert rc is None
        assert ctx.merge_gate_merged is False
        assert ctx.merge_gate_blocker == ""
        assert "no commits/branch resolvable" in ctx.merge_gate_evidence

    def test_no_dev_baseline_passes(self):
        """Evidence exists but the owning repo has no dev at all — not an
        integration trigger (no dev target to be missing from)."""
        ctx = _make_ctx()
        with (
            mock.patch.object(
                audit_runner, "_resolve_item_integration_evidence",
                return_value=(["deadbeef"], ""),
            ),
            mock.patch.object(
                audit_runner, "_verify_merged_in_dev",
                return_value=(False, "No dev baseline", False),
            ),
        ):
            rc = audit_runner._phase_merge_gate(ctx)
        assert rc is None
        assert ctx.merge_gate_blocker == ""
        assert "no dev baseline" in ctx.merge_gate_evidence

    def test_integration_success_passes_with_post_merge_note(self):
        ctx = _make_ctx()
        with (
            mock.patch.object(
                audit_runner, "_resolve_item_integration_evidence",
                return_value=(["deadbeef"], "wl-WL-ABC123-x"),
            ),
            mock.patch.object(
                audit_runner, "_verify_merged_in_dev",
                return_value=(False, "merge-base ... -> no", True),
            ),
            mock.patch.object(
                audit_runner, "_integrate_into_dev",
                return_value=(True, "cherry-pick ok\npush ok"),
            ),
        ):
            rc = audit_runner._phase_merge_gate(ctx)
        assert rc is None
        assert ctx.merge_gate_merged is True
        assert ctx.merge_gate_blocker == ""
        assert "integration completed" in ctx.merge_gate_evidence

    def test_integration_failure_blocks(self):
        ctx = _make_ctx()
        with (
            mock.patch.object(
                audit_runner, "_resolve_item_integration_evidence",
                return_value=(["deadbeef"], "wl-WL-ABC123-x"),
            ),
            mock.patch.object(
                audit_runner, "_verify_merged_in_dev",
                return_value=(False, "merge-base ... -> no", True),
            ),
            mock.patch.object(
                audit_runner, "_integrate_into_dev",
                return_value=(False, "push origin HEAD:refs/heads/dev failed"),
            ),
        ):
            rc = audit_runner._phase_merge_gate(ctx)
        assert rc == 1
        assert ctx.merge_gate_blocker != ""
        assert "integration FAILED" in ctx.merge_gate_blocker
        assert ctx.merge_gate_merged is False

    def test_unexpected_error_with_evidence_blocks(self):
        """An unexpected exception after evidence resolution fails closed —
        the work is unmerged and unintegratable (AC3)."""
        ctx = _make_ctx()
        with mock.patch.object(
            audit_runner, "_resolve_item_integration_evidence",
            return_value=(["deadbeef"], "wl-WL-ABC123-x"),
        ), mock.patch.object(
            audit_runner, "_verify_merged_in_dev",
            side_effect=RuntimeError("git exploded"),
        ):
            rc = audit_runner._phase_merge_gate(ctx)
        assert rc == 1
        assert ctx.merge_gate_blocker != ""
        assert ctx.script_failure is not None


# ---------------------------------------------------------------------------
# cmd_issue end-to-end: fail-closed on blocked gate (AC3)
# ---------------------------------------------------------------------------

_WL_ITEM = {
    "id": "WL-ABC123",
    "title": "Rename tab",
    "description": "## Acceptance Criteria\n- AC1: tab renamed",
    "status": "open",
    "stage": "plan_complete",
    "updatedAt": "2026-08-01T00:00:00.000Z",
}


def _wl_runner(recorded: list[list[str]]):
    """Stateful wl runner for the cmd_issue merge-gate paths."""
    def _side_effect(cmd):
        recorded.append(list(cmd))
        cmd_str = " ".join(cmd)
        if "show" in cmd_str and "--children" in cmd_str:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({
                    "success": True, "workItem": dict(_WL_ITEM),
                    "children": [],
                }),
                stderr="",
            )
        if "show" in cmd_str and "--json" in cmd_str:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"success": True,
                                   "workItem": dict(_WL_ITEM)}),
                stderr="",
            )
        if "update" in cmd_str:
            return SimpleNamespace(
                returncode=0, stdout=json.dumps({"success": True}), stderr="",
            )
        if "audit-show" in cmd_str:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"success": True, "audit": None}),
                stderr="",
            )
        return SimpleNamespace(
            returncode=0, stdout=json.dumps({"success": True}), stderr="",
        )
    return make_stateful_runner(_side_effect)


class TestCmdIssueMergeGateFailClosed:
    """AC2/AC3: when integration cannot complete, the audit fails with
    'Ready to close: No', marks the item needs-producer-review, and never
    runs screening (Phase 1) or Phase 2."""

    def test_blocked_gate_fails_closed(self, capsys):
        recorded: list[list[str]] = []
        runner = _wl_runner(recorded)

        pi_mock = mock.MagicMock()
        with (
            mock.patch.object(
                audit_runner, "_resolve_owning_project_root",
                return_value=audit_runner.TARGET_PROJECT_ROOT,
            ),
            mock.patch.object(
                audit_runner, "_resolve_item_integration_evidence",
                return_value=(["deadbeef"], "wl-WL-ABC123-rename-tab"),
            ),
            mock.patch.object(
                audit_runner, "_verify_merged_in_dev",
                return_value=(False, "merge-base ... -> no", True),
            ),
            mock.patch.object(
                audit_runner, "_integrate_into_dev",
                return_value=(False, "push failed: rejected"),
            ),
            mock.patch.object(
                audit_runner, "_call_pi_and_maybe_log", pi_mock,
            ),
            mock.patch(
                "code_review.scripts.code_quality.run_code_quality",
                return_value={"success": True, "findings": [],
                              "fixes_applied": 0},
            ),
        ):
            rc = audit_runner.cmd_issue(
                "WL-ABC123", persist=False, force=True, runner=runner,
            )

        assert rc == 1
        out = capsys.readouterr().out
        assert "Ready to close: No" in out
        assert "Merge gate" in out
        # Screening must NOT run: zero pi calls on the blocked path.
        pi_mock.assert_not_called()
        # Lifecycle applies the fail-closed transition with producer review.
        updates = [c for c in recorded if "update" in " ".join(c)]
        assert updates, "expected a lifecycle wl update"
        producer_updates = [
            c for c in updates
            if "--needs-producer-review" in c and "yes" in c
        ]
        assert producer_updates, (
            f"blocked gate must flag needs-producer-review, got {updates}"
        )
        open_updates = [
            c for c in updates if "--status" in c and "open" in c
        ]
        assert open_updates, f"blocked gate must demote to open, got {updates}"

    def test_merged_gate_proceeds_and_reports_evidence(self, capsys):
        """AC1: a merged item proceeds through screening and the report
        carries the merge gate evidence (check command + result)."""
        recorded: list[list[str]] = []
        runner = _wl_runner(recorded)
        pass_batch = {
            "extracted_text": json.dumps([
                {"index": 0, "verdict": "met", "evidence": "src/tab.py:1"},
            ]),
        }
        with (
            mock.patch.object(
                audit_runner, "_resolve_owning_project_root",
                return_value=audit_runner.TARGET_PROJECT_ROOT,
            ),
            mock.patch.object(
                audit_runner, "_resolve_item_integration_evidence",
                return_value=(["deadbeef"], "wl-WL-ABC123-rename-tab"),
            ),
            mock.patch.object(
                audit_runner, "_verify_merged_in_dev",
                return_value=(True, ("git merge-base --is-ancestor "
                                      "deadbeef origin/dev -> yes"), True),
            ),
            mock.patch.object(
                audit_runner, "_call_pi_and_maybe_log",
                return_value=pass_batch,
            ),
            mock.patch(
                "code_review.scripts.code_quality.run_code_quality",
                return_value={"success": True, "findings": [],
                              "fixes_applied": 0},
            ),
        ):
            rc = audit_runner.cmd_issue(
                "WL-ABC123", persist=False, force=True, runner=runner,
            )

        assert rc == 0
        out = capsys.readouterr().out
        assert "Ready to close: Yes" in out
        assert "## Merge Gate Evidence (Phase 1)" in out
        assert "merge-base --is-ancestor" in out


# ---------------------------------------------------------------------------
# _build_merge_gate_failure_report / lifecycle needs-producer-review
# ---------------------------------------------------------------------------

class TestMergeGateFailureReport:
    """The fail-closed report parses as 'Ready to close: No' and carries the
    integration failure evidence."""

    def test_report_shape(self):
        ctx = _make_ctx(
            work_item={"title": "Rename tab", "id": "WL-ABC123"}
        )
        ctx.merge_gate_blocker = (
            "Merge gate: work item changes are NOT merged into the owning "
            "repo's dev and integration FAILED. Evidence:\npush failed"
        )
        report = audit_runner._build_merge_gate_failure_report(ctx)
        assert "Ready to close: No" in report
        assert "## Merge Gate (Phase 1)" in report
        assert "push failed" in report
        # The verdict JSON payload the persister validates.
        assert '"verdict": "no"' in report


class TestLifecycleMergeGateBlocker:
    """The 'no' verdict with a merge-gate blocker adds
    --needs-producer-review yes (AC3)."""

    def test_lifecycle_flags_producer_review(self):
        ctx = _make_ctx()
        ctx.audit_verdict = "no"
        ctx.audit_completed = True
        ctx.merge_gate_blocker = "integration failed"
        calls: list[list[str]] = []

        def _runner(cmd):
            calls.append(list(cmd))
            cmd_str = " ".join(cmd)
            if "show" in cmd_str and "--json" in cmd_str and "audit-show" not in cmd_str:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({
                        "success": True,
                        "workItem": {
                            "id": "WL-ABC123",
                            "status": "open",
                            "stage": "plan_complete",
                        },
                    }),
                    stderr="",
                )
            if "update" in cmd_str:
                return SimpleNamespace(
                    returncode=0, stdout=json.dumps({"success": True}),
                    stderr="",
                )
            return SimpleNamespace(
                returncode=0, stdout=json.dumps({"success": True}), stderr="",
            )

        ctx.runner = make_stateful_runner(_runner)
        audit_runner._apply_terminal_lifecycle(ctx)
        update_cmds = [c for c in calls if "update" in " ".join(c)]
        assert any(
            "--needs-producer-review" in c and "yes" in c
            for c in update_cmds
        ), f"expected --needs-producer-review yes, got {update_cmds}"