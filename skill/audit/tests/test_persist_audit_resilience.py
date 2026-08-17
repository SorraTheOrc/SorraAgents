"""Tests for resilient audit persistence (SA-0MSF3RXUB000NLOI, P8).

Don't lose a completed audit run to a final JSON parse failure in the
verdict persistence path.

Covers:

- AC1: When the final ``wl update --audit-text`` persistence step fails with
  a JSON parse error against the assembled verdict content, the runner
  persists a *usable* audit instead of leaving the 43-char stub
  (``Audit result persisted via persist_audit.py``):
    * repair pass — broken JSON fragments in the report are salvaged and the
      repaired report is retried once (bounded, no model calls);
    * fallback — if the retry still fails, a compact markdown notice with a
      clear failure notice (naming the target work-item ID so the identity /
      readback guards still pass) is persisted.
- AC2: The repair never re-runs the full audit pipeline; any model re-ask is
  bounded at ≤1 additional model call; tests assert the actual retry count is
  within that bound.
- AC3: Existing persistence behaviour (original report persisted verbatim,
  identity guard, stage preservation) is unchanged.

All tests use a mocked runner — no real pi/wl calls.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

# Ensure repo root is on sys.path so the audit scripts are importable.
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from skill.audit.scripts.persist_audit import (
    PERSIST_CONTENT_INVALID,
    persist_audit,
)
from skill.audit.tests.wl_helpers import make_stateful_runner

# The 43-char stub observed in the Phase 2 failure: the summary string passed
# to ``wl audit-set`` when the ``wl update --audit-text`` replacement failed.
_STUB = "Audit result persisted via persist_audit.py"
assert len(_STUB) == 43

# A malformed JSON verdict fragment (unterminated string, no closing brace).
_BROKEN_FRAGMENT = '{"verdict": "met", "evidence": "line1'


def _proc(returncode: int = 0, stdout: str = "", stderr: str = "") -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _show_proc(issue_id: str = "SA-TEST", stage: str = "plan_complete") -> SimpleNamespace:
    return _proc(stdout=json.dumps({
        "success": True,
        "workItem": {"id": issue_id, "stage": stage},
    }))


def _audit_text_of(cmd: list[str]) -> str:
    return cmd[cmd.index("--audit-text") + 1]


def _make_update_gate(fail_when: object) -> object:
    """Build a stateful runner for the persist_audit wl calls.

    *fail_when* is a callable ``predicate(text) -> bool`` deciding whether a
    ``wl update --audit-text <text>`` attempt must fail (return rc=1 with a
    JSON parse error). All other wl calls succeed.
    """
    calls: list[list[str]] = []

    def fake_runner(cmd, **kwargs):
        calls.append(list(cmd))
        cmd_str = " ".join(cmd)
        if "audit-set" in cmd_str:
            return _proc(stdout='{"success": true}')
        if "--audit-text" in cmd_str:
            text = _audit_text_of(cmd)
            if fail_when(text):
                return _proc(
                    returncode=1,
                    stderr="Expected ',' or '}' after property value in JSON at position 28",
                )
            return _proc(stdout='{"success": true}')
        if "show" in cmd_str:
            return _show_proc()
        return _proc(stdout='{"success": true}')

    fake_runner.calls = calls  # type: ignore[attr-defined]
    return fake_runner


# ---------------------------------------------------------------------------
# AC1 — repair pass: broken JSON fragments are salvaged and retried once
# ---------------------------------------------------------------------------

class TestRepairPass:
    """AC1 tier 1: a broken JSON fragment is repaired and the retry succeeds."""

    def test_broken_json_fragment_is_repaired_and_retried(self):
        """A report whose verdict JSON failed validation is repaired and
        persisted — the persisted text is NOT the 43-char stub."""
        report = (
            "Audit of SA-TEST\n"
            "Ready to close: Yes\n"
            "\n"
            "| 1 | AC1 | met | " + _BROKEN_FRAGMENT + " |\n"
        )
        runner = _make_update_gate(lambda text: _BROKEN_FRAGMENT in text)

        rc = persist_audit("SA-TEST", report, runner=runner)

        assert rc == 0
        audit_text_calls = [c for c in runner.calls if "--audit-text" in c]  # type: ignore[attr-defined]
        # Original attempt failed → exactly one repair retry that succeeded.
        assert len(audit_text_calls) == 2
        persisted = _audit_text_of(audit_text_calls[-1])
        assert _BROKEN_FRAGMENT not in persisted  # malformed JSON removed
        assert "SA-TEST" in persisted  # identity guard still satisfiable
        assert "Audit persistence notice" in persisted  # clear failure notice
        assert _STUB not in persisted  # not the 43-char stub
        # The verdict row itself survives (per-AC rows are not fabricated).
        assert "| 1 | AC1 | met |" in persisted

    def test_repaired_persist_still_preserves_ready_to_close_first_line(self):
        """The repaired report keeps the wl-required first line so the retry
        passes wl's audit first-line validation."""
        report = (
            "Ready to close: Yes\n"
            "\n"
            "| 1 | AC1 | met | " + _BROKEN_FRAGMENT + " |\n"
        )
        runner = _make_update_gate(lambda text: _BROKEN_FRAGMENT in text)

        rc = persist_audit("SA-TEST", report, runner=runner)

        assert rc == 0
        persisted = _audit_text_of([c for c in runner.calls if "--audit-text" in c][-1])  # type: ignore[attr-defined]
        assert persisted.splitlines()[0] == "Ready to close: Yes"

    def test_clean_report_persisted_verbatim_in_one_attempt(self):
        """A report with no broken JSON is persisted unchanged on the first
        attempt (no repair, no extra calls)."""
        report = (
            "Audit of SA-TEST\n"
            "Ready to close: Yes\n"
            "All acceptance criteria met.\n"
        )
        runner = _make_update_gate(lambda text: False)

        rc = persist_audit("SA-TEST", report, runner=runner)

        assert rc == 0
        audit_text_calls = [c for c in runner.calls if "--audit-text" in c]  # type: ignore[attr-defined]
        assert len(audit_text_calls) == 1
        assert _audit_text_of(audit_text_calls[0]) == report


# ---------------------------------------------------------------------------
# AC1 — fallback: compact markdown notice with a clear failure notice
# ---------------------------------------------------------------------------

class TestFallbackNotice:
    """AC1 tier 3: when even the repaired report is rejected, a usable
    markdown fallback (with a clear failure notice) is persisted instead of
    the 43-char stub."""

    def test_fallback_notice_persisted_when_retry_fails(self):
        """The runner always rejects the full report → persist_audit returns
        PERSIST_CONTENT_INVALID and the persisted text is a fallback notice
        naming the work item with a clear failure notice."""
        report = (
            "Audit of SA-TEST\n"
            "Ready to close: No\n"
            "\n"
            "| 1 | AC1 | unmet | " + _BROKEN_FRAGMENT + " |\n"
        )
        runner = _make_update_gate(
            lambda text: "| 1 |" in text,
        )

        rc = persist_audit("SA-TEST", report, runner=runner)

        assert rc == PERSIST_CONTENT_INVALID
        audit_text_calls = [c for c in runner.calls if "--audit-text" in c]  # type: ignore[attr-defined]
        assert len(audit_text_calls) == 3  # original + repaired + fallback
        persisted = _audit_text_of(audit_text_calls[-1])
        assert persisted.splitlines()[0] == "Ready to close: No"  # verdict kept
        assert "SA-TEST" in persisted  # identity guard satisfiable
        assert "Audit persistence notice" in persisted  # clear failure notice
        assert _STUB not in persisted  # never the 43-char stub

    def test_fallback_respected_stage_preservation(self):
        """The fallback update still preserves the current work-item stage."""
        report = (
            "Audit of SA-TEST\n"
            "Ready to close: Yes\n"
            "\n"
            "| 1 | AC1 | met | " + _BROKEN_FRAGMENT + " |\n"
        )
        runner = _make_update_gate(
            lambda text: "| 1 |" in text,
        )

        rc = persist_audit(
            "SA-TEST", report, runner=runner,
            worklog_dir="/explicit/.worklog",
        )

        assert rc == PERSIST_CONTENT_INVALID
        audit_text_calls = [c for c in runner.calls if "--audit-text" in c]  # type: ignore[attr-defined]
        for call in audit_text_calls:
            assert call[1:3] == ["--worklog-dir", "/explicit/.worklog"]
            assert "--stage" in call and "plan_complete" in call


# ---------------------------------------------------------------------------
# AC1 — nothing persists when every attempt (incl. fallback) fails
# ---------------------------------------------------------------------------

class TestTotalPersistenceFailure:
    """All update attempts fail → persist_audit returns non-zero and prints
    diagnostics."""

    def test_all_attempts_fail_returns_1(self, capsys):
        report = (
            "Audit of SA-TEST\n"
            "Ready to close: Yes\n"
            "\n"
            "| 1 | AC1 | met | " + _BROKEN_FRAGMENT + " |\n"
        )
        runner = _make_update_gate(lambda text: True)

        rc = persist_audit("SA-TEST", report, runner=runner)

        assert rc == 1
        err = capsys.readouterr().err
        assert "audit-text" in err.lower() or "failed" in err.lower()
        assert len([c for c in runner.calls if "--audit-text" in c]) == 3  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# AC2 — bounded re-ask (≤1 additional model call) at the runner level
# ---------------------------------------------------------------------------

class TestBoundedReask:
    """AC2: the repair triggers at most one additional model call and never
    re-runs the full audit pipeline."""

    def _baseline_pi_calls(self, monkeypatch, capsys) -> int:
        """Run cmd_issue with a healthy persist; return the pi call count."""
        from skill.audit.scripts import audit_runner as ar_module

        calls: list[str] = []

        def counting_call_pi(prompt, model="x", pi_bin="x", **kwargs):
            calls.append(prompt)
            return _fake_pi_result()

        monkeypatch.setattr(ar_module, "_call_pi", counting_call_pi)
        monkeypatch.setattr(
            ar_module, "persist_audit",
            lambda issue_id, report_text, **kwargs: 0,
        )
        rc = ar_module.cmd_issue("SA-FIXTURE-001", runner=self._fixture_runner())
        assert rc == 0
        return len(calls)

    def _fixture_runner(self):
        def fake_runner(cmd, **kwargs):
            cmd_list = list(cmd)
            if "audit-show" in cmd_list:
                return _proc(stdout=json.dumps({
                    "success": True,
                    "workItemId": "SA-FIXTURE-001",
                    "audit": {
                        "workItemId": "SA-FIXTURE-001",
                        "auditedAt": "2026-07-20T10:00:00.000Z",
                        "rawOutput": (
                            "Audit report for work item SA-FIXTURE-001\n"
                            "Ready to close: Yes\n\n## Summary\nOK."
                        ),
                    },
                }))
            return _proc(stdout=json.dumps(_load_fixture("wi_with_numbered_ac.json")))
        return make_stateful_runner(fake_runner)

    def test_reask_uses_at_most_one_additional_model_call(self, monkeypatch, capsys):
        """When the first persist attempt returns PERSIST_CONTENT_INVALID, the
        runner re-asks the model exactly once (bounded ≤1) and then persists
        successfully. The full audit pipeline is NOT re-run."""
        from skill.audit.scripts import audit_runner as ar_module

        baseline = self._baseline_pi_calls(monkeypatch, capsys)
        assert baseline >= 1

        calls: list[str] = []

        def counting_call_pi(prompt, model="x", pi_bin="x", **kwargs):
            calls.append(prompt)
            return _fake_pi_result()

        monkeypatch.setattr(ar_module, "_call_pi", counting_call_pi)

        persist_results = iter([PERSIST_CONTENT_INVALID, 0])
        reask_payloads: list[str] = []

        def fake_persist(issue_id, report_text, **kwargs):
            # First call carries the original report (content-invalid); the
            # second carries a report the re-ask helped produce.
            reask_payloads.append(report_text)
            return next(persist_results)

        monkeypatch.setattr(ar_module, "persist_audit", fake_persist)

        rc = ar_module.cmd_issue("SA-FIXTURE-001", runner=self._fixture_runner())

        assert rc == 0
        # The re-ask added exactly one model call over the healthy baseline.
        assert len(calls) == baseline + 1
        # persist_audit was called exactly twice (original + post-re-ask).
        assert len(reask_payloads) == 2

    def test_no_reask_when_persist_succeeds(self, monkeypatch, capsys):
        """A healthy persist (rc 0) never triggers the re-ask."""
        from skill.audit.scripts import audit_runner as ar_module

        calls: list[str] = []

        def counting_call_pi(prompt, model="x", pi_bin="x", **kwargs):
            calls.append(prompt)
            return _fake_pi_result()

        monkeypatch.setattr(ar_module, "_call_pi", counting_call_pi)
        monkeypatch.setattr(
            ar_module, "persist_audit",
            lambda issue_id, report_text, **kwargs: 0,
        )

        rc = ar_module.cmd_issue("SA-FIXTURE-001", runner=self._fixture_runner())

        assert rc == 0
        # No re-ask: the count is exactly the healthy pipeline count.
        assert len(calls) == self._baseline_pi_calls(monkeypatch, capsys)

    def test_run_succeeds_when_reask_cannot_recover(self, monkeypatch, capsys):
        """When the single re-ask also fails, the run still succeeds because
        persist_audit already persisted the fallback notice (usable content)."""
        from skill.audit.scripts import audit_runner as ar_module

        def unparseable_call_pi(prompt, model="x", pi_bin="x", **kwargs):
            # The re-ask gets a response that cannot be parsed as a verdict
            # array → _reask_verdict_array_once returns None.
            return {"verdict": "met", "evidence": "", "extracted_text": ""}

        monkeypatch.setattr(ar_module, "_call_pi", unparseable_call_pi)
        persist_calls: list[str] = []

        def fake_persist(issue_id, report_text, **kwargs):
            persist_calls.append(report_text)
            return PERSIST_CONTENT_INVALID

        monkeypatch.setattr(ar_module, "persist_audit", fake_persist)

        rc = ar_module.cmd_issue("SA-FIXTURE-001", runner=self._fixture_runner())

        assert rc == 0  # fallback content already persisted → success
        # persist_audit called exactly once (the re-ask returned None, so no
        # second persist attempt); a warning was surfaced.
        assert len(persist_calls) == 1
        err = capsys.readouterr().err
        assert "fallback" in err.lower()

    def test_dict_evidence_completes_and_persists(self, monkeypatch):
        """SA-0MSKM2LSP006L0K8 AC3: a full cmd_issue run whose Phase 2
        parent deep call returns structured dict evidence completes (rc 0)
        and persists a report carrying the salvaged evidence string.

        The first persist attempt is rejected (PERSIST_CONTENT_INVALID) so
        the bounded verdict re-ask runs against ac_results that still carry
        the dict evidence — pre-fix this crashed at the re-emit prompt's
        evidence slice ((dict)[:200]) and no report was persisted; post-fix
        the evidence is normalized to a string everywhere.
        """
        from skill.audit.scripts import audit_runner as ar_module

        def fake_pi_and_maybe_log(issue_id, context, prompt, **kwargs):
            if context == "phase2_deep":
                batch = [{
                    "index": 0, "verdict": "met",
                    "evidence": {"file": "src/app.py", "line": 42,
                                  "note": "verified in code"},
                }]
                payload = json.dumps(batch)
                return {"verdict": "met", "evidence": payload,
                        "extracted_text": payload}
            return _fake_pi_result()

        monkeypatch.setattr(
            ar_module, "_call_pi_and_maybe_log", fake_pi_and_maybe_log)
        persist_results = iter([PERSIST_CONTENT_INVALID, 0])
        persisted: list[str] = []

        def fake_persist(issue_id, report_text, **kwargs):
            persisted.append(report_text)
            return next(persist_results)

        monkeypatch.setattr(ar_module, "persist_audit", fake_persist)

        rc = ar_module.cmd_issue("SA-FIXTURE-001", runner=self._fixture_runner())

        assert rc == 0
        # Original + post-re-ask persist attempts both completed.
        assert len(persisted) == 2
        # The dict evidence was normalized to a string and survived Phase 2
        # merge + report assembly + the re-ask prompt (no crash, no drop).
        assert "src/app.py" in persisted[0]


def _fake_pi_result(ac_count: int = 3, verdict: str = "met") -> dict:
    """Build a mock Pi result that returns a valid JSON array for AC review."""
    items = []
    for i in range(ac_count):
        items.append({"index": i, "verdict": verdict, "evidence": f"file:{i}.py:1 — {verdict}"})
    return {"verdict": "met", "evidence": json.dumps(items), "extracted_text": json.dumps(items)}


def _load_fixture(name: str) -> dict:
    fixture_path = _REPO_ROOT / "tests" / "fixtures" / "audit" / name
    with open(fixture_path, encoding="utf-8") as f:
        return json.load(f)
