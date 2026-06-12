"""Linter execution and severity classification for code quality automation.

Provides:
  - classify_finding(): Map raw linter severity to a normalised level.
  - run_ruff(): Execute ruff on a project and return structured findings.
  - run_eslint(): Execute eslint on a project and return structured findings.
  - run_linters_for_project(): Orchestrate detection + linting in one call.

Severity mapping
----------------
*Ruff rule-code prefix mapping:*
  - F (Pyflakes errors) → critical
  - E (pycodestyle errors), S (flake8-bandit/security) → high
  - W (pycodestyle warnings), D (pydocstyle), N (pep8-naming),
    UP (pyupgrade), ANN (flake8-annotations) → medium
  - C (mccabe complexity), default unknown → low
  - Any other unrecognised prefix → medium

*ESLint severity mapping:*
  - 2 / "error" → high
  - 1 / "warn" → medium
  - 0 / "off" → low
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Union

from .detection import detect_languages, get_linters_for_language, probe_linter

# ---------------------------------------------------------------------------
# Severity classification
# ---------------------------------------------------------------------------

# Ruff rule-code prefix → severity mapping
_RUFF_SEVERITY_MAP: dict[str, str] = {
    # Critical: Pyflakes errors (undefined names, unused imports, etc.)
    "F": "critical",
    # High: pycodestyle errors, security
    "E": "high",
    "S": "high",
    # Medium: pycodestyle warnings, docstring, naming, upgrade, annotations
    "W": "medium",
    "D": "medium",
    "N": "medium",
    "UP": "medium",
    "ANN": "medium",
    "B": "medium",       # flake8-bugbear
    "SIM": "medium",     # flake8-simplify
    "T20": "medium",     # flake8-print / flake8-debugger
    "PL": "medium",       # Pylint rules
    "RUF": "medium",      # Ruff-specific rules
    # Low: complexity, style
    "C": "low",
    "ISC": "low",         # implicit-string-concatenation
    "PIE": "low",         # flake8-pie
    "COM": "low",         # flake8-commas
}

_RUFF_DEFAULT_SEVERITY = "medium"


def _classify_ruff(code: str) -> str:
    """Classify a ruff rule code (e.g. ``F841``, ``E302``) to severity.

    The mapping uses the alphabetic prefix of the rule code.
    """
    # Extract alphabetic prefix
    prefix = ""
    for ch in code:
        if ch.isalpha():
            prefix += ch
        else:
            break

    # Try full prefix first (e.g. "ANN", "T20", "UP"), then single char
    if prefix in _RUFF_SEVERITY_MAP:
        return _RUFF_SEVERITY_MAP[prefix]
    if len(prefix) >= 1 and prefix[0] in _RUFF_SEVERITY_MAP:
        return _RUFF_SEVERITY_MAP[prefix[0]]

    return _RUFF_DEFAULT_SEVERITY


def _classify_eslint(severity: Any) -> str:
    """Classify an eslint message severity to a normalised level.

    Accepts numeric (0, 1, 2) and string ("off", "warn", "error") values,
    as well as string representations of numbers ("0", "1", "2").
    """
    # Normalise string representations
    if isinstance(severity, str):
        if severity.lower() in ("error",):
            return "high"
        if severity.lower() in ("warn", "warning"):
            return "medium"
        if severity.lower() == "off":
            return "low"
        # Try parsing as number
        try:
            severity = int(severity)
        except (ValueError, TypeError):
            return "low"

    if severity == 2:
        return "high"
    if severity == 1:
        return "medium"
    # 0 or anything else
    return "low"


def classify_finding(linter: str, raw_severity: Any) -> str:
    """Map a linter's raw severity value to a normalised severity level.

    Args:
        linter: The linter name (``"ruff"``, ``"eslint"``).
        raw_severity: The raw severity value from the linter's output.
                      For ruff this is a rule code like ``"F841"``.
                      For eslint this is a number (0,1,2) or label.

    Returns:
        One of ``"critical"``, ``"high"``, ``"medium"``, ``"low"``.
    """
    if linter == "ruff":
        return _classify_ruff(str(raw_severity) if raw_severity is not None else "")
    elif linter == "eslint":
        return _classify_eslint(raw_severity)
    # Unknown linter
    return "medium"


# ---------------------------------------------------------------------------
# Linter runners
# ---------------------------------------------------------------------------


def _run_subprocess(cmd: list[str], cwd: str | Path | None = None) -> subprocess.CompletedProcess:
    """Run a subprocess and return the result.

    Uses ``subprocess.run`` with text mode and captured output.
    Returns the CompletedProcess on any outcome (caller checks returncode).
    """
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=120,
        )
        return result
    except FileNotFoundError:
        # Linter binary not found
        return subprocess.CompletedProcess(
            args=cmd, returncode=-1,
            stdout="", stderr=f"Binary not found: {cmd[0]}",
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            args=cmd, returncode=-1,
            stdout="", stderr=f"Timed out: {' '.join(cmd)}",
        )


def _normalize_paths(root: Union[str, os.PathLike[str], None] = None) -> Path:
    """Normalise the project root to an absolute Path."""
    if root is None:
        return Path.cwd().resolve()
    return Path(root).resolve()


def run_ruff(
    project_root: Union[str, os.PathLike[str], None] = None,
    runner: Any = None,
) -> list[dict[str, Any]]:
    """Run ruff check on the given project root and return structured findings.

    Only runs if the linter is available on PATH and Python files are detected.

    Args:
        project_root: Path to the project root (default: cwd).
        runner: Optional injectable runner for testing (must be a callable
                accepting a list of strings and returning a
                ``subprocess.CompletedProcess``-like object).

    Returns:
        A list of finding dicts, each with keys:
          ``file``, ``line``, ``severity``, ``message``, ``linter``, ``code``.
        Returns an empty list if ruff is not available or no Python files exist.
    """
    # Check linter availability
    probe = probe_linter("ruff")
    if not probe["available"]:
        return []

    root = _normalize_paths(project_root)
    languages = detect_languages(root)

    if "python" not in languages:
        return []

    if runner is None:
        runner = _run_subprocess

    findings: list[dict[str, Any]] = []

    # Run ruff check on the whole project
    cmd = ["ruff", "check", str(root), "--output-format", "json", "--quiet"]
    result = runner(cmd)

    if result.returncode not in (0, 1):
        # Ruff exits 0 for no issues, 1 for issues found, other = error
        return []

    output = result.stdout.strip()
    if not output:
        return []

    try:
        raw = json.loads(output)
    except json.JSONDecodeError:
        return []

    if not isinstance(raw, list):
        return []

    for item in raw:
        if not isinstance(item, dict):
            continue

        code = str(item.get("code", ""))
        severity = classify_finding("ruff", code)
        loc = item.get("location", {}) or {}
        findings.append({
            "file": str(item.get("filename", "")),
            "line": loc.get("row", 0) if isinstance(loc, dict) else 0,
            "severity": severity,
            "message": item.get("message", ""),
            "linter": "ruff",
            "code": code,
        })

    return findings


def run_eslint(
    project_root: Union[str, os.PathLike[str], None] = None,
    runner: Any = None,
) -> list[dict[str, Any]]:
    """Run eslint on the given project root and return structured findings.

    Only runs if eslint is available on PATH and TypeScript files are detected.

    Args:
        project_root: Path to the project root (default: cwd).
        runner: Optional injectable runner for testing.

    Returns:
        A list of finding dicts (same format as :func:`run_ruff`).
        Returns an empty list if eslint is not available or no TS files exist.
    """
    # Check linter availability
    probe = probe_linter("eslint")
    if not probe["available"]:
        return []

    root = _normalize_paths(project_root)
    languages = detect_languages(root)

    if "typescript" not in languages:
        return []

    if runner is None:
        runner = _run_subprocess

    findings: list[dict[str, Any]] = []

    # Run eslint on the project
    # Use --no-eslintrc to avoid config issues, or just run with -f json
    cmd = ["eslint", str(root), "-f", "json", "--no-eslintrc", "--quiet"]
    result = runner(cmd)

    if result.returncode not in (0, 1):
        # eslint exits 0 for no issues, 1 for issues found, other = error
        return []

    output = result.stdout.strip()
    if not output:
        return []

    try:
        raw = json.loads(output)
    except json.JSONDecodeError:
        return []

    if not isinstance(raw, list):
        return []

    for file_result in raw:
        if not isinstance(file_result, dict):
            continue
        file_path = str(file_result.get("filePath", ""))
        messages = file_result.get("messages", [])
        if not isinstance(messages, list):
            continue

        for msg in messages:
            if not isinstance(msg, dict):
                continue
            severity_val = msg.get("severity", 1)
            severity = classify_finding("eslint", severity_val)
            findings.append({
                "file": file_path,
                "line": msg.get("line", 0),
                "severity": severity,
                "message": msg.get("message", ""),
                "linter": "eslint",
                "code": msg.get("ruleId", ""),
            })

    return findings


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run_linters_for_project(
    project_root: Union[str, os.PathLike[str], None] = None,
    runner: Any = None,
) -> dict[str, Any]:
    """Detect languages, probe linters, and run all available linters.

    Args:
        project_root: Path to the project root (default: cwd).
        runner: Optional injectable runner for testing.

    Returns:
        A dict with keys:
          - ``languages``: list of detected language names
          - ``linters``: list of probe results
          - ``total_findings``: total number of findings
          - ``findings_by_severity``: dict of severity → count
          - ``findings``: list of finding dicts
    """
    root = _normalize_paths(project_root)
    languages = detect_languages(root)

    # Collect linter probe results
    linters: list[dict[str, Any]] = []
    seen_linters: set[str] = set()
    for lang in languages:
        for linter_name in get_linters_for_language(lang):
            if linter_name not in seen_linters:
                seen_linters.add(linter_name)
                linters.append(probe_linter(linter_name))

    # Run all available linters
    all_findings: list[dict[str, Any]] = []

    for linter_info in linters:
        if not linter_info.get("available"):
            continue
        linter_name = linter_info["name"]
        if linter_name == "ruff":
            findings = run_ruff(root, runner=runner)
            all_findings.extend(findings)
        elif linter_name == "eslint":
            findings = run_eslint(root, runner=runner)
            all_findings.extend(findings)

    # Count by severity
    severity_counts: dict[str, int] = {"critical": 0, "high": 0, "medium": 0, "low": 0}
    for finding in all_findings:
        sev = finding.get("severity", "medium")
        if sev in severity_counts:
            severity_counts[sev] += 1

    return {
        "languages": languages,
        "linters": linters,
        "total_findings": len(all_findings),
        "findings_by_severity": severity_counts,
        "findings": all_findings,
    }
