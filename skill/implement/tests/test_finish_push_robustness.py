"""Git-transport robustness for a leaf ``finish`` (SA-0MW22SEMV00230O5).

Regression tests for the ACs of "Git transport is fragile under concurrent
agent runs":

* **AC1 — transient auth/transport is classified and retried.** The observed
  concurrent-run signatures (``Permission denied (publickey)``, ``Could not
  read from remote repository``, connect timeouts/reset, a remote
  ``Internal Server Error``) are classified ``transport`` (infrastructure) and
  retried on the bounded budget with backoff; they surface in the report as
  such, never as a bare "push failed".
* **AC2 — non-fast-forward is retried for leaf runs** on the bounded
  ``DEFAULT_INFRA_RETRY_LIMIT`` budget; a rebase conflict stays a genuine
  stop.
* **AC3/AC5 — ``finish`` converges when the commit already landed**
  (remote-verified ancestry): the push is skipped (``already_pushed``) and a
  landed item is never reset to ``open`` by a transport failure.
* **AC4 — the push outcomes are distinguishable** (`pushed` /
  `already_pushed` / `push_failed` / `push_landed_but_unverified`).
* **AC7 — a load-flake gate failure is labelled**, retried once on the
  identical tree, and still blocks when reproducible.
* **AC6 — one changed-scope gate per finish even across push retries.**

Hermetic at the ``git_push_to_dev`` level (scripted ``Popen``, recorded sleep
seam — reused from ``test_implement_push_retry.py``) and stubbed-boundary at
the ``phase_finish`` level (real-git sandbox + stubbed external boundaries,
reused from ``test_implement_push_from_worktree.py``).
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
for _path in (str(REPO_ROOT), str(REPO_ROOT / "skill")):
    if _path not in sys.path:
        sys.path.append(_path)

from shared import git_sandbox as gs

from skill.implement.scripts import implement
from skill.implement.tests.test_implement_push_from_worktree import (
    BRANCH,
    _build_result,
    _stubbed_finish,
)
from skill.implement.tests.test_implement_push_retry import (
    AUTH_FAILURE,
    _scripted_popen,
)

# ---------------------------------------------------------------------------
# Hermetic git_push_to_dev harness (same shape as test_implement_push_retry)
# ---------------------------------------------------------------------------


@pytest.fixture
def push_harness(monkeypatch, tmp_path):
    """Drive ``git_push_to_dev`` with a scripted push and recorded backoff."""
    calls: list[dict] = []
    sleeps: list[float] = []

    def install(script):
        calls.clear()
        sleeps.clear()
        monkeypatch.setattr(
            implement.subprocess, "Popen", _scripted_popen(script, calls)
        )
        monkeypatch.setattr(
            implement, "_push_retry_sleep", lambda seconds: sleeps.append(seconds)
        )

    return {
        "cwd": str(tmp_path),
        "calls": calls,
        "sleeps": sleeps,
        "install": install,
    }


# ---------------------------------------------------------------------------
# AC1 — pure classification of the five observed signatures
# ---------------------------------------------------------------------------


class TestClassifyPushFailure:
    @pytest.mark.parametrize(
        "message",
        [
            # Observed concurrent-run failures (2026-11-04 session).
            "git@github.com: Permission denied (publickey).",
            "fatal: Could not read from remote repository.",
            "ssh: connect to host github.com port 22: Connection timed out",
            "Connection reset by peer",
            "! [remote rejected]   HEAD -> dev (Internal Server Error)",
        ],
    )
    def test_transient_signatures_are_transport(self, message: str) -> None:
        assert (
            implement.classify_push_failure(message)
            == implement.PUSH_FAILURE_TRANSPORT
        )

    def test_non_fast_forward_is_its_own_outcome(self) -> None:
        assert (
            implement.classify_push_failure(
                "! [rejected]  wl-x -> dev (non-fast-forward)"
            )
            == implement.PUSH_FAILURE_NON_FAST_FORWARD
        )

    def test_gate_reason_wins_over_transport_phrases(self) -> None:
        # A hook-refused push (gate_reason=timeout) whose stderr also contains
        # a transport phrase must classify as a gate refusal — the hook's
        # reason is authoritative.
        stderr = (
            "gate_reason=timeout\nConnection to github.com timed out.\n"
        )
        assert implement.classify_push_failure(stderr) == implement.PUSH_FAILURE_GATE

    def test_content_rejection_is_not_transport(self) -> None:
        assert (
            implement.classify_push_failure(
                "remote: error: GH006: Protected branch update failed"
            )
            == implement.PUSH_FAILURE_CONTENT
        )

    def test_unknown_falls_back_safely(self) -> None:
        assert implement.classify_push_failure("") == implement.PUSH_FAILURE_UNKNOWN


# ---------------------------------------------------------------------------
# AC1 — auth/transport is retried with backoff; exhaustion is distinct
# ---------------------------------------------------------------------------


class TestGitPushToDevAuthRetry:
    def test_auth_failure_is_retried_then_succeeds(self, push_harness) -> None:
        push_harness["install"](
            [(128, AUTH_FAILURE), (128, AUTH_FAILURE), (0, b"")]
        )

        assert (
            implement.git_push_to_dev(push_harness["cwd"], "wl-test", "abc123")
            is True
        )
        assert len(push_harness["calls"]) == 3
        assert len(push_harness["sleeps"]) == 2
        assert len({call["cwd"] for call in push_harness["calls"]}) == 1, (
            "a retry must re-run from the same checkout so the gate serves its "
            "cached full-suite entry"
        )

    def test_remote_5xx_is_retried_with_the_transport_trail(self, push_harness) -> None:
        ise = (
            b"! [remote rejected]   HEAD -> dev (Internal Server Error)\n"
        )
        push_harness["install"]([(128, ise), (0, b"")])
        evidence: dict = {}

        assert (
            implement.git_push_to_dev(
                push_harness["cwd"], "wl-test", "abc123", evidence_out=evidence
            )
            is True
        )
        assert evidence["attempts"] == 2
        trail = evidence["retry_trail"]
        assert trail[0]["reason"] == "transport", trail
        assert trail[0]["delay_seconds"] == pytest.approx(
            push_harness["sleeps"][0], abs=0.001
        )

    def test_auth_exhaustion_raises_the_distinct_transport_error(
        self, push_harness
    ) -> None:
        push_harness["install"](
            [(128, AUTH_FAILURE)] * implement.PUSH_RETRY_MAX_ATTEMPTS
        )
        evidence: dict = {}

        with pytest.raises(implement.PushTransportExhaustedError) as excinfo:
            implement.git_push_to_dev(
                push_harness["cwd"],
                "wl-test",
                "abc123",
                evidence_out=evidence,
            )

        assert len(push_harness["calls"]) == implement.PUSH_RETRY_MAX_ATTEMPTS
        assert evidence["success"] is False
        assert evidence["reason"] == "transport"
        assert "infrastructure" in str(excinfo.value).lower()
        assert excinfo.value.attempts == implement.PUSH_RETRY_MAX_ATTEMPTS


# ---------------------------------------------------------------------------
# AC3 — remote-verified ancestry query (the already-landed short-circuit)
# ---------------------------------------------------------------------------


class _FakeCompleted:
    def __init__(self, returncode: int, stdout: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


def _scripted_ancestry(monkeypatch, script: dict[str, int]):
    """Route ``run_cmd`` for the ancestry queries onto a scripted table.

    ``script`` maps the command name to a returncode; ``rev-parse`` resolves
    to a fixed hash; everything else succeeds empty (the caller's own git
    plumbing still needs a working boundary).
    """
    calls: list[list[str]] = []

    def fake(cmd, **kwargs):
        calls.append(list(cmd))
        name = cmd[1] if len(cmd) > 1 else ""
        if name == "rev-parse":
            return _FakeCompleted(0, f"{'1' * 40}\n")
        if name in script:
            return _FakeCompleted(script[name])
        return _FakeCompleted(0)

    monkeypatch.setattr(implement, "run_cmd", fake)
    return calls


class TestCommitIsOnOriginDev:
    def test_remote_ancestor_is_verified(self, monkeypatch) -> None:
        # fetch ok (0) and merge-base says ancestor (0) -> verified.
        calls = _scripted_ancestry(monkeypatch, {"fetch": 0, "merge-base": 0})
        result = implement._commit_is_on_origin_dev(
            "/repo", "abc1234", parent_branch="dev"
        )
        assert result["verified"] is True
        assert result["fetched"] is True
        assert any(c[1] == "fetch" for c in calls), "must fetch before asserting"

    def test_stale_remote_ref_is_never_trusted(self, monkeypatch) -> None:
        # The merge-base check itself returns "not ancestor" -> not verified.
        calls = _scripted_ancestry(monkeypatch, {"fetch": 0, "merge-base": 1})
        result = implement._commit_is_on_origin_dev("/repo", "abc1234")
        assert result["verified"] is False
        assert any(c[1] == "fetch" for c in calls)

    def test_fetch_failure_fails_open(self, monkeypatch) -> None:
        # Cannot refresh the remote tracking ref -> not verified (push
        # proceeds; a wrong "already landed" would silently skip a push).
        monkeypatch.setattr(
            implement,
            "run_cmd",
            lambda cmd, **kwargs: _FakeCompleted(1)
            if len(cmd) > 1 and cmd[1] == "fetch"
            else _FakeCompleted(0),
        )
        result = implement._commit_is_on_origin_dev("/repo", "abc1234")
        assert result["verified"] is False
        assert result["fetched"] is False

    def test_empty_commit_is_not_verified(self, monkeypatch) -> None:
        result = implement._commit_is_on_origin_dev("/repo", None)
        assert result["verified"] is False


# ---------------------------------------------------------------------------
# Phase-finish harness: the real phase_finish with stubbed external boundaries
# ---------------------------------------------------------------------------


@pytest.fixture
def sandbox(tmp_path: Path) -> dict:
    """A scratch ``origin``, a ``dev`` main checkout and a feature worktree."""
    remote = gs.init_bare_remote(tmp_path / "origin.git")
    main = gs.init_repo(tmp_path / "main", default_branch="dev")
    (main / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (main / "tests").mkdir()
    (main / "tests" / "test_seed.py").write_text(
        "def test_seed():\n    assert True\n", encoding="utf-8"
    )
    base = gs.commit_all(main, "base commit")
    gs.add_remote(main, "origin", str(remote))
    gs.run_git(main, ["push", "-q", "origin", "dev"], check=True)

    wt = gs.add_worktree(main, tmp_path / BRANCH, BRANCH)
    (wt / "feature.txt").write_text("feature\n", encoding="utf-8")
    feature = gs.commit_all(wt, "optional feature")

    return {
        "tmp": tmp_path,
        "remote": remote,
        "main": main,
        "wt": wt,
        "base": base,
        "feature": feature,
        "feature_short": gs.run_git(
            wt, ["rev-parse", "--short", "HEAD"], check=True
        ).stdout.strip(),
    }


def _landed(repo_root: str, sandbox: dict, verified: bool) -> dict:
    return {
        "verified": verified,
        "commit": sandbox["feature_short"],
        "resolved_commit": sandbox["feature"],
        "origin_tip": sandbox["feature"] if verified else None,
        "target": "refs/remotes/origin/dev",
        "fetched": True,
    }


# ---------------------------------------------------------------------------
# AC3/AC4/AC5 — already-landed short-circuit and the push-outcome taxonomy
# ---------------------------------------------------------------------------


def test_finish_short_circuits_an_already_landed_commit(sandbox):
    """AC3: a commit verifiably on origin/dev is NOT re-pushed (zero push
    attempts), reaches the terminal transition, and reports
    ``already_pushed`` — a transport flake can never regress it (AC5)."""
    pushes: list[tuple] = []
    with (
        _stubbed_finish(sandbox, sandbox["feature_short"], pushes),
        patch.object(
            implement,
            "_commit_is_on_origin_dev",
            return_value=_landed(str(sandbox["main"]), sandbox, True),
        ),
    ):
        report = implement.phase_finish("SA-TEST", json_output=True, no_refactor=True)

    assert report["success"] is True, report["message"]
    push_step = report["steps"]["push"]
    assert push_step["outcome"] == "already_pushed", push_step
    assert pushes == [], (
        "an already-landed commit must not re-push; the recorded push "
        f"attempts were {pushes}"
    )
    assert report["steps"]["ancestry_assertion"]["verified"] is True
    assert report["steps"]["post_push_verification"]["verdict"] in (
        implement.POST_PUSH_VERDICT_GREEN,
        implement.POST_PUSH_VERDICT_NOT_APPLICABLE,
    ), report["steps"]["post_push_verification"]


def test_finish_converges_when_transport_fails_but_the_commit_landed(sandbox):
    """AC5: a transport failure with the commit verifiably on origin/dev
    (landed concurrently) converges to ``in_review`` instead of resetting the
    item to ``open``."""
    pushes: list[tuple] = []

    def transport_lost_push(cwd, branch, commit_hash=None, **kwargs):
        raise implement.PushTransportLostError(
            commit_hash or "unknown", branch, "Connection closed by remote host.", 3
        )

    with (
        _stubbed_finish(sandbox, sandbox["feature_short"], pushes),
        patch.object(implement, "git_push_to_dev", side_effect=transport_lost_push),
        patch.object(
            implement,
            "_commit_is_on_origin_dev",
            # pre-push check: not landed -> push attempted; transport handler
            # re-check: landed -> converge (never a transport regression).
            side_effect=[
                _landed(str(sandbox["main"]), sandbox, False),
                _landed(str(sandbox["main"]), sandbox, True),
            ],
        ),
    ):
        report = implement.phase_finish("SA-TEST", json_output=True, no_refactor=True)

    assert report["success"] is True, report["message"]
    assert report["steps"]["push"]["outcome"] == "already_pushed", (
        report["steps"]["push"]
    )
    assert report["steps"]["push"]["reason"] == "connection_closed", (
        "the transport reason is kept for observability even on convergence"
    )


def test_finish_reports_exhausted_transport_as_infrastructure(sandbox):
    """AC1/AC4: an exhausted auth/transport failure is ``push_failed`` with
    reason ``transport`` and an infrastructure-labelled comment — never a bare
    'push failed' and never the misleading 'push it manually' hint."""
    pushes: list[tuple] = []
    comments: list[str] = []

    def exhausted_push(cwd, branch, commit_hash=None, **kwargs):
        raise implement.PushTransportExhaustedError(
            commit_hash or "unknown", branch, "Permission denied (publickey).", 3
        )

    with (
        _stubbed_finish(sandbox, sandbox["feature_short"], pushes),
        patch.object(implement, "git_push_to_dev", side_effect=exhausted_push),
        patch.object(
            implement,
            "wl_add_comment",
            side_effect=lambda *args, **kwargs: comments.append(args[1]) or True,
        ),
    ):
        report = implement.phase_finish("SA-TEST", json_output=True, no_refactor=True)

    assert report["success"] is False, report["message"]
    push_step = report["steps"]["push"]
    assert push_step["reason"] == "transport", push_step
    assert push_step["outcome"] == "push_failed", push_step
    combined = "\n".join(comments)
    assert "infrastructure" in combined.lower(), combined
    assert "not a content rejection" in combined.lower(), combined


# ---------------------------------------------------------------------------
# AC2 — bounded rebase-and-retry for the leaf push loop
# ---------------------------------------------------------------------------


def test_finish_rebases_and_retries_a_concurrent_advance(sandbox):
    """AC2: a *second* concurrent advance (the observed repeated-race case) is
    rebased and re-attempted within the bounded budget instead of failing the
    item, and the changed-scope gate (Step 3) still ran exactly once (AC6)."""
    pushes: list[tuple] = []
    calls = {"n": 0}

    def nff_then_recover(cwd, branch, commit_hash=None, **kwargs):
        calls["n"] += 1
        if calls["n"] <= 3:
            raise implement.PushNonFastForwardError(
                commit_hash or "x", branch, "non-fast-forward", "git rebase origin/dev"
            )
        return True

    with (
        _stubbed_finish(sandbox, sandbox["feature_short"], pushes),
        patch.object(implement, "git_push_to_dev", side_effect=nff_then_recover),
        patch.object(implement, "_push_retry_sleep", lambda seconds: None),
    ):
        report = implement.phase_finish("SA-TEST", json_output=True, no_refactor=True)

    assert report["success"] is True, report["message"]
    push_step = report["steps"]["push"]
    assert push_step["recovered"] == "non_fast_forward", push_step
    assert push_step["outcome"] == "pushed", push_step
    assert calls["n"] == 4, "1 initial + 2 raced recoveries + 1 success = 4"
    # AC6: the changed-scope gate ran once at Step 3, never re-run per retry.
    assert len(report["steps"]["tests"]) == 1, report["steps"]["tests"]


def test_finish_stops_when_rebase_retries_exhaust_the_budget(sandbox):
    """AC2: rebase-and-retry is bounded — a dev that keeps advancing stops
    with an actionable message and the item stays open."""
    pushes: list[tuple] = []

    def always_nff(cwd, branch, commit_hash=None, **kwargs):
        raise implement.PushNonFastForwardError(
            commit_hash or "x", branch, "non-fast-forward", "git rebase origin/dev"
        )

    with (
        _stubbed_finish(sandbox, sandbox["feature_short"], pushes),
        patch.object(implement, "git_push_to_dev", side_effect=always_nff),
        patch.object(implement, "_push_retry_sleep", lambda seconds: None),
    ):
        report = implement.phase_finish("SA-TEST", json_output=True, no_refactor=True)

    assert report["success"] is False, report["message"]
    push_step = report["steps"]["push"]
    assert push_step["reason"] == "non_fast_forward", push_step
    assert push_step["outcome"] == "push_failed", push_step
    assert push_step["retries"] > implement.DEFAULT_INFRA_RETRY_LIMIT, push_step
    assert push_step["budget"] == implement.DEFAULT_INFRA_RETRY_LIMIT, push_step


# ---------------------------------------------------------------------------
# AC7 — a load-flake gate failure is labelled and retried once; blocked when
# reproducible
# ---------------------------------------------------------------------------


def test_finish_labels_a_recovered_load_flake(sandbox):
    """AC7: when the gate fails with suite *load* errors and the identical
    tree passes on the retry, the report says infrastructure/load flake (
    ``report[steps][push][load_flake]``) — not a code failure."""
    pushes: list[tuple] = []
    calls = {"n": 0}
    load_stderr = "Cannot find package '@worklog/shared/worklog-paths'"

    def flaky_gate_push(cwd, branch, commit_hash=None, **kwargs):
        calls["n"] += 1
        evidence = kwargs.get("evidence_out")
        if calls["n"] == 1:
            evidence.update(
                {
                    "success": False,
                    "gate_reason": "unparsed",
                    "stderr_tail": load_stderr,
                    "attempts": 1,
                    "retry_trail": [],
                }
            )
            return False
        evidence.update(
            {
                "root": str(cwd),
                "head": sandbox["feature"],
                "pushed": sandbox["feature"],
                "full_suite": "cached",
            }
        )
        return True

    with (
        _stubbed_finish(sandbox, sandbox["feature_short"], pushes),
        patch.object(implement, "git_push_to_dev", side_effect=flaky_gate_push),
        patch.object(implement, "_push_retry_sleep", lambda seconds: None),
    ):
        report = implement.phase_finish("SA-TEST", json_output=True, no_refactor=True)

    assert report["success"] is True, report["message"]
    push_step = report["steps"]["push"]
    flake = push_step.get("load_flake")
    assert flake is not None, push_step
    assert flake["failed_on_attempt"] == 1, flake
    assert calls["n"] == 2, "one failed load-flake attempt + one retry"


def test_finish_blocks_a_reproducible_load_failure(sandbox):
    """AC7: a load-signature gate failure that reproduces on the retry still
    blocks the push — the gate is never weakened."""
    pushes: list[tuple] = []
    calls = {"n": 0}
    load_stderr = "Cannot find package '@worklog/shared/worklog-paths'"

    def always_flaky(cwd, branch, commit_hash=None, **kwargs):
        calls["n"] += 1
        evidence = kwargs.get("evidence_out")
        evidence.update(
            {
                "success": False,
                "gate_reason": "unparsed",
                "stderr_tail": load_stderr,
                "attempts": 1,
                "retry_trail": [],
            }
        )
        return False

    with (
        _stubbed_finish(sandbox, sandbox["feature_short"], pushes),
        patch.object(implement, "git_push_to_dev", side_effect=always_flaky),
        patch.object(implement, "_push_retry_sleep", lambda seconds: None),
    ):
        report = implement.phase_finish("SA-TEST", json_output=True, no_refactor=True)

    assert report["success"] is False, report["message"]
    push_step = report["steps"]["push"]
    assert push_step["reason"] == "unparsed", push_step
    assert push_step["outcome"] == "push_failed", push_step
    assert calls["n"] == 2, "exactly one diagnostic retry, then blocked"


# ---------------------------------------------------------------------------
# AC1/AC2/AC5/AC6 (SA-0MWMRHW01009Q22Q) — never gate a tree you did not build
#
# A non-fast-forward recovery rebases the feature branch onto a concurrently
# advanced ``dev``. The rebased source is not reflected in gitignored build
# output (``packages/shared/dist``), so the retry push must rebuild first:
# otherwise the dev-push gate measures a stale artefact and false-reds on a
# ``TypeError`` naming a function the change set never touched.
# ---------------------------------------------------------------------------


def _recording_build(order: list[str], *, fail_on: int | None = None, calls=None):
    """A ``run_build`` replacement that records call order (and can fail)."""
    counter = calls if calls is not None else {"n": 0}

    def build(cwd: str) -> dict:
        counter["n"] += 1
        order.append("build")
        if fail_on is not None and counter["n"] == fail_on:
            return {
                "success": False,
                "stdout": "engine:sync ok",
                "stderr": "src/index.ts(3,9): error TS2554: boom",
                "exit_code": 2,
                "skipped": False,
            }
        return _build_result()

    return build


def _recording_push(order: list[str], *, nff_rounds: int = 0):
    """A ``git_push_to_dev`` replacement: NFF for *nff_rounds*, then success."""
    state = {"n": 0}

    def push(cwd, branch, commit_hash=None, **kwargs):
        state["n"] += 1
        order.append("push")
        if state["n"] <= nff_rounds:
            raise implement.PushNonFastForwardError(
                commit_hash or "x",
                branch,
                "non-fast-forward",
                "git rebase origin/dev",
            )
        evidence = kwargs.get("evidence_out")
        if evidence is not None:
            evidence.update(
                {
                    "root": str(cwd),
                    "head": commit_hash,
                    "pushed": commit_hash,
                    "full_suite": "cached",
                }
            )
        return True

    return push


def test_post_rebase_rebuild_runs_between_the_rebase_and_the_retry_push(sandbox):
    """AC1: the rebuild runs after the recovery rebase and before the retry push,
    and the recorded push verdict is green/pushed."""
    pushes: list[tuple] = []
    order: list[str] = []
    with (
        _stubbed_finish(sandbox, sandbox["feature_short"], pushes),
        patch.object(implement, "run_build", side_effect=_recording_build(order)),
        patch.object(
            implement, "git_push_to_dev", side_effect=_recording_push(order, nff_rounds=1)
        ),
        patch.object(implement, "_push_retry_sleep", lambda seconds: None),
    ):
        report = implement.phase_finish("SA-TEST", json_output=True, no_refactor=True)

    assert report["success"] is True, report["message"]
    assert order == ["build", "push", "build", "push"], (
        "the tree must be rebuilt after the rebase and before the retry push; "
        f"observed order {order}"
    )
    push_step = report["steps"]["push"]
    assert push_step["outcome"] == "pushed", push_step
    assert push_step["recovered"] == "non_fast_forward", push_step
    rebase_build = push_step.get("rebase_build")
    assert rebase_build is not None, push_step
    assert rebase_build["reason"] == "post_rebase", rebase_build
    assert rebase_build["success"] is True, rebase_build


def test_uncontended_push_pays_exactly_one_build(sandbox):
    """AC6: the normal (no-rebase) path keeps exactly one build per finish and
    never re-enters the changed-scope gate (Step 3)."""
    pushes: list[tuple] = []
    order: list[str] = []
    with (
        _stubbed_finish(sandbox, sandbox["feature_short"], pushes),
        patch.object(implement, "run_build", side_effect=_recording_build(order)),
    ):
        report = implement.phase_finish("SA-TEST", json_output=True, no_refactor=True)

    assert report["success"] is True, report["message"]
    assert order.count("build") == 1, order
    assert len(pushes) == 1, pushes
    assert len(report["steps"]["tests"]) == 1, report["steps"]["tests"]
    assert report["steps"]["push"].get("freshness_builds") is None


def test_finish_fails_closed_when_the_post_rebase_rebuild_fails(sandbox):
    """AC5: a rebuild that itself fails after the rebase fails closed — no
    retry push, an actionable comment naming the build failure and the
    worktree, and the item reset to ``open``."""
    pushes: list[tuple] = []
    order: list[str] = []
    comments: list[str] = []
    status_mock = MagicMock()

    with (
        _stubbed_finish(sandbox, sandbox["feature_short"], pushes),
        patch.object(
            implement, "run_build", side_effect=_recording_build(order, fail_on=2)
        ),
        patch.object(
            implement, "git_push_to_dev", side_effect=_recording_push(order, nff_rounds=99)
        ),
        patch.object(
            implement,
            "wl_add_comment",
            side_effect=lambda *args, **kwargs: comments.append(args[1]) or True,
        ),
        patch.object(implement.StatusLifecycle, "update_status", status_mock),
        patch.object(implement, "_push_retry_sleep", lambda seconds: None),
    ):
        report = implement.phase_finish("SA-TEST", json_output=True, no_refactor=True)

    assert report["success"] is False, report["message"]
    push_step = report["steps"]["push"]
    assert push_step["outcome"] == "push_failed", push_step
    assert push_step["reason"] == "post_rebase_build_failed", push_step
    assert push_step["build"]["exit_code"] == 2, push_step["build"]
    # Only the initial (rejected) push ran; the retry push was never attempted.
    assert order == ["build", "push", "build"], order
    combined = "\n".join(comments)
    assert "TS2554" in combined, combined
    assert str(sandbox["wt"]) in combined, combined
    reset_calls = [
        call for call in status_mock.call_args_list
        if len(call.args) > 1 and call.args[1] == "open"
    ]
    assert reset_calls, f"item was not reset to open: {status_mock.call_args_list}"


# ---------------------------------------------------------------------------
# AC2 — a stale shared build is a build/resolution diagnosis, never a failure
# of the pushed change set
# ---------------------------------------------------------------------------


class TestStaleSharedBuildSymptom:
    def test_runtime_typeerror_is_stale_shared_build(self) -> None:
        assert implement._is_stale_shared_build_failure(
            "TypeError: isNotStarted is not a function\n"
            "  at src/workflow.ts:42:9"
        )

    def test_load_error_is_not_stale_shared_build(self) -> None:
        # A load flake (missing module) must stay in the load-flake taxonomy.
        assert not implement._is_stale_shared_build_failure(
            "Cannot find package '@worklog/shared/worklog-paths'"
        )

    def test_unrelated_typeerror_is_not_stale_shared_build(self) -> None:
        assert not implement._is_stale_shared_build_failure(
            "TypeError: Cannot read properties of undefined (reading 'x')"
        )


def test_stale_dist_gate_failure_is_not_attributed_to_the_change_set(sandbox):
    """AC2: a gate refusal whose symptom is a stale shared build is reported as
    a build/resolution diagnosis naming ``packages/shared/dist`` (never
    ``test_failure``), and the push stays blocked."""
    pushes: list[tuple] = []
    comments: list[str] = []
    stale_stderr = (
        "TypeError: normalizeStartOn is not a function\n"
        "  at packages/workflow/src/start.ts:17:5"
    )

    def gate_fails(cwd, branch, commit_hash=None, **kwargs):
        evidence = kwargs.get("evidence_out")
        evidence.update(
            {
                "success": False,
                "gate_reason": "test_failure",
                "stderr_tail": stale_stderr,
                "attempts": 1,
                "retry_trail": [],
            }
        )
        return False

    with (
        _stubbed_finish(sandbox, sandbox["feature_short"], pushes),
        patch.object(implement, "git_push_to_dev", side_effect=gate_fails),
        patch.object(
            implement,
            "wl_add_comment",
            side_effect=lambda *args, **kwargs: comments.append(args[1]) or True,
        ),
        patch.object(implement, "_push_retry_sleep", lambda seconds: None),
    ):
        report = implement.phase_finish("SA-TEST", json_output=True, no_refactor=True)

    assert report["success"] is False, report["message"]
    push_step = report["steps"]["push"]
    assert push_step["reason"] == "stale_shared_build", push_step
    assert push_step["reason"] != "test_failure", push_step
    assert push_step["outcome"] == "push_failed", push_step
    assert push_step["stale_shared_build"]["artefact"] == "packages/shared/dist"
    assert "packages/shared/dist" in push_step["stale_shared_build"]["artefact_path"]
    combined = "\n".join(comments)
    assert "packages/shared/dist" in combined, combined
    assert "not by the pushed" in combined.lower(), combined


# ---------------------------------------------------------------------------
# AC2 mechanism B — built-tree freshness guard
# ---------------------------------------------------------------------------


def test_freshness_guard_rebuilds_a_tree_newer_than_the_last_build(sandbox):
    """AC2/B: when ``HEAD^{tree}`` no longer matches the built tree (a rebase,
    amend or ``git pull`` after the build), the guard rebuilds before the push
    and records it."""
    pushes: list[tuple] = []
    order: list[str] = []
    # Step 3 records the built tree as the stubbed gate tree ("abc1234"); the
    # guard then observes a different HEAD tree, forcing exactly one rebuild.
    with (
        _stubbed_finish(sandbox, sandbox["feature_short"], pushes),
        patch.object(implement, "_head_tree_sha", return_value="rebuilt-tree"),
        patch.object(implement, "run_build", side_effect=_recording_build(order)),
    ):
        report = implement.phase_finish("SA-TEST", json_output=True, no_refactor=True)

    assert report["success"] is True, report["message"]
    assert order == ["build", "build"], order
    assert len(pushes) == 1, pushes
    builds = report["steps"]["push"].get("freshness_builds")
    assert builds, report["steps"]["push"]
    assert builds[0]["reason"] == "freshness_guard", builds[0]
    assert builds[0]["success"] is True, builds[0]


def test_freshness_guard_fails_closed_with_a_stale_dist_diagnosis(sandbox):
    """AC2/B: when the freshness rebuild itself fails, the run fails closed
    with a stale-build diagnosis naming ``packages/shared/dist`` and no push."""
    pushes: list[tuple] = []
    order: list[str] = []
    comments: list[str] = []

    with (
        _stubbed_finish(sandbox, sandbox["feature_short"], pushes),
        patch.object(implement, "_head_tree_sha", return_value="rebuilt-tree"),
        patch.object(
            implement, "run_build", side_effect=_recording_build(order, fail_on=2)
        ),
        patch.object(
            implement,
            "wl_add_comment",
            side_effect=lambda *args, **kwargs: comments.append(args[1]) or True,
        ),
    ):
        report = implement.phase_finish("SA-TEST", json_output=True, no_refactor=True)

    assert report["success"] is False, report["message"]
    assert pushes == [], "an unbuilt tree must never be pushed"
    push_step = report["steps"]["push"]
    assert push_step["reason"] == "post_rebase_build_failed", push_step
    assert push_step["build"]["stale_shared_build"]["artefact"] == (
        "packages/shared/dist"
    ), push_step["build"]
    combined = "\n".join(comments)
    assert "packages/shared/dist" in combined, combined
