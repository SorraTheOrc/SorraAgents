"""Tests for skill/shared/measure_fanout.py -- process fan-out measurement.

Validates acceptance criteria from SA-0MSAK2G62002Z1RJ (Fan-out source map +
baseline harness):
- AC2: Measurement script records timestamp, per-type process counts (pi,
  vitest/tinypool, wl sync), load average, and swap usage.
- AC3: Script outputs machine-readable JSON and a human summary; runs
  non-intrusively (no side effects).
- AC4: Baseline run can be captured as a JSON artifact.

The script is intentionally read-only: it inspects /proc via psutil and
prints results. Tests avoid touching real system state by patching psutil
and os.getloadavg where classification logic is under test.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "skill" / "shared" / "measure_fanout.py"

# ---------------------------------------------------------------------------
# Presence / CLI contract
# ---------------------------------------------------------------------------


def test_script_exists():
    """The script must exist at the expected path."""
    assert SCRIPT_PATH.exists(), f"Script not found at {SCRIPT_PATH}"



def test_help_flag():
    """Script --help should display usage and exit 0."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"--help failed: {result.stderr}"
    assert "usage:" in result.stdout.lower() or "usage:" in result.stderr.lower()


def test_default_output_is_json():
    """Running without flags emits a single JSON document on stdout."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"script failed: {result.stderr}"
    parsed = json.loads(result.stdout)
    assert isinstance(parsed, dict)


def test_json_contains_required_keys():
    """JSON output must include timestamp, load, swap, and process counts."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"script failed: {result.stderr}"
    parsed = json.loads(result.stdout)

    # AC2: timestamp
    assert "timestamp" in parsed
    assert "processes" in parsed
    # AC2: per-type process counts
    proc = parsed["processes"]
    for key in ("pi", "node", "vitest", "wl_sync"):
        assert key in proc, f"missing process count for {key}"
    # AC2: load average
    assert "load" in parsed
    assert set(parsed["load"].keys()) >= {"1min", "5min", "15min"}
    # AC2: swap usage
    assert "swap" in parsed
    assert set(parsed["swap"].keys()) >= {"total", "used"}


def test_human_summary_flag():
    """--human should emit readable text (not JSON)."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--human"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"script failed: {result.stderr}"
    assert "load" in result.stdout.lower()
    assert "pi" in result.stdout.lower()


def test_out_file_flag(tmp_path):
    """--out <file> writes the JSON artifact to the given path."""
    out_file = tmp_path / "fanout-baseline.json"
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--out", str(out_file)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"script failed: {result.stderr}"
    assert out_file.exists(), "JSON artifact not written"
    parsed = json.loads(out_file.read_text())
    assert "timestamp" in parsed


# ---------------------------------------------------------------------------
# Classification logic (patched psutil)
# ---------------------------------------------------------------------------


class _FakeProcess:
    """Minimal psutil.Process stand-in exposing name() and cmdline()."""

    def __init__(self, name, cmdline):
        self._name = name
        self._cmdline = cmdline
        self.info = {"name": name, "cmdline": list(cmdline)}

    def name(self):
        return self._name

    def cmdline(self):
        return list(self._cmdline)


def _run_classify(processes, monkeypatch):
    """Run the script's classification over fake processes and return counts."""
    import skill.shared.measure_fanout as mf

    monkeypatch.setattr(
        mf.psutil,
        "process_iter",
        lambda attrs=None: [_FakeProcess(n, c) for n, c in processes],
    )
    return mf.classify_processes()


def test_classify_pi_processes(monkeypatch):
    """pi invocations (cmdline contains 'pi -p') count under 'pi'."""
    counts = _run_classify(
        [
            ("pi", ["pi", "-p", "--mode", "json", "--model", "x", "prompt"]),
            ("node", ["node", "/usr/local/bin/pi", "-p"]),
        ],
        monkeypatch,
    )
    assert counts["pi"] == 2
    assert counts["node"] == 0  # node wrapper counted as pi, not node


def test_classify_node_and_tinypool(monkeypatch):
    """vitest/tinypool workers count under 'vitest' key; plain node under node."""
    counts = _run_classify(
        [
            ("node", ["node", "/repo/node_modules/.bin/vitest"]),
            ("node", ["node", "/repo/node_modules/tinypool/dist/worker.js"]),
            ("node", ["node", "server.js"]),
        ],
        monkeypatch,
    )
    assert counts["vitest"] == 2  # vitest + tinypool worker
    assert counts["node"] == 1


def test_classify_wl_sync(monkeypatch):
    """wl sync invocations count under 'wl_sync'."""
    counts = _run_classify(
        [
            ("node", ["node", "/usr/local/lib/node_modules/worklog/dist/cli.js", "sync"]),
            ("node", ["node", "/usr/local/lib/node_modules/worklog/dist/cli.js", "list"]),
        ],
        monkeypatch,
    )
    assert counts["wl_sync"] == 1
    assert counts["node"] == 1


def test_classify_audit_runner(monkeypatch):
    """audit_runner.py subprocesses count under 'audit'."""
    counts = _run_classify(
        [
            ("python3", ["python3", "/home/rgardler/.pi/agent/skills/audit/scripts/audit_runner.py", "issue", "SA-1"]),
        ],
        monkeypatch,
    )
    assert counts["audit"] == 1


def test_classify_unknown_processes_ignored(monkeypatch):
    """Unrelated processes (vim, sshd) are not counted."""
    counts = _run_classify(
        [
            ("vim", ["vim", "notes.md"]),
            ("sshd", ["sshd", "-D"]),
        ],
        monkeypatch,
    )
    assert counts == {
        "pi": 0,
        "node": 0,
        "vitest": 0,
        "wl_sync": 0,
        "audit": 0,
    }


def test_non_intrusive_no_side_effects(monkeypatch, tmp_path):
    """Running the script must not create files (except explicit --out)."""
    import skill.shared.measure_fanout as mf

    monkeypatch.setattr(
        mf.psutil,
        "process_iter",
        lambda attrs=None: [_FakeProcess("vim", ["vim", "x"])],
    )
    monkeypatch.setattr(mf.os, "getloadavg", lambda: (0.1, 0.2, 0.3))
    monkeypatch.setattr(mf.time, "strftime", lambda *a, **k: "2026-08-01T00:00:00Z")

    # No --out: nothing should be written anywhere under tmp_path
    before = set(str(p) for p in tmp_path.rglob("*"))
    mf.collect()
    after = set(str(p) for p in tmp_path.rglob("*"))
    assert before == after


# ---------------------------------------------------------------------------
# Source-map doc (AC #1)
# ---------------------------------------------------------------------------

SOURCE_MAP_PATH = REPO_ROOT / "docs" / "dev" / "audit-fan-out-source-map.md"


def test_source_map_exists():
    """The fan-out source map doc must exist."""
    assert SOURCE_MAP_PATH.exists(), f"Source map not found at {SOURCE_MAP_PATH}"


def test_source_map_documents_audit_pi_spawn():
    """Doc must reference audit_runner.py _call_pi subprocess.Popen site."""
    text = SOURCE_MAP_PATH.read_text()
    assert "audit_runner.py" in text
    assert "L592" in text or "subprocess.Popen" in text


def test_source_map_documents_child_trigger():
    """Doc must reference the nested child-audit trigger path."""
    text = SOURCE_MAP_PATH.read_text()
    assert "L2665" in text or "child-trigger" in text
    assert "--force" in text


def test_source_map_documents_vitest_configs():
    """Doc must reference both vitest configs and the cap gap."""
    text = SOURCE_MAP_PATH.read_text()
    assert "vitest.config.ts" in text or "vite.config.ts" in text
    assert "maxWorkers" in text


def test_source_map_documents_wl_sync():
    """Doc must reference wl sync and its lack of process-level lock."""
    text = SOURCE_MAP_PATH.read_text()
    assert "wl sync" in text
    assert "sync.ts" in text


def test_source_map_documents_batch_skills():
    """Doc must reference implementall/intakeall/planall overlap."""
    text = SOURCE_MAP_PATH.read_text()
    assert "implementall" in text
    assert "intakeall" in text
    assert "planall" in text


def test_source_map_lists_related_work_items():
    """Doc must reference the parent and the six control child items."""
    text = SOURCE_MAP_PATH.read_text()
    for wid in (
        "SA-0MSAEKOQE009TEB4",
        "SA-0MSAK2G62002Z1RJ",
        "SA-0MSAK2L5P0066GW8",
        "SA-0MSAK2P3J0065POO",
        "SA-0MSAK2SNN005HCM5",
        "SA-0MSAK2W0F0027ZP7",
        "SA-0MSAK2ZH6009Z3TW",
        "SA-0MSAK33NN008NS95",
    ):
        assert wid in text, f"Missing work item ref {wid} in source map"


def test_baseline_artifact_exists():
    """A pre-control baseline JSON artifact must be committed for AC #4."""
    artifact = REPO_ROOT / "docs" / "dev" / "fanout-baseline-pre-control.json"
    assert artifact.exists(), f"Baseline artifact not found at {artifact}"
    parsed = json.loads(artifact.read_text())
    assert "load" in parsed
    assert "processes" in parsed
