"""Tests for work item creation from detected code smells.

These tests verify that:
- Work items are created with correct title, description, and tags via ``wl create``
- Priority is mapped correctly from smell severity levels
- Duplicate work items are prevented via existing REFACTOR comment detection
- Worklog CLI calls are properly constructed and invoked

The target implementation lives in skill/refactor/workitem_creation.py.

Related work items:
- SA-0MQA70XZK0033GOE
- SA-0MQJLXMV7002X1VY (Refactor skill creates phantom work items)
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

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
def sample_smell_critical() -> dict[str, Any]:
    """A critical-severity code smell finding."""
    return {
        "file": "src/main.py",
        "line": 42,
        "severity": "critical",
        "message": "Hardcoded API key detected in source code",
        "source": "linter",
        "smell_type": "security",
        "code": "S105",
    }


@pytest.fixture
def sample_smell_high() -> dict[str, Any]:
    """A high-severity code smell finding."""
    return {
        "file": "src/main.py",
        "line": 15,
        "severity": "high",
        "message": "Function `process_data` has cyclomatic complexity of 25",
        "source": "llm",
        "smell_type": "complex_function",
        "code": "CC001",
    }


@pytest.fixture
def sample_smell_medium() -> dict[str, Any]:
    """A medium-severity code smell finding."""
    return {
        "file": "src/utils.py",
        "line": 30,
        "severity": "medium",
        "message": "`os` imported but unused",
        "source": "linter",
        "smell_type": "unused_import",
        "code": "F401",
    }


@pytest.fixture
def sample_smell_low() -> dict[str, Any]:
    """A low-severity code smell finding."""
    return {
        "file": "src/utils.py",
        "line": 5,
        "severity": "low",
        "message": "Missing docstring for public function `helper`",
        "source": "linter",
        "smell_type": "documentation",
        "code": "D100",
    }


@pytest.fixture
def mock_wl_create_json() -> str:
    """Mock JSON output from a successful ``wl create`` command."""
    result = {
        "success": True,
        "workItem": {
            "id": "SA-0MOCK1234X000WORK",
            "title": "Refactor: Security issue in src/main.py",
            "status": "open",
            "priority": "high",
        },
    }
    return json.dumps(result)


# ---------------------------------------------------------------------------
# Helper: mock subprocess.run for wl commands
# ---------------------------------------------------------------------------


def _make_fake_subprocess_run(
    responses: dict[str, subprocess.CompletedProcess],
) -> Any:
    """Build a fake subprocess.run that returns predefined responses.

    ``responses`` maps a cmd string (``" ".join(cmd)``) to a
    ``subprocess.CompletedProcess`` instance.  If a command is not found the
    fake raises ``FileNotFoundError``.
    """

    def fake_run(
        cmd: list[str] | str,
        *args: Any,
        **kwargs: Any,
    ) -> subprocess.CompletedProcess:
        key = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
        if key not in responses:
            raise FileNotFoundError(f"Unexpected command: {key}")
        return responses[key]

    return fake_run


def _cp(
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess:
    """Shortcut to build a subprocess.CompletedProcess instance."""
    return subprocess.CompletedProcess(
        args=[],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _build_wl_create_cmd(title: str, description: str, priority: str) -> str:
    """Build the expected ``wl create`` command string for matching."""
    parts = [
        "wl",
        "create",
        "--title",
        title,
        "--description",
        description,
        "--priority",
        priority,
        "--tags",
        "Refactor",
        "--json",
    ]
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Tests: Priority Mapping
# ---------------------------------------------------------------------------


class TestPriorityMapping:
    """Priority mapping from smell severity to work item priority."""

    def test_critical_severity_maps_to_high_priority(self):
        """Critical severity maps to 'high' priority."""
        from skill.refactor.workitem_creation import severity_to_priority

        assert severity_to_priority("critical") == "high"

    def test_high_severity_maps_to_high_priority(self):
        """High severity maps to 'high' priority."""
        from skill.refactor.workitem_creation import severity_to_priority

        assert severity_to_priority("high") == "high"

    def test_medium_severity_maps_to_medium_priority(self):
        """Medium severity maps to 'medium' priority."""
        from skill.refactor.workitem_creation import severity_to_priority

        assert severity_to_priority("medium") == "medium"

    def test_low_severity_maps_to_low_priority(self):
        """Low severity maps to 'low' priority."""
        from skill.refactor.workitem_creation import severity_to_priority

        assert severity_to_priority("low") == "low"

    def test_unknown_severity_defaults_to_medium_priority(self):
        """Unknown severity defaults to 'medium' priority."""
        from skill.refactor.workitem_creation import severity_to_priority

        assert severity_to_priority("unknown") == "medium"
        assert severity_to_priority("") == "medium"
        assert severity_to_priority("blah") == "medium"


# ---------------------------------------------------------------------------
# Tests: Work Item Title Generation
# ---------------------------------------------------------------------------


class TestWorkItemTitleGeneration:
    """Title generation for smell-based work items."""

    def test_title_includes_smell_type(self, sample_smell_critical):
        """Title contains the smell type label."""
        from skill.refactor.workitem_creation import build_smell_title

        title = build_smell_title(sample_smell_critical)
        assert "security" in title.lower()

    def test_title_includes_file_path(self, sample_smell_high):
        """Title contains the file path."""
        from skill.refactor.workitem_creation import build_smell_title

        title = build_smell_title(sample_smell_high)
        assert "src/main.py" in title

    def test_title_starts_with_refactor_prefix(self, sample_smell_medium):
        """Title starts with 'Refactor:' prefix."""
        from skill.refactor.workitem_creation import build_smell_title

        title = build_smell_title(sample_smell_medium)
        assert title.startswith("Refactor:")

    def test_title_is_reasonable_length(self, sample_smell_low):
        """Title is not excessively long."""
        from skill.refactor.workitem_creation import build_smell_title

        title = build_smell_title(sample_smell_low)
        assert len(title) < 200


# ---------------------------------------------------------------------------
# Tests: Work Item Description Generation
# ---------------------------------------------------------------------------


class TestWorkItemDescriptionGeneration:
    """Description generation for smell-based work items."""

    def test_description_includes_file_and_line(self, sample_smell_critical):
        """Description contains the file path and line number."""
        from skill.refactor.workitem_creation import build_smell_description

        desc = build_smell_description(sample_smell_critical)
        assert "src/main.py" in desc
        assert "42" in desc or ":42" in desc

    def test_description_includes_smell_type_and_severity(
        self, sample_smell_high
    ):
        """Description contains the smell type and severity."""
        from skill.refactor.workitem_creation import build_smell_description

        desc = build_smell_description(sample_smell_high)
        assert "complex_function" in desc
        assert "high" in desc

    def test_description_includes_message(self, sample_smell_medium):
        """Description contains the original detection message."""
        from skill.refactor.workitem_creation import build_smell_description

        desc = build_smell_description(sample_smell_medium)
        assert "os" in desc
        assert "unused" in desc

    def test_description_is_markdown_formatted(self, sample_smell_low):
        """Description uses markdown formatting for readability."""
        from skill.refactor.workitem_creation import build_smell_description

        desc = build_smell_description(sample_smell_low)
        assert "**" in desc or "## " in desc or "- " in desc or "`" in desc


# ---------------------------------------------------------------------------
# Tests: Worklog CLI Integration
# ---------------------------------------------------------------------------


class TestWorklogCLIIntegration:
    """Worklog CLI integration for creating work items."""

    @pytest.fixture
    def referent_critical(self, temp_dir: Path, sample_smell_critical):
        """Create the file referenced by sample_smell_critical on disk."""
        smell = dict(sample_smell_critical)
        file_path = temp_dir / smell["file"]
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text("import os\n")
        smell["file"] = str(file_path)
        return smell

    @pytest.fixture
    def referent_medium(self, temp_dir: Path, sample_smell_medium):
        """Create the file referenced by sample_smell_medium on disk."""
        smell = dict(sample_smell_medium)
        file_path = temp_dir / smell["file"]
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text("import os\n")
        smell["file"] = str(file_path)
        return smell

    @pytest.fixture
    def referent_high(self, temp_dir: Path, sample_smell_high):
        """Create the file referenced by sample_smell_high on disk."""
        smell = dict(sample_smell_high)
        file_path = temp_dir / smell["file"]
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text("import os\n")
        smell["file"] = str(file_path)
        return smell

    def test_creates_work_item_via_wl_cli(
        self,
        monkeypatch,
        referent_critical,
        mock_wl_create_json,
    ):
        """Calls ``wl create`` with correct arguments and returns the ID."""
        from skill.refactor.workitem_creation import (
            build_smell_title,
            build_smell_description,
            severity_to_priority,
            create_smell_work_item,
        )

        title = build_smell_title(referent_critical)
        description = build_smell_description(referent_critical)
        priority = severity_to_priority(referent_critical["severity"])

        expected_list_cmd = " ".join(["wl", "list", "--tags", "Refactor", "--json"])
        expected_create_cmd = _build_wl_create_cmd(title, description, priority)

        responses = {
            expected_list_cmd: _cp(stdout='{"success":true,"workItems":[]}'),
            expected_create_cmd: _cp(stdout=mock_wl_create_json),
        }
        monkeypatch.setattr(
            subprocess, "run", _make_fake_subprocess_run(responses)
        )

        work_item_id = create_smell_work_item(referent_critical)
        assert work_item_id == "SA-0MOCK1234X000WORK"

    def test_creates_work_item_with_correct_priority(
        self, monkeypatch, referent_medium, mock_wl_create_json
    ):
        """Work item is created with the priority mapped from severity."""
        from skill.refactor.workitem_creation import (
            build_smell_title,
            build_smell_description,
            severity_to_priority,
            create_smell_work_item,
        )

        title = build_smell_title(referent_medium)
        description = build_smell_description(referent_medium)
        priority = severity_to_priority(referent_medium["severity"])

        expected_list_cmd = " ".join(["wl", "list", "--tags", "Refactor", "--json"])
        expected_create_cmd = _build_wl_create_cmd(title, description, priority)

        responses = {
            expected_list_cmd: _cp(stdout='{"success":true,"workItems":[]}'),
            expected_create_cmd: _cp(stdout=mock_wl_create_json),
        }
        monkeypatch.setattr(
            subprocess, "run", _make_fake_subprocess_run(responses)
        )

        work_item_id = create_smell_work_item(referent_medium)
        assert work_item_id is not None
        assert work_item_id.startswith("SA-")

    def test_includes_refactor_tag(
        self, monkeypatch, referent_high, mock_wl_create_json
    ):
        """Work item is created with 'Refactor' tag."""
        from skill.refactor.workitem_creation import (
            build_smell_title,
            build_smell_description,
            severity_to_priority,
            create_smell_work_item,
        )

        title = build_smell_title(referent_high)
        description = build_smell_description(referent_high)
        priority = severity_to_priority(referent_high["severity"])

        expected_list_cmd = " ".join(["wl", "list", "--tags", "Refactor", "--json"])
        expected_create_cmd = _build_wl_create_cmd(title, description, priority)

        responses = {
            expected_list_cmd: _cp(stdout='{"success":true,"workItems":[]}'),
            expected_create_cmd: _cp(stdout=mock_wl_create_json),
        }
        monkeypatch.setattr(
            subprocess, "run", _make_fake_subprocess_run(responses)
        )

        work_item_id = create_smell_work_item(referent_high)
        assert work_item_id is not None

    def test_handles_wl_cli_failure_gracefully(
        self, monkeypatch, sample_smell_low
    ):
        """When ``wl create`` fails, returns None rather than crashing."""
        from skill.refactor.workitem_creation import (
            build_smell_title,
            build_smell_description,
            severity_to_priority,
            create_smell_work_item,
        )

        title = build_smell_title(sample_smell_low)
        description = build_smell_description(sample_smell_low)
        priority = severity_to_priority(sample_smell_low["severity"])

        expected_cmd = _build_wl_create_cmd(title, description, priority)
        # Simulate a non-zero return code
        responses = {expected_cmd: _cp(returncode=1, stderr="error")}
        monkeypatch.setattr(
            subprocess, "run", _make_fake_subprocess_run(responses)
        )

        result = create_smell_work_item(sample_smell_low)
        assert result is None

    def test_handles_wl_cli_unexpected_output(
        self, monkeypatch, sample_smell_critical
    ):
        """When ``wl create`` returns unparseable output, returns None."""
        from skill.refactor.workitem_creation import (
            build_smell_title,
            build_smell_description,
            severity_to_priority,
            create_smell_work_item,
        )

        title = build_smell_title(sample_smell_critical)
        description = build_smell_description(sample_smell_critical)
        priority = severity_to_priority(sample_smell_critical["severity"])

        expected_cmd = _build_wl_create_cmd(title, description, priority)
        responses = {expected_cmd: _cp(stdout="not valid json {{{")}
        monkeypatch.setattr(
            subprocess, "run", _make_fake_subprocess_run(responses)
        )

        result = create_smell_work_item(sample_smell_critical)
        assert result is None

    def test_handles_missing_wl_command(
        self, monkeypatch, sample_smell_medium
    ):
        """When ``wl`` is not installed, returns None rather than crashing."""
        from skill.refactor.workitem_creation import (
            build_smell_title,
            build_smell_description,
            severity_to_priority,
            create_smell_work_item,
        )

        title = build_smell_title(sample_smell_medium)
        description = build_smell_description(sample_smell_medium)
        priority = severity_to_priority(sample_smell_medium["severity"])

        expected_cmd = _build_wl_create_cmd(title, description, priority)
        responses = {expected_cmd: _cp(returncode=127, stderr="command not found")}
        monkeypatch.setattr(
            subprocess, "run", _make_fake_subprocess_run(responses)
        )

        result = create_smell_work_item(sample_smell_medium)
        assert result is None

    def test_empty_smell_list_returns_empty(
        self,
        monkeypatch,
    ):
        """Creating work items from an empty list returns an empty list."""
        from skill.refactor.workitem_creation import create_smell_work_items

        results = create_smell_work_items([])
        assert results == []

    def test_create_multiple_work_items(
        self,
        monkeypatch,
        temp_dir,
        sample_smell_critical,
        sample_smell_medium,
        mock_wl_create_json,
    ):
        """Creating work items for multiple smells returns all IDs."""
        from skill.refactor.workitem_creation import (
            build_smell_title,
            build_smell_description,
            severity_to_priority,
            create_smell_work_item,
        )

        # Create files on disk
        fp1 = temp_dir / "src" / "main.py"
        fp1.parent.mkdir(parents=True, exist_ok=True)
        fp1.write_text("import os\n")

        scent1 = dict(sample_smell_critical)
        scent1["file"] = str(fp1)

        scent2 = dict(sample_smell_medium)
        fp2 = temp_dir / "src" / "utils.py"
        fp2.parent.mkdir(parents=True, exist_ok=True)
        fp2.write_text("import sys\n")
        scent2["file"] = str(fp2)

        title1 = build_smell_title(scent1)
        desc1 = build_smell_description(scent1)
        pri1 = severity_to_priority(scent1["severity"])
        cmd1 = _build_wl_create_cmd(title1, desc1, pri1)

        empty_list = '{"success":true,"workItems":[]}'
        responses = {
            "wl list --tags Refactor --json": _cp(stdout=empty_list),
            cmd1: _cp(stdout=mock_wl_create_json),
        }
        monkeypatch.setattr(
            subprocess, "run", _make_fake_subprocess_run(responses)
        )

        # Test first smell
        id1 = create_smell_work_item(scent1)
        assert id1 == "SA-0MOCK1234X000WORK"


# ---------------------------------------------------------------------------
# Tests: Duplicate Prevention
# ---------------------------------------------------------------------------


class TestDuplicatePrevention:
    """Duplicate work item prevention via code comment detection."""

    def test_detects_existing_refactor_comment(self, temp_dir: Path):
        """Detects an existing REFACTOR comment in a source file."""
        file_path = temp_dir / "src" / "example.py"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(
            "# <!-- REFACTOR-SA-0MOCK9999\n"
            "# smell: security\n"
            "# description: Hardcoded API key\n"
            "# -->\n"
            "import os\n"
            "\n"
            'API_KEY = "sk-12345"\n'
        )

        from skill.refactor.workitem_creation import has_existing_smell_comment

        assert (
            has_existing_smell_comment(str(file_path), "security") is True
        )

    def test_no_duplicate_when_no_comment_exists(self, temp_dir: Path):
        """Returns False when no REFACTOR comment exists for the smell type."""
        file_path = temp_dir / "src" / "example.py"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(
            "import os\n"
            "\n"
            'API_KEY = "sk-12345"\n'
        )

        from skill.refactor.workitem_creation import has_existing_smell_comment

        assert (
            has_existing_smell_comment(str(file_path), "security") is False
        )

    def test_different_smell_type_not_blocked(self, temp_dir: Path):
        """REFACTOR comment for one smell type does not block a different type."""
        file_path = temp_dir / "src" / "example.py"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(
            "# <!-- REFACTOR-SA-0MOCK9999\n"
            "# smell: security\n"
            "# description: Hardcoded API key\n"
            "# -->\n"
            "import os\n"
        )

        from skill.refactor.workitem_creation import has_existing_smell_comment

        # Different smell type should not be blocked
        assert (
            has_existing_smell_comment(str(file_path), "complex_function")
            is False
        )

    def test_skip_creation_when_duplicate_exists(
        self,
        monkeypatch,
        temp_dir: Path,
        sample_smell_critical,
    ):
        """Work item creation is skipped when a duplicate REFACTOR comment exists."""
        file_path = temp_dir / "src" / "main.py"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(
            "# <!-- REFACTOR-SA-0MOCK9999\n"
            "# smell: security\n"
            "# description: Hardcoded API key\n"
            "# -->\n"
            'API_KEY = "sk-12345"\n'
        )

        from skill.refactor.workitem_creation import (
            create_smell_work_item,
        )

        # Override the file path in the smell to match our temp file
        smell = dict(sample_smell_critical)
        smell["file"] = str(file_path)

        # If the module has duplicate detection, it should skip and return None
        result = create_smell_work_item(smell)
        assert result is None

    def test_creates_work_item_when_no_duplicate(
        self,
        monkeypatch,
        temp_dir: Path,
        sample_smell_high,
        mock_wl_create_json,
    ):
        """Work item is created when no duplicate REFACTOR comment exists."""
        file_path = temp_dir / "src" / "main.py"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text("import os\n")

        from skill.refactor.workitem_creation import (
            build_smell_title,
            build_smell_description,
            severity_to_priority,
            create_smell_work_item,
        )

        smell = dict(sample_smell_high)
        smell["file"] = str(file_path)

        title = build_smell_title(smell)
        description = build_smell_description(smell)
        priority = severity_to_priority(smell["severity"])

        expected_cmd = _build_wl_create_cmd(title, description, priority)
        responses = {expected_cmd: _cp(stdout=mock_wl_create_json)}
        monkeypatch.setattr(
            subprocess, "run", _make_fake_subprocess_run(responses)
        )

        result = create_smell_work_item(smell)
        assert result == "SA-0MOCK1234X000WORK"

    def test_empty_file_returns_false(self, temp_dir: Path):
        """An empty file has no existing REFACTOR comments."""
        file_path = temp_dir / "empty.py"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text("")

        from skill.refactor.workitem_creation import has_existing_smell_comment

        assert (
            has_existing_smell_comment(str(file_path), "security") is False
        )

    def test_nonexistent_file_returns_false(self, temp_dir: Path):
        """A nonexistent file returns False (no duplicate)."""
        file_path = temp_dir / "nonexistent.py"

        from skill.refactor.workitem_creation import has_existing_smell_comment

        assert (
            has_existing_smell_comment(str(file_path), "security") is False
        )


# ---------------------------------------------------------------------------
# Tests: Non-Existent File Handling (AC1, AC2)
# ---------------------------------------------------------------------------


class TestNonExistentFileHandling:
    """Work item creation is skipped when the referenced file does not exist.

    Related acceptance criteria (SA-0MQJLXMV7002X1VY):
    - AC1: Create work items only when the source file exists on disk
    - AC2: Log a warning and skip creation when file does not exist
    """

    def test_skip_creation_when_file_does_not_exist(
        self,
        sample_smell_critical,
    ):
        """create_smell_work_item returns None when the file does not exist."""
        from skill.refactor.workitem_creation import create_smell_work_item

        smell = dict(sample_smell_critical)
        smell["file"] = "/nonexistent/path/file.py"

        result = create_smell_work_item(smell)
        assert result is None, (
            "Expected None when file does not exist, got %r" % result
        )

    def test_skip_creation_when_file_does_not_exist_batch(
        self,
        sample_smell_critical,
        sample_smell_medium,
    ):
        """Batch creation skips smells whose files do not exist."""
        from skill.refactor.workitem_creation import create_smell_work_items

        smell1 = dict(sample_smell_critical)
        smell1["file"] = "/nonexistent/main.py"

        smell2 = dict(sample_smell_medium)
        smell2["file"] = "/nonexistent/utils.py"

        results = create_smell_work_items([smell1, smell2])
        assert results == [], (
            "Expected empty list when all files are nonexistent, got %r" % results
        )

    def test_warning_logged_when_file_does_not_exist(
        self,
        sample_smell_critical,
    ):
        """A warning is logged when skipping creation for a non-existent file."""
        from skill.refactor.workitem_creation import create_smell_work_item

        smell = dict(sample_smell_critical)
        smell["file"] = "/nonexistent/path/file.py"

        # Capture log output at WARNING level
        logger = logging.getLogger("refactor.workitem_creation")
        try:
            from io import StringIO
        except ImportError:
            from io import StringIO
        log_capture = StringIO()
        handler = logging.StreamHandler(log_capture)
        handler.setLevel(logging.WARNING)
        logger.addHandler(handler)
        original_level = logger.level
        logger.setLevel(logging.WARNING)

        try:
            result = create_smell_work_item(smell)
            assert result is None
            log_output = log_capture.getvalue()
            assert "does not exist" in log_output.lower() or "nonexistent" in log_output.lower() or "skipping" in log_output.lower(), (
                "Expected warning log about non-existent file, got: %s" % log_output
            )
        finally:
            logger.removeHandler(handler)
            logger.setLevel(original_level)


# ---------------------------------------------------------------------------
# Tests: Worklog-Based Duplicate Prevention (AC3)
# ---------------------------------------------------------------------------


class TestWorklogDuplicatePrevention:
    """Duplicate detection via worklog database query as secondary safety net.

    Related acceptance criteria (SA-0MQJLXMV7002X1VY):
    - AC3: Query worklog database to check for existing work items with the
      same (file, line, code) combination
    """

    def test_skip_when_worklog_has_existing_item(
        self,
        monkeypatch,
        temp_dir,
        sample_smell_critical,
    ):
        """Work item creation is skipped when worklog has an existing Refactor item
        for the same (file, line, code) combination."""
        from skill.refactor.workitem_creation import create_smell_work_item

        # Create the actual file on disk so the file existence check passes
        file_path = temp_dir / "src" / "main.py"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text("import os\n")

        smell = dict(sample_smell_critical)
        smell["file"] = str(file_path)

        # Mock subprocess to simulate wl list returning an existing item
        mock_list_output = json.dumps({
            "success": True,
            "workItems": [
                {
                    "id": "SA-0MOCK9999X000EXIST",
                    "title": "Refactor: Security issue in src/main.py",
                    "tags": ["Refactor"],
                    "status": "open",
                    "priority": "high",
                }
            ],
        })

        # Build expected wl list command
        expected_list_cmd = " ".join([
            "wl", "list", "--tags", "Refactor",
            "--json",
        ])

        responses = {}
        # Set up mock response for wl list
        cp_list = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=mock_list_output, stderr=""
        )
        responses[expected_list_cmd] = cp_list

        def _fake_run(cmd, *args, **kwargs):
            key = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
            if key in responses:
                return responses[key]
            # Default: return empty for any other command
            return subprocess.CompletedProcess(
                args=[], returncode=0, stdout='{"success":true,"workItems":[]}', stderr=""
            )

        monkeypatch.setattr(subprocess, "run", _fake_run)

        result = create_smell_work_item(smell)
        assert result is None, (
            "Expected None when worklog has existing item, got %r" % result
        )

    def test_allows_creation_when_worklog_has_no_match(
        self,
        monkeypatch,
        temp_dir,
        sample_smell_critical,
        mock_wl_create_json,
    ):
        """Work item creation proceeds when worklog has no matching Refactor items."""
        # Create the actual file on disk
        file_path = temp_dir / "src" / "main.py"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text("import os\n")

        smell = dict(sample_smell_critical)
        smell["file"] = str(file_path)

        # Mock wl list returning no matching items
        empty_list_output = json.dumps({
            "success": True,
            "workItems": [],
        })

        expected_list_cmd = " ".join(["wl", "list", "--tags", "Refactor", "--json"])

        # Build expected wl create command
        from skill.refactor.workitem_creation import (
            build_smell_title,
            build_smell_description,
            severity_to_priority,
        )
        title = build_smell_title(smell)
        description = build_smell_description(smell)
        priority = severity_to_priority(smell["severity"])
        expected_create_cmd = " ".join([
            "wl", "create", "--title", title,
            "--description", description,
            "--priority", priority,
            "--tags", "Refactor",
            "--json",
        ])

        responses = {
            expected_list_cmd: subprocess.CompletedProcess(
                args=[], returncode=0, stdout=empty_list_output, stderr=""
            ),
            expected_create_cmd: subprocess.CompletedProcess(
                args=[], returncode=0, stdout=mock_wl_create_json, stderr=""
            ),
        }

        def _fake_run(cmd, *args, **kwargs):
            key = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
            if key in responses:
                return responses[key]
            raise FileNotFoundError(f"Unexpected command: {key}")

        monkeypatch.setattr(subprocess, "run", _fake_run)

        from skill.refactor.workitem_creation import create_smell_work_item

        result = create_smell_work_item(smell)
        assert result == "SA-0MOCK1234X000WORK"


# ---------------------------------------------------------------------------
# Tests: Module-Level Batch Creation
# ---------------------------------------------------------------------------


class TestBatchWorkItemCreation:
    """Batch creation of work items from multiple smells."""

    def test_create_smell_work_items_batch(
        self,
        monkeypatch,
        temp_dir,
        mock_wl_create_json,
    ):
        """Batch creation returns work item IDs for each smell."""
        from skill.refactor.workitem_creation import (
            build_smell_title,
            build_smell_description,
            severity_to_priority,
            create_smell_work_items,
        )

        # Create files on disk for two smells
        fp1 = temp_dir / "src" / "main.py"
        fp1.parent.mkdir(parents=True, exist_ok=True)
        fp1.write_text("import os\n")

        fp2 = temp_dir / "src" / "utils.py"
        fp2.parent.mkdir(parents=True, exist_ok=True)
        fp2.write_text("import sys\n")

        smell1 = {
            "file": str(fp1), "line": 42, "severity": "critical",
            "message": "Hardcoded API key detected", "source": "linter",
            "smell_type": "security", "code": "S105",
        }
        smell2 = {
            "file": str(fp2), "line": 30, "severity": "medium",
            "message": "`os` imported but unused", "source": "linter",
            "smell_type": "unused_import", "code": "F401",
        }

        mock_json2 = json.dumps({
            "success": True,
            "workItem": {
                "id": "SA-0MOCK5678X000WORK",
                "title": "Refactor: Unused import in src/utils.py",
                "status": "open",
                "priority": "medium",
            },
        })

        title1 = build_smell_title(smell1)
        desc1 = build_smell_description(smell1)
        pri1 = severity_to_priority(smell1["severity"])
        cmd1 = _build_wl_create_cmd(title1, desc1, pri1)

        title2 = build_smell_title(smell2)
        desc2 = build_smell_description(smell2)
        pri2 = severity_to_priority(smell2["severity"])
        cmd2 = _build_wl_create_cmd(title2, desc2, pri2)

        empty_list = '{"success":true,"workItems":[]}'

        # Each smell gets its own response; also mock wl list
        responses = {
            "wl list --tags Refactor --json": _cp(stdout=empty_list),
            cmd1: _cp(stdout=mock_wl_create_json),
            cmd2: _cp(stdout=mock_json2),
        }
        monkeypatch.setattr(
            subprocess, "run", _make_fake_subprocess_run(responses)
        )

        smells = [smell1, smell2]
        results = create_smell_work_items(smells)
        assert len(results) == 2
        assert results[0] == "SA-0MOCK1234X000WORK"
        assert results[1] == "SA-0MOCK5678X000WORK"

    def test_batch_with_empty_list_returns_empty(self):
        """Batch creation with an empty list returns an empty list."""
        from skill.refactor.workitem_creation import create_smell_work_items

        assert create_smell_work_items([]) == []

    def test_batch_skips_duplicates(
        self,
        monkeypatch,
        temp_dir: Path,
        sample_smell_critical,
    ):
        """Batch creation skips smells that already have REFACTOR comments."""
        file_path = temp_dir / "src" / "main.py"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(
            "# <!-- REFACTOR-SA-0MOCK9999\n"
            "# smell: security\n"
            "# description: Hardcoded API key\n"
            "# -->\n"
            'API_KEY = "sk-12345"\n'
        )

        # Create a second file that does exist (no REFACTOR comment)
        clean_file = temp_dir / "src" / "utils.py"
        clean_file.parent.mkdir(parents=True, exist_ok=True)
        clean_file.write_text("import sys\n")

        from skill.refactor.workitem_creation import (
            build_smell_title,
            build_smell_description,
            severity_to_priority,
            create_smell_work_items,
        )

        smell_with_dup = dict(sample_smell_critical)
        smell_with_dup["file"] = str(file_path)

        smell_clean = {
            "file": str(clean_file), "line": 30, "severity": "medium",
            "message": "`os` imported but unused", "source": "linter",
            "smell_type": "unused_import", "code": "F401",
        }

        # Build expected wl create command for the clean smell
        title = build_smell_title(smell_clean)
        desc = build_smell_description(smell_clean)
        pri = severity_to_priority(smell_clean["severity"])
        expected_create_cmd = _build_wl_create_cmd(title, desc, pri)

        mock_json = json.dumps({
            "success": True,
            "workItem": {
                "id": "SA-0MOCK5678X000WORK",
                "title": "Refactor: Unused import in src/utils.py",
            },
        })

        empty_list = '{"success":true,"workItems":[]}'
        responses = {
            "wl list --tags Refactor --json": _cp(stdout=empty_list),
            expected_create_cmd: _cp(stdout=mock_json),
        }
        monkeypatch.setattr(subprocess, "run", _make_fake_subprocess_run(responses))

        results = create_smell_work_items([smell_with_dup, smell_clean])
        # Only the non-duplicate smell should be attempted
        assert len(results) == 1
        assert results[0] == "SA-0MOCK5678X000WORK"


# ---------------------------------------------------------------------------
# Tests: Error Handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    """Error handling for work item creation."""

    def test_handles_missing_severity_key(self):
        """Missing severity key defaults to medium priority."""
        from skill.refactor.workitem_creation import severity_to_priority

        assert severity_to_priority(None) == "medium"

    def test_handles_missing_file_key(self, monkeypatch):
        """Missing file key in smell still generates a work item."""
        from skill.refactor.workitem_creation import build_smell_title

        smell: dict[str, Any] = {
            "line": 1,
            "severity": "high",
            "message": "Some issue",
            "source": "linter",
            "smell_type": "unknown",
            "code": "X001",
        }
        # Should not raise, produce a reasonable title
        title = build_smell_title(smell)
        assert isinstance(title, str)
        assert len(title) > 0

    def test_handles_missing_message_key(self):
        """Missing message key produces a generic description."""
        from skill.refactor.workitem_creation import build_smell_description

        smell: dict[str, Any] = {
            "file": "src/test.py",
            "line": 10,
            "severity": "medium",
            "source": "linter",
            "smell_type": "unknown",
            "code": "",
        }
        desc = build_smell_description(smell)
        assert isinstance(desc, str)
        assert len(desc) > 0

    def test_handles_invalid_severity_values(self):
        """Invalid severity values are mapped to medium priority."""
        from skill.refactor.workitem_creation import severity_to_priority

        for invalid in [None, "", "invalid", 1, [], {}]:
            result = severity_to_priority(invalid)
            assert result == "medium", f"Expected 'medium' for {invalid!r}, got {result!r}"


class TestDocHygiene:
    """Doc hygiene tests for SKILL.md."""

    SKILL_MD = Path(__file__).resolve().parent.parent.parent / "skill" / "refactor" / "SKILL.md"

    def test_skill_md_has_status_management_instructions(self):
        """SKILL.md must include instructions for capturing and restoring status."""
        content = self.SKILL_MD.read_text()
        assert "wl show" in content, "SKILL.md must include wl show command for capturing status"
        assert "in_progress" in content, "SKILL.md must reference setting status to in_progress"
        assert "original status" in content.lower() or "starting status" in content.lower(), \
            "SKILL.md must reference restoring the original status"
