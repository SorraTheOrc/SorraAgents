"""Tests that the implement skill never stashes user changes without permission.

Verifies the fix for a real incident: during an implement run, the agent hit
the implement safety gate for uncommitted changes and ran
``git stash push`` on the USER's uncommitted, user-authored edits, never
restoring them. This test suite locks in the desired behavior:

1. The safety-gate guidance (SKILL.md Step 2) instructs agents to STOP and
   ask the operator before touching uncommitted changes, and explicitly
   forbids stashing user changes without permission.
2. The ``implement.py`` start-phase safety gate message says the same.
3. ``implement.py`` never executes ``git stash``.
4. A behavioral test runs ``implement.py start`` against a dirty repo and
   proves no stash occurs, the user's file is untouched, the run aborts with
   operator-ask guidance, and the work item is reset to ``open``.

Related work item: SA-0MSALRZ3B006FPI5
"""

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_IMPLEMENT_PY = _REPO_ROOT / "skill" / "implement" / "scripts" / "implement.py"
_SKILL_MD = _REPO_ROOT / "skill" / "implement" / "SKILL.md"


@pytest.fixture(scope="module")
def implement_mod():
    """Import ``implement.py`` as a module for the pure-logic tests below."""
    spec = importlib.util.spec_from_file_location(
        "implement_under_test_dirty_guidance", _IMPLEMENT_PY
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["implement_under_test_dirty_guidance"] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _skill_section(content: str, heading: str) -> str:
    """Extract the section starting at *heading*.

    Steps-section headings are plain numbered text (e.g. ``2. Safety gate: ...``)
    while top-level sections use markdown headings, so both forms are matched.
    """
    pattern = re.compile(
        rf"^(?:#+\s*|\d+\.\s*){re.escape(heading)}\s*$", re.MULTILINE
    )
    match = pattern.search(content)
    if not match:
        raise AssertionError(f"Section not found in SKILL.md: {heading!r}")
    rest = content[match.end():]
    # Cut at the next numbered step heading (same list level) or markdown section.
    next_heading = re.search(
        r"^(?:##\s+\S|\d+\.\s+\S)", rest, re.MULTILINE
    )
    end = match.end() + (next_heading.start() if next_heading else len(rest))
    return content[match.start():end]


def _safety_gate_section() -> str:
    """Return the 'Safety gate' section of skill/implement/SKILL.md."""
    content = _SKILL_MD.read_text(encoding="utf-8")
    return _skill_section(content, "Safety gate: handle dirty working tree")


def _implement_py_source() -> str:
    assert _IMPLEMENT_PY.exists(), f"implement.py not found at {_IMPLEMENT_PY}"
    return _IMPLEMENT_PY.read_text(encoding="utf-8")


# ===========================================================================
# Tests: SKILL.md safety-gate guidance
# ===========================================================================


class TestSkillSafetyGateGuidance:
    """skill/implement/SKILL.md Step 2 must forbid agent-initiated stashes."""

    def test_skill_md_instructs_agent_to_ask_operator(self):
        """The safety gate must tell the agent to ask the operator before
        touching uncommitted changes (not act on its own)."""
        section = _safety_gate_section()
        assert "ask the operator" in section, (
            "Step 2 safety gate must instruct agents to ask the operator "
            "before touching uncommitted changes"
        )

    def test_skill_md_forbids_stashing_user_changes(self):
        """The safety gate must explicitly forbid stashing the user's
        uncommitted changes without permission."""
        section = _safety_gate_section().lower()
        has_prohibition = any(
            phrase in section
            for phrase in (
                "never stash",
                "do not stash",
                "stash ... without explicit permission",
                "stash the user",
                "without permission",
                "forbidden",
            )
        )
        assert has_prohibition, (
            "Step 2 safety gate must explicitly forbid stashing the user's "
            "uncommitted changes without permission"
        )

    def test_skill_md_does_not_offer_stash_as_agent_action(self):
        """The safety gate must not present stash/commit/revert as a menu the
        agent may execute itself."""
        section = _safety_gate_section()
        assert "present choices: carry, commit, stash, revert, or abort" not in section, (
            "Step 2 must not present stash as an option the agent may execute; "
            "it must ask the operator instead"
        )
        assert "follow the carry/commit/stash/revert/abort prompt" not in section, (
            "Step 2 must not instruct the agent to follow a stash prompt; "
            "it must ask the operator instead"
        )

    def test_skill_md_stash_mentions_are_prohibition_or_ask_context(self):
        """Any mention of stash in the safety gate must sit near the
        operator-ask or prohibition guidance."""
        section = _safety_gate_section()
        context_phrases = (
            "ask the operator",
            "never stash",
            "do not stash",
            "without permission",
            "without asking",
            "forbidden",
            "explicit",
        )
        for match in re.finditer(r"stash", section, re.IGNORECASE):
            start = max(0, match.start() - 120)
            end = min(len(section), match.end() + 120)
            window = section[start:end].lower()
            assert any(phrase in window for phrase in context_phrases), (
                f"stash mention must be in an ask/prohibition context, got: "
                f"{section[max(0, match.start()-40):match.end()+40]!r}"
            )


# ===========================================================================
# Tests: implement.py safety-gate message
# ===========================================================================


class TestImplementPySafetyGateMessage:
    """The start-phase dirty-tree message must guide agents to ask the operator."""

    def test_message_instructs_agent_to_ask_operator(self):
        """The dirty-tree gate message must tell the agent to ask the operator."""
        source = _implement_py_source()
        assert "ask the operator" in source, (
            "implement.py safety gate message must instruct the agent to ask "
            "the operator before touching uncommitted changes"
        )

    def test_message_forbids_stashing_user_changes(self):
        """The dirty-tree gate message must explicitly forbid stashing user changes."""
        source = _implement_py_source()
        assert "Do NOT stash" in source or "never stash" in source, (
            "implement.py safety gate message must explicitly forbid stashing "
            "the user's uncommitted changes"
        )

    def test_script_never_executes_git_stash(self):
        """implement.py must never run `git stash` (no subprocess git call with
        a stash subcommand).

        Exception: READ-ONLY inspection via ``git stash list`` is allowed —
        it is the mechanism for the orphaned-stash warning gate
        (SA-0MT4DFE8Y004J8SP, AC3) and cannot modify state. State-changing
        stash commands (``git stash`` without subcommand, ``push``/``pop``/
        ``apply``/``drop``/``clear``) remain forbidden.
        """
        source = _implement_py_source()
        dangerous = (("stash", "push"), ("stash", "pop"), ("stash", "apply"),
                     ("stash", "drop"), ("stash", "clear"), ("stash", "save"))
        for line in source.splitlines():
            lower = line.lower()
            if "stash" not in lower:
                continue
            # Guidance messages are not commands.
            if "do not stash" in lower or "never stash" in lower:
                continue
            # Read-only inspection (git stash list) is allowed and required
            # by the stash-hygiene gate.
            if '"stash", "list"' in line or "'stash', 'list'" in line:
                continue
            # State-changing stash subcommands in a subprocess call are
            # forbidden (push/pop/apply/drop/clear/save).
            for cmd_word, sub in dangerous:
                if f'"{cmd_word}", "{sub}"' in line or f"'{cmd_word}', '{sub}'" in line:
                    raise AssertionError(
                        f"implement.py must never execute a state-changing git stash; "
                        f"suspicious line: {line.strip()!r}"
                    )
            # Reject bare "git stash" invocations (defaults to push).
            if re.search(r"\bgit\s+stash(?:\s|$)", lower) and "list" not in lower:
                raise AssertionError(
                    f"implement.py must never execute git stash; "
                    f"suspicious line: {line.strip()!r}"
                )


# ===========================================================================
# Tests: behavioral — implement.py start against a dirty tree
# ===========================================================================


_FAKE_WL_SRC = """\
#!/usr/bin/env python3
\"\"\"Fake wl CLI for tests: records calls and returns canned JSON.\"\"\"
import json
import os
import sys
from pathlib import Path

LOG = Path(sys.argv[0]).resolve().parent / "wl_calls.log"
with LOG.open("a") as f:
    f.write(" ".join(sys.argv[1:]) + "\\n")

args = sys.argv[1:]
# StatusLifecycle may inject --worklog-dir <path> (cwd-independent wl calls).
# Strip it so the fake parses the real subcommand.
if len(args) >= 2 and args[0] == "--worklog-dir":
    args = args[2:]
sub = args[0]
if sub == "update":
    status = "open"
    if "--status" in args:
        status = args[args.index("--status") + 1]
    print(json.dumps({"success": True, "workItem": {"id": "SA-TEST123", "status": status}}))
elif sub == "show":
    item = {"id": "SA-TEST123", "status": "open", "title": "Test"}
    desc = os.environ.get("FAKE_WL_DESCRIPTION")
    if desc:
        item["description"] = desc
    print(json.dumps({"success": True, "workItem": item}))
elif sub == "comment":
    print(json.dumps({"success": True}))
else:
    print(json.dumps({"success": False, "error": "unhandled: " + " ".join(args)}))
    sys.exit(1)
"""

_FAKE_GIT_SRC = """\
#!/usr/bin/env python3
\"\"\"Fake git wrapper: logs every invocation then delegates to real git.\"\"\"
import subprocess
import sys
from pathlib import Path

LOG = Path(sys.argv[0]).resolve().parent / "git_calls.log"
with LOG.open("a") as f:
    f.write(" ".join(sys.argv[1:]) + "\\n")

sys.exit(subprocess.call(["REAL_GIT_PATH"] + sys.argv[1:]))
"""


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def test_start_gate_aborts_without_stashing_user_changes(tmp_path: Path) -> None:
    """Running `implement.py start` against a dirty working tree must:

    - abort with success=False / dirty_worktree=True and exit code 2,
    - report guidance to ask the operator and forbid stashing,
    - never invoke `git stash`,
    - leave the user's uncommitted file byte-for-byte untouched, and
    - reset the work item status back to `open`.
    """
    # -- Build a fake git repo with the user's uncommitted work --
    repo = tmp_path / "repo"
    repo.mkdir()
    env = dict(os.environ)

    def run(cmd: list[str], cwd: Path = repo, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            cmd, cwd=str(cwd), env=env, capture_output=True, text=True,
            timeout=60, check=check,
        )

    run(["git", "init"])
    run(["git", "config", "user.email", "test@test.com"])
    run(["git", "config", "user.name", "Test"])
    (repo / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    run(["git", "add", "-A"])
    run(["git", "commit", "-m", "init"])
    run(["git", "branch", "dev"])

    # The user's uncommitted, user-authored edits (the incident scenario).
    dirty_file = repo / "shortcuts.json"
    user_content = '{\n  "shortcuts": "c/r/s/u+t"\n}\n'
    dirty_file.write_text(user_content, encoding="utf-8")

    # -- Stage fake wl + logging git wrapper in a bin dir on PATH --
    real_git = shutil.which("git")
    assert real_git, "real git not found"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(bin_dir / "wl", _FAKE_WL_SRC)
    _write_executable(bin_dir / "git", _FAKE_GIT_SRC.replace("REAL_GIT_PATH", real_git))
    env["PATH"] = f"{bin_dir}:{env['PATH']}"

    # -- Run implement.py start against the dirty repo --
    proc = subprocess.run(
        [sys.executable, str(_IMPLEMENT_PY), "start", "SA-TEST123", "--json"],
        cwd=str(repo), env=env, capture_output=True, text=True, timeout=120, check=False,
    )
    assert proc.returncode == 2, f"expected dirty-worktree abort code 2, got {proc.returncode}\n{proc.stdout}\n{proc.stderr}"
    report = json.loads(proc.stdout)
    assert report.get("success") is False
    assert report.get("dirty_worktree") is True

    msg = report.get("message", "")
    assert "ask the operator" in msg, f"message must tell agent to ask operator: {msg!r}"
    lowered = msg.lower()
    assert "stash" in lowered, f"message must address stashing: {msg!r}"
    assert any(
        phrase in lowered
        for phrase in ("do not stash", "never stash", "without permission", "forbidden")
    ), f"message must forbid stashing: {msg!r}"

    # -- The gate must never modify the stash. Read-only ``git stash list``
    #    from the stash-hygiene gate is permitted; state-changing stash
    #    commands are not (SA-0MSALRZ3B006FPI5). --
    git_log = (bin_dir / "git_calls.log").read_text(encoding="utf-8")
    for bad in (
        "stash push", "stash pop", "stash apply", "stash drop",
        "stash clear", "stash save",
    ):
        assert bad not in git_log, (
            f"state-changing {bad!r} was invoked! Calls:\n{git_log}"
        )
    assert not re.search(r"(?m)^stash\s*$", git_log), (
        f"bare git stash was invoked! Calls:\n{git_log}"
    )

    # -- The user's file must be byte-for-byte untouched --
    assert dirty_file.read_text(encoding="utf-8") == user_content, (
        "the user's uncommitted file must not be modified"
    )

    # -- The work item must be released back to open after the abort --
    wl_log = (bin_dir / "wl_calls.log").read_text(encoding="utf-8")
    last_call = wl_log.strip().splitlines()[-1]
    assert "--status open" in last_call, (
        f"work item must be reset to open after the dirty-tree abort, last wl call: {last_call!r}"
    )


# ===========================================================================
# Tests: Key Files parsing & dirty-tree relevance classification
# (SA-0MV0NSOGE000OSMR AC2/AC5)
# ===========================================================================


class TestExtractKeyFilesFromDescription:
    """The parser must handle every Key Files heading style used in practice."""

    def test_parses_hash_heading_with_predicted_suffix(self, implement_mod):
        desc = (
            "## Key Files (predicted)\n\n"
            "- `skill/implement/scripts/implement.py` — the gate\n"
            "- `tests/test_implement_start_resume.py`\n"
        )
        assert implement_mod.extract_key_files_from_description(desc) == [
            "skill/implement/scripts/implement.py",
            "tests/test_implement_start_resume.py",
        ]

    def test_parses_plan_bold_heading(self, implement_mod):
        desc = "intro\n\n**Key Files:**\n\n- `a/b.py`\n\n**Risks**\n- `ignored.py`\n"
        assert implement_mod.extract_key_files_from_description(desc) == ["a/b.py"]

    def test_returns_empty_without_section(self, implement_mod):
        assert implement_mod.extract_key_files_from_description("plain description") == []
        assert implement_mod.extract_key_files_from_description("") == []


class TestClassifyDirtyTreeRelevance:
    """The gate proceeds only when a Key Files section proves the dirt irrelevant."""

    def test_irrelevant_dirty_files_can_proceed(self, implement_mod):
        decision = implement_mod.classify_dirty_tree_relevance(
            "## dev...dev\n M standups/2026-10-10.md\n",
            "## Key Files\n\n- `skill/implement/scripts/implement.py`\n",
        )
        assert decision["dirty"] is True
        assert decision["can_proceed"] is True
        assert decision["relevant_files"] == []

    def test_relevant_dirty_file_blocks(self, implement_mod):
        decision = implement_mod.classify_dirty_tree_relevance(
            "## dev...dev\n M skill/implement/scripts/implement.py\n",
            "## Key Files\n\n- `skill/implement/scripts/implement.py`\n",
        )
        assert decision["dirty"] is True
        assert decision["can_proceed"] is False
        assert decision["relevant_files"] == ["skill/implement/scripts/implement.py"]

    def test_missing_key_files_section_stays_conservative(self, implement_mod):
        decision = implement_mod.classify_dirty_tree_relevance(
            "## dev...dev\n M standups/2026-10-10.md\n",
            "no key files section here",
        )
        assert decision["dirty"] is True
        assert decision["can_proceed"] is False

    def test_clean_tree_can_proceed(self, implement_mod):
        decision = implement_mod.classify_dirty_tree_relevance(
            "## dev...dev\n", "## Key Files\n\n- `a/b.py`\n"
        )
        assert decision["dirty"] is False
        assert decision["can_proceed"] is True

    def test_expected_dirty_paths_are_ignored(self, implement_mod):
        decision = implement_mod.classify_dirty_tree_relevance(
            "## dev...dev\n?? .llm-wiki/note.md\n",
            "no key files section",
            expected_dirty=(".llm-wiki/",),
        )
        assert decision["dirty"] is False
        assert decision["can_proceed"] is True


class TestCurrentCheckoutIsLinkedWorktree:
    """The dirty-tree gate must be skipped when invoked from a worktree."""

    def test_true_when_toplevel_has_git_file(
        self, implement_mod, tmp_path, monkeypatch
    ):
        wt = tmp_path / "wt"
        wt.mkdir()
        (wt / ".git").write_text("gitdir: /somewhere/.git/worktrees/wt\n")
        monkeypatch.setattr(
            implement_mod, "run_cmd",
            lambda *a, **k: types.SimpleNamespace(
                returncode=0, stdout=f"{wt}\n", stderr=""
            ),
        )
        assert implement_mod._current_checkout_is_linked_worktree() is True

    def test_false_for_main_checkout(
        self, implement_mod, tmp_path, monkeypatch
    ):
        main = tmp_path / "main"
        main.mkdir()
        (main / ".git").mkdir()
        monkeypatch.setattr(
            implement_mod, "run_cmd",
            lambda *a, **k: types.SimpleNamespace(
                returncode=0, stdout=f"{main}\n", stderr=""
            ),
        )
        assert implement_mod._current_checkout_is_linked_worktree() is False


# ===========================================================================
# Tests: behavioural — implement.py start against a dirty tree with
# irrelevant files (AC1/AC3/AC4/AC5a)
# ===========================================================================


def test_start_creates_clean_worktree_when_dirty_files_irrelevant(
    tmp_path: Path,
) -> None:
    """Dirty files that do not overlap the Key Files section must not block
    start: a clean worktree is created, the dirty files are left untouched,
    and the decision is recorded in a work-item comment."""
    repo = tmp_path / "repo"
    repo.mkdir()
    env = dict(os.environ)

    def run(
        cmd: list[str], cwd: Path = repo, check: bool = True
    ) -> subprocess.CompletedProcess:
        return subprocess.run(
            cmd, cwd=str(cwd), env=env, capture_output=True, text=True,
            timeout=60, check=check,
        )

    run(["git", "init"])
    run(["git", "config", "user.email", "test@test.com"])
    run(["git", "config", "user.name", "Test"])
    (repo / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    run(["git", "add", "-A"])
    run(["git", "commit", "-m", "init"])
    run(["git", "branch", "dev"])

    # A dirty file that is clearly unrelated to the Key Files below.
    dirty_dir = repo / "standups"
    dirty_dir.mkdir()
    dirty_file = dirty_dir / "2026-10-10.md"
    dirty_content = "# standup notes\n"
    dirty_file.write_text(dirty_content, encoding="utf-8")

    real_git = shutil.which("git")
    assert real_git, "real git not found"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(bin_dir / "wl", _FAKE_WL_SRC)
    _write_executable(
        bin_dir / "git", _FAKE_GIT_SRC.replace("REAL_GIT_PATH", real_git)
    )
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["FAKE_WL_DESCRIPTION"] = (
        "## Key Files (predicted)\n\n"
        "- `skill/implement/scripts/implement.py`\n"
    )

    proc = subprocess.run(
        [sys.executable, str(_IMPLEMENT_PY), "start", "SA-TEST123", "--json"],
        cwd=str(repo), env=env, capture_output=True, text=True,
        timeout=300, check=False,
    )
    assert proc.returncode == 0, (
        f"expected success, got {proc.returncode}\n{proc.stdout}\n{proc.stderr}"
    )
    report = json.loads(proc.stdout)
    assert report["success"] is True
    assert report.get("dirty_worktree") is False
    left_behind = report.get("dirty_worktree_left_behind") or []
    assert left_behind, "the left-behind dirty paths must be recorded"
    assert all(path.startswith("standups") for path in left_behind), left_behind

    # A clean worktree was created from HEAD.
    worktree = Path(report["worktree_path"])
    assert worktree.is_dir(), "worktree must be created"
    assert (worktree / ".git").is_file()

    # No state-changing stash, and the dirty file is byte-for-byte untouched.
    git_log = (bin_dir / "git_calls.log").read_text(encoding="utf-8")
    for bad in (
        "stash push", "stash pop", "stash apply", "stash drop",
        "stash clear", "stash save",
    ):
        assert bad not in git_log, (
            f"state-changing {bad!r} was invoked! Calls:\n{git_log}"
        )
    assert dirty_file.read_text(encoding="utf-8") == dirty_content

    # The decision and the left-behind path are recorded for auditability.
    wl_log = (bin_dir / "wl_calls.log").read_text(encoding="utf-8")
    assert "dirty main checkout left untouched" in wl_log
    assert "standups" in wl_log
