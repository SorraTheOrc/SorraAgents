"""Behavioural tests for the finish-lock contention harness.

Covers **SA-0MWFK10NH006QI7E**: ``scripts/measure_finish_contention.py``
reproduces the 2026-11-13 baseline bucketing over the pi session-log corpus
and measures a synthetic N-way concurrent ``finish`` fan-out, so the parent
epic's outcome is evidenced rather than assumed.

Two layers, mirroring the harness itself:

* The **session** layer is exercised on synthetic ``*.jsonl`` files (real
  assistant ``toolCall`` / ``toolResult`` pairs) so every metric has a
  deterministic fixture: bucket classification, reported vs absorbed lock
  waits, the genuine-run filter, rebase/gate counting, session selection, the
  window boundary, aggregate parallelism and the pass/fail/inconclusive
  verdicts.
* The **fan-out** layer drives the real ``phase_finish`` six-way through the
  harness with sub-second stub latencies, asserting each session is measured;
  the threshold evaluation itself is tested on fabricated rows so a red verdict
  is proven reachable without a slow, flaky run.

No test reads the live session store, runs a real suite or touches the real
repository.
"""
from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "measure_finish_contention.py"
ARTIFACT = REPO_ROOT / "docs" / "dev" / "finish-contention-measurement.json"


def _load_module():
    """Load the harness by file location (``scripts`` is not import-safe)."""
    assert SCRIPT.exists(), f"harness missing: {SCRIPT}"
    spec = importlib.util.spec_from_file_location(
        "_measure_finish_contention", SCRIPT
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def harness():
    return _load_module()


# ---------------------------------------------------------------------------
# Synthetic session-file builders
# ---------------------------------------------------------------------------


def _record(role: str, content, timestamp: str, **extra) -> dict:
    message = {"role": role, "content": content}
    message.update(extra)
    return {"type": "message", "timestamp": timestamp, "message": message}


def _tool_call(
    call_id: str,
    command: str,
    timestamp: str,
    name: str = "bash",
) -> dict:
    arguments = {"command": command} if name == "bash" else {}
    return _record(
        "assistant",
        [{"type": "toolCall", "id": call_id, "name": name,
          "arguments": arguments}],
        timestamp,
    )


def _tool_result(
    call_id: str,
    text: str,
    timestamp: str,
    tool: str = "bash",
) -> dict:
    return _record(
        "toolResult",
        [{"type": "text", "text": text}],
        timestamp,
        toolCallId=call_id,
        toolName=tool,
    )


def _bash(
    call_id: str,
    command: str,
    start: str,
    end: str,
    text: str = "",
) -> list[dict]:
    """One paired bash call/result (the unit every metric is derived from)."""
    return [
        _tool_call(call_id, command, start),
        _tool_result(call_id, text, end),
    ]


def _write_session(
    root: Path,
    name: str,
    records: list[dict],
    *,
    project: str = "--home-agent-projects-AI_Hell-.worklog-worktrees-wl-AH-1--",
) -> Path:
    directory = root / project
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.jsonl"
    path.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )
    return path


def _start_call(start: str = "2026-11-13T10:00:00Z") -> list[dict]:
    """An implement-skill marker call so the session is selected."""
    started = datetime.fromisoformat(start.replace("Z", "+00:00"))
    ended = (started + timedelta(seconds=5)).isoformat().replace("+00:00", "Z")
    return _bash(
        "start-1",
        "python3 skill/implement/scripts/implement.py start WL-1",
        start,
        ended,
        "Implementation started",
    )


FINISH = "python3 skill/implement/scripts/implement.py finish WL-1 --keep-worktree"
REFUSAL = (
    "Another run holds the repository lock "
    "/repo/.worklog/locks/main-checkout.lock (holder: work item WL-2, pid 9)."
)


# ---------------------------------------------------------------------------
# Bucket classification (same definitions as the 2026-11-13 baseline)
# ---------------------------------------------------------------------------


def test_classify_command_precedence(harness):
    """A wait wrapper outranks the call it drives; polling needs both parts."""
    assert harness.classify_command("python3 wait_for_job.py --log x") == (
        "wait_for_job"
    )
    assert harness.classify_command(
        "python3 implement.py finish WL-1"
    ) == "own_finish"
    # run_tests.py behind wait_for_job stays in the wait_for_job bucket.
    assert harness.classify_command(
        "wait_for_job.py -- python3 run_tests.py --scope changed"
    ) == "wait_for_job"
    assert harness.classify_command(
        "python3 run_tests.py --scope changed"
    ) == "scoped_tests"
    assert harness.classify_command(
        "for i in 1 2 3; do sleep 20; grep -c done log; done"
    ) == "polling"
    assert harness.classify_command("git status --porcelain") == "other"


# ---------------------------------------------------------------------------
# Session parsing / selection
# ---------------------------------------------------------------------------


def test_parse_session_pairs_call_and_result(harness, tmp_path):
    """A call's duration is measured from its own paired result."""
    path = _write_session(
        tmp_path,
        "s1",
        _start_call()
        + _bash(
            "b1",
            "sleep 1",
            "2026-11-13T10:00:10Z",
            "2026-11-13T10:00:40Z",
            "done",
        ),
    )
    session = harness.parse_session(path)
    assert session is not None
    assert len(session["bash_calls"]) == 2
    call = session["bash_calls"][1]
    assert call["duration"] == pytest.approx(30.0)
    assert call["result"] == "done"


def test_session_not_selected_without_an_implement_command(harness, tmp_path):
    """Implement sessions are the default population (baseline convention)."""
    _write_session(
        tmp_path,
        "interactive",
        _bash(
            "b1",
            "git status",
            "2026-11-13T10:00:00Z",
            "2026-11-13T10:01:00Z",
        ),
    )
    report = harness.analyse_window(
        tmp_path, since="2026-11-13T00:00:00Z", until="2026-11-14T00:00:00Z"
    )
    assert report["selection"]["sessions"] == 0
    assert report["success"] is False

    every = harness.analyse_window(
        tmp_path,
        since="2026-11-13T00:00:00Z",
        until="2026-11-14T00:00:00Z",
        all_sessions=True,
    )
    assert every["selection"]["sessions"] == 1


def test_window_filters_on_a_sessions_first_record(harness, tmp_path):
    """A session that started before ``--since`` is excluded, not split."""
    _write_session(tmp_path, "early", _start_call("2026-11-12T23:59:00Z"))
    _write_session(tmp_path, "inside", _start_call("2026-11-13T00:00:00Z"))
    report = harness.analyse_window(
        tmp_path, since="2026-11-13T00:00:00Z", until="2026-11-14T00:00:00Z"
    )
    assert report["selection"]["sessions"] == 1
    assert {row["session_id"] for row in report["sessions"]} == {"inside"}


def test_project_filter_selects_a_path_substring(harness, tmp_path):
    """Project filters are substrings of the session directory path."""
    _write_session(tmp_path, "prod", _start_call(), project="--x-AI_Hell--")
    _write_session(tmp_path, "other", _start_call(), project="--x-Other--")
    report = harness.analyse_window(
        tmp_path,
        since="2026-11-13T00:00:00Z",
        until="2026-11-14T00:00:00Z",
        projects=("AI_Hell",),
    )
    assert report["selection"]["sessions"] == 1
    assert report["selection"]["project_filters"] == ["AI_Hell"]


# ---------------------------------------------------------------------------
# Lock-wait extraction (reported, absorbed, echoed)
# ---------------------------------------------------------------------------


def test_bounded_wait_reports_its_own_sample(harness, tmp_path):
    """A post-fix finish reports ``waited_seconds`` — one wait, counted once."""
    path = _write_session(
        tmp_path,
        "reported",
        _start_call()
        + _bash(
            "f1",
            FINISH,
            "2026-11-13T10:01:00Z",
            "2026-11-13T10:02:00Z",
            '{"steps": {"main_checkout_lock": '
            '{"acquired": true, "waited_seconds": 42.5}}}',
        ),
    )
    row = harness.analyse_session(path, gate_keys=set())
    assert row["lock_waits"] == [42.5]
    assert row["lock_refusals"] == 0


def test_progress_line_is_used_when_the_report_is_absent(harness, tmp_path):
    """Older logs print the acquisition line but no ``waited_seconds``."""
    path = _write_session(
        tmp_path,
        "progress",
        _start_call()
        + _bash(
            "f1",
            FINISH,
            "2026-11-13T10:01:00Z",
            "2026-11-13T10:02:00Z",
            "Acquired the repository lock /x.lock after 12.5s",
        ),
    )
    row = harness.analyse_session(path, gate_keys=set())
    assert row["lock_waits"] == [12.5]


def test_report_and_progress_line_are_counted_once(harness, tmp_path):
    """One acquisition reporting both forms yields a single sample."""
    path = _write_session(
        tmp_path,
        "both",
        _start_call()
        + _bash(
            "f1",
            FINISH,
            "2026-11-13T10:01:00Z",
            "2026-11-13T10:02:00Z",
            "Acquired the repository lock /x.lock after 7.0s\n"
            '{"main_checkout_lock": {"acquired": true, '
            '"waited_seconds": 7.0}}',
        ),
    )
    row = harness.analyse_session(path, gate_keys=set())
    assert row["lock_waits"] == [7.0]


def test_fail_fast_refusal_is_not_a_zero_sample(harness, tmp_path):
    """A refusal reports ~0 s, which is not a wait; the episode is measured.

    The pre-fix pattern was: finish refused fail-fast, then the agent
    immediately re-ran it. The wait is the gap until the retry, never the
    refused call's own ~0 s report.
    """
    path = _write_session(
        tmp_path,
        "fail-fast",
        _start_call()
        + _bash(
            "f1",
            FINISH,
            "2026-11-13T10:01:00Z",
            "2026-11-13T10:01:30Z",
            REFUSAL + '\n{"main_checkout_lock": {"acquired": false, '
            '"waited_seconds": 0.0}}',
        )
        + _bash(
            "f2",
            FINISH,
            "2026-11-13T10:06:30Z",
            "2026-11-13T10:07:00Z",
            "Finish complete",
        ),
    )
    row = harness.analyse_session(path, gate_keys=set())
    assert row["lock_refusals"] == 1
    assert row["lock_waits"] == [330.0]


def test_fail_fast_episode_needs_an_immediate_retry(harness, tmp_path):
    """Work done between the refusal and the retry is not a lock wait."""
    path = _write_session(
        tmp_path,
        "edits-between",
        _start_call()
        + _bash(
            "f1",
            FINISH,
            "2026-11-13T10:01:00Z",
            "2026-11-13T10:01:30Z",
            REFUSAL,
        )
        + [
            _tool_call(
                "r1", "file.py", "2026-11-13T10:02:00Z", name="read"
            ),
            _tool_result("r1", "content", "2026-11-13T10:02:01Z", tool="read"),
        ]
        + _bash(
            "f2",
            FINISH,
            "2026-11-13T10:09:00Z",
            "2026-11-13T10:09:30Z",
            "Finish complete",
        ),
    )
    row = harness.analyse_session(path, gate_keys=set())
    assert row["lock_refusals"] == 1
    assert row["lock_waits"] == []


def test_retry_loop_absorbs_its_whole_duration(harness, tmp_path):
    """A scripted retry loop is one sample: the call spent absorbing a holder."""
    loop = (
        "for attempt in 1 2 3; do "
        "python3 skill/implement/scripts/implement.py finish WL-1 && break; "
        "sleep 60; done"
    )
    path = _write_session(
        tmp_path,
        "loop",
        _start_call()
        + _bash(
            "f1",
            loop,
            "2026-11-13T10:01:00Z",
            "2026-11-13T10:06:00Z",
            REFUSAL,
        ),
    )
    row = harness.analyse_session(path, gate_keys=set())
    assert row["lock_waits"] == [300.0]


def test_echoed_lock_text_from_a_grep_is_ignored(harness, tmp_path):
    """A grep/cat of a log is not a measurement (genuine-run filter)."""
    path = _write_session(
        tmp_path,
        "echo",
        _start_call()
        + _bash(
            "g1",
            "grep -n 'waited_seconds' old.log",
            "2026-11-13T10:01:00Z",
            "2026-11-13T10:01:01Z",
            REFUSAL + '\n"waited_seconds": 900.0',
        ),
    )
    row = harness.analyse_session(path, gate_keys=set())
    assert row["lock_waits"] == []
    assert row["lock_refusals"] == 0


# ---------------------------------------------------------------------------
# Rebase events, gate runs and no-op sessions
# ---------------------------------------------------------------------------


def test_rebase_events_and_gate_runs_are_counted(harness, tmp_path):
    """A losing push race prints ``non-fast-forward``; gate shas are unique."""
    push_output = (
        "! [rejected] dev -> dev (non-fast-forward)\n"
        "! [rejected] dev -> dev (non-fast-forward)\n"
        "run_tests: gate-subject pushed=abc1234\n"
        '"slot_wait_seconds": 1.2, "slot_lanes": ["full"]'
    )
    _write_session(
        tmp_path,
        "push-race",
        _start_call()
        + _bash(
            "p1",
            "git push origin HEAD:refs/heads/dev",
            "2026-11-13T10:01:00Z",
            "2026-11-13T10:02:00Z",
            push_output,
        )
        # The same gate verdict echoed by a second session must not double-count.
        + _bash(
            "p2",
            FINISH,
            "2026-11-13T10:03:00Z",
            "2026-11-13T10:04:00Z",
            push_output,
        ),
    )
    report = harness.analyse_window(
        tmp_path, since="2026-11-13T00:00:00Z", until="2026-11-14T00:00:00Z"
    )
    assert report["aggregate"]["rebase_events"] == 4
    assert report["aggregate"]["full_suite_gate_runs"] == 1


def test_no_op_session_is_flagged(harness, tmp_path):
    """A short session with no bash call is the no-op drive retry."""
    path = _write_session(
        tmp_path,
        "noop",
        [
            _record("assistant", "Loaded the skill.", "2026-11-13T10:00:00Z"),
            _record("assistant", "Nothing to do.", "2026-11-13T10:00:50Z"),
        ],
    )
    row = harness.analyse_session(path, gate_keys=set())
    assert row is not None
    assert row["no_op"] is True
    report = harness.analyse_window(
        tmp_path,
        since="2026-11-13T00:00:00Z",
        until="2026-11-14T00:00:00Z",
        all_sessions=True,
    )
    assert report["aggregate"]["no_op_sessions"] == 1


# ---------------------------------------------------------------------------
# Aggregates and verdicts
# ---------------------------------------------------------------------------


def test_aggregate_parallelism_and_peak_concurrency(harness, tmp_path):
    """Effective parallelism and peak concurrency come from the spans."""
    _write_session(
        tmp_path,
        "a",
        _start_call("2026-11-13T10:00:00Z")
        + _bash(
            "b1",
            "sleep 1",
            "2026-11-13T10:00:01Z",
            "2026-11-13T10:10:00Z",
        ),
    )
    _write_session(
        tmp_path,
        "b",
        _start_call("2026-11-13T10:05:00Z")
        + _bash(
            "b1",
            "sleep 1",
            "2026-11-13T10:05:01Z",
            "2026-11-13T10:15:00Z",
        ),
    )
    report = harness.analyse_window(
        tmp_path, since="2026-11-13T00:00:00Z", until="2026-11-14T00:00:00Z"
    )
    aggregate = report["aggregate"]
    assert aggregate["peak_concurrency"] == 2
    # 600 s + 600 s summed over a 900 s wall-clock span.
    assert aggregate["effective_parallelism"] == pytest.approx(1.33, abs=0.01)
    assert aggregate["bash_share"] > 0.9


def test_thresholds_fail_on_a_before_style_window(harness, tmp_path):
    """A 400 s absorbed wait breaks both the median and the max thresholds."""
    loop = (
        "for attempt in 1 2 3; do "
        "python3 skill/implement/scripts/implement.py finish WL-1 && break; "
        "done"
    )
    _write_session(
        tmp_path,
        "before",
        _start_call()
        + _bash(
            "f1",
            loop,
            "2026-11-13T10:01:00Z",
            "2026-11-13T10:07:40Z",
            REFUSAL,
        ),
    )
    report = harness.analyse_window(
        tmp_path,
        since="2026-11-13T00:00:00Z",
        until="2026-11-14T00:00:00Z",
        min_samples=1,
    )
    assert report["verdict"] == "fail"
    assert set(report["failures"]) == {
        "no_session_majority_lock_blocked",
        "median_lock_wait_under_60s",
        "max_lock_wait_under_300s",
    }


def test_thresholds_pass_on_an_after_style_window(harness, tmp_path):
    """Short reported waits pass the median, max and majority checks."""
    for index, wait in enumerate((5.0, 10.0, 15.0), start=1):
        _write_session(
            tmp_path,
            f"after-{index}",
            _start_call(f"2026-11-18T13:{index:02d}:00Z")
            + _bash(
                "f1",
                FINISH,
                f"2026-11-18T13:{index:02d}:10Z",
                f"2026-11-18T13:{index:02d}:50Z",
                '{"main_checkout_lock": {"acquired": true, '
                f'"waited_seconds": {wait}}}',
            ),
        )
    report = harness.analyse_window(
        tmp_path,
        since="2026-11-18T13:00:00Z",
        until="2026-11-19T00:00:00Z",
        min_samples=3,
    )
    assert report["verdict"] == "pass", report["checks"]
    assert report["aggregate"]["lock"]["median_seconds"] == 10.0
    assert report["aggregate"]["lock"]["max_seconds"] == 15.0


def test_under_sampled_window_is_inconclusive(harness, tmp_path):
    """Too few samples never reads as a hard failure."""
    _write_session(
        tmp_path,
        "thin",
        _start_call()
        + _bash(
            "f1",
            FINISH,
            "2026-11-18T13:01:00Z",
            "2026-11-18T13:02:00Z",
            '{"main_checkout_lock": {"acquired": true, '
            '"waited_seconds": 5.0}}',
        ),
    )
    report = harness.analyse_window(
        tmp_path,
        since="2026-11-18T13:00:00Z",
        until="2026-11-19T00:00:00Z",
        min_samples=3,
    )
    assert report["verdict"] == "inconclusive"
    assert report["failures"] == []


# ---------------------------------------------------------------------------
# CLI contract
# ---------------------------------------------------------------------------


def test_cli_json_output(harness, tmp_path, capsys):
    """``session --json`` emits one parseable report."""
    _write_session(tmp_path, "s1", _start_call())
    status = harness.main(
        [
            "session",
            "--sessions-root",
            str(tmp_path),
            "--since",
            "2026-11-13T00:00:00Z",
            "--until",
            "2026-11-14T00:00:00Z",
            "--json",
        ]
    )
    assert status == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["mode"] == "session"
    assert parsed["selection"]["sessions"] == 1


def test_cli_rejects_an_invalid_since(harness, tmp_path):
    """A malformed timestamp is an argparse error, not a silent empty window."""
    with pytest.raises(SystemExit):
        harness.main(
            ["session", "--sessions-root", str(tmp_path), "--since", "nope"]
        )


def test_cli_baseline_comparison(harness, tmp_path, capsys):
    """``--baseline`` renders the same metrics side by side."""
    _write_session(tmp_path, "s1", _start_call())
    base = harness.analyse_window(
        tmp_path, since="2026-11-13T00:00:00Z", until="2026-11-14T00:00:00Z"
    )
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(json.dumps(base), encoding="utf-8")
    status = harness.main(
        [
            "session",
            "--sessions-root",
            str(tmp_path),
            "--since",
            "2026-11-13T00:00:00Z",
            "--until",
            "2026-11-14T00:00:00Z",
            "--baseline",
            str(baseline_path),
            "--json",
        ]
    )
    assert status == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["comparison"]["mode"] == "comparison"
    metrics = {row["metric"] for row in parsed["comparison"]["metrics"]}
    assert {"effective_parallelism", "lock_refusals"} <= metrics


def test_cli_out_writes_the_report(harness, tmp_path, capsys):
    """``--out`` is the only write path in session mode."""
    _write_session(tmp_path, "s1", _start_call())
    target = tmp_path / "artifacts" / "report.json"
    status = harness.main(
        [
            "session",
            "--sessions-root",
            str(tmp_path),
            "--since",
            "2026-11-13T00:00:00Z",
            "--until",
            "2026-11-14T00:00:00Z",
            "--json",
            "--out",
            str(target),
        ]
    )
    assert status == 0
    capsys.readouterr()
    assert json.loads(target.read_text(encoding="utf-8"))["mode"] == "session"


def test_session_mode_is_read_only(harness, tmp_path, capsys):
    """Without ``--out`` no file in the corpus is touched."""
    path = _write_session(tmp_path, "s1", _start_call())
    before = (path.stat().st_mtime_ns, path.read_bytes())
    harness.main(
        [
            "session",
            "--sessions-root",
            str(tmp_path),
            "--since",
            "2026-11-13T00:00:00Z",
            "--until",
            "2026-11-14T00:00:00Z",
            "--json",
        ]
    )
    capsys.readouterr()
    assert (path.stat().st_mtime_ns, path.read_bytes()) == before


# ---------------------------------------------------------------------------
# Synthetic fan-out
# ---------------------------------------------------------------------------


def test_fanout_six_way_measures_every_session(harness):
    """Six concurrent finishes through the real lock are each measured."""
    report = harness.measure_fanout(
        6,
        suite_seconds=0.5,
        lock_window_seconds=0.02,
        lock_timeout_seconds=60,
    )
    assert report["mode"] == "fanout"
    assert report["parameters"]["count"] == 6
    assert len(report["sessions"]) == 6
    assert all(row["success"] for row in report["sessions"]), report["sessions"]
    assert all(row["lock_acquired"] for row in report["sessions"])
    assert report["verdict"] == "pass", report["checks"]
    assert report["aggregate"]["max_lock_wait_seconds"] < 60
    # The pre-lock phase is concurrent, so the batch is not six serial finishes.
    assert report["aggregate"]["effective_parallelism"] > 1.0


def test_fanout_checks_fail_when_a_session_is_majority_blocked(harness):
    """The majority-blocked check is not vacuous."""
    rows = [
        {
            "session": "fanout-1",
            "success": True,
            "wall_seconds": 100.0,
            "lock_wait_seconds": 90.0,
            "lock_hold_seconds": 10.0,
            "lock_share": 0.9,
        }
    ]
    checks = {check["id"]: check for check in harness._fanout_checks(rows)}
    assert checks["no_session_majority_lock_blocked"]["passed"] is False


def test_fanout_checks_fail_on_a_long_wait(harness):
    """Both the median and the max thresholds are evaluated."""
    rows = [
        {
            "session": f"fanout-{index}",
            "success": True,
            "wall_seconds": 400.0,
            "lock_wait_seconds": wait,
            "lock_hold_seconds": 1.0,
            "lock_share": wait / 400.0,
        }
        for index, wait in enumerate((120.0, 320.0, 500.0), start=1)
    ]
    checks = {check["id"]: check for check in harness._fanout_checks(rows)}
    assert checks["median_lock_wait_under_60s"]["passed"] is False
    assert checks["max_lock_wait_under_300s"]["passed"] is False


def test_fanout_rejects_a_non_positive_count(harness):
    """A zero/negative fan-out is a usage error, not an empty report."""
    with pytest.raises(ValueError):
        harness.measure_fanout(0, suite_seconds=0.0, lock_window_seconds=0.0)


# ---------------------------------------------------------------------------
# Recorded evidence
# ---------------------------------------------------------------------------


def test_recorded_measurement_artifact_is_complete():
    """The committed before/after artifact carries all three sections."""
    assert ARTIFACT.exists(), f"recorded measurement missing: {ARTIFACT}"
    artifact = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    assert {"before", "after", "fanout"} <= set(artifact)
    assert artifact["before"]["mode"] == "session"
    assert artifact["after"]["mode"] == "session"
    assert artifact["fanout"]["mode"] == "fanout"
    assert artifact["fanout"]["parameters"]["count"] == 6
    # The recorded evidence names the windows and the findings, not just numbers.
    assert artifact["before"]["selection"]["since"]
    assert artifact["after"]["selection"]["since"]
    assert artifact.get("notes")
