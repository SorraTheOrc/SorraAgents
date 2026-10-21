"""Per-suite vitest/tinypool worker-pool bound (SA-0MVE98T1M006A17T).

The host-wide ``TEST_MAX_CONCURRENCY`` semaphore bounds how many **suites**
run at once, but a single vitest suite sizes its tinypool pool to the CPU
count (15+ forks observed in SA-0MSAEKOQE009TEB4). This module pins the
per-suite complement: every suite command the test skill executes inherits a
``VITEST_MAX_WORKERS`` value in its child environment, so each vitest run is
bounded to a host-right number of workers.

Assertions are behavioural: the injected value is read back from a **real**
child process through :func:`_run_cmd` (the single suite-command execution
point), so a regression that drops the injection fails here instead of
silently restoring the unbounded fan-out.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Bootstrap sys.path — mirror test_test_skill_semaphore.py so `import
# run_tests` works regardless of collection order.
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_RUNNER_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_RUNNER_DIR) not in sys.path:
    sys.path.insert(0, str(_RUNNER_DIR))

from run_tests import (
    TEST_VITEST_MAX_WORKERS_DEFAULT,
    TEST_VITEST_MAX_WORKERS_ENV,
    _run_cmd,
    _suite_command_env,
    _test_vitest_max_workers,
    full_suite_commands,
)

_ENV = TEST_VITEST_MAX_WORKERS_ENV


def _child_env_probe(tmp_path: Path) -> dict[str, str]:
    """Read the child environment through the real suite execution path.

    Runs a trivial Python child via :func:`_run_cmd` (the same function every
    suite command goes through) and returns the variables it observed.
    """
    script = (
        "import json, os;"
        f"print(json.dumps({{'{_ENV}': os.environ.get('{_ENV}'),"
        "'NODE_OPTIONS': os.environ.get('NODE_OPTIONS')}))"
    )
    proc = _run_cmd([sys.executable, "-c", script], cwd=tmp_path, timeout=60)
    assert proc.returncode == 0, proc.stderr
    import json

    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_default_cap_reaches_the_child_process(tmp_path, monkeypatch):
    """With no ambient override the child sees the calibrated default (4)."""
    monkeypatch.delenv(_ENV, raising=False)

    observed = _child_env_probe(tmp_path)

    assert observed[_ENV] == str(TEST_VITEST_MAX_WORKERS_DEFAULT) == "4"


def test_ambient_override_wins(tmp_path, monkeypatch):
    """An operator/consumer-set value is preserved, never overridden."""
    monkeypatch.setenv(_ENV, "2")

    assert _test_vitest_max_workers() == 2
    assert _child_env_probe(tmp_path)[_ENV] == "2"


@pytest.mark.parametrize("raw", ["0", "-3"])
def test_values_below_one_clamp_to_one(raw, monkeypatch):
    """A zero/negative cap would mean 'unbounded' — clamp to serial instead."""
    monkeypatch.setenv(_ENV, raw)

    assert _test_vitest_max_workers() == 1


def test_invalid_value_warns_and_falls_back(monkeypatch, capsys):
    """A non-numeric value must not break runs; warn and use the default."""
    monkeypatch.setenv(_ENV, "lots")

    assert _test_vitest_max_workers() == TEST_VITEST_MAX_WORKERS_DEFAULT
    assert _ENV in capsys.readouterr().err


def test_invalid_ambient_value_is_normalised_for_children(tmp_path, monkeypatch):
    """The child inherits the resolved default, not the invalid raw value."""
    monkeypatch.setenv(_ENV, "lots")

    assert _child_env_probe(tmp_path)[_ENV] == str(TEST_VITEST_MAX_WORKERS_DEFAULT)


def test_node_options_is_preserved(tmp_path, monkeypatch):
    """NODE_OPTIONS plumbing is untouched; the cap is additive."""
    monkeypatch.delenv(_ENV, raising=False)
    monkeypatch.setenv("NODE_OPTIONS", "--max-old-space-size=512")

    observed = _child_env_probe(tmp_path)

    assert observed["NODE_OPTIONS"] == "--max-old-space-size=512"
    assert observed[_ENV] == str(TEST_VITEST_MAX_WORKERS_DEFAULT)


def test_suite_command_strings_are_not_rewritten(monkeypatch):
    """The cap is env-only, so the per-repo cache key is unchanged.

    Read-only consumers (the audit skill) resolve the suite through
    ``full_suite_commands``; if the cap were injected as CLI flags the
    canonical command would differ between producer and consumer and the
    cached full-suite evidence would be missed.
    """
    monkeypatch.delenv(_ENV, raising=False)
    before = full_suite_commands(REPO_ROOT)

    monkeypatch.setenv(_ENV, "2")
    after = full_suite_commands(REPO_ROOT)

    assert after == before
    assert not any("maxWorkers" in cmd for cmd in after)


def test_env_helper_does_not_mutate_os_environ(monkeypatch):
    """Building the child environment must not leak the cap into this process."""
    monkeypatch.delenv(_ENV, raising=False)

    env = _suite_command_env()

    assert env[_ENV] == str(TEST_VITEST_MAX_WORKERS_DEFAULT)
    assert _ENV not in os.environ
