"""Doc hygiene tests for the implement skill."""

from pathlib import Path
import re


_REPO_ROOT = Path(__file__).resolve().parent.parent
_SKILL_MD = _REPO_ROOT / "skill" / "implement" / "SKILL.md"


def _skill_content() -> str:
    assert _SKILL_MD.exists(), f"implement skill doc not found at {_SKILL_MD}"
    return _SKILL_MD.read_text(encoding="utf-8")


def test_implement_skill_uses_pi_command_names_instead_of_opencode_paths() -> None:
    content = _skill_content()
    assert ".command/" not in content
    assert "Intake/interview helpers: `intake`, `plan`." in content


def test_implement_skill_has_no_dangling_any_asset_line() -> None:
    content = _skill_content()
    assert not re.search(r"^\s*-\s+any\s*$", content, re.MULTILINE)
