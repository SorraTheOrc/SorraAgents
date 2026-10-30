"""Fixture isolation for the implement skill's own tests.

When this package's suite is itself run by ``implement.py finish`` the gate
exports ``GIT_INDEX_FILE`` pointing at *its* temporary index
(SA-0MVQHVGFR006W8LG). Every test here builds a hermetic fixture repository
and drives the gate itself, so inheriting that value would redirect fixture
``git`` commands (and ``test_cache`` fingerprints, which intentionally keep
``GIT_INDEX_FILE`` — F3, SA-0MVQHV9Y3001RNNW) at a foreign repository's index.

Each finish-gate test that needs an ambient index sets it itself
(``would_be_commit_index`` exports it and restores the previous value), so the
ambient value is removed for every test in this package.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_ambient_git_index_file(monkeypatch):
    """Remove an ambient ``GIT_INDEX_FILE`` for the duration of each test."""
    monkeypatch.delenv("GIT_INDEX_FILE", raising=False)
