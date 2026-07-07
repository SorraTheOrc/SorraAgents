#!/usr/bin/env python3
"""Shared autoplan decision logic — delegation wrapper for backward compatibility.

**This is a delegation wrapper.** The canonical copy of this module now lives at
``skill/plan/plan_helpers.py``. All logic, CLI entry points, and public API
are defined there.

This file exists so that existing callers (Ralph, tests, CLI invocations) continue
to work without changes. It loads the canonical module and re-exports everything
into the ``command.plan_helpers`` namespace.

The delegation uses ``exec(compile(..., "<canonical_path>", "exec"), globals())``
to run the canonical module's code in this module's namespace. This ensures:
1. All function definitions exist in ``command.plan_helpers`` (patch targets work)
2. ``__file__`` is set to the canonical path so internal path resolution works
3. CLI entry points (``plan-if-needed``, ``check-effort-risk``) work correctly
4. The ``if __name__ == "__main__"`` guard works for both import and CLI modes

See ``skill/plan/plan_helpers.py`` for the canonical source.
"""

from pathlib import Path

_canonical_path = str(
    Path(__file__).resolve().parent.parent / "skill" / "plan" / "plan_helpers.py"
)

# Point __file__ to the canonical path so internal path resolution
# (e.g., run_effort_and_risk finding the effort-and-risk script) resolves
# correctly relative to the canonical module's location.
_globals = globals()
_globals["__file__"] = _canonical_path

with open(_canonical_path, encoding="utf-8") as _f:
    exec(compile(_f.read(), _canonical_path, "exec"), _globals)
