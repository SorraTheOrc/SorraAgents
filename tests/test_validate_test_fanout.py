"""Contract tests for skill/shared/validate_test_fanout.py (SA-0MVF78Y3J004MGPM).

Test-first slice (child .1) of SA-0MVE995PV0077QB5 ("Raise
TEST_MAX_CONCURRENCY 2->3 with mini-F7 validation"). These tests pin the
test-suite fan-out harness's CLI and JSON-report contract *before* the
harness exists (it is built by SA-0MVF790HW005M0P3), so the measurement that
gates the ceiling raise cannot drift or silently change shape.

What is pinned:

- CLI: ``--help`` exits 0 and documents ``--ceiling``, ``--n``, ``--out``,
  ``--suite-seconds`` and ``--dry-run``; defaults are ``--ceiling 2`` and
  ``--n 8``; out-of-range values are rejected with a clear message.
- Report shape: ``success`` + ``config`` (``ceiling``, ``n``,
  ``suite_seconds``, ``semaphore_dir``), ``before``/``after`` snapshots,
  ``peak`` (``processes``, ``load``), ``batch`` (``n``, ``ok``,
  ``elapsed_seconds``, ``baseline_load``) and ``results`` carrying one
  ``exit_code`` entry per suite invocation.
- Synthetic payload: the throwaway repo carries ``.pi/test-config.json`` with
  a ``suiteCommands`` entry, and each batch's ``PI_SEMAPHORE_DIR`` lives
  inside that batch's directory (never the host-wide pool).

Unit tests never run a full 8-invocation batch: they use ``--dry-run`` or a
single ``--n 1`` invocation with a short synthetic suite.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "skill" / "shared" / "validate_test_fanout.py"

# Test-first slice: the harness is built by the next child of
# SA-0MVE995PV0077QB5 (SA-0MVF790HW005M0P3). Until it lands the contract is
# skipped rather than failed so this slice keeps the tree green; the guard is
# inert as soon as the module exists, and the assertions below are exactly
# what that child must satisfy.
pytestmark = pytest.mark.skipif(
    not SCRIPT_PATH.exists(),
    reason=(
        "skill/shared/validate_test_fanout.py is not implemented yet — the "
        "pinned harness contract activates when SA-0MVF790HW005M0P3 lands"
    ),
)

#: The documented CLI flags (AC2 of SA-0MVF78Y3J004MGPM).
DOCUMENTED_FLAGS = ("--ceiling", "--n", "--out", "--suite-seconds", "--dry-run")

#: Generous ceiling for a single synthetic invocation (well under the 30 s
#: unit-test budget in practice; startup dominates a one-suite batch).
SINGLE_RUN_TIMEOUT_SECONDS = 120


def _run(*args: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    """Run the harness with *args* and return the completed process."""
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH), *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def _report(*args: str, timeout: int = 60) -> dict:
    """Run the harness and parse its JSON report from stdout."""
    result = _run(*args, timeout=timeout)
    assert result.returncode == 0, (
        f"harness {' '.join(args)} failed ({result.returncode}):\n"
        f"stdout:\n{result.stdout[-2000:]}\nstderr:\n{result.stderr[-2000:]}"
    )
    return json.loads(result.stdout)


def _host_semaphore_base() -> Path:
    """Resolve the host-wide semaphore base dir, mirroring process_semaphore."""
    override = os.environ.get("PI_SEMAPHORE_DIR")
    if override:
        return Path(override)
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    base = Path(runtime) if runtime else Path(tempfile.gettempdir())
    return base / "pi-semaphores"


def _find_batch_dir(semaphore_dir: Path) -> Path:
    """Walk up from *semaphore_dir* to the batch dir holding the payload repo."""
    current = semaphore_dir.resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".pi" / "test-config.json").is_file():
            return candidate
    raise AssertionError(
        f"no .pi/test-config.json found above semaphore dir {semaphore_dir} — "
        "the isolated PI_SEMAPHORE_DIR must live inside the synthetic batch repo"
    )


# ---------------------------------------------------------------------------
# CLI contract
# ---------------------------------------------------------------------------


def test_help_exits_zero_and_documents_cli_flags():
    """``--help`` succeeds and documents every flag from the pinned contract."""
    result = _run("--help")
    assert result.returncode == 0, f"--help failed: {result.stderr[-2000:]}"
    help_text = result.stdout + result.stderr
    assert "usage:" in help_text.lower()
    for flag in DOCUMENTED_FLAGS:
        assert flag in help_text, f"--help does not document {flag}"


def test_dry_run_defaults_are_ceiling_two_and_n_eight():
    """With no flags the harness reports the documented defaults (AC3)."""
    config = _report("--dry-run")["config"]
    assert config["ceiling"] == 2, f"expected default ceiling 2, got {config['ceiling']!r}"
    assert config["n"] == 8, f"expected default n 8, got {config['n']!r}"


@pytest.mark.parametrize("value", ["0", "-1"])
def test_ceiling_below_one_is_rejected(value):
    """``--ceiling`` below 1 exits non-zero with a message naming the flag."""
    result = _run("--ceiling", value, "--dry-run")
    assert result.returncode != 0, f"--ceiling {value} should be rejected"
    combined = (result.stdout + result.stderr).lower()
    assert "--ceiling" in combined, f"rejection message should name --ceiling: {combined[-2000:]}"
    assert any(hint in combined for hint in ("must be", "at least", ">= 1", "positive")), (
        f"rejection message should state the valid range: {combined[-2000:]}"
    )


@pytest.mark.parametrize("value", ["0", "-1"])
def test_n_below_one_is_rejected(value):
    """``--n`` below 1 exits non-zero with a message naming the flag."""
    result = _run("--n", value, "--dry-run")
    assert result.returncode != 0, f"--n {value} should be rejected"
    combined = (result.stdout + result.stderr).lower()
    assert "--n" in combined, f"rejection message should name --n: {combined[-2000:]}"
    assert any(hint in combined for hint in ("must be", "at least", ">= 1", "positive")), (
        f"rejection message should state the valid range: {combined[-2000:]}"
    )


# ---------------------------------------------------------------------------
# Report shape
# ---------------------------------------------------------------------------


def test_dry_run_report_has_pinned_shape():
    """The dry-run report carries every pinned section and key (AC4)."""
    report = _report("--dry-run")

    assert report["success"] is True
    for key in ("success", "config", "before", "after", "peak", "batch", "results"):
        assert key in report, f"missing top-level {key!r} in report"

    config = report["config"]
    for key in ("ceiling", "n", "suite_seconds", "semaphore_dir"):
        assert key in config, f"missing config.{key} in report"
    assert isinstance(config["ceiling"], int)
    assert isinstance(config["n"], int)
    assert isinstance(config["suite_seconds"], (int, float))
    assert config["suite_seconds"] > 0
    assert isinstance(config["semaphore_dir"], str) and config["semaphore_dir"]

    for snapshot_key in ("before", "after"):
        snapshot = report[snapshot_key]
        assert isinstance(snapshot["processes"], dict), f"{snapshot_key}.processes must be a dict"
        assert isinstance(snapshot["load"], dict), f"{snapshot_key}.load must be a dict"

    peak = report["peak"]
    assert isinstance(peak["processes"], dict), "peak.processes must be a dict"
    assert isinstance(peak["load"], dict), "peak.load must be a dict"

    batch = report["batch"]
    for key in ("n", "ok", "elapsed_seconds", "baseline_load"):
        assert key in batch, f"missing batch.{key} in report"
    assert batch["n"] == config["n"]
    assert isinstance(batch["ok"], bool)
    assert isinstance(batch["elapsed_seconds"], (int, float))
    assert batch["elapsed_seconds"] >= 0
    assert isinstance(batch["baseline_load"], (int, float))

    # A dry run launches nothing, so it has no per-invocation results yet.
    assert report["results"] == []


def test_out_writes_the_same_json_report(tmp_path):
    """``--out`` persists the JSON report and matches the stdout report."""
    out_path = tmp_path / "report.json"
    report = _report("--dry-run", "--out", str(out_path))

    assert out_path.is_file(), f"--out did not write {out_path}"
    persisted = json.loads(out_path.read_text())
    for key in ("success", "config", "before", "after", "peak", "batch", "results"):
        assert key in persisted, f"missing {key!r} in persisted report"
    assert persisted["config"]["ceiling"] == report["config"]["ceiling"]
    assert persisted["config"]["n"] == report["config"]["n"]
    assert persisted["config"]["semaphore_dir"] == report["config"]["semaphore_dir"]


# ---------------------------------------------------------------------------
# Synthetic payload + semaphore isolation
# ---------------------------------------------------------------------------


def test_dry_run_prepares_synthetic_suite_payload():
    """The prepared batch repo declares a ``suiteCommands`` synthetic suite.

    ``--dry-run`` intentionally leaves the prepared batch directory in place
    so the synthetic-payload contract is inspectable (this test does not
    launch anything).
    """
    report = _report("--dry-run")
    semaphore_dir = Path(report["config"]["semaphore_dir"])
    assert semaphore_dir.is_dir(), (
        f"PI_SEMAPHORE_DIR {semaphore_dir} should exist after --dry-run"
    )

    batch_dir = _find_batch_dir(semaphore_dir)
    config_path = batch_dir / ".pi" / "test-config.json"
    payload = json.loads(config_path.read_text())
    suite_commands = payload.get("suiteCommands")
    assert isinstance(suite_commands, list) and suite_commands, (
        f"{config_path} must declare a non-empty suiteCommands list, got {suite_commands!r}"
    )
    assert all(isinstance(cmd, str) and cmd.strip() for cmd in suite_commands), (
        f"suiteCommands entries must be non-empty strings: {suite_commands!r}"
    )

    # The isolated semaphore pool lives inside the batch directory.
    resolved_sem = semaphore_dir.resolve()
    resolved_batch = batch_dir.resolve()
    assert resolved_sem == resolved_batch or resolved_batch in resolved_sem.parents, (
        f"PI_SEMAPHORE_DIR {resolved_sem} must live inside batch dir {resolved_batch}"
    )


def test_semaphore_dir_is_per_batch_and_never_the_host_pool():
    """Each batch gets its own pool, never the host-wide semaphore directory."""
    first = Path(_report("--dry-run")["config"]["semaphore_dir"]).resolve()
    second = Path(_report("--dry-run")["config"]["semaphore_dir"]).resolve()
    assert first != second, (
        "each batch must use an isolated PI_SEMAPHORE_DIR, but two dry runs "
        f"reported the same path: {first}"
    )

    host_pool = _host_semaphore_base().resolve()
    for semaphore_dir in (first, second):
        assert semaphore_dir != host_pool, (
            f"batch reused the host-wide semaphore pool {host_pool}"
        )
        assert host_pool not in semaphore_dir.parents, (
            f"batch semaphore dir {semaphore_dir} is nested inside the host pool {host_pool}"
        )
        assert semaphore_dir not in host_pool.parents, (
            f"batch semaphore dir {semaphore_dir} contains the host pool {host_pool}"
        )


# ---------------------------------------------------------------------------
# Single-invocation batch (kept small: never a full 8-invocation batch)
# ---------------------------------------------------------------------------


def test_single_invocation_batch_reports_one_exit_code(tmp_path):
    """A one-suite batch reports exactly one ``exit_code`` result and succeeds."""
    out_path = tmp_path / "ceiling-1.json"
    report = _report(
        "--n", "1",
        "--ceiling", "1",
        "--suite-seconds", "0.5",
        "--out", str(out_path),
        timeout=SINGLE_RUN_TIMEOUT_SECONDS,
    )

    assert report["success"] is True
    assert report["batch"]["n"] == 1
    results = report["results"]
    assert len(results) == 1, f"expected one result per invocation, got {results!r}"
    assert "exit_code" in results[0], f"result entry must carry exit_code: {results[0]!r}"
    assert all(entry["exit_code"] == 0 for entry in results), (
        f"synthetic suite invocation failed: {results!r}"
    )
    assert report["batch"]["ok"] is True
    assert isinstance(report["batch"]["baseline_load"], (int, float))

    assert out_path.is_file(), f"--out did not write {out_path}"
    persisted = json.loads(out_path.read_text())
    assert persisted["config"]["ceiling"] == 1
    assert len(persisted["results"]) == 1
