"""F1/F3 test contract: audit execution behavior (SA-0MSTMW275003BDWU /
SA-0MSTN5KRF0097TVP).

Pins the audit runner's execution-dependent verification contract:

- AC2 (GREEN since F3): a full-suite cache miss must NOT hard-exit the
  audit. F3 (SA-0MSTN5KRF0097TVP) replaced the pre-flight gate's hard
  block with default auto-execution: on a cache miss the runner invokes
  the test skill to run the repo's actual suite and proceeds to Phase 1.
- AC3 (GREEN since F3): ``AUDIT_NO_EXECUTE=1`` / ``--no-execute`` must
  prevent suite execution even on a cache miss — the hatch suppresses
  auto-execution and the audit proceeds fail-open partial (rc 0).
- AC5 (GREEN since F3): TCE audit E2E — cache miss, npm test suite
  detected, executed via the test skill, TEST-SKILL GREEN RUN block
  injected.
- AC4/AC6 (GREEN since F3): a FAILING suite execution (auto or
  ``--run-tests``) must never block the audit — execution-dependent ACs
  stay partial with failure evidence (no TEST-SKILL GREEN RUN block, so
  'met' cannot be claimed from implementer reports).
"""

from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from skill.audit.scripts import audit_runner

_GREEN_RUN_HEAD = "a1b2c3d4e5f67890abcdef1234567890abcdef12"

_GREEN_RUN_DESC = (
    "# Test\n\n## Acceptance Criteria\n\n"
    "- AC1: full project test suite passes with the new changes\n"
)

_GREEN_TEST_RUN = {
    "success": True,
    "results": [],
    "failures": [],
    "triaged": [],
    "notice": "",
}

_FAILING_TEST_RUN = {
    "success": False,
    "results": [],
    "failures": [
        {
            "test_name": "tests/test_x.py::test_boom",
            "stdout_excerpt": "AssertionError: boom",
            "stack_trace": "AssertionError: boom",
        }
    ],
    "triaged": [{"issueId": "SA-TRIAGE-1", "created": True}],
    "notice": "",
}

# The F3/F4 escape hatch (F4 AC4): the audit must not execute the suite when
# this env var is set, even on a cache miss.
AUDIT_NO_EXECUTE_ENV = "AUDIT_NO_EXECUTE"


def _make_cmd_issue_runner(description: str = _GREEN_RUN_DESC,
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


def _mock_cq():
    return mock.MagicMock(
        return_value={"success": True, "findings": [], "fixes_applied": 0}
    )


def _run_issue(*, cache_return=None, test_run=_GREEN_TEST_RUN,
               run_tests=False, no_execute=False, env=None,
               project_root=None):
    """Run cmd_issue (force, no persist) with mocked pi/cache/test-skill.

    Returns ``(rc, prompts, mock_run_tests)``.
    """
    mock_runner = _make_cmd_issue_runner()
    prompts: dict[str, str] = {}

    def _fake_call(*args, **kwargs):
        prompts[args[1]] = args[2]
        return {"extracted_text": "[]"}

    patches = [
        mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log", side_effect=_fake_call
        ),
        mock.patch.object(
            audit_runner, "query_cached", return_value=cache_return
        ),
        mock.patch.object(
            audit_runner, "_run_tests_via_test_skill", return_value=test_run
        ),
        mock.patch(
            "skill.code_review.scripts.code_quality.run_code_quality",
            _mock_cq(),
        ),
    ]
    if project_root is not None:
        patches.append(mock.patch.object(
            audit_runner, "TARGET_PROJECT_ROOT", project_root
        ))
    with mock.patch.dict(
        audit_runner.os.environ, env or {}, clear=False
    ), contextlib.ExitStack() as stack:
        entered = [stack.enter_context(p) for p in patches]
        mock_run_tests = entered[2]  # the _run_tests_via_test_skill mock
        rc = audit_runner.cmd_issue(
            "TEST-1", persist=False, force=True, runner=mock_runner,
            run_tests=run_tests, no_execute=no_execute,
        )
    return rc, prompts, mock_run_tests


class TestGateNeverBlocksOnCacheMiss:
    """AC2 (GREEN since F3): a full-suite cache miss must not hard-exit the
    audit — the runner auto-executes the suite and proceeds to Phase 1."""

    def test_cache_miss_proceeds_to_phase1(self):
        """A cache miss (no opt-out) must NOT exit non-zero: the suite is
        auto-executed and the audit reaches the Phase 1 parent prompt."""
        rc, prompts, mock_run_tests = _run_issue(cache_return=None)
        assert rc == 0
        assert "parent" in prompts, "Phase 1 must be reached on a cache miss"
        mock_run_tests.assert_called_once()

    def test_gate_message_no_longer_blocks(self, capsys):
        """The 'no green full-suite run is cached' blocking message must not
        be emitted as a hard exit on a cache miss."""
        rc, _, _ = _run_issue(cache_return=None)
        assert rc == 0
        err = capsys.readouterr().err
        assert "Audit blocked" not in err

    def test_no_hard_block_code_path_remains(self):
        """F4 AC6 (SA-0MSTN8CWM003AAU9): no code path in the audit runner may
        emit 'Audit blocked: no green full-suite run is cached at HEAD' as a
        hard exit — the pre-flight gate that produced it was removed."""
        src = Path(audit_runner.__file__).read_text(encoding="utf-8")
        assert "Audit blocked: no green full-suite run is cached at HEAD" not in src
        assert "_preflight_cache_gate" not in src


class TestNoExecuteHatch:
    """AC3 (regression guard, GREEN since F3): ``AUDIT_NO_EXECUTE=1`` must
    prevent suite execution even on a cache miss.

    F3 (SA-0MSTN5KRF0097TVP) introduced the hatch as the opt-out from
    default auto-execution: with the hatch set the audit proceeds fail-open
    partial — no suite execution, no TEST-SKILL GREEN RUN block, rc 0.
    """

    def test_no_execute_env_never_executes_suite(self):
        """With AUDIT_NO_EXECUTE=1 the test skill is never invoked — the
        auto-execution path is suppressed and the audit proceeds fail-open
        partial (rc 0, no executed-run evidence)."""
        rc, prompts, mock_run_tests = _run_issue(
            cache_return=None, env={AUDIT_NO_EXECUTE_ENV: "1"},
        )
        mock_run_tests.assert_not_called()
        assert rc == 0, "the hatch proceeds fail-open partial, never blocks"
        assert all(
            "TEST-SKILL GREEN RUN" not in p for p in prompts.values()
        ), "no executed-run evidence may be injected under the hatch"

    def test_no_execute_flag_never_executes_suite(self):
        """The ``--no-execute`` CLI flag has the same effect as the env var:
        the suite is never executed on a cache miss."""
        rc, prompts, mock_run_tests = _run_issue(
            cache_return=None, no_execute=True,
        )
        mock_run_tests.assert_not_called()
        assert rc == 0
        assert all(
            "TEST-SKILL GREEN RUN" not in p for p in prompts.values()
        )

    def test_flag_defaults_to_execute(self):
        """AC2/AC8: by default (no --no-execute, no env) a cache miss
        auto-executes the suite — the escape hatch is opt-in, not default."""
        rc, prompts, mock_run_tests = _run_issue(cache_return=None)
        assert rc == 0
        mock_run_tests.assert_called_once()
        assert "TEST-SKILL GREEN RUN" in prompts.get("parent", "")

    def test_flag_and_env_both_set(self):
        """The flag and the env var agree (both opt out) — deterministic."""
        rc, prompts, mock_run_tests = _run_issue(
            cache_return=None, no_execute=True,
            env={AUDIT_NO_EXECUTE_ENV: "1"},
        )
        mock_run_tests.assert_not_called()
        assert rc == 0
        assert all(
            "TEST-SKILL GREEN RUN" not in p for p in prompts.values()
        )


class TestTceAuditAutoExecution:
    """AC5 (RED until F3): TCE audit E2E — cache miss, npm test detected,
    executed via the test skill, TEST-SKILL GREEN RUN block injected."""

    def _tce_repo(self, tmp_path: Path) -> Path:
        """A TCE-like project root: package.json with a test script, no
        pytest config, no tests/{unit,node,cli} dirs."""
        (tmp_path / "package.json").write_text(
            json.dumps({"scripts": {"test": "vitest run"}}),
            encoding="utf-8",
        )
        return tmp_path

    def test_tce_audit_auto_executes_npm_test(self, tmp_path: Path):
        """A TCE audit on a cache miss executes the suite via the test skill
        and injects the TEST-SKILL GREEN RUN block into the Phase 1 parent
        prompt."""
        tce_root = self._tce_repo(tmp_path)
        rc, prompts, mock_run_tests = _run_issue(
            cache_return=None, project_root=tce_root,
        )
        assert rc == 0
        mock_run_tests.assert_called_once()
        assert "TEST-SKILL GREEN RUN" in prompts.get("parent", ""), (
            "the executed-run evidence block must be injected into the "
            "Phase 1 parent prompt"
        )
        assert "AUTO-VERIFIED GREEN RUN" not in prompts.get("parent", "")

    def test_tce_audit_records_executed_run_sha(self):
        """Green guard: the executed-run evidence sha renders in the report
        header ('Test skill run evidence', the existing --run-tests line)
        and must keep rendering when F3 reuses the same machinery for
        auto-execution."""
        issue = {"id": "TEST-1"}
        acs = [{"text": "AC", "verdict": "met", "evidence": ""}]
        report = audit_runner._assemble_issue_report(
            issue, acs, [], model="m", model_source="local",
            test_skill_run_sha=_GREEN_RUN_HEAD,
        )
        assert f"Test skill run evidence: {_GREEN_RUN_HEAD}" in report


class TestFailOpenOnExecutionError:
    """AC4/AC6 (regression guard): a FAILING suite execution must never
    block the audit — execution-dependent ACs stay partial with failure
    evidence.

    GREEN since F3 (SA-0MSTN5KRF0097TVP): auto-execution failures fail
    open exactly like the historical ``--run-tests`` failure path — rc 0,
    no TEST-SKILL GREEN RUN block (so 'met' cannot be claimed from
    implementer reports).
    """

    def test_failing_execution_never_blocks(self):
        """A failing executed run yields rc 0 (no hard block) and no
        TEST-SKILL GREEN RUN block (execution-dependent ACs cannot be marked
        'met' from implementer claims)."""
        rc, prompts, mock_run_tests = _run_issue(
            cache_return=None, test_run=_FAILING_TEST_RUN, run_tests=True,
        )
        assert rc == 0
        mock_run_tests.assert_called_once()
        assert "TEST-SKILL GREEN RUN" not in prompts.get("parent", "")

    def test_failing_auto_execution_never_blocks(self):
        """AC4: a failing AUTO-execution (no --run-tests, default F3 path)
        also fails open — rc 0, no executed-green evidence injected."""
        rc, prompts, mock_run_tests = _run_issue(
            cache_return=None, test_run=_FAILING_TEST_RUN,
        )
        assert rc == 0
        mock_run_tests.assert_called_once()
        assert "TEST-SKILL GREEN RUN" not in prompts.get("parent", "")

    def test_failing_execution_leaves_acs_partial(self):
        """Failure evidence is reflected in the report: no executed-green
        attestation line is added for a failing run."""
        issue = {"id": "TEST-1"}
        acs = [{"text": "AC", "verdict": "partial", "evidence": ""}]
        report = audit_runner._assemble_issue_report(
            issue, acs, [], model="m", model_source="local",
        )
        assert "Test skill run evidence" not in report
        assert "AUTO-VERIFIED GREEN RUN" not in report
