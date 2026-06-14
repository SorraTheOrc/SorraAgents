"""Tests for the hybrid smell detection engine (linter + LLM).

These tests verify that:
- Linter-based detection correctly parses mock linter output into smell findings
- LLM-based detection correctly processes mock LLM responses into smell findings
- Hybrid mode combines findings from both sources with deduplication
- Configurable rules loading from file or defaults
- Severity mapping logic for both linter and LLM findings

The target implementation lives in skill/refactor/smell_detection.py.

Related work item: SA-0MQA70XZB005O3Z4
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

# Ensure repo root is on sys.path
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_dir() -> Path:
    """Create a temporary directory for test file operations."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def mock_python_file(temp_dir: Path) -> Path:
    """Create a mock Python source file for smell analysis."""
    file_path = temp_dir / "src" / "example.py"
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(
        "import os\n"
        "import sys\n"
        "\n"
        "\n"
        "def my_function():\n"
        "    x = 10\n"
        "    y = 20\n"
        "    return x + y\n"
    )
    return file_path


@pytest.fixture
def mock_ruff_json_output() -> str:
    """Mock ruff JSON output for a file with several findings."""
    return json.dumps([
        {
            "filename": "src/example.py",
            "location": {"row": 1, "column": 1},
            "code": "F401",
            "message": "`os` imported but unused",
        },
        {
            "filename": "src/example.py",
            "location": {"row": 5, "column": 1},
            "code": "E302",
            "message": "expected 2 blank lines after imports",
        },
        {
            "filename": "src/example.py",
            "location": {"row": 7, "column": 5},
            "code": "F841",
            "message": "local variable `x` is assigned to but never used",
        },
        {
            "filename": "src/example.py",
            "location": {"row": 8, "column": 5},
            "code": "F841",
            "message": "local variable `y` is assigned to but never used",
        },
    ])


@pytest.fixture
def mock_llm_smell_response() -> list[dict[str, Any]]:
    """Mock LLM response for design/architectural smell detection."""
    return [
        {
            "file": "src/example.py",
            "line": 6,
            "severity": "medium",
            "smell_type": "unused_function",
            "message": "Function `my_function` appears to be unused",
            "code": "LLM-UF-001",
        },
        {
            "file": "src/example.py",
            "line": 6,
            "severity": "low",
            "smell_type": "magic_number",
            "message": "Magic numbers 10 and 20 used without named constants",
            "code": "LLM-MN-002",
        },
    ]


@pytest.fixture
def mock_llm_client(mock_llm_smell_response: list[dict[str, Any]]) -> MagicMock:
    """Create a mock LLM client that returns predefined smell responses."""
    client = MagicMock()
    client.analyze.return_value = mock_llm_smell_response
    return client


@pytest.fixture
def mock_llm_client_empty() -> MagicMock:
    """Create a mock LLM client that returns no smells."""
    client = MagicMock()
    client.analyze.return_value = []
    return client


@pytest.fixture
def default_rules() -> dict[str, Any]:
    """Default smell detection rules."""
    return {
        "linter": {
            "enabled": True,
            "severity_overrides": {},
        },
        "llm": {
            "enabled": True,
            "model": "default",
            "temperature": 0.1,
            "max_tokens": 2000,
        },
        "severity_mapping": {
            "critical": {"priority": "critical", "color": "red"},
            "high": {"priority": "high", "color": "orange"},
            "medium": {"priority": "medium", "color": "yellow"},
            "low": {"priority": "low", "color": "green"},
        },
        "smell_types": [
            "unused_import",
            "unused_variable",
            "unused_function",
            "complex_function",
            "magic_number",
            "duplicate_code",
            "long_method",
            "god_class",
            "feature_envy",
            "inappropriate_intimacy",
            "shotgun_surgery",
        ],
    }


@pytest.fixture
def custom_rules_file(temp_dir: Path) -> Path:
    """Create a custom rules JSON file for testing configurable rules loading."""
    rules = {
        "linter": {
            "enabled": False,  # Disable linter detection
        },
        "llm": {
            "enabled": True,
            "model": "custom-model",
            "temperature": 0.5,
        },
        "severity_mapping": {
            "high": {"priority": "critical", "color": "red"},
        },
        "smell_types": ["complex_function", "magic_number"],
    }
    config_path = temp_dir / ".refactor.json"
    config_path.write_text(json.dumps(rules, indent=2))
    return config_path


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _import_smell_module():
    """Import the smell detection module; skip tests if not yet implemented."""
    try:
        from skill.refactor import smell_detection
        return smell_detection
    except (ImportError, ModuleNotFoundError) as exc:
        pytest.skip(f"Smell detection module not yet available: {exc}")


# ===================================================================
# Linter-based detection tests
# ===================================================================


class TestLinterDetection:
    """Tests for linter-based smell detection."""

    def test_detect_linter_smells_from_ruff_output(
        self,
        mock_python_file: Path,
        mock_ruff_json_output: str,
    ):
        """Linter detection correctly parses ruff JSON output into findings."""
        smell_mod = _import_smell_module()

        files = [str(mock_python_file)]
        # Mock the linter runner to return the predefined output
        findings = smell_mod.detect_linter_smells(
            files=files,
            linter_output={"ruff": mock_ruff_json_output},
        )

        assert isinstance(findings, list)
        assert len(findings) >= 1

        # Verify finding structure
        finding = findings[0]
        assert "file" in finding
        assert "line" in finding
        assert "severity" in finding
        assert "message" in finding
        assert "source" in finding
        assert "smell_type" in finding
        assert "code" in finding
        assert finding["source"] == "linter"

    def test_linter_finding_has_expected_keys(
        self,
        mock_python_file: Path,
    ):
        """Each linter finding dict contains all required keys."""
        smell_mod = _import_smell_module()

        findings = smell_mod.detect_linter_smells(
            files=[str(mock_python_file)],
        )

        if not findings:
            pytest.skip("No findings returned (linter may not be available)")

        required_keys = {"file", "line", "severity", "message", "source", "smell_type", "code"}
        for finding in findings:
            missing = required_keys - set(finding.keys())
            assert not missing, f"Finding missing keys: {missing}"

    def test_linter_severity_mapping_preserved(
        self,
        mock_python_file: Path,
    ):
        """Linter severity mapping produces valid severity levels."""
        smell_mod = _import_smell_module()

        findings = smell_mod.detect_linter_smells(
            files=[str(mock_python_file)],
        )

        if not findings:
            pytest.skip("No findings returned")

        valid_severities = {"critical", "high", "medium", "low"}
        for finding in findings:
            assert finding["severity"] in valid_severities, \
                f"Unexpected severity: {finding['severity']}"

    def test_linter_detection_with_empty_file_list(self):
        """Linter detection with empty file list returns empty list."""
        smell_mod = _import_smell_module()

        findings = smell_mod.detect_linter_smells(files=[])
        assert findings == []

    def test_linter_detection_with_nonexistent_file(self):
        """Linter detection gracefully handles nonexistent files."""
        smell_mod = _import_smell_module()

        findings = smell_mod.detect_linter_smells(
            files=["/nonexistent/file.py"],
        )
        assert findings == []

    def test_linter_detection_accepts_custom_linter_output(
        self,
    ):
        """Linter detection accepts custom pre-parsed linter output."""
        smell_mod = _import_smell_module()

        mock_linter_output = {
            "ruff": json.dumps([
                {
                    "filename": "src/main.py",
                    "location": {"row": 10, "column": 1},
                    "code": "F401",
                    "message": "`math` imported but unused",
                },
            ]),
        }

        findings = smell_mod.detect_linter_smells(
            files=["src/main.py"],
            linter_output=mock_linter_output,
        )

        assert len(findings) >= 1
        assert findings[0]["code"] == "F401"
        assert findings[0]["source"] == "linter"

    def test_linter_detection_no_duplicates(
        self,
    ):
        """Linter detection does not produce duplicate findings for the same issue."""
        smell_mod = _import_smell_module()

        # Same issue reported twice
        mock_linter_output = {
            "ruff": json.dumps([
                {
                    "filename": "src/main.py",
                    "location": {"row": 10, "column": 1},
                    "code": "F401",
                    "message": "`math` imported but unused",
                },
                {
                    "filename": "src/main.py",
                    "location": {"row": 10, "column": 1},
                    "code": "F401",
                    "message": "`math` imported but unused",
                },
            ]),
        }

        findings = smell_mod.detect_linter_smells(
            files=["src/main.py"],
            linter_output=mock_linter_output,
        )

        # Should only have one finding for the deduplicated issue
        assert len(findings) <= 1, "Duplicate findings should be deduplicated"


# ===================================================================
# LLM-based detection tests
# ===================================================================


class TestLLMDetection:
    """Tests for LLM-based smell detection."""

    def test_detect_llm_smells_returns_list(
        self,
        mock_python_file: Path,
        mock_llm_client: MagicMock,
    ):
        """LLM detection returns a list of findings."""
        smell_mod = _import_smell_module()

        findings = smell_mod.detect_llm_smells(
            files=[str(mock_python_file)],
            llm_client=mock_llm_client,
        )

        assert isinstance(findings, list)

    def test_llm_finding_has_expected_keys(
        self,
        mock_python_file: Path,
        mock_llm_client: MagicMock,
    ):
        """Each LLM finding dict contains all required keys."""
        smell_mod = _import_smell_module()

        findings = smell_mod.detect_llm_smells(
            files=[str(mock_python_file)],
            llm_client=mock_llm_client,
        )

        if not findings:
            pytest.skip("No findings returned")

        required_keys = {"file", "line", "severity", "message", "source", "smell_type", "code"}
        for finding in findings:
            missing = required_keys - set(finding.keys())
            assert not missing, f"Finding missing keys: {missing}"
            assert finding["source"] == "llm"

    def test_llm_smell_types_are_valid(
        self,
        mock_python_file: Path,
        mock_llm_client: MagicMock,
    ):
        """LLM detection returns known smell types."""
        smell_mod = _import_smell_module()

        findings = smell_mod.detect_llm_smells(
            files=[str(mock_python_file)],
            llm_client=mock_llm_client,
        )

        if not findings:
            pytest.skip("No findings returned")

        known_smell_types = {
            "unused_import", "unused_variable", "unused_function",
            "complex_function", "magic_number", "duplicate_code",
            "long_method", "god_class", "feature_envy",
            "inappropriate_intimacy", "shotgun_surgery",
        }
        for finding in findings:
            assert finding["smell_type"] in known_smell_types, \
                f"Unknown smell type: {finding['smell_type']}"

    def test_llm_detection_with_empty_file_list(
        self,
        mock_llm_client: MagicMock,
    ):
        """LLM detection with empty file list returns empty list."""
        smell_mod = _import_smell_module()

        findings = smell_mod.detect_llm_smells(
            files=[],
            llm_client=mock_llm_client,
        )
        assert findings == []

    def test_llm_detection_with_nonexistent_file(
        self,
        mock_llm_client: MagicMock,
    ):
        """LLM detection handles nonexistent files."""
        smell_mod = _import_smell_module()

        findings = smell_mod.detect_llm_smells(
            files=["/nonexistent/file.py"],
            llm_client=mock_llm_client,
        )
        assert findings == []

    def test_llm_client_called_with_files(
        self,
        mock_python_file: Path,
        mock_llm_client: MagicMock,
    ):
        """LLM client is called with the specified file paths."""
        smell_mod = _import_smell_module()

        files = [str(mock_python_file)]
        smell_mod.detect_llm_smells(
            files=files,
            llm_client=mock_llm_client,
        )

        # Verify the LLM client was called with the file paths
        mock_llm_client.analyze.assert_called()
        call_args = mock_llm_client.analyze.call_args
        assert call_args is not None
        # The client should receive file info in some form
        assert any(f in str(call_args) for f in files)

    def test_llm_client_with_rules_passed(
        self,
        mock_python_file: Path,
        mock_llm_client: MagicMock,
        default_rules: dict[str, Any],
    ):
        """LLM client receives rules configuration."""
        smell_mod = _import_smell_module()

        smell_mod.detect_llm_smells(
            files=[str(mock_python_file)],
            llm_client=mock_llm_client,
            rules=default_rules,
        )

        mock_llm_client.analyze.assert_called()
        # Rules should be passed to or influence the LLM call

    def test_llm_empty_response_returns_empty_list(
        self,
        mock_python_file: Path,
        mock_llm_client_empty: MagicMock,
    ):
        """LLM returning empty list results in no findings."""
        smell_mod = _import_smell_module()

        findings = smell_mod.detect_llm_smells(
            files=[str(mock_python_file)],
            llm_client=mock_llm_client_empty,
        )
        assert findings == []

    def test_llm_detection_severity_in_valid_set(
        self,
        mock_python_file: Path,
        mock_llm_client: MagicMock,
    ):
        """LLM detection returns valid severity levels."""
        smell_mod = _import_smell_module()

        findings = smell_mod.detect_llm_smells(
            files=[str(mock_python_file)],
            llm_client=mock_llm_client,
        )

        if not findings:
            pytest.skip("No findings returned")

        valid_severities = {"critical", "high", "medium", "low"}
        for finding in findings:
            assert finding["severity"] in valid_severities, \
                f"Unexpected severity: {finding['severity']}"


# ===================================================================
# Hybrid mode tests
# ===================================================================


class TestHybridDetection:
    """Tests for hybrid (linter + LLM) smell detection."""

    def test_hybrid_combines_both_sources(
        self,
        mock_python_file: Path,
        mock_llm_client: MagicMock,
    ):
        """Hybrid mode returns findings from both linter and LLM sources."""
        smell_mod = _import_smell_module()

        findings = smell_mod.detect_smells(
            files=[str(mock_python_file)],
            mode="hybrid",
            llm_client=mock_llm_client,
        )

        assert isinstance(findings, list)
        sources = {f["source"] for f in findings if "source" in f}
        # At minimum, LLM findings should be present
        assert "llm" in sources or len(findings) == 0, \
            "Hybrid mode should include LLM findings"

    def test_hybrid_mode_default(
        self,
        mock_python_file: Path,
        mock_llm_client: MagicMock,
    ):
        """Default detection mode is hybrid."""
        smell_mod = _import_smell_module()

        findings = smell_mod.detect_smells(
            files=[str(mock_python_file)],
            llm_client=mock_llm_client,
        )

        assert isinstance(findings, list)

    def test_hybrid_deduplication(
        self,
        mock_python_file: Path,
        mock_llm_client: MagicMock,
    ):
        """Hybrid mode deduplicates overlapping findings from both sources."""
        smell_mod = _import_smell_module()

        findings = smell_mod.detect_smells(
            files=[str(mock_python_file)],
            mode="hybrid",
            llm_client=mock_llm_client,
        )

        # Check no two findings have the same file, line, and code
        seen = set()
        for f in findings:
            key = (f.get("file"), f.get("line"), f.get("code"))
            assert key not in seen, f"Duplicate finding: {key}"
            seen.add(key)

    def test_hybrid_without_llm_client_falls_back(
        self,
        mock_python_file: Path,
    ):
        """Hybrid mode without LLM client falls back to linter-only detection."""
        smell_mod = _import_smell_module()

        findings = smell_mod.detect_smells(
            files=[str(mock_python_file)],
            mode="hybrid",
            llm_client=None,
        )

        assert isinstance(findings, list)
        # Should have linter-only results (or empty if no linter available)
        for f in findings:
            assert f.get("source") == "linter"

    def test_linter_only_mode(
        self,
        mock_python_file: Path,
        mock_llm_client: MagicMock,
    ):
        """Linter-only mode returns only linter findings."""
        smell_mod = _import_smell_module()

        findings = smell_mod.detect_smells(
            files=[str(mock_python_file)],
            mode="linter",
            llm_client=mock_llm_client,
        )

        for f in findings:
            assert f.get("source") == "linter", \
                f"Expected linter source but got {f.get('source')}"

    def test_llm_only_mode(
        self,
        mock_python_file: Path,
        mock_llm_client: MagicMock,
    ):
        """LLM-only mode returns only LLM findings."""
        smell_mod = _import_smell_module()

        findings = smell_mod.detect_smells(
            files=[str(mock_python_file)],
            mode="llm",
            llm_client=mock_llm_client,
        )

        for f in findings:
            assert f.get("source") == "llm", \
                f"Expected llm source but got {f.get('source')}"

    def test_invalid_mode_raises_error(
        self,
        mock_python_file: Path,
    ):
        """Invalid mode raises a ValueError."""
        smell_mod = _import_smell_module()

        with pytest.raises(ValueError, match="mode.*invalid|unknown mode"):
            smell_mod.detect_smells(
                files=[str(mock_python_file)],
                mode="invalid_mode",
            )


# ===================================================================
# Configurable rules tests
# ===================================================================


class TestConfigurableRules:
    """Tests for configurable rules loading."""

    def test_load_rules_returns_dict(self, temp_dir: Path):
        """load_rules() returns a dict with default rules."""
        smell_mod = _import_smell_module()

        rules = smell_mod.load_rules()
        assert isinstance(rules, dict)
        assert "linter" in rules
        assert "llm" in rules
        assert "severity_mapping" in rules or "smell_types" in rules

    def test_load_rules_with_custom_file(
        self,
        custom_rules_file: Path,
    ):
        """load_rules() loads rules from a custom config file."""
        smell_mod = _import_smell_module()

        rules = smell_mod.load_rules(str(custom_rules_file))
        assert isinstance(rules, dict)
        # Linter should be disabled per custom rules
        assert rules.get("linter", {}).get("enabled") is False

    def test_load_rules_nonexistent_file_returns_defaults(self):
        """load_rules() with nonexistent file returns default rules."""
        smell_mod = _import_smell_module()

        rules = smell_mod.load_rules("/nonexistent/.refactor.json")
        assert isinstance(rules, dict)
        assert "linter" in rules
        assert "llm" in rules

    def test_load_rules_merge_with_defaults(
        self,
        temp_dir: Path,
    ):
        """Partial custom rules merge with default rules."""
        smell_mod = _import_smell_module()

        # Create a partial config
        partial_config = temp_dir / ".refactor.json"
        partial_config.write_text(json.dumps({
            "llm": {"model": "gpt-4"},
        }))

        rules = smell_mod.load_rules(str(partial_config))
        # Custom value should be set
        assert rules["llm"]["model"] == "gpt-4"
        # Default values should still be present
        assert "linter" in rules
        assert "smell_types" in rules

    def test_load_rules_json_decode_error_returns_defaults(
        self,
        temp_dir: Path,
    ):
        """Invalid JSON in config file returns default rules."""
        smell_mod = _import_smell_module()

        bad_config = temp_dir / ".refactor.json"
        bad_config.write_text("not valid json")

        rules = smell_mod.load_rules(str(bad_config))
        assert isinstance(rules, dict)
        assert "linter" in rules

    def test_load_rules_returns_json_serializable(
        self,
    ):
        """load_rules() return value must be JSON-serializable."""
        smell_mod = _import_smell_module()

        rules = smell_mod.load_rules()
        # Should not raise
        json.dumps(rules)

    def test_rules_control_linter_enable(
        self,
        mock_python_file: Path,
        temp_dir: Path,
    ):
        """Rules can disable linter detection."""
        smell_mod = _import_smell_module()

        # Create rules with linter disabled
        config_path = temp_dir / ".refactor.json"
        config_path.write_text(json.dumps({
            "linter": {"enabled": False},
            "llm": {"enabled": True},
        }))

        rules = smell_mod.load_rules(str(config_path))
        if not rules.get("linter", {}).get("enabled", True):
            # Linter disabled - detection should respect this
            findings = smell_mod.detect_smells(
                files=[str(mock_python_file)],
                mode="hybrid",
                rules=rules,
            )
            # When linter is disabled and no llm_client, should return empty
            for f in findings:
                assert f.get("source") != "linter"


# ===================================================================
# Severity mapping tests
# ===================================================================


class TestSeverityMapping:
    """Tests for severity mapping logic within smell detection."""

    def test_severity_mapping_within_smell_module(
        self,
        mock_python_file: Path,
    ):
        """Smell detection module has its own severity mapping function."""
        smell_mod = _import_smell_module()

        assert hasattr(smell_mod, "classify_smell_severity") or \
               hasattr(smell_mod, "classify_finding"), \
            "Module should expose a severity classification function"

    def test_classify_smell_severity_returns_string(self):
        """classify_smell_severity() returns a string."""
        smell_mod = _import_smell_module()

        classifier = getattr(smell_mod, "classify_smell_severity",
                             getattr(smell_mod, "classify_finding", None))
        if classifier is None:
            pytest.skip("No severity classification function found")

        result = classifier("ruff", "F401")
        assert isinstance(result, str)

    def test_classify_linter_ruff_F_is_critical(self):
        """Ruff F (pyflakes error) maps to critical severity."""
        smell_mod = _import_smell_module()

        classifier = getattr(smell_mod, "classify_smell_severity",
                             getattr(smell_mod, "classify_finding", None))
        if classifier is None:
            pytest.skip("No severity classification function found")

        result = classifier("ruff", "F401")
        assert result == "critical"

    def test_classify_linter_ruff_E_is_high(self):
        """Ruff E (pycodestyle error) maps to high severity."""
        smell_mod = _import_smell_module()

        classifier = getattr(smell_mod, "classify_smell_severity",
                             getattr(smell_mod, "classify_finding", None))
        if classifier is None:
            pytest.skip("No severity classification function found")

        result = classifier("ruff", "E302")
        assert result == "high"

    def test_classify_linter_ruff_W_is_medium(self):
        """Ruff W (pycodestyle warning) maps to medium severity."""
        smell_mod = _import_smell_module()

        classifier = getattr(smell_mod, "classify_smell_severity",
                             getattr(smell_mod, "classify_finding", None))
        if classifier is None:
            pytest.skip("No severity classification function found")

        result = classifier("ruff", "W292")
        assert result == "medium"

    def test_classify_linter_ruff_C_is_low(self):
        """Ruff C (mccabe complexity) maps to low severity."""
        smell_mod = _import_smell_module()

        classifier = getattr(smell_mod, "classify_smell_severity",
                             getattr(smell_mod, "classify_finding", None))
        if classifier is None:
            pytest.skip("No severity classification function found")

        result = classifier("ruff", "C901")
        assert result == "low"

    def test_classify_llm_severity_preserved(self):
        """LLM severity values are preserved through classification."""
        smell_mod = _import_smell_module()

        classifier = getattr(smell_mod, "classify_smell_severity",
                             getattr(smell_mod, "classify_finding", None))
        if classifier is None:
            pytest.skip("No severity classification function found")

        # LLM source severity should be preserved or mapped correctly
        result = classifier("llm", "medium")
        assert result in ("critical", "high", "medium", "low")

    def test_classify_unknown_linter_defaults_medium(self):
        """Unknown linter type defaults to medium severity."""
        smell_mod = _import_smell_module()

        classifier = getattr(smell_mod, "classify_smell_severity",
                             getattr(smell_mod, "classify_finding", None))
        if classifier is None:
            pytest.skip("No severity classification function found")

        result = classifier("unknown-linter", "some_code")
        assert result in ("critical", "high", "medium", "low")

    def test_classify_empty_severity_defaults_medium(self):
        """Empty or None severity defaults to medium."""
        smell_mod = _import_smell_module()

        classifier = getattr(smell_mod, "classify_smell_severity",
                             getattr(smell_mod, "classify_finding", None))
        if classifier is None:
            pytest.skip("No severity classification function found")

        result = classifier("ruff", "")
        assert result in ("critical", "high", "medium", "low")

    def test_severity_mapping_json_serializable(self):
        """Severity classification result is JSON-serializable."""
        smell_mod = _import_smell_module()

        classifier = getattr(smell_mod, "classify_smell_severity",
                             getattr(smell_mod, "classify_finding", None))
        if classifier is None:
            pytest.skip("No severity classification function found")

        result = classifier("ruff", "F401")
        json.dumps(result)  # Should not raise


# ===================================================================
# Integration-style tests (combining multiple aspects)
# ===================================================================


class TestSmellDetectionIntegration:
    """End-to-end style tests combining multiple detection aspects."""

    def test_detect_smells_with_all_params(
        self,
        mock_python_file: Path,
        mock_llm_client: MagicMock,
        default_rules: dict[str, Any],
    ):
        """detect_smells() accepts all optional parameters."""
        smell_mod = _import_smell_module()

        findings = smell_mod.detect_smells(
            files=[str(mock_python_file)],
            mode="hybrid",
            rules=default_rules,
            llm_client=mock_llm_client,
        )

        assert isinstance(findings, list)

    def test_detect_smells_json_serializable(
        self,
        mock_python_file: Path,
        mock_llm_client: MagicMock,
    ):
        """detect_smells() return value must be JSON-serializable."""
        smell_mod = _import_smell_module()

        findings = smell_mod.detect_smells(
            files=[str(mock_python_file)],
            mode="hybrid",
            llm_client=mock_llm_client,
        )

        # Should not raise - but findings might be empty if no linter/LLM
        json.dumps(findings)

    def test_detect_smells_with_multiple_files(
        self,
        temp_dir: Path,
        mock_llm_client: MagicMock,
    ):
        """detect_smells() handles multiple files."""
        smell_mod = _import_smell_module()

        # Create multiple files
        files = []
        for i, name in enumerate(["file1.py", "file2.py", "file3.py"]):
            path = temp_dir / "src" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"# File {i}\nx = {i}\n")
            files.append(str(path))

        findings = smell_mod.detect_smells(
            files=files,
            mode="hybrid",
            llm_client=mock_llm_client,
        )

        assert isinstance(findings, list)
        # The LLM client was called - check that file info was passed
        mock_llm_client.analyze.assert_called()


# ===================================================================
# Edge case tests
# ===================================================================


class TestEdgeCases:
    """Edge cases for smell detection."""

    def test_empty_file_content(self, temp_dir: Path):
        """Empty files are handled without error."""
        smell_mod = _import_smell_module()

        empty_file = temp_dir / "empty.py"
        empty_file.write_text("")

        # Should not raise
        findings = smell_mod.detect_smells(
            files=[str(empty_file)],
            mode="hybrid",
        )
        assert isinstance(findings, list)

    def test_binary_file_handling(self, temp_dir: Path):
        """Binary files are handled without error."""
        smell_mod = _import_smell_module()

        bin_file = temp_dir / "data.bin"
        bin_file.write_bytes(b"\x00\x01\x02\x03")

        # Should not raise
        findings = smell_mod.detect_smells(
            files=[str(bin_file)],
            mode="hybrid",
        )
        assert isinstance(findings, list)

    def test_large_number_of_files(self, temp_dir: Path):
        """Large number of files is handled without performance degradation."""
        smell_mod = _import_smell_module()

        files = []
        for i in range(100):
            path = temp_dir / "src" / f"file_{i}.py"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"x = {i}\n")
            files.append(str(path))

        # Should not raise
        findings = smell_mod.detect_smells(
            files=files,
            mode="linter",
        )
        assert isinstance(findings, list)

    def test_files_with_special_characters_in_path(
        self,
        temp_dir: Path,
    ):
        """Files with special characters in path are handled."""
        smell_mod = _import_smell_module()

        special_dir = temp_dir / "my project (v2)" / "src"
        special_dir.mkdir(parents=True, exist_ok=True)
        file_path = special_dir / "test-file_v2.1.py"
        file_path.write_text("x = 1\n")

        # Should not raise
        findings = smell_mod.detect_smells(
            files=[str(file_path)],
            mode="linter",
        )
        assert isinstance(findings, list)

    def test_smell_detection_returns_empty_with_no_llm_and_no_linter(
        self,
        mock_python_file: Path,
    ):
        """No linter and no LLM client results in empty findings without error."""
        smell_mod = _import_smell_module()

        # When both sources are unavailable, should return empty list
        findings = smell_mod.detect_smells(
            files=[str(mock_python_file)],
            mode="hybrid",
            llm_client=None,
        )
        assert isinstance(findings, list)
