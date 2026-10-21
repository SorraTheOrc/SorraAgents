"""Tests for the PR status-check background poller (SA-0MVDP9B83004UD5E).

The poller is the *job* half of the background-job etiquette pattern wired
into the ship release flow: the release wrapper launches it as a background
job and waits on it completion-aware with ``wait_for_job.py``. These tests
exercise the observable behaviour of the poller itself — outcome
classification, the terminal marker it writes, timeout handling, and the
no-CI path — through its public CLI/module API.

Anti-patterns avoided: no source grepping, no re-implementation of the
production logic, and the ``gh`` CLI is replaced with a fake on ``PATH``
rather than monkeypatching internals.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import ClassVar

REPO_ROOT = Path(__file__).resolve().parent.parent
POLLER = REPO_ROOT / "skill" / "ship" / "scripts" / "release" / "wait_for_pr_checks.py"
HELPER = REPO_ROOT / "skill" / "shared" / "wait_for_job.py"
ETIQUETTE = REPO_ROOT / "skill" / "shared" / "background-job-etiquette.md"


def _fake_gh(tmp_dir: Path, rollouts: list[list]) -> Path:
    """Create a fake ``gh`` that returns each rollup in turn (last one sticks)."""
    response_file = tmp_dir / "responses.json"
    response_file.write_text(json.dumps(rollouts), encoding="utf-8")
    counter_file = tmp_dir / "counter"
    counter_file.write_text("0", encoding="utf-8")

    gh = tmp_dir / "gh"
    gh.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"counter = {str(counter_file)!r}\n"
        f"responses = {str(response_file)!r}\n"
        "if sys.argv[1:3] == ['pr', 'view']:\n"
        "    with open(counter) as fh: n = int(fh.read())\n"
        "    data = json.load(open(responses))\n"
        "    rollup = data[min(n, len(data) - 1)]\n"
        "    with open(counter, 'w') as fh: fh.write(str(n + 1))\n"
        "    print(json.dumps({'statusCheckRollup': rollup}))\n"
        "elif sys.argv[1:3] == ['pr', 'merge']:\n"
        "    print('Merged')\n",
        encoding="utf-8",
    )
    gh.chmod(0o755)
    return gh


def _run_poller(args, env=None, timeout=60):
    run_env = dict(os.environ) if env is None else env
    return subprocess.run(
        [sys.executable, str(POLLER), *args],
        capture_output=True,
        text=True,
        env=run_env,
        timeout=timeout,
        check=False,
    )


class TestOutcomeClassification:
    """The poller maps statusCheckRollup states to the release-merge decisions."""

    def test_no_checks_writes_no_checks_outcome(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            _fake_gh(tmp, [[]])
            marker = tmp / "outcome.json"
            result = _run_poller([
                "--pr", "42", "--timeout", "5", "--interval", "0.1",
                "--marker", str(marker),
            ], env={**os.environ, "PATH": f"{tmp}:{os.environ['PATH']}"})
            assert result.returncode == 0, result.stderr
            assert json.loads(marker.read_text())["outcome"] == "no_checks"

    def test_all_passed_writes_passed_outcome(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            _fake_gh(tmp, [[
                {"status": "COMPLETED", "conclusion": "SUCCESS"},
                {"status": "COMPLETED", "conclusion": "SUCCESS"},
            ]])
            marker = tmp / "outcome.json"
            result = _run_poller([
                "--pr", "42", "--timeout", "5", "--interval", "0.1",
                "--marker", str(marker),
            ], env={**os.environ, "PATH": f"{tmp}:{os.environ['PATH']}"})
            assert result.returncode == 0, result.stderr
            assert json.loads(marker.read_text())["outcome"] == "passed"

    def test_failed_check_writes_failed_outcome_and_nonzero(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            _fake_gh(tmp, [[
                {"status": "COMPLETED", "conclusion": "SUCCESS"},
                {"status": "COMPLETED", "conclusion": "FAILURE"},
            ]])
            marker = tmp / "outcome.json"
            result = _run_poller([
                "--pr", "42", "--timeout", "5", "--interval", "0.1",
                "--marker", str(marker),
            ], env={**os.environ, "PATH": f"{tmp}:{os.environ['PATH']}"})
            assert result.returncode == 1, result.stderr
            assert json.loads(marker.read_text())["outcome"] == "failed"

    def test_cancelled_check_is_failed(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            _fake_gh(tmp, [[{"status": "COMPLETED", "conclusion": "CANCELLED"}]])
            marker = tmp / "outcome.json"
            result = _run_poller([
                "--pr", "42", "--timeout", "5", "--interval", "0.1",
                "--marker", str(marker),
            ], env={**os.environ, "PATH": f"{tmp}:{os.environ['PATH']}"})
            assert result.returncode == 1, result.stderr
            assert json.loads(marker.read_text())["outcome"] == "failed"

    def test_pending_then_completed_polls_until_terminal(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            _fake_gh(tmp, [
                [{"status": "IN_PROGRESS", "conclusion": None}],
                [{"status": "COMPLETED", "conclusion": "SUCCESS"}],
            ])
            marker = tmp / "outcome.json"
            result = _run_poller([
                "--pr", "42", "--timeout", "10", "--interval", "0.1",
                "--marker", str(marker),
            ], env={**os.environ, "PATH": f"{tmp}:{os.environ['PATH']}"})
            assert result.returncode == 0, result.stderr
            assert json.loads(marker.read_text())["outcome"] == "passed"


class TestTimeout:
    """A never-settling PR is bounded by --timeout and reports ``timeout``."""

    def test_pending_forever_times_out(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            _fake_gh(tmp, [[{"status": "IN_PROGRESS", "conclusion": None}]])
            marker = tmp / "outcome.json"
            result = _run_poller([
                "--pr", "42", "--timeout", "0.5", "--interval", "0.1",
                "--marker", str(marker),
            ], env={**os.environ, "PATH": f"{tmp}:{os.environ['PATH']}"})
            assert result.returncode == 2, result.stderr
            assert json.loads(marker.read_text())["outcome"] == "timeout"


class TestUsage:
    def test_non_positive_timeout_is_usage_error(self):
        result = _run_poller(["--pr", "42", "--timeout", "0"])
        assert result.returncode == 3
        assert "timeout" in result.stderr.lower()


class TestWiringDecisions:
    """AC1/AC3 — a per-candidate decision is recorded for every named script."""

    #: Every candidate named in the work item's Background section.
    CANDIDATES: ClassVar[list[str]] = [
        "skill/ship/scripts/run-release.js",
        "skill/audit/scripts/audit_runner.py",
        "skill/implement/scripts/implement.py",
        "skill/shared/git_sandbox.py",
        "skill/test/scripts/run_tests.py",
    ]

    def test_etiquette_doc_records_every_candidate(self):
        text = ETIQUETTE.read_text(encoding="utf-8")
        missing = [c for c in self.CANDIDATES if c not in text]
        assert not missing, f"no wiring decision recorded for: {missing}"

    def test_etiquette_doc_marks_the_wired_candidate(self):
        text = ETIQUETTE.read_text(encoding="utf-8")
        assert "waitForPRMerge" in text
        assert "| **wired** |" in text

    def test_wired_candidate_invokes_the_shared_helper(self):
        """The one wired script references wait_for_job (grep-verifiable)."""
        src = (REPO_ROOT / "skill" / "ship" / "scripts" / "run-release.js").read_text(
            encoding="utf-8"
        )
        assert "wait_for_job.py" in src
        assert "wait_for_pr_checks.py" in src

    def test_wired_candidate_has_no_fixed_sleep_poll(self):
        src = (REPO_ROOT / "skill" / "ship" / "scripts" / "run-release.js").read_text(
            encoding="utf-8"
        )
        assert "execSync('sleep" not in src
        assert 'execSync("sleep' not in src


class TestBackgroundWaitIntegration:
    """The poller is launchable as a background job waited on by the helper."""

    def test_poller_completes_a_wait_for_job_marker_wait(self):
        """End-to-end: launch the poller detached, wait via wait_for_job.py.

        Exercises the actual wiring shape the release wrapper uses: the
        poller writes a terminal marker and ``wait_for_job.py`` returns as
        soon as that marker appears (not after a fixed sleep).
        """
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            _fake_gh(tmp, [[{"status": "COMPLETED", "conclusion": "SUCCESS"}]])
            marker = tmp / "outcome.json"
            job = subprocess.Popen(
                [sys.executable, str(POLLER), "--pr", "42",
                 "--timeout", "10", "--interval", "0.1",
                 "--marker", str(marker)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env={**os.environ, "PATH": f"{tmp}:{os.environ['PATH']}"},
                start_new_session=True,
            )
            try:
                waited = subprocess.run(
                    [sys.executable, str(HELPER),
                     "--pid", str(job.pid), "--marker", str(marker),
                     "--timeout", "30", "--interval", "0.2"],
                    capture_output=True, text=True, timeout=60, check=False,
                )
                assert waited.returncode == 0, waited.stderr
                assert json.loads(marker.read_text())["outcome"] == "passed"
            finally:
                job.wait(timeout=15)
