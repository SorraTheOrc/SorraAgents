#!/usr/bin/env python3
"""Unit tests for skill/scripts/failure_notice.py."""


from skill.scripts.failure_notice import FailureNotice


class TestFailureNoticeConstruction:
    """Verify FailureNotice object construction and attributes."""

    def test_minimal_construction(self):
        """A FailureNotice can be created with just script_name and reason."""
        notice = FailureNotice(
            script_name="audit_runner.py",
            reason="Non-zero exit code: 1",
        )
        assert notice.script_name == "audit_runner.py"
        assert notice.reason == "Non-zero exit code: 1"
        assert notice.stderr_context is None

    def test_with_stderr_context(self):
        """stderr_context is stored when provided."""
        stderr = "Error: something went wrong"
        notice = FailureNotice(
            script_name="find_related.py",
            reason="Timeout after 900s",
            stderr_context=stderr,
        )
        assert notice.script_name == "find_related.py"
        assert notice.reason == "Timeout after 900s"
        assert notice.stderr_context == stderr


class TestFailureNoticeHeaderLine:
    """Verify the header line format."""

    def test_header_line_includes_script_and_reason(self):
        notice = FailureNotice("test.py", "Non-zero exit code: 1")
        assert "test.py" in notice.header_line
        assert "Non-zero exit code: 1" in notice.header_line
        assert "⚠" in notice.header_line
        assert notice.header_line.startswith("⚠ Script Execution Failure:")


class TestFailureNoticeFormatLines:
    """Verify format_lines output structure."""

    def test_basic_format(self):
        notice = FailureNotice("script.py", "Failed")
        lines = notice.format_lines()

        # First line should be separator
        assert lines[0] == "=" * 70
        # Second line should be header
        assert "script.py" in lines[1]
        assert "Failed" in lines[1]
        # Third line should be the manual output note
        assert "produced manually" in lines[2]
        # Fourth line should be separator
        assert lines[3] == "=" * 70

    def test_format_with_stderr(self):
        notice = FailureNotice("script.py", "Failed", stderr_context="traceback line 1\ntraceback line 2")
        lines = notice.format_lines()

        # Should include stderr section
        assert any("Captured stderr" in line for line in lines)
        assert any("traceback line 1" in line for line in lines)

    def test_format_without_stderr(self):
        notice = FailureNotice("script.py", "Non-zero exit code: 1")
        lines = notice.format_lines()

        # Should NOT include stderr section
        stderr_lines = [line for line in lines if "Captured stderr" in line]
        assert len(stderr_lines) == 0


class TestFailureNoticeWrap:
    """Verify wrap behavior for wrapping report output."""

    def test_wrap_adds_notice_as_first_and_last(self):
        notice = FailureNotice("test.py", "Non-zero exit code: 1")
        report = "## Summary\n\nAll acceptance criteria met."
        wrapped = notice.wrap(report)

        # Split into lines and find notice occurrences
        lines = wrapped.split("\n")
        separator_count = lines.count("=" * 70)

        # Separator should appear twice (start and end of each notice block)
        assert separator_count == 4, (
            f"Expected 4 separators (2 per notice block × 2 blocks), got {separator_count}"
        )

        # The report content should be preserved between the two notices
        assert "## Summary" in wrapped
        assert "All acceptance criteria met." in wrapped

    def test_wrap_none_report(self):
        """A None report produces just the notice with a '(no output produced)' note."""
        notice = FailureNotice("test.py", "Failed")
        wrapped = notice.wrap(None)

        assert "no output produced" in wrapped.lower()
        assert "Failed" in wrapped

    def test_wrap_empty_report(self):
        """An empty string report produces just the notice."""
        notice = FailureNotice("test.py", "Failed")
        wrapped = notice.wrap("")

        assert "no output produced" in wrapped.lower()

    def test_wrap_whitespace_only_report(self):
        """A whitespace-only report is treated as empty."""
        notice = FailureNotice("test.py", "Failed")
        wrapped = notice.wrap("   \n  \n   ")
        assert "no output produced" in wrapped.lower()

    def test_wrap_preserves_existing_content(self):
        """The original report content is preserved between the two notice blocks."""
        notice = FailureNotice("audit_runner.py", "Pi binary not found: /usr/bin/pi")
        report = (
            "Ready to close: Yes\n\n"
            "## Summary\n\n"
            "All criteria met.\n\n"
            "## Acceptance Criteria Status\n\n"
            "| # | Criterion | Verdict | Evidence |\n"
        )
        wrapped = notice.wrap(report)

        # Original content must be preserved
        assert "Ready to close: Yes" in wrapped
        assert "## Summary" in wrapped
        assert "All criteria met." in wrapped
        assert "## Acceptance Criteria Status" in wrapped

        # Notice must appear twice (first and last)
        first_separator = wrapped.find("=" * 70)
        last_separator = wrapped.rfind("=" * 70)
        assert first_separator != -1
        assert last_separator != -1
        assert first_separator != last_separator

        # The notice text should appear twice
        notice_count = wrapped.count("⚠ Script Execution Failure:")
        assert notice_count == 2, (
            f"Expected 2 occurrences of notice, got {notice_count}"
        )


class TestIntegrationFormat:
    """Integration-style tests simulating real failure scenarios."""

    def test_audit_runner_pi_not_found(self):
        """Simulate audit_runner.py failing because pi binary is missing."""
        notice = FailureNotice(
            script_name="audit_runner.py",
            reason="FileNotFoundError: pi binary not found",
            stderr_context="No such file or directory: 'pi'",
        )
        report = (
            "Phase 1 blocked: running Phase 2 deep code analysis...\n\n"
            "Ready to close: No\n\n"
            "## Summary\n\n"
            "Pi subprocess failed. No AC results available.\n\n"
        )
        wrapped = notice.wrap(report)

        # Verify notice structure
        assert wrapped.count("⚠ Script Execution Failure:") == 2
        assert wrapped.count("audit_runner.py") >= 2
        assert "pi binary not found" in wrapped
        # Original content preserved
        assert "Ready to close: No" in wrapped
        assert "## Summary" in wrapped
        # Stderr context included
        assert "No such file or directory" in wrapped

    def test_find_related_timeout(self):
        """Simulate find_related.py timing out."""
        notice = FailureNotice(
            script_name="find_related.py",
            reason="subprocess.TimeoutExpired after 30s",
        )
        report = "# Related Work Report\n\n## Results\n\nSearch completed partially."
        wrapped = notice.wrap(report)

        assert wrapped.count("⚠ Script Execution Failure:") == 2
        assert "find_related.py" in wrapped
        assert "TimeoutExpired" in wrapped
        assert "# Related Work Report" in wrapped
        assert "produced manually" in wrapped

    def test_implementall_exception(self):
        """Simulate implementall.py encountering a runtime exception."""
        notice = FailureNotice(
            script_name="implementall.py",
            reason="RuntimeError: Failed to invoke implement for item SA-123",
            stderr_context="Traceback (most recent call last):\n  ...\nKeyError: 'id'",
        )
        report = "# ImplementAll Summary\n\n**Total processed**: 3\n**Implemented**: 2\n**Errors**: 1"
        wrapped = notice.wrap(report)

        assert wrapped.count("⚠ Script Execution Failure:") == 2
        assert "implementall.py" in wrapped
        assert "RuntimeError" in wrapped
        assert "Failed to invoke implement" in wrapped
        assert "# ImplementAll Summary" in wrapped
        assert "stderr_context" not in wrapped  # Using attribute, not "stderr_context" literal
        assert "KeyError: 'id'" in wrapped
