"""Path setup for the triage skill's own tests.

The triage tests import ``triage.scripts.check_or_create`` (the ``skill``
directory is a package root). The repo-root ``conftest.py`` is loaded by file
location and deliberately does not mutate ``sys.path``, and the top-level
``tests/conftest.py`` only applies to the root ``tests/`` subtree, so the
skills root must be prepended here.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SKILLS_ROOT = _REPO_ROOT / "skill"
for _path in (str(_SKILLS_ROOT), str(_REPO_ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)
