"""Phantom test-failure suppression in the triage helper.

A mid-run fast-forward of the main checkout makes a suite report failures that
exist in no single commit (tests from X, source from Y). Minting a critical
``test-failure`` work item from that mixed-tree output is noise
(WL-0MVSPZ1FH000RWET AC3).

``check_or_create`` suppresses a payload carrying an **explicit** phantom
signal — a ``state_changed_during_run`` notice, a ``notice_code`` in
``PHANTOM_NOTICE_CODES``, or ``phantom: true`` — and returns
``suppressed: True`` with the reason. Suppression is deliberately conservative:
a genuine failure on a stable checkout still creates/updates an item exactly as
before.
"""
from __future__ import annotations

import json

import pytest
import triage.scripts.check_or_create as cc


@pytest.fixture()
def isolated_wl(monkeypatch):
    """Replace every wl-mutating call and record what triage tried to do."""
    state = {"list_calls": 0, "create_calls": 0, "comment_calls": 0}

    def fake_list():
        state["list_calls"] += 1
        return []

    def fake_create(title, body, parent_id=None, **kwargs):
        state["create_calls"] += 1
        return {"id": "WL-CREATED", "workItem": {"id": "WL-CREATED"}}

    def fake_comment(issue_id, comment):
        state["comment_calls"] += 1

    monkeypatch.setattr(cc, "list_critical_issues", fake_list)
    monkeypatch.setattr(cc, "create_issue", fake_create)
    monkeypatch.setattr(cc, "add_comment", fake_comment)
    return state


# ---------------------------------------------------------------------------
# detect_phantom()
# ---------------------------------------------------------------------------


def test_detect_phantom_recognises_state_changed_notice():
    facts = cc.detect_phantom(
        {"state_changed_during_run": {"notice_code": "state_changed_during_run"}}
    )
    assert facts is not None
    assert facts["notice_code"] == "state_changed_during_run"


def test_detect_phantom_recognises_notice_code_field():
    facts = cc.detect_phantom({"notice_code": "state_changed_during_run"})
    assert facts is not None
    assert facts["notice_code"] == "state_changed_during_run"


def test_detect_phantom_recognises_explicit_flag():
    facts = cc.detect_phantom({"phantom": True, "phantom_reason": "moving tree"})
    assert facts is not None
    assert "moving tree" in facts["reason"]


def test_detect_phantom_ignores_a_normal_failure():
    """No explicit signal means a normal, triageable failure."""
    assert cc.detect_phantom({"test_name": "test_x", "stdout_excerpt": "boom"}) is None
    assert cc.detect_phantom({"notice_code": "suite_timeout"}) is None


# ---------------------------------------------------------------------------
# check_or_create() suppression
# ---------------------------------------------------------------------------


def test_state_changed_verdict_is_suppressed_without_any_wl_call(isolated_wl):
    result = cc.check_or_create(
        {
            "test_name": "send-to-pi > settings",
            "state_changed_during_run": {"notice_code": "state_changed_during_run"},
        }
    )

    assert result["suppressed"] is True
    assert result["created"] is False
    assert result["issueId"] is None
    assert result["notice_code"] == "state_changed_during_run"
    assert "suppressed_phantom" in result["reason"]
    # Suppression is decided before the candidate scan and before creation.
    assert isolated_wl["list_calls"] == 0
    assert isolated_wl["create_calls"] == 0


def test_explicit_phantom_flag_is_suppressed(isolated_wl):
    result = cc.check_or_create(
        {"test_name": "test_x", "phantom": True, "phantom_reason": "mixed tree"}
    )
    assert result["suppressed"] is True
    assert "mixed tree" in result["reason"]
    assert isolated_wl["create_calls"] == 0


def test_genuine_failure_still_creates_an_item(isolated_wl):
    result = cc.check_or_create(
        {"test_name": "tests/test_real.py::test_real", "stdout_excerpt": "assert 1 == 2"}
    )

    assert result.get("suppressed") is not True
    assert result["created"] is True
    assert result["issueId"] == "WL-CREATED"
    assert isolated_wl["create_calls"] == 1


def test_suppression_event_is_emitted(capsys, isolated_wl):
    cc.check_or_create(
        {"test_name": "test_x", "state_changed_during_run": {"notice_code": "x"}}
    )
    err = capsys.readouterr().err
    events = [json.loads(line) for line in err.splitlines() if line.startswith("{")]
    assert any(
        e.get("event") == "triage.issue.suppressed" for e in events
    ), events
