"""Failure-notice and output diagnostics on suite results.

Covers WL-0MVGUT787000LNDG: when a suite fails WITHOUT structured test
failures, ``run_suite`` must carry a stable machine-readable ``notice_code``
(so ``implement.py`` can classify slot contention by code, never by string
matching the human-readable text) and a bounded ``stdout``/``stderr`` tail
(so an unparsed vitest/node failure reports its output). ``run_all``
aggregates both so ``guarded_run_all`` consumers see them at the top level.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

_SCRIPT_DIR = Path(__file__).resolve().parent
_RUNNER_DIR = _SCRIPT_DIR.parent / "scripts"
if str(_RUNNER_DIR) not in sys.path:
    sys.path.insert(0, str(_RUNNER_DIR))

import run_tests
from run_tests import (
    NOTICE_COMMAND_NOT_FOUND,
    NOTICE_SLOT_BUSY,
    NOTICE_SUITE_TIMEOUT,
    SLOT_BUSY_NOTICE_PREFIX,
    run_all,
    run_suite,
)

_CMD = "pytest -q -r a --disable-warnings"

_NOISE = "\n".join(f"stderr-line-{i}" for i in range(2000))


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".pi").mkdir()
    (repo / ".pi" / "test-config.json").write_text(
        '{"suiteCommands": ["%s"]}' % _CMD, encoding="utf-8"
    )
    return repo


def _fake_run(stdout: str = "", stderr: str = "", returncode: int = 1):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


class TestRunSuiteDiagnostics:
    def test_carries_bounded_output_and_no_notice(self, tmp_path) -> None:
        with mock.patch.object(
            run_tests, "_run_cmd", return_value=_fake_run(stdout=_NOISE, stderr=_NOISE)
        ), mock.patch.object(run_tests, "_test_concurrency_slot"):
            result = run_suite("all", cwd=_repo(tmp_path), use_cache=False)

        # The output tail survives; the head is dropped and the field stays
        # bounded so JSON reports cannot balloon.
        assert "stderr-line-1999" in result["stderr"]
        assert "stderr-line-0" not in result["stderr"]
        assert len(result["stderr"]) < len(_NOISE)
        assert "stderr-line-1999" in result["stdout"]
        assert len(result["stdout"]) < len(_NOISE)
        assert result["notice"] == ""
        assert result["notice_code"] == ""

    def test_command_not_found_is_classified(self, tmp_path) -> None:
        def raise_not_found(*args, **kwargs):
            # errno + filename form so ``exc.filename == "vitest"``.
            raise FileNotFoundError(2, "No such file or directory", "vitest")

        with mock.patch.object(run_tests, "_run_cmd", side_effect=raise_not_found), \
             mock.patch.object(run_tests, "_test_concurrency_slot"):
            result = run_suite("all", cwd=_repo(tmp_path), use_cache=False)

        assert result["notice_code"] == NOTICE_COMMAND_NOT_FOUND
        assert "vitest" in result["notice"]
        assert result["success"] is False

    def test_suite_timeout_is_classified(self, tmp_path) -> None:
        from subprocess import TimeoutExpired

        def raise_timeout(*args, **kwargs):
            raise TimeoutExpired(_CMD, timeout=600)

        with mock.patch.object(run_tests, "_run_cmd", side_effect=raise_timeout), \
             mock.patch.object(run_tests, "_test_concurrency_slot"):
            result = run_suite("all", cwd=_repo(tmp_path), use_cache=False)

        assert result["notice_code"] == NOTICE_SUITE_TIMEOUT
        assert "suite timed out" in result["notice"]

    def test_slot_contention_is_classified(self, tmp_path) -> None:
        def raise_slot_busy(*args, **kwargs):
            raise run_tests.TestConcurrencyTimeout(
                f"{SLOT_BUSY_NOTICE_PREFIX} no free slot within 600s"
            )

        with mock.patch.object(run_tests, "_run_cmd", side_effect=raise_slot_busy), \
             mock.patch.object(run_tests, "_test_concurrency_slot"):
            result = run_suite("all", cwd=_repo(tmp_path), use_cache=False)

        assert result["notice_code"] == NOTICE_SLOT_BUSY
        assert result["notice"].startswith(SLOT_BUSY_NOTICE_PREFIX)
        assert result["failures"] == []

    def test_slot_busy_prefix_is_used_in_the_exception_message(self, tmp_path) -> None:
        class _BusySemaphore:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                raise TimeoutError("no slot")

            def __exit__(self, *a):
                return False

        with mock.patch.object(run_tests, "Semaphore", _BusySemaphore):
            with pytest.raises(run_tests.TestConcurrencyTimeout) as exc_info:
                with run_tests._test_concurrency_slot():
                    pass  # pragma: no cover

        assert str(exc_info.value).startswith(SLOT_BUSY_NOTICE_PREFIX)


class TestRunAllAggregation:
    def test_aggregates_notices_codes_and_output_tails(self, monkeypatch) -> None:
        suites = {
            "all": {
                "success": False,
                "returncode": 1,
                "command": _CMD,
                "failures": [],
                "notice": f"{SLOT_BUSY_NOTICE_PREFIX} no free slot within 600s",
                "notice_code": NOTICE_SLOT_BUSY,
                "stdout": "head\ntail-of-stdout",
                "stderr": "tail-of-stderr",
                "scope": "full",
                "type": "full",
                "changed_files": [],
            },
            "node": {
                "success": True,
                "returncode": 0,
                "command": "npm --silent test",
                "failures": [],
                "notice": "",
                "notice_code": "",
                "stdout": "",
                "stderr": "",
                "scope": "full",
                "type": "full",
                "changed_files": [],
            },
        }

        def fake_run_suite(name, **kwargs):
            return suites[name]

        monkeypatch.setattr(run_tests, "run_suite", fake_run_suite)

        result = run_all(("all", "node"), use_cache=False)

        assert result["notices"] == [suites["all"]["notice"]]
        assert result["notice_codes"] == [NOTICE_SLOT_BUSY]
        assert "tail-of-stdout" in result["stdout"]
        assert "tail-of-stderr" in result["stderr"]
