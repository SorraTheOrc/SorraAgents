#!/usr/bin/env python3
"""Tests: effort-and-risk SKILL.md output redirect stays outside the repo.

Work item: SA-0MSJ1BP9K009M8MV.

The workflow captures the orchestrator's JSON via a shell redirect
(``> final-<id>.json``). When the redirect target is repo-relative, the
artifact lands inside the tracked ``skill/effort-and-risk/`` directory,
dirtying the working tree and blocking ``implement.py start`` (observed with
``final-SA-0MSIU5HFI0024D7W.json`` during SA-0MSIU5HFI0024D7W).

These tests assert every documented redirect target in SKILL.md resolves
outside the repository tree (mirrors the audit skill's SKILL.md-validation
test pattern at ``skill/audit/tests/test_skill_md_and_debug_logs.py``).

All tests run offline.
"""  # noqa: EXE001
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SKILL_MD = REPO_ROOT / "skill" / "effort-and-risk" / "SKILL.md"

# Documented redirect targets: "> <path>" or "redirect ... to <path>".
_REDIRECT_RE = re.compile(
    r"(?:>\s+|redirect\s+(?:the\s+)?(?:output|orchestrator(?:\s+output)?)?\s*(?:to\s+))"
    r"(?P<path>[^\s`\n]+\.json)"
)


class TestSkillMdOutputPath:
    def test_skill_md_documents_at_least_one_redirect(self) -> None:
        """SKILL.md documents a redirect target for the orchestrator output."""
        text = SKILL_MD.read_text()
        assert _REDIRECT_RE.search(text), (
            "No redirect target found; SKILL.md must document where the "
            "orchestrator output is written."
        )

    def test_every_documented_redirect_resolves_outside_repo(self) -> None:
        """Each documented redirect target is absolute and outside the repo."""
        text = SKILL_MD.read_text()
        matches = list(_REDIRECT_RE.finditer(text))
        assert matches, "SKILL.md must document the orchestrator output path."
        for m in matches:
            target = m.group("path")
            assert target.startswith("/"), (
                f"Redirect target {target!r} must be an absolute path "
                "(e.g. /tmp/effort-risk-final-<id>.json), not repo-relative."
            )
            resolved = Path(target).resolve()
            assert not resolved.is_relative_to(REPO_ROOT.resolve()), (
                f"Redirect target {target!r} resolves inside the repository "
                f"({resolved}); use /tmp/ so the artifact cannot pollute the "
                "working tree."
            )

    def test_skill_md_never_documents_repo_relative_final_json(self) -> None:
        """No redirect writes a bare `final-*.json` into the repo tree."""
        text = SKILL_MD.read_text()
        # Any redirect whose target is not absolute would land in the cwd.
        for m in _REDIRECT_RE.finditer(text):
            assert m.group("path").startswith("/"), (
                f"Redirect target {m.group('path')!r} is repo-relative; "
                "artifacts must go to /tmp/."
            )
