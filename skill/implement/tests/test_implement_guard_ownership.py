"""Guard-ownership parity at the implement.py ↔ canonical-runner boundary.

Covers SA-0MUH1MJL6003767Q: ``implement.py``'s ``run_tests`` must not arm its
own live-repo guard. Instead it delegates suite execution to the canonical
``run_tests.guarded_run_all`` (the single source of truth for outer guard
ownership), so a green finish gate always implies the checkout was verified
for mutation — even when ``LIVE_REPO_GUARD_ACTIVE`` is already set in the
environment (a nested run, where ``guarded_run_all`` stands down because an
outer guard owns the checkout).

The tests patch the delegation boundary (``_guarded_run_all``) and assert the
observable contract: the resolved commands/scope are forwarded, the adapted
result keeps implement.py's shape, a ``live_repo_mutation`` from the owning
guard fails the gate, and a partial install degrades to the direct path.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) in sys.path:
    sys.path.remove(str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT))

from skill.implement.scripts import implement


def _guarded_result(commands, scope="full", *, success=True, mutation=None, failures=None):
    """A canned ``guarded_run_all`` result (the ``run_all`` shape)."""
    return {
        "success": success and mutation is None,
        "suites": {
            "all": {
                "returncode": 0 if (success and mutation is None) else 1,
                "success": success and mutation is None,
                "failures": failures or [],
            }
        },
        "failures": failures or [],
        "notices": [],
        "scope": scope,
        "type": "full",
        "resolved_scopes": [scope],
        **({"live_repo_mutation": mutation} if mutation else {}),
    }


def _repo_with_suite(tmp_path: Path, commands: list[str]) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".pi").mkdir()
    (repo / ".pi" / "test-config.json").write_text(
        json.dumps({"suiteCommands": commands}), encoding="utf-8"
    )
    return repo


def test_run_tests_delegates_to_guarded_run_all(monkeypatch, tmp_path):
    """The test-skill path routes execution through ``guarded_run_all``."""
    repo = _repo_with_suite(tmp_path, ["pytest -q -r a --disable-warnings"])
    captured: dict[str, object] = {}

    def fake_guarded_run_all(cwd=None, timeout=600, *, commands=None, scope="full",
                             base_ref="origin/dev", use_cache=True, **kwargs):
        captured["cwd"] = cwd
        captured["commands"] = list(commands or [])
        captured["scope"] = scope
        return _guarded_result(commands, scope=scope)

    monkeypatch.setattr(implement, "_guarded_run_all", fake_guarded_run_all)

    result = implement.run_tests(str(repo), scope="full")

    assert captured["cwd"] == repo
    assert captured["commands"] == ["pytest -q -r a --disable-warnings"]
    assert captured["scope"] == "full"
    assert result["success"] is True
    assert result["tooling"] == "suite"
    assert result["scope"] == "full"


def test_run_tests_forwards_changed_scope(monkeypatch, tmp_path):
    """A changed-scope run keeps its scope metadata through the delegation."""
    repo = _repo_with_suite(tmp_path, ["pytest -q -r a --disable-warnings"])
    captured: dict[str, object] = {}

    def fake_guarded_run_all(cwd=None, timeout=600, *, commands=None, scope="full",
                             base_ref="origin/dev", use_cache=True, **kwargs):
        captured["scope"] = scope
        return _guarded_result(commands, scope=scope)

    monkeypatch.setattr(implement, "_guarded_run_all", fake_guarded_run_all)
    monkeypatch.setattr(
        implement,
        "_changed_scope_commands",
        lambda *a, **k: ["pytest -q -r a --disable-warnings tests/test_foo.py"],
    )

    result = implement.run_tests(str(repo), scope="changed")

    assert captured["scope"] == "changed"
    assert result["scope"] == "changed"


def test_live_repo_mutation_fails_the_gate(monkeypatch, tmp_path):
    """A mutation reported by the owning guard fails the finish gate."""
    repo = _repo_with_suite(tmp_path, ["pytest -q -r a --disable-warnings"])

    def fake_guarded_run_all(cwd=None, timeout=600, *, commands=None, scope="full",
                             base_ref="origin/dev", use_cache=True, **kwargs):
        return _guarded_result(
            commands,
            scope=scope,
            mutation="live-repo mutation detected: refs/heads/intruder",
        )

    monkeypatch.setattr(implement, "_guarded_run_all", fake_guarded_run_all)

    result = implement.run_tests(str(repo), scope="full")

    assert result["success"] is False
    assert "live_repo_mutation" in result


def test_structured_failures_are_flattened(monkeypatch, tmp_path):
    """``run_all`` failure dicts become implement.py's flat string list."""
    repo = _repo_with_suite(tmp_path, ["pytest -q -r a --disable-warnings"])

    def fake_guarded_run_all(cwd=None, timeout=600, *, commands=None, scope="full",
                             base_ref="origin/dev", use_cache=True, **kwargs):
        return _guarded_result(
            commands,
            scope=scope,
            success=False,
            failures=[{"test_name": "tests/test_x.py::test_y"}],
        )

    monkeypatch.setattr(implement, "_guarded_run_all", fake_guarded_run_all)

    result = implement.run_tests(str(repo), scope="full")

    assert result["success"] is False
    assert result["failures"] == ["tests/test_x.py::test_y"]


def test_partial_install_degrades_to_direct_path(monkeypatch, tmp_path):
    """Without the canonical runner, the direct cached path still runs."""
    repo = _repo_with_suite(tmp_path, ["pytest -q -r a --disable-warnings"])
    monkeypatch.setattr(implement, "_guarded_run_all", None)
    captured: list[str] = []

    def fake_run_cached(command, **kwargs):
        captured.append(command)
        return {
            "stdout": "",
            "stderr": "",
            "exit_code": 0,
            "completed_at": 0.0,
            "command": command,
            "git_state": "test",
            "cached": True,
        }

    monkeypatch.setattr(implement, "run_cached", fake_run_cached)

    result = implement.run_tests(str(repo), scope="full")

    assert captured == ["pytest -q -r a --disable-warnings"]
    assert result["success"] is True
