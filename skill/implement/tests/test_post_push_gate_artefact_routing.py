"""Gate/cache-artefact routing for a post-push verification refusal.

Regression coverage for SA-0MWOTZ17K005RH8P. ``_route_post_push_failure_to_triage``
used to file **every** refusal — including a ``verdict=missing`` cache miss
that carries no failing test — through the same helper path, producing a
critical ``[test-failure] post-push full-suite verification: no valid
full-suite cache entry — failing test`` item whose ACs no agent can satisfy.

The fix keys the routing on the verification verdict:

* ``missing`` → ``category="gate_artefact"`` (a ``[gate-artefact]`` item with an
  agent-satisfiable remediation);
* ``red`` → ``category="test_failure"`` (unchanged: a critical
  ``[test-failure]`` item naming the real failing test from suite output).

The refusal itself stays fail-closed in both cases.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
for _path in (str(REPO_ROOT), str(REPO_ROOT / "skill")):
    if _path not in sys.path:
        sys.path.append(_path)

from skill.implement.scripts import implement


class _FakeProc:
    returncode = 0
    stdout = '{"created": false, "matchedId": "SA-EXISTING"}'
    stderr = ""


def _capture_triage_payload(monkeypatch) -> dict:
    """Patch the triage subprocess call and return the captured JSON payload."""
    captured: dict = {}

    def fake_run(cmd, **_kwargs):
        # cmd == [python, <helper path>, json.dumps(payload)]
        captured["payload"] = json.loads(cmd[-1])
        captured["cmd"] = list(cmd)
        return _FakeProc()

    monkeypatch.setattr(implement.subprocess, "run", fake_run)
    return captured


def _missing_verification() -> dict:
    return {
        "verdict": implement.POST_PUSH_VERDICT_MISSING,
        "entries": [
            {
                "command": "pytest -q -r a --disable-warnings",
                "verdict": implement.POST_PUSH_VERDICT_MISSING,
                "cache_root": None,
            }
        ],
        "reason": "",
    }


def _red_verification() -> dict:
    return {
        "verdict": implement.POST_PUSH_VERDICT_RED,
        "entries": [
            {
                "command": "pytest -q -r a --disable-warnings",
                "verdict": implement.POST_PUSH_VERDICT_RED,
                "exit_code": 1,
                "stdout": "FAILED tests/test_demo.py::test_boom - AssertionError\n",
                "stderr": "Traceback (most recent call last):\n  ...\n",
            }
        ],
        "reason": "",
    }


# ---------------------------------------------------------------------------
# _post_push_failure_test_name
# ---------------------------------------------------------------------------


def test_red_verification_names_the_failing_test_from_output():
    """AC3: a red entry still names the failing test from its suite output."""
    assert (
        implement._post_push_failure_test_name(_red_verification())
        == "tests/test_demo.py::test_boom"
    )


def test_missing_verification_is_named_by_the_gate_signature():
    """A cache miss has no failing test, so it is named by the gate itself."""
    assert implement._post_push_failure_test_name(_missing_verification()) == (
        "post-push full-suite verification: no valid full-suite cache entry"
    )


# ---------------------------------------------------------------------------
# _route_post_push_failure_to_triage
# ---------------------------------------------------------------------------


def test_missing_verdict_is_routed_as_a_gate_artefact(monkeypatch):
    """AC1: a ``verdict=missing`` refusal requests a gate-artefact item."""
    captured = _capture_triage_payload(monkeypatch)

    implement._route_post_push_failure_to_triage(
        _missing_verification(), "abc1234", "SA-WIP"
    )

    payload = captured["payload"]
    assert payload["category"] == "gate_artefact"
    assert payload["test_name"] == (
        "post-push full-suite verification: no valid full-suite cache entry"
    )
    # A remedyable artefact must not couple the released work item as a blocker.
    assert "parent_work_item_id" not in payload
    assert payload["commit_hash"] == "abc1234"


def test_red_verdict_is_routed_as_a_test_failure(monkeypatch):
    """AC3: a ``verdict=red`` refusal keeps the critical test-failure routing."""
    captured = _capture_triage_payload(monkeypatch)

    implement._route_post_push_failure_to_triage(
        _red_verification(), "abc1234", "SA-WIP"
    )

    payload = captured["payload"]
    assert payload["category"] == "test_failure"
    assert payload["test_name"] == "tests/test_demo.py::test_boom"
    assert payload["commit_hash"] == "abc1234"
    # The red failure still blocks the reporting work item.
    assert payload["parent_work_item_id"] == "SA-WIP"


def test_routing_still_forwards_the_helper_result(monkeypatch):
    """The helper's structured report is passed back to the caller."""
    _capture_triage_payload(monkeypatch)

    result = implement._route_post_push_failure_to_triage(
        _missing_verification(), "abc1234", "SA-WIP"
    )

    assert result["success"] is True
    assert result["report"]["matchedId"] == "SA-EXISTING"
