"""Tests for the implement skill's project-local extension support.

Contract under test (SA-0MUYEV5AO006V0HK):

- Absent extension: implement behaviour is unchanged (a no-op).
- Present extension: prose hooks are surfaced before the first actionable step
  and after the final step.
- Present extension declaring an expected-dirty path: the dirty-tree gate does
  not hard-fail on that path, while non-declared dirty paths still do.
- The worktree placement gate honours the same exemption.

The tests assert observable behaviour through the public helpers and gate
functions. Git output is supplied directly (or patched) so no repository state
is touched.
"""

from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
_SKILLS_ROOT = REPO_ROOT / "skill"
for _path in (str(REPO_ROOT), str(_SKILLS_ROOT)):
    if _path in sys.path:
        sys.path.remove(_path)
    sys.path.insert(0, _path)

from shared.skill_extensions import SkillExtensionError

from skill.implement.scripts import implement

EXTENSION_DIR = Path(".pi") / "skills_extensions" / "implement"


def _write_data(root: Path, payload: object) -> None:
    directory = root / EXTENSION_DIR
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "extension.json").write_text(json.dumps(payload), encoding="utf-8")


def _write_prose(root: Path, name: str, text: str) -> None:
    directory = root / EXTENSION_DIR
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Declaration parsing
# ---------------------------------------------------------------------------


class TestExpectedDirtyPaths:
    def test_absent_extension_is_a_noop(self, tmp_path: Path) -> None:
        assert implement.load_implement_extension(tmp_path) is None
        assert implement.expected_dirty_paths(tmp_path) == ()

    def test_declared_paths_are_returned(self, tmp_path: Path) -> None:
        _write_data(tmp_path, {"ignoreDirtyPaths": [".llm-wiki/"]})
        assert implement.expected_dirty_paths(tmp_path) == (".llm-wiki/",)

    def test_single_string_declaration_is_accepted(self, tmp_path: Path) -> None:
        _write_data(tmp_path, {"ignoreDirtyPaths": ".llm-wiki/"})
        assert implement.expected_dirty_paths(tmp_path) == (".llm-wiki/",)

    def test_missing_key_returns_empty(self, tmp_path: Path) -> None:
        _write_data(tmp_path, {"types": {"unit": ["pytest -q"]}})
        assert implement.expected_dirty_paths(tmp_path) == ()

    def test_non_list_value_raises_naming_the_file(self, tmp_path: Path) -> None:
        _write_data(tmp_path, {"ignoreDirtyPaths": {"path": ".llm-wiki/"}})
        with pytest.raises(SkillExtensionError) as excinfo:
            implement.expected_dirty_paths(tmp_path)
        assert "extension.json" in str(excinfo.value)

    def test_empty_entry_raises(self, tmp_path: Path) -> None:
        _write_data(tmp_path, {"ignoreDirtyPaths": ["  "]})
        with pytest.raises(SkillExtensionError):
            implement.expected_dirty_paths(tmp_path)

    @pytest.mark.parametrize(
        ("file_path", "expected", "result"),
        [
            (".llm-wiki/wiki/x.md", (".llm-wiki/",), True),
            (".llm-wiki/x.md", (".llm-wiki",), True),
            (".llm-wiki", (".llm-wiki/",), True),
            (".llm-wiki-other/x.md", (".llm-wiki/",), False),
            ("README.md", (".llm-wiki/",), False),
            ("README.md", (), False),
        ],
    )
    def test_prefix_matching(
        self, file_path: str, expected: tuple[str, ...], result: bool
    ) -> None:
        assert implement._path_is_expected_dirty(file_path, expected) is result


# ---------------------------------------------------------------------------
# Dirty-tree gate
# ---------------------------------------------------------------------------


class TestDirtyGate:
    def test_expected_dirty_path_is_ignored(self) -> None:
        status = "## dev\n M .llm-wiki/wiki/x.md\n"
        assert implement.git_has_dirty_files(
            status, expected_dirty=(".llm-wiki/",)
        ) is False

    def test_non_exempt_path_still_dirty(self) -> None:
        status = "## dev\n M README.md\n"
        assert implement.git_has_dirty_files(
            status, expected_dirty=(".llm-wiki/",)
        ) is True

    def test_mixed_status_is_dirty(self) -> None:
        status = "## dev\n M .llm-wiki/x.md\n?? new.py\n"
        assert implement.git_has_dirty_files(
            status, expected_dirty=(".llm-wiki/",)
        ) is True

    def test_worklog_is_still_skipped(self) -> None:
        status = "## dev\n M .worklog/worklog-data.jsonl\n"
        assert implement.git_has_dirty_files(status) is False

    def test_no_exemption_keeps_original_behaviour(self) -> None:
        status = "## dev\n M .llm-wiki/wiki/x.md\n"
        assert implement.git_has_dirty_files(status) is True


# ---------------------------------------------------------------------------
# Worktree placement gate
# ---------------------------------------------------------------------------


class TestPlacementGate:
    def test_expected_dirty_main_checkout_is_not_a_violation(
        self, tmp_path: Path
    ) -> None:
        worktree = tmp_path / "wt"
        worktree.mkdir()
        main = tmp_path / "main"
        main.mkdir()
        _write_data(main, {"ignoreDirtyPaths": [".llm-wiki/"]})

        with (
            patch.object(
                implement,
                "git_status",
                return_value="## dev\n M .llm-wiki/wiki/x.md\n",
            ),
            patch.object(implement, "_git_path_has_changes", return_value=False),
        ):
            result = implement._worktree_placement_violation(
                str(worktree), cwd=str(main), repo_root=str(main)
            )

        assert result is None

    def test_non_exempt_main_checkout_is_a_violation(self, tmp_path: Path) -> None:
        worktree = tmp_path / "wt"
        worktree.mkdir()
        main = tmp_path / "main"
        main.mkdir()
        _write_data(main, {"ignoreDirtyPaths": [".llm-wiki/"]})

        with (
            patch.object(
                implement, "git_status", return_value="## dev\n M README.md\n"
            ),
            patch.object(implement, "_git_path_has_changes", return_value=False),
        ):
            result = implement._worktree_placement_violation(
                str(worktree), cwd=str(main), repo_root=str(main)
            )

        assert result is not None
        assert "outside the worktree" in result


# ---------------------------------------------------------------------------
# Prose hooks
# ---------------------------------------------------------------------------


class TestProseHooks:
    def test_prefix_and_postfix_load(self, tmp_path: Path) -> None:
        _write_prose(tmp_path, "SKILL_PREFIX.md", "prefix guidance")
        _write_prose(tmp_path, "SKILL_POSTFIX.md", "postfix guidance")
        extension = implement.load_implement_extension(tmp_path)
        assert extension is not None
        assert extension.prefix == "prefix guidance"
        assert extension.postfix == "postfix guidance"

    def test_surface_prefix_records_and_prints(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _write_prose(tmp_path, "SKILL_PREFIX.md", "always keep the wiki dirty")
        extension = implement.load_implement_extension(tmp_path)
        report: dict = {}

        text = implement._surface_extension_prose(
            extension, "prefix", json_output=False, report=report
        )

        assert text == "always keep the wiki dirty"
        assert report["extension"]["prefix_prose"] == "always keep the wiki dirty"
        assert "always keep the wiki dirty" in capsys.readouterr().out

    def test_surface_postfix_loads_and_records(self, tmp_path: Path) -> None:
        _write_prose(tmp_path, "SKILL_POSTFIX.md", "remember to clean up")
        report: dict = {}

        text = implement._surface_postfix_prose(tmp_path, True, report)

        assert text == "remember to clean up"
        assert report["extension"]["postfix_prose"] == "remember to clean up"

    def test_absent_prose_is_a_noop(self, tmp_path: Path) -> None:
        report: dict = {}
        assert (
            implement._surface_postfix_prose(tmp_path, True, report) is None
        )
        assert "extension" not in report


# ---------------------------------------------------------------------------
# phase_start integration — prefix surfaced before the first actionable step
# ---------------------------------------------------------------------------


def _mocked_phase_start(*, update_status, repo_root: Path):
    """Patch every external boundary of ``phase_start`` for a happy path."""

    @contextlib.contextmanager
    def _ctx():
        with contextlib.ExitStack() as stack:
            stack.enter_context(
                patch.object(implement, "is_code_freeze_active", return_value=False)
            )
            stack.enter_context(
                patch.object(implement, "git_status", return_value="## dev\n")
            )
            stack.enter_context(
                patch.object(implement, "check_orphaned_stashes", return_value={
                    "total_stashes": 0,
                    "orphaned_stashes": [],
                    "matched_stashes": [],
                    "has_orphaned": False,
                    "warning": None,
                })
            )
            stack.enter_context(
                patch.object(
                    implement,
                    "wl_show",
                    return_value={
                        "id": "SA-TEST",
                        "title": "Test item",
                        "risk": "Medium",
                        "effort": "Small",
                    },
                )
            )
            stack.enter_context(
                patch.object(
                    implement,
                    "_check_and_evaluate_risk_effort",
                    return_value={"success": True, "estimates_provided": True},
                )
            )
            stack.enter_context(
                patch.object(
                    implement, "_get_repo_root", return_value=str(repo_root)
                )
            )
            stack.enter_context(
                patch.object(
                    implement,
                    "_sync_parent_branch",
                    return_value={"method": "skipped", "success": True},
                )
            )
            stack.enter_context(
                patch.object(implement, "_is_worktree", return_value=False)
            )
            stack.enter_context(
                patch.object(implement, "git_worktree_add", return_value=True)
            )
            stack.enter_context(
                patch.object(implement, "_ensure_node_modules_symlink", return_value=True)
            )
            stack.enter_context(
                patch.object(
                    implement, "_ensure_nested_node_modules_symlinks", return_value=0
                )
            )
            stack.enter_context(
                patch.object(implement, "_ensure_submodules", return_value=True)
            )
            stack.enter_context(patch.object(implement, "_store_signal_globals"))
            stack.enter_context(patch.object(implement, "_register_signal_handlers"))
            stack.enter_context(patch.object(implement, "write_state"))
            stack.enter_context(patch.object(implement, "wl_add_comment"))
            stack.enter_context(patch.object(implement, "StatusLifecycle", update_status))
            stack.enter_context(
                patch.object(
                    implement,
                    "worktree_path_for",
                    return_value=str(repo_root / "wt"),
                )
            )
            yield

    return _ctx()


def test_phase_start_surfaces_prefix_and_exempts_dirty_wiki(tmp_path: Path) -> None:
    _write_data(tmp_path, {"ignoreDirtyPaths": [".llm-wiki/"]})
    _write_prose(tmp_path, "SKILL_PREFIX.md", "leave the wiki untouched")
    lifecycle = MagicMock()

    with _mocked_phase_start(update_status=lifecycle, repo_root=tmp_path):
        report = implement.phase_start("SA-TEST", json_output=True)

    assert report["success"] is True, report
    assert report["extension"]["present"] is True
    assert report["extension"]["expected_dirty_paths"] == [".llm-wiki/"]
    assert report["extension"]["prefix_prose"] == "leave the wiki untouched"


def test_phase_start_absent_extension_is_unchanged(tmp_path: Path) -> None:
    lifecycle = MagicMock()

    with _mocked_phase_start(update_status=lifecycle, repo_root=tmp_path):
        report = implement.phase_start("SA-TEST", json_output=True)

    assert report["success"] is True, report
    assert report["extension"] == {
        "present": False,
        "expected_dirty_paths": [],
    }
    assert "prefix_prose" not in report["extension"]
