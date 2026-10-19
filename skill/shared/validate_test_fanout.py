#!/usr/bin/env python3
"""F7-style load-validation harness for the test-run semaphore (SA-0MVF790HW005M0P3).

Sibling of ``validate_fanout.py`` (which is audit-only and is being migrated
by SA-0MVB713IZ000NO42) for the **test** ceiling
(``TEST_MAX_CONCURRENCY`` in ``skill/test/scripts/run_tests.py``). It gives
the ceiling value a committed, reproducible measurement instead of an
uncalibrated guess — the same precedent the audit knob followed
(SA-0MUSZJRB9006ZCHE).

What it does:

- Builds a throwaway git repo whose ``.pi/test-config.json`` declares a
  ``suiteCommands`` entry pointing at a synthetic payload. The payload burns
  a bounded number of worker processes for ``--suite-seconds`` and exits 0,
  mirroring F7's mock-pi approach: no vitest, no browser, no real project
  suite.
- Launches ``--n`` concurrent ``run_tests.py --no-cache`` invocations through
  the **real** semaphore path with ``TEST_MAX_CONCURRENCY=<ceiling>``.
  ``--no-cache`` matters: cache hits are served without acquiring a slot, so a
  cacheable run would measure nothing (SA-0MTG5U75A001F1RG AC4).
- Isolates the batch in its own ``PI_SEMAPHORE_DIR`` (inside the batch
  directory) so the measurement reflects this batch only and never queues
  behind unrelated host-wide test runs.
- Samples process counts, 1/5/15-minute load and swap every <= 0.5 s via
  ``skill/shared/measure_fanout.py`` (plus a per-batch worker count keyed on
  this batch's unique path, matching ``validate_fanout.FanoutSampler``), and
  reports ``before``/``after`` snapshots, peak load/processes, the baseline
  load sampled immediately before the batch (so deltas are
  batch-attributable) and per-invocation results.

Usage::

    python3 skill/shared/validate_test_fanout.py --ceiling 2 --n 8 \\
        --out /tmp/ceiling-2.json
    python3 skill/shared/validate_test_fanout.py --dry-run

``--dry-run`` prepares the throwaway repo and prints the report skeleton
without launching any suite: fast enough for unit tests, and it leaves the
prepared batch directory in place so the synthetic-payload contract is
inspectable. A dry-run report carries an empty ``results`` list and
``batch.launched == 0``.

Output: a JSON report on stdout (and at ``--out``) with ``success``,
``config``, ``before``, ``after``, ``peak``, ``batch`` and ``results``.
Exit code 0 when every invocation exited 0, else 1 (2 on a CLI error).
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

_SKILLS_ROOT = Path(__file__).resolve().parents[1]  # .../skill/shared -> <repo>/skill
if str(_SKILLS_ROOT) in sys.path:
    sys.path.remove(str(_SKILLS_ROOT))
sys.path.insert(0, str(_SKILLS_ROOT))

REPO_ROOT = _SKILLS_ROOT.parent

from shared.measure_fanout import collect

RUN_TESTS = REPO_ROOT / "skill" / "test" / "scripts" / "run_tests.py"

#: Sampling cadence for process counts / load / swap (AC: every <= 0.5 s).
SAMPLE_INTERVAL_SECONDS = 0.25

#: Defaults matching the documented invocation in SA-0MVE995PV0077QB5.
DEFAULT_CEILING = 2
DEFAULT_N = 8
DEFAULT_SUITE_SECONDS = 3.0
DEFAULT_WORKERS = 4

#: Recursion marker the child ``run_tests.py`` must own itself, so the
#: throwaway repo's live-repo guard is armed even when this harness is itself
#: invoked from inside a guarded suite (e.g. the pytest conftest).
LIVE_REPO_GUARD_ACTIVE_ENV = "LIVE_REPO_GUARD_ACTIVE"

_ZERO_LOAD = {"1min": 0.0, "5min": 0.0, "15min": 0.0}

#: Synthetic suite payload written into the throwaway repo. It models a test
#: suite whose workers fan out: each worker burns ~10% CPU for the requested
#: duration (a measurable but deliberately non-saturating contribution) and
#: the script only exits once every worker has finished, so the suite's wall
#: time is bounded by ``--suite-seconds``.
PAYLOAD_SOURCE = '''#!/usr/bin/env python3
"""Synthetic test-suite payload for skill/shared/validate_test_fanout.py.

Stands in for a real project suite: it burns a bounded number of worker
processes for a fixed duration so the harness can measure suite-level
concurrency through the real ``run_tests.py`` slot path without running
vitest, pytest or a browser.

Workers burn ~10% CPU (short bursts separated by sleeps) so each suite adds
measurable load without saturating the host — the gate is an attributable
load delta, never absolute load.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time


def _burn(seconds: float) -> None:
    """Burn ~10% CPU for *seconds* (measurable, non-saturating load)."""
    deadline = time.monotonic() + max(seconds, 0.0)
    while time.monotonic() < deadline:
        burst_end = min(deadline, time.monotonic() + 0.01)
        while time.monotonic() < burst_end:
            pass
        time.sleep(0.09)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=3.0)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--worker", action="store_true",
                        help="Run one worker instead of the suite driver.")
    args = parser.parse_args(argv)

    if args.worker:
        _burn(args.seconds)
        return 0

    procs = [
        subprocess.Popen(
            [sys.executable, __file__, "--worker", "--seconds", str(args.seconds)]
        )
        for _ in range(max(1, args.workers))
    ]
    for proc in procs:
        proc.wait()
    return 0 if all(proc.returncode == 0 for proc in procs) else 1


if __name__ == "__main__":
    sys.exit(main())
'''


def _positive_int(flag: str):
    """Argparse type: integer >= 1 whose error message names *flag*."""

    def parse(value: str) -> int:
        try:
            parsed = int(value)
        except ValueError:
            raise argparse.ArgumentTypeError(
                f"{flag} must be an integer, got {value!r}"
            ) from None
        if parsed < 1:
            raise argparse.ArgumentTypeError(
                f"{flag} must be at least 1, got {parsed}"
            )
        return parsed

    return parse


def _positive_float(flag: str):
    """Argparse type: float > 0 whose error message names *flag*."""

    def parse(value: str) -> float:
        try:
            parsed = float(value)
        except ValueError:
            raise argparse.ArgumentTypeError(
                f"{flag} must be a number, got {value!r}"
            ) from None
        if parsed <= 0:
            raise argparse.ArgumentTypeError(
                f"{flag} must be greater than 0, got {parsed}"
            )
        return parsed

    return parse


def _git(cwd: Path, *args: str) -> None:
    """Run a quiet git command in *cwd* (raises on failure)."""
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def prepare_batch(suite_seconds: float, workers: int) -> Path:
    """Create the throwaway batch repo and return its directory.

    The repo contains the synthetic payload and a ``.pi/test-config.json``
    whose ``suiteCommands`` entry runs that payload. It is a git repository
    because ``run_tests.py`` resolves its project root via
    ``git rev-parse --show-toplevel``; the live-repo guard is a no-op on a
    non-git root, and the guard should actually cover the synthetic run.
    """
    batch_dir = Path(tempfile.mkdtemp(prefix="f7-test-fanout-"))
    _git(batch_dir, "init", "-q")

    payload_dir = batch_dir / "payload"
    payload_dir.mkdir(parents=True, exist_ok=True)
    payload = payload_dir / "synthetic_suite.py"
    payload.write_text(PAYLOAD_SOURCE)
    payload.chmod(0o755)

    pi_dir = batch_dir / ".pi"
    pi_dir.mkdir(parents=True, exist_ok=True)
    suite_command = " ".join(
        shlex.quote(token)
        for token in [
            sys.executable,
            str(payload),
            "--seconds",
            str(suite_seconds),
            "--workers",
            str(workers),
        ]
    )
    (pi_dir / "test-config.json").write_text(
        json.dumps({"suiteCommands": [suite_command]}, indent=2) + "\n"
    )
    # The batch's isolated semaphore pool lives inside the batch directory
    # (so the measurement is self-contained), but its lock files must not
    # count as a checkout mutation to the child run_tests.py live-repo guard.
    (batch_dir / ".gitignore").write_text("semaphores/\n")
    return batch_dir


def _count_batch_processes(marker: str) -> int:
    """Count processes whose command line references *marker*.

    *marker* is this batch's unique directory path, so synthetic-suite
    processes from other concurrent harness runs on a shared host are never
    counted in this batch's peak (mirroring ``validate_fanout``'s
    per-batch mock-pi marker).
    """
    try:
        import psutil
    except ImportError:  # pragma: no cover - psutil is a project dependency
        return 0

    count = 0
    for proc in psutil.process_iter(["cmdline"]):
        try:
            cmdline = proc.info.get("cmdline") or []
        except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
            continue
        if any(marker in part for part in cmdline):
            count += 1
    return count


class TestFanoutSampler(threading.Thread):
    """Sample process counts, load and swap every ``<= 0.5 s`` during a batch."""

    def __init__(
        self,
        stop_event: threading.Event,
        marker: str,
        interval: float = SAMPLE_INTERVAL_SECONDS,
    ) -> None:
        super().__init__(daemon=True)
        self._stop_event = stop_event
        self._marker = marker
        self._interval = interval
        self.samples: list[dict[str, Any]] = []
        self.batch_process_peak = 0

    def run(self) -> None:
        while not self._stop_event.is_set():
            # Count the batch's own processes first (cheap cmdline scan) so
            # short-lived workers are not missed by the heavier snapshot.
            try:
                self.batch_process_peak = max(
                    self.batch_process_peak, _count_batch_processes(self._marker)
                )
            except Exception:  # noqa: BLE001, S110 -- sampler is best-effort
                pass
            try:
                self.samples.append(collect())
            except Exception:  # noqa: BLE001, S110 -- sampler is best-effort
                pass
            self._stop_event.wait(self._interval)

    def peak(self) -> dict[str, Any]:
        """Return peak process counts, load and swap across the samples."""
        if not self.samples:
            return _empty_peak()
        peak_proc: dict[str, int] = {}
        peak_load = dict(_ZERO_LOAD)
        peak_swap_used = 0
        peak_swap_total = 0
        for sample in self.samples:
            for key, value in sample.get("processes", {}).items():
                peak_proc[key] = max(peak_proc.get(key, 0), int(value))
            for key, peak_value in peak_load.items():
                peak_load[key] = max(
                    peak_value, float(sample.get("load", {}).get(key, 0.0))
                )
            swap = sample.get("swap", {})
            peak_swap_used = max(peak_swap_used, int(swap.get("used", 0)))
            peak_swap_total = max(peak_swap_total, int(swap.get("total", 0)))
        peak_proc["batch_processes"] = self.batch_process_peak
        return {
            "processes": peak_proc,
            "load": peak_load,
            "swap": {"used": peak_swap_used, "total": peak_swap_total},
        }


def _empty_peak() -> dict[str, Any]:
    """Peak skeleton for a run that launched nothing (``--dry-run``)."""
    return {"processes": {}, "load": dict(_ZERO_LOAD), "swap": {"used": 0, "total": 0}}


def run_batch(
    batch_dir: Path,
    ceiling: int,
    n: int,
    suite_seconds: float,
) -> dict[str, Any]:
    """Launch *n* concurrent ``run_tests.py`` executions and return the report parts.

    Every invocation runs with ``--no-cache`` so it must acquire a real slot in
    the batch's isolated ``PI_SEMAPHORE_DIR`` pool with
    ``TEST_MAX_CONCURRENCY=<ceiling>``.
    """
    semaphore_dir = batch_dir / "semaphores"
    semaphore_dir.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    env["TEST_MAX_CONCURRENCY"] = str(ceiling)
    env["PI_SEMAPHORE_DIR"] = str(semaphore_dir)
    # Let the child run_tests own its live-repo guard: the throwaway repo is
    # what must not mutate, not the (possibly already guarded) parent checkout.
    env.pop(LIVE_REPO_GUARD_ACTIVE_ENV, None)

    stop = threading.Event()
    sampler = TestFanoutSampler(stop, marker=str(batch_dir))

    before = collect()
    baseline_load = float(before["load"].get("1min", 0.0))
    sampler.start()

    # Generous bound: a full batch drains in ceil(n / ceiling) waves of
    # suite_seconds, plus per-invocation startup and slot queueing.
    timeout_seconds = max(
        120.0, (n / ceiling) * (suite_seconds + 10.0) + 60.0
    )

    started = time.monotonic()
    launch_times: list[float] = []
    procs: list[subprocess.Popen[str]] = []
    for _ in range(n):
        procs.append(
            subprocess.Popen(
                [sys.executable, str(RUN_TESTS), "--no-cache", "--json"],
                cwd=str(batch_dir),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        )
        launch_times.append(time.monotonic())

    results: list[dict[str, Any]] = []
    for index, proc in enumerate(procs):
        try:
            _stdout, stderr = proc.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            proc.kill()
            _stdout, stderr = proc.communicate()
            results.append(
                {
                    "index": index,
                    "exit_code": -9,
                    "duration_seconds": round(time.monotonic() - launch_times[index], 2),
                    "error": f"timed out after {timeout_seconds:.0f}s",
                    "stderr_tail": stderr.strip()[-300:],
                }
            )
            continue
        results.append(
            {
                "index": index,
                "exit_code": proc.returncode,
                "duration_seconds": round(time.monotonic() - launch_times[index], 2),
                "stderr_tail": stderr.strip()[-300:],
            }
        )
    elapsed = time.monotonic() - started

    stop.set()
    sampler.join(timeout=10)
    after = collect()

    batch = {
        "n": n,
        "launched": len(results),
        "ok": bool(results) and all(r["exit_code"] == 0 for r in results),
        "elapsed_seconds": round(elapsed, 2),
        "baseline_load": baseline_load,
    }
    return {
        "before": before,
        "after": after,
        "peak": sampler.peak(),
        "batch": batch,
        "results": results,
    }


def build_report(
    config: dict[str, Any],
    before: dict[str, Any],
    after: dict[str, Any],
    peak: dict[str, Any],
    batch: dict[str, Any],
    results: list[dict[str, Any]],
) -> dict[str, Any]:
    """Assemble the pinned report shape."""
    return {
        "success": batch["ok"],
        "config": config,
        "before": before,
        "after": after,
        "peak": peak,
        "batch": batch,
        "results": results,
    }


def _emit(report: dict[str, Any], out: str | None) -> None:
    """Persist the report to *out* (when given) and print it to stdout."""
    rendered = json.dumps(report, indent=2)
    if out:
        Path(out).write_text(rendered + "\n")
    print(rendered)


def build_parser() -> argparse.ArgumentParser:
    """Build the harness argument parser (the pinned CLI contract)."""
    parser = argparse.ArgumentParser(
        description=(
            "F7-style fan-out harness for the test-run semaphore: launch N "
            "concurrent run_tests.py --no-cache executions against a synthetic "
            "suite at a given TEST_MAX_CONCURRENCY ceiling and report sampled "
            "process counts, load and swap."
        )
    )
    parser.add_argument(
        "--ceiling",
        type=_positive_int("--ceiling"),
        default=DEFAULT_CEILING,
        help=(
            "TEST_MAX_CONCURRENCY ceiling for the batch (default "
            f"{DEFAULT_CEILING}); must be at least 1"
        ),
    )
    parser.add_argument(
        "--n",
        type=_positive_int("--n"),
        default=DEFAULT_N,
        help=f"number of concurrent suite executions (default {DEFAULT_N}); must be at least 1",
    )
    parser.add_argument(
        "--out",
        default=None,
        metavar="FILE",
        help="also write the JSON report to FILE",
    )
    parser.add_argument(
        "--suite-seconds",
        type=_positive_float("--suite-seconds"),
        default=DEFAULT_SUITE_SECONDS,
        help=(
            "duration the synthetic suite's workers burn (default "
            f"{DEFAULT_SUITE_SECONDS}); must be greater than 0"
        ),
    )
    parser.add_argument(
        "--workers",
        type=_positive_int("--workers"),
        default=DEFAULT_WORKERS,
        help=(
            "worker processes the synthetic suite fans out per execution "
            f"(default {DEFAULT_WORKERS}); must be at least 1"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="prepare the throwaway repo and print the report skeleton without launching suites",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the harness; return a process exit code."""
    args = build_parser().parse_args(argv)

    batch_dir = prepare_batch(args.suite_seconds, args.workers)
    semaphore_dir = batch_dir / "semaphores"
    semaphore_dir.mkdir(parents=True, exist_ok=True)

    config = {
        "ceiling": args.ceiling,
        "n": args.n,
        "suite_seconds": args.suite_seconds,
        "workers": args.workers,
        "semaphore_dir": str(semaphore_dir),
        "batch_dir": str(batch_dir),
        "cpu_count": os.cpu_count() or 0,
        "host": os.uname().nodename if hasattr(os, "uname") else "unknown",
        "dry_run": bool(args.dry_run),
    }

    if args.dry_run:
        before = collect()
        report = build_report(
            config,
            before=before,
            after=collect(),
            peak=_empty_peak(),
            batch={
                "n": args.n,
                "launched": 0,
                "ok": True,
                "elapsed_seconds": 0.0,
                "baseline_load": float(before["load"].get("1min", 0.0)),
            },
            results=[],
        )
        _emit(report, args.out)
        return 0

    parts = run_batch(batch_dir, args.ceiling, args.n, args.suite_seconds)
    report = build_report(config, **parts)
    _emit(report, args.out)
    return 0 if report["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
