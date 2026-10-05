"""Unit tests for skill/shared/audit_gate_metrics.py.

Covers the read-only idle-gate / audit-queue metrics collector
(SA-0MTG5UPBR0028K50). Tests exercise the public parsing and aggregation
API against synthetic log fixtures -- they assert computed values, not
implementation details.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
_SKILLS_ROOT_FOR_TESTS = REPO_ROOT / "skill"
if str(_SKILLS_ROOT_FOR_TESTS) not in sys.path:
    sys.path.append(str(_SKILLS_ROOT_FOR_TESTS))

from shared.audit_gate_metrics import (
    BOUNDED_SATURATION_PATTERN,
    HOST_SATURATION_PATTERN,
    LEGACY_FAILFAST_PATTERN,
    DispatchEvent,
    build_report,
    classify_concurrency_verdict,
    cluster_windows,
    compute_dispatch_metrics,
    compute_queue_metrics,
    count_concurrency_verdicts,
    load_worklog_verdicts,
    main,
    parse_dispatch_log,
    parse_queue_admissions,
    render_human,
)


def _event(item_id: str, kind: str, iso: str) -> DispatchEvent:
    return DispatchEvent(
        item_id=item_id,
        kind=kind,
        timestamp=datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone.utc),
    )


# ── Dispatch log parsing ───────────────────────────────────────────────


def test_parse_dispatch_log_parses_dedupes_and_skips_noise():
    enrichment = json.dumps(
        {
            "itemId": "SA-1",
            "kind": "audit",
            "dispatchedAt": "2026-09-26T10:00:00.000Z",
            "enrichment": True,
        }
    )
    lines = [
        json.dumps(
            {"itemId": "SA-1", "kind": "audit", "dispatchedAt": "2026-09-26T10:00:00.000Z"}
        ),
        enrichment,  # same (item, kind, ts) -> duplicate, collapsed
        json.dumps({"itemId": "SA-2", "kind": "plan", "dispatchedAt": "2026-09-26T10:05:00Z"}),
        json.dumps(
            {
                "itemId": "SA-3",
                "kind": "audit",
                "entryType": "pane-close",
                "timestamp": "2026-09-26T10:06:00Z",
            }
        ),
        json.dumps(
            {
                "itemId": "SA-4",
                "kind": "implement",
                "dispatchedAt": "2026-09-26T10:07:00Z",
                "outcome": "spawn-failed",
            }
        ),
        json.dumps({"itemId": "SA-5", "kind": "intake"}),  # no timestamp -> skipped
        "{not json",
        "",
    ]
    events = parse_dispatch_log("\n".join(lines))
    assert [(event.item_id, event.kind) for event in events] == [
        ("SA-1", "audit"),
        ("SA-2", "plan"),
    ]


def test_parse_dispatch_log_orders_by_timestamp():
    events = parse_dispatch_log(
        "\n".join(
            [
                json.dumps(
                    {"itemId": "SA-B", "kind": "plan", "dispatchedAt": "2026-09-26T12:00:00Z"}
                ),
                json.dumps(
                    {"itemId": "SA-A", "kind": "audit", "dispatchedAt": "2026-09-26T09:00:00Z"}
                ),
            ]
        )
    )
    assert [event.item_id for event in events] == ["SA-A", "SA-B"]


# ── Window clustering ──────────────────────────────────────────────────


def test_cluster_windows_splits_on_gap():
    events = [
        _event("SA-1", "audit", "2026-09-26T10:00:00+00:00"),
        _event("SA-2", "plan", "2026-09-26T10:10:00+00:00"),
        _event("SA-3", "audit", "2026-09-26T11:00:00+00:00"),  # 50 min gap -> new window
    ]
    windows = cluster_windows(events, gap_seconds=1800)
    assert len(windows) == 2
    assert [len(window.events) for window in windows] == [2, 1]
    assert windows[0].duration_seconds == 600.0
    assert windows[1].duration_seconds == 0.0


def test_cluster_windows_empty_and_single():
    assert cluster_windows([]) == []
    single = cluster_windows([_event("SA-1", "audit", "2026-09-26T10:00:00+00:00")])
    assert len(single) == 1
    assert single[0].kind_counts() == {"audit": 1}


# ── Dispatch metrics ───────────────────────────────────────────────────


def test_compute_dispatch_metrics_per_day_counts():
    events = [
        _event("SA-1", "audit", "2026-09-26T10:00:00+00:00"),
        _event("SA-2", "audit", "2026-09-26T10:10:00+00:00"),
        _event("SA-3", "plan", "2026-09-26T11:00:00+00:00"),
        _event("SA-4", "audit", "2026-09-27T09:00:00+00:00"),
    ]
    metrics = compute_dispatch_metrics(events, gap_seconds=1800)
    assert metrics["total_dispatches"] == 4
    assert metrics["audit_dispatches"] == 3
    assert metrics["total_windows"] == 3
    assert metrics["kind_totals"] == {"audit": 3, "plan": 1}
    day1 = metrics["per_day"]["2026-09-26"]
    assert day1["windows"] == 2
    assert day1["dispatches"] == 3
    assert day1["audit_dispatches"] == 2
    assert day1["dispatches_per_window"] == 1.5
    assert metrics["per_day"]["2026-09-27"]["windows"] == 1


# ── Queue admissions ───────────────────────────────────────────────────


def test_parse_queue_admissions_parses_and_skips_malformed():
    text = (
        "Audit slot acquired: queued_at=2026-09-26T18:19:25 priority=medium "
        "queue_position=1 dequeued_at=2026-09-26T18:19:25 wait_seconds=0.01 "
        "ticket=audit:A:1:0\n"
        "Audit slot acquired: queued_at=2026-09-26T18:29:25 priority=high "
        "queue_position=n/a dequeued_at=2026-09-26T18:29:25 wait_seconds=42.5 "
        "ticket=audit:B:1:1\n"
        "some unrelated log line\n"
        "Audit slot acquired: queued_at=not-a-date priority=low queue_position=2 "
        "dequeued_at=x wait_seconds=1.0 ticket=audit:C:1:2"
    )
    admissions = parse_queue_admissions(text)
    assert len(admissions) == 2
    assert admissions[0].priority == "medium"
    assert admissions[0].position == 1
    assert admissions[1].priority == "high"
    assert admissions[1].position is None
    assert admissions[1].wait_seconds == 42.5


def test_compute_queue_metrics_by_priority_and_slow_count():
    admissions = parse_queue_admissions(
        "\n".join(
            [
                f"Audit slot acquired: queued_at=2026-09-26T18:00:0{i} priority=critical "
                f"queue_position=1 dequeued_at=x wait_seconds={wait} ticket=audit:A:1:{i}"
                for i, wait in enumerate([0.0, 10.0, 90.0])
            ]
        )
        + "\n"
        + "Audit slot acquired: queued_at=2026-09-26T19:00:00 priority=low "
        "queue_position=1 dequeued_at=x wait_seconds=0.5 ticket=audit:B:1:9"
    )
    metrics = compute_queue_metrics(admissions, slow_wait_seconds=30.0)
    assert metrics["total_admissions"] == 4
    assert metrics["slow_admissions"] == 1
    assert metrics["max_wait_seconds"] == 90.0
    critical = metrics["by_priority"]["critical"]
    assert critical["count"] == 3
    assert critical["max_wait_seconds"] == 90.0
    assert critical["slow_admissions"] == 1
    assert metrics["by_priority"]["low"]["count"] == 1
    assert metrics["per_day"]["2026-09-26"]["count"] == 4


# ── Verdict classification ─────────────────────────────────────────────


def test_classify_concurrency_verdict_detects_categories():
    assert classify_concurrency_verdict("no slot free within 0.0s (max_workers=1)") == {
        "legacy_failfast"
    }
    assert classify_concurrency_verdict(
        "Audit concurrency limit reached: concurrency queue 'audit' saturated: no slot within 90s"
    ) == {"bounded_saturation"}
    assert classify_concurrency_verdict(
        "host-wide audit concurrency limit reached (semaphore 'audit-host' busy)"
    ) == {"host_saturation"}
    assert classify_concurrency_verdict("") == set()


def test_count_concurrency_verdicts_counts_items_once():
    raw_outputs = [
        f"{LEGACY_FAILFAST_PATTERN} repeated {LEGACY_FAILFAST_PATTERN}",
        f"{BOUNDED_SATURATION_PATTERN}",
        f"{BOUNDED_SATURATION_PATTERN} and {HOST_SATURATION_PATTERN}",
        "clean audit output",
    ]
    counts = count_concurrency_verdicts(raw_outputs)
    assert counts == {"legacy_failfast": 1, "bounded_saturation": 2, "host_saturation": 1}


# ── Source loading / report assembly ───────────────────────────────────


def test_load_worklog_verdicts_reads_sqlite(tmp_path):
    db = tmp_path / "worklog.db"
    connection = sqlite3.connect(db)
    connection.execute("CREATE TABLE audit_results (raw_output TEXT)")
    connection.executemany(
        "INSERT INTO audit_results VALUES (?)",
        [
            (f"x {BOUNDED_SATURATION_PATTERN} y",),
            (f"{LEGACY_FAILFAST_PATTERN}",),
            ("clean",),
        ],
    )
    connection.commit()
    connection.close()

    totals, used = load_worklog_verdicts([db])
    assert totals["bounded_saturation"] == 1
    assert totals["legacy_failfast"] == 1
    assert totals["host_saturation"] == 0
    assert used == [str(db)]


def test_build_report_from_synthetic_logs(tmp_path):
    dispatch_log = tmp_path / "downtime-dispatches.log"
    dispatch_log.write_text(
        "\n".join(
            [
                json.dumps(
                    {"itemId": "SA-1", "kind": "audit", "dispatchedAt": "2026-09-26T10:00:00Z"}
                ),
                json.dumps(
                    {"itemId": "SA-2", "kind": "audit", "dispatchedAt": "2026-09-26T10:05:00Z"}
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    audit_log = tmp_path / "audit.log"
    audit_log.write_text(
        "Audit slot acquired: queued_at=2026-09-26T10:06:00 priority=high "
        "queue_position=1 dequeued_at=x wait_seconds=5.0 ticket=audit:SA-2:1:0\n",
        encoding="utf-8",
    )
    db = tmp_path / "worklog.db"
    connection = sqlite3.connect(db)
    connection.execute("CREATE TABLE audit_results (raw_output TEXT)")
    connection.execute("INSERT INTO audit_results VALUES (?)", (BOUNDED_SATURATION_PATTERN,))
    connection.commit()
    connection.close()

    report = build_report(
        dispatch_logs=[dispatch_log],
        audit_logs=[audit_log],
        worklog_dbs=[db],
        gap_seconds=1800,
    )
    payload = report.to_dict()
    assert payload["dispatch"]["total_dispatches"] == 2
    assert payload["dispatch"]["audit_dispatches"] == 2
    assert payload["queue"]["total_admissions"] == 1
    assert payload["verdicts"]["by_worklog_item"]["bounded_saturation"] == 1
    assert payload["sources"]["dispatch_logs"] == [str(dispatch_log)]


def test_render_human_contains_key_sections(tmp_path):
    dispatch_log = tmp_path / "downtime-dispatches.log"
    dispatch_log.write_text(
        json.dumps({"itemId": "SA-1", "kind": "audit", "dispatchedAt": "2026-09-26T10:00:00Z"})
        + "\n",
        encoding="utf-8",
    )
    report = build_report(dispatch_logs=[dispatch_log], gap_seconds=1800)
    text = render_human(report)
    assert "Gate windows" in text
    assert "Audit queue admissions" in text
    assert "Concurrency-limit verdicts" in text
    assert "2026-09-26" in text


def test_main_writes_json_report(tmp_path, capsys):
    dispatch_log = tmp_path / "downtime-dispatches.log"
    dispatch_log.write_text(
        json.dumps({"itemId": "SA-1", "kind": "audit", "dispatchedAt": "2026-09-26T10:00:00Z"})
        + "\n",
        encoding="utf-8",
    )
    out = tmp_path / "report.json"
    rc = main(["--dispatch-log", str(dispatch_log), "--out", str(out)])
    assert rc == 0
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["dispatch"]["total_dispatches"] == 1
    assert "SA-1" not in json.dumps(written)  # aggregates are id-free
    capsys.readouterr()  # drain stdout


@pytest.mark.parametrize("kind", ["audit", "plan", "intake", "implement"])
def test_compute_dispatch_metrics_kind_totals(kind):
    events = [_event("SA-X", kind, "2026-09-26T10:00:00+00:00")]
    metrics = compute_dispatch_metrics(events, gap_seconds=1800)
    assert metrics["kind_totals"] == {kind: 1}
