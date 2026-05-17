"""Agent front-matter validator utilities used by pytest.

Provides a small set of helpers to extract YAML front-matter from agent/*.md
and perform schema + semantic checks.
"""
from pathlib import Path
import yaml
import re
from typing import List, Dict, Tuple

REQUIRED_FIELDS = ["description", "mode", "model", "temperature"]
ALLOWED_MODELS = [
    "github-copilot/gpt-5.2",
    "github-copilot/gpt-5-mini",
]

MODEL_CANONICAL_PATTERNS = {
    r"github-copilot/gpt-5.2(-codex)?": "github-copilot/gpt-5.2",
    r"proxy/gemma4": "github-copilot/gpt-5.2",
    r"github-copilot/gpt-5-mini": "github-copilot/gpt-5-mini",
}


def find_agent_files(base: str = "agent") -> List[Path]:
    p = Path(base)
    return sorted([x for x in p.glob("*.md") if x.is_file()])


def extract_front_matter(text: str) -> Tuple[Dict, str, str]:
    """Return (front_matter_dict, body_text, raw_fm_text). Raises ValueError on parse error."""
    if not text.startswith("---"):
        raise ValueError("missing front-matter delimiters")
    parts = text.split("---", 2)
    if len(parts) < 3:
        raise ValueError("invalid front-matter block")
    fm_text = parts[1]
    body = parts[2]
    data = yaml.safe_load(fm_text) or {}
    return data, body, fm_text


def validate_front_matter(data: Dict, body: str, fm_raw: str = "") -> Tuple[List[str], List[str]]:
    """Validate a single agent front-matter dict.

    Returns (errors, warnings).
    - errors: missing required fields, disallowed model values
    - warnings: canonicalisation suggestions, wildcard permissions (unless justified),
      tool/boundary contradictions (unless justified)

    Justification comments in the raw front-matter text suppress warnings:
    - `wildcard-bash-justification:` suppresses wildcard bash permission warnings
    - `tools-write-contradiction-justification:` suppresses tool/boundary contradiction warnings
    """
    errors: List[str] = []
    warnings: List[str] = []

    for f in REQUIRED_FIELDS:
        if f not in data:
            errors.append(f"missing required field '{f}'")

    model = data.get("model")
    if model:
        if model not in ALLOWED_MODELS:
            # check canonical patterns
            matched = False
            for pat, canon in MODEL_CANONICAL_PATTERNS.items():
                if re.fullmatch(pat, str(model)):
                    warnings.append(f"model '{model}' should be canonicalised to '{canon}'")
                    matched = True
                    break
            if not matched:
                errors.append(f"disallowed model value '{model}'")
    else:
        # missing handled above
        pass

    # permissions check (semantic)
    perm = data.get("permission") or {}
    bash_perm = perm.get("bash") if isinstance(perm, dict) else None
    if isinstance(bash_perm, dict):
        # look for wildcard key '*' or quoted '*' which YAML parses to '*'
        if any(k == "*" for k in bash_perm.keys()):
            if re.search(r"wildcard\-bash\-justification:", fm_raw):
                pass  # documented exception
            else:
                warnings.append("wildcard bash permission '*' detected; require justification")

    # tools vs boundaries contradiction detection (conservative)
    tools = data.get("tools") or {}
    write_allowed = bool(tools.get("write")) if isinstance(tools, dict) else False
    # search body for lines under 'Boundaries:' that include 'never' + verb
    boundaries_text = ""
    m = re.search(r"\nBoundaries:\n(.*?)(\n\S|$)", body, re.S)
    if m:
        boundaries_text = m.group(1)
    else:
        # fallback: search for 'Boundaries:' and take rest of document
        idx = body.find("Boundaries:")
        if idx != -1:
            boundaries_text = body[idx:]

    if write_allowed and boundaries_text:
        if re.search(r"never (write|modify|commit|push)", boundaries_text, re.I):
            if re.search(r"tools\-write\-contradiction\-justification:", fm_raw):
                pass  # documented exception
            else:
                warnings.append("tools.write=true but boundaries contain 'never write/modify' — possible contradiction")

    return errors, warnings


def validate_all_agents(base: str = "agent") -> Dict[str, Dict]:
    """Run validation across all agent files and return a map of path -> {errors, warnings}.

    This is intentionally lightweight and conservative to avoid false positives.
    Justification comments in front-matter suppress warnings: see validate_front_matter docstring.
    """
    files = find_agent_files(base)
    out = {}
    for p in files:
        txt = p.read_text(encoding="utf-8")
        try:
            data, body, fm_raw = extract_front_matter(txt)
        except Exception as e:
            out[str(p)] = {"errors": [f"front-matter parse error: {e}"], "warnings": []}
            continue
        errors, warnings = validate_front_matter(data, body, fm_raw)
        out[str(p)] = {"errors": errors, "warnings": warnings}
    return out
