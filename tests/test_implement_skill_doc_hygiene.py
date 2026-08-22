"""Doc hygiene tests for the implement skill."""

import re
from pathlib import Path

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


# ── Push-policy clarity (SA-0MT4D6N7T004LFYO) ────────────────────────────────

class TestPushPolicyClarity:
    """The security note must not read as requiring operator approval for
    dev pushes (incident: TCE CG-0MT3IPFSF005KEFB stalled at the push gate
    awaiting authorization that the workflow already grants)."""

    def test_dev_push_is_pre_authorized(self) -> None:
        """SKILL.md must state that pushing the feature branch to `dev`
        needs no additional approval when tests and build pass."""
        content = _skill_content()
        assert "pre-authorized" in content or "no additional operator approval" in content, (
            "Security note must explicitly pre-authorize dev pushes"
        )

    def test_security_note_scoped_to_protected_branches_and_prs(self) -> None:
        """The explicit-permission requirement must name protected branches
        (main/master/HEAD) and PRs — not the dev integration push."""
        content = _skill_content()
        note = content.split("Security note")[1].split("Privacy note")[0]
        assert "main" in note and "master" in note and "HEAD" in note, (
            "Security note must scope the permission requirement to "
            "main/master/HEAD and PRs"
        )
        assert "dev" in note, "Security note must reference the dev exception"

    def test_worktree_push_mechanics_documented(self) -> None:
        """The pre-push hook runs `wl sync`, which fails from worktrees;
        the skill must document running wl sync from the main checkout and
        the WORKLOG_SKIP_PRE_PUSH=1 bypass."""
        content = _skill_content()
        assert "WORKLOG_SKIP_PRE_PUSH" in content, (
            "Step 8 must document the WORKLOG_SKIP_PRE_PUSH=1 worktree bypass"
        )
