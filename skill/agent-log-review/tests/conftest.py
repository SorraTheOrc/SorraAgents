"""Shared fixtures for the agent-log-review skill test suite.

The skill directory is named ``agent-log-review`` (hyphenated) so it is not a
valid Python package name. Tests therefore add the skill's ``scripts/``
directory to ``sys.path`` and import the module directly as
``agent_log_review`` (mirroring the ``effort-and-risk`` skill convention).
"""
from __future__ import annotations

import sys
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
_SCRIPTS_DIR = _TESTS_DIR.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
