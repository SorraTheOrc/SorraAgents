"""Unit tests for the audit strict/permissive test-failure modes.

Covers SA-0MW0JJ36Z000AF57 acceptance criteria at the helper/verdict level:

- AC1: mode resolution precedence (flag > env > permissive default) and that a
  red suite under ``--strict`` blocks (today's verdict).
- AC2: permissive excusal of unrelated unit failures (verdict + evidence block).
- AC3: permissive fail-closed cases (integration, affected, inconclusive) and
  the mixed case.
- AC4: path-convention classification + deterministic relatedness mapping +
  baseline comparison (both outcomes) + fail-closed baseline errors.
- AC5: triage is still invoked for excused failures (mocked — no real suite).
- AC7: freshness respects mode strength.

No real suite is executed and no real ``audit_runner.py issue`` child is
spawned; every execution point is mocked.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from audit.scripts import audit_runner


@pytest.fixture
def clean_env(monkeypatch):
    monkeypatch.delenv(audit_runner.AUDIT_STRICT_ENV, raising=False)
    return monkeypatch


# ── AC1: mode resolution precedence ─────────────────────────────────────────


class TestModeResolution:
    def test_default_is_permissive(self, clean_env):
        assert audit_runner._resolve_strict_mode(None) == (False, "default")

    @pytest.mark.parametrize("flag", [True, False])
    def test_flag_wins_over_env(self, clean_env, flag):
        clean_env.setenv(audit_runner.AUDIT_STRICT_ENV,
                         "0" if flag else "1")
        strict, source = audit_runner._resolve_strict_mode(flag)
        assert strict is flag
        assert source == "flag"

    @pytest.mark.parametrize("value,expected", [
        ("1", True), ("true", True), ("yes", True), ("on", True),
        ("0", False), ("false", False), ("no", False), ("off", False),
    ])
    def test_env_truthiness(self, monkeypatch, value, expected):
        monkeypatch.setenv(audit_runner.AUDIT_STRICT_ENV, value)
        assert audit_runner._resolve_strict_mode(None) == (expected, "env")

    def test_blank_env_falls_through_to_default(self, monkeypatch):
        monkeypatch.setenv(audit_runner.AUDIT_STRICT_ENV, "  ")
        assert audit_runner._resolve_strict_mode(None) == (False, "default")

    def test_strict_reproduces_red_verdict(self):
        """AC1: --strict → any failure blocks (today's verdict)."""
        evaluation = audit_runner._evaluate_red_suite(
            [{"test_name": "tests/unit/test_x.py::test_a"}], strict=True,
        )
        assert evaluation["outcome"] == "blocked"
        assert evaluation["mode"] == "strict"
        assert evaluation["excused"] == []


# ── AC4: scope classification table ─────────────────────────────────────────


class TestScopeClassification:
    @pytest.mark.parametrize("test_name,expected", [
        ("tests/integration/test_a.py::t", "integration"),
        ("tests/e2e/test_b.py::t", "integration"),
        ("tests/manual/test_c.py::t", "integration"),
        ("skill/foo_check_e2e.py::t", "integration"),
        ("skill/foo/integration_helpers.py::t", "integration"),
        ("tests/unit/test_plain.py::t", "unit"),
        ("tests/test_top.py::t", "unit"),
        # Unclassifiable / no path → fail closed to integration.
        ("<suite exited 1>: pytest -q", "integration"),
        ("", "integration"),
    ])
    def test_pattern_table(self, test_name, expected):
        assert audit_runner._classify_test_failure_scope(
            {"test_name": test_name}
        ) == expected


# ── AC4: relatedness mapping ────────────────────────────────────────────────


class TestRelatedness:
    def test_changed_test_file_is_affected(self, tmp_path):
        verdict, evidence = audit_runner._failure_relatedness(
            {"test_name": "tests/unit/test_z.py::t"},
            touched_files=["tests/unit/test_z.py"], key_files=[],
            project_root=tmp_path,
        )
        assert verdict == "affected"
        assert "changed file" in evidence

    def test_changed_shared_infra_is_affected_for_any_failure(self, tmp_path):
        verdict, evidence = audit_runner._failure_relatedness(
            {"test_name": "tests/unit/unrelated.py::t"},
            touched_files=["conftest.py"], key_files=[],
            project_root=tmp_path,
        )
        assert verdict == "affected"
        assert "shared test infrastructure" in evidence

    def test_import_of_changed_module_is_affected(self, tmp_path):
        test_file = tmp_path / "tests" / "unit" / "test_importer.py"
        test_file.parent.mkdir(parents=True)
        test_file.write_text("from audit.scripts import audit_runner\n", encoding="utf-8")
        verdict, evidence = audit_runner._failure_relatedness(
            {"test_name": "tests/unit/test_importer.py::t"},
            touched_files=["skill/audit/scripts/audit_runner.py"],
            key_files=[],
            project_root=tmp_path,
        )
        assert verdict == "affected"
        assert "imports changed module" in evidence

    def test_no_link_is_inconclusive(self, tmp_path):
        verdict, _ = audit_runner._failure_relatedness(
            {"test_name": "tests/unit/test_other.py::t"},
            touched_files=["skill/audit/scripts/audit_runner.py"],
            key_files=[],
            project_root=tmp_path,
        )
        assert verdict == "inconclusive"

    def test_no_changed_files_is_inconclusive(self, tmp_path):
        verdict, _ = audit_runner._failure_relatedness(
            {"test_name": "tests/unit/test_other.py::t"},
            touched_files=None, key_files=None, project_root=tmp_path,
        )
        assert verdict == "inconclusive"


# ── AC4: baseline comparison (both outcomes + fail closed) ──────────────────


class TestBaseline:
    def _proc(self, returncode=0, stdout="", stderr=""):
        return subprocess.CompletedProcess(args=[], returncode=returncode,
                                           stdout=stdout, stderr=stderr)

    def test_fails_identically_is_unrelated(self, tmp_path):
        def fake_runner(argv, cwd):
            return self._proc(0)
        verdicts = audit_runner._baseline_unrelated(
            [{"test_name": "tests/unit/t.py::t"}], tmp_path, "abc123",
            run_test=lambda failure, wt: True,  # still fails at baseline
            worktree_runner=fake_runner,
        )
        assert verdicts[0][0] == "unrelated"

    def test_passes_at_baseline_is_affected(self, tmp_path):
        def fake_runner(argv, cwd):
            return self._proc(0)
        verdicts = audit_runner._baseline_unrelated(
            [{"test_name": "tests/unit/t.py::t"}], tmp_path, "abc123",
            run_test=lambda failure, wt: False,  # passes at baseline
            worktree_runner=fake_runner,
        )
        assert verdicts[0][0] == "affected"

    def test_worktree_failure_fails_closed(self, tmp_path):
        def fake_runner(argv, cwd):
            return self._proc(1, stderr="worktree add failed")
        verdicts = audit_runner._baseline_unrelated(
            [{"test_name": "tests/unit/t.py::t"}], tmp_path, "abc123",
            run_test=lambda failure, wt: False,
            worktree_runner=fake_runner,
        )
        assert verdicts[0][0] == "affected"

    def test_no_baseline_commit_fails_closed(self, tmp_path):
        verdicts = audit_runner._baseline_unrelated(
            [{"test_name": "tests/unit/t.py::t"}], tmp_path, None,
        )
        assert verdicts[0][0] == "affected"

    def test_executor_error_fails_closed(self, tmp_path):
        def fake_runner(argv, cwd):
            return self._proc(0)

        def boom(failure, wt):
            raise RuntimeError("exec exploded")

        verdicts = audit_runner._baseline_unrelated(
            [{"test_name": "tests/unit/t.py::t"}], tmp_path, "abc123",
            run_test=boom, worktree_runner=fake_runner,
        )
        assert verdicts[0][0] == "affected"


# ── AC2/AC3: verdict evaluation ─────────────────────────────────────────────


class TestVerdictEvaluation:
    def _unrelated_baseline(self, failures, root, base):
        return {i: ("unrelated", "fails identically") for i in range(len(failures))}

    def test_permissive_excuses_unrelated_unit(self, tmp_path):
        evaluation = audit_runner._evaluate_red_suite(
            [{"test_name": "tests/unit/test_other.py::t"}],
            strict=False,
            touched_files=["skill/audit/scripts/audit_runner.py"],
            key_files=[], project_root=tmp_path,
            baseline_commit="abc123",
            baseline_runner=self._unrelated_baseline,
        )
        assert evaluation["outcome"] == "excused"
        assert evaluation["excused"] == ["tests/unit/test_other.py::t"]
        assert evaluation["failures"][0]["relatedness"] == "unrelated"

    def test_permissive_blocks_integration_failure(self, tmp_path):
        evaluation = audit_runner._evaluate_red_suite(
            [{"test_name": "tests/integration/test_i.py::t"}],
            strict=False, touched_files=[], key_files=[], project_root=tmp_path,
            baseline_commit="abc123", baseline_runner=self._unrelated_baseline,
        )
        assert evaluation["outcome"] == "blocked"
        assert evaluation["failures"][0]["scope"] == "integration"

    def test_permissive_blocks_affected_unit(self, tmp_path):
        evaluation = audit_runner._evaluate_red_suite(
            [{"test_name": "tests/unit/test_z.py::t"}],
            strict=False, touched_files=["tests/unit/test_z.py"],
            key_files=[], project_root=tmp_path,
        )
        assert evaluation["outcome"] == "blocked"
        assert evaluation["failures"][0]["relatedness"] == "affected"

    def test_permissive_blocks_inconclusive_without_baseline(self, tmp_path):
        evaluation = audit_runner._evaluate_red_suite(
            [{"test_name": "tests/unit/test_other.py::t"}],
            strict=False,
            touched_files=["skill/audit/scripts/audit_runner.py"],
            key_files=[], project_root=tmp_path,
            baseline_commit=None,  # no baseline → fail closed
        )
        assert evaluation["outcome"] == "blocked"
        assert evaluation["failures"][0]["relatedness"] == "affected"

    def test_mixed_excusable_and_blocking_fails(self, tmp_path):
        evaluation = audit_runner._evaluate_red_suite(
            [
                {"test_name": "tests/integration/test_i.py::t"},
                {"test_name": "tests/unit/test_other.py::t"},
            ],
            strict=False,
            touched_files=["skill/audit/scripts/audit_runner.py"],
            key_files=[], project_root=tmp_path,
            baseline_commit="abc123", baseline_runner=self._unrelated_baseline,
        )
        assert evaluation["outcome"] == "blocked"
        assert evaluation["blocking"] == ["tests/integration/test_i.py::t"]
        assert evaluation["excused"] == ["tests/unit/test_other.py::t"]

    def test_red_with_no_parseable_failures_blocks(self, tmp_path):
        evaluation = audit_runner._evaluate_red_suite([], strict=False)
        assert evaluation["outcome"] == "blocked"

    def test_excused_prompt_block_names_evidence(self, tmp_path):
        evaluation = audit_runner._evaluate_red_suite(
            [{"test_name": "tests/unit/test_other.py::t"}],
            strict=False,
            touched_files=["skill/audit/scripts/audit_runner.py"],
            key_files=[], project_root=tmp_path,
            baseline_commit="abc123", baseline_runner=self._unrelated_baseline,
        )
        block = audit_runner._excused_red_prompt_block(evaluation, "deadbeef")
        assert "EXCUSED-RED SUITE" in block
        assert "tests/unit/test_other.py::t" in block
        assert "deadbeef" in block


# ── AC5: triage preserved for excused failures ──────────────────────────────


class TestTriagePreserved:
    def _run(self, tmp_path, strict):
        failure = {
            "test_name": "tests/unit/test_other.py::t",
            "stdout_excerpt": "FAILED",
            "stack_trace": "",
            "suite_command": "pytest -q",
        }
        red = {"stdout": "FAILED", "stderr": "", "exit_code": 1}
        triaged_calls: list[dict] = []

        def fake_check_or_create(payload):
            triaged_calls.append(payload)
            return {"id": "SA-TRIAGE"}

        fake_module = mock.MagicMock()
        fake_module.check_or_create = fake_check_or_create
        with mock.patch.object(audit_runner, "full_suite_commands",
                               return_value=["pytest -q"]), \
             mock.patch.object(audit_runner, "suite_timeout_per_command",
                               return_value=30), \
             mock.patch.object(audit_runner, "run_cached", return_value=red), \
             mock.patch.object(audit_runner, "parse_failures",
                               return_value=[dict(failure)]), \
             mock.patch.dict(sys.modules,
                             {"triage.scripts.check_or_create": fake_module}):
            result = audit_runner._run_tests_via_test_skill(
                cwd=tmp_path, strict=strict,
                touched_files=["skill/audit/scripts/audit_runner.py"],
                key_files=[], baseline_commit="abc123",
                baseline_runner=lambda f, r, b: {
                    i: ("unrelated", "fails identically") for i in range(len(f))
                },
            )
        return result, triaged_calls

    def test_excused_failures_are_still_triaged(self, tmp_path):
        result, triaged = self._run(tmp_path, strict=False)
        assert result["excused"] is True
        assert result["red_evaluation"]["outcome"] == "excused"
        assert triaged, "triage must be invoked for excused failures too"
        assert triaged[0]["test_name"] == "tests/unit/test_other.py::t"

    def test_strict_failures_are_triaged_and_block(self, tmp_path):
        result, triaged = self._run(tmp_path, strict=True)
        assert result["success"] is False
        assert result["excused"] is False
        assert result["red_evaluation"]["outcome"] == "blocked"
        assert triaged, "triage must be invoked for blocking failures"


# ── AC5/AC7: report section + freshness mode strength ───────────────────────


class TestPersistenceAndFreshness:
    def test_mode_section_lists_evidence(self, tmp_path):
        evaluation = audit_runner._evaluate_red_suite(
            [{"test_name": "tests/unit/test_other.py::t"}],
            strict=False,
            touched_files=["skill/audit/scripts/audit_runner.py"],
            key_files=[], project_root=tmp_path,
            baseline_commit="abc123",
            baseline_runner=lambda f, r, b: {0: ("unrelated", "fails identically")},
        )
        section = audit_runner._format_test_failure_mode_section(
            evaluation, strict=False, source="flag",
        )
        assert audit_runner.TEST_FAILURE_MODE_SECTION in section
        assert "Mode: permissive (source: flag)" in section
        assert "unrelated" in section
        assert "abc123" in section

    @pytest.mark.parametrize("stored_mode,required,allowed", [
        ("permissive", True, False),   # strict never reuses permissive
        ("strict", False, True),       # permissive may reuse strict
        ("strict", True, True),
        ("permissive", False, True),
    ])
    def test_mode_strength(self, stored_mode, required, allowed):
        report = (
            f"Ready to close: Yes\n\n"
            f"Test failure mode: {stored_mode} (source: flag)"
        )
        assert audit_runner._freshness_mode_allows(report, required) is allowed

    def test_legacy_report_treated_as_permissive(self):
        assert audit_runner._freshness_mode_allows(
            "Ready to close: Yes", True
        ) is False

    def test_failing_verdict_is_always_rerun(self):
        report = "Ready to close: No\n\nTest failure mode: permissive (source: flag)"
        assert audit_runner._freshness_mode_allows(report, True) is True

    def test_extract_mode(self):
        assert audit_runner._extract_test_failure_mode(
            "Test failure mode: strict (source: env)"
        ) == "strict"
        assert audit_runner._extract_test_failure_mode("no mode line") is None


# ── AC4: cached-red permissive classification ──────────────────────────────


class TestCachedRed:
    def _patch(self, failures):
        return (
            mock.patch.object(audit_runner, "full_suite_commands",
                              return_value=["pytest -q"]),
            mock.patch.object(
                audit_runner, "query_cached",
                return_value={"exit_code": 1,
                              "stdout": "FAILED", "stderr": ""},
            ),
            mock.patch.object(audit_runner, "parse_failures",
                              return_value=[dict(f) for f in failures]),
        )

    def test_cached_red_excused_when_unrelated_unit(self, tmp_path):
        p1, p2, p3 = self._patch([{"test_name": "tests/unit/test_other.py::t"}])
        with p1, p2, p3:
            evaluation = audit_runner._cached_red_evaluation(
                tmp_path, strict=False,
                touched_files=["skill/audit/scripts/audit_runner.py"],
                key_files=[], baseline_commit="abc123",
                baseline_runner=lambda f, r, b: {
                    0: ("unrelated", "fails identically"),
                },
            )
        assert evaluation is not None
        assert evaluation["outcome"] == "excused"

    def test_cached_red_no_parseable_returns_none(self, tmp_path):
        p1, p2, p3 = self._patch([])
        with p1, p2, p3:
            evaluation = audit_runner._cached_red_evaluation(
                tmp_path, strict=False, touched_files=[], key_files=[],
                baseline_commit="abc123",
            )
        assert evaluation is None
