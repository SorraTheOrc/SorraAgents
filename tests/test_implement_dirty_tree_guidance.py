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

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_IMPLEMENT_PY = _REPO_ROOT / "skill" / "implement" / "scripts" / "implement.py"
_SKILL_MD = _REPO_ROOT / "skill" / "implement" / "SKILL.md"


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
        a stash subcommand)."""
        source = _implement_py_source()
        for line in source.splitlines():
            if "stash" not in line.lower():
                continue
            # Only the guidance message text may mention stash — never a command.
            assert "git" not in line or "Do NOT stash" in line or "never stash" in line, (
                f"implement.py must never execute git stash; suspicious line: {line.strip()!r}"
            )


# ===========================================================================
# Tests: behavioral — implement.py start against a dirty tree
# ===========================================================================


_FAKE_WL_SRC = """\
#!/usr/bin/env python3
\"\"\"Fake wl CLI for tests: records calls and returns canned JSON.\"\"\"
import json
import sys
from pathlib import Path

LOG = Path(sys.argv[0]).resolve().parent / "wl_calls.log"
with LOG.open("a") as f:
    f.write(" ".join(sys.argv[1:]) + "\\n")

args = sys.argv[1:]
sub = args[0]
if sub == "update":
    status = "open"
    if "--status" in args:
        status = args[args.index("--status") + 1]
    print(json.dumps({"success": True, "workItem": {"id": "SA-TEST123", "status": status}}))
elif sub == "show":
    print(json.dumps({"success": True, "workItem": {"id": "SA-TEST123", "status": "open", "title": "Test"}}))
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

    # -- The gate must never have invoked git stash --
    git_log = (bin_dir / "git_calls.log").read_text(encoding="utf-8")
    assert "stash" not in git_log, f"git stash was invoked! Calls:\n{git_log}"

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
