#!/usr/bin/env python3
"""Completion-aware wait for a background job, with a progress heartbeat.

(SA-0MVCX570P003VLBK) Replaces fixed-length ``sleep N`` polling of background
processes: wait for a job's completion marker — process exit, checkpoint file,
or a log pattern — polling at a configurable interval and returning **as soon
as** the job completes, with a timestamped progress heartbeat emitted at least
once per poll interval so an operator can ``tail -f`` a pane and watch it.

Usage::

    wait_for_job.py --timeout 3600 --pid 12345
    wait_for_job.py --timeout 3600 --marker /tmp/job.done
    wait_for_job.py --timeout 3600 --log /tmp/job.log --pattern "Audit complete"
    wait_for_job.py --timeout 3600 --pid 12345 --log /tmp/job.log \\
        --pattern "Audit complete" --interval 30 --message "audit SA-XXXX"

At least one completion marker (``--pid`` / ``--marker`` / ``--log``+``--pattern``)
is required; any marker completing ends the wait. Exit codes: 0 on completion,
1 on timeout, 2 on usage error.

Herdr sidebar: when ``HERDR_SOCKET_PATH`` and ``HERDR_PANE_ID`` are set, the
helper sends ``pane.report_agent`` messages (``working``/``blocked`` +
human-readable status) per ``herdr-agent-state-protocol.md`` so the sidecar
shows live progress; without those env vars it silently degrades.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

#: Default poll interval (seconds) — the heartbeat cadence and check frequency.
DEFAULT_INTERVAL = 60
#: Exit codes.
EXIT_COMPLETED = 0
EXIT_TIMEOUT = 1
EXIT_USAGE = 2

#: Herdr protocol identity for this helper.
HERDR_SOURCE = "wait_for_job"


def utc_now() -> str:
    """RFC3339-ish UTC timestamp for heartbeat/progress lines."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _stamp(text: str) -> str:
    return f"[{utc_now()}] {text}"


# ---------------------------------------------------------------------------
# Completion markers
# ---------------------------------------------------------------------------


class ProcessMarker:
    """Completion when the given PID no longer exists (or is a zombie).

    ``os.kill(pid, 0)`` alone is insufficient: a zombie that has not been
    reaped by its parent still answers to signal 0, so polling would hang
    until the parent reaps it. On Linux, a ``Z`` state in
    ``/proc/<pid>/stat`` means the process has exited regardless of reaping.
    """

    def __init__(self, pid: int):
        self.pid = pid

    def done(self) -> bool:
        try:
            os.kill(self.pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            # Exists but is owned by another user — still running.
            return False
        except OSError:
            return False
        # The PID answers signal 0 — is it a reaped-zombie?
        try:
            with open(f"/proc/{self.pid}/stat", "r") as fh:
                # Format: pid (comm) state ... — state is after the last ')'.
                fields = fh.read().rsplit(")", 1)[-1].split()
                return bool(fields) and fields[0] in ("Z", "X")
        except OSError:
            return False

    def describe(self) -> str:
        return f"process {self.pid}"


class MarkerFileMarker:
    """Completion when the checkpoint file appears."""

    def __init__(self, path: str):
        self.path = Path(path)

    def done(self) -> bool:
        return self.path.exists()

    def describe(self) -> str:
        return f"marker file {self.path}"


class LogPatternMarker:
    """Completion when a log line matches the regex (incremental tail)."""

    def __init__(self, path: str, pattern: str):
        self.path = Path(path)
        self._regex = re.compile(pattern)
        self._offset = 0

    def done(self) -> bool:
        try:
            size = self.path.stat().st_size
        except OSError:
            # Log not created yet — keep waiting.
            return False
        if size < self._offset:
            # Log rotated/truncated — restart from the beginning.
            self._offset = 0
        if size == self._offset:
            return False
        try:
            with self.path.open("r", errors="replace") as fh:
                fh.seek(self._offset)
                chunk = fh.read()
                self._offset = fh.tell()
        except OSError:
            return False
        return self._regex.search(chunk) is not None

    def describe(self) -> str:
        return f"log {self.path} matching {self._regex.pattern!r}"


def build_markers(args: argparse.Namespace) -> list:
    """Build completion markers from parsed CLI args (validated elsewhere)."""
    markers: list = []
    for pid in args.pid:
        markers.append(ProcessMarker(pid))
    for path in args.marker:
        markers.append(MarkerFileMarker(path))
    for log, pattern in zip(args.log, args.pattern):
        markers.append(LogPatternMarker(log, pattern))
    return markers


# ---------------------------------------------------------------------------
# Herdr sidebar reporting (fail-open)
# ---------------------------------------------------------------------------


class HerdrReporter:
    """Sends ``pane.report_agent`` messages per herdr-agent-state-protocol.md.

    Enabled only when both ``HERDR_SOCKET_PATH`` and ``HERDR_PANE_ID`` are
    set; otherwise every call is a no-op. Socket failures are swallowed —
    fire-and-forget with one short retry, silently degrading.
    """

    def __init__(self, env=None):
        env = os.environ if env is None else env
        self.socket_path = env.get("HERDR_SOCKET_PATH") or ""
        self.pane_id = env.get("HERDR_PANE_ID") or ""
        self.enabled = bool(self.socket_path and self.pane_id)
        # Protocol: seq seeded with the process-start timestamp (ms); the first
        # message carries the seed itself, each subsequent message seq+1.
        self._seq = int(time.time() * 1000)
        self._counter = 0

    def report(self, state: str, message: str) -> bool:
        """Best-effort report of *state* with a human-readable *message*."""
        if not self.enabled:
            return False
        millis = int(time.time() * 1000)
        payload = {
            "id": f"herdr:{HERDR_SOURCE}:{millis}:{self._counter:x}",
            "method": "pane.report_agent",
            "params": {
                "pane_id": self.pane_id,
                "source": HERDR_SOURCE,
                "agent": HERDR_SOURCE,
                "state": state,
                "seq": self._seq,
                "message": message,
            },
        }
        self._seq += 1
        self._counter += 1
        line = json.dumps(payload) + "\n"
        for timeout_ms in (500, 1500):
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                    sock.settimeout(timeout_ms / 1000.0)
                    sock.connect(self.socket_path)
                    sock.sendall(line.encode("utf-8"))
                return True
            except (OSError, ValueError):
                continue
        return False


# ---------------------------------------------------------------------------
# The wait loop
# ---------------------------------------------------------------------------


def wait_for_job(
    markers,
    *,
    timeout: float,
    interval: float = DEFAULT_INTERVAL,
    message: str = "",
    reporter: HerdrReporter | None = None,
    json_output: bool = False,
) -> int:
    """Wait until any *markers* completes, the *timeout* elapses, or SIGINT.

    Emits a timestamped heartbeat at least once per poll *interval* while
    waiting. Returns the process exit code (0 completed / 1 timeout / 130
    interrupted).
    """
    reporter = reporter or HerdrReporter()
    start = time.monotonic()
    description = message or "job completion"

    def emit(event: str, text: str, elapsed: float | None = None) -> None:
        if json_output:
            row = {
                "ts": utc_now(),
                "event": event,
                "message": text,
            }
            if elapsed is not None:
                row["elapsed"] = round(elapsed, 1)
            print(json.dumps(row), flush=True)
        else:
            print(_stamp(f"{event}: {text}"), flush=True)

    def interrupted(_sig, _frame):
        raise KeyboardInterrupt

    old_int = signal.signal(signal.SIGINT, interrupted)
    old_term = signal.signal(signal.SIGTERM, interrupted)
    try:
        emit("started", f"waiting for {description}")
        reporter.report("working", f"waiting for {description}")

        while True:
            elapsed = time.monotonic() - start
            if any(m.done() for m in markers):
                emit("completed", f"{description} completed after {elapsed:.1f}s", elapsed)
                return EXIT_COMPLETED
            if elapsed >= timeout:
                text = f"timed out after {timeout:g}s waiting for {description}"
                emit("timeout", text, elapsed)
                reporter.report("blocked", text)
                return EXIT_TIMEOUT
            emit("heartbeat", f"waiting for {description} "
                              f"(elapsed {elapsed:.0f}s / timeout {timeout:g}s)", elapsed)
            reporter.report("working", f"waiting for {description} "
                                       f"(elapsed {elapsed:.0f}s / timeout {timeout:g}s)")
            time.sleep(min(interval, timeout - elapsed))
    except KeyboardInterrupt:
        emit("interrupted", f"wait interrupted after {time.monotonic() - start:.1f}s")
        return 130
    finally:
        signal.signal(signal.SIGINT, old_int)
        signal.signal(signal.SIGTERM, old_term)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wait_for_job.py",
        description="Completion-aware wait for a background job with a progress "
                    "heartbeat (SA-0MVCX570P003VLBK).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Exit codes: 0 completed, 1 timeout, 2 usage error.\n"
               "Herdr: set HERDR_SOCKET_PATH + HERDR_PANE_ID to report "
               "working/blocked to the sidebar (silently optional).",
    )
    parser.add_argument("--pid", type=int, action="append", default=[],
                        help="wait until this process exits (repeatable)")
    parser.add_argument("--marker", action="append", default=[],
                        help="wait until this checkpoint file appears (repeatable)")
    parser.add_argument("--log", action="append", default=[],
                        help="tail this log file (repeatable)")
    parser.add_argument("--pattern", action="append", default=[],
                        help="regex that marks completion in the matching --log")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL,
                        help=f"poll interval / heartbeat cadence in seconds "
                             f"(default {DEFAULT_INTERVAL})")
    parser.add_argument("--timeout", type=float, required=True,
                        help="hard timeout in seconds (required)")
    parser.add_argument("--message", default="",
                        help="human-readable job description for the heartbeat")
    parser.add_argument("--json", action="store_true",
                        help="emit newline-delimited JSON progress lines")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    if not (args.pid or args.marker or args.log):
        print('error: at least one of --pid, --marker or --log is required',
              file=sys.stderr)
        return EXIT_USAGE
    if args.log and len(args.log) != len(args.pattern):
        print("error: --pattern must be provided once per --log", file=sys.stderr)
        return EXIT_USAGE
    if args.pattern and not args.log:
        print("error: --pattern requires --log", file=sys.stderr)
        return EXIT_USAGE
    if args.timeout <= 0:
        print("error: --timeout must be positive", file=sys.stderr)
        return EXIT_USAGE
    if args.interval <= 0:
        print("error: --interval must be positive", file=sys.stderr)
        return EXIT_USAGE
    try:
        markers = build_markers(args)  # compiles --pattern regexes (raises re.error)
    except re.error as exc:
        print(f"error: invalid regex: {exc}", file=sys.stderr)
        return EXIT_USAGE

    return wait_for_job(
        markers,
        timeout=args.timeout,
        interval=args.interval,
        message=args.message,
        json_output=args.json,
    )


if __name__ == "__main__":
    sys.exit(main())
