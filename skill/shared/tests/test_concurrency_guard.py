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

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
_SKILLS_ROOT_FOR_TESTS = REPO_ROOT / "skill"
if str(_SKILLS_ROOT_FOR_TESTS) not in sys.path:
    sys.path.append(str(_SKILLS_ROOT_FOR_TESTS))
import errno
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
SEMAPHORE_MODULE = "shared.process_semaphore"

pytest.importorskip(SEMAPHORE_MODULE)

from shared.process_semaphore import Semaphore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

WORKER_TEMPLATE = r"""
import json
import os
import sys
import time

sys.path.insert(0, os.environ["REPO_ROOT"])
sys.path.insert(0, os.environ["SKILLS_ROOT"])

from shared.process_semaphore import Semaphore  # noqa: E402

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
                "SKILLS_ROOT": str(REPO_ROOT / "skill"),
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
            "SKILLS_ROOT": str(REPO_ROOT / "skill"),
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


# ---------------------------------------------------------------------------
# Arrival-ordered turnstile (SA-0MVSJX5HS002DJHI)
#
# Fairness is *ordering*, not capacity: with one slot, waiters must be served
# in arrival (ticket) order, so a waiter at queue position N is bounded by
# N x the hold time instead of being overtaken indefinitely by later
# arrivals. Each worker records the ticket it was granted and the instant it
# acquired, so the ordering is asserted from recorded sequence — never from
# sleep durations.
# ---------------------------------------------------------------------------

#: A worker that starts only when its start-file exists, then records the
#: ticket it was granted and the acquisition timestamp. Printing "arrived"
#: (just before calling acquire) lets the harness launch the next worker only
#: once its predecessor is ready, so launch order is controlled.
ORDERED_WORKER_TEMPLATE = r"""
import json
import os
import sys
import time

sys.path.insert(0, os.environ["REPO_ROOT"])
sys.path.insert(0, os.environ["SKILLS_ROOT"])

from shared.process_semaphore import Semaphore  # noqa: E402

name = os.environ["SEM_NAME"]
max_workers = int(os.environ.get("SEM_MAX", "1"))
timeout = float(os.environ.get("SEM_TIMEOUT", "30"))
hold = float(os.environ.get("SEM_HOLD", "0.15"))
start_file = os.environ["SEM_START_FILE"]

result = {"ticket": None, "acquired": None, "released": None, "error": None}
sem = Semaphore(name, max_workers=max_workers, timeout=timeout)
# Wait until the harness releases this worker to arrive.
deadline = time.time() + 30
while not os.path.exists(start_file):
    if time.time() > deadline:
        print(json.dumps({**result, "error": "start timeout"}))
        sys.exit(1)
    time.sleep(0.005)
print("arrived", file=sys.stderr, flush=True)
try:
    with sem:
        result["acquired"] = time.time()
        result["ticket"] = sem.last_ticket
        time.sleep(hold)
        result["released"] = time.time()
except Exception as exc:  # noqa: BLE001
    result["error"] = f"{type(exc).__name__}: {exc}"

print(json.dumps(result))
sys.exit(0 if result["acquired"] is not None else 1)
"""


def _run_ordered_workers(n, sem_name, tmp_path, max_workers=1, hold=0.15, timeout=30):
    """Launch *n* workers one at a time, in a controlled launch order.

    Worker *i* is released only once worker *i-1* has reported it is about to
    call ``acquire()``, so the arrivals are strictly ordered in practice. Each
    result carries the granted ticket and the acquisition timestamp.
    """
    procs = []
    results = []
    for i in range(n):
        start_file = tmp_path / f"start-{i}"
        start_file.write_text("go", encoding="utf-8")
        env = dict(os.environ)
        env.update(
            {
                "REPO_ROOT": str(REPO_ROOT),
                "SKILLS_ROOT": str(REPO_ROOT / "skill"),
                "SEM_NAME": sem_name,
                "SEM_MAX": str(max_workers),
                "SEM_HOLD": str(hold),
                "SEM_TIMEOUT": str(timeout),
                "SEM_START_FILE": str(start_file),
            }
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", ORDERED_WORKER_TEMPLATE],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        procs.append(proc)
        # Wait until this worker has arrived (about to enter acquire) before
        # launching the next, so the launch order is deterministic.
        deadline = time.time() + 15.0
        while time.time() < deadline:
            line = proc.stderr.readline()
            if line.strip() == "arrived":
                break
        else:  # pragma: no cover - worker failed to start
            proc.kill()
            proc.wait(timeout=10)
            raise RuntimeError(f"ordered worker {i} never arrived")

    for proc in procs:
        out, err = proc.communicate(timeout=timeout + 30)
        try:
            results.append(json.loads(out.strip()))
        except json.JSONDecodeError:
            results.append({"error": f"bad output: {out} {err}"})
    return results


def test_slots_are_granted_in_arrival_order(tmp_path):
    """One slot, four ordered waiters: no later arrival overtakes an earlier."""
    name = f"guard-fifo-{int(time.time())}"
    results = _run_ordered_workers(4, name, tmp_path, max_workers=1, hold=0.15)

    assert all(r.get("error") is None for r in results), results
    assert all(r.get("acquired") is not None for r in results), results
    tickets = [r["ticket"] for r in results]
    assert all(t is not None for t in tickets), (
        f"every waiting acquire must report the ticket it was granted: {results}"
    )
    assert len(set(tickets)) == 4, f"tickets must be distinct arrivals: {tickets}"

    # The fairness contract: a lower ticket (earlier arrival) always acquires
    # before a higher ticket (later arrival). There is exactly one slot, so
    # there is no legitimate overlap.
    ordered = sorted(results, key=lambda r: r["ticket"])
    acquisitions = [r["acquired"] for r in ordered]
    assert acquisitions == sorted(acquisitions), (
        "a later arrival overtook an earlier one: "
        f"tickets={tickets} acquisitions={acquisitions}"
    )


def test_killed_queued_waiter_does_not_hold_its_turn(tmp_path):
    """SIGKILLing a queued waiter must not strand the next waiter.

    The killed waiter holds the lowest outstanding ticket while queued; its
    ``flock`` is released by the kernel, so the next waiter must reclaim the
    turn and acquire within a bounded time (no stale tickets, no PID
    bookkeeping).
    """
    name = f"guard-fifo-kill-{int(time.time())}"
    start = tmp_path / "start-kill"
    start.write_text("go", encoding="utf-8")
    env = dict(os.environ)
    env.update(
        {
            "REPO_ROOT": str(REPO_ROOT),
            "SKILLS_ROOT": str(REPO_ROOT / "skill"),
            "SEM_NAME": name,
            "SEM_MAX": "1",
            "SEM_HOLD": "30",
            "SEM_TIMEOUT": "60",
            "SEM_START_FILE": str(start),
        }
    )
    # A holder takes (and keeps) the only slot.
    holder_env = dict(os.environ)
    holder_env.update(
        {
            "REPO_ROOT": str(REPO_ROOT),
            "SKILLS_ROOT": str(REPO_ROOT / "skill"),
            "SEM_NAME": name,
            "SEM_MAX": "1",
            "SEM_HOLD": "30",
            "SEM_TIMEOUT": "60",
        }
    )
    holder = subprocess.Popen(
        [sys.executable, "-c", WORKER_TEMPLATE],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=holder_env,
    )
    try:
        # Wait until the holder reports acquisition.
        deadline = time.time() + 15.0
        while time.time() < deadline:
            line = holder.stderr.readline()
            if line.strip() and "acquired" in line:
                break
        # A second waiter queues behind the holder (lowest outstanding ticket).
        queued = subprocess.Popen(
            [sys.executable, "-c", ORDERED_WORKER_TEMPLATE],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        deadline = time.time() + 15.0
        while time.time() < deadline:
            line = queued.stderr.readline()
            if line.strip() == "arrived":
                break
        # Let it allocate its ticket and enter the wait, then kill it in place.
        time.sleep(0.3)
        queued.kill()
        queued.wait(timeout=10)

        # Free the slot: the next waiter must not be blocked by the dead turn.
        holder.kill()
        holder.wait(timeout=10)

        t0 = time.monotonic()
        sem = Semaphore(name, max_workers=1, timeout=10)
        with sem:
            pass
        elapsed = time.monotonic() - t0
        assert elapsed < 5.0, (
            "a killed queued waiter must not hold its turn; "
            f"next acquisition took {elapsed:.1f}s"
        )
    finally:
        for proc in (holder,):
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)


def _turnstile_has_locked_ticket(sem):
    """True when the turnstile directory holds a ticket locked by a live waiter.

    A locked (``flock``-held) ticket file is an outstanding arrival: its owner
    has allocated the ticket and is still queued. A reclaimable (unlocked)
    ticket file belongs to a waiter that acquired, timed out or died.
    """
    try:
        names = os.listdir(sem._turnstile_dir)
    except OSError:
        return False
    for entry in names:
        if not entry.isdigit():
            continue
        path = sem._turnstile_dir / entry
        try:
            fd = os.open(path, os.O_RDWR)
        except OSError:
            continue
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                return True  # locked: a live waiter holds this ticket
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
    return False


def test_fail_fast_acquires_free_slot_while_tickets_are_outstanding(tmp_path):
    """A fail-fast caller (timeout=0) must take a free slot immediately even
    when tickets are outstanding — the turnstile bypass must not gate it.

    Closes the audit gap (AC3): the free-slot fail-fast case was only asserted
    with no outstanding ticket, and the outstanding-ticket case only exercised
    the full-pool ``TimeoutError`` path, so “slot free while tickets
    outstanding” was never exercised. Here the only holder is killed so the
    slot is free at the exact moment a frozen (SIGSTOPed) waiter still holds
    the lowest ticket — without the bypass, a fail-fast caller behind that
    ticket would raise immediately instead of acquiring.
    """
    name = f"guard-ff-free-{int(time.time())}"

    # A holder takes (and keeps) the only slot.
    holder_env = dict(os.environ)
    holder_env.update(
        {
            "REPO_ROOT": str(REPO_ROOT),
            "SKILLS_ROOT": str(REPO_ROOT / "skill"),
            "SEM_NAME": name,
            "SEM_MAX": "1",
            "SEM_HOLD": "30",
            "SEM_TIMEOUT": "60",
        }
    )
    holder = subprocess.Popen(
        [sys.executable, "-c", WORKER_TEMPLATE],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=holder_env,
    )
    queued = None
    try:
        # Wait until the holder reports acquisition.
        deadline = time.time() + 15.0
        while time.time() < deadline:
            line = holder.stderr.readline()
            if line.strip() and "acquired" in line:
                break
        else:  # pragma: no cover - holder failed to start
            holder.kill()
            holder.wait(timeout=10)
            raise RuntimeError("holder worker failed to acquire slot")

        # A second waiter queues behind the holder (lowest outstanding ticket).
        start = tmp_path / "start-ff-free"
        start.write_text("go", encoding="utf-8")
        queued_env = dict(os.environ)
        queued_env.update(
            {
                "REPO_ROOT": str(REPO_ROOT),
                "SKILLS_ROOT": str(REPO_ROOT / "skill"),
                "SEM_NAME": name,
                "SEM_MAX": "1",
                "SEM_HOLD": "30",
                "SEM_TIMEOUT": "60",
                "SEM_START_FILE": str(start),
            }
        )
        queued = subprocess.Popen(
            [sys.executable, "-c", ORDERED_WORKER_TEMPLATE],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=queued_env,
        )
        deadline = time.time() + 15.0
        while time.time() < deadline:
            line = queued.stderr.readline()
            if line.strip() == "arrived":
                break
        else:  # pragma: no cover - waiter failed to start
            queued.kill()
            queued.wait(timeout=10)
            raise RuntimeError("queued worker never arrived")

        # Wait until the waiter holds a locked ticket (it is genuinely
        # queued), then FREEZE it in place with SIGSTOP: it stays alive so its
        # kernel flock stays held (an outstanding ticket), but it can never
        # retry the pool — the upcoming free slot is uncontested and the test
        # is deterministic (no sleep-duration ordering).
        probe = Semaphore(name, max_workers=1, timeout=5)
        deadline = time.monotonic() + 5.0
        while (
            not _turnstile_has_locked_ticket(probe)
            and time.monotonic() < deadline
        ):
            time.sleep(0.02)
        assert _turnstile_has_locked_ticket(probe), (
            "queued worker never held a ticket"
        )
        os.kill(queued.pid, signal.SIGSTOP)
        assert _turnstile_has_locked_ticket(probe), (
            "ticket not held after freeze — SIGSTOP released the flock"
        )

        # Free the slot while the frozen waiter's ticket stays outstanding.
        holder.kill()
        holder.wait(timeout=10)

        # The fail-fast caller must bypass the turnstile and take the slot now
        # (audit_runner's timeout=0 semantics: no turnstile deadlock).
        fast = Semaphore(name, max_workers=1, timeout=0)
        t0 = time.monotonic()
        assert fast.acquire() is True
        elapsed = time.monotonic() - t0
        fast.release()

        assert elapsed < 1.0, (
            "fail-fast caller queued behind an outstanding ticket; "
            f"took {elapsed:.2f}s"
        )
        assert fast.last_ticket is None, (
            "fail-fast callers must be exempt from the turnstile"
        )
    finally:
        for proc in (holder, queued):
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)

