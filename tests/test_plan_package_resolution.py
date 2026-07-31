"""Regression test for the root ``plan`` package resolution (SA-0MS8WCP3E006NPA3).

When the full test suite is collected, pytest puts ``skill/`` on ``sys.path``
(e.g. while collecting ``skill/planall/tests`` and ``skill/implementall/tests``).
Because ``skill/plan/__init__.py`` is a regular package while the root ``plan/``
directory is a namespace portion, ``import plan`` previously resolved to
``skill/plan`` (regular packages beat namespace portions per PEP 420), which
lacks ``detection.py`` / ``wl_adapter.py``. This broke collection of
``tests/test_detection_module.py``, ``tests/test_wl_adapter_delete_comment.py``
and ``tests/test_wl_dep_commands.py``.

The fix adds an empty ``plan/__init__.py`` at the repository root so root
``plan/`` is a regular package and wins over ``skill/plan`` when the repo root
precedes ``skill/`` on ``sys.path``.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import plan


def test_plan_resolves_to_repo_root_not_skill_plan():
    """`plan` must be the root package, not skill/plan."""
    assert plan.__file__ is None or "skill/plan" not in str(plan.__file__)
    root_plan = Path(plan.__path__[0]).resolve()
    assert root_plan == (ROOT / "plan").resolve(), (
        f"plan resolved to {root_plan}, expected {ROOT / 'plan'}"
    )


def test_plan_detection_importable():
    """plan.detection (used by tests/test_detection_module.py) must import."""
    from plan import detection

    assert detection is not None


def test_plan_wl_adapter_importable():
    """plan.wl_adapter (used by test_wl_dep_commands.py) must import."""
    from plan.wl_adapter import WLAdapter

    assert WLAdapter is not None
