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
shows live progress; without those env vars it silently degrades. A startup
``herdr_status`` line reports whether sidebar reporting is enabled (and for
which pane), so an operator can see at a glance why a sidebar is (not)
updating.

Completion feedback: the final line is followed by a terminal bell (``\a``)
in text mode, and — when ``WAIT_NOTIFY=1`` and a ``notify-send`` binary is on
``PATH`` — a desktop notification is fired (non-blocking, fail-soft; disabled
by default so headless automation is never spammed).

Live job output: by default the helper tails the job's output — an explicit
``--log FILE``, otherwise the file a ``--pid`` process' stdout/stderr is
redirected to (``/proc/<pid>/fd/1``) — and prints only the lines written since
the previous poll, capped at ``--tail-lines`` (default 10). Every poll also
prints a compact ``progress:`` line with process vitals (state/cpu/rss/
threads/children/elapsed), so an unreadable (pipe/socket) stdout still shows
liveness. ``--no-tail`` disables output tailing.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import socket
import subprocess
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
#: Env var that opts into a desktop notification on completion (fail-closed).
NOTIFY_ENV = "WAIT_NOTIFY"


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
# Live job output: source discovery + incremental tail (SA-0MVDQF3VW002PKCA)
# ---------------------------------------------------------------------------

#: Default number of output lines shown per poll.
DEFAULT_TAIL_LINES = 10


def _readable_log_for_pid(pid: int) -> str:
    """Return the file a process' stdout/stderr is redirected to, if any.

    Background jobs are usually launched as ``nohup cmd > /tmp/job.log 2>&1 &``,
    so ``/proc/<pid>/fd/1`` resolves to the log path. Pipe- or socket-backed
    stdio cannot be read retroactively and yields an empty string.
    """
    for fd in (1, 2):
        try:
            target = os.readlink(f"/proc/{pid}/fd/{fd}")
        except OSError:
            continue
        if target.startswith("/") and os.path.isfile(target):
            return target
    return ""


class JobOutputTail:
    """Tails a job's output file, emitting only lines written since the last poll.

    Polling is offset-based: the first call reports the last *tail_lines* lines,
    and each later call reports only newly appended lines (capped at
    *tail_lines*). Truncation/rotation (size shrink) restarts from the current
    tail instead of failing. ``source`` is empty when no readable output was
    found, in which case :meth:`new_lines` always returns an empty list.
    """

    def __init__(self, source: str = "", tail_lines: int = DEFAULT_TAIL_LINES):
        self.source = Path(source) if source else None
        self.tail_lines = max(1, int(tail_lines))
        self._offset = 0

    @classmethod
    def for_job(cls, logs, pids, tail_lines: int = DEFAULT_TAIL_LINES) -> JobOutputTail:
        """Build a tail preferring an explicit --log, else a --pid's redirect."""
        for path in logs:
            return cls(path, tail_lines)
        for pid in pids:
            found = _readable_log_for_pid(pid)
            if found:
                return cls(found, tail_lines)
        return cls("", tail_lines)

    def new_lines(self) -> list:
        """Return output lines appended since the previous call (capped)."""
        if self.source is None:
            return []
        try:
            size = self.source.stat().st_size
        except OSError:
            return []
        if size < self._offset:
            # Rotated/truncated — start over from the current tail.
            self._offset = 0
        if size == self._offset:
            return []
        try:
            with self.source.open("r", errors="replace") as fh:
                if self._offset == 0:
                    # First poll: seed with the current tail rather than history.
                    fh.seek(0, os.SEEK_END)
                    head = max(0, fh.tell() - 64 * 1024)
                    fh.seek(head)
                else:
                    fh.seek(self._offset)
                chunk = fh.read()
                self._offset = fh.tell()
        except OSError:
            return []
        lines = [line for line in chunk.splitlines() if line.strip()]
        return lines[-self.tail_lines:]


class ProcessVitals:
    """Best-effort process vitals from /proc, for waits whose output is unreadable."""

    def __init__(self, pid: int):
        self.pid = pid

    def summary(self) -> str:
        """Return ``state=… cpu=… rss=… threads=… children=…`` or \"\" if gone."""
        try:
            with open(f"/proc/{self.pid}/stat", "r") as fh:
                fields = fh.read().rsplit(")", 1)[-1].split()
        except OSError:
            return ""
        if not fields:
            return ""
        state = fields[0]
        try:
            ticks = int(fields[11]) + int(fields[12])
            cpu = ticks / (os.sysconf("SC_CLK_TCK") or 100)
        except (ValueError, IndexError, OSError):
            cpu = 0.0
        rss = threads = 0
        try:
            with open(f"/proc/{self.pid}/status", "r") as fh:
                for line in fh:
                    if line.startswith("VmRSS:"):
                        rss = int(line.split()[1]) // 1024
                    elif line.startswith("Threads:"):
                        threads = int(line.split()[1])
        except (OSError, ValueError, IndexError):
            pass
        return (f"state={state} cpu={cpu:.1f}s rss={rss}MB "
                f"threads={threads} children={self._children()}")

    def _children(self) -> int:
        try:
            with open(f"/proc/{self.pid}/task/{self.pid}/children", "r") as fh:
                return len(fh.read().split())
        except OSError:
            return 0


# ---------------------------------------------------------------------------
# Completion notification (fail-soft, opt-in desktop) — SA-0MVDQF3VW002PKCA
# ---------------------------------------------------------------------------


class CompletionNotifier:
    """End-of-wait user feedback: a terminal bell + optional desktop notification.

    The terminal bell (``\\a``) is always emitted in text mode so an operator
    watching the pane sees/hears the wait end. A desktop notification is fired
    only when ``WAIT_NOTIFY=1`` **and** a ``notify-send`` binary is on ``PATH``;
    it is fire-and-forget with a short socket timeout and never crashes the
    helper (fail-soft). Default is fail-closed: no desktop notification without
    an explicit opt-in, so headless automation is never spammed.
    """

    def __init__(self, env=None, json_output: bool = False):
        env = os.environ if env is None else env
        self.json_output = json_output
        self.desktop_enabled = env.get(NOTIFY_ENV, "") == "1"

    def bell(self) -> None:
        """Emit the terminal bell in text mode (no-op for --json output)."""
        if self.json_output:
            return
        print("\a", end="", flush=True)

    def desktop(self, title: str, message: str) -> bool:
        """Fire a notify-send notification; returns whether it was dispatched."""
        if not self.desktop_enabled:
            return False
        binary = shutil.which("notify-send")
        if not binary:
            return False
        try:
            subprocess.Popen(
                [binary, title, message],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            return True
        except OSError:
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
    notifier: CompletionNotifier | None = None,
    output: JobOutputTail | None = None,
    vitals: ProcessVitals | None = None,
) -> int:
    """Wait until any *markers* completes, the *timeout* elapses, or SIGINT.

    Emits a timestamped heartbeat at least once per poll *interval* while
    waiting. Returns the process exit code (0 completed / 1 timeout / 130
    interrupted). On completion/timeout/interrupt a terminal bell and (when
    opted in) a desktop notification announce the end of the wait.
    """
    reporter = reporter or HerdrReporter()
    notifier = notifier or CompletionNotifier(json_output=json_output)
    start = time.monotonic()
    description = message or "job completion"

    def emit(event: str, text: str, elapsed: float | None = None,
             extra: dict | None = None) -> None:
        if json_output:
            row = {
                "ts": utc_now(),
                "event": event,
                "message": text,
            }
            if elapsed is not None:
                row["elapsed"] = round(elapsed, 1)
            if extra:
                row.update(extra)
            print(json.dumps(row), flush=True)
        else:
            print(_stamp(f"{event}: {text}"), flush=True)

    def finish(event: str, text: str, elapsed: float, title: str) -> None:
        emit(event, text, elapsed)
        notifier.bell()
        notifier.desktop(title, text)

    def interrupted(_sig, _frame):
        raise KeyboardInterrupt

    old_int = signal.signal(signal.SIGINT, interrupted)
    old_term = signal.signal(signal.SIGTERM, interrupted)
    try:
        if reporter.enabled:
            emit("herdr_status",
                 f"herdr reporting enabled (pane {reporter.pane_id})",
                 extra={"enabled": True, "pane_id": reporter.pane_id})
        else:
            emit("herdr_status",
                 "herdr reporting disabled (set HERDR_SOCKET_PATH and "
                 "HERDR_PANE_ID to show sidebar progress)",
                 extra={"enabled": False})
        emit("started", f"waiting for {description}")
        seen_output = False
        warned_buffering = False
        if output is not None:
            if output.source is not None:
                emit("output", f"tailing {output.source}")
            else:
                emit("output",
                     "no readable output (pass --log FILE to see job output)")
        reporter.report("working", f"waiting for {description}")

        while True:
            elapsed = time.monotonic() - start
            if any(m.done() for m in markers):
                text = f"{description} completed after {elapsed:.1f}s"
                finish("completed", text, elapsed, "wait_for_job: completed")
                return EXIT_COMPLETED
            if elapsed >= timeout:
                text = f"timed out after {timeout:g}s waiting for {description}"
                reporter.report("blocked", text)
                finish("timeout", text, elapsed, "wait_for_job: timed out")
                return EXIT_TIMEOUT
            # AC9: always emit a liveness/vitals line, even with no readable log.
            vitals_text = vitals.summary() if vitals is not None else ""
            progress = f"elapsed {elapsed:.0f}s / timeout {timeout:g}s"
            if vitals_text:
                progress = f"{progress} {vitals_text}"
            emit("progress", progress, elapsed)
            # AC8: only lines written since the previous poll, capped.
            if output is not None and output.source is not None:
                for line in output.new_lines():
                    emit("output", line)
                    seen_output = True
                # A tail that stays silent is usually block-buffered stdout;
                # say so once, with the fix, instead of showing nothing.
                if not seen_output and not warned_buffering and elapsed >= interval:
                    emit("output",
                         "no output yet — the job may be block-buffering "
                         "(run it with `python3 -u`, or `stdbuf -oL <cmd>`)")
                    warned_buffering = True
            emit("heartbeat", f"waiting for {description} "
                              f"(elapsed {elapsed:.0f}s / timeout {timeout:g}s)", elapsed)
            reporter.report("working", f"waiting for {description} "
                                       f"(elapsed {elapsed:.0f}s / timeout {timeout:g}s)")
            time.sleep(min(interval, timeout - elapsed))
    except KeyboardInterrupt:
        emit("interrupted", f"wait interrupted after {time.monotonic() - start:.1f}s")
        notifier.bell()
        notifier.desktop("wait_for_job: interrupted",
                         f"wait interrupted after {time.monotonic() - start:.1f}s")
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
               "Output: the job's output is tailed by default (explicit --log, or the "
               "--pid process' redirected stdout); see only new lines, capped by "
               "--tail-lines (--no-tail disables).\n"
               "Herdr: set HERDR_SOCKET_PATH + HERDR_PANE_ID to report "
               "working/blocked to the sidebar (silently optional).\n"
               "Notification: the final line is followed by a terminal bell; "
               f"set {NOTIFY_ENV}=1 (with notify-send on PATH) for a desktop notification.",
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
    parser.add_argument("--tail-lines", type=int, default=DEFAULT_TAIL_LINES,
                        help=f"show at most this many new job-output lines per poll "
                             f"(default {DEFAULT_TAIL_LINES})")
    parser.add_argument("--no-tail", action="store_true",
                        help="do not show the job's output (tailing is on by default)")
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
    if args.tail_lines <= 0:
        print("error: --tail-lines must be positive", file=sys.stderr)
        return EXIT_USAGE
    try:
        markers = build_markers(args)  # compiles --pattern regexes (raises re.error)
    except re.error as exc:
        print(f"error: invalid regex: {exc}", file=sys.stderr)
        return EXIT_USAGE

    output = None if args.no_tail else JobOutputTail.for_job(
        args.log, args.pid, tail_lines=args.tail_lines)
    vitals = ProcessVitals(args.pid[0]) if args.pid else None

    return wait_for_job(
        markers,
        timeout=args.timeout,
        interval=args.interval,
        message=args.message,
        json_output=args.json,
        output=output,
        vitals=vitals,
    )


if __name__ == "__main__":
    sys.exit(main())
