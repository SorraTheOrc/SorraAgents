"""Regression tests for the fresh-worktree shared build precondition.

SA-0MWHUVJFO009R9EC / SA-0MWHUVISS003APKM (framework half of
WL-0MW3EDP6D002U6V4). A linked worktree has no ``packages/shared/dist`` (it is
gitignored) while the link farm resolves ``@worklog/shared`` to the worktree's
own, unbuilt package — so every TypeScript suite that imports it fails with N
import errors until someone builds by hand.

``implement.py start`` must instead ensure the worktree's ``dist`` exists and
is fresh (producer decision Q2=A: freshness-based, unconditional on the change
set): build when missing or older than the newest ``src/**`` file, skip when
fresh, no-op for a repo without a workspace ``shared`` package, and report ONE
actionable diagnostic naming ``npm run build:shared`` on failure.

mtime semantics mirror ContextHub's ``isDistFresh()``
(``bench/wl-startup-perf.ts``); the single freshness helper defined here is the
one WL-0MW8G1X3V007CGAE reuses for its stale main-checkout guard. Precedent
fixtures: ``test_node_modules_link_farm.py``.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

from skill.implement.scripts import implement
from skill.implement.tests.test_node_modules_link_farm import (
    _make_main_checkout,
    _make_worktree,
    _mocked_phase_start,
)

SHARED_NAME = "@worklog/shared"


def _write(path: Path, text: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _make_worktree_with_shared(root: Path, *, dist: bool = True) -> Path:
    """A bare worktree fixture with a workspace ``@worklog/shared`` package."""
    wt = root / "wt"
    _write(
        wt / "package.json",
        '{"name": "fixture", "private": true, "workspaces": ["packages/*"],'
        ' "scripts": {"build:shared": "cd packages/shared && npm run build"}}',
    )
    _write(
        wt / "packages" / "shared" / "package.json",
        '{"name": "@worklog/shared", "main": "./dist/index.js",'
        ' "scripts": {"build": "tsc"}}',
    )
    _write(wt / "packages" / "shared" / "src" / "index.ts", "export const V = 1;\n")
    if dist:
        _write(
            wt / "packages" / "shared" / "dist" / "index.js",
            "exports.V = 1;\n",
        )
    return wt


def _resolution(*, applicable: bool = True, ok: bool = True) -> dict:
    """A :func:`_shared_resolution_check`-shaped result."""
    return {
        "applicable": applicable,
        "ok": ok if applicable else None,
        "package": SHARED_NAME,
        "expected_root": "/wt/packages/shared",
        "resolved_root": (
            "/wt/packages/shared"
            if ok
            else ("/main/packages/shared" if applicable else None)
        ),
        "stale_dist": None if ok else "/main/packages/shared/dist",
    }


def _set_mtime(path: Path, when: float) -> None:
    import os

    os.utime(path, (when, when))


def _ok_run(*_args, **_kwargs):
    """A ``_run_shared_build`` stand-in returning a successful build result."""
    return MagicMock(returncode=0, stdout="built", stderr="")


# ---------------------------------------------------------------------------
# Freshness helper (the single mtime helper WL-0MW8G1X3V007CGAE reuses)
# ---------------------------------------------------------------------------


def test_freshness_missing_dist_is_not_fresh(tmp_path):
    wt = _make_worktree_with_shared(tmp_path, dist=False)

    result = implement._shared_dist_freshness(wt / "packages" / "shared")

    assert result["fresh"] is False
    assert result["reason"] == "missing_dist"
    assert result["dist_mtime"] is None


def test_freshness_stale_dist_is_not_fresh(tmp_path):
    wt = _make_worktree_with_shared(tmp_path)
    shared = wt / "packages" / "shared"
    _set_mtime(shared / "src" / "index.ts", 2_000.0)
    _set_mtime(shared / "dist" / "index.js", 1_000.0)

    result = implement._shared_dist_freshness(shared)

    assert result["fresh"] is False
    assert result["reason"] == "stale_dist"


def test_freshness_equal_or_newer_dist_is_fresh(tmp_path):
    wt = _make_worktree_with_shared(tmp_path)
    shared = wt / "packages" / "shared"
    _set_mtime(shared / "src" / "index.ts", 1_000.0)
    _set_mtime(shared / "dist" / "index.js", 1_000.0)

    result = implement._shared_dist_freshness(shared)

    assert result["fresh"] is True
    assert result["reason"] == "fresh"


# ---------------------------------------------------------------------------
# The build step itself
# ---------------------------------------------------------------------------


def test_missing_dist_triggers_the_build(tmp_path):
    wt = _make_worktree_with_shared(tmp_path, dist=False)

    with patch.object(implement, "_run_shared_build", side_effect=_ok_run) as run_cmd:
        block = implement._ensure_shared_build(
            str(wt), str(tmp_path / "main"), _resolution()
        )

    assert block["applicable"] is True
    assert block["action"] == "built"
    assert block["success"] is True
    # The repo-appropriate command is the root build:shared script.
    assert block["command"] == ["npm", "run", "build:shared"]
    run_cmd.assert_called_once()
    assert run_cmd.call_args.args[0] == ["npm", "run", "build:shared"]


def test_stale_dist_triggers_the_build(tmp_path):
    wt = _make_worktree_with_shared(tmp_path)
    shared = wt / "packages" / "shared"
    _set_mtime(shared / "src" / "index.ts", 2_000.0)
    _set_mtime(shared / "dist" / "index.js", 1_000.0)

    with patch.object(implement, "_run_shared_build", side_effect=_ok_run) as run_cmd:
        block = implement._ensure_shared_build(
            str(wt), str(tmp_path / "main"), _resolution()
        )

    assert block["action"] == "built"
    run_cmd.assert_called_once()


def test_fresh_dist_is_not_rebuilt(tmp_path):
    wt = _make_worktree_with_shared(tmp_path)
    shared = wt / "packages" / "shared"
    _set_mtime(shared / "src" / "index.ts", 1_000.0)
    _set_mtime(shared / "dist" / "index.js", 2_000.0)

    with patch.object(implement, "_run_shared_build", side_effect=_ok_run) as run_cmd:
        block = implement._ensure_shared_build(
            str(wt), str(tmp_path / "main"), _resolution()
        )

    assert block["action"] == "skipped_fresh"
    assert block["success"] is True
    run_cmd.assert_not_called()


def test_repo_without_shared_package_is_a_noop(tmp_path):
    wt = _make_worktree_with_shared(tmp_path, dist=False)

    with patch.object(implement, "_run_shared_build", side_effect=_ok_run) as run_cmd:
        block = implement._ensure_shared_build(
            str(wt), str(tmp_path / "main"), _resolution(applicable=False)
        )

    assert block["applicable"] is False
    assert block["action"] == "not_applicable"
    assert block["success"] is True
    run_cmd.assert_not_called()


def test_unresolved_shared_package_is_not_built(tmp_path):
    """A stale resolution must never mutate the main checkout's package."""
    wt = _make_worktree_with_shared(tmp_path, dist=False)

    with patch.object(implement, "_run_shared_build", side_effect=_ok_run) as run_cmd:
        block = implement._ensure_shared_build(
            str(wt), str(tmp_path / "main"), _resolution(ok=False)
        )

    assert block["action"] == "skipped_unresolved"
    assert block["success"] is True
    run_cmd.assert_not_called()


def test_build_failure_emits_one_actionable_diagnostic(tmp_path):
    wt = _make_worktree_with_shared(tmp_path, dist=False)
    failure = MagicMock(
        returncode=2,
        stdout="",
        stderr="error TS: missing module",
    )

    with patch.object(implement, "_run_shared_build", return_value=failure):
        block = implement._ensure_shared_build(
            str(wt), str(tmp_path / "main"), _resolution()
        )

    assert block["action"] == "failed"
    assert block["success"] is False
    # ONE message naming the remediation, not a cascade of import errors.
    assert "npm run build:shared" in block["message"]
    assert str(wt / "packages" / "shared") in block["message"]


# ---------------------------------------------------------------------------
# start report integration
# ---------------------------------------------------------------------------


def test_start_report_includes_shared_build_block(tmp_path):
    """The start report gains ``shared_build`` without changing existing keys."""
    main = _make_main_checkout(tmp_path)
    wt = _make_worktree(main, tmp_path)
    # A fresh worktree copied from main ships a fresh dist — no rebuild.
    shared = wt / "packages" / "shared"
    _set_mtime(shared / "src" / "index.js", 1_000.0)
    _set_mtime(shared / "dist" / "index.js", 2_000.0)

    with _mocked_phase_start(main, wt, resumed=False):
        report = implement.phase_start("SA-TEST", json_output=True)

    assert report["success"] is True, report
    # Existing keys are unchanged.
    assert report["shared_resolution_ok"] is True
    assert "node_modules" in report["steps"]
    # New block is present and reports the action taken.
    block = report["steps"]["shared_build"]
    assert block["applicable"] is True
    assert block["action"] == "skipped_fresh"
    assert report["shared_build"] == block


def test_fresh_worktree_missing_dist_is_built_during_start(tmp_path):
    """A start on a worktree whose shared dist is missing invokes the build."""
    main = _make_main_checkout(tmp_path)
    wt = _make_worktree(main, tmp_path)
    shutil.rmtree(wt / "packages" / "shared" / "dist")
    # Declare the root build:shared script the step resolves.
    pkg = wt / "package.json"
    data = json.loads(pkg.read_text(encoding="utf-8"))
    data.setdefault("scripts", {})["build:shared"] = (
        "cd packages/shared && npm run build"
    )
    pkg.write_text(json.dumps(data), encoding="utf-8")

    with _mocked_phase_start(main, wt, resumed=False), patch.object(
        implement, "_run_shared_build", side_effect=_ok_run
    ) as run_cmd:
        report = implement.phase_start("SA-TEST", json_output=True)

    assert report["success"] is True, report
    block = report["steps"]["shared_build"]
    assert block["action"] == "built"
    assert block["success"] is True
    assert any(
        call.args and call.args[0] == ["npm", "run", "build:shared"]
        for call in run_cmd.call_args_list
    ), run_cmd.call_args_list
