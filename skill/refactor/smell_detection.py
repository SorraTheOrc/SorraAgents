"""Hybrid smell detection engine combining linter analysis with LLM analysis.

Provides:
  - detect_smells(): Main entry point for hybrid smell detection.
  - detect_linter_smells(): Linter-based code smell detection.
  - detect_llm_smells(): LLM-based design/architectural smell detection.
  - load_rules(): Load smell detection rules from config or defaults.
  - classify_smell_severity(): Map raw severity to normalised level.

Usage:

    from skill.refactor.smell_detection import detect_smells, load_rules

    rules = load_rules(".refactor.json")
    findings = detect_smells(
        files=["src/main.py", "src/utils.py"],
        mode="hybrid",
        rules=rules,
        llm_client=my_llm_client,
    )
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from skill.code_review.scripts.linter_runner import (
    classify_finding as _classify_linter_finding,
    run_ruff,
    run_eslint,
    probe_linter,
)


LOG = logging.getLogger("refactor.smell_detection")

# ---------------------------------------------------------------------------
# Default rules
# ---------------------------------------------------------------------------

DEFAULT_RULES: dict[str, Any] = {
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

# ---------------------------------------------------------------------------
# Finding key constants
# ---------------------------------------------------------------------------

REQUIRED_FINDING_KEYS = {"file", "line", "severity", "message", "source", "smell_type", "code"}
VALID_SEVERITIES = {"critical", "high", "medium", "low"}

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_rules(config_path: str | None = None) -> dict[str, Any]:
    """Load smell detection rules from a config file or return defaults.

    If *config_path* points to a valid JSON file, the file is loaded and
    merged with ``DEFAULT_RULES`` (file values take precedence).  If the
    file does not exist or contains invalid JSON, default rules are returned.

    Args:
        config_path: Optional path to a ``.refactor.json`` config file.
                     If ``None``, default rules are returned.

    Returns:
        A dict of rules configuration (always contains at least the keys
        from ``DEFAULT_RULES``).
    """
    rules: dict[str, Any] = {}
    try:
        rules = dict(DEFAULT_RULES)
    except Exception:
        rules = {}

    if config_path is not None:
        try:
            path = Path(config_path)
            if path.is_file():
                with open(path, "r") as f:
                    custom = json.load(f)
                if isinstance(custom, dict):
                    rules = _deep_merge(rules, custom)
        except (json.JSONDecodeError, OSError) as exc:
            LOG.warning("Failed to load rules from %s: %s", config_path, exc)

    return rules


def detect_smells(
    files: list[str],
    mode: str = "hybrid",
    rules: dict[str, Any] | None = None,
    llm_client: Any = None,
) -> list[dict[str, Any]]:
    """Detect code smells in the given files.

    Args:
        files: List of file paths to analyze.
        mode: Detection mode — ``"linter"``, ``"llm"``, or ``"hybrid"``
              (default).
        rules: Optional rules configuration. If ``None``, defaults are loaded
               via :func:`load_rules`.
        llm_client: Optional LLM client for LLM-based detection.  Must have
                    an ``analyze()`` method that accepts file paths and
                    returns a list of finding dicts.

    Returns:
        A list of finding dicts, each with keys:
        ``file``, ``line``, ``severity``, ``message``, ``source``,
        ``smell_type``, ``code``.

    Raises:
        ValueError: If *mode* is not one of ``"linter"``, ``"llm"``,
                    ``"hybrid"``.
    """
    if mode not in ("linter", "llm", "hybrid"):
        raise ValueError(
            f"Invalid mode: '{mode}'. Expected 'linter', 'llm', or 'hybrid'."
        )

    if rules is None:
        rules = load_rules()

    all_findings: list[dict[str, Any]] = []

    if mode in ("linter", "hybrid"):
        linter_enabled = rules.get("linter", {}).get("enabled", True)
        if linter_enabled:
            linter_findings = detect_linter_smells(
                files=files,
                rules=rules,
            )
            all_findings.extend(linter_findings)

    if mode in ("llm", "hybrid"):
        llm_enabled = rules.get("llm", {}).get("enabled", True)
        if llm_enabled and llm_client is not None:
            llm_findings = detect_llm_smells(
                files=files,
                llm_client=llm_client,
                rules=rules,
            )
            all_findings.extend(llm_findings)

    # Deduplicate by (file, line, code)
    deduplicated = _deduplicate_findings(all_findings)

    return deduplicated


def detect_linter_smells(
    files: list[str],
    rules: dict[str, Any] | None = None,
    linter_output: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Detect code smells using linter analysis.

    If *linter_output* is provided, it is parsed directly instead of
    running linters.  This is used primarily for testing.

    Args:
        files: List of file paths to analyze.
        rules: Optional rules configuration (unused in current impl).
        linter_output: Optional pre-collected linter output keyed by linter
                       name (e.g. ``{"ruff": "...json..."}``).

    Returns:
        A list of finding dicts.
    """
    if not files:
        return []

    findings: list[dict[str, Any]] = []

    if linter_output is not None:
        # Parse pre-collected linter output (bypass file existence check
        # since caller is providing mock/custom output for testing)
        for linter_name, raw_output in linter_output.items():
            parsed = _parse_linter_output(linter_name, raw_output)
            findings.extend(parsed)
        return _deduplicate_findings(findings)

    # Filter only existing files for actual linter runs
    existing_files = [f for f in files if os.path.isfile(f)]
    if not existing_files:
        return []
    else:
        # Run actual linters on the project
        project_root = _find_common_root(existing_files)
        if project_root is None:
            return []

        # Run ruff if available and Python files are present
        python_files = [f for f in existing_files if f.endswith(".py")]
        if python_files:
            ruff_probe = probe_linter("ruff")
            if ruff_probe.get("available"):
                ruff_result = run_ruff(project_root)
                for rf in ruff_result.get("findings", []):
                    finding = {
                        "file": rf.get("file", ""),
                        "line": rf.get("line", 0),
                        "severity": rf.get("severity", "medium"),
                        "message": rf.get("message", ""),
                        "source": "linter",
                        "smell_type": _linter_code_to_smell_type(
                            rf.get("linter", "ruff"),
                            rf.get("code", ""),
                        ),
                        "code": rf.get("code", ""),
                    }
                    findings.append(finding)

        # Run eslint if available and JS/TS files are present
        js_files = [f for f in existing_files
                    if f.endswith((".js", ".jsx", ".ts", ".tsx"))]
        if js_files:
            eslint_probe = probe_linter("eslint")
            if eslint_probe.get("available"):
                eslint_result = run_eslint(project_root)
                for ef in eslint_result.get("findings", []):
                    finding = {
                        "file": ef.get("file", ""),
                        "line": ef.get("line", 0),
                        "severity": ef.get("severity", "medium"),
                        "message": ef.get("message", ""),
                        "source": "linter",
                        "smell_type": _linter_code_to_smell_type(
                            ef.get("linter", "eslint"),
                            ef.get("code", ""),
                        ),
                        "code": ef.get("code", ""),
                    }
                    findings.append(finding)

    # Deduplicate within linter findings
    return _deduplicate_findings(findings)


def detect_llm_smells(
    files: list[str],
    llm_client: Any,
    rules: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Detect code smells using LLM analysis.

    The *llm_client* must have an ``analyze()`` method that can be called
    with file-related context and returns a list of finding dicts with at
    least ``file``, ``line``, ``severity``, ``message``, ``smell_type``,
    and ``code`` keys.

    Args:
        files: List of file paths to analyze.
        llm_client: An LLM client with an ``analyze()`` method.
        rules: Optional rules configuration.

    Returns:
        A list of finding dicts.
    """
    if not files:
        return []

    # Filter only existing files
    existing_files = [f for f in files if os.path.isfile(f)]
    if not existing_files:
        return []

    if not hasattr(llm_client, "analyze"):
        LOG.warning("LLM client missing 'analyze' method")
        return []

    findings: list[dict[str, Any]] = []

    try:
        result = llm_client.analyze(files=existing_files, rules=rules)
        if isinstance(result, list):
            for item in result:
                if not isinstance(item, dict):
                    continue
                # Ensure all required keys are present
                finding = {
                    "file": str(item.get("file", existing_files[0] if existing_files else "")),
                    "line": int(item.get("line", 0)),
                    "severity": str(item.get("severity", "medium")),
                    "message": str(item.get("message", "")),
                    "source": "llm",
                    "smell_type": str(item.get("smell_type", "unknown")),
                    "code": str(item.get("code", "")),
                }
                findings.append(finding)
    except Exception as exc:
        LOG.warning("LLM smell detection failed: %s", exc)

    return _deduplicate_findings(findings)


def classify_smell_severity(source: str, raw_severity: str) -> str:
    """Classify a raw severity value to a normalised level.

    Args:
        source: The source type (``"ruff"``, ``"eslint"``, ``"llm"``, etc.).
        raw_severity: The raw severity value.

    Returns:
        One of ``"critical"``, ``"high"``, ``"medium"``, ``"low"``.
    """
    if source in ("ruff", "eslint", "shellcheck", "markdownlint", "dotnet-format"):
        return _classify_linter_finding(source, raw_severity)

    # LLM or unknown source: preserve known severity, default to medium
    if raw_severity in VALID_SEVERITIES:
        return raw_severity
    return "medium"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _deep_merge(base: dict, override: dict) -> dict:
    """Deep-merge *override* into *base* and return a new dict."""
    result = {}
    for key, base_val in base.items():
        if key in override:
            override_val = override[key]
            if isinstance(base_val, dict) and isinstance(override_val, dict):
                result[key] = _deep_merge(base_val, override_val)
            else:
                result[key] = override_val
        else:
            result[key] = base_val
    # Add keys from override that are not in base
    for key in override:
        if key not in result:
            result[key] = override[key]
    return result


def _deduplicate_findings(
    findings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Remove duplicate findings based on (file, line, code)."""
    seen: set[tuple[str, int, str]] = set()
    unique: list[dict[str, Any]] = []
    for f in findings:
        key = (f.get("file", ""), f.get("line", 0), f.get("code", ""))
        if key not in seen:
            seen.add(key)
            unique.append(f)
    return unique


def _find_common_root(files: list[str]) -> str | None:
    """Find the common parent directory for a list of file paths."""
    if not files:
        return None
    try:
        paths = [os.path.dirname(os.path.abspath(f)) for f in files]
        if not paths:
            return None
        common = os.path.commonpath(paths)
        return common if common else None
    except (ValueError, OSError):
        return None


def _parse_linter_output(
    linter_name: str,
    raw_output: str,
) -> list[dict[str, Any]]:
    """Parse a linter's JSON output into finding dicts.

    Args:
        linter_name: The name of the linter (``"ruff"``, ``"eslint"``).
        raw_output: The raw JSON output string from the linter.

    Returns:
        A list of finding dicts.
    """
    findings: list[dict[str, Any]] = []
    if not raw_output or not raw_output.strip():
        return findings

    try:
        data = json.loads(raw_output)
    except json.JSONDecodeError:
        return findings

    if linter_name == "ruff" and isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            loc = item.get("location", {}) or {}
            code = str(item.get("code", ""))
            findings.append({
                "file": str(item.get("filename", "")),
                "line": loc.get("row", 0) if isinstance(loc, dict) else 0,
                "severity": classify_smell_severity("ruff", code),
                "message": item.get("message", ""),
                "source": "linter",
                "smell_type": _linter_code_to_smell_type("ruff", code),
                "code": code,
            })
    elif linter_name == "eslint" and isinstance(data, list):
        for file_result in data:
            if not isinstance(file_result, dict):
                continue
            file_path = file_result.get("filePath", "")
            messages = file_result.get("messages", [])
            if isinstance(messages, list):
                for msg in messages:
                    if not isinstance(msg, dict):
                        continue
                    code = str(msg.get("ruleId", ""))
                    sev = msg.get("severity", 1)
                    findings.append({
                        "file": file_path,
                        "line": msg.get("line", 0),
                        "severity": classify_smell_severity("eslint", sev),
                        "message": msg.get("message", ""),
                        "source": "linter",
                        "smell_type": _linter_code_to_smell_type("eslint", code),
                        "code": code,
                    })

    return findings


def _linter_code_to_smell_type(linter: str, code: str) -> str:
    """Map a linter rule code to a generalised smell type.

    Args:
        linter: The linter name (``"ruff"``, ``"eslint"``).
        code: The linter rule code (e.g. ``"F401"``, ``"E302"``).

    Returns:
        A smell type string (e.g. ``"unused_import"``, ``"formatting"``).
    """
    if linter == "ruff":
        # Ruff rule code prefix to smell type mapping
        prefix = ""
        for ch in code:
            if ch.isalpha():
                prefix += ch
            else:
                break

        mapping: dict[str, str] = {
            "F": "unused_import",      # Pyflakes (F401=unused import, F841=unused var)
            "E": "formatting",          # pycodestyle errors
            "W": "formatting",          # pycodestyle warnings
            "D": "documentation",       # pydocstyle
            "N": "naming",             # pep8-naming
            "C": "complexity",          # mccabe complexity
            "UP": "modernization",      # pyupgrade
            "ANN": "annotation",        # flake8-annotations
            "S": "security",            # flake8-bandit
            "B": "bug_risk",            # flake8-bugbear
            "SIM": "simplification",    # flake8-simplify
            "T20": "debug_code",        # flake8-print/debugger
            "PL": "pylint",             # Pylint rules
            "RUF": "ruff_specific",     # Ruff-specific rules
        }
        if prefix in mapping:
            return mapping[prefix]
        if len(prefix) >= 1 and prefix[0] in {"F", "E", "W"}:
            return mapping.get(prefix[0], "unknown")
        return "unknown"

    if linter == "eslint":
        return "lint"
    return "unknown"
