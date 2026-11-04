"""Regression tests: finalisation ordering and the audit-dispatch hold.

Parent **SA-0MVWFPI31000HP3H** (Only mark as complete when fully complete).

The race these tests pin (parent AC1/AC2): ``phase_finish`` writes the
terminal ``completed``/``in_review`` transition and releases the assignee
inside its ``StatusLifecycle`` block, then posts the completion comment (and
the session still performs the post-push verification) *after* it. The herdr
downtime dispatcher selects audit candidates by "terminal and not on an
assignee hold", so the item is dispatchable the moment the terminal write
lands — while the implement run is still finishing.

Design A (verify-then-transition, decided in SA-0MVXUSED6008J04R) makes the
terminal write and the assignee release the run's **last lifecycle action**:
every post-push step — the read-only post-push verification, the completion
comment and the worktree cleanup — runs inside the ``StatusLifecycle`` window
and therefore while the item is still ``in_progress`` with the assignee held.

Coverage:

- the ordered timeline of every store write and every post-push step: the
  verification, the completion comment and the cleanup all precede the
  terminal write, and the read-only ``_assert_terminal_status`` read-back is
  the phase's last action,
- audit candidacy at each post-push checkpoint (the item must not be a valid
  audit candidate there) and the single, post-terminal candidate transition,
- the final store state: ``completed``/``in_review`` with an empty assignee.

The real ``phase_finish`` state machine is driven with only its external
boundaries patched (git, the ``wl``/``StatusLifecycle`` store, the build and
gate runners, worktree removal, the read-only test cache), following the
stub/runner style of ``test_finish_no_diff_status.py``. No implement-phase
logic is re-implemented here: the only test-local model is
:func:`_is_audit_candidate`, a documented stand-in for the dispatcher's
selection predicate, which lives in another repository (ContextHub).

**Test-first slice.** The two ordering/candidacy expectations that the fix
enables are marked ``xfail(strict=True)`` until the sibling implementation
item (SA-0MVXUSJ2P009T97H) lands the re-ordering; the fixing change set removes
the markers, so a stale marker (an unexpected pass) fails loudly instead of
silently weakening the suite. The two unmarked companions (the single
candidate transition and the final store state + read-back) hold today and
guard against regressing the parts that are already correct. Each marked test
fails on the pre-fix tree for the documented reason — the terminal write lands
(and the assignee is released) before the completion comment, and the post-push
verification is not part of the run at all; the observed output is recorded on
SA-0MVXUSIM7009DKX7.
"""

from __future__ import annotations

import contextlib
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) in sys.path:
    sys.path.remove(str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT))

from skill.implement.scripts import implement

#: Stable full commit hash the fake git resolves every revision to. Both the
#: pre-push local-``dev`` fast-forward and the post-push ancestry assertion
#: resolve refs with ``git rev-parse --verify``; modelling a published commit
#: keeps those paths exercised instead of tripping their fail-closed
#: assertions (which have their own coverage in
#: ``tests/test_implement_finish_local_dev_verified_sync.py``).
_FAKE_COMMIT = "1" * 40

#: The full-suite command the stubbed suite resolver reports. The post-push
#: verification is a *read-only cache query* for exactly this command — it
#: never starts a suite run (design A).
_FULL_SUITE_COMMAND = "python3 -m pytest -q"

#: Why the ordering expectations are red until the sibling item lands. Shared
#: by the markers so the failure reason is one stable, greppable string.
_ORDERING_XFAIL = (
    "Pending SA-0MVXUSJ2P009T97H: phase_finish still writes the terminal "
    "completed/in_review transition (and releases the assignee) inside its "
    "StatusLifecycle block, and posts the completion comment afterwards while "
    "the post-push verification is left to the session — so the item is "
    "audit-dispatchable before the implement run is fully complete "
    "(SA-0MVWFPI31000HP3H AC1/AC2, design A). The fixing change set removes "
    "this marker."
)

#: Audit results fresher than this exclude an item from audit dispatch (the
#: dispatcher's ``AUDIT_FRESHNESS_MS`` window).
_AUDIT_FRESHNESS = timedelta(hours=1)

#: Items untouched for longer than this fall outside the dispatcher's
#: recency window.
_DISPATCH_RECENCY = timedelta(days=7)


# ---------------------------------------------------------------------------
# Audit candidacy — a documented stand-in for the out-of-repo dispatcher
# ---------------------------------------------------------------------------


def _parse_ts(value: object) -> datetime | None:
    """Parse an ISO-8601 work-item timestamp, or return ``None``."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _normalise_status(value: object) -> str:
    """Normalise ``in-progress``/``in_progress`` (``wl`` emits both spellings)."""
    return str(value or "").strip().replace("-", "_")


def _is_audit_candidate(item: dict) -> bool:
    """Whether the dispatcher would dispatch an audit for *item*.

    A test-local, documented mirror of ``selectAuditCandidate`` in ContextHub
    (``packages/herdr/src/downtime-worker.selection.ts``), which is outside
    this repository. The documented filters modelled here are: soft-deleted
    items, child items (``parentId`` set — dispatched through their parent),
    ``needsProducerReview`` items, items on an assignee hold, items with a
    fresh audit result, and items outside the dispatch recency window are all
    excluded; the item must be terminal (``completed`` / ``in_review``).

    The fixture item is a top-level, never-audited item, so its candidacy is
    decided by exactly the two properties this regression is about: terminal
    state and the assignee hold.
    """
    if item.get("deletedBy"):
        return False
    if item.get("parentId"):
        return False
    if item.get("needsProducerReview"):
        return False
    if str(item.get("assignee") or "").strip():
        # Non-empty assignee = scheduling/audit hold.
        return False
    terminal = (
        _normalise_status(item.get("status")) == "completed"
        or str(item.get("stage") or "") == "in_review"
    )
    if not terminal:
        return False
    audit = item.get("auditResult")
    audited_at = (
        _parse_ts(audit.get("auditedAt")) if isinstance(audit, dict) else None
    )
    if (
        audited_at is not None
        and datetime.now(timezone.utc) - audited_at < _AUDIT_FRESHNESS
    ):
        return False
    last_activity = _parse_ts(item.get("updatedAt") or item.get("activityAt"))
    return not (
        last_activity is not None
        and datetime.now(timezone.utc) - last_activity > _DISPATCH_RECENCY
    )


# ---------------------------------------------------------------------------
# Harness: the real phase_finish with only its external boundaries patched
# ---------------------------------------------------------------------------


def _fake_run_cmd(cmd: list[str], *args, **kwargs) -> subprocess.CompletedProcess:
    """Stand-in for ``run_cmd``: every git sub-command "succeeds"."""
    stdout = f"{_FAKE_COMMIT}\n" if len(cmd) > 1 and cmd[1] == "rev-parse" else ""
    return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=stdout, stderr="")


def _build_result() -> dict:
    return {
        "success": True,
        "stdout": "no build",
        "stderr": "",
        "exit_code": 0,
        "skipped": True,
    }


def _tests_result() -> dict:
    return {
        "success": True,
        "failures": [],
        "skipped": False,
        "tooling": "suite",
        "scope": "changed",
        "notices": [],
    }


def _cleanup_result() -> dict:
    return {"method": "wl_cleanup_worktree", "terminated": 0, "warning": ""}


@contextlib.contextmanager
def _run_finish(tmp_path, monkeypatch):
    """Drive the real ``phase_finish`` and record one ordered timeline.

    Every external boundary is patched; the ``wl``/``StatusLifecycle`` store is
    simulated by a mutable dict that the patched ``update_status`` writes and
    the patched ``show`` reads back, so lifecycle writes and reads are
    observable in order. The read-only cache boundary
    (``implement.query_cached``) marks the post-push verification: it is
    stubbed to a green full-suite entry for the pushed tree and is never
    allowed to start a run.

    Yields:
        ``(report, events, final_item)`` — the finish report, the ordered
        timeline (each event carries the item state observed *at* that
        moment), and the item as the store holds it afterwards.
    """
    monkeypatch.delenv("IMPLEMENT_DRIVE_ACTIVE", raising=False)
    monkeypatch.delenv("IMPLEMENT_KEEP_WORKTREE", raising=False)

    worktree = str((tmp_path / "worktree").resolve())
    Path(worktree).mkdir(parents=True, exist_ok=True)

    state = implement.ImplementState(
        work_item_id="OSL-TEST",
        worktree_path=worktree,
        repo_root=str((tmp_path / "main-checkout").resolve()),
        parent_branch="dev",
    )
    # A top-level, never-audited, currently claimed item: its audit candidacy
    # is decided by the terminal state and the assignee hold alone.
    item: dict = {
        "id": "OSL-TEST",
        "status": "in-progress",
        "stage": "intake_complete",
        "assignee": "implement",
        "parentId": "",
        "needsProducerReview": False,
        "auditResult": None,
        "updatedAt": datetime.now(timezone.utc).isoformat(),
    }
    events: list[dict] = []

    def _record(kind: str, **detail) -> None:
        events.append({"kind": kind, "state": dict(item), **detail})

    def _update_status(work_item_id, *args, **kwargs):
        """Simulated ``wl update`` — applies the write and records it."""
        status = args[0] if args else kwargs.get("status")
        stage = kwargs.get("stage")
        assignee = kwargs.get("assignee")
        if status is not None:
            item["status"] = str(status)
        if stage is not None:
            item["stage"] = str(stage)
        if assignee is not None:
            item["assignee"] = str(assignee)
        item["updatedAt"] = datetime.now(timezone.utc).isoformat()
        _record("store_write")
        return {"success": True}

    def _show(work_item_id, **kwargs):
        """Simulated ``wl show`` — records every store read in place."""
        _record("store_read")
        return {"workItem": dict(item)}

    def _query_cached(command, **kwargs):
        """The read-only post-push verification's cache query (green entry)."""
        _record("post_push_verification", command=command)
        return {
            "stdout": "1 passed",
            "stderr": "",
            "exit_code": 0,
            "completed_at": time.time(),
            "command": command,
            "git_state": "stub-git-state",
            "fingerprint_kind": "worktree",
            "scope": "full",
            "test_type": "full",
            "cached": True,
        }

    def _add_comment(work_item_id, comment):
        _record("completion_comment", body=comment)
        return True

    def _cleanup_processes(path):
        _record("worktree_cleanup")
        return _cleanup_result()

    def _remove_worktree(path, repo_root=None):
        _record("worktree_removal")
        return True

    # The read-back assertion is wrapped (and still executed for real) so its
    # position in the timeline is observable: parent AC4 requires it to run
    # *after* the terminal write, and the fail-loud guard's own behaviour is
    # covered by ``test_finish_no_diff_status.py``.
    real_assert_terminal_status = implement._assert_terminal_status

    def _assert_terminal_status(work_item_id):
        _record("status_readback")
        return real_assert_terminal_status(work_item_id)

    with contextlib.ExitStack() as stack:
        stack.enter_context(
            patch.object(implement, "_discover_worktree", return_value=worktree)
        )
        stack.enter_context(patch.object(implement, "read_state", return_value=state))
        stack.enter_context(
            patch.object(implement, "_worktree_placement_violation", return_value=None)
        )
        stack.enter_context(
            patch.object(implement, "run_build", return_value=_build_result())
        )
        # The gate runs against the would-be-commit tree through a temporary
        # index; both boundaries act on a real repository and are stubbed
        # green (their own behaviour is covered by ``test_finish_gate_*.py``).
        stack.enter_context(
            patch.object(
                implement,
                "run_gate_suite",
                return_value=(
                    _tests_result(),
                    {
                        "tree": "abc1234",
                        "scope": "changed",
                        "cached": False,
                        "index_unchanged": True,
                    },
                ),
            )
        )
        stack.enter_context(
            patch.object(
                implement,
                "stage_tested_tree",
                return_value={
                    "success": True,
                    "tree": "abc1234",
                    "expected": "abc1234",
                },
            )
        )
        stack.enter_context(patch.object(implement, "git_commit", return_value=True))
        stack.enter_context(
            patch.object(implement, "git_get_commit_hash", return_value="abc1234")
        )
        stack.enter_context(
            patch.object(implement, "git_push_to_dev", return_value=True)
        )
        # The push-subject guard reads the real git HEAD; this harness drives a
        # placeholder worktree, so it is stubbed with the push itself (it has
        # dedicated coverage in ``test_implement_push_from_worktree.py``).
        stack.enter_context(patch.object(implement, "_assert_push_subject"))
        stack.enter_context(
            patch.object(implement, "run_cmd", side_effect=_fake_run_cmd)
        )
        stack.enter_context(
            patch.object(implement, "cleanup_worktree_processes", _cleanup_processes)
        )
        stack.enter_context(patch.object(implement, "remove_state"))
        stack.enter_context(patch.object(implement, "_remove_worktree", _remove_worktree))
        stack.enter_context(
            patch.object(
                implement,
                "_sync_parent_branch",
                return_value={"method": "skipped", "success": True},
            )
        )
        stack.enter_context(patch.object(implement, "wl_add_comment", _add_comment))
        stack.enter_context(
            patch.object(implement, "_assert_terminal_status", _assert_terminal_status)
        )
        stack.enter_context(patch.object(implement, "_surface_postfix_prose"))
        # The post-push verification boundary: the read-only cache query and
        # the full-suite command resolver. Nothing is executed.
        stack.enter_context(
            patch.object(
                implement,
                "_resolve_full_suite_commands",
                return_value=[_FULL_SUITE_COMMAND],
            )
        )
        stack.enter_context(patch.object(implement, "query_cached", _query_cached))
        # ``MagicMock`` (not a plain function) because these are *static*
        # methods called through the instance: a plain function assigned to
        # the class would bind ``self`` and receive an extra argument.
        stack.enter_context(
            patch.object(
                implement.StatusLifecycle,
                "update_status",
                MagicMock(side_effect=_update_status),
            )
        )
        stack.enter_context(
            patch.object(
                implement.StatusLifecycle, "show", MagicMock(side_effect=_show)
            )
        )

        report = implement.phase_finish("OSL-TEST", json_output=True, no_refactor=True)
        yield report, events, dict(item)


def _is_terminal_write(event: dict) -> bool:
    """Whether *event* is the terminal ``completed``/``in_review`` write."""
    return (
        event.get("kind") == "store_write"
        and _normalise_status(event["state"]["status"]) == "completed"
        and str(event["state"]["stage"]) == "in_review"
    )


def _terminal_write_index(events: list[dict]) -> int | None:
    for index, event in enumerate(events):
        if _is_terminal_write(event):
            return index
    return None


def _describe(index: int, event: dict) -> str:
    state = event["state"]
    return (
        f"checkpoint {index} '{event['kind']}': "
        f"status={state['status']!r}, stage={state['stage']!r}, "
        f"assignee={state['assignee']!r}"
    )


# ---------------------------------------------------------------------------
# Ordering: the terminal write is the last lifecycle action
# ---------------------------------------------------------------------------


@pytest.mark.xfail(strict=True, reason=_ORDERING_XFAIL)
def test_post_push_steps_precede_the_terminal_write(tmp_path, monkeypatch):
    """Verification, completion comment and cleanup all precede the transition.

    Design A: the terminal ``completed``/``in_review`` write and the assignee
    release must be the run's last lifecycle action, so every remaining step
    runs inside the ``StatusLifecycle`` window — the item is un-auditable
    while work remains (parent AC1/AC2).
    """
    with _run_finish(tmp_path, monkeypatch) as (report, events, _final):
        assert report["success"] is True, report["message"]

        terminal = _terminal_write_index(events)
        assert terminal is not None, (
            "phase_finish never wrote the terminal completed/in_review "
            f"transition; timeline: {[e['kind'] for e in events]}"
        )

        for kind in (
            "post_push_verification",
            "completion_comment",
            "worktree_cleanup",
        ):
            positions = [i for i, e in enumerate(events) if e["kind"] == kind]
            assert positions, (
                f"phase_finish never performed the '{kind}' step inside the "
                "finalisation window — it still belongs to the session after "
                "finish returns, so the item is dispatchable while the "
                f"implement run is unfinished; timeline: "
                f"{[e['kind'] for e in events]}"
            )
            last = positions[-1]
            assert last < terminal, (
                f"the '{kind}' step ran after the terminal completed/in_review "
                f"write ({_describe(last, events[last])} vs {_describe(terminal, events[terminal])}) "
                "— post-push work must complete before the item becomes "
                "audit-dispatchable (parent AC1/AC2)"
            )

        # The read-only read-back is the phase's last action, so nothing that
        # a session could still be doing follows the lifecycle transition.
        assert events[-1]["kind"] == "status_readback", (
            "the terminal-status read-back must be the last action of the "
            f"finish phase; last event was "
            f"{_describe(len(events) - 1, events[-1])}"
        )
        assert "post_push_verification" in report["steps"], (
            "the post-push verification verdict must be recorded in the finish "
            f"report; steps: {sorted(report['steps'])}"
        )


@pytest.mark.xfail(strict=True, reason=_ORDERING_XFAIL)
def test_item_is_not_an_audit_candidate_at_any_post_push_checkpoint(
    tmp_path, monkeypatch
):
    """The dispatcher must not see the item while post-push work remains.

    Parent AC2, observably: at the moment each post-push step runs — the
    read-only verification, the completion comment, the cleanup and the
    worktree removal — the item is still on its assignee hold and
    non-terminal, so it is not a valid audit candidate.
    """
    checkpoints = {
        "post_push_verification",
        "completion_comment",
        "worktree_cleanup",
        "worktree_removal",
    }
    with _run_finish(tmp_path, monkeypatch) as (report, events, _final):
        assert report["success"] is True, report["message"]

        observed = [
            (index, event)
            for index, event in enumerate(events)
            if event["kind"] in checkpoints
        ]
        assert observed, (
            "phase_finish performed none of the post-push steps "
            f"({sorted(checkpoints)}); timeline: {[e['kind'] for e in events]}"
        )
        for index, event in observed:
            assert not _is_audit_candidate(event["state"]), (
                f"the item was already a valid audit candidate at "
                f"{_describe(index, event)} — the dispatcher could start an "
                "audit while the implement run is still finishing "
                "(parent AC2)"
            )


# ---------------------------------------------------------------------------
# Terminal state and read-back (green before and after the fix)
# ---------------------------------------------------------------------------


def test_completion_makes_the_item_an_audit_candidate_exactly_once(
    tmp_path, monkeypatch
):
    """Candidacy flips exactly once, on the terminal write, and survives.

    Parent AC3: the item becomes a valid audit candidate exactly once — after
    the final lifecycle action — and remains one afterwards. A second
    transition (or a flap back to held) would mean a late write raced the
    transition.
    """
    with _run_finish(tmp_path, monkeypatch) as (report, events, final_item):
        assert report["success"] is True, report["message"]

        states = [event["state"] for event in events] + [final_item]
        transitions = [
            index
            for index in range(1, len(states))
            if _is_audit_candidate(states[index])
            and not _is_audit_candidate(states[index - 1])
        ]
        assert len(transitions) == 1, (
            "the item must become an audit candidate exactly once; "
            f"transitions at {transitions} "
            f"(timeline: {[e['kind'] for e in events]})"
        )

        index = transitions[0]
        assert index < len(events), "the candidate transition needs a timeline event"
        event = events[index]
        assert _is_terminal_write(event), (
            "audit candidacy must begin on the terminal completed/in_review "
            f"write; it began at {_describe(index, event)}"
        )
        assert _is_audit_candidate(final_item), (
            f"the item must remain an audit candidate afterwards; got {final_item}"
        )
        assert all(
            _is_audit_candidate(state) for state in states[index:]
        ), "the item must never lose audit candidacy once it has become one"


def test_terminal_write_is_the_last_lifecycle_write_and_is_read_back(
    tmp_path, monkeypatch
):
    """Final store state, and the read-back runs after the terminal write.

    Parent AC2/AC4: ``completed``/``in_review`` with an empty assignee, and the
    fail-loud ``_assert_terminal_status`` read-back after the terminal write
    (OSL-0MVME1PI5005WU58).
    """
    with _run_finish(tmp_path, monkeypatch) as (report, events, final_item):
        assert report["success"] is True, report["message"]

        terminal = _terminal_write_index(events)
        assert terminal is not None, "the terminal transition was never written"

        later_writes = [
            (index, event)
            for index, event in enumerate(events[terminal + 1 :], start=terminal + 1)
            if event["kind"] == "store_write"
        ]
        assert not later_writes, (
            "no lifecycle write may follow the terminal transition; found "
            f"{[_describe(i, e) for i, e in later_writes]}"
        )

        readbacks = [
            index
            for index, event in enumerate(events)
            if index > terminal and event["kind"] == "status_readback"
        ]
        assert readbacks, (
            "the terminal-status read-back must run after the terminal write; "
            f"timeline: {[e['kind'] for e in events]}"
        )
        assert report["steps"]["status_readback"]["success"] is True
        readback_state = events[readbacks[-1]]["state"]
        assert _normalise_status(readback_state["status"]) == "completed"
        assert readback_state["stage"] == "in_review"

        assert _normalise_status(final_item["status"]) == "completed"
        assert final_item["stage"] == "in_review"
        assert final_item["assignee"] == ""
        assert _is_audit_candidate(final_item)
