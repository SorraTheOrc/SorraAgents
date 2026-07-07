#!/usr/bin/env python3
"""
_shared.py

Shared constants and utility functions for the effort-and-risk skill.

This module consolidates duplicated code from calc_effort.py,
calc_effort_with_risk.py, and orchestrate_estimate.py into a single
source of truth.

Exports:
    TSHIRT_MAP: dict[str, str] - Maps t-shirt size codes to full-text labels
    DEFAULT_THRESHOLDS: dict[str, dict] - Fallback threshold definitions
    compute_omp(data: dict) -> tuple[float, float, float]
    level_from_score(score: int | float) -> str
    pick_tshirt(hours: float, thresholds: dict | None = None) -> str
"""

# Map t-shirt size codes to human-readable labels
TSHIRT_MAP: dict[str, str] = {
    "XS": "Extra Small",
    "S": "Small",
    "M": "Medium",
    "L": "Large",
    "XL": "Extra Large",
}

# Fallback thresholds when references/t-shirt_sizes.json cannot be loaded.
# Bounds are min-inclusive and max-exclusive (e.g., S covers 4.00h up to but
# not including 24.00h). XL has no upper bound.
DEFAULT_THRESHOLDS: dict[str, dict] = {
    "XS": {"min": 0, "max": 4},
    "S": {"min": 4, "max": 24},
    "M": {"min": 24, "max": 80},
    "L": {"min": 80, "max": 240},
    "XL": {"min": 240, "max": None},
}


def pick_tshirt(hours: float, thresholds: dict | None = None) -> str:
    """Return a t-shirt size label for the given hours.

    Args:
        hours: The estimated hours to size.
        thresholds: A dict mapping size codes to {"min": float, "max": float | None}.
                    If None, DEFAULT_THRESHOLDS is used.

    Returns:
        A t-shirt size code string (e.g., "XS", "S", "M", "L", "XL").
    """
    if thresholds is None:
        thresholds = DEFAULT_THRESHOLDS

    for size, bounds in thresholds.items():
        mn = bounds.get("min", 0)
        mx = bounds.get("max")
        if mx is None:
            if hours >= mn:
                return size
        else:
            if hours >= mn and hours < mx:
                return size
    return "XS"


def compute_omp(data: dict) -> tuple[float, float, float]:
    """Compute optimistic, most-likely, and pessimistic totals.

    If the data dict contains a non-empty 'items' list, the values are
    aggregated across all items. Otherwise, individual 'o', 'm', 'p' keys
    are used (defaulting to 0 if absent).

    Args:
        data: A dict with either individual 'o', 'm', 'p' keys, or an 'items'
              list of dicts each containing 'o', 'm', 'p'.

    Returns:
        A tuple of (o, m, p) as floats.
    """
    items = data.get("items")
    if isinstance(items, list) and items:
        o_sum = sum(float(i.get("o", 0)) for i in items)
        m_sum = sum(float(i.get("m", 0)) for i in items)
        p_sum = sum(float(i.get("p", 0)) for i in items)
        return o_sum, m_sum, p_sum
    return (
        float(data.get("o", 0)),
        float(data.get("m", 0)),
        float(data.get("p", 0)),
    )


def level_from_score(score: int | float) -> str:
    """Return a risk level label for the given numeric score.

    Maps a risk score (typically 1-25) to a human-readable level:

    - score <= 5  -> "Low"
    - score <= 12 -> "Medium"
    - score <= 19 -> "High"
    - score > 19  -> "Critical"

    Args:
        score: A numeric risk score (int or float).

    Returns:
        A string label: "Low", "Medium", "High", or "Critical".
    """
    if score <= 5:
        return "Low"
    if score <= 12:
        return "Medium"
    if score <= 19:
        return "High"
    return "Critical"
