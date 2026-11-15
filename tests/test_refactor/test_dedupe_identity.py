"""Identity-based dedupe contract for refactor-filed code smells.

This module is the **test-first** definition of the contract implemented by the
sibling work items under parent SA-0MW7Z1AU1006UT9C:

- **SA-0MWE1E4GQ004E1JG (F2)** — identity key
  ``(repository-relative path, rule code)``, the ``Refactor-Finding-Key`` /
  ``Refactor-Fingerprint`` / ``Refactor-Affected-Lines`` markers, and
  same-identity batch grouping.
- **SA-0MWE1EGLM002KE7Z (F4)** — repeat-occurrence comments
  (``Refactor-Repeat``) that keep a re-found smell on its existing item.
- **SA-0MWE1ELZO004J1DY (F5)** — duplicate consolidation grouping
  (``{key, canonical, duplicates}``) used by the one-off tool.

A test whose production helper has not landed yet skips with an explicit
``not yet implemented (<id>)`` reason, matching the repository's test-first
convention (see :mod:`tests.test_refactor.test_smell_detection`). Each guard
probes for the *specific* helper/constant its sibling item introduces, so a
partially-landed contract runs exactly the tests its helpers support and each
sibling's changed-scope gate stays green while it works.

Contract summary (asserted below):

- Identity is ``<code>:<repository-relative-path>``. The line number and any
  absolute or ``.worklog/worktrees/**`` prefix are excluded, so line drift,
  worktree-prefix drift and title truncation cannot break dedupe.
- A newly-filed item's description carries
  ``Refactor-Finding-Key: <code>:<path>``, ``Refactor-Fingerprint: <sha256>``
  and ``Refactor-Affected-Lines: <n>, <m>`` near the top.
- The pre-creation guard matches the exact identity (never a substring), so a
  smell re-found after a line shift reuses the existing item instead of filing
  a clone.
- A repeat finding appends
  ``Refactor-Repeat: <code> in <path> — lines <n>, <m>`` as a comment.
- The consolidation helper groups non-terminal Refactor items by identity into
  ``{key, canonical, duplicates}`` records, canonical = oldest open item, and
  performs no worklog mutation in dry-run.

Related work items:
- SA-0MW7Z1AU1006UT9C (parent — dedupe substring matching files smells repeatedly)
- SA-0MWE1DXPW00846UV (this test-first contract item)
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

# Ensure repo root is on sys.path (mirrors the sibling refactor test modules).
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# Import / capability guards
# ---------------------------------------------------------------------------


def _workitem_creation():
    """Import ``refactor.workitem_creation``; skip if unavailable."""
    try:
        from refactor import workitem_creation as wic
    except (ImportError, ModuleNotFoundError) as exc:  # pragma: no cover
        pytest.skip(f"workitem_creation not available: {exc}")
    return wic


def _require(wic: Any, names: tuple[str, ...], *, item: str, feature: str) -> Any:
    """Skip unless every helper in *names* exists on *wic*.

    The guard is deliberately per-helper so each sibling item turns on only
    the tests it implements (see the module docstring).
    """
    missing = [name for name in names if not hasattr(wic, name)]
    if missing:
        pytest.skip(
            f"{feature} not yet implemented ({item}): missing "
            f"{', '.join(missing)}"
        )
    return wic


def _identity():
    """Guard: the identity key build/parse helpers (F2)."""
    return _require(
        _workitem_creation(),
        ("build_finding_key", "parse_finding_key"),
        item="SA-0MWE1E4GQ004E1JG",
        feature="finding identity",
    )


def _fingerprint():
    """Guard: identity + content fingerprint + marker emission (F2)."""
    return _require(
        _workitem_creation(),
        ("build_finding_key", "compute_content_fingerprint"),
        item="SA-0MWE1E4GQ004E1JG",
        feature="identity marker and content fingerprint",
    )


def _repeat():
    """Guard: repeat-occurrence comment formatting (F4)."""
    return _require(
        _workitem_creation(),
        ("build_repeat_comment",),
        item="SA-0MWE1EGLM002KE7Z",
        feature="repeat-occurrence comments",
    )


def _consolidation():
    """Guard: the one-off consolidation grouping helper (F5)."""
    try:
        from refactor.scripts import consolidate_duplicates as cd
    except (ImportError, ModuleNotFoundError) as exc:
        pytest.skip(
            "duplicate consolidation not yet implemented "
            f"(SA-0MWE1ELZO004J1DY): {exc}"
        )
    return _require(
        cd,
        ("group_by_identity",),
        item="SA-0MWE1ELZO004J1DY",
        feature="duplicate consolidation grouping",
    )


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _cp(
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess:
    """Build a subprocess.CompletedProcess for the fake runner."""
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _make_file(
    tmp_path: Path,
    rel: str = "skill/x.py",
    content: str = "a = 1\n",
) -> Path:
    """Create *rel* under *tmp_path* and return its absolute path."""
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def _fake_run_factory(
    repo_root: Path,
    list_items: list[dict[str, Any]],
    created: list[list[str]],
    comments: list[list[str]],
):
    """Build a fake ``subprocess.run`` for git + wl commands.

    ``git rev-parse`` resolves to *repo_root* so absolute scan paths become
    repository-relative. Every ``wl list`` returns *list_items*; ``wl create``
    and ``wl comment add`` are recorded in *created* / *comments*.
    """

    def fake_run(cmd, *args, **kwargs):
        argv = list(cmd) if isinstance(cmd, (list, tuple)) else str(cmd).split()
        prog = argv[0] if argv else ""
        if prog == "git" and "rev-parse" in argv:
            return _cp(stdout=str(repo_root))
        if prog == "wl" and len(argv) > 1:
            sub = argv[1]
            if sub == "list":
                return _cp(
                    stdout=json.dumps({"success": True, "workItems": list_items})
                )
            if sub == "create":
                created.append(argv)
                return _cp(
                    stdout=json.dumps(
                        {
                            "success": True,
                            "workItem": {"id": f"SA-0MOCK{len(created):04d}X"},
                        }
                    )
                )
            if sub == "comment":
                comments.append(argv)
                return _cp(stdout='{"success":true}')
            if sub in ("update", "close"):
                return _cp(stdout='{"success":true}')
        return _cp(stdout='{"success":true,"workItems":[]}')

    return fake_run


def _option(argv: list[str], name: str) -> str | None:
    """Return the value following *name* in *argv*, or ``None``."""
    if name in argv:
        index = argv.index(name)
        if index + 1 < len(argv):
            return argv[index + 1]
    return None


def _affected_lines(description: str) -> set[int]:
    """Parse the ``Refactor-Affected-Lines`` marker into a set of ints."""
    match = re.search(
        r"Refactor-Affected-Lines:[ \t]*([0-9][0-9, \t]*)", description or ""
    )
    if not match:
        return set()
    return {int(value) for value in re.findall(r"\d+", match.group(1))}


def _repeat_lines(comment: str) -> set[int]:
    """Parse the line numbers out of a ``Refactor-Repeat`` comment."""
    match = re.search(r"lines?[ \t]+([0-9][0-9, \t]*)", comment or "")
    if not match:
        return set()
    return {int(value) for value in re.findall(r"\d+", match.group(1))}


def _smell(code: str, line: int, file_path: str, **extra: Any) -> dict[str, Any]:
    """Build a smell finding dict with the required keys."""
    smell: dict[str, Any] = {
        "file": file_path,
        "line": line,
        "severity": "medium",
        "message": f"{code} finding",
        "source": "linter",
        "smell_type": "lint",
        "code": code,
    }
    smell.update(extra)
    return smell


# ---------------------------------------------------------------------------
# AC6: identity key build / parse (rule code + repo-relative path)
# ---------------------------------------------------------------------------


class TestFindingIdentity:
    """Identity is ``<code>:<repository-relative path>`` — never line/absolute."""

    def test_build_finding_key_uses_code_and_repo_relative_path(self):
        """Build yields ``<code>:<repo-relative path>`` with no line/absolute."""
        wic = _identity()
        worktree_root = "/repo/.worklog/worktrees/wl-AAA-1"
        smell = _smell(
            "S112", 42, f"{worktree_root}/skill/x.py"
        )

        key = wic.build_finding_key(smell, repo_root=worktree_root)

        assert key == "S112:skill/x.py"
        assert "42" not in key
        assert not key.startswith("/")
        assert ".worklog/worktrees" not in key

    def test_build_finding_key_ignores_line_drift(self):
        """Two findings at different lines share the same identity."""
        wic = _identity()

        low = wic.build_finding_key(_smell("S112", 10, "skill/x.py"))
        high = wic.build_finding_key(_smell("S112", 410, "skill/x.py"))

        assert low == high == "S112:skill/x.py"

    def test_parse_finding_key_round_trips(self):
        """``parse(build(...))`` recovers the code and repo-relative path."""
        wic = _identity()
        smell = _smell("SIM117", 42, "skill/x.py")

        parsed = wic.parse_finding_key(wic.build_finding_key(smell))

        if isinstance(parsed, dict):
            code, path = parsed.get("code"), parsed.get("path")
        else:
            code, path = parsed
        assert code == "SIM117"
        assert path == "skill/x.py"

    def test_identity_is_stable_across_worktrees(self):
        """The same repo-relative file scanned from two worktrees is one identity."""
        wic = _identity()
        wt1 = "/repo/.worklog/worktrees/wl-AAA-1"
        wt2 = "/repo/.worklog/worktrees/wl-BBB-2"

        key1 = wic.build_finding_key(
            _smell("S112", 12, f"{wt1}/skill/x.py"), repo_root=wt1
        )
        key2 = wic.build_finding_key(
            _smell("S112", 99, f"{wt2}/skill/x.py"), repo_root=wt2
        )

        assert key1 == key2 == "S112:skill/x.py"

    def test_build_finding_key_reuses_repo_relative_path(self):
        """The key uses the shared ``_repo_relative_path`` normalisation."""
        wic = _identity()
        worktree_root = "/repo/.worklog/worktrees/wl-AAA-1"
        absolute = f"{worktree_root}/skill/x.py"
        expected_path = wic._repo_relative_path(absolute, worktree_root)

        key = wic.build_finding_key(
            _smell("S112", 1, absolute), repo_root=worktree_root
        )

        assert key == f"S112:{expected_path}"


# ---------------------------------------------------------------------------
# AC6: marker and content-fingerprint emission in item descriptions
# ---------------------------------------------------------------------------


class TestMarkerAndFingerprintEmission:
    """New item descriptions carry the machine-readable identity markers."""

    def test_description_emits_finding_key_marker(self, tmp_path: Path):
        """The description contains ``Refactor-Finding-Key: <code>:<path>``."""
        wic = _fingerprint()
        file_path = _make_file(tmp_path)
        smell = _smell("S112", 12, str(file_path))

        desc = wic.build_smell_description(smell, repo_root=str(tmp_path))

        assert "Refactor-Finding-Key: S112:skill/x.py" in desc

    def test_finding_key_marker_sits_near_the_top(self, tmp_path: Path):
        """The marker is near the top so truncation cannot hide it."""
        wic = _fingerprint()
        file_path = _make_file(tmp_path)
        smell = _smell("S112", 12, str(file_path))

        lines = wic.build_smell_description(smell, repo_root=str(tmp_path)).splitlines()
        marker_index = next(
            i for i, line in enumerate(lines) if "Refactor-Finding-Key:" in line
        )

        assert marker_index < 10, (
            f"identity marker is line {marker_index} (expected near the top)"
        )

    def test_description_emits_content_fingerprint(self, tmp_path: Path):
        """The description contains a sha256 ``Refactor-Fingerprint`` marker."""
        wic = _fingerprint()
        file_path = _make_file(tmp_path)
        smell = _smell("S112", 12, str(file_path))

        desc = wic.build_smell_description(smell, repo_root=str(tmp_path))

        assert re.search(r"Refactor-Fingerprint:\s*[0-9a-f]{64}", desc), desc

    def test_fingerprint_is_stable_and_content_sensitive(self, tmp_path: Path):
        """Same content → same fingerprint; changed content → different."""
        wic = _fingerprint()
        file_path = _make_file(tmp_path, content="a = 1\n")
        smell = _smell("S112", 1, str(file_path))

        first = wic.compute_content_fingerprint(smell, repo_root=str(tmp_path))
        second = wic.compute_content_fingerprint(smell, repo_root=str(tmp_path))
        assert first == second
        assert re.fullmatch(r"[0-9a-f]{64}", first), first

        file_path.write_text("a = 2\n")
        changed = wic.compute_content_fingerprint(smell, repo_root=str(tmp_path))
        assert changed != first

    def test_description_emits_all_affected_lines(self, tmp_path: Path):
        """Every affected line is listed, deduplicated and sorted."""
        wic = _fingerprint()
        file_path = _make_file(tmp_path)
        smell = _smell("S112", 12, str(file_path), lines=[40, 12, 40])

        desc = wic.build_smell_description(smell, repo_root=str(tmp_path))

        assert _affected_lines(desc) == {12, 40}


# ---------------------------------------------------------------------------
# AC1 + AC4: same-identity batch grouping vs distinct identities
# ---------------------------------------------------------------------------


class TestBatchIdentityGrouping:
    """A batch run groups by identity, not by line or array position."""

    def test_same_smell_twice_in_one_batch_files_one_item(
        self, monkeypatch, tmp_path: Path
    ):
        """Two findings of one (file, rule) identity file exactly one item."""
        wic = _identity()
        file_path = _make_file(tmp_path)
        first = _smell("S112", 12, str(file_path))
        second = _smell("S112", 40, str(file_path))

        created: list[list[str]] = []
        comments: list[list[str]] = []
        monkeypatch.setattr(
            subprocess, "run", _fake_run_factory(tmp_path, [], created, comments)
        )
        monkeypatch.setattr(wic.shutil, "which", lambda _name: "/usr/bin/wl")

        ids = wic.create_smell_work_items([first, second])

        assert len(created) == 1, (
            f"expected one item for one identity, created {len(created)}"
        )
        assert len(set(ids)) == 1

        description = _option(created[0], "--description") or ""
        assert "Refactor-Finding-Key: S112:skill/x.py" in description
        assert _affected_lines(description) == {12, 40}, description

    def test_two_different_rules_in_one_file_file_two_items(
        self, monkeypatch, tmp_path: Path
    ):
        """Different rule codes are different identities → two items."""
        from refactor import workitem_creation as wic

        file_path = _make_file(tmp_path)
        first = _smell("S112", 12, str(file_path))
        second = _smell("F401", 12, str(file_path))

        created: list[list[str]] = []
        comments: list[list[str]] = []
        monkeypatch.setattr(
            subprocess, "run", _fake_run_factory(tmp_path, [], created, comments)
        )
        monkeypatch.setattr(wic.shutil, "which", lambda _name: "/usr/bin/wl")

        ids = wic.create_smell_work_items([first, second])

        assert len(created) == 2, (
            f"expected two items for two identities, created {len(created)}"
        )
        assert len(set(ids)) == 2


# ---------------------------------------------------------------------------
# AC2: a re-found smell after a line shift reuses the existing item
# ---------------------------------------------------------------------------


class TestRepeatAfterLineShift:
    """Identity survives line drift; a repeat is recorded, not re-filed."""

    def test_line_shift_reuses_existing_item_and_records_repeat(
        self, monkeypatch, tmp_path: Path
    ):
        """Re-finding a smell one line later appends a repeat comment only."""
        wic = _repeat()
        file_path = _make_file(tmp_path)
        # The existing item was filed before the finding moved to line 42.
        existing = {
            "id": "SA-0EXISTINGX",
            "title": "Refactor: Lint in skill/x.py",
            "description": (
                "Refactor-Finding-Key: S112:skill/x.py\n"
                "Refactor-Affected-Lines: 12\n"
            ),
            "status": "open",
            "issueType": "task",
            "tags": ["Refactor"],
            "createdAt": "2026-11-01T00:00:00.000Z",
        }
        smell = _smell("S112", 42, str(file_path))

        created: list[list[str]] = []
        comments: list[list[str]] = []
        monkeypatch.setattr(
            subprocess,
            "run",
            _fake_run_factory(tmp_path, [existing], created, comments),
        )
        monkeypatch.setattr(wic.shutil, "which", lambda _name: "/usr/bin/wl")

        result = wic.create_smell_work_item(smell)

        assert not created, "a line-shifted repeat must not file a new item"
        assert result == "SA-0EXISTINGX", (
            f"expected the existing item id, got {result!r}"
        )
        assert len(comments) == 1, f"expected one repeat comment, got {comments}"
        comment = _option(comments[0], "--comment") or ""
        assert "Refactor-Repeat: S112 in skill/x.py" in comment
        assert "42" in comment
        assert "—" in comment


# ---------------------------------------------------------------------------
# AC7: repeat-occurrence comment formatting
# ---------------------------------------------------------------------------


class TestRepeatCommentFormatting:
    """The repeat comment lists the newly observed lines in a stable format."""

    def test_build_repeat_comment_lists_all_lines(self):
        """Comment is ``Refactor-Repeat: <code> in <path> — lines <n>, <m>``."""
        wic = _repeat()
        smell = _smell("S112", 12, "/repo/skill/x.py", lines=[12, 40])

        comment = wic.build_repeat_comment(smell, repo_root="/repo")

        assert comment.startswith("Refactor-Repeat: S112 in skill/x.py")
        assert "—" in comment
        assert _repeat_lines(comment) == {12, 40}, comment


# ---------------------------------------------------------------------------
# AC7: consolidation dry-run grouping (F5)
# ---------------------------------------------------------------------------


class TestConsolidationGrouping:
    """The consolidation helper groups by identity without mutating worklog."""

    @staticmethod
    def _item(
        item_id: str,
        key: str,
        created_at: str,
        *,
        status: str = "open",
        lines: str = "12",
    ) -> dict[str, Any]:
        return {
            "id": item_id,
            "title": f"Refactor: finding in {key.split(':', 1)[1]}",
            "description": (
                f"Refactor-Finding-Key: {key}\n"
                f"Refactor-Affected-Lines: {lines}\n"
            ),
            "status": status,
            "issueType": "task",
            "tags": ["Refactor"],
            "createdAt": created_at,
        }

    def test_dry_run_groups_identity_with_canonical_and_duplicates(
        self, monkeypatch
    ):
        """Groups carry ``key``, an oldest-open ``canonical`` and duplicates."""
        cd = _consolidation()
        items = [
            self._item("SA-0DUP2", "S112:skill/x.py", "2026-11-02T00:00:00Z"),
            self._item("SA-0CANON", "S112:skill/x.py", "2026-11-01T00:00:00Z"),
            self._item("SA-0OTHER", "F401:skill/y.py", "2026-11-01T00:00:00Z"),
        ]

        # Dry-run must never touch the worklog.
        def _no_subprocess(*args, **kwargs):  # pragma: no cover - guard
            raise AssertionError("dry-run grouping must not run subprocesses")

        monkeypatch.setattr(subprocess, "run", _no_subprocess)

        groups = cd.group_by_identity(items)

        by_key = {group["key"]: group for group in groups}
        s112 = by_key["S112:skill/x.py"]
        assert s112["canonical"]["id"] == "SA-0CANON"
        assert {item["id"] for item in s112["duplicates"]} == {"SA-0DUP2"}
        assert by_key["F401:skill/y.py"]["canonical"]["id"] == "SA-0OTHER"
        assert by_key["F401:skill/y.py"]["duplicates"] == []

    def test_dry_run_excludes_terminal_items(self):
        """Completed/deleted items no longer form part of a live group."""
        cd = _consolidation()
        items = [
            self._item("SA-0OPEN", "S112:skill/x.py", "2026-11-01T00:00:00Z"),
            self._item(
                "SA-0DONE",
                "S112:skill/x.py",
                "2026-11-02T00:00:00Z",
                status="completed",
            ),
        ]

        groups = cd.group_by_identity(items)

        assert len(groups) == 1
        assert groups[0]["canonical"]["id"] == "SA-0OPEN"
        assert groups[0]["duplicates"] == []
