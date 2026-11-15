"""Push transport-retry policy for ``git_push_to_dev`` (SA-0MVSJX5HS002DJHI).

AC4: a transient transport close (``Connection ... closed by remote host``,
``Connection reset by peer``, ``remote end hung up unexpectedly``,
``kex_exchange_identification``) is retried up to a bounded number of attempts
with jittered exponential backoff **from the same cwd**; AC1
(SA-0MW22SEMV00230O5) extends the retryable set to the observed
concurrent-run auth/transport failures (``Permission denied (publickey)``,
``Could not read from remote repository``, connect timeouts, a remote
``Internal Server Error``). Branch-policy rejections and other content
conditions fail fast — retrying cannot fix them (non-fast-forward keeps its
dedicated rebase recovery), and a timeout is never retried.

Hermetic: ``subprocess.Popen`` is replaced with a scripted fake process and the
backoff sleep seam is recorded, so no git process runs and no wall-clock time
passes. The gate's machine-readable ``gate_reason`` is carried into
``evidence_out`` on failure for ``implement.py finish`` to report (AC2).
"""
from __future__ import annotations

import logging
import os
import random
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) in sys.path:
    sys.path.remove(str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT))

from skill.implement.scripts import implement

TRANSPORT_CLOSE = (
    b"kex_exchange_identification: Connection closed by remote host\n"
    b"Connection to github.com closed by remote host.\n"
    b"fatal: Could not read from remote repository.\n"
)
AUTH_FAILURE = (
    b"git@github.com: Permission denied (publickey).\n"
    b"fatal: Could not read from remote repository.\n"
)


class _FakeProc:
    """A minimal stand-in for a ``git push`` child process."""

    pid = 424242

    def __init__(self, returncode: int, stderr: bytes, timeout: bool = False) -> None:
        self.returncode = returncode
        self._stderr = stderr
        self._timeout = timeout

    def communicate(self, timeout=None):
        if self._timeout:
            raise subprocess.TimeoutExpired(cmd="git push", timeout=timeout or 0)
        return (b"", self._stderr)

    def wait(self):
        return self.returncode


#: The real ``Popen`` — non-push commands (e.g. the evidence path's
#: ``git rev-parse``) must still run so the fixture's git state is readable.
_REAL_POPEN = subprocess.Popen


def _scripted_popen(script, calls):
    """Return a ``Popen`` replacement yielding *script* entries in order.

    Only ``git push`` invocations are faked and recorded; every other command
    (the success path's ``git rev-parse`` evidence probe) delegates to the real
    ``Popen`` so the fixture is not starved of its script.
    """
    iterator = iter(script)

    def factory(argv, **kwargs):
        if list(argv[:2]) != ["git", "push"]:
            return _REAL_POPEN(argv, **kwargs)
        calls.append(
            {
                "argv": list(argv),
                "cwd": kwargs.get("cwd"),
                "env": kwargs.get("env"),
            }
        )
        item = next(iterator)
        if item == "timeout":
            return _FakeProc(-9, b"", timeout=True)
        returncode, stderr = item
        return _FakeProc(returncode, stderr)

    return factory


@pytest.fixture
def push_harness(monkeypatch, tmp_path):
    """Drive ``git_push_to_dev`` with a scripted push and recorded backoff."""
    calls: list[dict] = []
    sleeps: list[float] = []
    kills: list[int] = []

    def install(script):
        calls.clear()
        sleeps.clear()
        kills.clear()
        monkeypatch.setattr(
            implement.subprocess, "Popen", _scripted_popen(script, calls)
        )
        monkeypatch.setattr(
            implement, "_push_retry_sleep", lambda seconds: sleeps.append(seconds)
        )
        monkeypatch.setattr(
            implement, "_kill_process_group", lambda pid: kills.append(pid)
        )

    return {
        "cwd": str(tmp_path),
        "calls": calls,
        "sleeps": sleeps,
        "kills": kills,
        "install": install,
    }


# ── Retryable vs non-retryable stderr ───────────────────────────────────────


class TestRetryablePatterns:
    @pytest.mark.parametrize(
        "message",
        [
            "Connection to github.com closed by remote host.",
            "Connection reset by peer",
            "fatal: the remote end hung up unexpectedly",
            "kex_exchange_identification: read: Connection reset",
            # AC1 (SA-0MW22SEMV00230O5): the concurrent-run auth/transport
            # signatures are classified as transient and retried.
            "git@github.com: Permission denied (publickey).",
            "fatal: Could not read from remote repository.",
            "ssh: connect to host github.com port 22: Connection timed out",
            "! [remote rejected]   HEAD -> dev (Internal Server Error)",
        ],
    )
    def test_transport_messages_are_retryable(self, message: str) -> None:
        assert implement._is_retryable_push_transport(message) is True

    @pytest.mark.parametrize(
        "message",
        [
            # Content rejections and gate refusals are never retried as
            # transport (AC1: distinct from a real content rejection).
            "remote: error: GH006: Protected branch update failed",
            "! [rejected]  wl-x -> dev (non-fast-forward)",
            "gate_reason=slot_contention",
            "",
        ],
    )
    def test_real_errors_are_not_retryable(self, message: str) -> None:
        assert implement._is_retryable_push_transport(message) is False


# ── Backoff policy ──────────────────────────────────────────────────────────


class TestBackoff:
    def test_backoff_grows_exponentially(self) -> None:
        rng = random.Random(0)
        first = implement._push_transport_backoff(1, rng)
        second = implement._push_transport_backoff(2, rng)
        assert first < second

    def test_backoff_is_jittered(self) -> None:
        values = {
            implement._push_transport_backoff(1, random.Random(seed))
            for seed in range(1, 20)
        }
        assert len(values) > 1, "concurrent pushers would retry in lockstep"

    def test_backoff_is_capped(self) -> None:
        for seed in range(20):
            value = implement._push_transport_backoff(10, random.Random(seed))
            assert value <= implement.PUSH_RETRY_CAP_SECONDS * 1.25

    def test_single_attempt_budget_covers_initial_plus_two_retries(self) -> None:
        assert implement.PUSH_RETRY_MAX_ATTEMPTS == 3


# ── git_push_to_dev retry behaviour ─────────────────────────────────────────


class TestGitPushToDevRetry:
    def test_transient_close_is_retried_then_succeeds(self, push_harness):
        push_harness["install"](
            [
                (128, TRANSPORT_CLOSE),
                (128, TRANSPORT_CLOSE),
                (0, b""),
            ]
        )

        result = implement.git_push_to_dev(
            push_harness["cwd"], "wl-test", "abc123"
        )

        assert result is True
        assert len(push_harness["calls"]) == 3
        assert len(push_harness["sleeps"]) == 2

    def test_retry_reuses_same_cwd_argv_and_env_without_bypass(self, push_harness):
        """AC3: a transport retry is byte-identical to the first attempt.

        Same cwd, same argv, same env — and none of the documented bypass
        injections (``--no-verify``, ``TEST_SCOPE_SKIP``) may appear, so the
        pre-push gate runs again against the same tree and serves its cached
        full-suite entry.
        """
        push_harness["install"](
            [(128, TRANSPORT_CLOSE), (128, TRANSPORT_CLOSE), (0, b"")]
        )

        implement.git_push_to_dev(push_harness["cwd"], "wl-test", "abc123")

        calls = push_harness["calls"]
        assert {call["cwd"] for call in calls} == {push_harness["cwd"]}

        expected_argv = [
            "git", "push", implement.PUSH_REMOTE_NAME, "wl-test:refs/heads/dev",
        ]
        for call in calls:
            assert call["argv"] == expected_argv, (
                "a retry must re-run the identical push; no --no-verify or "
                f"other injected args (got {call['argv']})"
            )

        envs = [call["env"] or {} for call in calls]
        assert len({tuple(sorted(env.items())) for env in envs}) == 1, (
            "a retry must re-run with the identical env so the gate re-serves "
            f"its cached entry; env varied across attempts: {envs}"
        )
        ambient = set(os.environ.items())
        for env in envs:
            extras = set(env.items()) - ambient
            # ``GIT_SSH_COMMAND`` carries the SSH keepalive (SA-0MVWNHLQQ002TFZR)
            # and ``TMPDIR`` the disk-backed scratch dir: neither bypasses the
            # gate. Anything else (e.g. TEST_SCOPE_SKIP) is forbidden.
            assert all(
                key in ("TMPDIR", "GIT_SSH_COMMAND") for key, _ in extras
            ), (
                "a retry must not inject TEST_SCOPE_SKIP or any other bypass "
                f"env; unexpected keys: {extras}"
            )
            assert "ServerAliveInterval" in env.get("GIT_SSH_COMMAND", ""), (
                "every attempt must carry the SSH keepalive so the gate cannot "
                "idle out the connection"
            )

    def test_transport_retry_is_bounded(self, push_harness):
        transport = (128, TRANSPORT_CLOSE)
        push_harness["install"]([transport] * implement.PUSH_RETRY_MAX_ATTEMPTS)

        with pytest.raises(implement.PushTransportLostError):
            implement.git_push_to_dev(
                push_harness["cwd"], "wl-test", "abc123"
            )

        assert len(push_harness["calls"]) == implement.PUSH_RETRY_MAX_ATTEMPTS
        assert len(push_harness["sleeps"]) == implement.PUSH_RETRY_MAX_ATTEMPTS - 1

    def test_exhausted_trail_reports_attempts_backoff_and_final_reason(
        self, push_harness, caplog
    ):
        """AC4: an exhausted retry trail reports the attempt count, the
        backoff actually used and the final reason (via the log trail).
        """
        transport = (128, TRANSPORT_CLOSE)
        push_harness["install"]([transport] * implement.PUSH_RETRY_MAX_ATTEMPTS)

        with (
            caplog.at_level(logging.WARNING, logger=implement.LOG.name),
            pytest.raises(implement.PushTransportLostError),
        ):
            implement.git_push_to_dev(
                push_harness["cwd"], "wl-test", "abc123"
            )
        assert len(push_harness["calls"]) == implement.PUSH_RETRY_MAX_ATTEMPTS
        assert len(push_harness["sleeps"]) == implement.PUSH_RETRY_MAX_ATTEMPTS - 1

        records = [r for r in caplog.records if r.name == implement.LOG.name]
        retries = [
            r.getMessage() for r in records if "retrying in" in r.getMessage()
        ]
        assert len(retries) == implement.PUSH_RETRY_MAX_ATTEMPTS - 1, (
            f"the trail must log each retry with its backoff; got {retries}"
        )
        for index, message in enumerate(retries):
            match = re.search(
                r"attempt (\d)/\d failed.*retrying in ([\d.]+)s", message
            )
            assert match is not None, (
                f"retry record must name the attempt and backoff: {message}"
            )
            attempt, logged_delay = int(match.group(1)), float(match.group(2))
            assert attempt == index + 1
            assert logged_delay == pytest.approx(
                push_harness["sleeps"][index], abs=0.06
            ), "the trail must report the backoff that was actually used"

        final = next(
            (
                r.getMessage()
                for r in records
                if "attempt" in r.getMessage()
                and "retrying" not in r.getMessage()
            ),
            "",
        )
        assert f"attempt {implement.PUSH_RETRY_MAX_ATTEMPTS}/" in final
        assert "Could not read from remote repository." in final, (
            "the exhausted trail must end with the final reason (the stderr): "
            f"{final}"
        )

    def test_content_rejection_fails_fast(self, push_harness):
        # A branch-policy rejection is a *content* condition — retrying it
        # cannot fix it, so it must fail immediately (AC1: distinct from the
        # transient transport set).
        content = (
            b"remote: error: GH006: Protected branch update failed for "
            b"refs/heads/dev.\n"
            b"remote: This branch is protected and requires a pull request.\n"
        )
        push_harness["install"]([(128, content)])

        result = implement.git_push_to_dev(
            push_harness["cwd"], "wl-test", "abc123"
        )

        assert result is False
        assert len(push_harness["calls"]) == 1, (
            "content rejections must not retry"
        )
        assert push_harness["sleeps"] == []

    def test_transient_auth_is_retried_then_succeeds(self, push_harness):
        """AC1: an auth refusal is transient infrastructure, not a fail-fast.

        The observed concurrent-run signature ``Permission denied (publickey)``
        is retried on the bounded budget with backoff, so a leaf `finish` no
        longer dies on one flaky key handshake (SA-0MW22SEMV00230O5).
        """
        push_harness["install"](
            [(128, AUTH_FAILURE), (128, AUTH_FAILURE), (0, b"")]
        )

        result = implement.git_push_to_dev(
            push_harness["cwd"], "wl-test", "abc123"
        )

        assert result is True
        assert len(push_harness["calls"]) == 3
        assert len(push_harness["sleeps"]) == 2

    def test_auth_exhaustion_raises_the_distinct_transport_error(
        self, push_harness
    ):
        """AC1: exhausted auth is *transport* infrastructure, never a bare
        "push failed" and never the misleading "push manually" hint."""
        push_harness["install"]([(128, AUTH_FAILURE)] * implement.PUSH_RETRY_MAX_ATTEMPTS)
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

    def test_non_fast_forward_still_raises_without_retry(self, push_harness):
        stderr = (
            b"! [rejected]  wl-test -> dev (non-fast-forward)\n"
            b"error: failed to push some refs\n"
        )
        push_harness["install"]([(1, stderr)])

        with pytest.raises(implement.PushNonFastForwardError):
            implement.git_push_to_dev(push_harness["cwd"], "wl-test", "abc123")

        assert len(push_harness["calls"]) == 1
        assert push_harness["sleeps"] == []

    def test_timeout_raises_without_retry(self, push_harness):
        push_harness["install"](["timeout"])

        with pytest.raises(implement.PushTimeoutError):
            implement.git_push_to_dev(
                push_harness["cwd"], "wl-test", "abc123", timeout=1
            )

        assert len(push_harness["calls"]) == 1
        assert push_harness["sleeps"] == []
        assert push_harness["kills"] == [_FakeProc.pid]


# ── Gate reason carried on failure (AC2) ────────────────────────────────────


class TestGateReasonEvidence:
    def test_failed_gate_reason_and_stderr_tail_reach_the_caller(
        self, push_harness
    ):
        stderr = (
            b"pre-push: full-suite gate for dev/main push\n"
            b"gate_reason=slot_contention\n"
            b"ERROR: No free test slot\n"
        )
        push_harness["install"]([(1, stderr)])
        evidence: dict = {}

        result = implement.git_push_to_dev(
            push_harness["cwd"], "wl-test", "abc123", evidence_out=evidence
        )

        assert result is False
        assert evidence["success"] is False
        assert evidence["gate_reason"] == "slot_contention"
        assert "No free test slot" in evidence["stderr_tail"]

    def test_result_without_a_gate_reason_reports_none(self, push_harness):
        push_harness["install"]([(1, b"some other push failure\n")])
        evidence: dict = {}

        implement.git_push_to_dev(
            push_harness["cwd"], "wl-test", "abc123", evidence_out=evidence
        )

        assert evidence["success"] is False
        assert evidence["gate_reason"] is None


# ── Retry trail carried on the evidence (AC3, SA-0MVXEQEI500641SJ) ──────────


class TestRetryTrailEvidence:
    """The attempt trail must reach ``evidence_out``, not only the log.

    ``finish`` exposes this on ``report["steps"]["push"]`` so a ``--json``
    consumer can see how many attempts a dev push made and the backoff it
    actually slept (SA-0MVXEQEI500641SJ AC3).
    """

    def test_success_after_a_retry_exposes_the_trail(self, push_harness):
        push_harness["install"]([(128, TRANSPORT_CLOSE), (0, b"")])
        evidence: dict = {}

        result = implement.git_push_to_dev(
            push_harness["cwd"], "wl-test", "abc123", evidence_out=evidence
        )

        assert result is True
        assert evidence["attempts"] == 2
        trail = evidence["retry_trail"]
        assert len(trail) == 1, trail
        assert trail[0]["attempt"] == 1
        assert trail[0]["reason"] == "transport_close"
        assert trail[0]["delay_seconds"] == pytest.approx(
            push_harness["sleeps"][0], abs=0.001
        ), "the trail must report the backoff actually slept"

    def test_exhausted_trail_is_exposed_on_failure(self, push_harness):
        transport = (128, TRANSPORT_CLOSE)
        push_harness["install"]([transport] * implement.PUSH_RETRY_MAX_ATTEMPTS)
        evidence: dict = {}

        with pytest.raises(implement.PushTransportLostError):
            implement.git_push_to_dev(
                push_harness["cwd"], "wl-test", "abc123", evidence_out=evidence
            )

        assert evidence["success"] is False
        assert evidence["reason"] == "connection_closed"
        assert evidence["transport_lost"] is True
        assert evidence["attempts"] == implement.PUSH_RETRY_MAX_ATTEMPTS
        trail = evidence["retry_trail"]
        assert [entry["attempt"] for entry in trail] == [1, 2], trail
        for entry, slept in zip(trail, push_harness["sleeps"]):
            assert entry["delay_seconds"] == pytest.approx(slept, abs=0.001)

    def test_fail_fast_still_reports_a_single_attempt(self, push_harness):
        # A content rejection (branch policy) is the fail-fast case now: it
        # returns False on the first attempt with a distinct ``content``
        # reason, never retried (AC1).
        content = (
            b"remote: error: GH006: Protected branch update failed for "
            b"refs/heads/dev.\n"
        )
        push_harness["install"]([(128, content)])
        evidence: dict = {}

        implement.git_push_to_dev(
            push_harness["cwd"], "wl-test", "abc123", evidence_out=evidence
        )

        assert evidence["attempts"] == 1
        assert evidence["retry_trail"] == []
        assert evidence["reason"] == "content"


class TestParseGateReason:
    def test_last_gate_reason_line_wins(self) -> None:
        stderr = "gate_reason=unknown\ngate_reason=timeout\n"
        assert implement._parse_gate_reason(stderr) == "timeout"

    def test_no_line_returns_none(self) -> None:
        assert implement._parse_gate_reason("nothing here") is None

    def test_lookalike_text_is_not_a_reason_line(self) -> None:
        assert implement._parse_gate_reason("note: gate_reason=timeout") is None
