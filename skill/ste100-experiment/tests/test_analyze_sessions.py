"""Tests for the STE100 session-log analyser (SA-0MU9VSL250092LTY).

Every test drives the public API (``parse_session_file`` / ``analyze`` /
``main``) against synthetic JSONL fixtures written under ``tmp_path`` — no
live session logs, no clock, no randomness. The analyser's whole purpose is a
deterministic measurement, so the assertions cover observable behaviour:
duration extraction, token/cost aggregation, window attribution, statistics,
project/model breakdowns, and the CLI's two output formats.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import analyze_sessions as a

BASE = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def iso(minutes: float) -> str:
    """ISO-8601 timestamp *minutes* after the fixed test base."""
    return (BASE + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def assistant(minutes: float, usage: dict | None, model: str = "plan") -> dict:
    message: dict = {"role": "assistant", "model": model}
    if usage is not None:
        message["usage"] = usage
    return {"type": "message", "timestamp": iso(minutes), "message": message}


def usage_record(
    input_: int = 100,
    output: int = 20,
    cache_read: int = 1000,
    cache_write: int = 5,
    total_tokens: int | None = 1125,
    cost: object = 0.0,
) -> dict:
    record: dict = {
        "input": input_,
        "output": output,
        "cacheRead": cache_read,
        "cacheWrite": cache_write,
    }
    if total_tokens is not None:
        record["totalTokens"] = total_tokens
    if cost is not None:
        record["cost"] = cost
    return record


def write_session(
    root: Path,
    name: str,
    start_minutes: float,
    events: list[dict],
    cwd: str = "/home/rgardler/projects/ProjA",
) -> Path:
    header = {"type": "session", "version": 3, "id": name, "timestamp": iso(start_minutes), "cwd": cwd}
    path = root / name / f"{name}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(header), *(json.dumps(event) for event in events)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def only_session(report: dict) -> dict:
    assert report["session_count"] == 1
    return report["sessions"][0]


# ---------------------------------------------------------------------------
# Per-session extraction
# ---------------------------------------------------------------------------


def test_parse_extracts_duration_and_token_sums(tmp_path: Path) -> None:
    write_session(
        tmp_path,
        "s1",
        0,
        [
            assistant(5, usage_record()),
            assistant(10, usage_record(input_=50, output=10, cache_read=500, cache_write=0, total_tokens=560)),
        ],
    )

    session = a.parse_session_file(tmp_path / "s1" / "s1.jsonl")

    assert session.duration_minutes == pytest.approx(10.0)
    assert session.assistant_messages == 2
    assert session.tokens["input"] == 150
    assert session.tokens["output"] == 30
    assert session.tokens["cacheRead"] == 1500
    assert session.tokens["cacheWrite"] == 5
    assert session.tokens["totalTokens"] == 1685
    assert session.start is not None and session.end is not None
    assert session.end - session.start == pytest.approx(600.0)


def test_total_tokens_falls_back_to_base_sum_when_absent(tmp_path: Path) -> None:
    write_session(
        tmp_path,
        "s1",
        0,
        [assistant(1, usage_record(input_=10, output=5, cache_read=2, cache_write=1, total_tokens=None))],
    )

    session = a.parse_session_file(tmp_path / "s1" / "s1.jsonl")

    assert session.tokens["totalTokens"] == 18


def test_missing_usage_is_zero_but_session_still_counted(tmp_path: Path) -> None:
    write_session(tmp_path, "s1", 0, [assistant(3, None)])

    session = a.parse_session_file(tmp_path / "s1" / "s1.jsonl")

    assert session.duration_minutes == pytest.approx(3.0)
    assert session.assistant_messages == 1
    assert session.tokens["totalTokens"] == 0


def test_malformed_lines_are_counted_and_skipped(tmp_path: Path) -> None:
    path = write_session(tmp_path, "s1", 0, [assistant(2, usage_record())])
    with path.open("a", encoding="utf-8") as handle:
        handle.write("{not valid json\n")

    session = a.parse_session_file(path)

    assert session.malformed_lines == 1
    assert session.assistant_messages == 1


def test_cost_accepts_object_and_scalar_records(tmp_path: Path) -> None:
    write_session(
        tmp_path,
        "obj",
        0,
        [
            assistant(
                1,
                usage_record(
                    cost={"input": 0.001, "output": 0.002, "cacheRead": 0.0, "cacheWrite": 0.0, "total": 0.003}
                ),
            )
        ],
    )
    write_session(tmp_path, "scalar", 100, [assistant(101, usage_record(cost=0.5))])

    report = a.analyze(tmp_path)

    assert report["cost"]["total"] == pytest.approx(0.503)
    assert report["cost"]["input"] == pytest.approx(0.001)
    assert report["cost"]["output"] == pytest.approx(0.002)


# ---------------------------------------------------------------------------
# Window attribution
# ---------------------------------------------------------------------------


def test_window_start_inclusive_and_end_exclusive(tmp_path: Path) -> None:
    write_session(tmp_path, "before", -30, [assistant(-29, usage_record())])
    write_session(tmp_path, "at_start", 0, [assistant(1, usage_record())])
    write_session(tmp_path, "at_end", 60, [assistant(61, usage_record())])

    report = a.analyze(tmp_path, a.parse_timestamp(iso(0)), a.parse_timestamp(iso(60)))

    assert [s["id"] for s in report["sessions"]] == ["at_start"]


def test_spanning_session_is_attributed_by_start(tmp_path: Path) -> None:
    # Session runs 00:00 -> 02:00. Window [00:00, 01:00) contains its start.
    write_session(tmp_path, "spanning", 0, [assistant(120, usage_record())])

    including = a.analyze(tmp_path, a.parse_timestamp(iso(0)), a.parse_timestamp(iso(60)))
    excluding = a.analyze(tmp_path, a.parse_timestamp(iso(30)), a.parse_timestamp(iso(120)))

    assert including["session_count"] == 1
    assert excluding["session_count"] == 0


def test_session_without_start_excluded_from_bounded_window(tmp_path: Path) -> None:
    path = tmp_path / "noheader" / "noheader.jsonl"
    path.parent.mkdir(parents=True)
    # No header and no timestamps anywhere: the session has no start time.
    path.write_text(
        json.dumps({"type": "message", "message": {"role": "assistant", "usage": usage_record()}})
        + "\n",
        encoding="utf-8",
    )

    bounded = a.analyze(tmp_path, a.parse_timestamp(iso(0)), a.parse_timestamp(iso(60)))
    unbounded = a.analyze(tmp_path)

    assert bounded["session_count"] == 0
    assert unbounded["session_count"] == 1


# ---------------------------------------------------------------------------
# Statistics and breakdowns
# ---------------------------------------------------------------------------


def test_mean_median_and_sum_over_window(tmp_path: Path) -> None:
    for index, (start, duration, total) in enumerate([(0, 10, 100), (100, 20, 200), (200, 30, 300)]):
        write_session(
            tmp_path,
            f"s{index}",
            start,
            [assistant(start + duration, usage_record(input_=total, output=0, cache_read=0, cache_write=0, total_tokens=total))],
        )

    report = a.analyze(tmp_path)

    assert report["session_count"] == 3
    assert report["duration_minutes"] == {
        "count": 3,
        "mean": 20.0,
        "median": 20.0,
        "min": 10.0,
        "max": 30.0,
        "sum": 60.0,
    }
    assert report["total_tokens"]["mean"] == 200.0
    assert report["total_tokens"]["median"] == 200.0
    assert report["total_tokens"]["sum"] == 600


def test_empty_window_reports_nulls_and_zeros(tmp_path: Path) -> None:
    write_session(tmp_path, "s1", 0, [assistant(5, usage_record())])

    report = a.analyze(tmp_path, a.parse_timestamp(iso(1000)), a.parse_timestamp(iso(2000)))

    assert report["session_count"] == 0
    assert report["duration_minutes"]["mean"] is None
    assert report["total_tokens"]["sum"] == 0
    assert report["projects"] == {}
    assert report["models"] == {}
    assert report["sessions"] == []


def test_per_project_breakdown(tmp_path: Path) -> None:
    write_session(tmp_path, "a1", 0, [assistant(5, usage_record(total_tokens=100))], cwd="/home/rgardler/projects/Alpha")
    write_session(tmp_path, "a2", 100, [assistant(105, usage_record(total_tokens=100))], cwd="/home/rgardler/projects/Alpha")
    write_session(tmp_path, "b1", 200, [assistant(205, usage_record(total_tokens=50))], cwd="/home/rgardler/projects/Beta")

    report = a.analyze(tmp_path)

    assert sorted(report["projects"]) == ["Alpha", "Beta"]
    assert report["projects"]["Alpha"]["session_count"] == 2
    assert report["projects"]["Alpha"]["total_tokens"]["sum"] == 200
    assert report["projects"]["Beta"]["session_count"] == 1


def test_worktree_cwd_maps_to_parent_project(tmp_path: Path) -> None:
    write_session(
        tmp_path,
        "wt",
        0,
        [assistant(5, usage_record())],
        cwd="/home/rgardler/projects/ProjA/.worklog/worktrees/wl-xyz",
    )

    session = a.parse_session_file(tmp_path / "wt" / "wt.jsonl")

    assert session.project == "ProjA"


def test_per_model_breakdown_uses_message_model(tmp_path: Path) -> None:
    write_session(
        tmp_path,
        "s1",
        0,
        [
            assistant(1, usage_record(input_=10, output=1, cache_read=0, cache_write=0, total_tokens=11), model="plan"),
            assistant(2, usage_record(input_=20, output=2, cache_read=0, cache_write=0, total_tokens=22), model="fast"),
        ],
    )

    report = a.analyze(tmp_path)

    assert sorted(report["models"]) == ["fast", "plan"]
    assert report["models"]["fast"]["tokens"]["totalTokens"] == 22
    assert report["models"]["plan"]["tokens"]["totalTokens"] == 11
    assert report["models"]["plan"]["session_count"] == 1


def test_analysis_is_deterministic(tmp_path: Path) -> None:
    write_session(tmp_path, "s1", 0, [assistant(5, usage_record())])
    write_session(tmp_path, "s2", 50, [assistant(55, usage_record(total_tokens=7))])

    first = a.analyze(tmp_path)
    second = a.analyze(tmp_path)

    assert first == second


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_json_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_session(tmp_path, "s1", 0, [assistant(5, usage_record())])

    exit_code = a.main(["--sessions-dir", str(tmp_path), "--json"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["session_count"] == 1
    assert only_session(payload)["id"] == "s1"


def test_cli_text_output_renders_sections(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_session(tmp_path, "s1", 0, [assistant(5, usage_record())])

    exit_code = a.main(["--sessions-dir", str(tmp_path)])

    assert exit_code == 0
    text = capsys.readouterr().out
    assert "Session log analysis" in text
    assert "By project" in text
    assert "By model" in text


def test_cli_output_file_is_written(tmp_path: Path) -> None:
    write_session(tmp_path, "s1", 0, [assistant(5, usage_record())])
    destination = tmp_path / "out" / "report.json"

    exit_code = a.main(["--sessions-dir", str(tmp_path), "--json", "--output", str(destination)])

    assert exit_code == 0
    assert json.loads(destination.read_text(encoding="utf-8"))["session_count"] == 1


def test_cli_missing_sessions_dir_returns_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = a.main(["--sessions-dir", str(tmp_path / "does-not-exist")])

    assert exit_code == 2
    assert "not found" in capsys.readouterr().err


def test_cli_invalid_window_returns_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_session(tmp_path, "s1", 0, [assistant(5, usage_record())])

    exit_code = a.main(["--sessions-dir", str(tmp_path), "--window-start", "not-a-date"])

    assert exit_code == 2
    assert "invalid --window-start" in capsys.readouterr().err
