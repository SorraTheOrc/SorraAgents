"""Tests for the shared Code Freeze marker helper (SA-0MSBU4OBU005WJNB).

Contract (fail-open, per work item AC):

- AC: `is_code_freeze_active(project_root)` reads
  `<project-root>/.worklog/code-freeze.json`.
- Fail-open: a missing marker file is NOT frozen.
- Fail-open: a corrupt (unparseable) marker is NOT frozen.
- Fail-open: a marker with `active: false` is NOT frozen.
- A marker with `active: true` IS frozen (conservative: treat presence as
  frozen, rely on robust ship-side trap/finally cleanup).
- The marker format matches the cross-repo contract (WL-0MSBU4KMA004PKSR):
  `{ "active": true, "reason": "ship release in progress",
     "startedAt": "<ISO>", "pid": <pid> }`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
CODE_FREEZE_MODULE = "skill.shared.code_freeze"

pytest.importorskip(CODE_FREEZE_MODULE)

from skill.shared.code_freeze import (
    code_freeze_marker_path,
    is_code_freeze_active,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    """A temporary project root with a .worklog directory."""
    root = tmp_path / "proj"
    (root / ".worklog").mkdir(parents=True)
    return root


def _write_marker(project_root: Path, data: dict | None) -> Path:
    marker = project_root / ".worklog" / "code-freeze.json"
    marker.write_text(json.dumps(data) if data is not None else "{corrupt")
    return marker


# ---------------------------------------------------------------------------
# Marker path resolution
# ---------------------------------------------------------------------------


class TestMarkerPath:
    def test_resolves_to_worklog_code_freeze_json(self, project_root: Path):
        """The marker path is <project-root>/.worklog/code-freeze.json."""
        assert code_freeze_marker_path(project_root) == (
            project_root / ".worklog" / "code-freeze.json"
        )

    def test_defaults_to_cwd(self, tmp_path: Path, monkeypatch):
        """With no argument, the marker path resolves under cwd."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".worklog").mkdir()
        assert code_freeze_marker_path() == tmp_path / ".worklog" / "code-freeze.json"


# ---------------------------------------------------------------------------
# Fail-open behaviour
# ---------------------------------------------------------------------------


class TestFailOpen:
    def test_missing_marker_is_not_frozen(self, project_root: Path):
        """No marker file → not frozen (fail-open)."""
        assert is_code_freeze_active(project_root) is False

    def test_corrupt_marker_is_not_frozen(self, project_root: Path):
        """Unparseable marker file → not frozen (fail-open)."""
        _write_marker(project_root, None)
        assert is_code_freeze_active(project_root) is False

    def test_non_object_marker_is_not_frozen(self, project_root: Path):
        """Marker that is valid JSON but not an object → not frozen."""
        _write_marker(project_root, {"active": True})  # valid object first
        marker = project_root / ".worklog" / "code-freeze.json"
        marker.write_text('"just a string"')
        assert is_code_freeze_active(project_root) is False

    def test_inactive_marker_is_not_frozen(self, project_root: Path):
        """Marker with active:false → not frozen."""
        _write_marker(
            project_root,
            {
                "active": False,
                "reason": "no release",
                "startedAt": "2026-08-03T00:00:00Z",
                "pid": 1234,
            },
        )
        assert is_code_freeze_active(project_root) is False


# ---------------------------------------------------------------------------
# Active marker
# ---------------------------------------------------------------------------


class TestActiveMarker:
    def test_active_marker_is_frozen(self, project_root: Path):
        """Marker with active:true → frozen."""
        _write_marker(
            project_root,
            {
                "active": True,
                "reason": "ship release in progress",
                "startedAt": "2026-08-03T00:00:00Z",
                "pid": 1234,
            },
        )
        assert is_code_freeze_active(project_root) is True

    def test_active_marker_missing_optional_fields_is_frozen(self, project_root: Path):
        """Only `active: true` is required; missing optional fields still freeze."""
        _write_marker(project_root, {"active": True})
        assert is_code_freeze_active(project_root) is True

    def test_contract_marker_format_round_trip(self, project_root: Path):
        """The exact cross-repo contract marker is detected as frozen."""
        _write_marker(
            project_root,
            {
                "active": True,
                "reason": "ship release in progress",
                "startedAt": "2026-08-03T00:00:00Z",
                "pid": 1234,
            },
        )
        assert is_code_freeze_active(project_root) is True
