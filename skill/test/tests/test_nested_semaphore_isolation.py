"""Nested-run semaphore isolation (SA-0MUY6U05F008RG86).

A full-suite run holds a host-wide ``"test"`` semaphore slot for its whole
duration. A test *inside* that suite which invokes ``run_tests.py`` again is a
**nested run**; if it acquires the same namespace it queues behind the slot its
own outer run holds and can wait out ``TEST_LOCK_TIMEOUT`` (observed 2700s
hangs in ``implement.py finish`` and the pre-push gate when two full suites ran
concurrently). The contract under test:

- AC1 — a nested run whose fixture points ``PI_SEMAPHORE_DIR`` at a per-test
  isolated directory completes while the host namespace is saturated, and two
  concurrent outer runs each containing a nested run both complete.
- AC3 — top-level (non-nested) runs still contend for the host-wide namespace:
  a nested run that is *not* isolated and shares the host directory still waits
  (bounded) behind a held slot.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Bootstrap sys.path so `run_tests` and `shared` import from this subtree too
# (mirrors test_test_skill_semaphore.py).
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[3]
_SKILLS_ROOT = REPO_ROOT / "skill"
_RUNNER_DIR = Path(__file__).resolve().parent.parent / "scripts"
for _path in (str(REPO_ROOT), str(_SKILLS_ROOT), str(_RUNNER_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from run_tests import nested_semaphore_dir
from shared.process_semaphore import Semaphore

RUN_TESTS_PY = _RUNNER_DIR / "run_tests.py"
TEST_SEMAPHORE = "test"

#: Outer worker: hold a host-wide slot, then run a nested runner in isolation.
#: Used to prove two concurrent outers never deadlock their own nested runs.
_OUTER_WORKER = r"""
import json
import os
import subprocess
import sys

sys.path.insert(0, os.environ["RUNNER_DIR"])
from run_tests import _test_concurrency_slot  # noqa: E402

result = {"outer_acquired": False, "nested_returncode": None, "error": None}
try:
    with _test_concurrency_slot():
        result["outer_acquired"] = True
        env = dict(os.environ)
        env["PI_SEMAPHORE_DIR"] = os.environ["NESTED_SEM_DIR"]
        proc = subprocess.run(
            [
                sys.executable,
                os.environ["RUN_TESTS"],
                "--project-root", os.environ["NESTED_REPO"],
                "--suite", "all",
                "--scope", "full",
                "--json",
                "--no-cache",
            ],
            cwd=os.environ["NESTED_REPO"],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        result["nested_returncode"] = proc.returncode
        result["nested_stdout"] = proc.stdout[-400:]
        result["nested_stderr"] = proc.stderr[-400:]
except Exception as exc:  # noqa: BLE001
    result["error"] = f"{type(exc).__name__}: {exc}"

print(json.dumps(result))
"""


def _make_noop_project(tmp_path: Path, name: str = "nested-repo") -> Path:
    """A committed repo whose whole suite is ``bash noop.sh`` (no real tests)."""
    repo = tmp_path / name
    repo.mkdir()
    for args in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "nested-test@example.com"],
        ["git", "config", "user.name", "Nested Test"],
    ):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)
    (repo / ".pi").mkdir()
    (repo / ".pi" / "test-config.json").write_text(
        json.dumps({"suiteCommands": ["bash noop.sh"]}), encoding="utf-8"
    )
    (repo / "noop.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "init"], cwd=repo, check=True, capture_output=True
    )
    return repo


def _run_nested(repo: Path, env_overrides: dict[str, str], timeout: int = 30):
    """Run ``run_tests.py`` as a nested child with the given env overrides."""
    env = dict(os.environ)
    env.update(env_overrides)
    return subprocess.run(
        [
            sys.executable,
            str(RUN_TESTS_PY),
            "--project-root",
            str(repo),
            "--suite",
            "all",
            "--scope",
            "full",
            "--json",
            "--no-cache",
        ],
        cwd=str(repo),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def test_nested_semaphore_dir_is_isolated(tmp_path):
    """The helper names a dedicated subdirectory, distinct from its base."""
    base = tmp_path / "session"
    isolated = nested_semaphore_dir(base)

    assert isolated == base / "pi-semaphores"
    assert isolated != base


def test_nested_run_completes_while_host_slot_is_held(tmp_path, monkeypatch):
    """AC1: an isolated nested run never waits on the outer host slot."""
    repo = _make_noop_project(tmp_path)
    host_dir = tmp_path / "host-semaphores"
    monkeypatch.setenv("PI_SEMAPHORE_DIR", str(host_dir))
    monkeypatch.setenv("TEST_MAX_CONCURRENCY", "1")

    holder = Semaphore(TEST_SEMAPHORE, max_workers=1, timeout=5)
    holder.acquire()
    try:
        proc = _run_nested(
            repo,
            {
                "PI_SEMAPHORE_DIR": str(nested_semaphore_dir(tmp_path)),
                "TEST_MAX_CONCURRENCY": "1",
            },
        )
    finally:
        holder.release()

    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["success"] is True, payload


def test_nested_run_sharing_host_dir_waits_bounded(tmp_path, monkeypatch):
    """AC3: without isolation the nested run still contends host-wide."""
    repo = _make_noop_project(tmp_path)
    host_dir = tmp_path / "host-semaphores"
    monkeypatch.setenv("PI_SEMAPHORE_DIR", str(host_dir))
    monkeypatch.setenv("TEST_MAX_CONCURRENCY", "1")

    holder = Semaphore(TEST_SEMAPHORE, max_workers=1, timeout=5)
    holder.acquire()
    try:
        proc = _run_nested(
            repo,
            {
                # Deliberately NOT isolated: same namespace as the held slot.
                "PI_SEMAPHORE_DIR": str(host_dir),
                "TEST_MAX_CONCURRENCY": "1",
                "TEST_LOCK_TIMEOUT": "1",
            },
        )
    finally:
        holder.release()

    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "concurrency slot" in (proc.stdout + proc.stderr), proc.stdout


def test_two_concurrent_outers_with_nested_runs_complete(tmp_path):
    """AC1: two concurrent outer runs, each with an isolated nested run.

    Both outer processes hold a slot in the shared host namespace while their
    nested runs execute. Isolation means the nested runs never need a third
    slot, so the whole thing finishes promptly instead of deadlocking.
    """
    repo = _make_noop_project(tmp_path)
    host_dir = tmp_path / "shared-host-semaphores"

    def _outer_env(nested_dir: Path) -> dict[str, str]:
        env = dict(os.environ)
        env.update(
            {
                "RUNNER_DIR": str(_RUNNER_DIR),
                "RUN_TESTS": str(RUN_TESTS_PY),
                "NESTED_REPO": str(repo),
                "NESTED_SEM_DIR": str(nested_dir),
                "PI_SEMAPHORE_DIR": str(host_dir),
                "TEST_MAX_CONCURRENCY": "2",
                "TEST_LOCK_TIMEOUT": "30",
            }
        )
        return env

    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _OUTER_WORKER],
            env=_outer_env(nested_semaphore_dir(tmp_path / f"outer-{i}")),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for i in range(2)
    ]

    results = []
    for proc in procs:
        out, err = proc.communicate(timeout=90)
        try:
            results.append(json.loads(out.strip()))
        except json.JSONDecodeError:
            pytest.fail(f"outer worker produced no JSON:\n{out}\n{err}")

    assert all(r["outer_acquired"] for r in results), results
    assert all(r["error"] is None for r in results), results
    assert all(r["nested_returncode"] == 0 for r in results), results
