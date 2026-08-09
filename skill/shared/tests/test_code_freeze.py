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
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
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


def _dead_pid() -> int:
    """Return a pid that is guaranteed no longer alive on this host."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.01)"])
    proc.wait(timeout=10)
    return proc.pid


def _recent_iso() -> str:
    """An ISO timestamp "now", well inside the stale grace period."""
    return datetime.now(timezone.utc).isoformat()


def _old_iso() -> str:
    """An ISO timestamp well beyond the stale grace period."""
    return (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()


# ---------------------------------------------------------------------------
# Pid liveness (stale-marker auto-expiry, SA-0MSDX3EYZ005SGIK)
# ---------------------------------------------------------------------------


class TestPidLiveness:
    def test_active_marker_with_live_pid_is_frozen(self, project_root: Path):
        """active:true + live pid → frozen (the release process is running)."""
        _write_marker(
            project_root,
            {
                "active": True,
                "reason": "ship release in progress",
                "startedAt": _recent_iso(),
                "pid": os.getpid(),
            },
        )
        assert is_code_freeze_active(project_root) is True

    def test_active_marker_without_pid_is_frozen(self, project_root: Path):
        """active:true without a pid → frozen (cannot verify liveness)."""
        _write_marker(
            project_root,
            {"active": True, "reason": "ship release in progress"},
        )
        assert is_code_freeze_active(project_root) is True

    def test_dead_pid_older_than_grace_is_stale_not_frozen(self, project_root: Path):
        """A marker whose recording pid is dead and older than the grace
        period is stale — it must NOT block implementation forever."""
        _write_marker(
            project_root,
            {
                "active": True,
                "reason": "ship release in progress",
                "startedAt": _old_iso(),
                "pid": _dead_pid(),
            },
        )
        assert is_code_freeze_active(project_root) is False

    def test_dead_pid_within_grace_is_frozen(self, project_root: Path):
        """A recently-started release whose pid just died stays frozen within
        the grace window (conservative — the merge child may still be running)."""
        _write_marker(
            project_root,
            {
                "active": True,
                "reason": "ship release in progress",
                "startedAt": _recent_iso(),
                "pid": _dead_pid(),
            },
        )
        assert is_code_freeze_active(project_root) is True

    def test_dead_pid_with_unparseable_started_at_stays_frozen(self, project_root: Path):
        """If startedAt cannot be parsed, keep the freeze (fail-safe)."""
        _write_marker(
            project_root,
            {
                "active": True,
                "reason": "ship release in progress",
                "startedAt": "not-a-timestamp",
                "pid": _dead_pid(),
            },
        )
        assert is_code_freeze_active(project_root) is True

    def test_missing_pid_field_stays_frozen(self, project_root: Path):
        """Marker written without the optional pid field (older writer) still
        freezes — liveness cannot be verified, so keep the conservative default."""
        _write_marker(
            project_root,
            {
                "active": True,
                "reason": "ship release in progress",
                "startedAt": _old_iso(),
            },
        )
        assert is_code_freeze_active(project_root) is True


# ---------------------------------------------------------------------------
# Active marker
# ---------------------------------------------------------------------------


class TestActiveMarker:
    def test_active_marker_is_frozen(self, project_root: Path):
        """Marker with active:true + live pid → frozen."""
        _write_marker(
            project_root,
            {
                "active": True,
                "reason": "ship release in progress",
                "startedAt": _recent_iso(),
                "pid": os.getpid(),
            },
        )
        assert is_code_freeze_active(project_root) is True

    def test_active_marker_missing_optional_fields_is_frozen(self, project_root: Path):
        """Only `active: true` is required; missing optional fields still freeze."""
        _write_marker(project_root, {"active": True})
        assert is_code_freeze_active(project_root) is True

    def test_contract_marker_format_round_trip(self, project_root: Path):
        """The exact cross-repo contract marker (live pid) is detected as frozen."""
        _write_marker(
            project_root,
            {
                "active": True,
                "reason": "ship release in progress",
                "startedAt": _recent_iso(),
                "pid": os.getpid(),
            },
        )
        assert is_code_freeze_active(project_root) is True
