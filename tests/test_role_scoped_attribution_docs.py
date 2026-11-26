"""Doc-contract tests for the role-scoped working-tree verdict (WL-0MWR1P26Y004CYE9).

The implementation siblings (WL-0MWR1OXHX001KJC6, WL-0MWR1P1F3008AH3V) narrow
the live-repo guard's **main-checkout** working-tree verdict to tracked-file
deletions, while the **guarded root** keeps full clean→dirty sensitivity. The
deliverable of this item is the documentation that must move with that rule, so
these tests pin the documented contract in the established style of
``tests/test_test_isolation_launcher_docs.py`` and
``tests/test_worktree_isolation_docs.py``:

* ``docs/dev/test-isolation.md`` §8.5's rule table is role-scoped, its blind
  spot #2 states exactly what the narrowing no longer catches and names the
  remaining nets plus the long-term isolated-worktree alternative, and the
  ``excluded.status`` JSON shape (``removed`` / ``narrowed``) and its note
  rendering are documented;
* ``docs/dev/implement-skill-reference.md`` documents what a finish-time
  excluded note means, keeps the ``unknown`` actor fallback, and restates that
  a genuine ``live_repo_mutation`` is still an infrastructure failure;
* the module docstrings of ``git_sandbox``, ``live_repo_guard`` and
  ``run_tests`` state the role-scoped rule;
* no document still claims the clean→dirty direction is unconditionally
  retained;
* the characterisation pins the documentation names exist in the test module.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ISOLATION_DOC = _REPO_ROOT / "docs" / "dev" / "test-isolation.md"
_IMPLEMENT_DOC = _REPO_ROOT / "docs" / "dev" / "implement-skill-reference.md"
_GIT_SANDBOX_TESTS = _REPO_ROOT / "skill" / "shared" / "tests" / "test_git_sandbox.py"

#: Test modules §8.5 names regression pins in (class names resolve across them).
_PIN_MODULES = (
    _GIT_SANDBOX_TESTS,
    _REPO_ROOT / "tests" / "test_live_repo_mutation_guard.py",
    _REPO_ROOT / "skill" / "test" / "tests" / "test_guarded_run_all.py",
)

_MODULE_PATHS = {
    "git_sandbox": _REPO_ROOT / "skill" / "shared" / "git_sandbox.py",
    "live_repo_guard": _REPO_ROOT / "skill" / "shared" / "live_repo_guard.py",
    "run_tests": _REPO_ROOT / "skill" / "test" / "scripts" / "run_tests.py",
}

#: Sentences that would re-assert the pre-narrowing rule unconditionally.
_STALE_RETENTION_CLAIMS = (
    "retained clean→dirty false positive",
    "clean→dirty damage is suite-caused",
    "deliberately does not touch the incident direction",
    "a path that becomes dirty from a clean start is still a mutation",
)


def _normalise(text: str) -> str:
    """Collapse whitespace so markdown wrapping never affects the contract."""
    return " ".join(text.split())


def _section(path: Path, start_marker: str, end_marker: str) -> str:
    """Normalised markdown from *start_marker* up to (excluding) *end_marker*."""
    content = path.read_text(encoding="utf-8")
    start = content.find(start_marker)
    assert start != -1, f"{path.name} must contain {start_marker!r}"
    end = content.find(end_marker, start + len(start_marker))
    assert end != -1 and end > start, (
        f"{path.name}: {start_marker!r} must be followed by {end_marker!r}"
    )
    return _normalise(content[start:end])


def _section_8_5() -> str:
    return _section(
        _ISOLATION_DOC,
        "### 8.5 Working-tree attribution",
        "### 8.6 Own-checkout state surface",
    )


def _section_8_6() -> str:
    return _section(
        _ISOLATION_DOC,
        "### 8.6 Own-checkout state surface",
        "## 9. ",
    )


def _guard_ownership_section() -> str:
    return _section(
        _IMPLEMENT_DOC,
        "## Live-repo guard ownership and main-checkout coverage",
        "## Finish main-checkout guard",
    )


def _actor_evidence_section() -> str:
    return _section(
        _IMPLEMENT_DOC,
        "### Concurrent-actor evidence (second signal)",
        "### Fail-closed `repo_root` resolution",
    )


def _module_docstring(path: Path) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstring = ast.get_docstring(tree) or ""
    assert docstring, f"{path.name} must keep a module docstring"
    return _normalise(docstring)


def _defined_test_names() -> set[str]:
    tree = ast.parse(_GIT_SANDBOX_TESTS.read_text(encoding="utf-8"))
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    }


def _defined_class_names() -> set[str]:
    names: set[str] = set()
    for path in _PIN_MODULES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names.update(
            node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)
        )
    return names


class TestSection85RuleIsRoleScoped:
    """§8.5's rule table distinguishes the guarded root from the main checkout."""

    def test_rule_table_has_both_roles(self) -> None:
        section = _section_8_5()
        assert "Guarded root" in section
        assert "Main checkout" in section

    def test_main_checkout_narrows_to_tracked_deletions(self) -> None:
        section = _section_8_5()
        assert "tracked-file deletion" in section
        assert "packages/shared" in section

    def test_guarded_root_keeps_full_sensitivity(self) -> None:
        section = _section_8_5()
        assert "guarded root" in section.lower()
        assert "full" in section.lower()


class TestSection85DocumentsTheExcludedBucket:
    """The ``excluded.status`` JSON contract and its note rendering are auditable."""

    def test_names_both_excluded_status_buckets(self) -> None:
        section = _section_8_5()
        assert "excluded.status" in section
        assert "excluded.status.removed" in section
        assert "excluded.status.narrowed" in section

    def test_names_note_renderer_and_json_surface(self) -> None:
        section = _section_8_5()
        assert "EXCLUDED_CHURN_NOTE_PREFIX" in section
        assert "excluded_working_tree_churn" in section
        assert "live_repo_excluded" in section

    def test_explains_the_note_is_never_silent(self) -> None:
        assert "never silently dropped" in _section_8_5().lower()


class TestSection85BlindSpot2IsRewritten:
    """Blind spot #2 states the narrowed surface, the nets and the alternative."""

    def test_states_what_the_narrowing_no_longer_catches(self) -> None:
        section = _section_8_5().lower()
        assert "blind spot" in section
        assert "modifies or adds" in section

    def test_names_the_remaining_nets(self) -> None:
        section = _section_8_5()
        assert "WL-0MVPADZ1Z000FOSG" in section
        assert "guarded root" in section.lower()

    def test_names_the_long_term_isolated_worktree_alternative(self) -> None:
        assert "WL-0MW4W15U500009D2" in _section_8_5()

    def test_no_sentence_claims_unconditional_retention(self) -> None:
        section = _section_8_5().lower()
        for stale in _STALE_RETENTION_CLAIMS:
            assert stale not in section, f"§8.5 still claims: {stale!r}"


class TestSection85NamesThePins:
    """The regression-pin list names the role-scoped and the retained pins."""

    def test_names_role_scoped_pin_class(self) -> None:
        assert "TestMainCheckoutRoleScopedVerdict" in _section_8_5()

    def test_named_pins_exist_in_the_test_modules(self) -> None:
        section = _section_8_5()
        defined_functions = _defined_test_names()
        defined_classes = _defined_class_names()
        for name in re.findall(r"``(Test[A-Za-z0-9_]+)``", section):
            assert name in defined_classes, f"§8.5 names a missing pin class: {name}"
        for name in re.findall(r"``(test_[a-z0-9_]+)``", section):
            assert name in defined_functions, f"§8.5 names a missing pin: {name}"


class TestSection86KeepsTheGuardedRootPin:
    """The own-checkout surface is guarded-root-only, and its pin is named."""

    def test_names_the_renamed_clean_to_dirty_pin(self) -> None:
        assert "test_clean_to_dirty_on_the_guarded_root_is_a_move" in _section_8_6()

    def test_renamed_pin_exists(self) -> None:
        assert "test_clean_to_dirty_on_the_guarded_root_is_a_move" in (
            _defined_test_names()
        )


class TestImplementReferenceDocumentsFinishNotes:
    """The finish reference explains an excluded note and keeps the verdict honest."""

    def test_documents_finish_time_excluded_note(self) -> None:
        section = _guard_ownership_section()
        assert "excluded note" in section.lower() or "excluded-churn note" in section.lower()
        assert "does not fail the run" in section.lower()

    def test_documents_actor_sources_and_unknown_fallback(self) -> None:
        section = _actor_evidence_section()
        assert "`.implement_state.json`" in section
        assert "unknown" in section

    def test_restates_infrastructure_classification(self) -> None:
        section = _guard_ownership_section()
        assert "live_repo_mutation" in section
        assert "GATE_FAILURE_INFRASTRUCTURE" in section

    def test_no_unconditional_retention_claim(self) -> None:
        for section in (_guard_ownership_section(), _actor_evidence_section()):
            lowered = section.lower()
            for stale in _STALE_RETENTION_CLAIMS:
                assert stale not in lowered, f"reference still claims: {stale!r}"


class TestModuleDocstringsStateTheRule:
    """The three modules the guard lives in state the role-scoped rule."""

    def test_each_module_states_role_scoping(self) -> None:
        for name, path in _MODULE_PATHS.items():
            docstring = _module_docstring(path)
            assert "role-scoped" in docstring.lower(), f"{name} lacks the rule"
            assert "tracked-file deletion" in docstring.lower(), (
                f"{name} lacks the deletion narrowing"
            )

    def test_each_module_keeps_full_guarded_root_sensitivity(self) -> None:
        for name, path in _MODULE_PATHS.items():
            docstring = _module_docstring(path).lower()
            assert "guarded root" in docstring, f"{name} must name the guarded root"

    def test_no_module_docstring_claims_unconditional_retention(self) -> None:
        for name, path in _MODULE_PATHS.items():
            docstring = _module_docstring(path).lower()
            assert "deliberately retained" not in docstring, name
            assert "deliberately retains" not in docstring, name
