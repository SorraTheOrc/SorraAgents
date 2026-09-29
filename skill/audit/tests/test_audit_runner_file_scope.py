"""Regression tests for the Phase 2 FILE SCOPE manifest's source files.

Covers SA-0MUJNZ5RN0078B5M / SA-0MUKCOQI10095463: on a clean checkout the
working-tree diff is empty, so the manifest must still include the audited
item's *committed* touched files (resolved via ``_resolve_touched_files``).
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from audit.scripts import audit_runner

_COMMIT_MARKER = audit_runner._TOUCHED_FILES_COMMIT_MARKER


def _git_runner(*, committed=None, changed=None, porcelain=None,
                ls_files=None, grep_ok=True):
    """Return a fake runner simulating a repository's git responses.

    ``committed`` is what the item-id ``git log --grep`` returns; the
    working tree is clean unless ``changed``/``porcelain`` are supplied.
    ``grep_ok=False`` simulates git failing so touched-file resolution
    returns ``None`` (the fail-open signal).
    """
    committed = list(committed or [])
    changed = list(changed or [])
    porcelain = list(porcelain or [])
    ls_files = list(ls_files or [])

    def _run(cmd):
        cmd = [str(c) for c in cmd]
        joined = " ".join(cmd)
        if cmd[:2] == ["git", "log"] and "--name-only" in joined:
            if not grep_ok:
                return SimpleNamespace(returncode=128, stdout="", stderr="fatal")
            body = [f"{_COMMIT_MARKER}abc1234"] + committed
            return SimpleNamespace(returncode=0, stdout="\n".join(body) + "\n",
                                   stderr="")
        if cmd[:2] == ["git", "log"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[:2] == ["git", "diff"]:
            return SimpleNamespace(returncode=0,
                                   stdout="\n".join(changed) + ("\n" if changed else ""),
                                   stderr="")
        if cmd[:2] == ["git", "status"]:
            return SimpleNamespace(returncode=0,
                                   stdout="\n".join(porcelain) + ("\n" if porcelain else ""),
                                   stderr="")
        if cmd[:2] == ["git", "ls-files"]:
            return SimpleNamespace(returncode=0,
                                   stdout="\n".join(ls_files) + ("\n" if ls_files else ""),
                                   stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    return _run


def _issue(issue_id="SA-TEST-1", description=""):
    return {"id": issue_id, "title": "Test item", "description": description}


def test_manifest_includes_committed_touched_files_when_tree_clean():
    """AC1: a clean tree still yields the item's committed touched files."""
    committed = [
        "skill/audit/scripts/audit_runner.py",
        "skill/audit/tests/test_audit_runner_file_scope.py",
        "skill/test_cache.py",
    ]
    issue = _issue(description="## Key Files\n\n- `skill/audit/scripts/audit_runner.py`\n")
    runner = _git_runner(committed=committed, ls_files=committed)

    manifest = audit_runner._build_file_scope_manifest(issue, [], runner=runner)

    for path in committed:
        assert path in manifest, f"committed touched file missing: {path}"


def test_manifest_composition_preserved():
    """AC2: Key Files, Phase 1 refs and repo index remain in the manifest."""
    issue = _issue(
        description="## Key Files\n\n- `skill/audit/scripts/audit_runner.py`\n",
    )
    acs = [{
        "index": 0,
        "text": "the thing works",
        "verdict": "met",
        "evidence": "skill/audit/scripts/audit_runner.py:42",
    }]
    runner = _git_runner(
        committed=["skill/audit/scripts/audit_runner.py"],
        ls_files=["skill/audit/scripts/audit_runner.py", "README.md"],
    )

    manifest = audit_runner._build_file_scope_manifest(issue, acs, runner=runner)

    assert "Key Files (from the work item)" in manifest
    assert "Phase 1 evidence file:line references" in manifest
    assert "Repository index" in manifest
    assert "audit_runner.py:42" in manifest


def test_manifest_graceful_when_touched_resolution_returns_none():
    """AC3: git failure degrades gracefully — non-empty manifest, no raise."""
    issue = _issue(
        description="## Key Files\n\n- `skill/audit/scripts/audit_runner.py`\n",
    )
    runner = _git_runner(
        committed=[], changed=[], ls_files=["skill/audit/"],
        grep_ok=False,
    )

    manifest = audit_runner._build_file_scope_manifest(issue, [], runner=runner)

    assert isinstance(manifest, str)
    assert manifest
    assert "Key Files (from the work item)" in manifest


def test_manifest_dedupes_touched_against_changed_and_key_files():
    """AC4: a path present in changed/Key Files is not repeated as touched."""
    path = "skill/audit/scripts/audit_runner.py"
    issue = _issue(description=f"## Key Files\n\n- `{path}`\n")
    runner = _git_runner(committed=[path], changed=[path], ls_files=[path])

    manifest = audit_runner._build_file_scope_manifest(issue, [], runner=runner)

    # The path appears exactly once as a bullet despite three sources.
    assert manifest.count(f"- `{path}`") == 1


def test_touched_resolution_skips_grep_for_empty_id():
    """An empty item id must not run ``git log --grep=`` (matches everything)."""
    calls: list[list[str]] = []

    def _runner(cmd):
        calls.append([str(c) for c in cmd])
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    result = audit_runner._resolve_touched_files(
        _runner, "", work_item={"description": ""},
    )

    assert result is None
    assert not any("--grep=" in " ".join(c) for c in calls)
