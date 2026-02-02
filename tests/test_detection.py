import datetime
import pytest


def select_blocker(items):
    """Utility: items is list of dicts with keys: id, priority, createdAt (ISO str)
    priority order: critical > high > medium > low
    Returns the selected blocker id.
    """
    order = {"critical": 3, "high": 2, "medium": 1, "low": 0}

    # sort by priority desc, then createdAt asc
    def key(i):
        return (-order.get(i.get("priority", "medium"), 1), i.get("createdAt"))

    items_sorted = sorted(items, key=key)
    return items_sorted[0]["id"]


def test_select_blocker_priority_then_createdat():
    items = [
        {"id": "A", "priority": "high", "createdAt": "2026-01-02T00:00:00Z"},
        {"id": "B", "priority": "high", "createdAt": "2026-01-01T00:00:00Z"},
        {"id": "C", "priority": "medium", "createdAt": "2026-01-01T00:00:00Z"},
    ]
    assert select_blocker(items) == "B"


def test_select_blocker_priority_wins_over_createdat():
    items = [
        {"id": "A", "priority": "critical", "createdAt": "2026-01-05T00:00:00Z"},
        {"id": "B", "priority": "high", "createdAt": "2026-01-01T00:00:00Z"},
    ]
    assert select_blocker(items) == "A"
