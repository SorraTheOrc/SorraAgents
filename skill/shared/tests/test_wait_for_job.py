"""Tests for the shared completion-aware wait helper (SA-0MVCX570P003VLBK).

The helper replaces fixed-length ``sleep N`` polling of background jobs with a
completion-aware wait: it returns the moment the job's completion marker
appears (process exit / checkpoint file / log pattern) and emits a timestamped
progress heartbeat at least once per poll interval, optionally reporting
``working``/``blocked`` to the herdr sidebar. These tests exercise the
observable CLI behaviour: exit codes, early exit, timeout bound, heartbeat
cadence, herdr reporting, and silent degradation when herdr env vars are
absent.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent

#: The helper CLI under test (the public API exercised through subprocess runs).
_HELPER = Path(__file__).resolve().parent.parent / "wait_for_job.py"
#: Exit codes of the helper CLI.
EXIT_COMPLETED = 0
EXIT_TIMEOUT = 1
EXIT_USAGE = 2


def _run(args, env=None, timeout=90):
    """Run the helper CLI as a subprocess (public API = the CLI)."""
    run_env = dict(os.environ) if env is None else env
    return subprocess.run(
        [sys.executable, str(_HELPER), *args],
        capture_output=True,
        text=True,
        env=run_env,
        timeout=timeout,
        check=False,
    )


def _spawn_job(script, script_args=()):
    """Start a background job (nohup-style) and return the Popen handle."""
    return subprocess.Popen(
        [sys.executable, "-c", script, *script_args],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


# ---------------------------------------------------------------------------
# AC1 — CLI contract: exit 0 on completion, non-zero on timeout/failure
# ---------------------------------------------------------------------------


class TestCliContract:
    def test_exit_zero_when_marker_exists(self):
        """A checkpoint file that already exists completes immediately (exit 0)."""
        with tempfile.TemporaryDirectory() as td:
            marker = Path(td) / "done.marker"
            marker.write_text("ok")
            result = _run(["--marker", str(marker), "--timeout", "10"])
            assert result.returncode == EXIT_COMPLETED, result.stderr

    def test_exit_nonzero_on_usage_error(self):
        """No completion condition is a usage error (exit 2)."""
        result = _run(["--timeout", "10"])
        assert result.returncode == EXIT_USAGE
        assert "required" in (result.stderr or "").lower()

    def test_log_pattern_requires_matching_log(self):
        """A --pattern without a corresponding --log is a usage error."""
        result = _run(["--pattern", "x", "--timeout", "10"])
        assert result.returncode == EXIT_USAGE


# ---------------------------------------------------------------------------
# AC2 — early exit: a ~5 s job with poll interval 1 s returns in < ~20 s
# ---------------------------------------------------------------------------


class TestEarlyExit:
    def test_five_second_job_returns_well_under_twenty_seconds(self):
        """A job completing after ~5 s is detected at the next poll — no fixed long sleep."""
        with tempfile.TemporaryDirectory() as td:
            marker = Path(td) / "job.marker"
            job = _spawn_job(
                "import sys,time; time.sleep(5); open(sys.argv[1],'w').close()",
                [str(marker)],
            )
            try:
                start = time.monotonic()
                result = _run(
                    ["--marker", str(marker), "--timeout", "60", "--interval", "1"]
                )
                elapsed = time.monotonic() - start
                assert result.returncode == EXIT_COMPLETED, result.stderr
                assert elapsed < 20.0, f"early exit failed: took {elapsed:.1f}s"
                assert elapsed >= 4.0, f"suspiciously fast: {elapsed:.1f}s"
            finally:
                job.wait(timeout=15)

    def test_pid_exit_completion(self):
        """Waiting on a PID returns once that process exits (marker: process exit)."""
        job = _spawn_job("import time; time.sleep(2)")
        try:
            result = _run(["--pid", str(job.pid), "--timeout", "30", "--interval", "1"])
            assert result.returncode == EXIT_COMPLETED, result.stderr
        finally:
            job.wait(timeout=15)

    def test_log_pattern_completion(self):
        """A log line matching the pattern completes the wait (marker: log pattern)."""
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "job.log"
            log.write_text("")
            job = _spawn_job(
                "import sys,time; time.sleep(1); "
                "open(sys.argv[1],'a').write('Audit complete\\n')",
                [str(log)],
            )
            try:
                result = _run(
                    ["--log", str(log), "--pattern", "Audit complete",
                     "--timeout", "30", "--interval", "1"]
                )
                assert result.returncode == EXIT_COMPLETED, result.stderr
            finally:
                job.wait(timeout=15)


# ---------------------------------------------------------------------------
# AC3 — timeout bound honoured: a job that never completes exits non-zero
# ---------------------------------------------------------------------------


class TestTimeoutBound:
    def test_timeout_bound_is_honoured(self):
        """A never-completing job exits non-zero at (about) the configured timeout."""
        with tempfile.TemporaryDirectory() as td:
            never = Path(td) / "never.marker"
            start = time.monotonic()
            result = _run(["--marker", str(never), "--timeout", "3", "--interval", "1"])
            elapsed = time.monotonic() - start
            assert result.returncode == EXIT_TIMEOUT, result.stdout
            assert elapsed >= 3.0, f"returned before timeout: {elapsed:.1f}s"
            assert elapsed < 12.0, f"timeout overshot badly: {elapsed:.1f}s"


# ---------------------------------------------------------------------------
# AC4 — a timestamped heartbeat line at least once per poll interval
# ---------------------------------------------------------------------------


class TestHeartbeat:
    def test_heartbeat_emitted_at_least_once_per_poll_interval(self):
        """While waiting, a timestamped heartbeat appears at least once per interval."""
        with tempfile.TemporaryDirectory() as td:
            never = Path(td) / "never.marker"
            result = _run(["--marker", str(never), "--timeout", "4", "--interval", "1"])
            assert result.returncode == EXIT_TIMEOUT
            lines = [l for l in result.stdout.splitlines() if l.strip()]
            heartbeats = [l for l in lines if "waiting" in l]
            # Heartbeats at t ~ 0, 1, 2, 3 (one per interval over 4 s).
            assert len(heartbeats) >= 3, f"too few heartbeats: {heartbeats!r}"
            for line in heartbeats:
                assert re.match(r"^\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\]", line), line

    def test_json_heartbeat_lines_are_structured(self):
        """--json emits newline-delimited JSON lines with ts/event/elapsed fields."""
        with tempfile.TemporaryDirectory() as td:
            marker = Path(td) / "done.marker"
            marker.write_text("ok")
            result = _run(["--marker", str(marker), "--timeout", "10", "--json"])
            assert result.returncode == EXIT_COMPLETED, result.stderr
            payloads = [json.loads(l) for l in result.stdout.splitlines() if l.strip()]
            assert payloads, "no JSON output"
            assert payloads[-1]["event"] == "completed"
            assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", payloads[-1]["ts"])
            assert "elapsed" in payloads[-1]


# ---------------------------------------------------------------------------
# AC5 — herdr sidebar reporting: sent when env vars set, silent degradation
# ---------------------------------------------------------------------------


class SpyHerdrServer:
    """A tiny Unix-socket server that captures JSON lines sent by the helper."""

    def __init__(self, sock_path):
        self.sock_path = str(sock_path)
        self.received: list[str] = []
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(self.sock_path)
        self._server.listen(1)
        self._server.settimeout(0.2)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            with conn:
                try:
                    conn.settimeout(2)
                    data = conn.recv(65536)
                    if data:
                        self.received.append(data.decode(errors="replace"))
                except OSError:
                    pass

    def close(self):
        self._stop.set()
        try:
            self._server.close()
        except OSError:
            pass
        self._thread.join(timeout=2)


class TestHerdrReporting:
    def test_report_agent_sent_when_env_vars_set(self):
        """HERDR_SOCKET_PATH + HERDR_PANE_ID set → a pane.report_agent message is sent."""
        with tempfile.TemporaryDirectory() as td:
            sock_path = Path(td) / "herdr.sock"
            marker = Path(td) / "job.marker"
            spy = SpyHerdrServer(sock_path)
            job = _spawn_job(
                "import sys,time; time.sleep(1); open(sys.argv[1],'w').close()",
                [str(marker)],
            )
            env = dict(os.environ)
            env["HERDR_SOCKET_PATH"] = str(sock_path)
            env["HERDR_PANE_ID"] = "w1:p1"
            try:
                result = _run(
                    ["--marker", str(marker), "--timeout", "30", "--interval", "1"],
                    env=env,
                )
                assert result.returncode == EXIT_COMPLETED, result.stderr
                time.sleep(0.3)  # let the spy thread drain pending connections
                assert spy.received, "no pane.report_agent message received"
                first = json.loads(spy.received[0].strip().splitlines()[0])
                assert first["method"] == "pane.report_agent"
                params = first["params"]
                assert params["state"] == "working"
                assert params["pane_id"] == "w1:p1"
                assert params["agent"] == "wait_for_job"
                assert str(first["id"]).startswith("herdr:")
                assert isinstance(params["seq"], int) and params["seq"] > 0
            finally:
                job.wait(timeout=15)
                spy.close()

    def test_silent_degradation_without_env(self):
        """No HERDR_* env vars → the helper completes normally with no error."""
        with tempfile.TemporaryDirectory() as td:
            marker = Path(td) / "job.marker"
            job = _spawn_job(
                "import sys,time; time.sleep(1); open(sys.argv[1],'w').close()",
                [str(marker)],
            )
            env = {k: v for k, v in os.environ.items() if not k.startswith("HERDR_")}
            try:
                result = _run(
                    ["--marker", str(marker), "--timeout", "30", "--interval", "1"],
                    env=env,
                )
                assert result.returncode == EXIT_COMPLETED, result.stderr
                assert "error" not in (result.stderr or "").lower()
            finally:
                job.wait(timeout=15)

    def test_fail_open_when_socket_unreachable(self):
        """A socket path with no listener is silently ignored (fail-open)."""
        with tempfile.TemporaryDirectory() as td:
            marker = Path(td) / "job.marker"
            env = dict(os.environ)
            env["HERDR_SOCKET_PATH"] = str(Path(td) / "missing.sock")
            env["HERDR_PANE_ID"] = "w1:p1"
            job = _spawn_job(
                "import sys,time; time.sleep(1); open(sys.argv[1],'w').close()",
                [str(marker)],
            )
            try:
                result = _run(
                    ["--marker", str(marker), "--timeout", "30", "--interval", "1"],
                    env=env,
                )
                assert result.returncode == EXIT_COMPLETED, result.stderr
            finally:
                job.wait(timeout=15)


# ---------------------------------------------------------------------------
# AC6 — the implement skill documents the background-job etiquette pattern
# ---------------------------------------------------------------------------


class TestDocumentation:
    def test_implement_skill_documents_background_job_etiquette(self):
        """AC6: the implement skill documents the etiquette and deprecates sleep polling."""
        skill_md = REPO_ROOT / "skill" / "implement" / "SKILL.md"
        text = skill_md.read_text()
        assert "Background Job Etiquette" in text
        assert "sleep" in text.lower()
        assert "wait_for_job.py" in text
