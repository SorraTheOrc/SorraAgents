"""Linter execution and severity classification for code quality automation.

Provides:
  - classify_finding(): Map raw linter severity to a normalised level.
  - run_ruff(): Execute ruff on a project and return structured findings.
  - run_eslint(): Execute eslint on a project and return structured findings.
  - run_markdownlint(): Execute markdownlint on a project and return structured findings.
  - run_shellcheck(): Execute shellcheck on a project and return structured findings.
  - run_dotnet_format(): Execute dotnet format on a project and return structured findings.
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

*Markdownlint severity mapping:*
  - error → high
  - warning → medium
  - default → medium

*Shellcheck severity mapping:*
  - error → high
  - warning → medium
  - default → medium

*dotnet-format severity mapping:*
  - All findings → medium (formatting issues)
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Callable, Union

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


def _run_eslint_findings(
    result: Any,
) -> list[dict[str, Any]]:
    """Parse eslint JSON output into findings.

    Args:
        result: A CompletedProcess-like object from running eslint.

    Returns:
        A list of finding dicts.
    """
    findings: list[dict[str, Any]] = []
    output = result.stdout.strip() if hasattr(result, "stdout") else ""
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


def _run_eslint_findings_check(
    root: Path,
    runner: Callable,
) -> list[dict[str, Any]]:
    """Run eslint check (without fix) and return structured findings.

    Args:
        root: The project root path.
        runner: Subprocess runner callable.

    Returns:
        A list of finding dicts.
    """
    cmd = ["eslint", str(root), "-f", "json", "--no-eslintrc", "--quiet"]
    result = runner(cmd)

    if result.returncode not in (0, 1):
        return []

    return _run_eslint_findings(result)


def _run_ruff_check(
    root: Path,
    runner: Callable,
) -> list[dict[str, Any]]:
    """Run ruff check (without fix) and return structured findings.

    Args:
        root: The project root path.
        runner: Subprocess runner callable.

    Returns:
        A list of finding dicts.
    """
    findings: list[dict[str, Any]] = []

    cmd = ["ruff", "check", str(root), "--output-format", "json", "--quiet"]
    result = runner(cmd)

    if result.returncode not in (0, 1):
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


def classify_finding(linter: str, raw_severity: Any) -> str:
    """Map a linter's raw severity value to a normalised severity level.

    Args:
        linter: The linter name (``"ruff"``, ``"eslint"``, ``"markdownlint"``,
                ``"shellcheck"``, ``"dotnet-format"``).
        raw_severity: The raw severity value from the linter's output.
                      For ruff this is a rule code like ``"F841"``.
                      For eslint this is a number (0,1,2) or label.
                      For markdownlint/shellcheck this is a string label.
                      For dotnet-format this is ignored (always medium).

    Returns:
        One of ``"critical"``, ``"high"``, ``"medium"``, ``"low"``.
    """
    if linter == "ruff":
        return _classify_ruff(str(raw_severity) if raw_severity is not None else "")
    elif linter == "eslint":
        return _classify_eslint(raw_severity)
    elif linter == "markdownlint":
        return _classify_markdownlint(raw_severity)
    elif linter == "shellcheck":
        return _classify_shellcheck(raw_severity)
    elif linter == "dotnet-format":
        return "medium"
    # Unknown linter
    return "medium"


def _classify_markdownlint(severity: Any) -> str:
    """Classify a markdownlint severity to normalised level.
    
    Args:
        severity: The severity value (typically "error" or "warning").
    
    Returns:
        One of "high", "medium", "low".
    """
    if isinstance(severity, str):
        if severity.lower() in ("error",):
            return "high"
        if severity.lower() in ("warn", "warning"):
            return "medium"
    return "medium"


def _classify_shellcheck(severity: Any) -> str:
    """Classify a shellcheck severity to normalised level.
    
    Args:
        severity: The severity value (typically "error" or "warning").
    
    Returns:
        One of "high", "medium", "low".
    """
    if isinstance(severity, str):
        if severity.lower() in ("error",):
            return "high"
        if severity.lower() in ("warn", "warning"):
            return "medium"
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


def _commit_changes(
    root: Path,
    linter_name: str,
    runner: Callable | None = None,
) -> bool:
    """Stage and commit any changes made by a linter.

    Args:
        root: The project root path.
        linter_name: The name of the linter (for commit message).
        runner: Optional injectable runner for testing.

    Returns:
        True if changes were committed, False otherwise.
    """
    if runner is None:
        runner = _run_subprocess

    try:
        # Check if there are changes to commit
        check = runner(["git", "status", "--porcelain"], cwd=root)
        if not check.stdout.strip():
            return False

        # Stage all changes
        runner(["git", "add", "."], cwd=root)

        # Commit
        msg = f"Auto-fix: {linter_name} applied fixes"
        runner(["git", "commit", "-m", msg], cwd=root)
        return True
    except Exception:
        # Silently fail - don't block the main flow
        return False


def _normalize_paths(root: Union[str, os.PathLike[str], None] = None) -> Path:
    """Normalise the project root to an absolute Path."""
    if root is None:
        return Path.cwd().resolve()
    return Path(root).resolve()


def _run_linter_fix_mode(
    linter_name: str,
    root: Path,
    runner: Callable,
    *,
    fix_cmd_builder: Callable[[Path], list[str]],
    rescan_cmd_builder: Callable[[Path], list[str]],
    fixes_detected: Callable[[Any, str], bool],
    rescan_parser: Callable[[Any], list[dict[str, Any]]],
    commit_after_fix: bool = False,
) -> tuple[list[dict[str, Any]], bool]:
    """Run a linter in fix mode: fix → detect → optionally commit → rescan.

    Args:
        linter_name: The linter name (for logging/commit messages).
        root: The project root path.
        runner: Subprocess runner callable.
        fix_cmd_builder: Builds the fix command from the project root.
        rescan_cmd_builder: Builds the rescan (post-fix) command.
        fixes_detected: Callable that checks if fixes were applied,
                        receives (result, stdout_output) and returns bool.
        rescan_parser: Parses the rescan output into finding dicts.
        commit_after_fix: Whether to commit changes after fixing.

    Returns:
        A tuple of (findings, fixes_applied).
    """
    # Step 1: Run fix
    cmd = fix_cmd_builder(root)
    result = runner(cmd)

    if result.returncode not in (0, 1):
        return [], False

    # Step 2: Detect if fixes were applied
    output = result.stdout.strip() if hasattr(result, "stdout") else ""
    applied = fixes_detected(result, output)

    # Step 3: Optionally commit changes
    if applied and commit_after_fix:
        _commit_changes(root, linter_name, runner=runner)

    # Step 4: Rescan
    cmd = rescan_cmd_builder(root)
    result = runner(cmd)

    findings = rescan_parser(result)
    return findings, bool(applied)


def _run_ruff_fix_mode(
    root: Path,
    runner: Callable,
) -> tuple[list[dict[str, Any]], bool]:
    """Run ruff check --fix and return remaining findings."""
    def fix_cmd(root: Path) -> list[str]:
        return ["ruff", "check", str(root), "--fix", "--output-format", "json", "--quiet"]

    def rescan_cmd(root: Path) -> list[str]:
        return ["ruff", "check", str(root), "--output-format", "json", "--quiet"]

    def fixes_detected(result: Any, output: str) -> bool:
        if result.returncode == 1:
            return True
        if output:
            try:
                raw = json.loads(output)
                return isinstance(raw, list) and len(raw) > 0
            except (json.JSONDecodeError, ValueError):
                pass
        return False

    def rescan_parser(result: Any) -> list[dict[str, Any]]:
        findings: list[dict[str, Any]] = []
        if result.returncode != 1:
            return findings
        output = result.stdout.strip() if hasattr(result, "stdout") else ""
        if not output:
            return findings
        try:
            raw = json.loads(output)
        except json.JSONDecodeError:
            return findings
        if not isinstance(raw, list):
            return findings
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

    return _run_linter_fix_mode(
        "ruff", root, runner,
        fix_cmd_builder=fix_cmd,
        rescan_cmd_builder=rescan_cmd,
        fixes_detected=fixes_detected,
        rescan_parser=rescan_parser,
        commit_after_fix=False,
    )


def _run_eslint_fix_mode(
    root: Path,
    runner: Callable,
) -> tuple[list[dict[str, Any]], bool]:
    """Run eslint --fix and return remaining findings."""
    def fix_cmd(root: Path) -> list[str]:
        return ["eslint", str(root), "-f", "json", "--fix", "--no-eslintrc", "--quiet"]

    def rescan_cmd(root: Path) -> list[str]:
        return ["eslint", str(root), "-f", "json", "--no-eslintrc", "--quiet"]

    def fixes_detected(result: Any, output: str) -> bool:
        if result.returncode == 1:
            return True
        if output:
            try:
                raw = json.loads(output)
                if isinstance(raw, list):
                    for file_result in raw:
                        if isinstance(file_result, dict):
                            msgs = file_result.get("messages", [])
                            if msgs:
                                return True
            except (json.JSONDecodeError, ValueError):
                pass
        return False

    def rescan_parser(result: Any) -> list[dict[str, Any]]:
        return _run_eslint_findings(result)

    return _run_linter_fix_mode(
        "eslint", root, runner,
        fix_cmd_builder=fix_cmd,
        rescan_cmd_builder=rescan_cmd,
        fixes_detected=fixes_detected,
        rescan_parser=rescan_parser,
        commit_after_fix=True,
    )


def run_ruff(
    project_root: Union[str, os.PathLike[str], None] = None,
    runner: Any = None,
    fix: bool = False,
) -> dict[str, Any]:
    """Run ruff check on the given project root and return structured findings.

    Only runs if the linter is available on PATH and Python files are detected.

    Args:
        project_root: Path to the project root (default: cwd).
        runner: Optional injectable runner for testing (must be a callable
                accepting a list of strings and returning a
                ``subprocess.CompletedProcess``-like object).
        fix: If True, run ruff with ``--fix`` to auto-fix issues, then re-scan
             for remaining (non-fixable) issues.

    Returns:
        A dict with keys:
          - ``findings``: list of finding dicts
          - ``fixes_applied``: bool — True if any fixes were applied
        Returns empty findings and ``fixes_applied: False`` if ruff is not
        available or no Python files exist.
    """
    probe = probe_linter("ruff")
    if not probe["available"]:
        return {"findings": [], "fixes_applied": False}

    root = _normalize_paths(project_root)
    languages = detect_languages(root)

    if "python" not in languages:
        return {"findings": [], "fixes_applied": False}

    if runner is None:
        runner = _run_subprocess

    if fix:
        findings, fixes_applied = _run_ruff_fix_mode(root, runner)
    else:
        findings = _run_ruff_check(root, runner)
        fixes_applied = False

    return {"findings": findings, "fixes_applied": fixes_applied}


def run_eslint(
    project_root: Union[str, os.PathLike[str], None] = None,
    runner: Any = None,
    fix: bool = False,
) -> dict[str, Any]:
    """Run eslint on the given project root and return structured findings.

    Only runs if eslint is available on PATH and TypeScript files are detected.

    Args:
        project_root: Path to the project root (default: cwd).
        runner: Optional injectable runner for testing.
        fix: If True, run eslint with ``--fix`` to auto-fix issues, then re-scan
             for remaining (non-fixable) issues.

    Returns:
        A dict with keys:
          - ``findings``: list of finding dicts
          - ``fixes_applied``: bool — True if any fixes were applied
    """
    probe = probe_linter("eslint")
    if not probe["available"]:
        return {"findings": [], "fixes_applied": False}

    root = _normalize_paths(project_root)
    languages = detect_languages(root)

    if "typescript" not in languages and "javascript" not in languages:
        return {"findings": [], "fixes_applied": False}

    if runner is None:
        runner = _run_subprocess

    if fix:
        findings, fixes_applied = _run_eslint_fix_mode(root, runner)
    else:
        findings = _run_eslint_findings_check(root, runner)
        fixes_applied = False

    return {"findings": findings, "fixes_applied": fixes_applied}


# ---------------------------------------------------------------------------
# Phase 2 Linter runners
# ---------------------------------------------------------------------------


def run_markdownlint(
    project_root: Union[str, os.PathLike[str], None] = None,
    runner: Any = None,
    fix: bool = False,
) -> dict[str, Any]:
    """Run markdownlint on the given project root and return structured findings.

    Only runs if the linter is available on PATH and Markdown files are detected.

    Args:
        project_root: Path to the project root (default: cwd).
        runner: Optional injectable runner for testing.
        fix: If True, run markdownlint with ``--fix`` to auto-fix issues, then
             re-scan for remaining (non-fixable) issues.

    Returns:
        A dict with keys:
          - ``findings``: list of finding dicts
          - ``fixes_applied``: bool — True if any fixes were applied
    """
    probe = probe_linter("markdownlint")
    if not probe["available"]:
        return {"findings": [], "fixes_applied": False}

    root = _normalize_paths(project_root)
    languages = detect_languages(root)

    if "markdown" not in languages:
        return {"findings": [], "fixes_applied": False}

    if runner is None:
        runner = _run_subprocess

    fixes_applied = False

    if fix:
        # Run markdownlint with --fix to auto-fix issues
        cmd = ["markdownlint", "--fix", "--json", str(root)]
        result = runner(cmd)

        # markdownlint may exit 0 or 1; check if fixes were applied by
        # looking at git changes
        _commit_changes(root, "markdownlint", runner=runner)
        fixes_applied = True

        # Re-scan to get remaining issues
        cmd = ["markdownlint", "--json", str(root)]
        result = runner(cmd)
    else:
        # Normal check mode
        cmd = ["markdownlint", "--json", str(root)]
        result = runner(cmd)

    findings: list[dict[str, Any]] = []
    if result.returncode not in (0, 1):
        return {"findings": findings, "fixes_applied": fixes_applied}

    output = result.stdout.strip()
    if not output:
        return {"findings": findings, "fixes_applied": fixes_applied}

    try:
        raw = json.loads(output)
    except json.JSONDecodeError:
        return {"findings": findings, "fixes_applied": fixes_applied}

    if not isinstance(raw, list):
        return {"findings": findings, "fixes_applied": fixes_applied}

    for item in raw:
        if not isinstance(item, dict):
            continue
        severity = classify_finding("markdownlint", item.get("severity", "warning"))
        findings.append({
            "file": str(item.get("path", "")),
            "line": item.get("lineNumber", 0),
            "severity": severity,
            "message": item.get("message", ""),
            "linter": "markdownlint",
            "code": str(item.get("rule", "")),
        })

    return {"findings": findings, "fixes_applied": fixes_applied}


def run_shellcheck(
    project_root: Union[str, os.PathLike[str], None] = None,
    runner: Any = None,
) -> list[dict[str, Any]]:
    """Run shellcheck on the given project root and return structured findings.

    Only runs if shellcheck is available on PATH and Shell files are detected.

    Args:
        project_root: Path to the project root (default: cwd).
        runner: Optional injectable runner for testing.

    Returns:
        A list of finding dicts (same format as :func:`run_ruff`).
        Returns an empty list if shellcheck is not available or no Shell files exist.
    """
    probe = probe_linter("shellcheck")
    if not probe["available"]:
        return []

    root = _normalize_paths(project_root)
    languages = detect_languages(root)

    if "shell" not in languages:
        return []

    if runner is None:
        runner = _run_subprocess

    findings: list[dict[str, Any]] = []

    # Find shell scripts to check
    shell_files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for filename in filenames:
            ext = os.path.splitext(filename)[1].lower()
            if ext in {".sh", ".bash", ".zsh", ".ksh"}:
                shell_files.append(Path(dirpath) / filename)

    if not shell_files:
        return []

    for shell_file in shell_files:
        cmd = ["shellcheck", "-f", "json", str(shell_file)]
        result = runner(cmd)

        if result.returncode not in (0, 1):
            continue

        output = result.stdout.strip()
        if not output:
            continue

        try:
            raw = json.loads(output)
        except json.JSONDecodeError:
            continue

        # shellcheck -f json outputs a list of diagnostics
        diagnostics = raw if isinstance(raw, list) else [raw]
        for diag in diagnostics:
            if not isinstance(diag, dict):
                continue
            severity = classify_finding("shellcheck", diag.get("severity", "warning"))
            findings.append({
                "file": str(diag.get("file", "")),
                "line": diag.get("line", 0),
                "severity": severity,
                "message": diag.get("message", ""),
                "linter": "shellcheck",
                "code": str(diag.get("code", "")),
            })

    return findings


def run_dotnet_format(
    project_root: Union[str, os.PathLike[str], None] = None,
    runner: Any = None,
    fix: bool = False,
) -> dict[str, Any]:
    """Run dotnet-format on the given project root and return structured findings.

    Only runs if dotnet-format is available on PATH and C# files are detected.

    Args:
        project_root: Path to the project root (default: cwd).
        runner: Optional injectable runner for testing.
        fix: If True, run dotnet format without ``--verify-no-changes`` to
             auto-format files.

    Returns:
        A dict with keys:
          - ``findings``: list of finding dicts
          - ``fixes_applied``: bool — True if any formatting was applied
    """
    probe = probe_linter("dotnet-format")
    if not probe["available"]:
        return {"findings": [], "fixes_applied": False}

    root = _normalize_paths(project_root)
    languages = detect_languages(root)

    if "csharp" not in languages:
        return {"findings": [], "fixes_applied": False}

    if runner is None:
        runner = _run_subprocess

    fixes_applied = False

    if fix:
        # Auto-format mode: run without --verify-no-changes
        cmd = ["dotnet", "format", str(root), "--verbosity", "quiet"]
        result = runner(cmd)

        # Check if changes were made by looking at git status
        status = _run_subprocess(["git", "status", "--porcelain"], cwd=root)
        if status.stdout.strip():
            fixes_applied = True
            _commit_changes(root, "dotnet-format", runner=runner)

        # Re-scan for remaining issues (should be none after fix, but check)
        cmd = ["dotnet", "format", str(root), "--verify-no-changes", "--verbosity", "quiet"]
        result = runner(cmd)
    else:
        # Normal check mode
        cmd = ["dotnet", "format", str(root), "--verify-no-changes", "--verbosity", "quiet"]
        result = runner(cmd)

    findings: list[dict[str, Any]] = []
    if result.returncode not in (0, 1):
        return {"findings": findings, "fixes_applied": fixes_applied}

    output = (result.stdout + result.stderr).strip()
    if not output:
        return {"findings": findings, "fixes_applied": fixes_applied}

    # dotnet format outputs file paths for violations
    for line in output.splitlines():
        line = line.strip()
        if line and (line.endswith(".cs") or line.endswith(".csproj")):
            findings.append({
                "file": line,
                "line": 0,
                "severity": "medium",
                "message": "Formatting violation detected",
                "linter": "dotnet-format",
                "code": "formatting",
            })

    return {"findings": findings, "fixes_applied": fixes_applied}


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run_linters_for_project(
    project_root: Union[str, os.PathLike[str], None] = None,
    runner: Any = None,
    fix: bool = False,
) -> dict[str, Any]:
    """Detect languages, probe linters, and run all available linters.

    Args:
        project_root: Path to the project root (default: cwd).
        runner: Optional injectable runner for testing.
        fix: If True, run all linters with auto-fix mode enabled.

    Returns:
        A dict with keys:
          - ``languages``: list of detected language names
          - ``linters``: list of probe results
          - ``total_findings``: total number of findings
          - ``findings_by_severity``: dict of severity → count
          - ``findings``: list of finding dicts
          - ``fixes_applied``: total number of linters that applied fixes
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
    fixes_applied = 0

    for linter_info in linters:
        if not linter_info.get("available"):
            continue
        linter_name = linter_info["name"]
        if linter_name == "ruff":
            result = run_ruff(root, runner=runner, fix=fix)
            all_findings.extend(result.get("findings", []))
            if result.get("fixes_applied"):
                fixes_applied += 1
        elif linter_name == "eslint":
            result = run_eslint(root, runner=runner, fix=fix)
            all_findings.extend(result.get("findings", []))
            if result.get("fixes_applied"):
                fixes_applied += 1
        elif linter_name == "markdownlint":
            result = run_markdownlint(root, runner=runner, fix=fix)
            all_findings.extend(result.get("findings", []))
            if result.get("fixes_applied"):
                fixes_applied += 1
        elif linter_name == "shellcheck":
            result = run_shellcheck(root, runner=runner)
            all_findings.extend(result)
        elif linter_name == "dotnet-format":
            result = run_dotnet_format(root, runner=runner, fix=fix)
            all_findings.extend(result.get("findings", []))
            if result.get("fixes_applied"):
                fixes_applied += 1

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
        "fixes_applied": fixes_applied,
    }
