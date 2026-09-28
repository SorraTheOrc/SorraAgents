"""Behavioural tests for the git-identity guard used before ``wl sync``.

Covers SA-0MUJ2VMF7000UHCX AC4: the guard detects and reports a mismatched
``user.email`` *before* ``wl sync`` runs, so the Worklog author-identity gate's
refusal becomes actionable instead of a silent strand.

The guard is exercised end-to-end as a subprocess against throwaway git repos
created under ``tmp_path`` (never the live checkout), with an isolated ``HOME``
so the global identity is controlled by the test. Pure decision logic is tested
directly via :func:`scripts.check_git_identity.evaluate_identity` so every
reason can be driven without a repository.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
GUARD = REPO_ROOT / "scripts" / "check_git_identity.py"
HOOK = REPO_ROOT / ".githooks" / "pre-push"


def _load_guard_module():
    """Load the guard by file location.

    Deliberately *not* ``import scripts.check_git_identity``: the repo-root
    ``scripts`` package collides with ``skill/scripts`` (``failure_notice``,
    ``pi_utils``) and importing it here would shadow that package for the
    audit-runner imports in ``tests/conftest.py``.
    """
    spec = importlib.util.spec_from_file_location("_check_git_identity", GUARD)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError(f"cannot load guard from {GUARD}")
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves ``cls.__module__`` via sys.modules during exec.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_guard = _load_guard_module()
IdentityState = _guard.IdentityState
evaluate_identity = _guard.evaluate_identity

#: Repository-override variables that would redirect git away from the
#: fixture repo (the 2026-09-26 incident mechanism). Strip them so the tests
#: can never touch the live checkout.
_OVERRIDE_VARS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_CONFIG",
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_SYSTEM",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
)


def _env(home: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _OVERRIDE_VARS}
    env["HOME"] = str(home)
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env.pop("WORKLOG_EXPECTED_EMAIL", None)
    return env


def _git(repo: Path, *args: str, env: dict[str, str]) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )


def _init_repo(repo: Path, env: dict[str, str]) -> None:
    subprocess.run(
        ["git", "init", "-q", str(repo)],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )


def _run_guard(
    repo: Path,
    env: dict[str, str],
    *extra_args: str,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(GUARD), "--repo-root", str(repo), *extra_args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def _make_repo(
    tmp_path: Path,
    *,
    global_email: str | None = "agent@example.com",
    local_email: str | None = None,
    local_name: str | None = None,
) -> tuple[Path, dict[str, str]]:
    home = tmp_path / "home"
    home.mkdir()
    env = _env(home)
    if global_email is not None:
        subprocess.run(
            ["git", "config", "--global", "user.email", global_email],
            check=True, capture_output=True, text=True, env=env,
        )
        subprocess.run(
            ["git", "config", "--global", "user.name", "Agent"],
            check=True, capture_output=True, text=True, env=env,
        )
    repo = tmp_path / "repo"
    _init_repo(repo, env)
    if local_email is not None:
        _git(repo, "config", "--local", "user.email", local_email, env=env)
    if local_name is not None:
        _git(repo, "config", "--local", "user.name", local_name, env=env)
    return repo, env


# ── End-to-end: the guard fires on a mismatched identity ────────────────────


class TestGuardFiresOnMismatch:
    def test_incident_local_override_is_rejected(self, tmp_path: Path):
        """A local override of the global identity (the incident signature)
        must fail with an actionable remedy."""
        repo, env = _make_repo(
            tmp_path, global_email="agent@example.com",
            local_email="t@t.com", local_name="T",
        )
        result = _run_guard(repo, env)
        assert result.returncode == 1, result.stdout + result.stderr
        combined = result.stdout + result.stderr
        assert "t@t.com" in combined
        assert "agent@example.com" in combined
        assert "git config --local --unset" in combined
        assert "SA-0MUJ2VMF7000UHCX" in combined

    def test_expected_email_mismatch_is_rejected(self, tmp_path: Path):
        """When an expected email is supplied, a different configured email
        must fail."""
        repo, env = _make_repo(
            tmp_path, global_email=None, local_email="wrong@example.com",
        )
        result = _run_guard(repo, env, "--expected-email", "canonical@example.com")
        assert result.returncode == 1, result.stdout + result.stderr
        assert "wrong@example.com" in result.stdout + result.stderr
        assert "canonical@example.com" in result.stdout + result.stderr

    def test_empty_email_is_rejected(self, tmp_path: Path):
        """No configured email at all must fail (the gate cannot match)."""
        repo, env = _make_repo(tmp_path, global_email=None)
        result = _run_guard(repo, env)
        assert result.returncode == 1
        assert "not configured" in result.stdout + result.stderr

    def test_bare_repo_is_rejected(self, tmp_path: Path):
        """A leaked ``git init --bare`` flips core.bare; the guard must flag it."""
        home = tmp_path / "home"
        home.mkdir()
        env = _env(home)
        repo = tmp_path / "bare.git"
        subprocess.run(
            ["git", "init", "-q", "--bare", str(repo)],
            check=True, capture_output=True, text=True, env=env,
        )
        result = _run_guard(repo, env)
        assert result.returncode == 1
        assert "bare" in (result.stdout + result.stderr).lower()


# ── End-to-end: the guard stays quiet on a healthy identity ─────────────────


class TestGuardPassesOnHealthyIdentity:
    def test_no_local_override_passes(self, tmp_path: Path):
        """A checkout using only the global identity is fine."""
        repo, env = _make_repo(tmp_path, global_email="agent@example.com")
        result = _run_guard(repo, env)
        assert result.returncode == 0, result.stdout + result.stderr

    def test_matching_local_override_passes(self, tmp_path: Path):
        """A local override equal to the global identity is not a mismatch."""
        repo, env = _make_repo(
            tmp_path, global_email="agent@example.com",
            local_email="agent@example.com", local_name="Agent",
        )
        result = _run_guard(repo, env)
        assert result.returncode == 0, result.stdout + result.stderr

    def test_expected_email_match_passes(self, tmp_path: Path):
        """A configured email equal to the expected one passes."""
        repo, env = _make_repo(
            tmp_path, global_email="canonical@example.com",
        )
        result = _run_guard(repo, env, "--expected-email", "canonical@example.com")
        assert result.returncode == 0, result.stdout + result.stderr

    def test_non_git_root_is_fail_open(self, tmp_path: Path):
        """A non-git root is a no-op (the guard must not block unrelated use)."""
        not_repo = tmp_path / "plain"
        not_repo.mkdir()
        home = tmp_path / "home"
        home.mkdir()
        result = _run_guard(not_repo, _env(home))
        assert result.returncode == 0


# ── JSON contract (CLI/API work must offer JSON) ────────────────────────────


class TestJsonOutput:
    def test_json_reports_failure_reasons(self, tmp_path: Path):
        repo, env = _make_repo(
            tmp_path, global_email="agent@example.com", local_email="t@t.com",
        )
        result = _run_guard(repo, env, "--json")
        assert result.returncode == 1
        payload = json.loads(result.stdout)
        assert payload["ok"] is False
        assert payload["user_email"] == "t@t.com"
        assert payload["global_email"] == "agent@example.com"
        assert "incident-identity" in payload["reasons"]

    def test_json_reports_success(self, tmp_path: Path):
        repo, env = _make_repo(tmp_path, global_email="agent@example.com")
        result = _run_guard(repo, env, "--json")
        assert result.returncode == 0
        payload = json.loads(result.stdout)
        assert payload["ok"] is True
        assert payload["reasons"] == []


# ── Pure decision logic coverage ────────────────────────────────────────────


def _state(**overrides) -> IdentityState:
    base = {
        "repo_root": "/repo",
        "core_bare": False,
        "effective_name": "Agent",
        "effective_email": "agent@example.com",
        "local_name": None,
        "local_email": None,
        "global_name": "Agent",
        "global_email": "agent@example.com",
        "expected_email": None,
    }
    base.update(overrides)
    return IdentityState(**base)


class TestEvaluateIdentity:
    def test_clean_state_has_no_reasons(self):
        report = evaluate_identity(_state())
        assert report.ok is True
        assert report.failures == ()

    def test_local_override_differing_from_global_fails(self):
        report = evaluate_identity(
            _state(local_email="other@example.com", effective_email="other@example.com")
        )
        assert report.ok is False
        assert "local-override" in report.failures

    def test_incident_sentinel_fails_even_without_global(self):
        report = evaluate_identity(
            _state(
                global_email=None, global_name=None,
                local_email="t@t.com", effective_email="t@t.com",
            )
        )
        assert report.ok is False
        assert "incident-identity" in report.failures

    def test_expected_email_takes_priority(self):
        report = evaluate_identity(
            _state(expected_email="canonical@example.com")
        )
        assert report.ok is False
        assert "expected-mismatch" in report.failures

    def test_missing_global_identity_is_only_a_warning(self):
        report = evaluate_identity(
            _state(global_email=None, global_name=None)
        )
        assert report.ok is True
        assert "no-global-identity" in report.warnings


# ── Pre-push wiring: the guard runs before `wl sync` ────────────────────────


class TestPrePushWiring:
    """The pre-push hook must run the guard before invoking `wl sync`.

    A fake ``wl`` records every invocation so the tests can assert that a
    mismatched identity skips the sync while a healthy identity runs it.
    """

    def _setup_hook_repo(
        self, tmp_path: Path, *, local_email: str | None, global_email: str | None
    ) -> tuple[Path, dict[str, str], Path]:
        home = tmp_path / "home"
        home.mkdir()
        env = _env(home)
        if global_email is not None:
            subprocess.run(
                ["git", "config", "--global", "user.email", global_email],
                check=True, capture_output=True, text=True, env=env,
            )
        repo = tmp_path / "repo"
        _init_repo(repo, env)
        if local_email is not None:
            _git(repo, "config", "--local", "user.email", local_email, env=env)

        hooks_dir = repo / ".git" / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        hook_dst = hooks_dir / "pre-push"
        hook_dst.write_text(HOOK.read_text(encoding="utf-8"), encoding="utf-8")
        hook_dst.chmod(hook_dst.stat().st_mode | stat.S_IEXEC)

        guard_dst = repo / "scripts" / "check_git_identity.py"
        guard_dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(GUARD, guard_dst)

        marker = tmp_path / "wl-invocations.txt"
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        fake_wl = bin_dir / "wl"
        fake_wl.write_text(
            "#!/bin/sh\n"
            'echo "$@" >> "$WL_MARKER"\n'
            "exit 0\n",
            encoding="utf-8",
        )
        fake_wl.chmod(fake_wl.stat().st_mode | stat.S_IEXEC)
        env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
        env.update(
            {
                "CONTEXT_BUDGET_SKIP": "1",
                "BRANCH_POLICY_SKIP": "1",
                "TEST_SCOPE_SKIP": "1",
                "WL_MARKER": str(marker),
            }
        )
        return repo, env, marker

    def _run_hook(self, repo: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["sh", str(repo / ".git" / "hooks" / "pre-push")],
            cwd=str(repo),
            input="refs/heads/wl-x 0000 refs/heads/dev 1111\n",
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )

    def test_mismatched_identity_skips_wl_sync(self, tmp_path: Path):
        repo, env, marker = self._setup_hook_repo(
            tmp_path, local_email="t@t.com", global_email="agent@example.com",
        )
        result = self._run_hook(repo, env)
        assert result.returncode == 0, result.stdout + result.stderr
        assert not marker.exists(), "wl sync must be skipped on a mismatched identity"
        assert "git identity mismatch" in result.stderr

    def test_healthy_identity_runs_wl_sync(self, tmp_path: Path):
        repo, env, marker = self._setup_hook_repo(
            tmp_path, local_email=None, global_email="agent@example.com",
        )
        result = self._run_hook(repo, env)
        assert result.returncode == 0, result.stdout + result.stderr
        assert marker.exists(), result.stderr
        assert "sync" in marker.read_text(encoding="utf-8")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
