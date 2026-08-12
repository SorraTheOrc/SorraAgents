"""Relative-link hygiene tests for SKILL.md files (SA-0MSOZDDLY009YBLL).

F5 trimmed the six largest SKILL.md files by relocating implementation
reference sections to repo-root `docs/dev/`. The trimmed files link back to
those docs with relative paths like `[docs/dev/audit-skill-reference.md]
(../../docs/dev/audit-skill-reference.md)`. A one-level-short path
(`../docs/dev/...`) resolves from the skill dir to `skill/docs/dev/...`,
which does not exist — a dead link an agent would hit mid-workflow.

These tests scan every `skill/*/SKILL.md` for relative markdown links and
assert each one resolves to an existing file (or directory anchor) relative
to the skill file's own directory. Absolute links and anchors (`#...`) are
skipped; URLs are skipped.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_DIR = REPO_ROOT / "skill"

LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")


def _lines_outside_fences(text: str) -> list[tuple[int, str]]:
    """Return (line_no, line) pairs, skipping ```-fenced code blocks where
    link-looking text is illustrative, not a real reference."""
    result: list[tuple[int, str]] = []
    in_fence = False
    for line_no, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence:
            result.append((line_no, line))
    return result


def _relative_links(skill_file: Path) -> list[tuple[int, str, str]]:
    """Return [(line_no, raw_target, resolved_path_str), ...] for relative links."""
    found: list[tuple[int, str, str]] = []
    for line_no, line in _lines_outside_fences(skill_file.read_text()):
        for match in LINK_RE.finditer(line):
            target = match.group(1).strip()
            # Skip anchors, URLs, and mailto links.
            if target.startswith("#") or "://" in target or target.startswith("mailto:"):
                continue
            # Strip a trailing #fragment before resolving the file path.
            file_target = target.split("#", 1)[0]
            # Skip fully-qualified filesystem paths.
            if file_target.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", file_target):
                continue
            found.append((line_no, target, str((skill_file.parent / file_target).resolve())))
    return found


def _all_skill_files() -> list[Path]:
    return sorted(SKILL_DIR.glob("*/SKILL.md"))


def _all_reference_docs() -> list[Path]:
    """Reference docs at repo root docs/dev/ that skills link back to."""
    return sorted((REPO_ROOT / "docs" / "dev").glob("*.md"))


class TestSkillRelativeLinksResolve:
    def test_every_relative_link_in_every_skill_resolves(self):
        broken: list[str] = []
        for skill_file in _all_skill_files():
            for line_no, target, resolved in _relative_links(skill_file):
                resolved_path = Path(resolved)
                if not resolved_path.exists():
                    broken.append(
                        f"{skill_file.relative_to(REPO_ROOT)}:{line_no} "
                        f"-> '{target}' (resolved to {resolved}, missing)"
                    )
        assert not broken, "Broken relative links:\n" + "\n".join(broken)

    def test_every_relative_link_in_every_reference_doc_resolves(self):
        """Reference docs are linked both from skills and from each other;
        keep their internal relative links healthy too."""
        broken: list[str] = []
        for doc in _all_reference_docs():
            for line_no, target, resolved in _relative_links(doc):
                resolved_path = Path(resolved)
                if not resolved_path.exists():
                    broken.append(
                        f"{doc.relative_to(REPO_ROOT)}:{line_no} "
                        f"-> '{target}' (resolved to {resolved}, missing)"
                    )
        assert not broken, "Broken relative links:\n" + "\n".join(broken)

    def test_no_one_level_short_docs_dev_links(self):
        """Regression: `../docs/dev/` from a skill dir is always wrong
        (resolves to skill/docs/dev/). Every docs/dev link must use ../../."""
        offenders = []
        for skill_file in _all_skill_files():
            text = skill_file.read_text()
            if re.search(r"\]\(\.\./docs/dev/", text):
                offenders.append(str(skill_file.relative_to(REPO_ROOT)))
        assert not offenders, (
            "Found one-level-short docs/dev links (should be ../../docs/dev/): "
            + ", ".join(offenders)
        )

    def test_reference_docs_present_at_repo_root(self):
        """The F5-relocated reference docs must exist at repo root docs/dev/."""
        expected = [
            "audit-skill-reference.md",
            "implement-skill-reference.md",
            "intake-skill-reference.md",
            "plan-skill-reference.md",
            "ship-skill-reference.md",
            "test-skill-reference.md",
        ]
        missing = [n for n in expected if not (REPO_ROOT / "docs" / "dev" / n).exists()]
        assert not missing, f"Missing relocated reference docs: {missing}"
