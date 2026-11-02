"""Docs-vs-source consistency for the self-referential-upstream section
(SA-0MVX35BDU003Q8X9 — child 6 of SA-0MVX1M6HH004YPX2).

``docs/dev/worktree-isolation.md`` tells an operator how to detect, repair and
verify the self-referential-upstream defect. Every command it shows must
exist in the tool it names, or the guidance silently rots. This test pins
both directions:

* the documented operator-facing facts are present in the section (the
  contract under test is the wording an operator reads), and
* the ``--fix`` / ``--dry-run`` / ``--repo-root`` / ``--json`` flags the docs
  show are real options of ``skill/shared/self_referential_upstream.py`` (the
  module's own argument parser is asked, not the source text).
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
_SKILL_ROOT = REPO_ROOT / "skill"
for _path in (str(REPO_ROOT), str(_SKILL_ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from shared import self_referential_upstream as sru

DOC = REPO_ROOT / "docs" / "dev" / "worktree-isolation.md"
SECTION_HEADING = "## A branch upstream that points at itself breaks `git pull`"


def _section() -> str:
    """The documentation section under test (heading → next ``## ``)."""
    text = DOC.read_text(encoding="utf-8")
    start = text.index(SECTION_HEADING)
    rest = text[start + len(SECTION_HEADING) :]
    end = rest.find("\n## ")
    return rest if end == -1 else rest[:end]


def test_section_exists_in_worktree_isolation_docs():
    """The canonical location carries the section (parent AC6)."""
    assert SECTION_HEADING in DOC.read_text(encoding="utf-8")


def test_section_states_the_shared_key_and_the_ownership_signal():
    """The three facts an operator must know before touching the config."""
    section = _section()
    assert "one per branch name" in section, (
        "the section must state that branch tracking keys are shared per BRANCH NAME"
    )
    assert "worktree-ownership signal" in section
    assert "survives `git worktree remove`" in section
    assert "silent" in section, "the no-op pull failure mode must be called out"


def test_section_names_both_write_paths_and_this_work_item():
    """Mechanism: ``worktree add --track`` and ``git push -u .`` (parent AC6)."""
    section = _section()
    assert "git worktree add --track" in section
    assert "git push -u . dev:dev" in section
    assert "SA-0MVX1M6HH004YPX2" in section, "cross-reference the epic"


def test_section_names_the_detection_surfaces_and_repair_command():
    """Detection (hygiene + start) and repair (``--fix``) are documented."""
    section = _section()
    assert "hygiene_check.sh" in section
    assert "implement.py start" in section
    assert "self_referential_upstream.py --fix" in section
    # and the verification steps
    assert "git config --get branch.dev.remote" in section
    assert "git log origin/dev..dev" in section


def test_documented_cli_flags_exist_in_the_module():
    """Doc-vs-source: every flag the section shows is accepted by the CLI.

    Behavioural check (not source text): run the documented combinations and
    assert argparse does not reject them. ``--dry-run`` makes the repair
    invocation read-only, so the check against the real repository is safe.
    """
    import subprocess

    def run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                sys.executable,
                str(Path(sru.__file__)),
                "--repo-root",
                str(REPO_ROOT),
                *args,
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    detect = run("--json")
    assert "unrecognized arguments" not in detect.stderr
    assert detect.returncode in (0, 1), detect.stderr  # 0 clean, 1 defects

    dry_run = run("--fix", "--dry-run", "--json")
    assert "unrecognized arguments" not in dry_run.stderr
    assert dry_run.returncode in (0, 1, 2), dry_run.stderr


def test_documented_module_path_is_the_real_module():
    """The docs' module path resolves to the implementation under test."""
    documented = REPO_ROOT / "skill" / "shared" / "self_referential_upstream.py"
    assert documented == Path(sru.__file__).resolve()
