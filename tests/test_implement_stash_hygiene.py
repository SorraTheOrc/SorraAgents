"""Tests for stash hygiene: implement.py stash warning and recovery playbook.

Verifies the fix for SA-0MT4DFE8Y004J8SP — orphaned git stashes and direct-to-dev
edits that indicated broad worktree/commit hygiene failure.

Covers:
- AC3: implement.py start warns on orphaned stashes (fail-open, --allow-orphaned-stashes to skip)
- AC4: Periodic hygiene check script exists
- AC5: Recovery playbook in implement skill + AGENTS.md pointer
- AC6: Unit tests for the guard logic

Related work item: SA-0MT4DFE8Y004J8SP
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure repo root is on path for imports
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SKILLS_ROOT = _REPO_ROOT / "skill"
sys.path.insert(0, str(_SKILLS_ROOT))

_IMPLEMENT_PY = _REPO_ROOT / "skill" / "implement" / "scripts" / "implement.py"
_SKILL_MD = _REPO_ROOT / "skill" / "implement" / "SKILL.md"
_HYGIENE_SCRIPT = _REPO_ROOT / "scripts" / "hygiene_check.sh"
_AGENTS_MD = _REPO_ROOT / "AGENTS.md"


# ===========================================================================
# Tests: check_orphaned_stashes() function (AC3)
# ===========================================================================


import importlib
import importlib.util

# Load implement.py as a module
# We need to put the scripts dir on sys.path so imports like 'from shared.status_lifecycle' work
_scripts_dir = str(_IMPLEMENT_PY.parent)
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)
# Also add skill/implement dir so 'from import_guard' works
_implement_dir = str(_IMPLEMENT_PY.parent.parent)
if _implement_dir not in sys.path:
    sys.path.insert(0, _implement_dir)
# Also add skill/shared dir
_shared_dir = str(_REPO_ROOT / "skill" / "shared")
if _shared_dir not in sys.path:
    sys.path.insert(0, _shared_dir)
# Also add skill/test_cache dir
_test_cache_dir = str(_REPO_ROOT / "skill")
if _test_cache_dir not in sys.path:
    sys.path.insert(0, _test_cache_dir)

# Import the module fresh
if "implement_module" not in sys.modules:
    _implement_spec = importlib.util.spec_from_file_location(
        "implement_module", str(_IMPLEMENT_PY),
        submodule_search_locations=[_implement_dir],
    )
    _implement_mod = importlib.util.module_from_spec(_implement_spec)
    sys.modules["implement_module"] = _implement_mod
    _implement_spec.loader.exec_module(_implement_mod)

check_orphaned_stashes = _implement_mod.check_orphaned_stashes
_extract_work_item_ids_from_stash = _implement_mod._extract_work_item_ids_from_stash


class _FakeSubprocess:
    """Replaces subprocess.run with a controllable stub.

    __call__ is used as the ``side_effect`` for ``patch.object`` so each
    call can inspect the command list and return a canned result.

    Handles worklog-dir flag that shifts command indices.
    """

    def __init__(self, stash_list_output: str = "", work_items: dict | None = None):
        self.stash_list_output = stash_list_output
        self.work_items = work_items or {}

    def __call__(self, cmd, **kwargs):
        result = MagicMock(name=f"result:{cmd}")
        result.returncode = 0
        result.stdout = ""
        if cmd[0] == "git" and len(cmd) >= 3 and cmd[1] == "stash" and cmd[2] == "list":
            result.stdout = self.stash_list_output
        elif cmd[0] == "wl" and "show" in cmd and "--json" in cmd:
            # cmd may have --worklog-dir flags inserted at position 1:1
            # e.g. ["wl", "--worklog-dir", "/path", "show", "ID", "--json"]
            # The work-item id is the first positional arg after "show"
            show_idx = cmd.index("show")
            wid = cmd[show_idx + 1] if show_idx + 1 < len(cmd) else ""
            if wid in self.work_items:
                result.stdout = json.dumps({
                    "workItem": {"id": wid, "status": self.work_items[wid]}
                })
        return result


class TestCheckOrphanedStashes:
    """Unit tests for the check_orphaned_stashes() function."""

    def _run(self, stash_list: str, work_items: dict | None = None):
        fake = _FakeSubprocess(stash_list, work_items)
        with patch.object(subprocess, "run", fake):
            return check_orphaned_stashes()

    def test_clean_stash_list_no_warning(self):
        """When there are no stashes, there should be no warning."""
        result = self._run("")
        assert result["total_stashes"] == 0
        assert result["has_orphaned"] is False
        assert result["warning"] is None

    def test_orphaned_stash_no_matching_work_item(self):
        """A stash with no work-item ID is orphaned."""
        result = self._run("stash@{0}: On dev: WIP: forgotten experiment\n")
        assert result["total_stashes"] == 1
        assert result["has_orphaned"] is True
        assert len(result["orphaned_stashes"]) == 1
        assert result["orphaned_stashes"][0]["stash_name"] == "stash@{0}"
        assert result["orphaned_stashes"][0]["matched_ids"] == []
        assert result["warning"] is not None
        assert "WARNING" in result["warning"]

    def test_orphaned_stash_with_closed_work_item(self):
        """A stash referencing a non-open work item is orphaned."""
        result = self._run(
            "stash@{0}: On dev: WIP: partial SA-0MSUT8GQP004WSYN top-level filter\n",
            {"SA-0MSUT8GQP004WSYN": "in_review"},
        )
        assert result["total_stashes"] == 1
        assert result["has_orphaned"] is True
        assert len(result["orphaned_stashes"]) == 1
        assert "SA-0MSUT8GQP004WSYN" in result["orphaned_stashes"][0]["matched_ids"]

    def test_matched_stash_no_warning(self):
        """A stash referencing an open work item is NOT orphaned."""
        result = self._run(
            "stash@{0}: On dev: WIP: partial SA-0MSUT8GQP004WSYN top-level filter\n",
            {"SA-0MSUT8GQP004WSYN": "in_progress"},
        )
        assert result["total_stashes"] == 1
        assert result["has_orphaned"] is False
        assert len(result["matched_stashes"]) == 1
        assert result["warning"] is None

    def test_mixed_stashes_correct_classification(self):
        """Mixed stash list: some matched, some orphaned."""
        result = self._run(
            "stash@{0}: On dev: WIP: partial SA-0MSUT8GQP004WSYN top-level filter\n"
            "stash@{1}: On dev: WIP: forgotten experiment\n",
            {"SA-0MSUT8GQP004WSYN": "in_progress"},
        )
        assert result["total_stashes"] == 2
        assert result["has_orphaned"] is True
        assert len(result["matched_stashes"]) == 1
        assert len(result["orphaned_stashes"]) == 1
        assert result["orphaned_stashes"][0]["stash_name"] == "stash@{1}"

    def test_multiple_orphaned_stashes(self):
        """Multiple orphaned stashes are all reported."""
        result = self._run(
            "stash@{0}: On dev: WIP: experiment A\n"
            "stash@{1}: On dev: WIP: experiment B\n"
            "stash@{2}: On dev: WIP: experiment C\n"
        )
        assert result["total_stashes"] == 3
        assert result["has_orphaned"] is True
        assert len(result["orphaned_stashes"]) == 3

    def test_stash_with_open_work_item_is_matched(self):
        """A stash with an open work-item ID is matched, not orphaned."""
        result = self._run(
            "stash@{0}: On dev: WIP: SA-0MT4DFE8Y004J8SP implementation\n",
            {"SA-0MT4DFE8Y004J8SP": "in_progress"},
        )
        assert result["has_orphaned"] is False
        assert result["matched_stashes"][0]["matched_ids"] == ["SA-0MT4DFE8Y004J8SP"]

    def test_stash_with_open_status_variants(self):
        """Stashes matching open work items (various statuses) are not orphaned."""
        for status in ("open", "in-progress", "in_progress", "blocked"):
            result = self._run(
                "stash@{0}: On dev: WIP: SA-0TEST000000000 work\n",
                {"SA-0TEST000000000": status},
            )
            assert result["has_orphaned"] is False, f"Status {status} should be open"

    def test_stash_with_closed_status_variants(self):
        """Stashes matching closed work items are orphaned."""
        for status in ("in_review", "completed", "done", "deleted"):
            result = self._run(
                "stash@{0}: On dev: WIP: SA-0TEST000000000 work\n",
                {"SA-0TEST000000000": status},
            )
            assert result["has_orphaned"] is True, f"Status {status} should be orphaned"


# ===========================================================================
# Tests: --allow-orphaned-stashes flag (AC3)
# ===========================================================================


class TestForceFlag:
    """Tests for the stash-acknowledgment flag on implement.py start.

    The flag is deliberately NOT named ``--force`` — ``--force`` is reserved
    to mean "no bypass" for the code-freeze gate (test_implement_code_freeze_gate.py).
    """

    def test_force_argument_exists(self):
        """The acknowledgment flag should exist in the argument parser."""
        mod = _implement_mod
        # Parse with the stash-acknowledgment flag
        args = mod.parse_args(["start", "SA-0TEST000000000", "--allow-orphaned-stashes"])
        assert args.allow_orphaned_stashes is True

    def test_force_default_false(self):
        """The acknowledgment flag should default to False."""
        mod = _implement_mod
        args = mod.parse_args(["start", "SA-0TEST000000000"])
        assert hasattr(args, "allow_orphaned_stashes")
        assert args.allow_orphaned_stashes is False

    def test_no_generic_force_flag_in_parser(self):
        """The parser must NOT expose a generic --force flag (freeze gate)."""
        source = _IMPLEMENT_PY.read_text(encoding="utf-8")
        parser_start = source.index("def parse_args")
        parser_end = source.index("def main")
        parser_section = source[parser_start:parser_end]
        assert '"--force"' not in parser_section and "'--force'" not in parser_section, (
            "implement.py must not expose a --force bypass flag (code-freeze gate invariant)"
        )


# ===========================================================================
# Tests: Periodic hygiene check script (AC4)
# ===========================================================================


class TestHygieneCheckScript:
    """Tests for the periodic hygiene check script (AC4)."""

    def test_hygiene_script_exists(self):
        """The hygiene check script should exist."""
        assert _HYGIENE_SCRIPT.exists(), f"Hygiene check script not found at {_HYGIENE_SCRIPT}"

    def test_hygiene_script_is_executable(self):
        """The hygiene check script should be executable."""
        assert os.access(_HYGIENE_SCRIPT, os.X_OK), (
            "hygiene_check.sh should be executable"
        )

    def test_hygiene_script_has_help_flag(self):
        """The hygiene check script should accept --help."""
        result = subprocess.run(
            ["bash", str(_HYGIENE_SCRIPT), "--help"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert result.returncode == 0
        assert "Usage" in result.stdout or "usage" in result.stdout.lower()

    def test_hygiene_script_accepts_json_flag(self):
        """The hygiene check script should accept --json."""
        # This should not crash (even if repo state isn't ideal)
        result = subprocess.run(
            ["bash", str(_HYGIENE_SCRIPT), "--json"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(_REPO_ROOT),
            check=False,
        )
        # Script may fail if not in a git repo, but should parse args
        assert result.returncode in (0, 1, 2)


# ===========================================================================
# Tests: Recovery playbook documentation (AC5)
# ===========================================================================


class TestRecoveryPlaybook:
    """Tests for the recovery playbook documentation (AC5)."""

    def test_skill_md_has_recovery_playbook_section(self):
        """The implement skill should have a recovery playbook section."""
        content = _SKILL_MD.read_text(encoding="utf-8")
        assert "## Dirty main-checkout recovery playbook" in content, (
            "Implement skill must contain a recovery playbook section"
        )

    def test_skill_md_recovery_decision_tree(self):
        """The recovery playbook should contain decision trees."""
        content = _SKILL_MD.read_text(encoding="utf-8")
        assert "Decision tree: dirty working tree" in content
        assert "Decision tree: orphaned stashes" in content

    def test_skill_md_recovery_examples(self):
        """The recovery playbook should contain recovery examples."""
        content = _SKILL_MD.read_text(encoding="utf-8")
        assert "## Dirty main-checkout recovery playbook" in content
        assert "git stash apply" in content
        assert "git stash drop" in content

    def test_skill_md_recovery_key_rules(self):
        """The recovery playbook should contain key rules."""
        content = _SKILL_MD.read_text(encoding="utf-8")
        assert "Key rules" in content
        assert "never stash" in content.lower()
        assert "without explicit permission" in content.lower()

    def test_agents_md_has_hygiene_pointer(self):
        """AGENTS.md should contain a pointer to the hygiene check."""
        content = _AGENTS_MD.read_text(encoding="utf-8")
        assert "hygiene" in content.lower() or "stash" in content.lower(), (
            "AGENTS.md should reference stash/worktree hygiene"
        )

    def test_skill_md_worktree_numbering_consistent(self):
        """After adding the stash gate, step numbers should be sequential."""
        content = _SKILL_MD.read_text(encoding="utf-8")
        # Extract top-level numbered steps (single digit only, not substeps)
        # Steps like "1. Set status" or "4. Understand" but not "4.1. Definition"
        step_nums = set()
        for line in content.splitlines():
            m = re.match(r"^(\d+)\.\s+[A-Z]", line)
            if m:
                step_nums.add(int(m.group(1)))
        # We expect 1, 2, 3, 4, 5, 6, 7, 8, 9
        expected = {1, 2, 3, 4, 5, 6, 7, 8, 9}
        for exp in expected:
            assert exp in step_nums, f"Step {exp} is missing from SKILL.md"


# ===========================================================================
# Tests: Key files updated (AC7 - documentation)
# ===========================================================================


class TestDocumentationUpdates:
    """Tests for documentation updates (AC7)."""

    def test_skill_md_mentions_stash_gate(self):
        """The implement skill should document the stash hygiene gate."""
        content = _SKILL_MD.read_text(encoding="utf-8")
        assert "Stash hygiene gate" in content
        assert "orphaned stash" in content.lower()
        assert "--allow-orphaned-stashes" in content

    def test_skill_md_mentions_hygiene_check_script(self):
        """The implement skill should reference the hygiene check script."""
        content = _SKILL_MD.read_text(encoding="utf-8")
        assert "hygiene_check" in content or "scripts/hygiene" in content

    def test_agENTS_md_mentions_hygiene_section(self):
        """AGENTS.md should have a worktree hygiene section."""
        content = _AGENTS_MD.read_text(encoding="utf-8")
        assert "worktree hygiene" in content.lower() or "hygiene check" in content.lower()


# ===========================================================================
# Tests: Integration — implement.py start flag
# ===========================================================================


class TestImplementPyStartIntegration:
    """Integration tests for implement.py start flag."""

    def test_implement_py_has_flag_in_docstring(self):
        """The implement.py module docstring should document the stash flag."""
        content = _IMPLEMENT_PY.read_text(encoding="utf-8")
        assert "--allow-orphaned-stashes" in content
