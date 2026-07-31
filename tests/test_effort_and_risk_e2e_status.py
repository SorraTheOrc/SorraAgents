"""End-to-end tests for the effort-and-risk skill status lifecycle.

Exercises the **real** ``wl`` CLI and the **real** skill scripts
(``run_skill.py`` + ``orchestrate_estimate.py``) against an isolated
temporary worklog (created via ``wl init`` in a ``tmp_path``), so the repo's
own worklog is never touched and the tests are safe to run in CI.

Validates the acceptance criteria of SA-0MS93J0ZC007IO8V:

- the skill runs successfully from a cwd that is *not* the worklog project
  root (cwd independence via ``--worklog-dir`` injection / ancestor
  resolution)
- after the skill runs, an item at ``stage=intake_complete`` or
  ``plan_complete`` keeps ``status=open`` — it is **never** flipped to
  ``completed``
- effort/risk fields are populated and a comment is posted
- no ``wl`` invocation made by the skill sets ``--status completed``
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "skill" / "effort-and-risk" / "scripts"
RUN_SKILL = SCRIPTS_DIR / "run_skill.py"
ORCHESTRATOR = SCRIPTS_DIR / "orchestrate_estimate.py"

_REAL_WL = shutil.which("wl")

pytestmark = pytest.mark.skipif(
    _REAL_WL is None,
    reason="wl CLI is required for the e2e effort-and-risk status test",
)


# ===========================================================================
# Helpers
# ===========================================================================


def _run(cmd, cwd, env=None, input_text=None):
    """Run a command and return (returncode, stdout, stderr)."""
    proc = subprocess.run(
        cmd,
        cwd=str(cwd),
        env=env,
        input=input_text,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _wl_show(cwd, item_id, worklog_dir):
    """Fetch a work item from the temp worklog (explicit --worklog-dir)."""
    rc, out, err = _run(
        ["wl", "show", item_id, "--worklog-dir", str(worklog_dir), "--json"],
        cwd=cwd,
    )
    assert rc == 0, f"wl show failed: {out} {err}"
    return json.loads(out)["workItem"]


def _install_wl_shim(tmp_path, log_file):
    """Install a `wl` shim on PATH that logs every invocation to ``log_file``.

    The shim records each argv (as JSON, one per line) before delegating to
    the real ``wl`` CLI. Used to prove no skill invocation sets
    ``--status completed``.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    shim = bin_dir / "wl"
    shim.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        f"log = {str(log_file)!r}\n"
        "with open(log, 'a', encoding='utf-8') as fh:\n"
        "    fh.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        f"os.execv({_REAL_WL!r}, [{_REAL_WL!r}] + sys.argv[1:])\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
    return env


def _init_worklog(tmp_path):
    """Initialize an isolated temp worklog and return its ``.worklog`` path."""
    worklog_dir = tmp_path / ".worklog"
    rc, out, err = _run(
        [
            "wl", "init",
            "--worklog-dir", str(worklog_dir),
            "--project-name", "E2E effort-risk",
            "--prefix", "E2E",
            "--auto-export", "no",
            "--auto-sync", "no",
            "--json",
        ],
        cwd=tmp_path,
    )
    assert rc == 0, f"wl init failed: {out} {err}"
    assert worklog_dir.is_dir()
    return worklog_dir


def _create_item(tmp_path, worklog_dir, stage):
    """Create a work item at the given stage with status=open."""
    rc, out, err = _run(
        [
            "wl", "create",
            "--title", "E2E effort-risk item",
            "--description", "Auto-created by test_effort_and_risk_e2e_status.py",
            "--priority", "low",
            "--issue-type", "task",
            "--status", "open",
            "--stage", stage,
            "--worklog-dir", str(worklog_dir),
            "--json",
        ],
        cwd=tmp_path,
    )
    assert rc == 0, f"wl create failed: {out} {err}"
    return json.loads(out)["workItem"]["id"]


# ===========================================================================
# Fixture: temp worklog with a fresh item
# ===========================================================================


@pytest.fixture
def e2e_worklog(tmp_path):
    """Initialize an isolated worklog and return (root, worklog_dir)."""
    worklog_dir = _init_worklog(tmp_path)
    return tmp_path, worklog_dir


# ===========================================================================
# Tests
# ===========================================================================


class TestEffortRiskE2EStatus:
    """E2E: run_skill.py preserves status for intake/plan items."""

    @staticmethod
    def _assert_skill_success(proc_out):
        output = json.loads(proc_out)
        assert output.get("update_result", {}).get("success") is True, (
            "effort/risk fields should have been updated"
        )
        assert output.get("comment_result", {}).get("success") is True, (
            "a summary comment should have been posted"
        )
        assert output.get("effort", {}).get("tshirt") == "Small", (
            "o=1,m=2,p=3 + 4h overheads should yield a Small t-shirt"
        )
        assert output.get("risk", {}).get("level") in (
            "Low", "Medium", "High", "Critical",
        )
        return output

    @staticmethod
    def _assert_status_preserved(worklog_dir, item_id, stage, env, log_slice):
        # The skill-run wl invocations must never set --status completed
        for argv in log_slice:
            tokens = list(argv)
            if "--status" in tokens:
                idx = tokens.index("--status")
                if idx + 1 < len(tokens) and tokens[idx + 1] == "completed":
                    raise AssertionError(
                        f"skill wl invocation set --status completed: {argv}"
                    )

        wi = _wl_show(worklog_dir, item_id, worklog_dir)
        assert wi["status"] == "open", (
            f"status must stay open after the skill run, got {wi['status']!r}"
        )
        assert wi["stage"] == stage, (
            f"stage must stay {stage!r} after the skill run, got {wi['stage']!r}"
        )
        assert wi["effort"] == "Small", (
            f"effort field should be set to 'Small', got {wi['effort']!r}"
        )
        assert wi["risk"] in ("Low", "Medium", "High", "Severe"), (
            f"risk field should be set, got {wi['risk']!r}"
        )

    @pytest.mark.parametrize("stage", ["intake_complete", "plan_complete"])
    def test_run_skill_preserves_status(self, e2e_worklog, tmp_path, stage):
        """run_skill.py from a nested cwd keeps status=open at the given stage."""
        tmp_root, worklog_dir = e2e_worklog
        item_id = _create_item(tmp_root, worklog_dir, stage)

        # Run the skill from a NESTED subdir (not the worklog root) to
        # exercise cwd-independent worklog resolution.
        nested_cwd = tmp_root / "nested" / "work"
        nested_cwd.mkdir(parents=True)

        log_file = tmp_path / "wl_calls.jsonl"
        env = _install_wl_shim(tmp_path, log_file)

        rc, out, err = _run(
            [
                sys.executable, str(RUN_SKILL),
                "--issue", item_id,
                "--o", "1", "--m", "2", "--p", "3",
                "--coord", "1", "--review", "1",
                "--testing", "1", "--risk_buffer", "1",
                "--certainty", "85",
            ],
            cwd=nested_cwd,
            env=env,
        )
        assert rc == 0, f"run_skill.py failed (rc={rc}): {out} {err}"

        self._assert_skill_success(out)

        log_lines = log_file.read_text().splitlines()
        log_slice = [json.loads(line) for line in log_lines]
        self._assert_status_preserved(worklog_dir, item_id, stage, env, log_slice)

    def test_orchestrator_direct_preserves_status(self, e2e_worklog, tmp_path):
        """The orchestrator itself never changes status.

        Run ``orchestrate_estimate.py`` directly via stdin JSON (bypassing the
        run_skill.py wrapper) and verify the item still stays ``open``.
        """
        tmp_root, worklog_dir = e2e_worklog
        item_id = _create_item(tmp_root, worklog_dir, "intake_complete")

        nested_cwd = tmp_root / "nested"
        nested_cwd.mkdir(parents=True)

        payload = {
            "issue_id": item_id,
            "o": 2, "m": 4, "p": 6,
            "overheads": {
                "coordination": 1, "review": 1,
                "testing": 1, "risk_buffer": 1,
            },
            "parent": {"probability": 3, "impact": 3},
            "children": [],
            "certainty": 85,
            "assumptions": [],
            "unknowns": [],
        }

        log_file = tmp_path / "wl_calls_orch.jsonl"
        env = _install_wl_shim(tmp_path, log_file)

        rc, out, err = _run(
            [sys.executable, str(ORCHESTRATOR)],
            cwd=nested_cwd,
            env=env,
            input_text=json.dumps(payload),
        )
        assert rc == 0, f"orchestrator failed (rc={rc}): {out} {err}"

        output = json.loads(out)
        assert output.get("update_result", {}).get("success") is True
        assert output.get("comment_result", {}).get("success") is True

        log_lines = log_file.read_text().splitlines()
        log_slice = [json.loads(line) for line in log_lines]
        self._assert_status_preserved(worklog_dir, item_id, "intake_complete", env, log_slice)
