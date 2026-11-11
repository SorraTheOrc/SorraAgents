"""Behavioural tests for the slot-wait re-measurement harness.

Covers **SA-0MW7C69KU008ZT5K** (AC1, AC2, AC3, AC4, AC5, AC6):
``scripts/measure_slot_wait.py`` parses the pi session-log corpus, attributes
each wait to one of the four call-site categories and reports
count / p50 / p90 / max without double counting or echo false positives.

The parser is exercised two ways:

* :func:`parse_block` directly on synthetic run_tests.py / finish / gate text,
  covering the AC5 fixture cases — escaped JSON-in-JSON, missing keys, lane
  attribution and double-count avoidance.
* :func:`measure` / :func:`main` against synthetic ``*.jsonl`` session files
  written to ``tmp_path`` (with real assistant ``toolCall`` / ``toolResult``
  pairs), covering the genuine-run command filter, the ``--since`` window, the
  parent thresholds and the too-few-samples labelling.

No test reads the live session store and no test runs a suite.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "measure_slot_wait.py"


def _load_module():
    """Load the harness by file location (``scripts`` is not import-safe)."""
    assert SCRIPT.exists(), f"harness missing: {SCRIPT}"
    spec = importlib.util.spec_from_file_location(
        "_measure_slot_wait", SCRIPT
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


def _tool_call(call_id: str, command: str, timestamp: str) -> dict:
    return {
        "type": "message",
        "timestamp": timestamp,
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "toolCall",
                    "id": call_id,
                    "name": "bash",
                    "arguments": {"command": command},
                }
            ],
        },
    }


def _tool_result(call_id: str, text: str, timestamp: str) -> dict:
    return {
        "type": "message",
        "timestamp": timestamp,
        "message": {
            "role": "toolResult",
            "toolCallId": call_id,
            "toolName": "bash",
            "content": [{"type": "text", "text": text}],
        },
    }


def _write_session(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


def _bare_run_tests_block(
    full: list[float], fast: list[float], *, emit_aggregate: bool = True
) -> str:
    """Build a run_tests.py ``--json`` block with per-suite slot samples."""
    lines = ['{', '  "success": true,', '  "suites": {']
    total = 0.0
    for index, seconds in enumerate(full):
        total += seconds
        lines.append(f'    "full{index}": {{')
        lines.append(f'      "slot_wait_seconds": {seconds},')
        lines.append('      "slot_lane": "full"')
        lines.append("    },")
    for index, seconds in enumerate(fast):
        total += seconds
        lines.append(f'    "fast{index}": {{')
        lines.append(f'      "slot_wait_seconds": {seconds},')
        lines.append('      "slot_lane": "fast"')
        lines.append("    },")
    lines.append("  },")
    if emit_aggregate:
        lanes = sorted(
            {*('"full"' for _ in full), *('"fast"' for _ in fast)}
        )
        lanes = [lane for lane in ("full", "fast") if f'"{lane}"' in lanes]
        lines.append(f'  "slot_wait_seconds": {total},')
        lines.append('  "slot_lanes": [')
        for lane in lanes:
            lines.append(f'    "{lane}",')
        lines.append("  ],")
    lines.append('  "notice_codes": []')
    lines.append("}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# parse_block — AC5 fixture cases
# ---------------------------------------------------------------------------


def test_parses_per_suite_samples_with_lane_attribution(harness):
    """Per-suite samples keep their lane; the aggregate is not double counted."""
    block = _bare_run_tests_block(full=[5.0], fast=[7.0, 0.0])
    parsed = harness.parse_block(block)

    assert parsed["run_tests"] == [("full", 5.0), ("fast", 7.0), ("fast", 0.0)]
    # The aggregate (sum of per-suite) must not appear as an extra sample.
    assert parsed["finish"] == []
    assert parsed["gate"] == []


def test_aggregate_only_single_lane_is_a_fallback_sample(harness):
    """A truncated result with only an aggregate names one lane -> one sample."""
    block = (
        '"slot_wait_seconds": 122.687, "slot_lanes": [\n    "full"\n  ]'
    )
    assert harness.parse_block(block)["run_tests"] == [("full", 122.687)]


def test_multi_lane_aggregate_without_per_suite_is_not_split(harness):
    """An aggregate naming both lanes cannot be split and is skipped."""
    block = '"slot_wait_seconds": 9.0, "slot_lanes": ["full", "fast"]'
    assert harness.parse_block(block)["run_tests"] == []


def test_escaped_json_in_json_is_parsed(harness):
    """Escaped quotes from nested JSON are un-escaped before matching."""
    block = (
        '{\\"slot_wait_seconds\\": 3.5, \\"slot_lane\\": \\"fast\\"}'
    )
    assert harness.parse_block(block)["run_tests"] == [("fast", 3.5)]


def test_missing_lane_key_is_tolerated(harness):
    """A ``slot_wait_seconds`` with no lane is ignored, never an error."""
    parsed = harness.parse_block('"slot_wait_seconds": 12.0, "cached": false')
    assert parsed["run_tests"] == []


def test_finish_telemetry_is_extracted(harness):
    """The finish slot-wait telemetry line becomes one finish sample."""
    block = (
        "INFO: Implement run_tests: test slot acquired — "
        "queued_at=1792453431.513 wait_seconds=134.851\n"
        '{ "phase": "finish" }'
    )
    assert harness.parse_block(block)["finish"] == [("1792453431.513", 134.851)]


def test_finish_block_does_not_also_count_its_inner_run_tests_json(harness):
    """Double-count avoidance: a finish block's run_tests JSON is not (a)."""
    block = (
        "Implement run_tests: test slot acquired - queued_at=10.5 "
        "wait_seconds=42.0\n"
        + _bare_run_tests_block(full=[42.0], fast=[])
    )
    parsed = harness.parse_block(block)

    assert parsed["finish"] == [("10.5", 42.0)]
    assert parsed["run_tests"] == []


def test_gate_block_attributes_aggregate_to_the_gate(harness):
    """A gate-provenance block's aggregate is the dev-push gate wait."""
    block = (
        "pre-push: full-suite gate for dev/main push — running run_tests.py "
        "--scope full --strict-git-env --gate-commit abc123\n"
        "run_tests: gate-subject pushed=abc123 checkout=in-place "
        "fingerprint_kind=worktree\n"
        '"slot_wait_seconds": 1.25, "slot_lanes": [\n    "full"\n  ]'
    )
    parsed = harness.parse_block(block)

    assert parsed["gate"] == [("abc123", 1.25, ("full",))]
    assert parsed["run_tests"] == []


def test_queued_notice_requires_run_text_structure(harness):
    """A notice inside genuine run text counts; a bare echo does not."""
    genuine = (
        "all: FAIL [changed scope] [type full] [fast lane] (vitest run x)\n"
        "  queued 263.4s for a fast-lane test slot "
        "(wait, not execution — see elapsed in the facts below)\n"
        "  facts: elapsed=3.81s, slot_wait=263.37s, lane=fast"
    )
    assert harness.parse_block(genuine)["queued"] == [("fast", 263.4)]

    echo = "queued 263.4s for a fast-lane test slot (wait, not execution)"
    assert harness.parse_block(echo)["queued"] == []


# ---------------------------------------------------------------------------
# measure() — genuine-run filter, dedup, window, thresholds
# ---------------------------------------------------------------------------


def test_run_source_command_is_counted_and_echo_is_excluded(harness, tmp_path):
    """Only a result from a test-slot consumer's own command is measured."""
    session = tmp_path / "sessions" / "--demo--" / "s1.jsonl"
    block = _bare_run_tests_block(full=[2.0], fast=[])
    _write_session(
        session,
        [
            _tool_call(
                "run",
                "cd /w && python3 skill/test/scripts/run_tests.py --json",
                "2026-11-05T10:00:00Z",
            ),
            _tool_result("run", block, "2026-11-05T10:00:05Z"),
            # An analysis grep echoes a much larger sample that must not count.
            _tool_call(
                "grep",
                "grep -n slot_wait_seconds /w/app.log",
                "2026-11-05T10:01:00Z",
            ),
            _tool_result(
                "grep",
                '"slot_wait_seconds": 999.0, "slot_lanes": [\n "full"\n]',
                "2026-11-05T10:01:01Z",
            ),
        ],
    )

    report = harness.measure(tmp_path / "sessions")
    full = next(
        row for row in report["categories"] if row["category"] == "run_tests_full"
    )
    assert full["calls"] == 1
    assert full["max_seconds"] == 2.0


def test_finish_samples_are_deduplicated_by_queued_at(harness, tmp_path):
    """A wait_for_job tail re-print does not create a second finish sample."""
    line = (
        "INFO: Implement run_tests: test slot acquired — "
        "queued_at=100.0 wait_seconds=12.5\n"
    )
    session = tmp_path / "sessions" / "--demo--" / "s1.jsonl"
    _write_session(
        session,
        [
            _tool_call("tail", "wait_for_job.py --pid 1", "2026-11-05T10:00:00Z"),
            _tool_result("tail", line, "2026-11-05T10:00:05Z"),
            _tool_call("fin", "implement.py finish WL-1", "2026-11-05T10:01:00Z"),
            _tool_result("fin", line, "2026-11-05T10:01:05Z"),
        ],
    )

    report = harness.measure(tmp_path / "sessions")
    finish = next(
        row for row in report["categories"] if row["category"] == "implement_finish"
    )
    assert finish["calls"] == 1
    assert finish["max_seconds"] == 12.5


def test_since_window_excludes_older_records(harness, tmp_path):
    """Records before ``--since`` are ignored even in the same file."""
    block = _bare_run_tests_block(full=[1.0], fast=[])
    session = tmp_path / "sessions" / "--demo--" / "s1.jsonl"
    _write_session(
        session,
        [
            _tool_call(
                "old", "run_tests.py", "2026-10-30T10:00:00Z"
            ),
            _tool_result("old", block, "2026-10-30T10:00:05Z"),
            _tool_call(
                "new", "run_tests.py", "2026-11-05T10:00:00Z"
            ),
            _tool_result("new", block, "2026-11-05T10:00:05Z"),
        ],
    )

    report = harness.measure(
        tmp_path / "sessions", since="2026-10-31T23:21:00Z"
    )
    full = next(
        row for row in report["categories"] if row["category"] == "run_tests_full"
    )
    assert full["calls"] == 1


def test_too_few_samples_is_labelled_not_percentiled(harness, tmp_path):
    """A thin category is labelled with a re-check date, max still reported."""
    block = (
        "INFO: Implement run_tests: test slot acquired — "
        "queued_at=1.0 wait_seconds=27.7\n"
    )
    session = tmp_path / "sessions" / "--demo--" / "s1.jsonl"
    _write_session(
        session,
        [
            _tool_call("fin", "implement.py finish WL-1", "2026-11-05T10:00:00Z"),
            _tool_result("fin", block, "2026-11-05T10:00:05Z"),
        ],
    )

    report = harness.measure(tmp_path / "sessions", min_samples=10)
    finish = next(
        row for row in report["categories"] if row["category"] == "implement_finish"
    )
    assert finish["reliable"] is False
    assert finish["p50_seconds"] is None
    assert finish["p90_seconds"] is None
    assert finish["max_seconds"] == 27.7
    assert "no regression observed in 1 samples" in finish["note"]
    assert finish["recheck_after"] is not None


def test_thresholds_pass_when_within_bounds(harness, tmp_path):
    """A clean corpus passes all three parent thresholds."""
    block = _bare_run_tests_block(full=[1.0], fast=[2.0])
    session = tmp_path / "sessions" / "--demo--" / "s1.jsonl"
    _write_session(
        session,
        [
            _tool_call("run", "run_tests.py", "2026-11-05T10:00:00Z"),
            _tool_result("run", block, "2026-11-05T10:00:05Z"),
        ],
    )

    report = harness.measure(
        tmp_path / "sessions", since="2026-10-31T23:21:00Z", min_samples=1
    )
    checks = {check["id"]: check for check in report["checks"]}
    assert checks["no_run_over_120s"]["passed"] is True
    assert checks["partial_p90_under_30s"]["passed"] is True
    assert checks["no_queued_notice_30s"]["passed"] is True
    assert report["verdict"] == "pass"


def test_thresholds_fail_on_a_large_full_wait(harness, tmp_path):
    """A >120 s full-lane run fails the no-run-over-120 s check."""
    block = _bare_run_tests_block(full=[193.0], fast=[])
    session = tmp_path / "sessions" / "--demo--" / "s1.jsonl"
    _write_session(
        session,
        [
            _tool_call("run", "run_tests.py", "2026-11-05T10:00:00Z"),
            _tool_result("run", block, "2026-11-05T10:00:05Z"),
        ],
    )

    report = harness.measure(
        tmp_path / "sessions", since="2026-10-31T23:21:00Z", min_samples=1
    )
    checks = {check["id"]: check for check in report["checks"]}
    assert checks["no_run_over_120s"]["passed"] is False
    assert report["verdict"] == "fail"
    assert "no_run_over_120s" in report["failures"]


def test_thresholds_fail_on_a_large_queued_notice(harness, tmp_path):
    """A ``queued N s`` notice >= 30 s fails the notice check."""
    block = (
        "all: FAIL [changed scope] [type full] [fast lane] (vitest run x)\n"
        "  queued 263.4s for a fast-lane test slot "
        "(wait, not execution — see elapsed in the facts below)\n"
        "  facts: elapsed=3.81s, slot_wait=263.37s, lane=fast"
    )
    session = tmp_path / "sessions" / "--demo--" / "s1.jsonl"
    _write_session(
        session,
        [
            _tool_call("run", "run_tests.py --json", "2026-11-05T10:00:00Z"),
            _tool_result("run", block, "2026-11-05T10:00:05Z"),
        ],
    )

    report = harness.measure(
        tmp_path / "sessions", since="2026-10-31T23:21:00Z", min_samples=1
    )
    checks = {check["id"]: check for check in report["checks"]}
    assert checks["no_queued_notice_30s"]["passed"] is False
    assert report["verdict"] == "fail"


# ---------------------------------------------------------------------------
# CLI + read-only guarantee
# ---------------------------------------------------------------------------


def test_cli_json_output(harness, tmp_path, capsys):
    """``--json`` emits a deterministic machine-readable report."""
    block = _bare_run_tests_block(full=[1.0], fast=[2.0])
    session = tmp_path / "sessions" / "--demo--" / "s1.jsonl"
    _write_session(
        session,
        [
            _tool_call("run", "run_tests.py", "2026-11-05T10:00:00Z"),
            _tool_result("run", block, "2026-11-05T10:00:05Z"),
        ],
    )

    exit_code = harness.main(
        [
            "--sessions-root",
            str(tmp_path / "sessions"),
            "--since",
            "2026-10-31T23:21:00Z",
            "--min-samples",
            "1",
            "--json",
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["success"] is True
    assert payload["window_start"] == "2026-10-31T23:21:00+00:00"
    assert payload["window_end"] is not None
    assert {row["category"] for row in payload["categories"]} == {
        "run_tests_full",
        "run_tests_fast",
        "implement_finish",
        "dev_push_gate",
    }
    assert exit_code == 0


def test_cli_reports_empty_corpus_loudly(harness, tmp_path, capsys):
    """A corpus with no samples exits non-zero with actionable advice."""
    empty = tmp_path / "sessions"
    empty.mkdir()

    exit_code = harness.main(
        ["--sessions-root", str(empty), "--since", "2026-10-31T23:21:00Z"]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "no slot-wait samples found" in captured.err
    assert "--sessions-root" in captured.err


def test_cli_rejects_an_invalid_since(harness, tmp_path, capsys):
    """A malformed ``--since`` is a usage error, not a silent default."""
    with pytest.raises(SystemExit):
        harness.main(
            ["--sessions-root", str(tmp_path), "--since", "not-a-timestamp"]
        )
    assert "invalid --since timestamp" in capsys.readouterr().err


def test_harness_is_read_only(harness, tmp_path):
    """Measuring never writes to, renames or deletes the session corpus."""
    block = _bare_run_tests_block(full=[1.0], fast=[2.0])
    session = tmp_path / "sessions" / "--demo--" / "s1.jsonl"
    _write_session(
        session,
        [
            _tool_call("run", "run_tests.py", "2026-11-05T10:00:00Z"),
            _tool_result("run", block, "2026-11-05T10:00:05Z"),
        ],
    )

    def snapshot() -> dict[str, tuple[int, int]]:
        return {
            str(path.relative_to(tmp_path)): (
                path.stat().st_size,
                path.stat().st_mtime_ns,
            )
            for path in sorted(tmp_path.rglob("*"))
            if path.is_file()
        }

    before = snapshot()
    harness.measure(tmp_path / "sessions")
    harness.main(
        ["--sessions-root", str(tmp_path / "sessions"), "--json"]
    )
    assert snapshot() == before
