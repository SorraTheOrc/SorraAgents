import json
import json
import subprocess

from ampa.selection import select_candidate


def _make_proc(payload, returncode=0):
    stdout = json.dumps(payload)
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=""
    )


def test_selection_returns_first_candidate():
    def run_shell(cmd, shell, check, capture_output, text, cwd, timeout):
        assert cmd == "wl next --json"
        return _make_proc(
            {
                "items": [
                    {
                        "id": "SA-2",
                        "status": "open",
                        "priority": 1,
                        "updated_at": "2026-02-01T00:00:00+00:00",
                    },
                    {
                        "id": "SA-1",
                        "status": "open",
                        "priority": 1,
                        "updated_at": "2026-01-01T00:00:00+00:00",
                    },
                ]
            }
        )

    selected = select_candidate(run_shell=run_shell)
    assert selected is not None
    assert selected["id"] == "SA-2"


def test_selection_returns_first_even_if_blocked():
    def run_shell(cmd, shell, check, capture_output, text, cwd, timeout):
        assert cmd == "wl next --json"
        return _make_proc(
            {
                "items": [
                    {"id": "SA-1", "status": "open", "priority": 2, "blocked": True},
                    {
                        "id": "SA-2",
                        "status": "ready",
                        "priority": 2,
                        "tags": ["skip"],
                    },
                    {"id": "SA-3", "status": "open", "priority": 1},
                ]
            }
        )

    selected = select_candidate(run_shell=run_shell)
    assert selected is not None
    assert selected["id"] == "SA-1"


def test_selection_returns_none_when_empty():
    def run_shell(cmd, shell, check, capture_output, text, cwd, timeout):
        assert cmd == "wl next --json"
        return _make_proc({"items": []})

    selected = select_candidate(run_shell=run_shell)
    assert selected is None
