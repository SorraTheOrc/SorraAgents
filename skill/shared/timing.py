#!/usr/bin/env python3
"""Shared timing utility for skills.

Provides a ``Timer`` class (context manager) that records per-step and
nested-step elapsed wall-clock time using ``time.monotonic()``.

Usage::

    from shared.timing import Timer

    with Timer("root") as root:
        with Timer("step_a") as a:
            do_work_a()
        with Timer("step_b") as b:
            do_work_b()
        # root.elapsed now equals a.elapsed + b.elapsed
        print(root.render())          # human-readable
        print(json.dumps(root.to_dict()))  # JSON-serializable

The timer tracks nested named steps with wall-clock elapsed time, supports
JSON serialization, and renders both human-readable (table/Markdown) and
JSON formats. Percentages of individual steps sum to ~100% of total time.
Sub-second precision (2 decimal places) is used for human output.

Overhead is negligible (< ~1ms per step) — only ``time.monotonic()`` calls
and list/dict appends inside context managers.

Public API
----------
- ``Timer(name)`` — context manager; ``name`` is the step label.
- ``Timer.render()`` — human-readable table report.
- ``Timer.to_dict()`` — JSON-serializable dict.
"""  # noqa: EXE001

from __future__ import annotations

import json
import threading
import time
from typing import Any

# Thread-local stack of active timers for automatic parent resolution.
_local = threading.local()


def _active_stack() -> list[Timer]:
    """Return the thread-local stack of active timers."""
    if not hasattr(_local, "stack"):
        _local.stack: list[Timer] = []
    return _local.stack


class Timer:
    """Context manager that records per-step and nested-step elapsed time.

    Args:
        name: The step label (e.g. ``"step_a"``).

    Example::

        with Timer("setup") as setup:
            with Timer("init_db") as db:
                init_database()
            with Timer("init_cache") as cache:
                init_cache()
            # setup.elapsed == db.elapsed + cache.elapsed
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self.parent: Timer | None = None
        self.start_time: float = 0.0
        self.elapsed: float = 0.0
        self.nested_steps: list[Timer] = []

    def __enter__(self) -> Timer:
        """Start timing; automatically link to parent from the active stack."""
        return self.start()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,  # noqa: PYI036
    ) -> bool:
        """Stop timing. Return False to propagate any exception."""
        self.stop()
        return False  # do not suppress exceptions

    def start(self) -> Timer:
        """Start timing manually (equivalent to entering the context manager).

        Records the wall-clock start time and links the timer into the
        active thread-local stack so nested timers roll up into the parent.
        """
        self.start_time = time.monotonic()
        stack = _active_stack()
        if stack:
            self.parent = stack[-1]
            self.parent.nested_steps.append(self)
        stack.append(self)
        return self

    def stop(self) -> float:
        """Stop timing manually and return elapsed seconds.

        Equivalent to exiting the context manager. Idempotent: a second
        ``stop()`` is a no-op.
        """
        if self.start_time == 0.0:
            return self.elapsed
        self.elapsed = time.monotonic() - self.start_time
        self.start_time = 0.0
        stack = _active_stack()
        # Pop from stack (should be the current timer)
        if stack and stack[-1] is self:
            stack.pop()
        return self.elapsed

    @property
    def total_time(self) -> float:
        """Total elapsed time for this timer (including nested children).

        For leaf timers this equals ``self.elapsed``; for parent timers
        it is the sum of all nested step elapsed times.
        """
        if self.nested_steps:
            return sum(s.total_time for s in self.nested_steps)
        return self.elapsed

    @property
    def percentage(self) -> float:
        """Percentage of total time this step represents, relative to root.

        Returns 100.0 for the root timer itself even when no measurable
        time elapsed (a step is always 100% of itself); 0.0 for children
        of a zero-total root (undefined).
        """
        root = self._find_root()
        if root is None:
            return 0.0
        total = root.total_time
        if total == 0.0:
            return 100.0 if root is self else 0.0
        return (self.total_time / total) * 100.0

    def _find_root(self) -> Timer | None:
        """Walk up to find the root timer."""
        current = self
        while current.parent is not None:
            current = current.parent
        return current

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def render(self) -> str:
        """Render a human-readable table report.

        Returns a string with columns: step name, elapsed seconds (2 dp),
        percentage of total, and total time (including nested children).

        When the timer has no nested steps, the timer's own name is shown
        in the table. When it has children, the children are listed and the
        root timer's name appears as a header.
        """
        lines: list[str] = []
        lines.append("Timing Report")
        lines.append("=" * 70)
        lines.append(f"{'Step':<30} {'Elapsed':>10} {'%':>6} {'Total':>10}")
        lines.append("-" * 70)
        root = self._find_root()
        root_total = root.total_time if root else self.total_time

        if self.nested_steps:
            # Show root timer name as a header line when it has nested steps
            lines.append(f"{self.name}")
            self._render_tree(lines, 0, root_total)
        else:
            # No children — show the timer's own row
            lines.append(
                f"{self.name:<28} {self.total_time:>10.2f} "
                f"{self.percentage:>5.1f}% {self.total_time:>10.2f}"
            )

        lines.append("-" * 70)
        lines.append(f"{'Total':<30} {root_total:>10.2f} {'100.0':>6} {root_total:>10.2f}")
        lines.append("=" * 70)
        return "\n".join(lines)

    def _render_tree(
        self, lines: list[str], indent: int, root_total: float
    ) -> None:
        """Recursively render nested steps as a table tree."""
        prefix = "  " * indent
        for step in self.nested_steps:
            pct = (step.total_time / root_total * 100.0) if root_total > 0 else 0.0
            lines.append(
                f"{prefix}{step.name:<28} {step.total_time:>10.2f} "
                f"{pct:>5.1f}% {step.total_time:>10.2f}"
            )
            if step.nested_steps:
                step._render_tree(lines, indent + 1, root_total)

    # ------------------------------------------------------------------
    # JSON serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict representation.

        Structure::

            {
                "name": "root",
                "elapsed": 1.234,
                "total_time": 1.234,
                "percentage": 100.0,
                "nested_steps": [
                    {
                        "name": "step_a",
                        "elapsed": 0.500,
                        "total_time": 0.500,
                        "percentage": 40.5,
                        "nested_steps": []
                    },
                    ...
                ]
            }
        """
        return {
            "name": self.name,
            "elapsed": round(self.elapsed, 3),
            "total_time": round(self.total_time, 3),
            "percentage": round(self.percentage, 1),
            "nested_steps": [
                step.to_dict() for step in self.nested_steps
            ],
        }

    def to_json(self) -> str:
        """Return a JSON string representation."""
        return json.dumps(self.to_dict(), indent=2)
