#!/usr/bin/env python3
"""Lint agent front-matter for required fields, allowed model values, wildcard permissions, and tool/boundary contradictions.

Usage:
  scripts/agent_frontmatter_lint.py [--path agent] [--allowed-models-file file] [--format json]

Checks:
  - Required fields: description, mode, model, temperature
  - Allowed model values and canonical mappings
  - Wildcard bash permissions (flagged as warnings)
  - Tool/boundary contradictions (flagged as warnings)

Exits with code 0 if no errors found, 1 if warnings only, 2 if errors found.
"""
import argparse
import sys
import yaml
import re
from pathlib import Path

REQUIRED_FIELDS = ["description", "mode", "model", "temperature"]
# Allowed canonical models list. Keep this small and extendable.
ALLOWED_MODELS = [
    "github-copilot/gpt-5.2",
    "github-copilot/gpt-5-mini",
]

# Mapping patterns -> canonical model for safe automated fixes
MODEL_CANONICAL_MAP = {
    # variants that should be canonicalised to gpt-5.2
    r"github-copilot/gpt-5.2(-codex)?": "github-copilot/gpt-5.2",
    r"proxy/gemma4": "github-copilot/gpt-5.2",
    # lightweight
    r"github-copilot/gpt-5-mini": "github-copilot/gpt-5-mini",
}


def extract_front_matter(text):
    """Extract YAML between leading '---' pairs.

    Returns (fm_text, body_text) or (None, None) if no front-matter found.
    """
    if not text.startswith("---"):
        return None, None
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None, None
    return parts[1], parts[2]


def find_agent_files(base):
    p = Path(base)
    return sorted([str(x) for x in p.glob("*.md") if x.is_file()])


def validate_file(path):
    text = Path(path).read_text(encoding="utf-8")
    fm_text, body = extract_front_matter(text)
    if fm_text is None:
        return {"file": path, "errors": ["missing front-matter"], "warnings": []}
    try:
        data = yaml.safe_load(fm_text) or {}
    except Exception as e:
        return {"file": path, "errors": [f"yaml parse error: {e}"], "warnings": []}
    errors = []
    warnings = []
    for f in REQUIRED_FIELDS:
        if f not in data:
            errors.append(f"missing required field '{f}'")
    model = data.get("model")
    if model:
        if model not in ALLOWED_MODELS:
            # check if model matches a known canonical pattern -> suggest
            matched = False
            for pat, canon in MODEL_CANONICAL_MAP.items():
                if re.fullmatch(pat, model):
                    warnings.append(f"model '{model}' should be canonicalised to '{canon}'")
                    matched = True
                    break
            if not matched:
                errors.append(f"disallowed model value '{model}' (not in allowed list)")
    else:
        errors.append("model missing or empty")

    # Wildcard bash permission detection
    perm = data.get("permission") or {}
    bash_perm = perm.get("bash") if isinstance(perm, dict) else None
    if isinstance(bash_perm, dict):
        if any(k == "*" for k in bash_perm.keys()):
            # Check for documented justification in raw front-matter
            if re.search(r"wildcard\-bash\-justification:", fm_text):
                pass  # documented exception
            else:
                warnings.append("wildcard bash permission '*' detected; require justification")

    # Tools vs boundaries contradiction detection (conservative)
    tools = data.get("tools") or {}
    write_allowed = bool(tools.get("write")) if isinstance(tools, dict) else False
    boundaries_text = ""
    if body:
        m = re.search(r"\nBoundaries:\n(.*?)(\n\S|$)", body, re.S)
        if m:
            boundaries_text = m.group(1)
        else:
            idx = body.find("Boundaries:")
            if idx != -1:
                boundaries_text = body[idx:]
    if write_allowed and boundaries_text:
        if re.search(r"never (write|modify|commit|push)", boundaries_text, re.I):
            # Check for documented justification in raw front-matter
            if re.search(r"tools\-write\-contradiction\-justification:", fm_text):
                pass  # documented exception
            else:
                warnings.append("tools.write=true but boundaries contain 'never write/modify' — possible contradiction")

    return {"file": path, "errors": errors, "warnings": warnings}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--path", default="agent", help="Path to agent md files")
    ap.add_argument("--format", choices=["text", "json"], default="text")
    args = ap.parse_args()

    files = find_agent_files(args.path)
    results = [validate_file(f) for f in files]

    total_errors = sum(len(r["errors"]) for r in results)
    total_warnings = sum(len(r["warnings"]) for r in results)

    if args.format == "json":
        import json
        print(json.dumps({"results": results, "errors": total_errors, "warnings": total_warnings}, indent=2))
    else:
        for r in results:
            print(f"{r['file']}")
            for e in r["errors"]:
                print(f"  ERROR: {e}")
            for w in r["warnings"]:
                print(f"  WARN:  {w}")
            if not r["errors"] and not r["warnings"]:
                print("  OK")
            print()
        print(f"Summary: {len(files)} files scanned, {total_errors} errors, {total_warnings} warnings")

    if total_errors > 0:
        sys.exit(2)
    if total_warnings > 0:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
