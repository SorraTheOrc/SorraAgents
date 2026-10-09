"""Unit tests for ``run_tests.guarded_run_all`` — guard-ownership contract.

Covers SA-0MV0EERBB007QA4V (child of SA-0MUH1MJL6003767Q): the canonical
guard-owning programmatic API. ``guarded_run_all`` is the single source of
truth for outer live-repo guard ownership; the CLI (``main``) and
``implement.py`` delegate to it.

Contract under test:

- When ``LIVE_REPO_GUARD_ACTIVE`` is unset the function OWNS the checkout:
  it snapshots before, sets the marker for the duration of ``run_all`` (so
  the inner conftest guard stands down), clears it in a ``finally`` block,
  and fails the run on a detected mutation.
- When the marker is already set an outer guard owns the checkout: the
  function stands down — it does not snapshot, does not touch the marker,
  and does not run the mutation detector.
- The result is the ``run_all`` dict shape, plus ``live_repo_mutation`` when
  the owning guard detected a mutation.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_RUNNER_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_RUNNER_DIR) not in sys.path:
    sys.path.insert(0, str(_RUNNER_DIR))

import run_tests
from run_tests import guarded_run_all

MARKER_ENV = "LIVE_REPO_GUARD_ACTIVE"


def _fake_result(**overrides):
    base = {
        "success": True,
        "suites": {},
        "failures": [],
        "notices": [],
        "scope": "full",
        "type": "full",
        "resolved_scopes": ["full"],
    }
    base.update(overrides)
    return base


@pytest.fixture(autouse=True)
def _clean_marker():
    """Ensure the recursion marker never leaks between tests."""
    original = os.environ.pop(MARKER_ENV, None)
    yield
    if original is None:
        os.environ.pop(MARKER_ENV, None)
    else:
        os.environ[MARKER_ENV] = original


class TestGuardOwnership:
    def test_arms_guard_and_clears_marker_when_unset(self, monkeypatch):
        """Marker unset → own the checkout: snapshot, set marker, run, clear."""
        observed: dict[str, object] = {}

        def fake_snapshot(root):
            observed["snapshot_root"] = root
            return {"git": True, "refs": {}}

        def fake_run_all(**kwargs):
            observed["marker_during_run"] = os.environ.get(MARKER_ENV)
            observed["run_all_kwargs"] = kwargs
            return _fake_result()

        def fake_detect(root, snapshot_before):
            observed["detect_called"] = (root, snapshot_before)

        monkeypatch.delenv(MARKER_ENV, raising=False)
        monkeypatch.setattr(run_tests, "snapshot_repo_state", fake_snapshot)
        monkeypatch.setattr(run_tests, "run_all", fake_run_all)
        monkeypatch.setattr(run_tests, "_detect_live_repo_mutation", fake_detect)

        result = guarded_run_all(REPO_ROOT, timeout=42, scope="changed")

        assert observed["marker_during_run"] == "1"
        assert MARKER_ENV not in os.environ
        assert observed["snapshot_root"] == REPO_ROOT.resolve()
        assert observed["detect_called"][1] == {"git": True, "refs": {}}
        assert result["success"] is True
        assert "live_repo_mutation" not in result
        # Run-all receives the resolved project root, not the raw argument.
        assert observed["run_all_kwargs"]["cwd"] == REPO_ROOT.resolve()
        assert observed["run_all_kwargs"]["timeout"] == 42
        assert observed["run_all_kwargs"]["scope"] == "changed"

    def test_clears_marker_even_when_run_all_raises(self, monkeypatch):
        """The marker is cleared in a ``finally`` even on an exception."""
        monkeypatch.delenv(MARKER_ENV, raising=False)
        monkeypatch.setattr(run_tests, "snapshot_repo_state", lambda root: {"git": True})

        def boom(**kwargs):
            raise RuntimeError("suite exploded")

        monkeypatch.setattr(run_tests, "run_all", boom)
        monkeypatch.setattr(
            run_tests, "_detect_live_repo_mutation", lambda root, snapshot: None
        )

        with pytest.raises(RuntimeError):
            guarded_run_all(REPO_ROOT)
        assert MARKER_ENV not in os.environ

    def test_stands_down_when_marker_preset(self, monkeypatch):
        """Marker preset → an outer guard owns; do not snapshot or detect."""
        monkeypatch.setenv(MARKER_ENV, "1")
        observed: dict[str, object] = {}

        def fake_snapshot(root):  # pragma: no cover - must not be called
            observed["snapshot_called"] = True
            return {"git": True}

        def fake_run_all(**kwargs):
            observed["marker_during_run"] = os.environ.get(MARKER_ENV)
            return _fake_result(scope="full")

        def fake_detect(root, snapshot_before):  # pragma: no cover
            observed["detect_called"] = True
            return "should not happen"

        monkeypatch.setattr(run_tests, "snapshot_repo_state", fake_snapshot)
        monkeypatch.setattr(run_tests, "run_all", fake_run_all)
        monkeypatch.setattr(run_tests, "_detect_live_repo_mutation", fake_detect)

        result = guarded_run_all(REPO_ROOT)

        assert observed["marker_during_run"] == "1"
        assert "snapshot_called" not in observed
        assert "detect_called" not in observed
        # The outer guard's marker is preserved, not cleared.
        assert os.environ.get(MARKER_ENV) == "1"
        assert "live_repo_mutation" not in result
        assert result["success"] is True

    def test_mutation_is_merged_and_fails_the_run(self, monkeypatch):
        """A detected mutation marks the result failed with the diff attached."""
        monkeypatch.delenv(MARKER_ENV, raising=False)
        monkeypatch.setattr(run_tests, "snapshot_repo_state", lambda root: {"git": True})
        monkeypatch.setattr(run_tests, "run_all", lambda **kwargs: _fake_result())
        monkeypatch.setattr(
            run_tests,
            "_detect_live_repo_mutation",
            lambda root, snapshot_before: "live-repo mutation detected: refs moved",
        )

        result = guarded_run_all(REPO_ROOT)

        assert result["success"] is False
        assert result["live_repo_mutation"] == "live-repo mutation detected: refs moved"
        # The original run_all shape is preserved (not replaced).
        assert result["suites"] == {}
        assert MARKER_ENV not in os.environ

    def test_forwards_all_run_all_kwargs(self, monkeypatch):
        """Every kwarg is forwarded to run_all unchanged."""
        monkeypatch.delenv(MARKER_ENV, raising=False)
        monkeypatch.setattr(run_tests, "snapshot_repo_state", lambda root: {"git": True})
        monkeypatch.setattr(run_tests, "_detect_live_repo_mutation", lambda r, s: None)
        captured: dict[str, object] = {}

        def fake_run_all(**kwargs):
            captured.update(kwargs)
            return _fake_result()

        monkeypatch.setattr(run_tests, "run_all", fake_run_all)

        guarded_run_all(
            REPO_ROOT,
            timeout=99,
            suites=("pytest",),
            use_cache=False,
            force=True,
            no_cache=True,
            scope="changed",
            base_ref="origin/main",
            commands=["pytest tests/x.py"],
            test_type="unit",
            group="audit",
        )

        assert captured["suites"] == ("pytest",)
        assert captured["timeout"] == 99
        assert captured["use_cache"] is False
        assert captured["force"] is True
        assert captured["no_cache"] is True
        assert captured["scope"] == "changed"
        assert captured["base_ref"] == "origin/main"
        assert captured["commands"] == ["pytest tests/x.py"]
        assert captured["test_type"] == "unit"
        assert captured["group"] == "audit"
