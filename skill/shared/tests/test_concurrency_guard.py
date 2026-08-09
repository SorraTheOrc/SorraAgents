"""Concurrency guard test suite (SA-0MSAK2L5P0066GW8).

Contract-first tests that drive the design of the shared cross-process
semaphore (SA-0MSAK2P3J0065POO) and verify the guard behaviour required by
the fan-out investigation (SA-0MSAEKOQE009TEB4):

- AC1: N concurrent audit-style invocations are bounded to the configured
  maximum (active count never exceeds the ceiling).
- AC2: Waiting invocations proceed once a slot frees (no deadlock, no
  starvation for a small bounded wait).
- AC3: Concurrent `wl sync`-style invocations serialize (max 1 active).
- AC4: Configurable ceiling via env var (AUDIT_MAX_CONCURRENCY) takes effect.
- AC5: Runnable via the standard test runner; passes once implementation
  features land.

These tests exercise the semaphore through real subprocesses (cross-process
advisory locking) using lightweight worker shims that mimic an audit/pi or
wl-sync style workload. The suite skips cleanly (``importorskip``) until the
semaphore module lands in SA-0MSAK2P3J0065POO, so this file can be committed
green in advance of the implementation.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
SEMAPHORE_MODULE = "skill.shared.process_semaphore"

pytest.importorskip(SEMAPHORE_MODULE)

from skill.shared.process_semaphore import Semaphore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

WORKER_TEMPLATE = r"""
import json
import os
import sys
import time

sys.path.insert(0, os.environ["REPO_ROOT"])

from skill.shared.process_semaphore import Semaphore  # noqa: E402

name = os.environ["SEM_NAME"]
max_workers = int(os.environ.get("SEM_MAX", "2"))
timeout = float(os.environ.get("SEM_TIMEOUT", "30"))
hold = float(os.environ.get("SEM_HOLD", "0.4"))
env_ceiling = os.environ.get("AUDIT_MAX_CONCURRENCY")

result = {"acquired": None, "released": None, "error": None}
try:
    if env_ceiling:
        sem = Semaphore(name)  # ceiling from env var
    else:
        sem = Semaphore(name, max_workers=max_workers, timeout=timeout)
    t0 = None
    with sem:
        result["acquired"] = time.time()  # recorded AFTER acquire succeeds
        print(json.dumps(result), file=sys.stderr, flush=True)  # acquisition signal
        time.sleep(hold)
        result["released"] = time.time()
except Exception as exc:  # noqa: BLE001
    result["error"] = f"{type(exc).__name__}: {exc}"

print(json.dumps(result))
sys.exit(0 if result["acquired"] is not None else 1)
"""


def _run_workers(n, sem_name, max_workers=2, hold=0.4, timeout=30, env_ceiling=None):
    """Launch *n* concurrent worker subprocesses sharing one semaphore.

    Returns a list of parsed result dicts (acquired/released timestamps).
    """
    procs = []
    for _ in range(n):
        env = dict(os.environ)
        env.update(
            {
                "REPO_ROOT": str(REPO_ROOT),
                "SEM_NAME": sem_name,
                "SEM_MAX": str(max_workers),
                "SEM_HOLD": str(hold),
                "SEM_TIMEOUT": str(timeout),
            }
        )
        if env_ceiling is not None:
            env["AUDIT_MAX_CONCURRENCY"] = str(env_ceiling)
        procs.append(
            subprocess.Popen(
                [sys.executable, "-c", WORKER_TEMPLATE],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
        )
    results = []
    for p in procs:
        out, err = p.communicate(timeout=timeout + 30)
        try:
            results.append(json.loads(out.strip()))
        except json.JSONDecodeError:
            results.append({"acquired": None, "released": None, "error": f"bad output: {out} {err}"})
    return results


def _max_concurrent(results):
    """Compute the maximum number of overlapping acquisitions."""
    intervals = [
        (r["acquired"], r["released"])
        for r in results
        if r.get("acquired") is not None and r.get("released") is not None
    ]
    intervals.sort()
    active = 0
    max_active = 0
    events = []
    for start, end in intervals:
        events.append((start, 1))
        events.append((end, -1))
    events.sort(key=lambda e: (e[0], -e[1]))
    for _ts, delta in events:
        active += delta
        max_active = max(max_active, active)
    return max_active


# ---------------------------------------------------------------------------
# AC1: active count never exceeds the configured maximum
# ---------------------------------------------------------------------------


@pytest.mark.smoke
def test_active_never_exceeds_max():
    """With max_workers=2, 8 concurrent workers never have >2 active."""
    name = f"guard-max-{int(time.time())}"
    results = _run_workers(8, name, max_workers=2, hold=0.3, timeout=30)
    acquired = [r for r in results if r.get("acquired") is not None]
    assert len(acquired) == 8, f"not all workers acquired: {results}"
    assert _max_concurrent(results) <= 2, f"max concurrency exceeded: {results}"


# ---------------------------------------------------------------------------
# AC2: waiting invocations proceed once a slot frees (no deadlock)
# ---------------------------------------------------------------------------


@pytest.mark.smoke
def test_waiters_proceed_after_slot_frees():
    """Workers beyond the ceiling must all complete within a bounded wait."""
    name = f"guard-wait-{int(time.time())}"
    results = _run_workers(4, name, max_workers=1, hold=0.3, timeout=30)
    acquired = [r for r in results if r.get("acquired") is not None]
    assert len(acquired) == 4, f"starvation/deadlock: {results}"
    assert _max_concurrent(results) <= 1


# ---------------------------------------------------------------------------
# AC3: serialization (max 1 active) — wl-sync style workload
# ---------------------------------------------------------------------------


@pytest.mark.smoke
def test_serialize_max_one():
    """With max_workers=1, no two workers overlap — like concurrent wl sync."""
    name = f"guard-serialize-{int(time.time())}"
    results = _run_workers(3, name, max_workers=1, hold=0.25, timeout=30)
    assert _max_concurrent(results) == 1, f"not serialized: {results}"
    assert all(r.get("acquired") is not None for r in results)


# ---------------------------------------------------------------------------
# AC4: configurable ceiling via env var (AUDIT_MAX_CONCURRENCY)
# ---------------------------------------------------------------------------


def test_env_ceiling_takes_effect():
    """AUDIT_MAX_CONCURRENCY=1 must bound a 3-worker batch to 1 active."""
    name = f"guard-env-{int(time.time())}"
    results = _run_workers(3, name, hold=0.25, timeout=30, env_ceiling=1)
    assert _max_concurrent(results) <= 1, f"env ceiling ignored: {results}"
    assert all(r.get("acquired") is not None for r in results)


def test_env_ceiling_default_without_var():
    """Without AUDIT_MAX_CONCURRENCY, Semaphore(name) uses the documented default."""
    sem = Semaphore(f"guard-default-{int(time.time())}")
    assert sem.max_workers > 0


# ---------------------------------------------------------------------------
# Timeout / fail-fast behaviour
# ---------------------------------------------------------------------------


def _launch_holder(sem_name, hold=3.0, max_workers=1):
    """Launch a background worker that holds one slot for *hold* seconds.

    Returns (proc, env) so the caller can acquire while the holder runs.
    Blocks until the holder reports acquisition (bounded by 15s).
    """
    env = dict(os.environ)
    env.update(
        {
            "REPO_ROOT": str(REPO_ROOT),
            "SEM_NAME": sem_name,
            "SEM_MAX": str(max_workers),
            "SEM_HOLD": str(hold),
            "SEM_TIMEOUT": "30",
        }
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", WORKER_TEMPLATE],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    # Poll the holder's stderr until it reports acquisition (JSON line).
    deadline = time.time() + 15.0
    while time.time() < deadline:
        line = proc.stderr.readline()
        if not line:
            break
        try:
            parsed = json.loads(line.strip())
            if parsed.get("acquired") is not None:
                return proc, env
        except json.JSONDecodeError:
            continue
    # Holder never acquired (or already finished); fail fast is not possible.
    proc.kill()
    proc.wait(timeout=10)
    raise RuntimeError("holder worker failed to acquire slot")


def test_timeout_raises_when_busy():
    """A bounded acquire must raise once the wait deadline is exceeded."""
    name = f"guard-timeout-{int(time.time())}"
    proc, _env = _launch_holder(name, hold=3.0)
    try:
        # Slot is busy; a 0.2s bounded acquire must time out.
        sem = Semaphore(name, max_workers=1, timeout=0.2)
        with pytest.raises(TimeoutError):
            sem.acquire()
        sem.release()  # idempotent no-op after failed acquire
    finally:
        proc.kill()
        proc.wait(timeout=10)


def test_fail_fast_option():
    """timeout=0 means fail fast (raise) when no slot is free."""
    name = f"guard-fast-{int(time.time())}"
    proc, _env = _launch_holder(name, hold=3.0)
    try:
        sem = Semaphore(name, max_workers=1, timeout=0)
        with pytest.raises(TimeoutError):
            sem.acquire()
        sem.release()
    finally:
        proc.kill()
        proc.wait(timeout=10)


# ---------------------------------------------------------------------------
# Cleanup: lock released on error and on process death (no stale locks)
# ---------------------------------------------------------------------------


def test_release_on_exception():
    """A worker that raises inside the context must still release the slot."""
    name = f"guard-exc-{int(time.time())}"
    script = (
        "import json, os, sys, time\n"
        "sys.path.insert(0, os.environ['REPO_ROOT'])\n"
        "from skill.shared.process_semaphore import Semaphore\n"
        "name = os.environ['SEM_NAME']\n"
        "try:\n"
        "    with Semaphore(name, max_workers=1, timeout=10):\n"
        "        print('acquired', flush=True)\n"
        "        raise RuntimeError('boom')\n"
        "except RuntimeError:\n"
        "    print('released', flush=True)\n"
    )
    env = dict(os.environ)
    env.update({"REPO_ROOT": str(REPO_ROOT), "SEM_NAME": name})
    subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, env=env, check=True
    )
    # Slot must be free now: a fresh acquire succeeds immediately.
    sem = Semaphore(name, max_workers=1, timeout=5)
    with sem:
        pass


def test_lock_released_after_worker_killed():
    """Killing a holder (SIGKILL) must not leave a stale lock (flock auto-release)."""
    name = f"guard-kill-{int(time.time())}"
    env = dict(os.environ)
    env.update(
        {
            "REPO_ROOT": str(REPO_ROOT),
            "SEM_NAME": name,
            "SEM_MAX": "1",
            "SEM_HOLD": "30",  # hold long enough to kill
            "SEM_TIMEOUT": "60",
        }
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", WORKER_TEMPLATE],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    # Give the worker time to acquire the slot.
    time.sleep(0.8)
    proc.kill()
    proc.wait(timeout=10)
    # The slot must now be free: acquire succeeds without a long wait.
    t0 = time.monotonic()
    sem = Semaphore(name, max_workers=1, timeout=5)
    with sem:
        pass
    assert time.monotonic() - t0 < 3.0, "stale lock blocked acquisition"
