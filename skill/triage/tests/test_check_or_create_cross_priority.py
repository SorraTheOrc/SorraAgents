"""Triage helper: gate/cache-artefact routing and cross-priority dedupe.

Regression coverage for SA-0MWOTZ17K005RH8P. Two defects are pinned here:

1. **Cross-priority dedupe.** ``list_critical_issues()`` used to query only
   ``--priority critical``, so an identical failure signature already filed on
   a ``high``-priority item was invisible and a second (critical) item was
   created. The candidate scan now unions the ``test-failure`` and
   ``gate-artefact`` tags across every priority, keeping "enhance, never
   duplicate".
2. **Gate/cache-artefact routing.** A post-push ``verdict=missing`` refusal is
   not a test regression, so it is filed as a ``[gate-artefact]`` item with an
   agent-satisfiable remediation, never as ``[test-failure] … — failing test``.

A red regression must keep its existing behaviour (critical ``test-failure``
item naming the failing test); that path is covered in the implement skill's
own test suite.
"""

from __future__ import annotations

import json

import triage.scripts.check_or_create as cc

#: The signature the post-push verification helper names a cache-miss refusal.
GATE_SIGNATURE = (
    "post-push full-suite verification: no valid full-suite cache entry"
)


def _high_priority_item(signature: str) -> dict:
    """An existing, non-critical ``test-failure`` item matching *signature*."""
    return {
        "id": "SA-HIGH-EXISTING",
        "title": f"[test-failure] {signature} — failing test",
        "description": f"## Failure Signature\n\n- Test name: {signature}\n",
        "status": "open",
        "priority": "high",
        "updatedAt": "2026-11-20T09:00:00Z",
    }


def _recording_run_wl(candidates, *, created_id="SA-NEW"):
    """Build a ``run_wl`` fake that records every call it receives.

    ``wl list`` returns *candidates* regardless of the tag/status filters (the
    helper is expected to de-duplicate by id); ``wl create`` returns
    ``created_id`` and records the invocation.
    """
    calls = {"list": [], "comment": [], "create": []}

    def fake_run_wl(args):
        if not args:
            return None
        if args[0] == "list":
            calls["list"].append(list(args))
            return json.dumps(
                {"success": True, "count": len(candidates), "workItems": candidates}
            )
        if args[0] == "comment":
            calls["comment"].append(list(args))
            return "{}"
        if args[0] == "create":
            calls["create"].append(list(args))
            return json.dumps({"id": created_id})
        return None

    return calls, fake_run_wl


# ---------------------------------------------------------------------------
# AC2 — cross-priority signature match, no duplicate
# ---------------------------------------------------------------------------


def test_high_priority_signature_is_matched_not_duplicated(monkeypatch):
    """An identical signature on a ``high``-priority item is enhanced, not re-filed."""
    calls, fake_run_wl = _recording_run_wl([_high_priority_item(GATE_SIGNATURE)])
    monkeypatch.setattr(cc, "run_wl", fake_run_wl)

    result = cc.check_or_create(
        {
            "test_name": GATE_SIGNATURE,
            "stdout_excerpt": "no valid full-suite cache entry",
            "category": "gate_artefact",
        }
    )

    assert result["created"] is False
    assert result["matchedId"] == "SA-HIGH-EXISTING"
    assert "matched_existing" in result["reason"]
    # The recurrence is recorded as a comment on the existing item.
    assert len(calls["comment"]) == 1
    assert calls["comment"][0][0] == "comment"
    assert "SA-HIGH-EXISTING" in calls["comment"][0]
    # No second item is created.
    assert calls["create"] == []


def test_candidate_scan_is_not_restricted_to_critical_priority(monkeypatch):
    """The candidate scan queries every priority (no ``--priority`` filter).

    This is the mechanical regression guard: while the scan filtered on
    ``--priority critical`` the ``high``-priority duplicate above was invisible.
    """
    calls, fake_run_wl = _recording_run_wl([])
    monkeypatch.setattr(cc, "run_wl", fake_run_wl)

    cc.check_or_create({"test_name": "tests/test_x.py::test_y"})

    assert calls["list"], "the helper must scan for candidates"
    for query in calls["list"]:
        assert "--priority" not in query, query


def test_candidate_scan_covers_both_triage_tags(monkeypatch):
    """The scan looks at ``test-failure`` and ``gate-artefact`` items alike."""
    calls, fake_run_wl = _recording_run_wl([])
    monkeypatch.setattr(cc, "run_wl", fake_run_wl)

    cc.check_or_create({"test_name": "tests/test_x.py::test_y"})

    tags = {
        query[query.index("--tags") + 1]
        for query in calls["list"]
        if "--tags" in query
    }
    assert "test-failure" in tags
    assert "gate-artefact" in tags


def test_duplicate_candidates_are_collapsed_by_id(monkeypatch):
    """A row matching both tag queries is counted once."""
    item = _high_priority_item(GATE_SIGNATURE)
    _calls, fake_run_wl = _recording_run_wl([item, dict(item)])
    monkeypatch.setattr(cc, "run_wl", fake_run_wl)

    items = cc.list_critical_issues()

    assert [i["id"] for i in items] == ["SA-HIGH-EXISTING"]


# ---------------------------------------------------------------------------
# AC1 — gate-artefact routing for a ``verdict=missing`` refusal
# ---------------------------------------------------------------------------


def test_missing_verdict_creates_gate_artefact_not_test_failure(monkeypatch):
    """A cache-miss signature is filed as a ``[gate-artefact]`` item."""
    calls, fake_run_wl = _recording_run_wl([])
    monkeypatch.setattr(cc, "run_wl", fake_run_wl)

    result = cc.check_or_create(
        {
            "test_name": GATE_SIGNATURE,
            "stdout_excerpt": "No valid full-suite cache entry for: pytest -q",
            "commit_hash": "abc1234",
            "category": "gate_artefact",
        }
    )

    assert result["created"] is True
    assert result["issueId"] == "SA-NEW"
    create_args = calls["create"][0]

    title = create_args[create_args.index("--title") + 1]
    tags = create_args[create_args.index("--tags") + 1]
    priority = create_args[create_args.index("--priority") + 1]
    body = create_args[create_args.index("--description") + 1]

    # AC1: gate-artefact title prefix/tag, never the test-failure mislabel.
    assert title.startswith("[gate-artefact]")
    assert "— failing test" not in title
    assert "test-failure" not in tags
    assert "gate-artefact" in tags
    # A remedyable artefact is never filed as a critical blocker.
    assert priority != "critical"

    # AC1: the acceptance criteria are satisfiable by an agent (remediation).
    assert "run_tests.py --scope full" in body
    assert "finish" in body
    assert "abc1234" in body


def test_default_category_still_creates_a_critical_test_failure(monkeypatch):
    """Without a category the historical critical ``test-failure`` item is kept."""
    calls, fake_run_wl = _recording_run_wl([])
    monkeypatch.setattr(cc, "run_wl", fake_run_wl)

    result = cc.check_or_create(
        {"test_name": "tests/test_x.py::test_y", "stdout_excerpt": "assert 1 == 2"}
    )

    assert result["created"] is True
    create_args = calls["create"][0]
    title = create_args[create_args.index("--title") + 1]
    tags = create_args[create_args.index("--tags") + 1]
    priority = create_args[create_args.index("--priority") + 1]

    assert title == "[test-failure] tests/test_x.py::test_y — failing test"
    assert "test-failure" in tags
    assert priority == "critical"
