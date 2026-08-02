#!/usr/bin/env python3
"""F7 load-validation batch harness (SA-0MSAK33NN008NS95).

Runs a typical batch of concurrent `audit_runner.py issue` invocations with
the concurrency controls enabled (shared semaphore ceiling via
AUDIT_MAX_CONCURRENCY) and samples process counts / load / swap during the
run to validate the fan-out bounds (AC #3/#4 of SA-0MSAEKOQE009TEB4).

Design:
- Uses a throwaway worklog project (temp dir with git init + wl init) and a
  mock pi binary so the batch completes quickly and does not launch real pi
  agent sessions on the operator's machine.
- Launches N concurrent audit_runner invocations; each audit calls pi via
  the mock binary. The shared semaphore (skill/shared/process_semaphore.py)
  bounds concurrent pi subprocesses to AUDIT_MAX_CONCURRENCY.
- A sampler thread records per-type process counts, load average, and swap
  usage every 0.5s; peak values are reported alongside the batch result.

Output: JSON document with before/after metrics and batch summary.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]  # .../skill/shared -> repo root
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from skill.shared.measure_fanout import collect

AUDIT_RUNNER = REPO_ROOT / "skill" / "audit" / "scripts" / "audit_runner.py"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def setup_worklog() -> tuple[Path, list[str]]:
    """Create a throwaway initialized worklog with *n* items; return (dir, ids)."""
    tmp = Path(tempfile.mkdtemp(prefix="f7-validate-"))
    _git(tmp, "init", "-q")
    _git(tmp, "config", "user.email", "f7@test.local")
    _git(tmp, "config", "user.name", "F7 Validation")
    subprocess.run(["mkdir", "-p", ".worklog"], cwd=tmp, check=True)
    # Non-interactive init: no workflow, auto-export on, auto-sync off.
    subprocess.run(
        [
            "wl", "init", "--project-name", "F7 Validation", "--prefix", "F7",
            "--auto-export", "yes", "--auto-sync", "no",
        ],
        cwd=tmp,
        input="n\nn\nn\n",
        text=True,
        capture_output=True,
        check=False,
    )
    ids: list[str] = []
    for i in range(8):
        subprocess.run(
            ["wl", "create", "-t", f"F7 item {i}",
             "-d", (
                 "Feature X\n\n## Acceptance Criteria\n"
                 "1. Criterion one works.\n2. Criterion two works.\n"
             )],
            cwd=tmp,
            capture_output=True,
            check=False,
        )
    out = subprocess.run(["wl", "list", "--json"], cwd=tmp, capture_output=True, text=True, check=False)
    try:
        data = json.loads(out.stdout)
        items = data.get("workItems", [])
        ids = [it["id"] for it in items]
    except (json.JSONDecodeError, AttributeError):
        ids = []
    return tmp, ids


class FanoutSampler(threading.Thread):
    """Samples fan-out metrics during the batch run."""

    def __init__(self, stop_event: threading.Event, interval: float = 0.5,
                 batch_marker: str = "mockpi"):
        super().__init__(daemon=True)
        self._stop = stop_event
        self._interval = interval
        self._batch_marker = batch_marker
        self.samples: list[dict] = []
        self.batch_pi_peak = 0

    def run(self) -> None:
        while not self._stop.is_set():
            # Count batch pi first (cheap cmdline scan) so short-lived mock
            # pi processes are not missed while the heavier full snapshot runs.
            try:
                self.batch_pi_peak = max(self.batch_pi_peak, _count_batch_pi(self._batch_marker))
            except Exception:  # noqa: BLE001, S110 -- sampler is best-effort; ignore on failure
                pass
            try:
                self.samples.append(collect())
            except Exception:  # noqa: BLE001, S110 -- sampler is best-effort; ignore on failure
                pass
            self._stop.wait(self._interval)

    def peak(self) -> dict:
        if not self.samples:
            return {"processes": {}, "load": {"1min": 0.0, "5min": 0.0, "15min": 0.0}}
        peak_proc: dict[str, int] = {}
        peak_load = {"1min": 0.0, "5min": 0.0, "15min": 0.0}
        for s in self.samples:
            for k, v in s.get("processes", {}).items():
                peak_proc[k] = max(peak_proc.get(k, 0), int(v))
            for k, value in peak_load.items():
                peak_load[k] = max(value, float(s.get("load", {}).get(k, 0)))
        peak_proc["batch_pi"] = self.batch_pi_peak
        return {"processes": peak_proc, "load": peak_load}


def _count_batch_pi(marker: str) -> int:
    """Count pi subprocesses whose cmdline references *marker* (mock path)."""
    import psutil

    count = 0
    for proc in psutil.process_iter(["cmdline"]):
        try:
            cmdline = proc.info.get("cmdline") or []
        except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
            continue
        joined = " ".join(cmdline).lower()
        if marker.lower() in joined and (
            " -p" in joined or "--mode" in joined or "-p " in joined
        ):
            count += 1
    return count


def run_batch(worklog_dir: Path, ids: list[str], pi_bin: Path, max_concurrency: int) -> dict:
    """Launch concurrent audits with the ceiling and return the batch result."""
    env = dict(os.environ)
    env["AUDIT_MAX_CONCURRENCY"] = str(max_concurrency)
    env["AUDIT_LOCK_TIMEOUT"] = "120"
    # Isolate the concurrency semaphore to this batch: without this, the
    # batch's audits share the host-wide "audit" semaphore (PI_SEMAPHORE_DIR)
    # with every other live audit/pi session on the machine and can queue
    # behind them for many seconds each, making the wall-clock budget a
    # measure of unrelated host load rather than this batch's own fan-out.
    env["PI_SEMAPHORE_DIR"] = str(worklog_dir / "semaphores")

    stop = threading.Event()
    sampler = FanoutSampler(stop, interval=0.25)
    sampler.start()

    before = collect()

    procs = []
    started = time.monotonic()
    for wid in ids:
        procs.append(
            subprocess.Popen(
                [
                    sys.executable, str(AUDIT_RUNNER), "issue", wid,
                    "--pi-bin", str(pi_bin), "--do-not-persist",
                ],
                cwd=worklog_dir,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        )
    results = []
    for p in procs:
        _out, err = p.communicate(timeout=300)
        results.append({"exit_code": p.returncode, "stderr_tail": err.strip()[-200:]})
    elapsed = time.monotonic() - started

    stop.set()
    sampler.join(timeout=10)
    after = collect()

    peak = sampler.peak()
    ok = all(r["exit_code"] == 0 for r in results)
    return {
        "batch": {"n": len(ids), "ok": ok, "elapsed_seconds": round(elapsed, 2)},
        "before": before,
        "after": after,
        "peak": peak,
        "results": results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-concurrency", type=int, default=2,
                        help="AUDIT_MAX_CONCURRENCY ceiling for the batch (default 2)")
    parser.add_argument("--out", default=None, help="Write JSON report to this file")
    args = parser.parse_args(argv)

    worklog_dir, ids = setup_worklog()
    if not ids:
        print(json.dumps({"success": False, "error": "failed to create worklog items"}))
        return 1

    pi_bin = worklog_dir / "mockpi" / "pi"
    pi_bin.parent.mkdir(parents=True, exist_ok=True)
    pi_bin.write_text(MOCK_PI)
    pi_bin.chmod(0o755)

    report = run_batch(worklog_dir, ids, pi_bin, args.max_concurrency)
    report["success"] = True
    report["config"] = {"max_concurrency": args.max_concurrency, "n_items": len(ids)}

    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2))

    print(json.dumps(report, indent=2))
    return 0 if report["batch"]["ok"] else 1


MOCK_PI = """#!/usr/bin/env python3
import json, sys, time
time.sleep(1.2)
inner = json.dumps({"verdict": "met", "evidence": "mock evidence ok"})
msg = json.dumps({
    "type": "message_update",
    "assistantMessageEvent": {"type": "text_end", "content": inner},
})
print(msg)
sys.exit(0)
"""


if __name__ == "__main__":
    sys.exit(main())
