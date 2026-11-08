"""Candidate-false-positive chore deduplication, waivers and gating.

SA-0MVZNECSD005XB42 — the audit runner created a NEW chore for every
medium/low ``confident-false-positive`` finding on every audit cycle and then
set ``needsProducerReview`` on the passing audit, so releases could never
converge (20+ duplicate chores; 7 items cleared by hand in the 2026-11-02
release).

Acceptance criteria pinned here (each test names the AC it protects):

* **AC1** — repeated audits of unchanged code do not create duplicate chores
  (deduplicated on ``rule + file + line`` via the ``CFP-Chore-Key`` marker;
  idempotent across runs).
* **AC2** — a passing audit does NOT set ``needsProducerReview`` when the only
  medium/low findings are already tracked (or accepted) candidate false
  positives; a new chore still surfaces the producer once.
* **AC3** — an accepted false positive is recorded once (waiver) and then
  triggers neither chore creation nor blocking.
* **AC4** — audit-generated tracker chores are not producer-review decisions
  (the release gate regression is covered in
  ``tests/unit/test-ship-cfp-chore-release-gate.mjs``).
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
from audit.scripts import audit_runner
from audit.tests.wl_helpers import audit_show_response, stateful_wl_side_effect


@pytest.fixture(autouse=True)
def _free_audit_slot():
    with mock.patch.object(
        audit_runner, "_acquire_audit_slot", return_value=contextlib.nullcontext()
    ):
        yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _finding(severity: str = "medium", code: str = "F841",
             file: str = "src/bad.py", line: int = 1) -> dict:
    return {
        "severity": severity,
        "file": file,
        "line": line,
        "message": f"{code} message",
        "linter": "ruff",
        "code": code,
    }


def _entry(finding: dict, classification: str = "confident-false-positive",
           remediable: bool = False, justification: str = "rule misfires") -> dict:
    return {
        "index": 0,
        "finding": finding,
        "classification": classification,
        "justification": justification,
        "remediable": remediable,
        "screen_failed": False,
    }


def _chore_item(chore_id: str, key: str, *, status: str = "open",
                issue_type: str = "chore") -> dict:
    """An existing tracking chore as ``wl list --json`` reports it."""
    return {
        "id": chore_id,
        "title": f"ruff chore for {key}",
        "description": f"Tracking.\n\n{audit_runner.CFP_CHORE_KEY_PREFIX} {key}",
        "status": status,
        "issueType": issue_type,
    }


def _runner(*, existing: list[dict] | None = None,
            list_error: bool = False,
            create_ids: list[str] | None = None) -> tuple[mock.MagicMock, list]:
    """Mock ``wl`` runner recording commands; ``create`` returns fresh ids."""
    created = list(create_ids or ["CHORE-NEW"])
    commands: list[list[str]] = []
    runner = mock.MagicMock()

    def _side(cmd, **kwargs):
        commands.append(list(cmd))
        joined = " ".join(cmd)
        if joined.startswith("git rev-parse"):
            return SimpleNamespace(returncode=0, stdout="abc1234\n", stderr="")
        if " list " in f" {joined} ":
            if list_error:
                return SimpleNamespace(returncode=1, stdout="", stderr="boom")
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"success": True,
                                   "workItems": list(existing or [])}),
                stderr="",
            )
        if " create " in f" {joined} ":
            chore_id = created.pop(0) if len(created) > 1 else created[0]
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"success": True,
                                   "workItem": {"id": chore_id}}),
                stderr="",
            )
        return SimpleNamespace(
            returncode=0, stdout=json.dumps({"success": True}), stderr="",
        )

    runner.side_effect = _side
    runner.commands = commands
    return runner, commands


def _run_loop(tmp_path, entries, *, waivers=None, existing=None,
              list_error=False) -> tuple[dict, list]:
    runner, commands = _runner(existing=existing, list_error=list_error)
    with (
        mock.patch.object(audit_runner, "_git_changed_files",
                          return_value=["src/bad.py"]),
        mock.patch.object(audit_runner, "_screen_ruff_findings",
                          return_value=[]),
    ):
        results = audit_runner._run_remediation_loop(
            "TEST-1", [e["finding"] for e in entries], entries, runner,
            "pi", "m", None, None, mock.Mock(), tmp_path, "TEST-WORKLOG",
            {"id": "TEST-1"}, "fp-before", waivers=waivers,
        )
    return results, commands


def _create_commands(commands: list[list[str]]) -> list[list[str]]:
    return [c for c in commands if " create " in f" {' '.join(c)} "]


def _waiver(rule="F841", file="src/bad.py", line=1, justification="accepted"):
    return {
        "linter": "ruff", "rule": rule, "file": file, "line": line,
        "justification": justification, "author": "producer",
    }


# ---------------------------------------------------------------------------
# AC1: deduplication (within a run, across runs, idempotent)
# ---------------------------------------------------------------------------


class TestChoreDeduplication:
    def test_duplicate_findings_in_one_run_create_one_chore(self, tmp_path):
        """AC1: the same rule+file+line twice in one run → one chore."""
        finding = _finding()
        entries = [_entry(finding), _entry(dict(finding))]
        results, commands = _run_loop(tmp_path, entries)
        assert len(_create_commands(commands)) == 1
        assert results["chores_created"] == 1
        assert results["chores_reused"][0]["id"] == "CHORE-NEW"
        assert [e["tracked_chore_id"] for e in results["fp_screen_results"]] == [
            "CHORE-NEW", "CHORE-NEW",
        ]

    def test_existing_chore_is_reused_not_duplicated(self, tmp_path):
        """AC1: a live chore for the same finding is reused (no new chore)."""
        key = audit_runner._fp_finding_key(_finding())
        results, commands = _run_loop(
            tmp_path, [_entry(_finding())],
            existing=[_chore_item("CHORE-9", key)],
        )
        assert _create_commands(commands) == []
        assert results["chore_items"] == []
        assert results["chores_created"] == 0
        assert results["chores_reused"] == [{"id": "CHORE-9", "key": key}]
        assert results["fp_screen_results"][0]["tracked_chore_id"] == "CHORE-9"

    def test_repeated_runs_are_idempotent(self, tmp_path):
        """AC1: two consecutive audits yield the same chore set."""
        first, _ = _run_loop(tmp_path, [_entry(_finding())])
        assert first["chores_created"] == 1
        # Second run sees exactly the chore the first one created.
        key = audit_runner._fp_finding_key(_finding())
        second, commands = _run_loop(
            tmp_path, [_entry(_finding())],
            existing=[_chore_item(first["chore_items"][0]["id"], key)],
        )
        assert _create_commands(commands) == []
        assert second["chores_reused"][0]["id"] == "CHORE-NEW"
        assert second["chore_items"] == []

    def test_lookup_is_keyed_on_rule_file_and_line(self, tmp_path):
        """AC1: a different line number is a different finding."""
        key = audit_runner._fp_finding_key(_finding(line=1))
        results, commands = _run_loop(
            tmp_path, [_entry(_finding(line=99))],
            existing=[_chore_item("CHORE-9", key)],
        )
        assert len(_create_commands(commands)) == 1
        assert results["chores_created"] == 1

    def test_terminal_chore_is_not_reused(self, tmp_path):
        """AC1: a closed/completed chore no longer tracks a live finding."""
        key = audit_runner._fp_finding_key(_finding())
        results, commands = _run_loop(
            tmp_path, [_entry(_finding())],
            existing=[_chore_item("CHORE-9", key, status="completed")],
        )
        assert len(_create_commands(commands)) == 1
        assert results["chores_created"] == 1

    def test_non_chore_item_with_matching_text_is_ignored(self, tmp_path):
        """AC1: dedup only reuses chores — never an unrelated item's text."""
        key = audit_runner._fp_finding_key(_finding())
        _results, commands = _run_loop(
            tmp_path, [_entry(_finding())],
            existing=[_chore_item("BUG-1", key, issue_type="bug")],
        )
        assert len(_create_commands(commands)) == 1

    def test_chore_carries_dedup_marker_and_waiver_recipe(self, tmp_path):
        """AC1/AC3: the chore carries the key marker and the acceptance recipe."""
        key = audit_runner._fp_finding_key(_finding(line=7))
        _, commands = _run_loop(tmp_path, [_entry(_finding(line=7))])
        create = " ".join(_create_commands(commands)[0])
        assert f"{audit_runner.CFP_CHORE_KEY_PREFIX} {key}" in create
        assert "src/bad.py:7" in create
        assert "audit_waiver.py add TEST-1" in create
        assert audit_runner.FP_CHORE_ANNOTATION in create

    def test_lookup_failure_creates_a_chore(self, tmp_path):
        """AC1 fail-safe: an unreadable worklog creates tracking, never loses it."""
        results, commands = _run_loop(
            tmp_path, [_entry(_finding())], list_error=True,
        )
        assert len(_create_commands(commands)) == 1
        assert results["chores_created"] == 1


# ---------------------------------------------------------------------------
# AC3: waiver recording and filtering
# ---------------------------------------------------------------------------


class TestAuditWaivers:
    def test_waived_finding_creates_no_chore(self, tmp_path):
        """AC3: an accepted false positive never gets a tracking chore."""
        results, commands = _run_loop(
            tmp_path, [_entry(_finding())], waivers=[_waiver()],
        )
        assert _create_commands(commands) == []
        assert results["chores_created"] == 0
        assert [w["key"] for w in results["waived_findings"]] == [
            audit_runner._fp_finding_key(_finding()),
        ]
        assert results["fp_screen_results"][0]["waived"] is True

    def test_file_level_waiver_matches_any_line(self, tmp_path):
        """AC3: a waiver without a line accepts the rule for the whole file."""
        results, commands = _run_loop(
            tmp_path, [_entry(_finding(line=42))],
            waivers=[_waiver(line=0)],
        )
        assert _create_commands(commands) == []
        assert results["waived_findings"]

    def test_waiver_for_another_rule_does_not_apply(self, tmp_path):
        """AC3: waivers are per rule+file — never a blanket suppression."""
        results, commands = _run_loop(
            tmp_path, [_entry(_finding(code="F841"))],
            waivers=[_waiver(rule="E402")],
        )
        assert len(_create_commands(commands)) == 1
        assert results["waived_findings"] == []

    def test_waiver_for_another_file_does_not_apply(self, tmp_path):
        _results, commands = _run_loop(
            tmp_path, [_entry(_finding(file="src/bad.py"))],
            waivers=[_waiver(file="src/other.py")],
        )
        assert len(_create_commands(commands)) == 1

    def test_parse_waivers_from_comment_markers(self):
        """AC3: the waiver transport is a comment marker (wl CLI limitation)."""
        comments = [
            {"comment": (
                "Accepting this one.\n"
                'AUDIT-FP-WAIVER: {"rule": "F401", "file": "src/app.py", '
                '"line": 12, "justification": "re-export", "author": "Map"}'
            )},
            {"comment": "AUDIT-FP-WAIVER: not-json"},
            {"comment": "AUDIT-FP-WAIVER: {\"rule\": \"F401\"}"},
        ]
        waivers = audit_runner._parse_audit_waivers(comments)
        assert waivers == [{
            "linter": "ruff", "rule": "F401", "file": "src/app.py", "line": 12,
            "justification": "re-export", "author": "Map",
        }]

    def test_collect_waivers_also_reads_audit_waiver_findings(self):
        """AC3: a store that persists ``auditWaiver.findings`` is honoured."""
        wi = {"auditWaiver": {"reason": "gap", "findings": [
            {"rule": "F401", "file": "src/app.py", "line": 3},
        ]}}
        waivers = audit_runner._collect_audit_waivers(wi, [])
        assert [(w["rule"], w["file"], w["line"]) for w in waivers] == [
            ("F401", "src/app.py", 3),
        ]

    def test_apply_audit_waivers_is_idempotent(self):
        entry = _entry(_finding())
        assert audit_runner._apply_audit_waivers([entry], [_waiver()]) == 1
        assert audit_runner._apply_audit_waivers([entry], [_waiver()]) == 0

    def test_waived_critical_finding_is_not_blocking(self):
        """AC3: an accepted finding is excluded from blocking checks."""
        finding = _finding(severity="high", code="E402")
        entry = _entry(finding, classification="uncertain", remediable=False)
        entry["waived"] = True
        assert audit_runner._effective_blocking_findings([finding], [entry]) == []
        blocked, _reason = audit_runner._has_phase1_blocking_issues(
            [finding], [], fp_screen_results=[entry],
        )
        assert blocked is False

    def test_unwaived_critical_finding_still_blocks(self):
        """Constraint: uncertain/genuine findings keep blocking unchanged."""
        finding = _finding(severity="high", code="E402")
        entry = _entry(finding, classification="uncertain", remediable=False)
        blocked, reason = audit_runner._has_phase1_blocking_issues(
            [finding], [], fp_screen_results=[entry],
        )
        assert blocked is True
        assert audit_runner.FP_CANDIDATE_ANNOTATION in reason


# ---------------------------------------------------------------------------
# AC2/AC4: producer-review flag gating
# ---------------------------------------------------------------------------


def _ctx(*, entries=None, remediation=None, wi=None, cq_findings=None,
         prior=None):
    """Minimal ``_AuditContext`` for producer-review decision unit tests."""
    return audit_runner._AuditContext(
        issue_id="TEST-1", persist=False, timeout=None, parent_timeout=None,
        pi_bin="pi", model=None, model_source="local",
        runner=mock.MagicMock(), json_mode=False, debug_log=None, force=True,
        worklog_dir=None, batch_phase2=False, green_run=None,
        audit_children=False, max_child_audits=None, run_tests=False,
        wi=wi if wi is not None else {"id": "TEST-1", "title": "t"},
        prior_audit=prior,
        fp_screen_results=list(entries or []),
        remediation_results=dict(remediation or {}),
        cq_findings=list(cq_findings or []),
    )


def _tracked_entry() -> dict:
    entry = _entry(_finding())
    entry["tracked_chore_id"] = "CHORE-9"
    return entry


class TestProducerReviewGating:
    def test_tracked_cfp_pass_sets_no_flag(self):
        """AC2: only tracked CFPs → no flag (release gate not re-armed)."""
        decision = audit_runner._resolve_producer_review_decision(
            _ctx(entries=[_tracked_entry()]), "yes",
        )
        assert decision["action"] == audit_runner.PRODUCER_REVIEW_UNTOUCHED
        assert "tracked" in decision["reason"]

    def test_waived_cfp_pass_sets_no_flag(self):
        """AC3: only accepted CFPs → no flag."""
        entry = _entry(_finding())
        entry["waived"] = True
        decision = audit_runner._resolve_producer_review_decision(
            _ctx(entries=[entry]), "yes",
        )
        assert decision["action"] == audit_runner.PRODUCER_REVIEW_UNTOUCHED

    def test_new_chore_still_surfaces_the_producer_once(self):
        """AC2: a new chore is a genuine decision → flag once."""
        decision = audit_runner._resolve_producer_review_decision(
            _ctx(entries=[_tracked_entry()], remediation={"chores_created": 1}),
            "yes",
        )
        assert decision["action"] == audit_runner.PRODUCER_REVIEW_REQUIRED
        assert "new candidate false-positive chore" in decision["reason"]

    def test_untracked_cfp_still_flags(self):
        """AC2: an untracked CFP is not yet decided → flag (fail safe)."""
        decision = audit_runner._resolve_producer_review_decision(
            _ctx(entries=[_entry(_finding())]), "yes",
        )
        assert decision["action"] == audit_runner.PRODUCER_REVIEW_REQUIRED
        assert "not yet tracked" in decision["reason"]

    def test_uncertain_finding_still_flags(self):
        """Constraint: uncertain findings keep their producer decision."""
        entry = _entry(_finding(severity="high"), classification="uncertain")
        decision = audit_runner._resolve_producer_review_decision(
            _ctx(entries=[entry]), "yes",
        )
        assert decision["action"] == audit_runner.PRODUCER_REVIEW_REQUIRED
        assert "uncertain" in decision["reason"]

    def test_clean_first_pass_still_flags(self):
        """SA-0MVR9B1UH002BUST AC2 preserved: an unreviewed pass is flagged."""
        decision = audit_runner._resolve_producer_review_decision(_ctx(), "yes")
        assert decision["action"] == audit_runner.PRODUCER_REVIEW_REQUIRED

    def test_tracker_chore_pass_never_flags(self):
        """AC4: an audit-generated tracking chore is not a producer decision."""
        wi = {
            "id": "CHORE-1",
            "issueType": "chore",
            "title": f"ruff F841 in src/bad.py:1 — {audit_runner.FP_CHORE_ANNOTATION}",
            "description": "Tracking.",
        }
        decision = audit_runner._resolve_producer_review_decision(
            _ctx(wi=wi), "yes",
        )
        assert decision["action"] == audit_runner.PRODUCER_REVIEW_UNTOUCHED
        assert "tracker" in decision["reason"]

    def test_non_tracker_chore_still_flags(self):
        """Guard: an ordinary chore is still a normal reviewed item."""
        wi = {"id": "CHORE-1", "issueType": "chore", "title": "Tidy imports",
              "description": "nothing to see"}
        decision = audit_runner._resolve_producer_review_decision(
            _ctx(wi=wi), "yes",
        )
        assert decision["action"] == audit_runner.PRODUCER_REVIEW_REQUIRED

    def test_preserved_review_beats_the_tracking_shortcut(self):
        """A producer review of unchanged content stays preserved (AC1)."""
        prior = {"readyToClose": True, "fingerprint": "fp",
                 "currentFingerprint": "fp"}
        decision = audit_runner._resolve_producer_review_decision(
            _ctx(entries=[_tracked_entry()], prior=prior), "yes",
        )
        assert decision["action"] == audit_runner.PRODUCER_REVIEW_PRESERVED


# ---------------------------------------------------------------------------
# AC2 end to end: a passing audit of a tracked CFP writes no flag
# ---------------------------------------------------------------------------

_ITEM = {"id": "TEST-1", "title": "Tracked false positive",
         "description": ""}
_YES_REPORT = "Ready to close: Yes\n\n## Summary\nNothing to do."


def _issue_runner(updates: list, *, existing_chores: list[dict]):
    """Mock ``wl`` runner for an end-to-end ``cmd_issue`` run."""
    created: list[str] = ["CHORE-1"]

    def _side(cmd):
        joined = " ".join(cmd)
        if " list " in f" {joined} ":
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"success": True,
                                   "workItems": list(existing_chores)}),
                stderr="",
            )
        if " create " in f" {joined} ":
            chore_id = created.pop(0)
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"success": True,
                                   "workItem": {"id": chore_id}}),
                stderr="",
            )
        if "audit-show" in joined:
            return audit_show_response(ready_to_close=None)
        if "update" in joined:
            updates.append(list(cmd))
            return SimpleNamespace(
                returncode=0, stdout=json.dumps({"success": True}), stderr="",
            )
        if "show" in joined and "--children" in joined:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({
                    "success": True,
                    "workItem": dict(_ITEM, status="completed",
                                     stage="in_review"),
                    "children": [],
                }),
                stderr="",
            )
        if "show" in joined:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({
                    "success": True,
                    "workItem": dict(_ITEM, status="completed",
                                     stage="in_review",
                                     needsProducerReview=False),
                }),
                stderr="",
            )
        return SimpleNamespace(
            returncode=0, stdout=json.dumps({"success": True}), stderr="",
        )

    runner = mock.MagicMock()
    runner.side_effect = stateful_wl_side_effect(_side)
    return runner


def _run_issue(updates: list, *, existing_chores: list[dict]) -> int:
    finding = _finding()
    runner = _issue_runner(updates, existing_chores=existing_chores)
    with (
        mock.patch.object(audit_runner, "_phase_merge_gate",
                          return_value=None),
        mock.patch.object(audit_runner, "_assemble_issue_report",
                          return_value=_YES_REPORT),
        mock.patch.object(audit_runner, "_resolve_code_quality_scope",
                          return_value=["src/bad.py"]),
        mock.patch.object(audit_runner, "_screen_ruff_findings",
                          return_value=[_entry(finding)]),
        mock.patch(
            "code_review.scripts.code_quality.run_code_quality",
            return_value={"success": True, "findings": [finding],
                          "fixes_applied": 0},
        ),
    ):
        return audit_runner.cmd_issue(
            "TEST-1", persist=False, force=True, runner=runner,
            batch_drain=False,
        )


def _terminal_update(updates: list) -> list[str]:
    assert updates, "expected a terminal wl update"
    cmd = updates[-1]
    if cmd[:1] == ["wl"] and len(cmd) >= 3 and cmd[1] == "--worklog-dir":
        cmd = ["wl"] + cmd[3:]
    return cmd


class TestPassingAuditFlagGatingEndToEnd:
    def test_tracked_cfp_pass_writes_no_producer_flag(self):
        """AC2: the tracked-CFP pass leaves needsProducerReview untouched."""
        key = audit_runner._fp_finding_key(_finding())
        updates: list = []
        rc = _run_issue(updates, existing_chores=[_chore_item("CHORE-9", key)])
        assert rc == 0
        cmd = _terminal_update(updates)
        assert "--needs-producer-review" not in cmd
        assert cmd == [
            "wl", "update", "TEST-1",
            "--status", "completed", "--stage", "in_review",
            "--assignee", "", "--json",
        ]

    def test_first_run_creates_one_chore_and_flags_once(self):
        """AC1/AC2: the first run creates the chore and surfaces the decision."""
        updates: list = []
        rc = _run_issue(updates, existing_chores=[])
        assert rc == 0
        cmd = _terminal_update(updates)
        assert cmd[cmd.index("--needs-producer-review") + 1] == "yes"
