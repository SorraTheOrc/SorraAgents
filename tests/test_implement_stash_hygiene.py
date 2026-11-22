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
for _path in (str(_REPO_ROOT), str(_SKILLS_ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from skill.shared import git_sandbox

_IMPLEMENT_PY = _REPO_ROOT / "skill" / "implement" / "scripts" / "implement.py"
_SKILL_MD = _REPO_ROOT / "skill" / "implement" / "SKILL.md"
_REFERENCE_MD = _REPO_ROOT / "docs" / "dev" / "implement-skill-reference.md"
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

    def test_hygiene_script_accepts_json_flag(self, tmp_path: Path):
        """The hygiene check script should accept --json.

        Run against a bounded, seeded repository (never the live checkout) so
        the result depends on the flag contract alone — not on this checkout's
        stash/worktree count or on host load (SA-0MWRWM2ON008Z5YI). The
        ``--json`` contract is one JSON object per non-blank line, each
        carrying a ``check`` key.
        """
        repo = _seed_hygiene_repo(tmp_path / "repo")
        bin_dir = _fake_wl(tmp_path / "bin", {})

        result = _run_hygiene(repo, "--json", bin_dir=bin_dir)

        # The script may warn (exit 1) or error (exit 2), but must parse args.
        assert result.returncode in (0, 1, 2)
        payloads = [
            json.loads(line) for line in result.stdout.splitlines() if line.strip()
        ]
        assert payloads, "expected at least one JSON line from --json output"
        for payload in payloads:
            assert isinstance(payload, dict), payload
            assert "check" in payload, payload


# ===========================================================================
# Tests: Recovery playbook documentation (AC5)
# ===========================================================================


class TestRecoveryPlaybook:
    """Tests for the recovery playbook documentation (AC5).

    The playbook was relocated verbatim from ``skill/implement/SKILL.md`` to
    ``docs/dev/implement-skill-reference.md`` (SA-0MVYDCUWA001Z8K0, AC5).
    """

    def test_skill_md_has_recovery_playbook_section(self):
        """The implement reference should have a recovery playbook section."""
        content = _REFERENCE_MD.read_text(encoding="utf-8")
        assert "## Dirty main-checkout recovery playbook" in content, (
            "Implement reference must contain a recovery playbook section"
        )

    def test_skill_md_recovery_decision_tree(self):
        """The recovery playbook should contain decision trees."""
        content = _REFERENCE_MD.read_text(encoding="utf-8")
        assert "Decision tree: dirty working tree" in content
        assert "Decision tree: orphaned stashes" in content

    def test_skill_md_recovery_examples(self):
        """The recovery playbook should contain recovery examples."""
        content = _REFERENCE_MD.read_text(encoding="utf-8")
        assert "## Dirty main-checkout recovery playbook" in content
        assert "git stash apply" in content
        assert "git stash drop" in content

    def test_skill_md_recovery_key_rules(self):
        """The recovery playbook should contain key rules."""
        content = _REFERENCE_MD.read_text(encoding="utf-8")
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


# ===========================================================================
# Tests: Debris worktree reports (WL-0MWQLHDOS006CAIH AC3)
# ===========================================================================
#
# The hygiene path is where an operator learns *which* stale trees to remove.
# Until this item it only reported worktrees whose branch had been deleted, so
# the three ``/tmp/wl-*`` scratch trees and the leftover tree of the already
# ``deleted`` item that refused a clean epic child were invisible here.
#
# Contract: `scripts/hygiene_check.sh` reports, in text *and* `--json`,
#   (a) registered worktrees outside the repository's worktree root
#       (``.worklog/worktrees/``), and
#   (b) worktrees whose owning work item is terminal,
# and prints the prune remedy ``git worktree remove <path> && git worktree
# prune``. Reporting is read-only (nothing is ever pruned) and fail-open (an
# unresolvable owner or a missing ``wl`` reports nothing rather than guessing).
# Existing checks, their JSON entries and the exit-code contract are unchanged.


def _seed_hygiene_repo(root: Path) -> Path:
    """A real repository on ``dev`` with one commit and no stashes."""
    git_sandbox.init_repo(root)
    (root / ".gitignore").write_text(".worklog/\n", encoding="utf-8")
    (root / "README.md").write_text("base\n", encoding="utf-8")
    git_sandbox.commit_all(root, "base")
    return root


def _add_hygiene_worktree(repo: Path, path: Path, name: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    return git_sandbox.add_worktree(repo, path, name, base="dev")


def _fake_wl(bin_dir: Path, items: dict[str, dict]) -> Path:
    """Install a ``wl`` stub that resolves the given work items."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "wl"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"ITEMS = {items!r}\n"
        "args = [a for a in sys.argv[1:] if not a.startswith('--')]\n"
        "if 'show' not in args:\n"
        "    print('{}')\n"
        "    raise SystemExit(0)\n"
        "wid = args[args.index('show') + 1]\n"
        "item = ITEMS.get(wid)\n"
        "if item is None:\n"
        "    print(json.dumps({'success': False, 'error': 'not found'}))\n"
        "    raise SystemExit(1)\n"
        "print(json.dumps({'success': True, 'workItem': item}))\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return bin_dir


def _run_hygiene(
    repo: Path, *args: str, bin_dir: Path | None = None
) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    if bin_dir is not None:
        env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    return subprocess.run(
        ["bash", str(_HYGIENE_SCRIPT), "--repo-root", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        check=False,
    )


def _json_checks(stdout: str) -> dict[str, list[dict]]:
    """Group the script's one-JSON-object-per-line output by check name."""
    grouped: dict[str, list[dict]] = {}
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        payload = json.loads(line)
        grouped.setdefault(payload.get("check", ""), []).append(payload)
    return grouped


class TestDebrisWorktreeReports:
    """AC3: outside-root and terminal-owner worktrees, plus the remedy."""

    def test_text_report_names_both_debris_classes_and_the_remedy(
        self, tmp_path: Path
    ) -> None:
        repo = _seed_hygiene_repo(tmp_path / "repo")
        outside = _add_hygiene_worktree(
            repo, tmp_path / "scratch" / "wl-SA-OUTSIDE00001", "wl-SA-OUTSIDE00001"
        )
        (outside / "scratch-marker.js").write_text("x\n", encoding="utf-8")
        leftover = _add_hygiene_worktree(
            repo,
            repo / ".worklog" / "worktrees" / "wl-SA-TERMINAL0001-slug",
            "wl-SA-TERMINAL0001-slug",
        )
        (leftover / "index.ts").write_text("x\n", encoding="utf-8")
        bin_dir = _fake_wl(
            tmp_path / "bin",
            {
                "SA-TERMINAL0001": {
                    "id": "SA-TERMINAL0001",
                    "title": "Finished",
                    "status": "completed",
                }
            },
        )

        result = _run_hygiene(repo, bin_dir=bin_dir)

        assert str(outside) in result.stdout
        assert str(leftover) in result.stdout
        assert "owning work item" in result.stdout
        assert "SA-TERMINAL0001" in result.stdout
        assert "git worktree remove" in result.stdout
        assert "git worktree prune" in result.stdout
        # Reporting only: nothing was pruned.
        assert outside.exists()
        assert leftover.exists()

    def test_json_report_is_additive_and_machine_readable(
        self, tmp_path: Path
    ) -> None:
        repo = _seed_hygiene_repo(tmp_path / "repo")
        outside = _add_hygiene_worktree(
            repo, tmp_path / "scratch" / "wl-SA-OUTSIDE00001", "wl-SA-OUTSIDE00001"
        )
        (outside / "scratch-marker.js").write_text("x\n", encoding="utf-8")
        leftover = _add_hygiene_worktree(
            repo,
            repo / ".worklog" / "worktrees" / "wl-SA-TERMINAL0001-slug",
            "wl-SA-TERMINAL0001-slug",
        )
        bin_dir = _fake_wl(
            tmp_path / "bin",
            {
                "SA-TERMINAL0001": {
                    "id": "SA-TERMINAL0001",
                    "title": "Finished",
                    "status": "deleted",
                }
            },
        )

        result = _run_hygiene(repo, "--json", bin_dir=bin_dir)

        checks = _json_checks(result.stdout)
        debris = checks["debris_worktrees"][0]
        assert debris["status"] == "notice"
        assert [entry["path"] for entry in debris["outside_root"]] == [str(outside)]
        assert [entry["path"] for entry in debris["terminal_owner"]] == [
            str(leftover)
        ]
        assert debris["terminal_owner"][0]["work_item"] == "SA-TERMINAL0001"
        assert debris["terminal_owner"][0]["status"] == "deleted"
        assert "git worktree remove" in debris["remedy"]
        # The pre-existing checks keep their entries.
        for name in (
            "orphaned_stashes",
            "dirty_checkout",
            "orphaned_worktrees",
            "main_checkout_lock",
            "tracked_deletions",
            "self_referential_upstream",
        ):
            assert name in checks, name

    def test_clean_checkout_reports_no_debris(self, tmp_path: Path) -> None:
        repo = _seed_hygiene_repo(tmp_path / "repo")
        bin_dir = _fake_wl(tmp_path / "bin", {})

        result = _run_hygiene(repo, "--json", bin_dir=bin_dir)

        debris = _json_checks(result.stdout)["debris_worktrees"][0]
        assert debris["status"] == "ok"
        assert result.returncode == 0

    def test_a_live_inside_root_worktree_is_not_debris(self, tmp_path: Path) -> None:
        repo = _seed_hygiene_repo(tmp_path / "repo")
        _add_hygiene_worktree(
            repo,
            repo / ".worklog" / "worktrees" / "wl-SA-LIVEITEM0001-slug",
            "wl-SA-LIVEITEM0001-slug",
        )
        bin_dir = _fake_wl(
            tmp_path / "bin",
            {
                "SA-LIVEITEM0001": {
                    "id": "SA-LIVEITEM0001",
                    "title": "Live",
                    "status": "in-progress",
                }
            },
        )

        result = _run_hygiene(repo, "--json", bin_dir=bin_dir)

        debris = _json_checks(result.stdout)["debris_worktrees"][0]
        assert debris["status"] == "ok"

    def test_an_unresolvable_owner_is_never_guessed(self, tmp_path: Path) -> None:
        """Fail-open: no ``wl`` answer means no terminal claim."""
        repo = _seed_hygiene_repo(tmp_path / "repo")
        _add_hygiene_worktree(
            repo,
            repo / ".worklog" / "worktrees" / "wl-SA-UNKNOWN00001-slug",
            "wl-SA-UNKNOWN00001-slug",
        )
        bin_dir = _fake_wl(tmp_path / "bin", {})

        result = _run_hygiene(repo, "--json", bin_dir=bin_dir)

        debris = _json_checks(result.stdout)["debris_worktrees"][0]
        assert debris["status"] == "ok"
        assert debris["terminal_owner"] == []

    def test_debris_is_reported_without_changing_the_exit_code(
        self, tmp_path: Path
    ) -> None:
        """The finding is a notice: existing exit codes stay authoritative.

        A checkout may legitimately carry a registered tree outside the
        worktree root (a manual experiment), so debris never escalates the
        script's exit code on its own.
        """
        clean_repo = _seed_hygiene_repo(tmp_path / "clean-repo")
        clean = _run_hygiene(
            clean_repo, bin_dir=_fake_wl(tmp_path / "bin-clean", {})
        )
        assert clean.returncode == 0

        debris_repo = _seed_hygiene_repo(tmp_path / "debris-repo")
        _add_hygiene_worktree(
            debris_repo,
            tmp_path / "scratch" / "wl-SA-OUTSIDE00001",
            "wl-SA-OUTSIDE00001",
        )
        debris = _run_hygiene(
            debris_repo, bin_dir=_fake_wl(tmp_path / "bin-debris", {})
        )
        assert debris.returncode == 0
        assert "git worktree remove" in debris.stdout
        assert "Hygiene check completed with warnings" not in debris.stdout

        # An existing warning still governs the exit code.
        (debris_repo / "README.md").write_text("dirty\n", encoding="utf-8")
        dirty = _run_hygiene(
            debris_repo, bin_dir=_fake_wl(tmp_path / "bin-dirty", {})
        )
        assert dirty.returncode == 1
        assert "Hygiene check completed with warnings" in dirty.stdout
