"""Doc hygiene tests for the implement skill."""

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SKILL_MD = _REPO_ROOT / "skill" / "implement" / "SKILL.md"
_REFERENCE_DOC = _REPO_ROOT / "docs" / "dev" / "implement-skill-reference.md"


def _skill_content() -> str:
    assert _SKILL_MD.exists(), f"implement skill doc not found at {_SKILL_MD}"
    return _SKILL_MD.read_text(encoding="utf-8")


def test_implement_skill_uses_pi_command_names_instead_of_opencode_paths() -> None:
    content = _skill_content()
    assert ".command/" not in content
    assert "Intake/interview helpers: `intake`, `plan`, `interview`." in content


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


# ── Finish lock, verified sync, refusal recovery (SA-0MVPZGAU5001FDO3) ───────


def _normalised(path: Path) -> str:
    assert path.exists(), f"File not found: {path}"
    return " ".join(path.read_text(encoding="utf-8").split())


def _finish_refusal_section() -> str:
    """The SKILL.md 'Finish lock, verified sync and refusal recovery' block."""
    content = _normalised(_SKILL_MD)
    marker = "Finish lock, verified sync and refusal recovery"
    start = content.find(marker)
    assert start != -1, f"{_SKILL_MD.name} must document {marker!r}"
    end = content.find("Pre-push blocking check", start)
    assert end != -1 and end > start, (
        "the finish-refusal section must precede the pre-push check section"
    )
    return content[start:end]


class TestFinishLockAndVerifiedSyncDocs:
    """AC: the lock, verified sync and refusal recovery are documented."""

    def test_documents_bounded_wait_and_crash_safety(self) -> None:
        section = _finish_refusal_section()
        assert "IMPLEMENT_FINISH_LOCK_TIMEOUT" in section
        assert "IMPLEMENT_START_LOCK_TIMEOUT" in section
        assert "flock" in section
        assert "stale lock file cannot block a later finish" in section
        assert "indefinite wait is rejected" in section

    def test_documents_the_verified_reversible_advance(self) -> None:
        section = _finish_refusal_section()
        assert "git merge --ff-only" in section
        assert "refs/remotes/origin/dev" in section
        assert "reset --merge" in section
        assert "reset --hard" in section  # the prose forbids it explicitly
        assert "local_dev_diverged" in section

    def test_documents_the_ancestry_assertion_failing_closed(self) -> None:
        section = _finish_refusal_section()
        assert "git merge-base --is-ancestor" in section
        assert "AncestryAssertionError" in section
        assert "never report success for an orphaned commit" in section

    def test_documents_the_refusal_recovery_path(self) -> None:
        section = _finish_refusal_section()
        assert "retained" in section
        assert "stays `open`" in section
        assert "git rebase origin/dev" in section
        assert "git push origin HEAD:refs/heads/dev" in section
        assert ".worklog/locks/main-checkout.lock" in section
        assert "scripts/install_pi.sh" in section
        assert "only after" in section

    def test_step_9_cross_links_the_recovery_section(self) -> None:
        content = _normalised(_SKILL_MD)
        assert "Finish lock, verified sync and refusal recovery" in content
        assert "per-repository finish lock" in content

    def test_reference_doc_documents_the_same_contract(self) -> None:
        doc = _normalised(_REFERENCE_DOC)
        assert (
            "Verified reversible local-dev sync and post-push ancestry "
            "assertion" in doc
        )
        for token in (
            "local_dev_fast_forward",
            "local_dev_restore",
            "ancestry_assertion",
            "AncestryAssertionError",
            "branch_moved_since_advance",
            "local_dev_diverged",
            "commit_not_based_on_remote_tip",
            "scripts/install_pi.sh",
        ):
            assert token in doc, f"reference doc is missing {token!r}"
