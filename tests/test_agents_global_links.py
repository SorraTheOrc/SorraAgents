"""Reader-perspective link tests for ``AGENTS_GLOBAL.md`` (SA-0MVZ4BECY002X81S).

``~/.pi/agent/AGENTS.md`` is a symlink to this repository's
``AGENTS_GLOBAL.md``. A reader whose working reference is the *installed*
path resolves a relative link such as ``docs/dev/worktree-isolation.md``
against ``~/.pi/agent/`` — where no ``docs/`` directory exists — so the link
dangles at exactly the moment the file is telling an agent it must not write
outside a worktree. The same relative link resolves fine inside this checkout,
which is why the defect survived: it was only ever exercised from the repo.

The contract these tests pin is therefore stated from the reader's side:

* no repo-relative file reference survives (every file link an agent is
  expected to follow after install is an absolute path), and
* every absolute target exists, so an absolute path is not just a syntactic
  escape hatch.

Assertions resolve links against the installed location, not against this
checkout, so a regression that reintroduces a checkout-relative link fails
here instead of in an agent's mid-run navigation.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_AGENTS_GLOBAL = _REPO_ROOT / "AGENTS_GLOBAL.md"
_WORKTREE_ISOLATION_DOC_RELATIVE = Path("docs") / "dev" / "worktree-isolation.md"

#: Where the file is read from after installation (``~/.pi/agent/AGENTS.md``).
_INSTALL_DIR = Path.home() / ".pi" / "agent"

_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")


def _lines_outside_fences() -> list[tuple[int, str]]:
    """Return ``(line_no, line)`` pairs, skipping fenced code blocks where
    link-looking text is illustrative rather than a real reference."""
    assert _AGENTS_GLOBAL.exists(), f"File not found: {_AGENTS_GLOBAL}"
    result: list[tuple[int, str]] = []
    in_fence = False
    for line_no, line in enumerate(
        _AGENTS_GLOBAL.read_text(encoding="utf-8").splitlines(), 1
    ):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence:
            result.append((line_no, line))
    return result


def _canonical_repo_root() -> Path:
    """The repository's long-lived checkout root — never a linked worktree.

    A link written into ``AGENTS_GLOBAL.md`` outlives any worktree it was
    authored in, so it must point at the canonical checkout. ``--git-common-dir``
    resolves to the main checkout's ``.git`` from inside a linked worktree
    (where ``--show-toplevel`` would wrongly answer with the worktree).
    """
    try:
        common_dir = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return _REPO_ROOT
    if not common_dir:
        return _REPO_ROOT
    return (Path(common_dir).resolve().parent)


def _is_external(target: str) -> bool:
    """A URL (``scheme:…``) or protocol-relative (``//host/…``) reference."""
    return bool(re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", target)) or target.startswith(
        "//"
    )


def _file_target(raw_target: str) -> str:
    """The file path of a link target, without an in-file ``#fragment``."""
    return raw_target.strip().split("#", 1)[0]


def _file_links() -> list[tuple[int, str]]:
    """Every ``(line_no, target)`` file link in the file, anchors and URLs
    excluded — i.e. the links an installed reader may actually follow."""
    found: list[tuple[int, str]] = []
    for line_no, line in _lines_outside_fences():
        for match in _LINK_RE.finditer(line):
            target = match.group(1).strip()
            if not target or target.startswith("#") or _is_external(target):
                continue
            found.append((line_no, target))
    return found


class TestAgentsGlobalFileLinks:
    def test_file_links_are_discovered(self) -> None:
        """Guard: the file has file links to check, so the suite is not
        vacuous after an edit that (accidentally) strips them all."""
        assert len(_file_links()) >= 3, (
            "AGENTS_GLOBAL.md should carry several file references "
            "(skill, doc and script targets); found "
            f"{[t for _, t in _file_links()]}"
        )

    def test_no_repo_relative_file_reference_survives(self) -> None:
        """AC1/AC2: a checker-relative target dangles for every installed
        reader, so only absolute paths are allowed."""
        relative = [
            (line_no, target)
            for line_no, target in _file_links()
            if not Path(_file_target(target)).is_absolute()
        ]
        assert not relative, (
            "AGENTS_GLOBAL.md must not reference repository paths relatively "
            "(they resolve to ~/.pi/agent/... for an installed reader): "
            + ", ".join(f"line {n}: {t!r}" for n, t in relative)
        )

    def test_every_absolute_target_exists(self) -> None:
        """AC1: an absolute path must name a real file, not a hopeful path."""
        missing = [
            (line_no, target)
            for line_no, target in _file_links()
            if not Path(_file_target(target)).exists()
        ]
        assert not missing, "Absolute links pointing at missing files: " + ", ".join(
            f"line {n}: {t!r}" for n, t in missing
        )

    def test_every_link_resolves_from_the_installed_location(self) -> None:
        """The regression pin for the original defect: resolve each link the
        way a reader of ``~/.pi/agent/AGENTS.md`` does. A checkout-relative
        link fails here even though it resolves inside this repository."""
        dangling: list[str] = []
        for line_no, target in _file_links():
            file_target = Path(_file_target(target))
            resolved = (
                file_target if file_target.is_absolute() else _INSTALL_DIR / file_target
            )
            if not resolved.exists():
                dangling.append(f"line {line_no}: {target!r} -> {resolved} (missing)")
        assert not dangling, (
            "Links dangle for a reader of "
            f"{_INSTALL_DIR / 'AGENTS.md'}: " + "; ".join(dangling)
        )

    def test_worktree_isolation_link_targets_this_repositorys_doc(self) -> None:
        """AC1: the worktree-isolation reference resolves to *this* repo's
        document (the comprehensive copy the producer chose as the target),
        not a relative path and not a foreign checkout's copy."""
        targets = [
            _file_target(target)
            for _, target in _file_links()
            if _file_target(target).endswith(str(_WORKTREE_ISOLATION_DOC_RELATIVE))
        ]
        assert targets, (
            "AGENTS_GLOBAL.md must link the worktree-isolation doc (see the "
            "MANDATORY worktree-requirement block)"
        )
        canonical = _canonical_repo_root() / _WORKTREE_ISOLATION_DOC_RELATIVE
        assert str(canonical) in targets, (
            "the worktree-isolation link must be the absolute path of this "
            f"repository's copy ({canonical}) — a worktree path would dangle "
            f"once that worktree is removed; found {targets}"
        )
        assert canonical.exists()
