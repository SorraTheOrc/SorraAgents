#!/usr/bin/env python3
"""End-to-end probe: a worktree observes its own ``packages/shared`` change.

Purpose
-------
Prove, without touching the live checkout, that the sanctioned gate
(``run_tests.py --type full --scope full``) sees a change under a worktree's
``packages/shared/src/**`` **when** the worktree's ``node_modules`` link farm
resolves ``@worklog/shared`` to the worktree's own package — and that an
unmodified main checkout does **not** observe it.

AC3 of SA-0MVPI41SR008JYXL (parent SA-0MVMZBJGT00718ZO). The resolution chain
it exercises is the one that made the change invisible before the link farm:

    <worktree>/node_modules -> <main>/node_modules
    <main>/node_modules/@worklog/shared -> ../../packages/shared   (main copy)
    <main>/packages/shared/package.json main -> ./dist/index.js    (stale build)

Scenarios
---------
1. **worktree, link farm** (`implement._ensure_node_modules_symlink`, the real
   ``start`` step) — the gate passes: the probe change is observed.
2. **worktree, stale whole-directory symlink** (the pre-link-farm ``start``
   behaviour) — the gate fails: the same change is invisible, which is the
   false-red / false-green hazard the hard-fail detects.
3. **unmodified main checkout** — the gate fails: it resolves the main
   checkout's stale ``dist`` and never sees the probe change.

Every repository is a throwaway fixture created with
``skill/shared/git_sandbox.py`` under the system temp dir, and each gate run
goes through the real ``run_tests.py`` CLI (not nested inside pytest, so it
never contends with the caller's test-slot budget).

Usage
-----
    python3 docs/dev/probe_worktree_shared_resolution.py [--keep] [--json]

Exit code 0 when all three scenarios behave as described, 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
for _path in (str(REPO_ROOT), str(REPO_ROOT / "skill")):
    if _path not in sys.path:
        sys.path.append(_path)

from shared import git_sandbox as gs

from skill.implement.scripts import implement

SHARED_REL = Path("packages") / "shared"
SHARED_NAME = "@worklog/shared"
RUN_TESTS = REPO_ROOT / "skill" / "test" / "scripts" / "run_tests.py"
PROBE_VALUE = "probe-dist"
MAIN_VALUE = "main-dist"

_GITIGNORE = "node_modules\n**/dist/\n.worklog/\n__pycache__/\n"

_PROBE_TEST = """\
import assert from 'node:assert/strict';

const shared = await import('@worklog/shared');

assert.equal(
  shared.VERSION,
  '{probe}',
  `@worklog/shared resolved VERSION=${{shared.VERSION}} (expected '{probe}')`,
);
console.log(`probe ok: @worklog/shared VERSION=${{shared.VERSION}}`);
"""


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _seed_main(main: Path) -> None:
    """Seed a minimal workspace checkout at the reference layout."""
    main.mkdir(parents=True, exist_ok=True)
    _write(main / ".gitignore", _GITIGNORE)
    _write(
        main / "package.json",
        json.dumps(
            {
                "name": "probe-workspace",
                "private": True,
                "workspaces": ["packages/*"],
            }
        )
        + "\n",
    )
    # The gate is the sanctioned runner; its command set comes from the
    # project's extension point (the same resolution a real repo uses).
    _write(
        main / ".pi" / "test-config.json",
        json.dumps(
            {"suiteCommands": ["node probe.test.mjs"]}
        )
        + "\n",
    )
    _write(
        main / SHARED_REL / "package.json",
        json.dumps(
            {
                "name": SHARED_NAME,
                "version": "0.0.0",
                "type": "module",
                "main": "./dist/index.js",
                "exports": {".": "./dist/index.js"},
            }
        )
        + "\n",
    )
    _write(
        main / SHARED_REL / "src" / "index.js",
        "export const VERSION = 'main-src';\n",
    )
    _write(
        main / SHARED_REL / "dist" / "index.js",
        f"export const VERSION = '{MAIN_VALUE}';\n",
    )
    # The probe lives OUTSIDE the shared package: a test inside it would
    # resolve ``@worklog/shared`` through Node's package self-reference
    # (package.json ``exports``) and never exercise ``node_modules``.
    _write(
        main / "probe.test.mjs",
        _PROBE_TEST.format(probe=PROBE_VALUE),
    )
    # The reference workspace install: a relative link back into the checkout.
    (main / "node_modules" / "@worklog").mkdir(parents=True)
    (main / "node_modules" / "@worklog" / "shared").symlink_to(
        "../../packages/shared"
    )


def _apply_probe_change(wt: Path) -> None:
    """The item's change set: shared source plus its rebuild in the worktree."""
    src = wt / SHARED_REL / "src" / "index.js"
    src.write_text(
        src.read_text(encoding="utf-8") + "export const PROBE = true;\n",
        encoding="utf-8",
    )
    # Stands in for `npm run build`: the worktree's own dist is rebuilt.
    _write(
        wt / SHARED_REL / "dist" / "index.js",
        f"export const VERSION = '{PROBE_VALUE}';\n",
    )


def _install_stale_symlink(wt: Path, main: Path) -> None:
    """Reproduce the pre-link-farm ``start`` behaviour."""
    nm = wt / "node_modules"
    if nm.is_symlink() or nm.is_file():
        nm.unlink()
    elif nm.is_dir():
        shutil.rmtree(nm)
    os.symlink(str(main / "node_modules"), str(nm), target_is_directory=True)


def _run_gate(project_root: Path, timeout: int) -> dict:
    """Run the sanctioned gate in *project_root* and return its JSON result."""
    proc = subprocess.run(
        [
            sys.executable,
            str(RUN_TESTS),
            "--type", "full",
            "--scope", "full",
            "--project-root", str(project_root),
            "--timeout", str(timeout),
            "--no-cache",
            "--json",
        ],
        cwd=str(project_root),
        capture_output=True,
        text=True,
        timeout=timeout + 120,
        check=False,
    )
    try:
        return json.loads(proc.stdout)
    except (json.JSONDecodeError, ValueError):
        return {
            "success": False,
            "returncode": proc.returncode,
            "stdout": proc.stdout[-4000:],
            "stderr": proc.stderr[-4000:],
        }


def _summary(result: dict) -> list[str]:
    lines: list[str] = []
    for suite, data in (result.get("suites") or {}).items():
        lines.append(
            f"    {suite}: success={data.get('success')} "
            f"exit={data.get('returncode')} command={data.get('command')!r}"
        )
        tail = (data.get("stdout") or "").strip().splitlines()[-3:]
        for line in tail:
            lines.append(f"      | {line}")
        err = (data.get("stderr") or "").strip().splitlines()[-3:]
        for line in err:
            lines.append(f"      ! {line}")
    if not lines:
        lines.append(f"    raw: {json.dumps(result)[:1000]}")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep", action="store_true", help="keep the fixture")
    parser.add_argument("--json", action="store_true", help="emit JSON evidence")
    parser.add_argument(
        "--timeout", type=int, default=180, help="per-gate-run timeout (s)"
    )
    args = parser.parse_args(argv)

    sandbox = Path(tempfile.mkdtemp(prefix="probe-shared-resolution-"))
    evidence: dict = {"sandbox": str(sandbox), "scenarios": {}}
    failures: list[str] = []
    try:
        main_checkout = gs.init_repo(
            sandbox / "main", default_branch="dev", sandbox_root=sandbox
        )
        _seed_main(main_checkout)
        gs.commit_all(main_checkout, "seed probe workspace")

        # ── Scenario 1: worktree with the link farm ────────────────
        wt = gs.add_worktree(
            main_checkout, sandbox / "wt-farm", "wl-probe-farm", base="dev"
        )
        assert implement._ensure_node_modules_symlink(
            str(wt), str(main_checkout)
        ), "link farm must be created"
        resolution = implement._shared_resolution_check(
            str(wt), str(main_checkout)
        )
        _apply_probe_change(wt)
        farm_gate = _run_gate(wt, args.timeout)
        evidence["scenarios"]["worktree_link_farm"] = {
            "resolution": resolution,
            "gate_success": bool(farm_gate.get("success")),
            "gate": farm_gate,
        }
        if not resolution.get("ok") or not farm_gate.get("success"):
            failures.append(
                "scenario 1: the gate did not observe the worktree's change "
                "with the link farm present"
            )

        # ── Scenario 2: worktree with the stale whole-dir symlink ──
        wt_stale = gs.add_worktree(
            main_checkout, sandbox / "wt-stale", "wl-probe-stale", base="dev"
        )
        _install_stale_symlink(wt_stale, main_checkout)
        stale_resolution = implement._shared_resolution_check(
            str(wt_stale), str(main_checkout)
        )
        _apply_probe_change(wt_stale)
        stale_gate = _run_gate(wt_stale, args.timeout)
        stale_refusal = implement._stale_shared_resolution(
            str(wt_stale), str(main_checkout)
        )
        evidence["scenarios"]["worktree_stale_symlink"] = {
            "resolution": stale_resolution,
            "gate_success": bool(stale_gate.get("success")),
            "finish_refuses": stale_refusal is not None,
            "gate": stale_gate,
        }
        if stale_resolution.get("ok") or stale_gate.get("success"):
            failures.append(
                "scenario 2: the stale symlink unexpectedly observed the change"
            )
        if stale_refusal is None:
            failures.append(
                "scenario 2: finish did not refuse the stale resolution"
            )

        # ── Scenario 3: the unmodified main checkout ───────────────
        main_gate = _run_gate(main_checkout, args.timeout)
        evidence["scenarios"]["main_checkout"] = {
            "gate_success": bool(main_gate.get("success")),
            "gate": main_gate,
        }
        if main_gate.get("success"):
            failures.append(
                "scenario 3: the unmodified main checkout observed the probe "
                "change"
            )

        # ── AC5-adjacent: no contamination of the main checkout ────
        status = gs.run_git(main_checkout, ["status", "--porcelain=v1"]).stdout
        evidence["main_checkout_status"] = status.strip()
        if status.strip():
            failures.append(
                f"the main checkout is dirty after the probe:\n{status}"
            )
    finally:
        if not args.keep:
            shutil.rmtree(sandbox, ignore_errors=True)

    evidence["failures"] = failures
    evidence["ok"] = not failures

    if args.json:
        print(json.dumps(evidence, indent=2))
    else:
        for name, data in evidence["scenarios"].items():
            verdict = "PASS" if data["gate_success"] else "FAIL"
            print(f"[{name}] gate {verdict}")
            print("\n".join(_summary(data["gate"])))
            if "resolution" in data:
                print(f"    resolution.ok={data['resolution'].get('ok')}")
            if "finish_refuses" in data:
                print(f"    finish_refuses={data['finish_refuses']}")
            print()
        for failure in failures:
            print(f"UNEXPECTED: {failure}")
        print(
            "RESULT: probe PASSED — a worktree change is observed only when "
            "the link farm resolves the shared package locally."
            if not failures
            else "RESULT: probe FAILED — see the unexpected outcomes above."
        )
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
