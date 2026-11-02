"""Fixture isolation for the test skill's own suite.

``implement.py finish`` runs this suite with ``GIT_INDEX_FILE`` pointing at
the gate's temporary index (SA-0MVQHVGFR006W8LG). The tests here build
throwaway repositories and drive ``run_tests.py`` in-place; inheriting that
value lets a fixture ``git add`` stage into the gate's index (invalidating the
gate's verdict) and makes in-process git helpers read a foreign index
(SA-0MVQHVHXI007PQAB).

Each test that needs an ambient index sets it explicitly for the child process
it spawns, so removing it here cannot weaken the pass-through contract pinned
by ``tests/test_git_index_file_env.py``.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_ambient_git_index_file(monkeypatch):
    """Remove an ambient ``GIT_INDEX_FILE`` for the duration of each test."""
    monkeypatch.delenv("GIT_INDEX_FILE", raising=False)
