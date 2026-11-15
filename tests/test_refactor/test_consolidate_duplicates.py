"""Tests for the one-off duplicate consolidation tool.

Covers the acceptance criteria of SA-0MWE1ELZO004J1DY:

- ``group_by_identity`` — pure grouping of non-terminal Refactor items by
  identity, oldest-open canonical, no worklog mutation (the F1 contract in
  ``test_dedupe_identity.py`` covers the marker-carrying case end-to-end).
- Legacy (pre-marker) item identity derivation, so the historical duplicate
  backlog — the reason this tool exists — is actually consolidated.
- Worktree-prefix and absolute-path normalisation against the main checkout
  root (parent AC4).
- CLI dry-run (default): groups listed, ZERO worklog mutations.
- ``--apply``: canonical description gets the unioned affected lines, each
  duplicate gets a ``superseded by`` comment and is closed; audit-family
  twins are reported and NEVER auto-closed; re-running is idempotent.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from refactor.scripts import consolidate_duplicates as cd

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _item(
    item_id: str,
    key: str,
    created_at: str,
    *,
    status: str = "open",
    lines: str = "12",
    description: str | None = None,
) -> dict[str, Any]:
    """A marker-carrying refactor item (the modern format)."""
    return {
        "id": item_id,
        "title": f"Refactor: finding in {key.split(':', 1)[1]}",
        "description": description or (
            f"Refactor-Finding-Key: {key}\n"
            f"Refactor-Affected-Lines: {lines}\n"
        ),
        "status": status,
        "issueType": "task",
        "tags": ["Refactor"],
        "createdAt": created_at,
    }


def _legacy_item(
    item_id: str,
    code: str,
    file: str,
    created_at: str,
    *,
    status: str = "open",
) -> dict[str, Any]:
    """A legacy (pre-marker) code-quality style item."""
    return {
        "id": item_id,
        "title": f"[MEDIUM] {file}:12 — message ({code})",
        "description": (
            "## Code quality finding\n"
            f"- **Severity**: medium\n"
            f"- **File**: {file}\n"
            "- **Line**: 12\n"
            "- **Message**: something\n"
            f"- **Linter**: ruff\n"
            f"- **Code**: {code}\n"
            "\nDiscovered during automated code quality review."
        ),
        "status": status,
        "issueType": "task",
        "tags": ["Refactor"],
        "createdAt": created_at,
    }


# ---------------------------------------------------------------------------
# Identity derivation and normalisation (AC4, legacy formats)
# ---------------------------------------------------------------------------


class TestIdentityDerivation:
    def test_marker_key_wins(self):
        """A marker-carrying item uses its marker identity."""
        item = _item("SA-0M", "S112:skill/x.py", "2026-11-01T00:00:00Z")
        assert cd._item_identity(item) == "S112:skill/x.py"

    def test_legacy_code_quality_item_derives_identity(self, monkeypatch):
        """A legacy code-quality item yields the same identity shape."""
        monkeypatch.setattr(cd, "_main_root", lambda: "/repo")
        item = _legacy_item("SA-0L", "S112", "/repo/skill/x.py", "2026-11-01T00:00:00Z")
        assert cd._item_identity(item) == "S112:skill/x.py"

    def test_worktree_prefix_is_stripped(self):
        """AC4: worktree-recorded and main-checkout paths collapse to one key."""
        assert cd._strip_worktree_prefix(
            "/repo/.worklog/worktrees/wl-ABC-1/skill/x.py"
        ) == "skill/x.py"

    def test_normalise_key_relativises_against_main_root(self, monkeypatch):
        """Absolute legacy paths are made relative to the MAIN checkout root."""
        monkeypatch.setattr(cd, "_main_root", lambda: "/repo")
        key = cd._normalise_key("S112:/repo/skill/x.py")
        assert key == "S112:skill/x.py"

    def test_normalise_key_passes_relative_untouched(self, monkeypatch):
        """Relative (repo-relative) paths pass through without subprocess."""
        def _no_git(*args, **kwargs):  # pragma: no cover - guard
            raise AssertionError("relative paths must not run subprocesses")
        monkeypatch.setattr(cd, "_main_root", _no_git)
        assert cd._normalise_key("S112:skill/x.py") == "S112:skill/x.py"


# ---------------------------------------------------------------------------
# Pure grouping (F1 contract + legacy mixing)
# ---------------------------------------------------------------------------


class TestGrouping:
    def test_marker_and_legacy_items_for_same_identity_group_together(
        self, monkeypatch,
    ):
        """A modern marker item and a legacy item for the same (code, path)
        collapse into one group (the exact cross-era backlog case)."""
        monkeypatch.setattr(cd, "_main_root", lambda: "/repo")
        items = [
            _item("SA-0NEW", "S112:skill/x.py", "2026-11-03T00:00:00Z"),
            _legacy_item("SA-0OLD", "S112", "/repo/skill/x.py", "2026-11-01T00:00:00Z"),
        ]
        groups = cd.group_by_identity(items)
        assert len(groups) == 1
        assert groups[0]["key"] == "S112:skill/x.py"
        assert groups[0]["canonical"]["id"] == "SA-0OLD"
        assert [d["id"] for d in groups[0]["duplicates"]] == ["SA-0NEW"]

    def test_terminal_items_excluded(self):
        """Completed items never join a live group (idempotent re-run)."""
        items = [
            _item("SA-0OPEN", "S112:skill/x.py", "2026-11-01T00:00:00Z"),
            _item("SA-0DONE", "S112:skill/x.py", "2026-11-02T00:00:00Z",
                  status="completed"),
        ]
        groups = cd.group_by_identity(items)
        assert len(groups) == 1
        assert groups[0]["duplicates"] == []

    def test_dry_run_grouping_never_runs_subprocesses(self, monkeypatch):
        """The pure grouping never touches the worklog (F1 dry-run guard)."""
        monkeypatch.setattr(cd, "_main_root", lambda: "/repo")
        items = [
            _item("SA-0A", "S112:skill/x.py", "2026-11-01T00:00:00Z"),
            _item("SA-0B", "S112:skill/x.py", "2026-11-02T00:00:00Z"),
            _item("SA-0C", "F401:skill/y.py", "2026-11-01T00:00:00Z"),
        ]
        import subprocess
        def _no_subprocess(*args, **kwargs):  # pragma: no cover - guard
            raise AssertionError("dry-run grouping must not run subprocesses")
        monkeypatch.setattr(subprocess, "run", _no_subprocess)
        groups = cd.group_by_identity(items)
        assert {g["key"] for g in groups} == {"S112:skill/x.py", "F401:skill/y.py"}


# ---------------------------------------------------------------------------
# Apply path (AC: union lines, superseded comments, close, never audit twins)
# ---------------------------------------------------------------------------


class TestApply:
    def test_apply_unions_lines_and_closes_duplicates(self, monkeypatch):
        """--apply updates the canonical description with the unioned lines,
        comments each duplicate and closes it."""
        canonical = _item("SA-0CANON", "S112:skill/x.py", "2026-11-01T00:00:00Z",
                          lines="12")
        duplicate = _item("SA-0DUP", "S112:skill/x.py", "2026-11-02T00:00:00Z",
                          lines="40")
        group = {"key": "S112:skill/x.py", "canonical": canonical,
                 "duplicates": [duplicate]}

        commands: list[list[str]] = []

        def _fake_wl(cmd):
            commands.append(list(cmd))
            joined = " ".join(cmd)
            if "update" in joined:
                return {"success": True}
            if "comment" in joined:
                return {"success": True}
            if "close" in joined:
                return {"success": True}
            return {"success": True}

        monkeypatch.setattr(cd, "_run_wl", _fake_wl)
        report = cd.apply_consolidation(group)

        assert report["canonical"] == "SA-0CANON"
        assert report["closed"] == ["SA-0DUP"]
        assert report["errors"] == []
        joined = " ".join(" ".join(c) for c in commands)
        assert "refactor-consolidation" in joined
        # The canonical update unioned lines 12 and 40.
        update_cmd = next(c for c in commands if "update" in c)
        assert "Refactor-Affected-Lines: 12, 40" in " ".join(update_cmd)
        # The duplicate got a comment and a close with the recorded reason.
        close_cmd = next(c for c in commands if "close" in c)
        assert "superseded by SA-0CANON — duplicate identity" in " ".join(close_cmd)

    def test_single_member_group_is_noop(self, monkeypatch):
        """A group without duplicates performs zero worklog writes."""
        group = {"key": "S112:skill/x.py",
                 "canonical": _item("SA-0SOLO", "S112:skill/x.py",
                                    "2026-11-01T00:00:00Z"),
                 "duplicates": []}
        calls: list[list[str]] = []
        monkeypatch.setattr(cd, "_run_wl", lambda cmd: calls.append(list(cmd)) or {"success": True})
        report = cd.apply_consolidation(group)
        assert report["closed"] == []
        assert calls == []


class TestCLI:
    def test_dry_run_default_lists_groups_without_mutation(self, monkeypatch, capsys):
        """AC: dry-run (default) lists groups; zero wl writes (only reads)."""
        items = [
            _item("SA-0CANON", "S112:skill/x.py", "2026-11-01T00:00:00Z"),
            _item("SA-0DUP", "S112:skill/x.py", "2026-11-02T00:00:00Z"),
        ]
        commands: list[list[str]] = []

        def _fake_wl(cmd):
            commands.append(list(cmd))
            joined = " ".join(cmd)
            if "list" in joined:
                return {"success": True, "workItems": items}
            return {"success": True}

        monkeypatch.setattr(cd, "_run_wl", _fake_wl)
        monkeypatch.setattr(cd, "load_audit_twins", list)

        code = cd.main(["--json"])
        out = json.loads(capsys.readouterr().out)
        assert code == 0
        assert out["dry_run"] is True
        assert out["groups_with_duplicates"] == 1
        # Only reads: every command is a `wl list`.
        assert all("list" in " ".join(c) for c in commands), commands

    def test_apply_closes_duplicates_but_never_audit_twins(self, monkeypatch, capsys):
        """AC: --apply closes refactor duplicates; audit-family twins are
        reported but never closed or mutated."""
        items = [
            _item("SA-0CANON", "S112:skill/x.py", "2026-11-01T00:00:00Z"),
            _item("SA-0DUP", "S112:skill/x.py", "2026-11-02T00:00:00Z"),
        ]
        twin = {
            "id": "SA-0TWIN",
            "title": "ruff S112 in skill/x.py:12 — candidate false positive "
                     "— producer decision required",
            "description": "CFP-Chore-Key: ruff:S112:skill/x.py:12\n",
            "status": "open",
            "issueType": "chore",
            "tags": [],
        }
        writes: list[list[str]] = []

        def _fake_wl(cmd):
            joined = " ".join(cmd)
            if "list" in joined:
                if "candidate false positive" in joined:
                    return {"success": True, "workItems": [twin]}
                return {"success": True, "workItems": items}
            writes.append(list(cmd))
            return {"success": True}

        monkeypatch.setattr(cd, "_run_wl", _fake_wl)
        monkeypatch.setattr(cd, "load_audit_twins", lambda: [twin])

        code = cd.main(["--apply", "--json"])
        report = json.loads(capsys.readouterr().out)
        assert code == 0
        assert report["dry_run"] is False
        assert report["apply"][0]["closed"] == ["SA-0DUP"]
        # The audit twin was never closed or updated.
        assert not any(
            ("close" in " ".join(c) or "update" in " ".join(c)) and "SA-0TWIN" in c
            for c in writes
        ), writes
