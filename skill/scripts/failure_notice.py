#!/usr/bin/env python3
"""Shared utility for surfacing script execution failures in skill outputs.

Provides a standardized notice format that appears as both the first AND last
line of any skill output when its automated script fails.

Usage:
    from skill.scripts.failure_notice import FailureNotice

    notice = FailureNotice(
        script_name="audit_runner.py",
        reason="Non-zero exit code: 1",
        stderr_context="... captured stderr ...",
    )

    # Wrap an existing report with the failure notice
    wrapped = notice.wrap(report)

    # Or format just the notice lines
    lines = notice.format_lines()

The notice uses a prominent banner format:
    ════════════════════════════════════════════════════════
    ⚠ Script Execution Failure: <script_name> — <reason>
    The following output was produced manually.
    ════════════════════════════════════════════════════════

And is placed as the first and last lines of the output.
"""

from __future__ import annotations

from typing import Optional


DEFAULT_NOTICE_LINE = (
    "The following output was produced manually."
)


class FailureNotice:
    """A standardized failure notice for script execution failures.

    Attributes:
        script_name: Name of the script that failed.
        reason: Reason for the failure (e.g., ``"Non-zero exit code: 1"``,
            ``"Timeout after 900s"``, ``"File not found: /usr/bin/pi"``).
        stderr_context: Optional captured stderr text for additional context.
    """

    def __init__(
        self,
        script_name: str,
        reason: str,
        stderr_context: Optional[str] = None,
    ) -> None:
        self.script_name = script_name
        self.reason = reason
        self.stderr_context = stderr_context

    @property
    def header_line(self) -> str:
        """The main notice line for the failure."""
        return (
            f"⚠ Script Execution Failure: {self.script_name} — {self.reason}"
        )

    def format_lines(self) -> list[str]:
        """Format the failure notice as a list of lines.

        Returns a list of strings suitable for prepending/appending to a report.
        """
        separator = "=" * 70
        lines = [
            separator,
            self.header_line,
            DEFAULT_NOTICE_LINE,
            separator,
        ]
        if self.stderr_context:
            lines.append("")
            lines.append("Captured stderr:")
            lines.append("")
            lines.append(self.stderr_context.strip())
        return lines

    def format_notice_block(self) -> str:
        """Format the failure notice as a single block of text."""
        lines = self.format_lines()
        return "\n".join(lines)

    def wrap(self, report: Optional[str]) -> str:
        """Wrap *report* with the failure notice as first and last lines.

        If *report* is ``None`` or empty, just the notice block plus a
        "(no output produced)" note is returned.
        """
        notice = self.format_notice_block()

        if not report or not report.strip():
            return (
                f"{notice}\n\n"
                "(no output produced)\n\n"
                f"{notice}"
            )

        return f"{notice}\n\n{report.rstrip()}\n\n{notice}"
