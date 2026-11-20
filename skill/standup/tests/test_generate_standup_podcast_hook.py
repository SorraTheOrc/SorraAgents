#!/usr/bin/env python3
"""Tests: standup skill podcast post-step hook (opt-in, non-fatal, idempotent).

Test-first contract for SA-0MWJJQEU80008MW0 (implementation child). The hook
invokes ContentCreator's per-project standup→podcast entry point
(``scripts/standup-podcast.sh``) after the dated report is written and
committed.

Contract pinned here:

* Opt-in via ``STANDUP_PODCAST=1``; unset or ``0`` leaves the hook dormant.
* ``--no-sync``, ``--no-commit`` and ``--json``-to-stdout runs never trigger it.
* Invocation is ``--project <slug> --report <report path>``, with the project
  auto-detected from the worklog's ``projectName``.
* Every failure mode (missing ContentCreator checkout, non-zero exit) is
  non-fatal: the standup still writes its report and exits 0.
* An already-produced date (``standup-podcast-result: up-to-date``) is an
  idempotent no-op.

The ContentCreator side is integration-tested in ContentCreator; these are
unit-level contract tests.
"""  # noqa: EXE001
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
_SKILLS_ROOT = REPO_ROOT / "skill"
if str(_SKILLS_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILLS_ROOT))

_MODULE_NAME = "generate_standup_post_step"

# Test-first: the hook itself lands in SA-0MWJJQEU80008MW0. ``strict=True``
# makes the suite fail (XPASS) once it does, which is the signal to drop this
# marker in the implementation child.
pytestmark = pytest.mark.xfail(
    strict=True,
    reason="pending post-step hook implementation SA-0MWJJQEU80008MW0",
)

#: The hook's public surface — absent until the implementation child lands.
_HOOK_API = ("PODCAST_OPT_IN_ENV", "podcast_hook_enabled", "publish_podcast_episode")


def _reload_standup():
    """Import a fresh generate_standup module from the repo skill tree."""
    os.environ.pop("WL_WORKLOG_DIR", None)
    spec_path = REPO_ROOT / "skill" / "standup" / "scripts" / "generate_standup.py"
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, str(spec_path))
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _cleanup_standup_env():
    os.environ.pop("WL_WORKLOG_DIR", None)
    os.environ.pop("STANDUP_PODCAST", None)
    os.environ.pop("STANDUP_PODCAST_SCRIPT", None)


def _fake_run_wl_empty_git_fail(mod, wl_json=None):
    """Patch mod.subprocess.run so every wl call returns empty data and git fails."""
    wl_json = wl_json or json.dumps({"results": [], "workItems": []})

    def _fake(cmd, *args, **kwargs):
        if isinstance(cmd, list) and cmd[0] == "wl":
            return subprocess.CompletedProcess(cmd, 0, wl_json, "")
        if isinstance(cmd, list) and cmd[0] == "git":
            return subprocess.CompletedProcess(cmd, 1, "", "not a git repo")
        return subprocess.CompletedProcess(cmd if isinstance(cmd, list) else [cmd], 1, "", "")

    return mock.patch.object(mod.subprocess, "run", side_effect=_fake)


def _project(tmp_path: Path, name: str = "ContextHub") -> Path:
    """Create a project root with a worklog config carrying projectName."""
    root = tmp_path / name
    worklog = root / ".worklog"
    worklog.mkdir(parents=True)
    (worklog / "config.yaml").write_text(f"projectName: {name}\nprefix: X\n", encoding="utf-8")
    return root


def _run_main(mod, argv, tmp_path):
    """Run mod.main() in isolation, capturing print output."""
    stderr: list[str] = []

    def capture_print(*args, **kwargs):
        if kwargs.get("file") is sys.stderr:
            stderr.append(" ".join(str(a) for a in args))

    with (
        mock.patch.object(sys, "argv", ["generate_standup.py", *argv]),
        _fake_run_wl_empty_git_fail(mod),
        mock.patch("builtins.print", side_effect=capture_print),
    ):
        return mod.main(), stderr


def _today() -> str:
    return (
        datetime.now(tz=timezone.utc)
        .astimezone()
        .replace(tzinfo=None)
        .strftime("%Y_%m_%d")
    )


class TestHookOptInGating:
    """The hook is dormant unless explicitly enabled."""

    def test_flag_unset_does_not_invoke_hook(self, tmp_path):
        root = _project(tmp_path)
        mod = _reload_standup()
        try:
            with mock.patch.object(mod, "publish_podcast_episode") as publish:
                rc, _ = _run_main(mod, ["--worklog-dir", str(root / ".worklog")], tmp_path)
            assert rc == 0
            publish.assert_not_called()
        finally:
            _cleanup_standup_env()

    def test_flag_zero_does_not_invoke_hook(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STANDUP_PODCAST", "0")
        root = _project(tmp_path)
        mod = _reload_standup()
        try:
            with mock.patch.object(mod, "publish_podcast_episode") as publish:
                rc, _ = _run_main(mod, ["--worklog-dir", str(root / ".worklog")], tmp_path)
            assert rc == 0
            publish.assert_not_called()
        finally:
            _cleanup_standup_env()

    def test_flag_one_invokes_hook_once_for_the_written_report(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STANDUP_PODCAST", "1")
        root = _project(tmp_path)
        mod = _reload_standup()
        try:
            with mock.patch.object(mod, "publish_podcast_episode") as publish:
                rc, _ = _run_main(mod, ["--worklog-dir", str(root / ".worklog")], tmp_path)
            assert rc == 0
            assert publish.call_count == 1
            report = publish.call_args.args[0]
            assert str(report).endswith(f"standups/{_today()}.md")
        finally:
            _cleanup_standup_env()

    @pytest.mark.parametrize(
        "argv",
        [
            ["--no-sync"],
            ["--no-commit"],
            ["--json"],
        ],
    )
    def test_non_triggering_modes_do_not_invoke_hook(self, tmp_path, monkeypatch, argv):
        monkeypatch.setenv("STANDUP_PODCAST", "1")
        root = _project(tmp_path)
        mod = _reload_standup()
        try:
            with mock.patch.object(mod, "publish_podcast_episode") as publish:
                rc, _ = _run_main(
                    mod, ["--worklog-dir", str(root / ".worklog"), *argv], tmp_path
                )
            assert rc == 0
            publish.assert_not_called()
        finally:
            _cleanup_standup_env()


class TestInvocationContract:
    """When enabled, the hook calls ContentCreator with a stable contract."""

    def _publish(self, mod, report: Path, root: Path, runner, env=None):
        return mod.publish_podcast_episode(
            report,
            project_root=root,
            env=env if env is not None else {"STANDUP_PODCAST": "1"},
            runner=runner,
        )

    def test_calls_entry_point_with_project_and_report_flags(self, tmp_path):
        root = _project(tmp_path)
        report = root / "standups" / "2026_11_13.md"
        report.parent.mkdir(parents=True)
        report.write_text("# Standup Report\n", encoding="utf-8")
        captured: list[list[str]] = []

        def runner(cmd):
            captured.append(list(cmd))
            return (0, "standup-podcast-result: produced", "")

        mod = _reload_standup()
        try:
            status = self._publish(mod, report, root, runner)
        finally:
            _cleanup_standup_env()

        assert status["status"] == "published"
        assert len(captured) == 1
        cmd = captured[0]
        assert cmd[0] in ("bash", "sh") or cmd[0].endswith("standup-podcast.sh")
        assert any(part.endswith("ContentCreator/scripts/standup-podcast.sh") for part in cmd)
        assert "--project" in cmd
        assert cmd[cmd.index("--project") + 1] == "contexthub"
        assert "--report" in cmd
        assert cmd[cmd.index("--report") + 1].endswith("2026_11_13.md")

    def test_already_produced_is_an_idempotent_no_op(self, tmp_path):
        root = _project(tmp_path)
        report = root / "standups" / "2026_11_13.md"
        report.parent.mkdir(parents=True)
        report.write_text("# Standup Report\n", encoding="utf-8")

        mod = _reload_standup()
        try:
            status = self._publish(
                mod, report, root, lambda cmd: (0, "standup-podcast-result: up-to-date", "")
            )
        finally:
            _cleanup_standup_env()
        assert status["status"] == "up-to-date"
        assert report.is_file()

    def test_missing_contentcreator_checkout_is_a_non_fatal_skip(self, tmp_path):
        root = _project(tmp_path)  # no sibling ../ContentCreator
        report = root / "standups" / "2026_11_13.md"
        report.parent.mkdir(parents=True)
        report.write_text("# Standup Report\n", encoding="utf-8")
        called: list[list[str]] = []

        mod = _reload_standup()
        try:
            status = self._publish(mod, report, root, lambda cmd: called.append(cmd) or (1, "", ""))
        finally:
            _cleanup_standup_env()
        assert status["status"] == "skipped"
        assert called == []

    def test_nonzero_exit_is_recorded_and_never_raises(self, tmp_path):
        root = _project(tmp_path)
        (root.parent / "ContentCreator" / "scripts").mkdir(parents=True)
        (root.parent / "ContentCreator" / "scripts" / "standup-podcast.sh").write_text(
            "#!/usr/bin/env bash\nexit 1\n", encoding="utf-8"
        )
        report = root / "standups" / "2026_11_13.md"
        report.parent.mkdir(parents=True)
        report.write_text("# Standup Report\n", encoding="utf-8")

        mod = _reload_standup()
        try:
            status = self._publish(mod, report, root, lambda cmd: (1, "", "boom"))
        finally:
            _cleanup_standup_env()
        assert status["status"] == "failed"

    def test_runner_exception_is_swallowed(self, tmp_path):
        root = _project(tmp_path)
        (root.parent / "ContentCreator" / "scripts").mkdir(parents=True)
        (root.parent / "ContentCreator" / "scripts" / "standup-podcast.sh").write_text(
            "#!/usr/bin/env bash\nexit 0\n", encoding="utf-8"
        )
        report = root / "standups" / "2026_11_13.md"
        report.parent.mkdir(parents=True)
        report.write_text("# Standup Report\n", encoding="utf-8")

        def boom(cmd):
            raise OSError("no such file")

        mod = _reload_standup()
        try:
            status = self._publish(mod, report, root, boom)
        finally:
            _cleanup_standup_env()
        assert status["status"] == "failed"


class TestHookIsNonFatalToTheStandup:
    """A broken podcast pipeline must never break the standup itself."""

    def test_failing_hook_leaves_the_report_and_exit_code_intact(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STANDUP_PODCAST", "1")
        root = _project(tmp_path)
        mod = _reload_standup()
        try:
            with mock.patch.object(
                mod,
                "publish_podcast_episode",
                side_effect=RuntimeError("pipeline exploded"),
            ):
                rc, _ = _run_main(mod, ["--worklog-dir", str(root / ".worklog")], tmp_path)
            assert rc == 0
            assert (root / "standups" / f"{_today()}.md").is_file()
        finally:
            _cleanup_standup_env()

    def test_hook_runs_after_the_report_is_written(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STANDUP_PODCAST", "1")
        root = _project(tmp_path)
        mod = _reload_standup()
        seen: list[bool] = []

        def assert_written(report_path, *args, **kwargs):
            seen.append(Path(report_path).is_file())
            return {"status": "published"}

        try:
            with mock.patch.object(
                mod, "publish_podcast_episode", side_effect=assert_written
            ):
                rc, _ = _run_main(mod, ["--worklog-dir", str(root / ".worklog")], tmp_path)
            assert rc == 0
            assert seen == [True]
        finally:
            _cleanup_standup_env()


class TestHookApiPresent:
    """The module exposes the documented hook surface."""

    def test_module_exposes_the_hook_api(self):
        mod = _reload_standup()
        try:
            for name in _HOOK_API:
                assert hasattr(mod, name), f"generate_standup must expose {name}"
        finally:
            _cleanup_standup_env()

    def test_opt_in_constant_names_the_flag(self):
        mod = _reload_standup()
        try:
            assert mod.PODCAST_OPT_IN_ENV == "STANDUP_PODCAST"
        finally:
            _cleanup_standup_env()
