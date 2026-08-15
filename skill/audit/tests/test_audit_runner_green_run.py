from __future__ import annotations

import contextlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pytest

from skill.audit.scripts import audit_runner
from skill.test.scripts.run_tests import repo_has_pytest_suite


@pytest.fixture(autouse=True)
def _free_audit_slot():
    """Neutralize the host-wide audit semaphore for deterministic unit tests.

    ``_call_pi`` acquires the real cross-process audit semaphore before
    launching the (mocked) subprocess. Under concurrent audit load the
    semaphore can saturate, making these timing-path unit tests flaky (see
    SA-0MSCDC4750019G9Y, SA-0MSCDC76A007JCJK). Replace it with a
    null-context so the mocked return paths are exercised directly.

    The real semaphore behavior is covered separately by
    ``test_audit_runner_concurrency.py``.
    """
    with mock.patch.object(
        audit_runner, "_acquire_audit_slot", return_value=contextlib.nullcontext()
    ):
        yield

_GREEN_RUN_HEAD = "a1b2c3d4e5f67890abcdef1234567890abcdef12"

_GREEN_RUN_OTHER = "f1e2d3c4b5a67890fedcba0987654321fedcba98"

_GREEN_RUN_DESC = (
    "# Test\n\n## Acceptance Criteria\n\n"
    "- AC1: full project test suite passes with the new changes\n"
)

def _green_run_git_runner(head_sha: str | None = _GREEN_RUN_HEAD):
    """Build a mock runner answering ``git rev-parse HEAD``.

    *head_sha* of ``None`` simulates git being unavailable (non-zero rc).
    """
    mock_runner = mock.MagicMock()

    def _side_effect(cmd):
        if list(cmd[:2]) == ["git", "rev-parse"]:
            if head_sha is None:
                return SimpleNamespace(
                    returncode=128, stdout="", stderr="fatal: not a git repository"
                )
            return SimpleNamespace(returncode=0, stdout=head_sha + "\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    mock_runner.side_effect = _side_effect
    return mock_runner

class TestGreenRunResolution:
    """Unit tests for green-run value resolution and HEAD validation."""

    def test_env_var_constant_defined(self):
        """AC5: the AUDIT_GREEN_RUN env var constant is defined."""
        assert audit_runner.AUDIT_GREEN_RUN_ENV == "AUDIT_GREEN_RUN"

    def test_no_flag_no_env_no_attestation(self):
        """AC1: with neither flag nor env there is no attestation (unchanged)."""
        block, sha = audit_runner._resolve_green_run_attestation(
            None, _green_run_git_runner(),
        )
        assert block is None
        assert sha is None

    def test_head_alias_resolves_to_head_sha(self):
        """AC2/AC5: the HEAD alias resolves to the audited HEAD sha."""
        block, sha = audit_runner._resolve_green_run_attestation(
            "HEAD", _green_run_git_runner(),
        )
        assert sha == _GREEN_RUN_HEAD
        assert block is not None
        assert _GREEN_RUN_HEAD in block

    def test_exact_sha_match_accepted(self):
        """AC2/AC5: an exact sha matching the audited HEAD is accepted."""
        block, sha = audit_runner._resolve_green_run_attestation(
            _GREEN_RUN_HEAD, _green_run_git_runner(),
        )
        assert sha == _GREEN_RUN_HEAD
        assert block is not None

    def test_sha_mismatch_rejected(self, capsys):
        """AC3: a non-matching sha is rejected with an error naming both shas."""
        block, sha = audit_runner._resolve_green_run_attestation(
            _GREEN_RUN_OTHER, _green_run_git_runner(),
        )
        assert block is None
        assert sha is None
        err = capsys.readouterr().err
        assert _GREEN_RUN_OTHER in err
        assert _GREEN_RUN_HEAD in err
        assert "does not match" in err

    def test_env_var_fallback(self):
        """AC5: AUDIT_GREEN_RUN is used when no CLI flag is passed."""
        with mock.patch.dict(
            audit_runner.os.environ,
            {audit_runner.AUDIT_GREEN_RUN_ENV: _GREEN_RUN_HEAD},
            clear=False,
        ):
            block, sha = audit_runner._resolve_green_run_attestation(
                None, _green_run_git_runner(),
            )
        assert sha == _GREEN_RUN_HEAD
        assert block is not None

    def test_cli_flag_wins_over_env(self):
        """AC1: the --green-run CLI flag wins over the env var."""
        with mock.patch.dict(
            audit_runner.os.environ,
            {audit_runner.AUDIT_GREEN_RUN_ENV: _GREEN_RUN_OTHER},
            clear=False,
        ):
            block, sha = audit_runner._resolve_green_run_attestation(
                _GREEN_RUN_HEAD, _green_run_git_runner(),
            )
        assert sha == _GREEN_RUN_HEAD
        assert block is not None

    def test_git_unavailable_rejected(self, capsys):
        """A green-run value cannot be verified without git → rejected.

        Fail-closed: without the audited HEAD the attestation is never
        silently accepted, so execution-dependent ACs stay partial.
        """
        block, sha = audit_runner._resolve_green_run_attestation(
            "HEAD", _green_run_git_runner(head_sha=None),
        )
        assert block is None
        assert sha is None
        assert "could not be resolved" in capsys.readouterr().err

    def test_prompt_block_keeps_read_only_mandate(self):
        """AC7: the block permits met-on-attestation but defers suite
        execution to the runner (no universal 'Do NOT execute the test suite'
        rule)."""
        block, _ = audit_runner._resolve_green_run_attestation(
            "HEAD", _green_run_git_runner(),
        )
        assert "GREEN-RUN ATTESTATION" in block
        assert "MAY be marked met based on this attestation" in block
        assert "Do NOT execute tests yourself" in block
        assert "runner manages test execution" in block
        assert "read-only mandate" in block

class TestGreenRunPromptInjection:
    """Prompt-content assertions (AC2): the GREEN-RUN block is present in the
    Phase 1 parent prompt and all Phase 2 prompts when the attestation is
    accepted, and absent otherwise."""

    def _make_cmd_issue_runner(self, description: str = _GREEN_RUN_DESC,
                               head_sha: str | None = _GREEN_RUN_HEAD):
        """Build a mock runner handling all wl commands + git for cmd_issue."""
        mock_runner = mock.MagicMock()

        def _side_effect(cmd):
            cmd_str = " ".join(cmd)
            if list(cmd[:2]) == ["git", "rev-parse"]:
                if head_sha is None:
                    return SimpleNamespace(returncode=128, stdout="", stderr="fatal")
                return SimpleNamespace(returncode=0, stdout=head_sha + "\n", stderr="")
            if "show" in cmd_str and "--children" not in cmd_str and "--json" in cmd_str:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({
                        "success": True,
                        "workItem": {"id": "TEST-1", "status": "open"},
                    }),
                    stderr="",
                )
            if "update" in cmd_str:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({"success": True}),
                    stderr="",
                )
            if "--children" in cmd_str:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({
                        "success": True,
                        "workItem": {
                            "id": "TEST-1",
                            "description": description,
                            "status": "in_progress",
                        },
                        "children": [],
                    }),
                    stderr="",
                )
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"success": True}),
                stderr="",
            )

        mock_runner.side_effect = _side_effect
        return mock_runner

    def _mock_cq(self):
        return mock.MagicMock(
            return_value={"success": True, "findings": [], "fixes_applied": 0}
        )

    def _capture_context_prompts(self, **cmd_kwargs):
        """Run cmd_issue and return prompts keyed by pi call context."""
        mock_runner = self._make_cmd_issue_runner()
        prompts: dict[str, str] = {}

        def _fake_call(*args, **kwargs):
            prompts[args[1]] = args[2]
            return {"extracted_text": "[]"}

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log", side_effect=_fake_call
        ), mock.patch(
            "skill.code_review.scripts.code_quality.run_code_quality",
            self._mock_cq(),
        ):
            audit_runner.cmd_issue(
                "TEST-1", persist=False, force=True, runner=mock_runner,
                **cmd_kwargs,
            )
        return prompts

    def test_phase1_parent_prompt_includes_block(self):
        """AC2: the Phase 1 parent prompt includes the GREEN-RUN block."""
        prompts = self._capture_context_prompts(green_run="HEAD")
        assert "parent" in prompts
        assert "GREEN-RUN ATTESTATION" in prompts["parent"]
        assert _GREEN_RUN_HEAD in prompts["parent"]

    def test_no_flag_prompts_lack_block(self):
        """AC5: without an attestation the Phase 1 parent prompt is unchanged."""
        prompts = self._capture_context_prompts()
        assert "parent" in prompts
        assert "GREEN-RUN ATTESTATION" not in prompts["parent"]

    def test_phase2_deep_prompt_includes_block(self):
        """AC2: the phase2_deep prompt includes the GREEN-RUN block."""
        block, _ = audit_runner._resolve_green_run_attestation(
            "HEAD", _green_run_git_runner(),
        )
        issue = {"id": "TEST-1", "title": "Test", "description": ""}
        acs = [{"index": 0, "text": "full suite passes", "verdict": "met", "evidence": ""}]
        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log",
            return_value={"extracted_text": "[]"},
        ) as mock_call:
            audit_runner._run_phase2_deep_analysis(
                issue, acs, [], "test-model", green_run_block=block,
            )
        deep_calls = [c for c in mock_call.call_args_list if c[0][1] == "phase2_deep"]
        assert deep_calls
        assert "GREEN-RUN ATTESTATION" in deep_calls[0][0][2]

    def test_phase2_child_prompt_includes_block(self):
        """AC2: the phase2_child prompt includes the GREEN-RUN block."""
        block, _ = audit_runner._resolve_green_run_attestation(
            "HEAD", _green_run_git_runner(),
        )
        child = {
            "id": "CHILD-1", "title": "Child", "stage": "in_progress",
            "status": "open",
            "ac_results": [
                {"index": 0, "text": "Child AC", "verdict": "met", "evidence": ""}
            ],
        }
        issue = {"id": "TEST-1", "title": "Test", "description": ""}
        acs = [{"index": 0, "text": "AC", "verdict": "met", "evidence": ""}]
        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log",
            return_value={"extracted_text": "[]"},
        ) as mock_call:
            audit_runner._run_phase2_deep_analysis(
                issue, acs, [child], "test-model", green_run_block=block,
            )
        child_calls = [
            c for c in mock_call.call_args_list if c[0][1].startswith("phase2_child")
        ]
        assert child_calls
        assert "GREEN-RUN ATTESTATION" in child_calls[0][0][2]

    def test_phase2_batch_prompt_includes_block(self):
        """AC2: the phase2_batch prompt includes the GREEN-RUN block."""
        block, _ = audit_runner._resolve_green_run_attestation(
            "HEAD", _green_run_git_runner(),
        )
        child = {
            "id": "CHILD-1", "title": "Child", "stage": "in_progress",
            "status": "open",
            "ac_results": [
                {"index": 0, "text": "Child AC", "verdict": "met", "evidence": ""}
            ],
        }
        issue = {"id": "TEST-1", "title": "Test", "description": ""}
        acs = [{"index": 0, "text": "AC", "verdict": "met", "evidence": ""}]
        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log",
            return_value={"extracted_text": "[]"},
        ) as mock_call:
            audit_runner._run_phase2_deep_analysis(
                issue, acs, [child], "test-model",
                batch_phase2=True, green_run_block=block,
            )
        batch_calls = [c for c in mock_call.call_args_list if c[0][1] == "phase2_batch"]
        assert batch_calls
        assert "GREEN-RUN ATTESTATION" in batch_calls[0][0][2]

    def test_phase2_prompts_without_block_unchanged(self):
        """AC5: without an attestation Phase 2 prompts carry no block."""
        issue = {"id": "TEST-1", "title": "Test", "description": ""}
        acs = [{"index": 0, "text": "AC", "verdict": "met", "evidence": ""}]
        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log",
            return_value={"extracted_text": "[]"},
        ) as mock_call:
            audit_runner._run_phase2_deep_analysis(issue, acs, [], "test-model")
        deep_calls = [c for c in mock_call.call_args_list if c[0][1] == "phase2_deep"]
        assert deep_calls
        assert "GREEN-RUN ATTESTATION" not in deep_calls[0][0][2]

    def test_main_forwards_green_run_flag(self):
        """AC1: main() accepts --green-run on the issue subcommand and forwards it."""
        with mock.patch.object(audit_runner, "cmd_issue") as mock_cmd, \
                mock.patch.object(audit_runner, "_apply_proxy_mode_serialization"):
            rc = audit_runner.main(
                ["issue", "SA-123", "--do-not-persist", "--green-run", "HEAD"]
            )
            assert rc == mock_cmd.return_value
            _args, kwargs = mock_cmd.call_args
            assert kwargs["green_run"] == "HEAD"

class TestGreenRunReportLine:
    """The persisted report records the accepted attestation (AC4)."""

    def test_report_includes_attestation_line(self):
        """AC4: 'Green run attestation: <sha>' appears near the header."""
        issue = {"id": "TEST-1"}
        acs = [{"text": "AC", "verdict": "met", "evidence": ""}]
        report = audit_runner._assemble_issue_report(
            issue, acs, [], model="m", model_source="local",
            green_run_sha=_GREEN_RUN_HEAD,
        )
        assert f"Green run attestation: {_GREEN_RUN_HEAD}" in report
        # Ready to close remains the first line (parsers depend on it).
        assert report.startswith("Ready to close:")

    def test_report_without_attestation_has_no_line(self):
        """Backward compatibility: no attestation line without a sha."""
        issue = {"id": "TEST-1"}
        acs = [{"text": "AC", "verdict": "met", "evidence": ""}]
        report = audit_runner._assemble_issue_report(
            issue, acs, [], model="m", model_source="local",
        )
        assert "Green run attestation" not in report

    def test_report_no_model_with_attestation(self):
        """The attestation line also renders on the no-model header path."""
        issue = {"id": "TEST-1"}
        acs = [{"text": "AC", "verdict": "met", "evidence": ""}]
        report = audit_runner._assemble_issue_report(
            issue, acs, [], green_run_sha=_GREEN_RUN_HEAD,
        )
        assert f"Green run attestation: {_GREEN_RUN_HEAD}" in report

class TestGreenRunCmdIssue:
    """End-to-end cmd_issue behavior (AC3, AC4)."""

    def _make_cmd_issue_runner(self, description: str = _GREEN_RUN_DESC,
                               head_sha: str | None = _GREEN_RUN_HEAD):
        """Build a mock runner handling all wl commands + git for cmd_issue."""
        mock_runner = mock.MagicMock()

        def _side_effect(cmd):
            cmd_str = " ".join(cmd)
            if list(cmd[:2]) == ["git", "rev-parse"]:
                if head_sha is None:
                    return SimpleNamespace(returncode=128, stdout="", stderr="fatal")
                return SimpleNamespace(returncode=0, stdout=head_sha + "\n", stderr="")
            if "show" in cmd_str and "--children" not in cmd_str and "--json" in cmd_str:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({
                        "success": True,
                        "workItem": {"id": "TEST-1", "status": "open"},
                    }),
                    stderr="",
                )
            if "update" in cmd_str:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({"success": True}),
                    stderr="",
                )
            if "--children" in cmd_str:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({
                        "success": True,
                        "workItem": {
                            "id": "TEST-1",
                            "description": description,
                            "status": "in_progress",
                        },
                        "children": [],
                    }),
                    stderr="",
                )
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"success": True}),
                stderr="",
            )

        mock_runner.side_effect = _side_effect
        return mock_runner

    def _mock_cq(self):
        return mock.MagicMock(
            return_value={"success": True, "findings": [], "fixes_applied": 0}
        )

    def _run_issue(self, mock_runner, met_batch, persist: bool, green_run=None):
        """Run cmd_issue with the green-run / persistence mocks in place."""
        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log", return_value=met_batch
        ), mock.patch(
            "skill.code_review.scripts.code_quality.run_code_quality",
            self._mock_cq(),
        ):
            return audit_runner.cmd_issue(
                "TEST-1", persist=persist, force=True, runner=mock_runner,
                green_run=green_run,
            )

    def test_mismatch_rejected_run_proceeds_without_attestation(self, capsys):
        """AC3: a mismatched --green-run errors clearly and the run proceeds
        WITHOUT the attestation (execution-dependent ACs stay partial)."""
        mock_runner = self._make_cmd_issue_runner()
        met_batch = {
            "extracted_text": json.dumps([
                {"index": 0, "verdict": "met", "evidence": "file.py:1"},
            ]),
        }
        rc = self._run_issue(
            mock_runner, met_batch, persist=False, green_run=_GREEN_RUN_OTHER,
        )
        assert rc == 0
        captured = capsys.readouterr()
        assert _GREEN_RUN_OTHER in captured.err
        assert _GREEN_RUN_HEAD in captured.err
        assert "does not match" in captured.err
        assert "GREEN-RUN ATTESTATION" not in captured.out
        assert "Green run attestation" not in captured.out

    def test_persisted_report_contains_attestation_line(self):
        """AC4: with a valid attestation the persisted report (read back via
        wl audit-show) contains 'Green run attestation: <sha>'."""
        captured: dict = {}
        mock_runner = self._make_cmd_issue_runner()
        met_batch = {
            "extracted_text": json.dumps([
                {"index": 0, "verdict": "met", "evidence": "file.py:1"},
            ]),
        }

        def _fake_persist(issue_id, report_text, worklog_dir=None):
            captured["report"] = report_text
            return 0

        original_run_wl = audit_runner._run_wl

        def _fake_run_wl(runner, cmd, worklog_dir=None):
            cmd_str = " ".join(cmd)
            if "audit-show" in cmd_str:
                return {
                    "success": True,
                    "audit": {
                        "rawOutput": captured.get("report", ""),
                        "auditedAt": "2026-01-01T00:00:00.000Z",
                    },
                }
            return original_run_wl(runner, cmd, worklog_dir=worklog_dir)

        with mock.patch.object(
            audit_runner, "persist_audit", side_effect=_fake_persist
        ), mock.patch.object(
            audit_runner, "_run_wl", side_effect=_fake_run_wl
        ), mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log", return_value=met_batch
        ), mock.patch(
            "skill.code_review.scripts.code_quality.run_code_quality",
            self._mock_cq(),
        ):
            rc = audit_runner.cmd_issue(
                "TEST-1", persist=True, force=True, runner=mock_runner,
                green_run=_GREEN_RUN_HEAD,
            )

        assert rc == 0
        assert "report" in captured, "persist_audit should have been invoked"
        persisted = captured["report"]
        assert f"Green run attestation: {_GREEN_RUN_HEAD}" in persisted
        assert "TEST-1" in persisted  # content identity check passes

    def test_persisted_report_without_attestation_has_no_line(self):
        """Backward compatibility: no attestation line without --green-run."""
        captured: dict = {}
        mock_runner = self._make_cmd_issue_runner()
        met_batch = {
            "extracted_text": json.dumps([
                {"index": 0, "verdict": "met", "evidence": "file.py:1"},
            ]),
        }

        def _fake_persist(issue_id, report_text, worklog_dir=None):
            captured["report"] = report_text
            return 0

        original_run_wl = audit_runner._run_wl

        def _fake_run_wl(runner, cmd, worklog_dir=None):
            cmd_str = " ".join(cmd)
            if "audit-show" in cmd_str:
                return {
                    "success": True,
                    "audit": {
                        "rawOutput": captured.get("report", ""),
                        "auditedAt": "2026-01-01T00:00:00.000Z",
                    },
                }
            return original_run_wl(runner, cmd, worklog_dir=worklog_dir)

        with mock.patch.object(
            audit_runner, "persist_audit", side_effect=_fake_persist
        ), mock.patch.object(
            audit_runner, "_run_wl", side_effect=_fake_run_wl
        ), mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log", return_value=met_batch
        ), mock.patch(
            "skill.code_review.scripts.code_quality.run_code_quality",
            self._mock_cq(),
        ):
            rc = audit_runner.cmd_issue(
                "TEST-1", persist=True, force=True, runner=mock_runner,
            )

        assert rc == 0
        assert "report" in captured
        assert "Green run attestation" not in captured["report"]

_AUTO_GREEN_ENTRY = {
    "stdout": "5 passed in 0.03s",
    "stderr": "",
    "exit_code": 0,
    "completed_at": 1000.0,
    "command": "pytest -q -r a --disable-warnings",
    "git_state": "fingerprint",
    "cached": True,
}

_NODE_SUITE_DIRS = ("tests/node", "tests/cli", "tests/unit")

def _make_suite_dirs(tmp_path) -> Path:
    """Create the canonical node suite dirs under *tmp_path*.

    ``full_suite_commands`` skips missing suite dirs (SA-0MSJELL44009XYIL), so
    tests that exercise the full 4-command set must create the dirs first —
    otherwise only the pytest command is queried.
    """
    for d in _NODE_SUITE_DIRS:
        (tmp_path / d).mkdir(parents=True, exist_ok=True)
    return tmp_path


def _with_pytest_config(tmp_path) -> Path:
    """Declare a pytest suite under *tmp_path* (pytest.ini).

    F2 (SA-0MSTMYE79006NA61): ``full_suite_commands`` emits pytest only when
    the repo declares a pytest suite — fixtures that exercise the pytest
    command must declare it explicitly.
    """
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    return tmp_path

class TestAutoGreenRunResolution:
    """Unit tests for the read-only automatic green-run resolution."""

    def test_no_cached_run_no_evidence(self, tmp_path):
        """AC1/AC2: a cache miss yields NO evidence (fail-closed)."""
        with mock.patch.object(audit_runner, "query_cached", return_value=None):
            block, sha = audit_runner._resolve_auto_green_run(
                _green_run_git_runner(), cwd=str(tmp_path),
            )
        assert block is None
        assert sha is None

    def test_timed_out_run_leaves_no_evidence(self, tmp_path):
        """AC2: a timed-out run never lands in the cache → no green verdict."""
        with mock.patch.object(audit_runner, "query_cached", return_value=None):
            block, sha = audit_runner._resolve_auto_green_run(
                _green_run_git_runner(), cwd=str(tmp_path),
            )
        assert block is None
        assert sha is None

    def test_green_cached_run_yields_evidence(self, tmp_path):
        """AC1: green cached full-suite entries yield a prompt block + sha."""
        _with_pytest_config(tmp_path)
        with mock.patch.object(audit_runner, "query_cached", return_value=_AUTO_GREEN_ENTRY):
            block, sha = audit_runner._resolve_auto_green_run(
                _green_run_git_runner(), cwd=str(tmp_path),
            )
        assert sha == _GREEN_RUN_HEAD
        assert block is not None
        assert "AUTO-VERIFIED GREEN RUN" in block
        assert _GREEN_RUN_HEAD in block
        assert "MAY be marked met" in block
        assert "Do NOT execute tests yourself" in block
        assert "read-only mandate" in block

    def test_failing_cached_run_no_evidence(self, tmp_path):
        """AC2: a cached run with a non-zero exit never yields a green verdict."""
        def _side_effect(command, **kwargs):
            entry = dict(_AUTO_GREEN_ENTRY)
            if "pytest" in command:
                entry["exit_code"] = 1
            return entry

        with mock.patch.object(audit_runner, "query_cached", side_effect=_side_effect):
            block, sha = audit_runner._resolve_auto_green_run(
                _green_run_git_runner(), cwd=str(tmp_path),
            )
        assert block is None
        assert sha is None

    def test_partial_cache_no_evidence(self, tmp_path):
        """AC2: only some suites cached → fail-closed (no evidence)."""
        _make_suite_dirs(tmp_path)

        def _side_effect(command, **kwargs):
            # pytest + two node dirs cached green; the tests/unit run is missing
            return _AUTO_GREEN_ENTRY if "tests/unit" not in command else None

        with mock.patch.object(audit_runner, "query_cached", side_effect=_side_effect):
            block, sha = audit_runner._resolve_auto_green_run(
                _green_run_git_runner(), cwd=str(tmp_path),
            )
        assert block is None
        assert sha is None

    def test_cache_error_fail_closed(self, tmp_path):
        """AC2: a cache/infra error never crashes the audit, yields no evidence."""
        with mock.patch.object(
            audit_runner, "query_cached", side_effect=RuntimeError("cache corrupt")
        ):
            block, sha = audit_runner._resolve_auto_green_run(
                _green_run_git_runner(), cwd=str(tmp_path),
            )
        assert block is None
        assert sha is None

    def test_git_unavailable_fail_closed(self, tmp_path):
        """AC2: unresolvable HEAD → no evidence (mirrors the operator path)."""
        with mock.patch.object(audit_runner, "query_cached") as mock_q:
            block, sha = audit_runner._resolve_auto_green_run(
                _green_run_git_runner(head_sha=None), cwd=str(tmp_path),
            )
        assert block is None
        assert sha is None
        mock_q.assert_not_called()

    def test_query_cached_consumed_read_only(self, tmp_path, capsys):
        """AC1: resolution consumes the cache (never executes) at the project cwd."""
        _make_suite_dirs(tmp_path)
        _with_pytest_config(tmp_path)
        with mock.patch.object(
            audit_runner, "query_cached", return_value=_AUTO_GREEN_ENTRY
        ) as mock_q:
            audit_runner._resolve_auto_green_run(
                _green_run_git_runner(), cwd=str(tmp_path),
            )
        assert mock_q.call_count == 4  # pytest + 3 node suite commands
        for call in mock_q.call_args_list:
            assert call.kwargs["cwd"] == str(tmp_path.resolve())
            assert call.kwargs["ttl"] == audit_runner.DEFAULT_TTL_SECONDS

    # -----------------------------------------------------------------------
    # Diagnostic output (SA-0MSJELL44009XYIL AC: clear diagnostic on failure)
    # -----------------------------------------------------------------------

    def test_missing_cache_emits_diagnostic(self, tmp_path, capsys):
        """A cache miss yields a clear diagnostic naming the command + remedy."""
        _make_suite_dirs(tmp_path)
        _with_pytest_config(tmp_path)
        with mock.patch.object(audit_runner, "query_cached", return_value=None):
            block, sha = audit_runner._resolve_auto_green_run(
                _green_run_git_runner(), cwd=str(tmp_path),
            )
        assert block is None
        assert sha is None
        err = capsys.readouterr().err
        assert "Automatic full-suite verification unavailable" in err
        assert "pytest -q -r a --disable-warnings" in err
        assert "no cached full-suite run" in err
        assert "run_tests.py --force" in err or "/skill:test" in err
        assert "--green-run HEAD" in err

    def test_failed_cached_run_emits_diagnostic(self, tmp_path, capsys):
        """A non-zero cached run is distinguished from a miss in the diagnostic."""
        _make_suite_dirs(tmp_path)

        def _side_effect(command, **kwargs):
            entry = dict(_AUTO_GREEN_ENTRY)
            if "tests/cli" in command:
                entry["exit_code"] = 7
            return entry

        with mock.patch.object(audit_runner, "query_cached", side_effect=_side_effect):
            block, sha = audit_runner._resolve_auto_green_run(
                _green_run_git_runner(), cwd=str(tmp_path),
            )
        assert block is None
        assert sha is None
        err = capsys.readouterr().err
        assert "Automatic full-suite verification unavailable" in err
        assert "exited non-zero" in err
        assert "(7)" in err
        assert "--green-run HEAD" in err

    def test_green_run_no_diagnostic(self, tmp_path, capsys):
        """A fully green cache set yields evidence and no diagnostic noise."""
        _make_suite_dirs(tmp_path)
        with mock.patch.object(
            audit_runner, "query_cached", return_value=_AUTO_GREEN_ENTRY
        ):
            block, sha = audit_runner._resolve_auto_green_run(
                _green_run_git_runner(), cwd=str(tmp_path),
            )
        assert sha == _GREEN_RUN_HEAD
        assert block is not None
        assert "Automatic full-suite verification unavailable" not in capsys.readouterr().err

class TestAutoGreenRunReportLine:
    """The persisted report records the automatic evidence (AC1)."""

    def test_report_includes_auto_evidence_line(self):
        """AC1: 'Automatic green run evidence: <sha>' appears near the header."""
        issue = {"id": "TEST-1"}
        acs = [{"text": "AC", "verdict": "met", "evidence": ""}]
        report = audit_runner._assemble_issue_report(
            issue, acs, [], model="m", model_source="local",
            auto_green_run_sha=_GREEN_RUN_HEAD,
        )
        assert f"Automatic green run evidence: {_GREEN_RUN_HEAD}" in report
        assert report.startswith("Ready to close:")

    def test_report_without_auto_evidence_has_no_line(self):
        """Backward compatibility: no auto line without evidence."""
        issue = {"id": "TEST-1"}
        acs = [{"text": "AC", "verdict": "met", "evidence": ""}]
        report = audit_runner._assemble_issue_report(
            issue, acs, [], model="m", model_source="local",
        )
        assert "Automatic green run evidence" not in report

    def test_report_no_model_with_auto_evidence(self):
        """The auto line also renders on the no-model header path."""
        issue = {"id": "TEST-1"}
        acs = [{"text": "AC", "verdict": "met", "evidence": ""}]
        report = audit_runner._assemble_issue_report(
            issue, acs, [], auto_green_run_sha=_GREEN_RUN_HEAD,
        )
        assert f"Automatic green run evidence: {_GREEN_RUN_HEAD}" in report

class TestAutoGreenRunPromptInjection:
    """Prompt-content assertions (AC1/AC3): the AUTO-VERIFIED block is present
    in the Phase 1 parent prompt when the read-only cache holds a green
    full-suite run, and absent otherwise."""

    def _make_cmd_issue_runner(self, description: str = _GREEN_RUN_DESC,
                               head_sha: str | None = _GREEN_RUN_HEAD):
        """Build a mock runner handling all wl commands + git for cmd_issue."""
        mock_runner = mock.MagicMock()

        def _side_effect(cmd):
            cmd_str = " ".join(cmd)
            if list(cmd[:2]) == ["git", "rev-parse"]:
                if head_sha is None:
                    return SimpleNamespace(returncode=128, stdout="", stderr="fatal")
                return SimpleNamespace(returncode=0, stdout=head_sha + "\n", stderr="")
            if "show" in cmd_str and "--children" not in cmd_str and "--json" in cmd_str:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({
                        "success": True,
                        "workItem": {"id": "TEST-1", "status": "open"},
                    }),
                    stderr="",
                )
            if "update" in cmd_str:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({"success": True}),
                    stderr="",
                )
            if "--children" in cmd_str:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({
                        "success": True,
                        "workItem": {
                            "id": "TEST-1",
                            "description": description,
                            "status": "in_progress",
                        },
                        "children": [],
                    }),
                    stderr="",
                )
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"success": True}),
                stderr="",
            )

        mock_runner.side_effect = _side_effect
        return mock_runner

    def _mock_cq(self):
        return mock.MagicMock(
            return_value={"success": True, "findings": [], "fixes_applied": 0}
        )

    def _capture_context_prompts(self, cache_result=_AUTO_GREEN_ENTRY, **cmd_kwargs):
        """Run cmd_issue with query_cached mocked; return prompts by context."""
        mock_runner = self._make_cmd_issue_runner()
        prompts: dict[str, str] = {}

        def _fake_call(*args, **kwargs):
            prompts[args[1]] = args[2]
            return {"extracted_text": "[]"}

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log", side_effect=_fake_call
        ), mock.patch.object(
            audit_runner, "query_cached", return_value=cache_result
        ), mock.patch(
            "skill.code_review.scripts.code_quality.run_code_quality",
            self._mock_cq(),
        ):
            audit_runner.cmd_issue(
                "TEST-1", persist=False, force=True, runner=mock_runner,
                **cmd_kwargs,
            )
        return prompts

    def test_green_cache_injects_auto_block(self):
        """AC1: with a green cached full-suite run the Phase 1 parent prompt
        carries the AUTO-VERIFIED block (no operator attestation needed)."""
        prompts = self._capture_context_prompts()
        assert "parent" in prompts
        assert "AUTO-VERIFIED GREEN RUN" in prompts["parent"]
        assert _GREEN_RUN_HEAD in prompts["parent"]
        assert "GREEN-RUN ATTESTATION" not in prompts["parent"]

    def test_cache_miss_proceeds_on_auto_execute(self, capsys):
        """F3 (SA-0MSTN5KRF0097TVP): a cache miss no longer blocks — auto-
        executes the suite and proceeds to Phase 1.

        The old gate (SA-0MSQ72BVV0011SRU) that exited rc 1 on cache miss
        is replaced by F3's default auto-execution path."""
        mock_runner = self._make_cmd_issue_runner()
        prompts: dict[str, str] = {}

        def _fake_call(*args, **kwargs):
            prompts[args[1]] = args[2]
            return {"extracted_text": "[]"}

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log", side_effect=_fake_call
        ), mock.patch.object(
            audit_runner, "query_cached", return_value=None
        ), mock.patch.object(
            audit_runner, "_run_tests_via_test_skill",
            return_value={
                "success": True, "results": [], "failures": [],
                "triaged": [], "notice": "",
            },
        ):
            rc = audit_runner.cmd_issue(
                "TEST-1", persist=False, force=True, runner=mock_runner,
            )
        assert rc == 0
        assert "parent" in prompts
        assert "TEST-SKILL GREEN RUN" in prompts["parent"]
        err = capsys.readouterr().err
        assert "AUDIT_NO_EXECUTE" not in err

    def test_failing_cache_no_auto_block(self):
        """AC2: a non-zero cached exit never injects the block."""
        def _side_effect(command, **kwargs):
            entry = dict(_AUTO_GREEN_ENTRY)
            if "pytest" in command:
                entry["exit_code"] = 1
            return entry

        prompts: dict[str, str] = {}
        mock_runner = self._make_cmd_issue_runner()

        def _fake_call(*args, **kwargs):
            prompts[args[1]] = args[2]
            return {"extracted_text": "[]"}

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log", side_effect=_fake_call
        ), mock.patch.object(
            audit_runner, "query_cached", side_effect=_side_effect
        ), mock.patch(
            "skill.code_review.scripts.code_quality.run_code_quality",
            self._mock_cq(),
        ):
            rc = audit_runner.cmd_issue(
                "TEST-1", persist=False, force=True, runner=mock_runner,
            )
        assert rc == 0
        assert "AUTO-VERIFIED GREEN RUN" not in prompts["parent"]

    def test_operator_attestation_precedes_auto_path(self):
        """AC7: with a valid --green-run, the automatic path is not consulted."""
        prompts = self._capture_context_prompts(green_run="HEAD")
        assert "GREEN-RUN ATTESTATION" in prompts["parent"]
        assert "AUTO-VERIFIED GREEN RUN" not in prompts["parent"]

    def test_operator_attestation_skips_cache_query(self):
        """AC7: a valid --green-run means query_cached is never called."""
        mock_runner = self._make_cmd_issue_runner()

        def _fake_call(*args, **kwargs):
            return {"extracted_text": "[]"}

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log", side_effect=_fake_call
        ), mock.patch.object(
            audit_runner, "query_cached"
        ) as mock_q, mock.patch(
            "skill.code_review.scripts.code_quality.run_code_quality",
            self._mock_cq(),
        ):
            audit_runner.cmd_issue(
                "TEST-1", persist=False, force=True, runner=mock_runner,
                green_run="HEAD",
            )
        mock_q.assert_not_called()

class TestAutoGreenRunCmdIssue:
    """End-to-end cmd_issue behavior (AC1, AC2)."""

    def _make_cmd_issue_runner(self, description: str = _GREEN_RUN_DESC,
                               head_sha: str | None = _GREEN_RUN_HEAD):
        return TestAutoGreenRunPromptInjection()._make_cmd_issue_runner(
            description=description, head_sha=head_sha,
        )

    def _mock_cq(self):
        return mock.MagicMock(
            return_value={"success": True, "findings": [], "fixes_applied": 0}
        )

    def test_persisted_report_records_auto_evidence(self):
        """AC1: with a green cached run the persisted report (read back via
        wl audit-show) contains 'Automatic green run evidence: <sha>'."""
        captured: dict = {}
        mock_runner = self._make_cmd_issue_runner()
        met_batch = {
            "extracted_text": json.dumps([
                {"index": 0, "verdict": "met", "evidence": "file.py:1"},
            ]),
        }

        def _fake_persist(issue_id, report_text, worklog_dir=None):
            captured["report"] = report_text
            return 0

        original_run_wl = audit_runner._run_wl

        def _fake_run_wl(runner, cmd, worklog_dir=None):
            cmd_str = " ".join(cmd)
            if "audit-show" in cmd_str:
                return {
                    "success": True,
                    "audit": {
                        "rawOutput": captured.get("report", ""),
                        "auditedAt": "2026-01-01T00:00:00.000Z",
                    },
                }
            return original_run_wl(runner, cmd, worklog_dir=worklog_dir)

        with mock.patch.object(
            audit_runner, "persist_audit", side_effect=_fake_persist
        ), mock.patch.object(
            audit_runner, "_run_wl", side_effect=_fake_run_wl
        ), mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log", return_value=met_batch
        ), mock.patch.object(
            audit_runner, "query_cached", return_value=_AUTO_GREEN_ENTRY
        ), mock.patch(
            "skill.code_review.scripts.code_quality.run_code_quality",
            self._mock_cq(),
        ):
            rc = audit_runner.cmd_issue(
                "TEST-1", persist=True, force=True, runner=mock_runner,
            )

        assert rc == 0
        assert "report" in captured, "persist_audit should have been invoked"
        persisted = captured["report"]
        assert f"Automatic green run evidence: {_GREEN_RUN_HEAD}" in persisted
        assert "TEST-1" in persisted  # content identity check passes

    def test_cache_error_never_crashes_audit(self, capsys):
        """AC2: an infra error in the cache query never crashes the audit."""
        mock_runner = self._make_cmd_issue_runner()
        met_batch = {
            "extracted_text": json.dumps([
                {"index": 0, "verdict": "met", "evidence": "file.py:1"},
            ]),
        }

        def _fake_call(*args, **kwargs):
            return met_batch

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log", side_effect=_fake_call
        ), mock.patch.object(
            audit_runner, "query_cached",
            side_effect=RuntimeError("cache corrupt"),
        ), mock.patch(
            "skill.code_review.scripts.code_quality.run_code_quality",
            self._mock_cq(),
        ):
            rc = audit_runner.cmd_issue(
                "TEST-1", persist=False, force=True, runner=mock_runner,
            )

        assert rc == 0
        out = capsys.readouterr().out
        assert "AUTO-VERIFIED GREEN RUN" not in out
        assert "Automatic green run evidence" not in out

class TestFullSuiteCacheClassification:
    """Unit tests for _classify_full_suite_cache statuses."""

    def test_green_all_commands_cached(self, tmp_path):
        """Every suite command cached green at HEAD → 'green' + evidence sha."""
        _with_pytest_config(tmp_path)
        with mock.patch.object(
            audit_runner, "query_cached", return_value=_AUTO_GREEN_ENTRY
        ):
            status, sha, problems = audit_runner._classify_full_suite_cache(
                _green_run_git_runner(), cwd=str(tmp_path),
            )
        assert status == "green"
        assert sha == _GREEN_RUN_HEAD
        assert problems == []

    def test_miss_any_command_uncached(self, tmp_path):
        """At least one command without a cached entry → 'miss'."""
        def _side_effect(command, **kwargs):
            return None if "tests/unit" in command else _AUTO_GREEN_ENTRY

        _make_suite_dirs(tmp_path)
        with mock.patch.object(audit_runner, "query_cached", side_effect=_side_effect):
            status, sha, problems = audit_runner._classify_full_suite_cache(
                _green_run_git_runner(), cwd=str(tmp_path),
            )
        assert status == "miss"
        assert sha == _GREEN_RUN_HEAD
        assert any("no cached full-suite run" in p for p in problems)

    def test_red_nonzero_cached_run(self, tmp_path):
        """All commands cached but one exited non-zero → 'red'."""
        def _side_effect(command, **kwargs):
            entry = dict(_AUTO_GREEN_ENTRY)
            if "tests/cli" in command:
                entry["exit_code"] = 7
            return entry

        _make_suite_dirs(tmp_path)
        with mock.patch.object(audit_runner, "query_cached", side_effect=_side_effect):
            status, sha, problems = audit_runner._classify_full_suite_cache(
                _green_run_git_runner(), cwd=str(tmp_path),
            )
        assert status == "red"
        assert sha == _GREEN_RUN_HEAD
        assert any("exited non-zero" in p for p in problems)

    def test_miss_dominates_over_red(self, tmp_path):
        """A mix of missing + red entries classifies as 'miss' (gate fires)."""
        def _side_effect(command, **kwargs):
            if "tests/unit" in command:
                return None
            entry = dict(_AUTO_GREEN_ENTRY)
            if "tests/cli" in command:
                entry["exit_code"] = 7
            return entry

        _make_suite_dirs(tmp_path)
        with mock.patch.object(audit_runner, "query_cached", side_effect=_side_effect):
            status, _, _ = audit_runner._classify_full_suite_cache(
                _green_run_git_runner(), cwd=str(tmp_path),
            )
        assert status == "miss"

    def test_error_when_head_unresolvable(self, tmp_path):
        """Unresolvable HEAD → 'error' with no cache query."""
        with mock.patch.object(audit_runner, "query_cached") as mock_q:
            status, sha, _ = audit_runner._classify_full_suite_cache(
                _green_run_git_runner(head_sha=None), cwd=str(tmp_path),
            )
        assert status == "error"
        assert sha is None
        mock_q.assert_not_called()

class TestEffectiveSuiteCommands:
    """Repo-aware command set (AC3): no phantom pytest for no-pytest repos.

    Pytest-suite detection is the shared ``repo_has_pytest_suite`` from the
    test skill runner (single source of truth, F2 AC4); the effective command
    set is ``full_suite_commands`` directly (F4, SA-0MSTN8CWM003AAU9:
    the thin ``_effective_suite_commands`` wrapper was removed with the
    pre-flight gate it served).
    """

    def test_pytest_ini_counts_as_pytest_suite(self, tmp_path):
        (tmp_path / "pytest.ini").write_text("[pytest]\n")
        assert repo_has_pytest_suite(tmp_path) is True
        assert any(
            "pytest" in c
            for c in audit_runner.full_suite_commands(tmp_path)
        )

    def test_pyproject_marker_counts_as_pytest_suite(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
        assert repo_has_pytest_suite(tmp_path) is True

    def test_python_test_files_count_as_pytest_suite(self, tmp_path):
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_x.py").write_text("def test_x(): pass\n")
        assert repo_has_pytest_suite(tmp_path) is True

    def test_no_pytest_suite_skips_pytest_command(self, tmp_path):
        """A node-only repo keeps node commands but drops the pytest command."""
        _make_suite_dirs(tmp_path)
        assert repo_has_pytest_suite(tmp_path) is False
        cmds = audit_runner.full_suite_commands(tmp_path)
        assert cmds  # node commands remain
        assert not any("pytest" in c for c in cmds)

    def test_no_suite_at_all_yields_empty_set(self, tmp_path):
        assert repo_has_pytest_suite(tmp_path) is False
        assert audit_runner.full_suite_commands(tmp_path) == []


class TestNeverBlocksOnExecutionImpossible:
    """F4 never-block guarantee (SA-0MSTN8CWM003AAU9 AC1/AC2): the audit
    must NEVER exit with a hard block solely because it cannot run tests —
    no cache, no test runner, no configured suite commands, execution
    impossible. Every such case degrades to a fail-open partial verdict with
    a clear diagnostic (the old pre-flight cache gate was removed).
    """

    def test_gate_function_removed(self):
        """AC6: the blocking gate no longer exists — no code path can emit
        'Audit blocked: no green full-suite run is cached at HEAD' as a hard
        exit (the F1 contract test asserts the message never appears in err)."""
        assert not hasattr(audit_runner, "_preflight_cache_gate")

    def test_no_suite_repo_proceeds_partial(self, tmp_path):
        """AC2: a docs-only repo (empty command set) proceeds — no block, no
        execution, execution-dependent ACs stay partial with a documented
        reason."""
        prompts: dict[str, str] = {}
        mock_runner = TestAutoGreenRunPromptInjection()._make_cmd_issue_runner()

        def _fake_call(*args, **kwargs):
            prompts[args[1]] = args[2]
            return {"extracted_text": "[]"}

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log", side_effect=_fake_call
        ), mock.patch.object(
            audit_runner, "query_cached", return_value=None
        ), mock.patch.object(
            audit_runner, "_run_tests_via_test_skill",
            return_value={
                "success": True, "results": [], "failures": [],
                "triaged": [], "notice": "",
            },
        ) as mock_run_tests, mock.patch(
            "skill.code_review.scripts.code_quality.run_code_quality",
            mock.MagicMock(
                return_value={"success": True, "findings": [], "fixes_applied": 0}
            ),
        ), mock.patch.object(audit_runner, "TARGET_PROJECT_ROOT", tmp_path):
            rc = audit_runner.cmd_issue(
                "TEST-1", persist=False, force=True, runner=mock_runner,
            )
        assert rc == 0, "an execution-impossible repo must never hard-block"
        assert "parent" in prompts, "Phase 1 must be reached"
        # EMPTY classification → no auto-execution (nothing to run) and no
        # TEST-SKILL GREEN RUN block.
        mock_run_tests.assert_not_called()
        assert "TEST-SKILL GREEN RUN" not in prompts.get("parent", "")


class TestPreflightGateCmdIssue:
    """End-to-end gate matrix at the cmd_issue level (AC1/AC2/AC4)."""

    def _make_cmd_issue_runner(self, **kwargs):
        return TestAutoGreenRunPromptInjection()._make_cmd_issue_runner(**kwargs)

    def _run_issue(self, *, cache_return=None, cache_side_effect=None,
                   green_run=None, run_tests=False):
        """Run cmd_issue (force, no persist) with a mocked pi call."""
        mock_runner = self._make_cmd_issue_runner()
        calls: list[str] = []

        def _fake_call(*args, **kwargs):
            calls.append(args[1])
            return {"extracted_text": "[]"}

        patches = [
            mock.patch.object(
                audit_runner, "_call_pi_and_maybe_log", side_effect=_fake_call
            ),
            mock.patch(
                "skill.code_review.scripts.code_quality.run_code_quality",
                mock.MagicMock(
                    return_value={"success": True, "findings": [], "fixes_applied": 0}
                ),
            ),
        ]
        if cache_side_effect is not None:
            patches.append(mock.patch.object(
                audit_runner, "query_cached", side_effect=cache_side_effect
            ))
        else:
            patches.append(mock.patch.object(
                audit_runner, "query_cached", return_value=cache_return
            ))
        # F3 (SA-0MSTN5KRF0097TVP): a cache miss auto-executes the suite via
        # the test skill by default, so mock the invocation unconditionally
        # to keep these cmd_issue-level tests hermetic (a real invocation
        # would run the repo's actual suite).
        patches.append(mock.patch.object(
            audit_runner, "_run_tests_via_test_skill",
            return_value={
                "success": True, "results": [], "failures": [],
                "triaged": [], "notice": "",
            },
        ))
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            rc = audit_runner.cmd_issue(
                "TEST-1", persist=False, force=True, runner=mock_runner,
                green_run=green_run, run_tests=run_tests,
            )
        return rc, calls, mock_runner

    def test_cache_miss_auto_executes_and_proceeds(self, capsys):
        """F3 (SA-0MSTN5KRF0097TVP): cache miss + no opt-out → auto-executes
        the suite, proceeds to Phase 1 (no longer exits non-zero).

        The old gate (SA-0MSQ72BVV0011SRU) that exited rc 1 is replaced by
        F3's auto-execution path."""
        rc, calls, _runner = self._run_issue(cache_return=None)
        assert rc == 0
        assert "parent" in calls  # reached Phase 1
        err = capsys.readouterr().err
        assert "AUDIT_NO_EXECUTE" not in err

    def test_cache_miss_with_run_tests_proceeds(self):
        """AC1/AC2: cache miss + --run-tests executes the suite and proceeds."""
        rc, calls, _ = self._run_issue(cache_return=None, run_tests=True)
        assert rc == 0
        assert "parent" in calls  # reached Phase 1

    def test_cache_miss_with_green_run_proceeds(self):
        """AC1/AC2: cache miss + --green-run HEAD attests and proceeds."""
        rc, calls, _ = self._run_issue(cache_return=None, green_run="HEAD")
        assert rc == 0
        assert "parent" in calls

    def test_green_cache_proceeds(self):
        """A green cached full-suite run proceeds (auto-verified)."""
        rc, calls, _ = self._run_issue(cache_return=_AUTO_GREEN_ENTRY)
        assert rc == 0
        assert "parent" in calls

    def test_red_cache_proceeds_partial(self):
        """AC2: a red cached run keeps current behavior (partial, no gate)."""
        def _side_effect(command, **kwargs):
            entry = dict(_AUTO_GREEN_ENTRY)
            if "pytest" in command:
                entry["exit_code"] = 1
            return entry

        rc, calls, _ = self._run_issue(cache_side_effect=_side_effect)
        assert rc == 0
        assert "parent" in calls

class TestRunTestsViaTestSkill:
    """Unit tests for the --run-tests test-skill invocation.

    Covers: green executed run (AC1), failures triaged per the test skill
    (AC4), fail-closed notices, and triage-error resilience.
    """

    def _green_run(self, command, **kwargs):
        return {
            "stdout": "5 passed in 0.03s",
            "stderr": "",
            "exit_code": 0,
            "completed_at": 1000.0,
            "command": command,
            "git_state": "fingerprint",
            "cached": False,
        }

    def test_green_run_success_refreshes_cache(self, tmp_path, capsys):
        """AC1: a green executed suite yields success and refreshes the cache."""
        _make_suite_dirs(tmp_path)
        _with_pytest_config(tmp_path)
        with mock.patch.object(
            audit_runner, "run_cached", side_effect=self._green_run
        ) as mock_run:
            result = audit_runner._run_tests_via_test_skill(cwd=tmp_path)
        assert result["success"] is True
        assert result["failures"] == []
        assert result["triaged"] == []
        assert result["notice"] == ""
        # force=True executes fresh and stores, so the cache is refreshed.
        assert mock_run.call_count == 4  # pytest + 3 node suite commands
        for call in mock_run.call_args_list:
            assert call.kwargs["cwd"] == str(tmp_path.resolve())
            assert call.kwargs["force"] is True
            assert call.kwargs["ttl"] == audit_runner.DEFAULT_TTL_SECONDS
        err = capsys.readouterr().err
        assert "Invoking test skill (run_tests.py)" in err
        assert "Test skill run completed: success=True" in err

    def test_failing_run_triages_failures(self, tmp_path, capsys):
        """AC4: failures are triaged per the test skill, never silently ignored."""
        _with_pytest_config(tmp_path)

        def _side_effect(command, **kwargs):
            if "pytest" in command:
                return {
                    "stdout": (
                        "FAILED tests/test_x.py::test_boom - "
                        "AssertionError: boom"
                    ),
                    "stderr": "",
                    "exit_code": 1,
                    "completed_at": 1000.0,
                    "command": command,
                    "git_state": "fingerprint",
                    "cached": False,
                }
            return self._green_run(command, **kwargs)

        with mock.patch.object(
            audit_runner, "run_cached", side_effect=_side_effect
        ), mock.patch(
            "skill.triage.scripts.check_or_create.check_or_create",
            return_value={"issueId": "SA-TRIAGE-1", "created": True},
        ) as mock_triage:
            result = audit_runner._run_tests_via_test_skill(
                cwd=tmp_path, parent_work_item_id="TEST-1", head_sha="abc123",
            )
        assert result["success"] is False
        assert len(result["failures"]) == 1
        assert result["failures"][0]["test_name"] == "tests/test_x.py::test_boom"
        assert mock_triage.call_count == 1
        payload = mock_triage.call_args.args[0]
        assert payload["test_name"] == "tests/test_x.py::test_boom"
        assert payload["parent_work_item_id"] == "TEST-1"
        assert payload["commit_hash"] == "abc123"
        assert payload["repo_path"] == str(tmp_path.resolve())
        err = capsys.readouterr().err
        assert "Test skill run completed: success=False" in err
        assert "failures=1 triaged=1" in err

    def test_nonzero_exit_without_parseable_failures(self, tmp_path):
        """A non-zero exit with no FAILED lines is recorded, never silently green."""
        _with_pytest_config(tmp_path)

        def _side_effect(command, **kwargs):
            if "pytest" in command:
                return {
                    "stdout": "crash before any test ran",
                    "stderr": "",
                    "exit_code": 1,
                    "completed_at": 1000.0,
                    "command": command,
                    "git_state": "fingerprint",
                    "cached": False,
                }
            return self._green_run(command, **kwargs)

        with mock.patch.object(
            audit_runner, "run_cached", side_effect=_side_effect
        ), mock.patch(
            "skill.triage.scripts.check_or_create.check_or_create",
            return_value={"issueId": "SA-TRIAGE-1", "created": True},
        ) as mock_triage:
            result = audit_runner._run_tests_via_test_skill(cwd=tmp_path)
        assert result["success"] is False
        assert len(result["failures"]) == 1
        assert "<suite exited 1>" in result["failures"][0]["test_name"]
        assert mock_triage.call_count == 1

    def test_timeout_notice_fail_closed(self, tmp_path):
        """A suite timeout yields a notice, no evidence, no crash."""
        _with_pytest_config(tmp_path)

        def _side_effect(command, **kwargs):
            raise subprocess.TimeoutExpired(cmd=command, timeout=600)

        with mock.patch.object(
            audit_runner, "run_cached", side_effect=_side_effect
        ):
            result = audit_runner._run_tests_via_test_skill(cwd=tmp_path)
        assert result["success"] is False
        assert "timed out" in result["notice"]
        assert result["failures"] == []

    def test_command_not_found_notice_fail_closed(self, tmp_path):
        """A missing suite binary yields a notice, no evidence, no crash."""
        _with_pytest_config(tmp_path)

        def _side_effect(command, **kwargs):
            raise FileNotFoundError("pytest")

        with mock.patch.object(
            audit_runner, "run_cached", side_effect=_side_effect
        ):
            result = audit_runner._run_tests_via_test_skill(cwd=tmp_path)
        assert result["success"] is False
        assert "command not found" in result["notice"]
        assert result["failures"] == []

    def test_triage_error_never_crashes_run(self, tmp_path):
        """AC4: a triage helper failure is recorded, never crashes the run."""
        _with_pytest_config(tmp_path)

        def _side_effect(command, **kwargs):
            if "pytest" in command:
                return {
                    "stdout": (
                        "FAILED tests/test_x.py::test_boom - "
                        "AssertionError: boom"
                    ),
                    "stderr": "",
                    "exit_code": 1,
                    "completed_at": 1000.0,
                    "command": command,
                    "git_state": "fingerprint",
                    "cached": False,
                }
            return self._green_run(command, **kwargs)

        with mock.patch.object(
            audit_runner, "run_cached", side_effect=_side_effect
        ), mock.patch(
            "skill.triage.scripts.check_or_create.check_or_create",
            side_effect=RuntimeError("triage boom"),
        ):
            result = audit_runner._run_tests_via_test_skill(cwd=tmp_path)
        assert result["success"] is False
        assert result["triaged"][0]["error"] == "triage boom"

class TestRunTestsPromptInjection:
    """Prompt-content assertions for the --run-tests path (SA-0MSJELSWS002UF60)."""

    def _make_cmd_issue_runner(self, description: str = _GREEN_RUN_DESC,
                               head_sha: str | None = _GREEN_RUN_HEAD):
        return TestAutoGreenRunPromptInjection()._make_cmd_issue_runner(
            description=description, head_sha=head_sha,
        )

    def _mock_cq(self):
        return mock.MagicMock(
            return_value={"success": True, "findings": [], "fixes_applied": 0}
        )

    def _capture_context_prompts(self, cache_result=None, test_run=None,
                                 **cmd_kwargs):
        """Run cmd_issue with query_cached + _run_tests_via_test_skill mocked.

        Returns (prompts, mock_run_tests) so tests can assert both the prompt
        content and whether the test-skill invocation was (not) called.
        """
        mock_runner = self._make_cmd_issue_runner()
        prompts: dict[str, str] = {}
        effective_test_run = test_run or {
            "success": True, "results": [], "failures": [],
            "triaged": [], "notice": "",
        }

        def _fake_call(*args, **kwargs):
            prompts[args[1]] = args[2]
            return {"extracted_text": "[]"}

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log", side_effect=_fake_call
        ), mock.patch.object(
            audit_runner, "query_cached", return_value=cache_result
        ), mock.patch.object(
            audit_runner, "_run_tests_via_test_skill",
            return_value=effective_test_run,
        ) as mock_run_tests, mock.patch(
            "skill.code_review.scripts.code_quality.run_code_quality",
            self._mock_cq(),
        ):
            audit_runner.cmd_issue(
                "TEST-1", persist=False, force=True, runner=mock_runner,
                **cmd_kwargs,
            )
        return prompts, mock_run_tests

    def test_run_tests_with_empty_cache_invokes_test_skill(self):
        """AC1: cache miss + --run-tests → the test skill is invoked and a green
        executed run injects the TEST-SKILL RUN block (no operator round-trip)."""
        prompts, mock_run_tests = self._capture_context_prompts(
            cache_result=None, run_tests=True,
        )
        assert "parent" in prompts
        assert mock_run_tests.call_count == 1
        assert mock_run_tests.call_args.kwargs["parent_work_item_id"] == "TEST-1"
        assert "TEST-SKILL GREEN RUN" in prompts["parent"]
        assert _GREEN_RUN_HEAD in prompts["parent"]
        assert "AUTO-VERIFIED GREEN RUN" not in prompts["parent"]

    def test_without_run_tests_auto_executes_on_cache_miss(self, capsys):
        """F3 (SA-0MSTN5KRF0097TVP): without --run-tests but with a cache miss,
        the test skill IS auto-invoked (default auto-execution path).

        The old gate (SA-0MSQ72BVV0011SRU) that blocked before invocation is
        replaced by F3's auto-execution: rc 0, Phase 1 reached, TEST-SKILL
        GREEN RUN injected. With --no-execute the skill is not invoked
        (fail-open partial)."""
        mock_runner = self._make_cmd_issue_runner()
        prompts: dict[str, str] = {}

        def _fake_call(*args, **kwargs):
            prompts[args[1]] = args[2]
            return {"extracted_text": "[]"}

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log", side_effect=_fake_call
        ), mock.patch.object(
            audit_runner, "query_cached", return_value=None
        ), mock.patch.object(
            audit_runner, "_run_tests_via_test_skill",
            return_value={
                "success": True, "results": [], "failures": [],
                "triaged": [], "notice": "",
            },
        ) as mock_run_tests, mock.patch(
            "skill.code_review.scripts.code_quality.run_code_quality",
            self._mock_cq(),
        ):
            rc = audit_runner.cmd_issue(
                "TEST-1", persist=False, force=True, runner=mock_runner,
            )
        assert rc == 0
        mock_run_tests.assert_called_once()
        assert "parent" in prompts
        assert "TEST-SKILL GREEN RUN" in prompts["parent"]

    def test_green_cache_skips_test_skill_invocation(self):
        """AC1: a green cached run short-circuits the invocation entirely."""
        prompts, mock_run_tests = self._capture_context_prompts(
            cache_result=_AUTO_GREEN_ENTRY, run_tests=True,
        )
        assert "AUTO-VERIFIED GREEN RUN" in prompts["parent"]
        mock_run_tests.assert_not_called()

    def test_failing_test_run_injects_no_block(self):
        """AC1/AC4: a non-green executed run yields no evidence (fail-closed)."""
        prompts, mock_run_tests = self._capture_context_prompts(
            cache_result=None, run_tests=True,
            test_run={
                "success": False, "results": [],
                "failures": [{"test_name": "tests/test_x.py::test_boom"}],
                "triaged": [{"issueId": "SA-TRIAGE-1"}], "notice": "",
            },
        )
        assert "parent" in prompts
        assert mock_run_tests.call_count == 1
        assert "TEST-SKILL GREEN RUN" not in prompts["parent"]
        assert "AUTO-VERIFIED GREEN RUN" not in prompts["parent"]

    def test_log_lines_show_invocation_and_result(self, capsys):
        """AC3: clear log lines show when the test skill is invoked and its result.

        The real ``_run_tests_via_test_skill`` runs here (with ``run_cached``
        mocked green) so the invocation log lines are actually emitted.
        """
        mock_runner = self._make_cmd_issue_runner()

        def _fake_call(*args, **kwargs):
            return {"extracted_text": "[]"}

        def _green_run(command, **kwargs):
            return {
                "stdout": "5 passed in 0.03s",
                "stderr": "",
                "exit_code": 0,
                "completed_at": 1000.0,
                "command": command,
                "git_state": "fingerprint",
                "cached": False,
            }

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log", side_effect=_fake_call
        ), mock.patch.object(
            audit_runner, "query_cached", return_value=None
        ), mock.patch.object(
            audit_runner, "run_cached", side_effect=_green_run
        ), mock.patch(
            "skill.code_review.scripts.code_quality.run_code_quality",
            self._mock_cq(),
        ):
            audit_runner.cmd_issue(
                "TEST-1", persist=False, force=True, runner=mock_runner,
                run_tests=True,
            )
        err = capsys.readouterr().err
        assert "Invoking test skill (run_tests.py)" in err
        assert "Test skill run completed: success=True" in err

class TestRunTestsReportLine:
    """The persisted report records the executed-run evidence (AC1/AC3)."""

    def test_report_includes_test_skill_run_line(self):
        """AC1: 'Test skill run evidence: <sha>' appears near the header."""
        issue = {"id": "TEST-1"}
        acs = [{"text": "AC", "verdict": "met", "evidence": ""}]
        report = audit_runner._assemble_issue_report(
            issue, acs, [], model="m", model_source="local",
            test_skill_run_sha=_GREEN_RUN_HEAD,
        )
        assert f"Test skill run evidence: {_GREEN_RUN_HEAD}" in report
        assert "executed full-suite run" in report
        assert "Automatic green run evidence" not in report

    def test_report_without_test_skill_run_has_no_line(self):
        """Backward compatibility: no executed-run line without evidence."""
        issue = {"id": "TEST-1"}
        acs = [{"text": "AC", "verdict": "met", "evidence": ""}]
        report = audit_runner._assemble_issue_report(
            issue, acs, [], model="m", model_source="local",
        )
        assert "Test skill run evidence" not in report

class TestRunTestsCliFlag:
    """The --run-tests / --no-execute flags parse and default correctly
    (F3 AC2: --no-execute defaults off; auto-execution is the default)."""

    def test_flag_defaults_off(self):
        """AC2: without the flag the audit stays strictly read-only."""
        args = audit_runner.build_parser().parse_args(["issue", "TEST-1"])
        assert args.run_tests is False

    def test_flag_enables(self):
        """The flag enables the test-skill invocation path."""
        args = audit_runner.build_parser().parse_args(
            ["issue", "TEST-1", "--run-tests"]
        )
        assert args.run_tests is True

    def test_no_execute_defaults_off(self):
        """F3 AC2: auto-execution is the default — --no-execute defaults off."""
        args = audit_runner.build_parser().parse_args(["issue", "TEST-1"])
        assert args.no_execute is False

    def test_no_execute_flag_enables(self):
        """F3 AC2: --no-execute opts out of auto-execution on cache miss."""
        args = audit_runner.build_parser().parse_args(
            ["issue", "TEST-1", "--no-execute"]
        )
        assert args.no_execute is True

