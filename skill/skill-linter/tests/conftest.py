"""Shared fixtures for the skill-linter test suite.

``skill/skill-linter`` is not a valid Python package name (hyphenated), so
tests add the skill's ``scripts/`` directory to ``sys.path`` and import the
module directly as ``lint_skills`` (mirroring the ``agent-log-review``
convention).
"""

from __future__ import annotations

import sys
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
_SCRIPTS_DIR = _TESTS_DIR.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
