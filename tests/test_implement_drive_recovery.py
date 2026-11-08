
# <!-- REFACTOR-SA-0MVYYNOK4008GKAL
# smell: ruff_specific
# severity: medium
# description: Prefer `next(...)` over single element slice
# -->

"""Drive-recovery test harness + terminal-child regression
(SA-0MVLJXKB60088SND, parent SA-0MVLIZ33M009C5QY).

Scope of this module
--------------------

This is the *test harness* slice of the drive bounded-recovery work. It adds
no recovery behaviour itself; it establishes the fixtures the sibling feature
items build on, plus the regression guard that must stay green through every
one of them:

- **Harness (AC1/AC2).** :class:`DriveHarness` drives ``phase_drive`` with an
  injectable ``parent_runner`` / ``spawn_session``, patched worklog plumbing,
  and recorders for every mutation a recovery path may perform (lifecycle
  status updates, worklog comments, the worktree reset helper). The
  :func:`finishing_failed_report` helper scripts the exact
  ``phase_parent`` report shape emitted by ``_run_finish``
  (``success: False``, ``finishing_failed: True``, ``next_child``,
  ``message``) — the livelock entry point.
- **Terminal-child regression (AC3/parent AC5).** A child already terminal
  (``completed``/``done``/``in_review`` status, an ``in_review`` stage, or
  soft-deleted via ``deletedBy``) is never finished, reset or re-spawned by
  ``phase_drive``, and the ``skip-terminal`` precedence in
  :func:`_classify_child` is preserved.

The fixtures intentionally mirror — and reuse — the ``ScriptedParent`` /
``RecordingSpawner`` pattern and the ``implement_mod`` fixture from
``tests/test_implement_drive.py`` rather than inventing a second style.

Future sibling work in this file: bounded finish attempts, finish-failure
terminal reconciliation, and non-terminal reset. The harness exposes
:meth:`DriveHarness.patch` and :meth:`DriveHarness.record_reset_helper` so
those tests can inject not-yet-existing recovery helpers (``create=True``)
without touching production code from this item.
"""

from __future__ import annotations

import contextlib
import importlib.util
import re
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from shared.git_sandbox import run_git

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_IMPLEMENT_PY = _REPO_ROOT / "skill" / "implement" / "scripts" / "implement.py"

#: Lazily-loaded production implement module used to compute the
#: ``finish_failure`` payload with the *production* classifier (see
#: :func:`_production_implement_mod`).
_PRODUCTION_IMPLEMENT_MOD: Any = None

# Reuse the existing drive-loop harness pattern verbatim: the scripted parent
# runner, the recording session spawner, the child-report helper and the
# module-under-test fixture all come from ``tests/test_implement_drive.py``.
from tests.test_implement_drive import (
    PARENT,
    RecordingSpawner,
    ScriptedParent,
    implement_mod,  # noqa: F401 — re-exported as a pytest fixture
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _neutralise_ambient_drive_guard(monkeypatch):
    """Make this module hermetic when pytest runs inside a driven session.

    ``implement.py finish`` runs the changed-scope suite with
    ``IMPLEMENT_DRIVE_ACTIVE=1`` exported by the driving parent. Every
    ``phase_drive`` test here would otherwise hit the recursion guard and
    fail the run (same hazard as SA-0MV8HEQ4O009Y3UB, pinned in
    ``tests/test_implement_drive.py``).
    """
    monkeypatch.delenv("IMPLEMENT_DRIVE_ACTIVE", raising=False)


def _child(cid: str, **overrides) -> dict:
    """Build a child work-item dict with the fields the loop inspects."""
    child = {
        "id": cid,
        "title": f"Child {cid}",
        "status": "open",
        "stage": "intake_complete",
        "deletedBy": "",
        "assignee": "",
    }
    child.update(overrides)
    return child


def _production_implement_mod() -> Any:
    """Load (once) the production implement module for the classifier.

    The harness scripts parent reports carrying the exact ``finish_failure``
    payload ``phase_parent._run_finish`` emits — which is
    ``classify_finish_failure(parsed)`` over the child's failing ``finish``
    subprocess report (LP-0MVWEP1MM0097RAT). The payload must therefore be
    produced by the *production* classifier, loaded by file path under its
    own module name. The ``sys.modules`` registration happens before
    ``exec_module`` — required by the module's dataclass forward references,
    mirroring the ``implement_mod`` fixture in ``tests/test_implement_drive.py``.
    """
    global _PRODUCTION_IMPLEMENT_MOD
    if _PRODUCTION_IMPLEMENT_MOD is None:
        spec = importlib.util.spec_from_file_location(
            "implement_under_test_recovery", _IMPLEMENT_PY
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        _PRODUCTION_IMPLEMENT_MOD = mod
    return _PRODUCTION_IMPLEMENT_MOD


def finishing_failed_report(
    child_id: str,
    message: str | None = None,
    *,
    step: str | None = None,
    reason: str | None = None,
    kind: str | None = None,
    lock_holder: str | None = None,
    guard_verifies: list | None = None,
) -> dict:
    """Script the exact ``phase_parent`` report shape ``_run_finish`` emits.

    When ``implement.py finish <child>`` fails inside ``phase_parent``,
    ``_run_finish`` sets ``success: False``, ``finishing_failed: True``,
    ``next_child`` (the child whose finish failed) and ``message``. That is
    the livelock entry point: today ``phase_drive`` breaks the chain on any
    ``success: False`` report without accounting for or recovering from it.

    When *step* is given the report additionally carries ``finish_failure`` —
    byte-for-byte the payload ``_run_finish`` would emit, i.e. produced by
    the production :func:`classify_finish_failure` over a failing ``finish``
    report scripted by :func:`failing_finish_report`. *kind*, when given,
    must match the classifier's derived kind for the scripted pair, so a
    scripted report can never silently claim the wrong failure class (a
    mismatch raises).

    Args:
        child_id: The child whose finish supposedly failed.
        message: Optional failure message; defaults to the production text.
        step: Optional failing ``finish`` step to script (e.g.
            ``"push"``); when given, ``finish_failure`` is added to the
            report. Both *reason* and *kind* are only meaningful with it.
        reason: Optional reason code for the step (e.g. ``"timeout"``).
        kind: Optional expected failure kind; must equal the production
            classifier's derivation for ``(step, reason)``.

    Returns:
        A report dict accepted by :class:`ScriptedParent`. Called with no
        extra arguments the returned dict has byte-identical keys to the
        pre-extension helper (AC1 — backwards compatible, existing callers
        unchanged).
    """
    if kind is not None and step is None:
        raise ValueError(
            "finishing_failed_report: kind requires a scripted step/reason "
            "to derive the payload"
        )
    report = {
        "phase": "parent",
        "work_item_id": PARENT,
        "success": False,
        "message": message or f"Failed to finish child {child_id}",
        "next_child": child_id,
        "finishing_failed": True,
        "children_processed": [],
    }
    if step is not None:
        finish_report = failing_finish_report(
            step, reason, lock_holder=lock_holder,
            guard_verifies=guard_verifies,
        )
        payload = _production_implement_mod().classify_finish_failure(finish_report)
        if kind is not None and payload["kind"] != kind:
            raise AssertionError(
                "finishing_failed_report: production classifier derived "
                f"kind {payload['kind']!r} for (step={step!r}, "
                f"reason={reason!r}); expected {kind!r} — the failure "
                "vocabulary or the scripted pair drifted"
            )
        report["finish_failure"] = payload
    return report


class RecordingParent(ScriptedParent):
    """A :class:`ScriptedParent` that also records every report it returns.

    ``phase_drive`` observes ``phase_parent`` behaviour only through its
    reports — finish attempts inside the real parent happen in a subprocess.
    Recording the returned reports lets a test count the ``finishing_failed``
    reports drive consumed for a given child (the per-child finish-attempt
    observability the bounded-recovery features build on).
    """

    def __init__(self, reports: list[dict]):
        super().__init__(reports)
        self.returned: list[dict] = []

    def __call__(self, action, work_item_id, no_refactor=False, verbose=False):
        report = super().__call__(action, work_item_id, no_refactor, verbose)
        self.returned.append(report)
        return report


class DriveHarness:
    """Deterministic ``phase_drive`` harness with injectable collaborators.

    Records everything a recovery path may mutate so a test can assert both
    what happened (spawns, finish invocations, resets) and what must *never*
    happen to a terminal child.
    """

    def __init__(
        self,
        mod,
        *,
        children: list[dict],
        reports: list[dict] | None = None,
        runner: ScriptedParent | None = None,
        spawn_results: list[dict] | None = None,
        child_states: dict[str, dict] | None = None,
        worktree_path: str = "",
    ):
        self.mod = mod
        self.children = list(children)
        self.runner = runner or RecordingParent(list(reports or []))
        self.spawner = RecordingSpawner(spawn_results)
        self.child_states = dict(child_states or {})
        self.worktree_path = worktree_path
        self.show_calls: list[str] = []
        self.comments: list[tuple[str, str]] = []
        self.status_updates: list[dict] = []
        self.extra_patches: list = []

    # -- injectable collaborators ------------------------------------------

    def _show(self, work_item_id: str) -> dict:
        """Serve the parent plus scripted per-child state.

        ``phase_drive`` fetches the parent, then (on the failure/recovery
        path) re-fetches individual children; an unmapped child resolves to
        ``{}`` — non-terminal — so a wrong fetch can never fake success.
        """
        self.show_calls.append(work_item_id)
        if work_item_id == PARENT:
            return {"id": PARENT}
        return self.child_states.get(work_item_id, {})

    def _update_status(self, work_item_id, status, *args, **kwargs):
        self.status_updates.append(
            {"id": work_item_id, "status": status, **kwargs}
        )
        return {"success": True}

    def _comment(self, work_item_id, text):
        self.comments.append((work_item_id, text))
        return True

    def record_reset_helper(self, name: str = "_reset_child_worktree") -> list:
        """Patch the worktree-reset helper with a recorder and return the list.

        The helper is introduced by a sibling feature, so the patch is
        created with ``create=True``: the terminal-child guard can assert
        "never reset" before the helper exists, and keeps asserting it
        afterwards.
        """
        calls: list = []

        def _record(*args, **kwargs):
            calls.append((args, kwargs))
            return {
                "success": True,
                "reset": True,
                "ref": kwargs.get("parent_branch", "dev") if kwargs else "dev",
                "reason": "",
            }

        self.extra_patches.append(
            mock.patch.object(self.mod, name, side_effect=_record, create=True)
        )
        return calls

    def patch(self, name: str, **kwargs):
        """Register an extra ``mock.patch.object`` on the module under test."""
        patcher = mock.patch.object(self.mod, name, create=True, **kwargs)
        self.extra_patches.append(patcher)
        return patcher

    # -- recording helpers -------------------------------------------------

    @property
    def spawn_calls(self) -> list[str]:
        return [call["id"] for call in self.spawner.calls]

    @property
    def returned_reports(self) -> list[dict]:
        """Every ``parent`` report drive consumed, in order."""
        return list(self.runner.returned)

    def finishing_failed_reports(
        self, child_id: str | None = None,
    ) -> list[dict]:
        """The ``finishing_failed`` reports drive consumed.

        Optionally filtered to one child.
        """
        return [
            report
            for report in self.runner.returned
            if report.get("finishing_failed")
            and (child_id is None or report.get("next_child") == child_id)
        ]

    def mutated_ids(self) -> set[str]:
        return {update["id"] for update in self.status_updates}

    # -- execution ---------------------------------------------------------

    def run(self, **kwargs) -> dict:
        """Patch worklog plumbing and run one ``phase_drive`` invocation."""
        patches = [
            mock.patch.object(self.mod, "wl_show", side_effect=self._show),
            mock.patch.object(
                self.mod, "wl_show_children", return_value=self.children,
            ),
            mock.patch.object(self.mod, "_discover_worktree", return_value=self.worktree_path),
            mock.patch.object(
                self.mod, "wl_add_comment", side_effect=self._comment,
            ),
            mock.patch.object(
                self.mod.StatusLifecycle, "update_status", self._update_status,
            ),
        ]
        with contextlib.ExitStack() as stack:
            for patcher in patches + self.extra_patches:
                stack.enter_context(patcher)
            report = self.mod.phase_drive(
                PARENT,
                parent_runner=self.runner,
                spawn_session=self.spawner,
                **kwargs,
            )
        report["_show_calls"] = list(self.show_calls)
        return report


@pytest.fixture
def drive_harness(implement_mod):  # noqa: F811 — fixture param, not a redefinition
    """Factory fixture building a :class:`DriveHarness` bound to the module."""
    def _make(**kwargs) -> DriveHarness:
        return DriveHarness(implement_mod, **kwargs)

    return _make


@pytest.fixture
def finishing_failed_parent_runner():
    """Factory fixture scripting a ``parent_runner`` that reports finish
    failure.

    The returned runner always reports ``finishing_failed`` for the given
    child, ``times`` times (default 10 — enough for a bounded-retry recovery
    loop to exhaust its per-child bound without exhausting the script first,
    which would raise from :class:`ScriptedParent`).
    """
    def _make(child_id: str, *, times: int = 10, message=None):
        return RecordingParent([
            finishing_failed_report(child_id, message=message)
            for _ in range(times)
        ])

    return _make


def failing_finish_report(step: str, reason: str | None = None,
                          message: str = "Finishing failed",
                          *,
                          lock_holder: str | None = None,
                          guard_verifies: list | None = None) -> dict:
    """Script a ``finish`` subprocess failure report for one structured step.

    Mirrors the ``phase_finish`` failure exits: each exit records its
    ``(step, reason)`` in ``report["steps"]`` and the classifier resolves
    the failure from that structure — never from ``report["message"]``
    prose (LP-0MVWEP1MM0097RAT AC5).
    """
    report = {
        "phase": "finish",
        "work_item_id": "SA-CHILD1",
        "success": False,
        "message": message,
    }
    entry: dict = {"success": False}
    if step == "main_checkout_lock":
        entry = {"acquired": False, "holder": lock_holder}
    elif step == "main_checkout_guard":
        entry = {"verdict": "fail", "verifies": guard_verifies or []}
    elif step == "local_dev_fast_forward":
        entry = {"method": "refused", "reason": reason or "local_dev_diverged"}
    elif step == "push":
        entry["reason"] = reason or "timeout"
    elif step == "tests":
        report["steps"] = {
            step: [{"attempt": 1, "success": False, "failures": [],
                    "scope": "changed"}],
        }
        return report
    elif step == "build":
        entry = {"success": False, "exit_code": 1, "skipped": False}
    elif step in ("ancestry_assertion", "worktree_placement", "diff",
                  "commit", "finish"):
        entry["reason"] = reason or f"{step}_failed"
    else:
        entry["reason"] = reason or "generic"
    report["steps"] = {step: entry}
    return report


# ---------------------------------------------------------------------------
# AC1: finishing_failed_report scripts the finish_failure payload
# (LP-0MVWEP1XV003TGD2)
# ---------------------------------------------------------------------------


class TestFinishingFailedReportHarness:
    """The extended report helper scripts the classifier payload (AC1)."""

    def test_default_shape_is_byte_identical_to_today(self):
        """No extra arguments: exactly today's keys, no ``finish_failure``."""
        report = finishing_failed_report("SA-C1")
        assert report == {
            "phase": "parent",
            "work_item_id": PARENT,
            "success": False,
            "message": "Failed to finish child SA-C1",
            "next_child": "SA-C1",
            "finishing_failed": True,
            "children_processed": [],
        }
        assert "finish_failure" not in report

    def test_step_reason_script_produces_classifier_payload(
        self, implement_mod,  # noqa: F811 — fixture param
    ):
        """(step, reason) adds the payload the production classifier
        produces for a scripted failing finish report."""
        report = finishing_failed_report(
            "SA-C1", step="push", reason="timeout",
        )
        parsed = failing_finish_report("push", "timeout")
        assert report["finish_failure"] == (
            implement_mod.classify_finish_failure(parsed)
        )
        assert report["finish_failure"] == {
            "kind": "infrastructure",
            "step": "push",
            "reason": "timeout",
            "message": "Finishing failed",
            # LP-0MVWEP37K000JU0O: the payload carries the raw ``steps`` so
            # the verdict messages can surface the lock/guard evidence.
            "steps": {"push": {"success": False, "reason": "timeout"}},
        }

    def test_kind_argument_pins_the_expected_class(self):
        """kind names the classifier's derived kind; a mismatch raises so a
        script can never silently claim the wrong failure class."""
        report = finishing_failed_report(
            "SA-C1", step="build", kind="gate",
        )
        assert report["finish_failure"]["kind"] == "gate"

        with pytest.raises(AssertionError, match="expected 'gate'"):
            finishing_failed_report(
                "SA-C1", step="push", reason="timeout", kind="gate",
            )

    def test_kind_without_step_is_rejected(self):
        """A kind without a scripted step/reason is a script bug, not a
        silent no-op."""
        with pytest.raises(ValueError, match="requires a scripted step"):
            finishing_failed_report("SA-C1", kind="infrastructure")


# ---------------------------------------------------------------------------
# Classifier: finish refusal vocabulary and threading (LP-0MVWEP1MM0097RAT)
# ---------------------------------------------------------------------------


class TestFinishFailureClassifier:
    """Table-driven tests for ``classify_finish_failure``.

    AC1/AC2: the module-level vocabulary constants beside the emitters are
    the table; ``classify_finish_failure`` maps each listed ``(step,
    reason)`` onto its kind and every unlisted pair onto ``unknown``
    (fail-safe, never ``infrastructure``). AC3: ``None``/``{}``/non-dict
    input returns ``kind="unknown"`` without raising.
    """

    @pytest.mark.parametrize(
        "step_reason",
        [
            pytest.param(
                ("local_dev_fast_forward", "local_dev_diverged"),
                id="fast-forward-diverged",
            ),
            pytest.param(
                ("local_dev_fast_forward", "commit_not_based_on_remote_tip"),
                id="fast-forward-not-based",
            ),
            pytest.param(
                ("local_dev_fast_forward", "fetch_failed"),
                id="fast-forward-fetch-failed",
            ),
            pytest.param(
                ("local_dev_fast_forward", "checkout_failed"),
                id="fast-forward-checkout-failed",
            ),
            pytest.param(
                ("local_dev_fast_forward", "no_remote_tracking_ref"),
                id="fast-forward-no-tracking-ref",
            ),
            pytest.param(("main_checkout_lock", "lock_held"), id="lock-held"),
            pytest.param(
                ("main_checkout_guard", "fail"), id="guard-fail",
            ),
            pytest.param(("push", "timeout"), id="push-timeout"),
            pytest.param(
                ("push", "connection_closed"), id="push-connection-closed",
            ),
            pytest.param(("push", "transport"), id="push-transport"),
            pytest.param(
                ("ancestry_assertion", "ancestry_failed"),
                id="ancestry-failed",
            ),
        ],
    )
    def test_infra_vocabulary_pairs_classify_infrastructure(
        self, implement_mod, step_reason,  # noqa: F811 — fixture param
    ):
        """Every allow-listed (step, reason) classifies as infrastructure."""
        step, reason = step_reason
        assert (step, reason) in implement_mod.FINISH_INFRA_REFUSALS
        result = implement_mod.classify_finish_failure(
            failing_finish_report(step, reason)
        )
        assert result["kind"] == "infrastructure"
        assert result["kind"] in implement_mod.FINISH_FAILURE_KINDS
        assert result["step"] == step
        assert result["reason"] == reason

    @pytest.mark.parametrize(
        "step_reason",
        [
            pytest.param(("tests", "tests_failed"), id="tests-failed"),
            pytest.param(("build", "build_failed"), id="build-failed"),
            pytest.param(
                ("worktree_placement", "*"), id="worktree-placement-any",
            ),
            pytest.param(("diff", "no_changes"), id="diff-no-changes"),
        ],
    )
    def test_gate_vocabulary_pairs_classify_gate(
        self, implement_mod, step_reason,  # noqa: F811 — fixture param
    ):
        """Every listed gate (step, reason) pair classifies as gate."""
        step, reason = step_reason
        assert (step, reason) in implement_mod.FINISH_GATE_REFUSALS
        result = implement_mod.classify_finish_failure(
            failing_finish_report(step, reason)
        )
        assert result["kind"] == "gate"
        assert result["kind"] in implement_mod.FINISH_FAILURE_KINDS
        assert result["step"] == step

    @pytest.mark.parametrize(
        "step_reason",
        [
            pytest.param(
                ("local_dev_fast_forward", "mystery_refusal"),
                id="fast-forward-unlisted",
            ),
            pytest.param(("push", "push_failed"), id="push-failed"),
            pytest.param(
                ("push", "non_fast_forward"), id="push-non-fast-forward",
            ),
        ],
    )
    def test_unlisted_reasons_are_unknown_never_infrastructure(
        self, implement_mod, step_reason,  # noqa: F811 — fixture param
    ):
        """AC2 fail-safe: a resolvable (step, reason) pair that is not
        listed never classifies as infrastructure."""
        step, reason = step_reason
        result = implement_mod.classify_finish_failure(
            failing_finish_report(step, reason)
        )
        assert result["kind"] == "unknown"
        assert result["kind"] in implement_mod.FINISH_FAILURE_KINDS
        assert result["step"] == step
        assert result["reason"] == reason

    @pytest.mark.parametrize(
        "step_reason",
        [
            pytest.param(("unknown_step", "anything"), id="unknown-step"),
            pytest.param(("commit", "commit_failed"), id="commit-failed"),
            pytest.param(("finish", "runtime_error"), id="finish-runtime"),
        ],
    )
    def test_steps_outside_the_vocabulary_are_unknown(
        self, implement_mod, step_reason,  # noqa: F811 — fixture param
    ):
        """AC2/AC5 fail-safe: a step the precedence vocabulary does not
        resolve (e.g. the AC5 ``commit``/``finish`` exits) is ``unknown``
        with no step/reason — never ``infrastructure``."""
        step, reason = step_reason
        result = implement_mod.classify_finish_failure(
            failing_finish_report(step, reason)
        )
        assert result["kind"] == "unknown"
        assert result["kind"] in implement_mod.FINISH_FAILURE_KINDS
        assert result["step"] is None
        assert result["reason"] is None

    def test_gate_steps_classify_gate_despite_noise(
        self, implement_mod,  # noqa: F811 — fixture param
    ):
        """Build/tests failures still classify as gate with extra noise.

        The derived reasons for a failing ``build``/``tests`` step are the
        canonical listed pairs, even when the entry carries extra fields
        (exit code) and the report's prose says otherwise."""
        build = failing_finish_report("build", message="Push timed out")
        build["steps"]["build"]["exit_code"] = 9
        result = implement_mod.classify_finish_failure(build)
        assert result["kind"] == "gate"
        assert result["step"] == "build"
        assert result["reason"] == "build_failed"

        tests = failing_finish_report("tests", message="Local connect refused")
        result = implement_mod.classify_finish_failure(tests)
        assert result["kind"] == "gate"
        assert result["step"] == "tests"
        assert result["reason"] == "tests_failed"

    @pytest.mark.parametrize(
        "malformed",
        [
            pytest.param(None, id="none"),
            pytest.param({}, id="empty-dict"),
            pytest.param("not a dict", id="string"),
            pytest.param(42, id="int"),
            pytest.param([], id="list"),
        ],
    )
    def test_malformed_inputs_default_to_unknown(
        self, implement_mod, malformed,  # noqa: F811 — fixture param
    ):
        """AC3: None/{}/non-dict input returns unknown without raising."""
        result = implement_mod.classify_finish_failure(malformed)
        assert result["kind"] == "unknown"
        assert result["step"] is None
        assert result["reason"] is None
        assert result["message"] is None

    def test_failure_resolved_from_structure_not_prose(
        self, implement_mod,  # noqa: F811 — fixture param
    ):
        """AC5: classification reads ``steps``, never ``message`` prose.

        A report whose message names a gate failure but whose structured
        steps carry the transient fast-forward refusal classifies as
        infrastructure — and vice versa."""
        report = failing_finish_report(
            "local_dev_fast_forward", "local_dev_diverged",
            message="Build failed (exit code 1)",
        )
        result = implement_mod.classify_finish_failure(report)
        assert result == {
            "kind": "infrastructure",
            "step": "local_dev_fast_forward",
            "reason": "local_dev_diverged",
            "message": "Build failed (exit code 1)",
            # LP-0MVWEP37K000JU0O: the payload carries the raw ``steps`` so
            # the verdict messages can surface the lock/guard evidence.
            "steps": {
                "local_dev_fast_forward": {
                    "method": "refused",
                    "reason": "local_dev_diverged",
                },
            },
        }
        # Prose-only report (no structured steps) is unknown, never gate.
        prose_only = {
            "phase": "finish",
            "success": False,
            "message": "Build failed (exit code 1)",
            "steps": {},
        }
        assert implement_mod.classify_finish_failure(prose_only)["kind"] == (
            "unknown"
        )

    def test_precedence_resolution_uses_first_failing_step(
        self, implement_mod,  # noqa: F811 — fixture param
    ):
        """When several steps fail, the first in precedence order wins."""
        report = failing_finish_report("push", "timeout")
        report["steps"]["local_dev_fast_forward"] = {
            "method": "refused", "reason": "local_dev_diverged",
        }
        report["steps"]["build"] = {"success": False, "exit_code": 1}
        result = implement_mod.classify_finish_failure(report)
        assert result["kind"] == "infrastructure"
        assert result["step"] == "local_dev_fast_forward"
        assert result["reason"] == "local_dev_diverged"

    def test_emitted_pairs_stay_in_sync_with_vocabulary(
        self, implement_mod,  # noqa: F811 — fixture param
    ):
        """Drift rule: every emitted reason has a documented kind.

        The refusals ``_fast_forward_local_dev_for_push`` can emit and the
        structured failure reasons ``phase_finish`` exits record are all
        either listed in the vocabulary or fail safe as ``unknown``."""
        emitted_fast_forward_refusals = {
            "local_dev_diverged",
            "commit_not_based_on_remote_tip",
            "fetch_failed",
            "checkout_failed",
            "no_remote_tracking_ref",
        }
        for reason in emitted_fast_forward_refusals:
            pair = ("local_dev_fast_forward", reason)
            assert pair in implement_mod.FINISH_INFRA_REFUSALS, pair
        # push: timeout is infra, push_failed / non_fast_forward fail safe.
        assert (
            "push", "timeout",
        ) in implement_mod.FINISH_INFRA_REFUSALS
        # A lost SSH transport is infrastructure, not a red suite: finish emits
        # it as ``connection_closed`` and drive retries it on the infra budget
        # (SA-0MVWNHLQQ002TFZR).
        assert (
            "push", "connection_closed",
        ) in implement_mod.FINISH_INFRA_REFUSALS
        for reason in ("push_failed", "non_fast_forward"):
            result = implement_mod.classify_finish_failure(
                failing_finish_report("push", reason)
            )
            assert result["kind"] == "unknown"
        # tests / build are documented as gate failures.
        assert ("tests", "tests_failed") in implement_mod.FINISH_GATE_REFUSALS
        assert ("build", "build_failed") in implement_mod.FINISH_GATE_REFUSALS


class TestRunFinishFailureThreading:
    """AC4: ``phase_parent._run_finish`` threads the parsed report through.

    The finish subprocess runs with ``parse_on_failure=True``, the ``None``
    sentinel is preserved on failure (parsed or not), and the parent report
    carries ``finish_failure`` = ``classify_finish_failure(parsed)``.
    """

    def test_run_finish_uses_parse_on_failure_and_records_finish_failure(
        self, implement_mod, monkeypatch,  # noqa: F811 — fixture param
    ):
        """The parent report carries the classifier payload for a scripted
        failing finish report, and the ``None`` sentinel is preserved."""
        child = _child("SA-CHILD1")
        parsed = failing_finish_report(
            "local_dev_fast_forward", "local_dev_diverged",
        )
        captured: list[dict] = []

        def _fake_finish(*args, **kwargs):
            captured.append({"args": args, "kwargs": kwargs})
            return parsed

        # Hermetic: never let a release's Code Freeze marker leak into the
        # test (phase_parent runs the freeze gate on entry).
        monkeypatch.setattr(implement_mod, "is_code_freeze_active", lambda: False)
        monkeypatch.setattr(implement_mod, "wl_show",
                            lambda wid: {"id": wid, "status": "in_progress"})
        monkeypatch.setattr(implement_mod, "wl_show_children",
                            lambda wid: [child])
        monkeypatch.setattr(
            implement_mod, "_outbound_gate",
            lambda wid: {"blocked": False, "blockers": [],
                         "coordination_blockers": []},
        )
        monkeypatch.setattr(implement_mod, "wl_dep_blockers", lambda cid: [])
        monkeypatch.setattr(implement_mod, "_probe_child_lock", lambda cid: None)
        monkeypatch.setattr(
            implement_mod, "_discover_worktree", lambda cid: "/wt/SA-CHILD1",
        )
        monkeypatch.setattr(implement_mod, "_has_worktree_changes",
                            lambda *a: True)
        monkeypatch.setattr(implement_mod, "wl_add_comment",
                            lambda *a, **k: True)
        monkeypatch.setattr(implement_mod, "_invoke_implement", _fake_finish)

        report = implement_mod.phase_parent("SA-PARENT")

        assert [c["args"][0] for c in captured] == ["finish"]
        assert captured[0]["args"][1] == "SA-CHILD1"
        assert captured[0]["kwargs"]["parse_on_failure"] is True
        assert report["success"] is False
        assert report["finishing_failed"] is True
        assert report["next_child"] == "SA-CHILD1"
        assert report["finish_failure"] == {
            "kind": "infrastructure",
            "step": "local_dev_fast_forward",
            "reason": "local_dev_diverged",
            "message": "Finishing failed",
            "steps": {
                "local_dev_fast_forward": {
                    "method": "refused",
                    "reason": "local_dev_diverged",
                },
            },
        }

    def test_run_finish_keeps_none_sentinel_for_unparseable_failure(
        self, implement_mod, monkeypatch,  # noqa: F811 — fixture param
    ):
        """A finish subprocess that fails without a parseable report still
        yields the ``None`` sentinel and an ``unknown`` finish_failure."""
        child = _child("SA-CHILD1")
        # Hermetic: never let a release's Code Freeze marker leak into the
        # test (phase_parent runs the freeze gate on entry).
        monkeypatch.setattr(implement_mod, "is_code_freeze_active", lambda: False)
        monkeypatch.setattr(implement_mod, "wl_show",
                            lambda wid: {"id": wid, "status": "in_progress"})
        monkeypatch.setattr(implement_mod, "wl_show_children",
                            lambda wid: [child])
        monkeypatch.setattr(
            implement_mod, "_outbound_gate",
            lambda wid: {"blocked": False, "blockers": [],
                         "coordination_blockers": []},
        )
        monkeypatch.setattr(implement_mod, "wl_dep_blockers", lambda cid: [])
        monkeypatch.setattr(implement_mod, "_probe_child_lock", lambda cid: None)
        monkeypatch.setattr(
            implement_mod, "_discover_worktree", lambda cid: "/wt/SA-CHILD1",
        )
        monkeypatch.setattr(implement_mod, "_has_worktree_changes",
                            lambda *a: True)
        monkeypatch.setattr(implement_mod, "wl_add_comment",
                            lambda *a, **k: True)
        monkeypatch.setattr(
            implement_mod, "_invoke_implement",
            lambda *args, **kwargs: None,
        )

        report = implement_mod.phase_parent("SA-PARENT")

        assert report["success"] is False
        assert report["finishing_failed"] is True
        assert report["next_child"] == "SA-CHILD1"
        assert report["finish_failure"]["kind"] == "unknown"

    def test_run_finish_success_path_unaffected(
        self, implement_mod, monkeypatch,  # noqa: F811 — fixture param
    ):
        """A green finish keeps the pre-existing success semantics."""
        child = _child("SA-CHILD1")
        status_updates: list[dict] = []
        # Hermetic: never let a release's Code Freeze marker leak into the
        # test (phase_parent runs the freeze gate on entry).
        monkeypatch.setattr(implement_mod, "is_code_freeze_active", lambda: False)
        monkeypatch.setattr(implement_mod, "wl_show",
                            lambda wid: {"id": wid, "status": "in_progress"})
        monkeypatch.setattr(implement_mod, "wl_show_children",
                            lambda wid: [child])
        monkeypatch.setattr(
            implement_mod, "_outbound_gate",
            lambda wid: {"blocked": False, "blockers": [],
                         "coordination_blockers": []},
        )
        monkeypatch.setattr(implement_mod, "wl_dep_blockers", lambda cid: [])
        monkeypatch.setattr(implement_mod, "_probe_child_lock", lambda cid: None)
        monkeypatch.setattr(
            implement_mod, "_discover_worktree", lambda cid: "/wt/SA-CHILD1",
        )
        monkeypatch.setattr(implement_mod, "_has_worktree_changes",
                            lambda *a: True)
        monkeypatch.setattr(implement_mod, "wl_add_comment",
                            lambda *a, **k: True)
        monkeypatch.setattr(
            implement_mod, "_invoke_implement",
            lambda *args, **kwargs: {"phase": "finish", "success": True},
        )

        def _update_status(work_item_id, status, *args, **kwargs):
            status_updates.append({"id": work_item_id, "status": status})
            return {"success": True}

        monkeypatch.setattr(
            implement_mod.StatusLifecycle, "update_status", _update_status,
        )

        report = implement_mod.phase_parent("SA-PARENT")

        assert report["success"] is True
        assert report["parent_advanced"] is True
        assert "finishing_failed" not in report
        assert "finish_failure" not in report
        assert status_updates[-1]["status"] == "completed"


# ---------------------------------------------------------------------------
# AC2: the finishing_failed report is the livelock entry point
# ---------------------------------------------------------------------------


class TestFinishingFailedReports:
    """``phase_drive`` consumes the exact ``finishing_failed`` report shape."""

    def test_finishing_failed_stops_without_spawning_a_session(
        self, drive_harness, finishing_failed_parent_runner,
    ):
        """No session is spawned for a child whose finish failed.

        The finish failure is a *parent* failure, not a spawn failure: drive
        must never invent a session for it, must not claim success and must
        not advance the parent while the child is non-terminal.
        """
        harness = drive_harness(
            children=[_child("SA-C1")],
            runner=finishing_failed_parent_runner("SA-C1"),
        )

        report = harness.run()

        assert report["success"] is False
        assert report["parent_advanced"] is False
        assert report["failed_child"] == "SA-C1"
        assert report["sessions_spawned"] == 0
        assert harness.spawner.calls == []
        # The only parent report drive consumed was the failed finish for
        # SA-C1 — the fixture scripted exactly the `_run_finish` shape.
        assert harness.finishing_failed_reports("SA-C1")

    def test_finishing_failed_is_not_confused_with_parent_completion(
        self, drive_harness,
    ):
        """A finish failure never reads as "epic complete"."""
        harness = drive_harness(
            children=[_child("SA-C1")],
            reports=[finishing_failed_report("SA-C1")],
        )

        report = harness.run()

        assert report["success"] is False
        assert report["parent_advanced"] is False
        assert report["failed_child"] == "SA-C1"
        assert "Failed to finish child SA-C1" in report["message"]
        assert all(
            r.get("finishing_failed") and r.get("next_child") == "SA-C1"
            for r in harness.returned_reports
        )


# ---------------------------------------------------------------------------
# Parent AC1 (SA-0MVLJXS7R000XIFL): bounded finish-attempt accounting
# ---------------------------------------------------------------------------


class TestBoundedFinishAttempts:
    """Failing ``finish`` executions are counted per child and bounded.

    AC1: ``phase_drive`` distinguishes ``finishing_failed`` reports from other
    parent failures and counts them per child (a counter distinct from the
    session-spawn ``attempts``). AC2: the finish-attempt bound is configurable
    and defaults to the per-child session cap, so the same child's failing
    ``finish`` is never executed beyond the bound within one drive
    invocation. AC5: a single ``finishing_failed`` at the default bound still
    aborts exactly as before this feature.
    """

    def test_default_limit_preserves_immediate_stop(
        self, drive_harness, finishing_failed_parent_runner,
    ):
        """The default bound is the session cap, so the first failing finish
        stops the chain exactly as before this feature."""
        harness = drive_harness(
            children=[_child("SA-C1")],
            runner=finishing_failed_parent_runner("SA-C1"),
        )

        report = harness.run()

        assert report["success"] is False
        assert report["failed_child"] == "SA-C1"
        assert report["finish_attempts"] == {"SA-C1": 1}
        assert report["finish_attempt_limit"] == 1
        # Only the single failing parent report was consumed — no re-runs,
        # no invented session spawn.
        assert len(harness.runner.returned) == 1
        assert harness.spawner.calls == []

    def test_finish_failures_retried_up_to_configured_bound(
        self, drive_harness, finishing_failed_parent_runner,
    ):
        """The same child's failing ``finish`` is retried only up to the
        configured bound, then the chain stops with the attempt count."""
        harness = drive_harness(
            children=[_child("SA-C1")],
            runner=finishing_failed_parent_runner("SA-C1", times=10),
        )

        report = harness.run(finish_attempt_limit=3)

        assert report["success"] is False
        assert report["failed_child"] == "SA-C1"
        assert report["finish_attempts"] == {"SA-C1": 3}
        assert report["finish_attempt_limit"] == 3
        # The parent was re-run 3 times (3 real finish executions observed)
        # and never beyond the bound; the scripted runner is not exhausted.
        assert len(harness.runner.returned) == 3
        assert harness.spawner.calls == []
        assert "finish-attempt bound 3 exhausted" in report["message"]

    def test_finish_attempt_bound_is_independent_of_session_cap(
        self, drive_harness, finishing_failed_parent_runner,
    ):
        """The finish-attempt bound can exceed the per-child session cap."""
        harness = drive_harness(
            children=[_child("SA-C1")],
            runner=finishing_failed_parent_runner("SA-C1", times=10),
        )

        report = harness.run(max_child_sessions=4, finish_attempt_limit=2)

        assert report["success"] is False
        assert report["failed_child"] == "SA-C1"
        assert report["finish_attempts"] == {"SA-C1": 2}
        assert report["finish_attempt_limit"] == 2
        assert len(harness.runner.returned) == 2

    def test_bounded_retry_resumes_when_parent_recovers(
        self, drive_harness,
    ):
        """A transient finish failure inside the bound is not chain-fatal:
        when the next parent report returns to progress, the drive loop
        continues normally (session spawn + parent advance)."""
        harness = drive_harness(
            children=[_child("SA-C1")],
            reports=[
                finishing_failed_report("SA-C1"),
                {
                    "success": True,
                    "next_child": "SA-C1",
                    "worktree_path": "/wt/SA-C1",
                },
                {"success": True, "parent_advanced": True},
            ],
        )

        report = harness.run(finish_attempt_limit=3)

        assert report["success"] is True
        assert report["parent_advanced"] is True
        assert harness.spawn_calls == ["SA-C1"]
        assert report["finish_attempts"] == {"SA-C1": 1}

    def test_generic_parent_failure_is_not_counted_as_finish(
        self, drive_harness,
    ):
        """Non-``finishing_failed`` parent failures (start_failed, placement
        violation) still stop immediately and are not finish-attempt counts."""
        harness = drive_harness(
            children=[_child("SA-C1")],
            reports=[{
                "success": False,
                "message": "start failed",
                "next_child": "SA-C1",
                "start_failed": True,
            }],
        )

        report = harness.run(finish_attempt_limit=5)

        assert report["success"] is False
        assert report["failed_child"] == "SA-C1"
        assert report["finish_attempts"] == {}
        assert "start failed" in report["message"]


# ---------------------------------------------------------------------------
# LP-0MVWEP292000ODBS: infra-retry budget for transient finish refusals
# ---------------------------------------------------------------------------


def infra_refusal_report(child_id: str) -> dict:
    """A ``finishing_failed`` parent report classified as **infrastructure**.

    Scripts the incident shape (a ``local_dev_fast_forward`` refusal with
    ``local_dev_diverged`` — a concurrent session fast-forwarded local
    ``dev``) through the production classifier, so the payload can never
    silently claim the wrong failure class (LP-0MVWEP292000ODBS).
    """
    return finishing_failed_report(
        child_id,
        step="local_dev_fast_forward",
        reason="local_dev_diverged",
        kind="infrastructure",
    )


def gate_failure_report(child_id: str) -> dict:
    """A ``finishing_failed`` parent report classified as **gate**."""
    return finishing_failed_report(
        child_id,
        step="tests",
        reason="tests_failed",
        kind="gate",
    )


class TestInfraRetryBudget:
    """Transient infrastructure refusals are retried on their **own** bounded
    budget, never consuming the finish-attempt budget
    (LP-0MVWEP292000ODBS AC2–AC6).

    AC2: an infrastructure refusal increments ``infra_retries[child]`` and
    leaves ``finish_attempts`` without an entry for that child. AC3: a
    scripted harness returning three infrastructure refusals then success
    ends with ``success: True``, the child recorded as ``finished``,
    ``infra_retries == {child: 3}`` and ``infra_retry_limit == 5``. AC4:
    every retry emits a ``children_driven`` entry with ``result:
    "infra-retry"`` carrying ``step``, ``reason`` and the attempt number,
    and spawns **no** session. AC5: exhausting ``--infra-retry-limit`` falls
    through to the existing reconcile/reset path and stops with a message
    naming the failing step and reason. AC6: the backoff is bounded and
    injectable.
    """

    def test_three_infra_refusals_then_success_ends_finished(
        self, drive_harness,
    ):
        """AC3 (and parent AC2): the refusal clears inside the budget, the
        same ``finish`` succeeds and the chain continues — the committed
        slice is never discarded by an infra refusal that self-clears."""
        harness = drive_harness(
            children=[_child("SA-C1")],
            runner=RecordingParent([
                *(infra_refusal_report("SA-C1") for _ in range(3)),
                {"success": True, "next_child": "SA-C1",
                 "worktree_path": "/wt/SA-C1"},
                {"success": True, "parent_advanced": True},
            ]),
        )
        delays: list[float] = []

        report = harness.run(delay=delays.append)

        assert report["success"] is True
        assert report["parent_advanced"] is True
        assert report["infra_retry_limit"] == 5
        # The child finished after the refusals cleared.
        assert [e["result"] for e in report["children_driven"]] == [
            "infra-retry", "infra-retry", "infra-retry", "finished",
        ]
        assert report["children_driven"][-1]["id"] == "SA-C1"
        # AC2: the refusals were counted in infra_retries, never in
        # finish_attempts.
        assert report["infra_retries"] == {"SA-C1": 3}
        assert report["finish_attempts"] == {}
        # One real session (the final progress report), never per retry.
        assert report["sessions_spawned"] == 1
        assert harness.spawn_calls == ["SA-C1"]
        # AC6: the injected backoff ran (no real sleeps).
        assert delays == [2, 4, 8]

    def test_infra_retry_entries_carry_step_reason_and_attempt(
        self, drive_harness,
    ):
        """AC4: each retry entry names the failing step/reason and numbers
        the attempt."""
        harness = drive_harness(
            children=[_child("SA-C1")],
            runner=RecordingParent([
                *(infra_refusal_report("SA-C1") for _ in range(3)),
                {"success": True, "next_child": "SA-C1",
                 "worktree_path": "/wt/SA-C1"},
                {"success": True, "parent_advanced": True},
            ]),
        )

        report = harness.run(delay=lambda _s: None)

        retries = [
            e for e in report["children_driven"]
            if e["result"] == "infra-retry"
        ]
        assert len(retries) == 3
        for i, entry in enumerate(retries, start=1):
            assert entry["id"] == "SA-C1"
            assert entry["step"] == "local_dev_fast_forward"
            assert entry["reason"] == "local_dev_diverged"
            assert entry["attempt"] == i
        # The retries themselves spawned no session: only the eventual
        # child session counts (AC4 — ``sessions_spawned`` unchanged by
        # the retries).
        assert len(harness.spawner.calls) == 1

    def test_exhausting_infra_budget_falls_through_and_names_step_reason(
        self, drive_harness,
    ):
        """AC5: spending the infra budget is bounded — the loop falls
        through to the existing reconcile/reset path and stops with a
        message naming the failing step and reason."""
        harness = drive_harness(
            children=[_child("SA-C1")],
            runner=RecordingParent([
                *(infra_refusal_report("SA-C1") for _ in range(3)),
            ]),
            child_states={"SA-C1": _child("SA-C1")},
        )

        report = harness.run(infra_retry_limit=2)

        assert report["success"] is False
        assert report["failed_child"] == "SA-C1"
        # Two infra retries ran on the separate budget; the third refusal
        # exhausted it and fell through to the finish-attempt path.
        assert report["infra_retries"] == {"SA-C1": 2}
        assert [e["result"] for e in report["children_driven"]] == [
            "infra-retry", "infra-retry",
        ]
        assert report["finish_attempts"] == {"SA-C1": 1}
        # The stopping verdict names the failing step and reason.
        assert "local_dev_fast_forward" in report["message"]
        assert "local_dev_diverged" in report["message"]
        assert harness.spawner.calls == []

    def test_gate_failures_never_touch_the_infra_budget(
        self, drive_harness,
    ):
        """A genuine gate failure (tests/tests_failed) is never retried as
        infrastructure: it consumes the existing finish-attempt path
        unchanged and leaves ``infra_retries`` empty."""
        harness = drive_harness(
            children=[_child("SA-C1")],
            runner=RecordingParent([
                *(gate_failure_report("SA-C1") for _ in range(3)),
            ]),
            child_states={"SA-C1": _child("SA-C1")},
        )

        report = harness.run(finish_attempt_limit=3)

        assert report["success"] is False
        assert report["failed_child"] == "SA-C1"
        assert report["infra_retries"] == {}
        assert report["children_driven"] == []
        assert report["finish_attempts"] == {"SA-C1": 3}
        assert harness.spawner.calls == []

    def test_backoff_schedule_is_bounded(self, implement_mod):  # noqa: F811
        """AC6: ``_drive_infra_backoff_delay`` yields [2, 4, 8, 16, 30, 30]
        seconds for attempts 1–6 and caps at 30 s thereafter."""
        m = implement_mod
        assert [m._drive_infra_backoff_delay(n) for n in range(1, 7)] == [
            2, 4, 8, 16, 30, 30,
        ]
        assert m._drive_infra_backoff_delay(10) == 30
        assert m._drive_infra_backoff_delay(100) == 30


# ---------------------------------------------------------------------------
# Parent AC2 (SA-0MVLJXTYY005XQDV): terminal reconciliation on finish failure
# ---------------------------------------------------------------------------


class TestFinishFailureTerminalReconciliation:
    """A child whose ``finish`` exhausted the bound is re-fetched and, when
    terminal, recorded as done instead of stalling the epic."""

    def test_terminal_child_reconciled_when_finish_bound_exhausted(
        self, drive_harness, finishing_failed_parent_runner,
    ):
        """After the finish bound, a terminal child is recorded as done and
        the drive continues; it is never reset, finished or re-spawned."""
        child = _child("SA-C1", status="completed", stage="in_review")
        harness = drive_harness(
            children=[child],
            runner=RecordingParent([
                *(finishing_failed_report("SA-C1") for _ in range(3)),
                {"success": True, "parent_advanced": True,
                 "message": "all children terminal"},
            ]),
            child_states={"SA-C1": child},
        )
        reset_calls = harness.record_reset_helper()

        report = harness.run(finish_attempt_limit=3)

        assert report["success"] is True
        assert report["parent_advanced"] is True
        assert report["finish_attempts"] == {"SA-C1": 3}
        entry = report["children_driven"][0]
        assert entry["id"] == "SA-C1"
        assert entry["result"] == "terminal-reconciled"
        assert entry["reconciled"] is True
        assert entry["status"] == "completed"
        assert entry["stage"] == "in_review"
        assert entry["finish_attempts"] == 3
        assert entry["evidence"] == "status=completed, stage=in_review"
        assert "terminal state" in entry["reason"]
        assert report["terminal_reconciled"] == 1
        # Nothing was finished, reset, or re-spawned.
        assert harness.spawner.calls == []
        assert harness.status_updates == []
        assert reset_calls == []

    def test_non_terminal_child_without_resettable_worktree_fails_closed(
        self, drive_harness, finishing_failed_parent_runner,
    ):
        """A still non-terminal child is never reconciled as done; when no
        resettable worktree is available the hard finish failure stands (the
        reset path fails closed rather than guessing)."""
        child = _child("SA-C1")
        harness = drive_harness(
            children=[child],
            runner=finishing_failed_parent_runner("SA-C1", times=10),
            child_states={"SA-C1": child},
        )

        report = harness.run(finish_attempt_limit=3)

        assert report["success"] is False
        assert report["failed_child"] == "SA-C1"
        assert report["finish_attempts"] == {"SA-C1": 3}
        assert report.get("terminal_reconciled", 0) == 0
        assert "status=open" in report["message"]
        assert "recovery reset refused" in report["message"]
        assert harness.spawner.calls == []
        assert harness.status_updates == []
        assert report["children_driven"] == []

    def test_unfetchable_child_fails_closed(self, drive_harness):
        """When the child cannot be re-fetched, the hard failure stands
        (never a silent success)."""
        harness = drive_harness(
            children=[_child("SA-C1")],
            reports=[finishing_failed_report("SA-C1")],
        )

        report = harness.run(finish_attempt_limit=1)

        assert report["success"] is False
        assert report["failed_child"] == "SA-C1"
        assert report["children_driven"] == []

    def test_happy_path_never_re_fetches_children(self, drive_harness):
        """Reconciliation fetches happen only on the slow finish-failure
        path (AC6) — a clean drive re-fetches no child."""
        child = _child("SA-C1")
        harness = drive_harness(
            children=[child],
            reports=[
                {"success": True, "next_child": "SA-C1",
                 "worktree_path": "/wt/SA-C1"},
                {"success": True, "parent_advanced": True},
            ],
        )

        report = harness.run()

        assert report["success"] is True
        assert report["_show_calls"] == [PARENT]


# ---------------------------------------------------------------------------
# Parent AC3 (SA-0MVLJXVND0015LPW): non-terminal reset for a clean retry
# ---------------------------------------------------------------------------


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    """Run a git command in *cwd* hermetically, raising on failure."""
    return run_git(cwd, args, check=True)


@pytest.fixture
def child_worktree(tmp_path):
    """A real repo with a linked child worktree holding abandoned WIP.

    Mirrors the production failure state: the child's own worktree has
    uncommitted modifications and untracked files left behind by a killed
    session, while the main checkout has its own unrelated dirty file that
    must never be touched by a reset.
    """
    repo = tmp_path / "main_repo"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    _git("config", "user.email", "test@test.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    (repo / "README.md").write_text("# Test\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "Initial commit", cwd=repo)
    _git("branch", "dev", cwd=repo)

    worktree = (tmp_path / "wt-child").resolve()
    _git(
        "worktree", "add", "--track", "-b", "wl-SA-C1-test",
        str(worktree), "dev", cwd=repo,
    )

    # Abandoned session WIP: a tracked-file modification plus untracked files.
    (worktree / "README.md").write_text("# Abandoned WIP\n")
    (worktree / "wip.txt").write_text("half-finished work\n")
    tests_dir = worktree / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_wip.py").write_text("def test_wip():\n    assert False\n")

    # Unrelated dirty file in the MAIN checkout: must survive any reset.
    (repo / "main_dirty.txt").write_text("operator work\n")

    return {"repo": repo, "worktree": worktree}


class TestNonTerminalResetForRetry:
    """An exhausted non-terminal child is reset and retried in a fresh
    session instead of livelocking the epic."""

    def test_reset_helper_resets_only_the_child_worktree(
        self, implement_mod, child_worktree,  # noqa: F811 — fixture param
    ):
        m = implement_mod
        worktree = child_worktree["worktree"]
        repo = child_worktree["repo"]

        result = m._reset_child_worktree(str(worktree), "dev")

        assert result["success"] is True
        assert result["reset"] is True
        assert result["ref"] == "dev"
        # The child worktree is clean and back at the parent-branch HEAD.
        status = subprocess.run(
            ["git", "status", "--porcelain=v1"], cwd=str(worktree),
            capture_output=True, text=True, check=True,
        )
        assert status.stdout.strip() == ""
        assert not (worktree / "wip.txt").exists()
        assert not (worktree / "tests" / "test_wip.py").exists()
        assert (worktree / "README.md").read_text() == "# Test\n"
        # The main checkout's own dirty file is untouched.
        assert (repo / "main_dirty.txt").read_text() == "operator work\n"

    def test_reset_helper_refuses_the_main_checkout(
        self, implement_mod, child_worktree,  # noqa: F811 — fixture param
    ):
        m = implement_mod
        repo = child_worktree["repo"]

        result = m._reset_child_worktree(str(repo), "dev")

        assert result["success"] is False
        assert result["reset"] is False
        assert "not a linked git worktree" in result["reason"]
        # The main checkout's dirty file survived the refused reset.
        assert (repo / "main_dirty.txt").read_text() == "operator work\n"

    def test_reset_helper_fails_closed_for_missing_worktree(
        self, implement_mod, tmp_path,  # noqa: F811 — fixture param
    ):
        result = implement_mod._reset_child_worktree(
            str(tmp_path / "does-not-exist"), "dev",
        )

        assert result["success"] is False
        assert result["reset"] is False
        assert "does not exist" in result["reason"]

    def test_non_terminal_child_reset_then_retried_in_fresh_session(
        self, drive_harness, child_worktree,
    ):
        """AC3/AC6: after the finish bound the child is reset, its status is
        set back to ``open``, an audit comment is recorded, and the loop
        continues so the next iteration spawns a fresh session."""
        worktree = child_worktree["worktree"]
        repo = child_worktree["repo"]
        harness = drive_harness(
            children=[_child("SA-C1")],
            runner=RecordingParent([
                *(finishing_failed_report("SA-C1") for _ in range(3)),
                {"success": True, "next_child": "SA-C1",
                 "worktree_path": str(worktree)},
                {"success": True, "parent_advanced": True},
            ]),
            child_states={"SA-C1": _child("SA-C1")},
            worktree_path=str(worktree),
        )

        report = harness.run(finish_attempt_limit=3)

        assert report["success"] is True
        assert report["parent_advanced"] is True
        assert report["reset_for_retry"] == 1
        results = [e["result"] for e in report["children_driven"]]
        assert results == ["reset-for-retry", "finished"]
        reset_entry = report["children_driven"][0]
        assert reset_entry["reset"] is True
        assert reset_entry["finish_attempts"] == 3
        assert reset_entry["status"] == "open"
        assert "reset to dev" in reset_entry["reason"]
        # The evidence is on the entry: the discarded uncommitted files are
        # reported (never a commit), so the reset-for-retry record is
        # auditable from the drive report alone (LP-0MVWEP2VQ001NPRK AC2/AC3).
        assert reset_entry["discarded_paths"] == [
            "README.md", "tests/test_wip.py", "wip.txt",
        ]
        # Status reset through the shared lifecycle path, then a fresh session.
        assert {"id": "SA-C1", "status": "open", "assignee": ""} in (
            harness.status_updates
        )
        assert harness.spawn_calls == ["SA-C1"]
        # Audit comment names the failure, the attempts and the reset.
        child_comments = [
            text for wid, text in harness.comments if wid == "SA-C1"
        ]
        assert len(child_comments) == 1
        assert "Failed to finish child SA-C1" in child_comments[0]
        assert "3" in child_comments[0]
        assert "git reset --hard" in child_comments[0]
        assert "- Worktree reset to: `dev`" in child_comments[0]
        # The abandoned WIP was discarded from the child worktree only.
        status = subprocess.run(
            ["git", "status", "--porcelain=v1"], cwd=str(worktree),
            capture_output=True, text=True, check=True,
        )
        assert status.stdout.strip() == ""
        assert (repo / "main_dirty.txt").read_text() == "operator work\n"

    def test_reset_runs_at_most_once_per_child(
        self, drive_harness, child_worktree,
    ):
        """A child that keeps failing after its reset stops the chain
        (bounded) instead of resetting forever."""
        worktree = child_worktree["worktree"]
        harness = drive_harness(
            children=[_child("SA-C1")],
            runner=RecordingParent([
                *(finishing_failed_report("SA-C1") for _ in range(8)),
            ]),
            child_states={"SA-C1": _child("SA-C1")},
            worktree_path=str(worktree),
        )

        report = harness.run(finish_attempt_limit=3)

        assert report["success"] is False
        assert report["failed_child"] == "SA-C1"
        assert report["reset_for_retry"] == 1
        assert len(harness.status_updates) == 1
        # Four finish failures consumed: the bound (3) triggers one reset,
        # then the next failure stops the chain without a second reset.
        assert len(harness.runner.returned) == 4
        assert report["children_driven"][0]["result"] == "reset-for-retry"


# ---------------------------------------------------------------------------
# AC2/AC3: commit-carrying worktree fixture + recording reset helper
# (LP-0MVWEP1XV003TGD2)
# ---------------------------------------------------------------------------


@pytest.fixture
def commit_carrying_worktree(tmp_path):
    """A real repo whose linked worktree is one commit ahead of ``dev``.

    Mirrors the state a committed-child preservation gate must protect: the
    child's slice is a *committed* change on its own branch that the parent
    branch does not contain, so a destructive reset (``git reset --hard
    dev`` / ``git clean -fd``) would discard real work. The fixture asserts
    the ahead-of-parent relationship itself (AC2) so a later preservation
    test can never pass vacuously.

    The uncommitted-only variant — abandoned WIP in the worktree, nothing
    committed — is the existing :func:`child_worktree` fixture.
    """
    repo = tmp_path / "main_repo"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    _git("config", "user.email", "test@test.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    (repo / "README.md").write_text("# Test\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "Initial commit", cwd=repo)
    _git("branch", "dev", cwd=repo)

    worktree = (tmp_path / "wt-child").resolve()
    _git(
        "worktree", "add", "--track", "-b", "wl-SA-C1-test",
        str(worktree), "dev", cwd=repo,
    )

    # The child's committed slice work, ahead of dev.
    (worktree / "feature.txt").write_text("committed slice work\n")
    _git("add", "-A", cwd=worktree)
    _git("commit", "-q", "-m", "Child slice commit", cwd=worktree)

    # Explicit setup assertions (AC2): HEAD is exactly one commit ahead of
    # dev and that commit is reachable from HEAD (not dangling).
    dev_rev = _git("rev-parse", "dev", cwd=repo).stdout.strip()
    head_rev = _git("rev-parse", "HEAD", cwd=worktree).stdout.strip()
    ahead = _git(
        "rev-list", "--count", f"{dev_rev}..HEAD", cwd=worktree,
    ).stdout.strip()
    assert ahead == "1", (
        f"setup failure: expected HEAD to be 1 commit ahead of dev, "
        f"got {ahead}"
    )
    assert head_rev != dev_rev, "setup failure: HEAD equals dev"
    assert _git("merge-base", "dev", "HEAD", cwd=worktree).stdout.strip() == (
        dev_rev
    ), "setup failure: dev is not an ancestor of HEAD"

    return {
        "repo": repo,
        "worktree": worktree,
        "dev_rev": dev_rev,
        "head_rev": head_rev,
    }


class TestCommitCarryingWorktree:
    """Meta-tests pinning the preservation-gate fixture's setup (AC2/AC3)."""

    def test_fixture_head_is_ahead_of_dev_with_reachable_commit(
        self, commit_carrying_worktree,
    ):
        """The ahead-of-parent relationship holds and the commit is
        reachable from HEAD (AC2)."""
        fxt = commit_carrying_worktree
        head_rev = _git(
            "rev-parse", "HEAD", cwd=fxt["worktree"],
        ).stdout.strip()
        dev_rev = _git(
            "rev-parse", "dev", cwd=fxt["worktree"],
        ).stdout.strip()
        assert head_rev == fxt["head_rev"]
        assert dev_rev == fxt["dev_rev"]
        # Exactly the initial commit plus the one slice commit is reachable
        # from HEAD — the slice commit is real, not dangling.
        assert _git(
            "rev-list", "--count", "HEAD", cwd=fxt["worktree"],
        ).stdout.strip() == "2"

    def test_commit_carrying_worktree_is_detected_as_having_changes(
        self, implement_mod, commit_carrying_worktree,  # noqa: F811
    ):
        """The fixture's worktree registers "has changes" — the same check
        the production drive paths use to hand a child to ``finish`` (AC3)."""
        assert implement_mod._has_worktree_changes(
            str(commit_carrying_worktree["worktree"]), "dev"
        ) is True

    def test_clean_worktree_at_dev_is_not_detected_as_having_changes(
        self, implement_mod, tmp_path,  # noqa: F811
    ):
        """A clean worktree sitting at ``dev`` has no changes — so the
        positive detection above cannot pass vacuously (AC3)."""
        repo = tmp_path / "clean_repo"
        repo.mkdir()
        _git("init", "-q", cwd=repo)
        _git("config", "user.email", "test@test.com", cwd=repo)
        _git("config", "user.name", "Test", cwd=repo)
        (repo / "README.md").write_text("# Test\n")
        _git("add", "-A", cwd=repo)
        _git("commit", "-q", "-m", "Initial commit", cwd=repo)
        _git("branch", "dev", cwd=repo)

        worktree = (tmp_path / "wt-clean").resolve()
        _git(
            "worktree", "add", "--track", "-b", "wl-SA-C1-clean",
            str(worktree), "dev", cwd=repo,
        )
        assert implement_mod._has_worktree_changes(str(worktree), "dev") is False


class RecordingReset:
    """Wrap ``_reset_child_worktree`` recording every call and git command.

    Records each invocation (positional/keyword args) and every ``git``
    command the wrapped implementation executed (via the module's
    ``run_cmd``), so a test can assert *both* directions:

    * that a reset ran — :attr:`calls` non-empty and :meth:`destructive`
      non-empty for the worktree;
    * the preservation-gate guarantee — that **no** ``git reset --hard`` or
      ``git clean -fd`` ran against a given worktree path
      (:meth:`destructive` empty), without mocking the reset out of
      existence: the wrapper runs the real implementation underneath
      recording (AC4).
    """

    def __init__(self, mod):
        self.mod = mod
        # The real implementation, snapped at construction: when this recorder
        # is installed as a mock ``side_effect`` (drive-path tests), re-reading
        # ``mod._reset_child_worktree`` would hit the mock and recurse — the
        # wrapped call must go through the original it captured.
        self._original = mod._reset_child_worktree
        self.calls: list[tuple[tuple, dict]] = []
        self.commands: list[list[str]] = []

    def __call__(self, worktree_path: str, parent_branch: str = "dev", **kwargs):
        """Invoke the real ``_reset_child_worktree`` under command recording.

        Returns the reset result; the invocation and every git command it
        ran are recorded on this instance.
        """
        self.calls.append(((worktree_path, parent_branch), kwargs))
        original = self._original
        original_run_cmd = self.mod.run_cmd

        def _recorded(cmd, **kw):
            self.commands.append(list(cmd))
            return original_run_cmd(cmd, **kw)

        with mock.patch.object(self.mod, "run_cmd", side_effect=_recorded):
            return original(
                worktree_path, parent_branch, **kwargs
            )

    def destructive(self, worktree_path: str | None = None) -> list[list[str]]:
        """Recorded commands that discard committed work, per worktree.

        The exact pair ``_reset_child_worktree`` runs: ``git reset --hard``
        and ``git clean -fd`` (both in the ``git -C <path>`` form). When
        *worktree_path* is given only commands targeting that path are
        returned, so "nothing destructive ran **against this worktree**" is
        asserted precisely.
        """
        return [
            list(cmd)
            for cmd in self.commands
            if _is_destructive_git_command(cmd, worktree_path)
        ]


def _is_destructive_git_command(
    cmd: list[str], worktree_path: str | None,
) -> bool:
    """True when *cmd* is a destructive reset/clean invocation."""
    tokens = set(cmd)
    is_destructive = ({"reset", "--hard"} <= tokens) or (
        {"clean", "-fd"} <= tokens
    )
    if not is_destructive:
        return False
    if worktree_path is None:
        return True
    return worktree_path in cmd  # the ``-C <path>`` argument


@pytest.fixture
def recording_reset(implement_mod):  # noqa: F811 — fixture param
    """Factory returning a :class:`RecordingReset` bound to the module."""
    def _make() -> RecordingReset:
        return RecordingReset(implement_mod)

    return _make


class TestRecordingResetHelper:
    """The recording reset proves *presence* and *absence* of destructive
    commands (AC4)."""

    def test_recording_reset_observes_the_destructive_commands(
        self, recording_reset, child_worktree,
    ):
        """When a reset actually runs, the recorder sees it — so an empty
        ``destructive()`` result is evidence, not a blind pass."""
        recorder = recording_reset()
        result = recorder(child_worktree["worktree"], "dev")

        assert result["success"] is True
        assert len(recorder.calls) == 1
        destructive = recorder.destructive(str(child_worktree["worktree"]))
        assert ["git", "-C", str(child_worktree["worktree"]),
                "reset", "--hard", "dev"] in destructive
        assert ["git", "-C", str(child_worktree["worktree"]),
                "clean", "-fd"] in destructive

    def test_recording_reset_proves_nothing_destructive_ran(
        self, recording_reset, commit_carrying_worktree,
    ):
        """A path that never invokes the reset records no call and no
        destructive command against the worktree (AC4 negative proof)."""
        recorder = recording_reset()

        assert recorder.calls == []
        assert recorder.destructive(
            str(commit_carrying_worktree["worktree"])
        ) == []

    def test_recording_reset_refusal_is_seen_without_destructives(
        self, recording_reset, child_worktree,
    ):
        """A refused reset (main checkout) records the call but no
        destructive command against the refused path."""
        recorder = recording_reset()
        result = recorder(child_worktree["repo"], "dev")

        assert result["success"] is False
        assert result["reset"] is False
        assert len(recorder.calls) == 1
        assert recorder.destructive(str(child_worktree["repo"])) == []

    def test_recording_reset_filters_to_the_given_worktree(
        self, recording_reset, child_worktree,
    ):
        """Destructive commands are attributable to a specific path."""
        recorder = recording_reset()
        recorder(child_worktree["worktree"], "dev")

        other = str(child_worktree["worktree"]) + "2"
        assert recorder.destructive(other) == []


# ---------------------------------------------------------------------------
# AC3 (parent AC5): terminal children are never touched
# ---------------------------------------------------------------------------


class TestTerminalChildRegression:
    """A terminal child is never finished, reset or re-spawned by drive."""

    @pytest.mark.parametrize(
        "state",
        [
            pytest.param(
                {"status": "completed", "stage": "in_review"},
                id="completed-status",
            ),
            pytest.param(
                {"status": "done", "stage": "in_review"},
                id="done-status",
            ),
            pytest.param(
                {"status": "in_review", "stage": "in_review"},
                id="in-review-status",
            ),
            pytest.param(
                {"status": "open", "stage": "in_review"},
                id="in-review-stage",
            ),
            pytest.param(
                {"status": "open", "deletedBy": "operator"},
                id="soft-deleted",
            ),
        ],
    )
    def test_terminal_child_is_never_driven(self, drive_harness, state):
        """An all-terminal epic spawns, finishes and resets nothing."""
        child = _child("SA-T", **state)
        harness = drive_harness(
            children=[child],
            reports=[{
                "success": True,
                "parent_advanced": True,
                "message": "all children terminal",
            }],
            child_states={"SA-T": child},
        )
        reset_calls = harness.record_reset_helper()

        report = harness.run()

        assert report["success"] is True
        assert report["parent_advanced"] is True
        assert report["children_driven"] == []
        assert harness.spawner.calls == []
        assert harness.finishing_failed_reports() == []
        assert harness.status_updates == []
        assert reset_calls == []

    def test_mixed_epic_drives_only_the_non_terminal_child(
        self, drive_harness,
    ):
        """A terminal sibling is skipped while the open child is driven."""
        terminal = _child("SA-T", status="completed", stage="in_review")
        open_child = _child("SA-O")
        harness = drive_harness(
            children=[terminal, open_child],
            reports=[
                {
                    "success": True,
                    "next_child": "SA-O",
                    "worktree_path": "/wt/SA-O",
                    "children_processed": [{
                        "id": "SA-T",
                        "action": "skipped-terminal",
                        "status": "completed",
                    }],
                },
                {"success": True, "parent_advanced": True},
            ],
            child_states={"SA-T": terminal, "SA-O": open_child},
        )
        reset_calls = harness.record_reset_helper()

        report = harness.run()

        assert report["success"] is True
        assert report["parent_advanced"] is True
        # Only the open child got a session; the terminal sibling never did.
        assert harness.spawn_calls == ["SA-O"]
        assert [e["id"] for e in report["children_driven"]] == ["SA-O"]
        # The terminal child was never finished, reset or reconciled.
        assert not harness.finishing_failed_reports("SA-T")
        assert "SA-T" not in harness.mutated_ids()
        assert reset_calls == []

    @pytest.mark.parametrize(
        "overrides",
        [
            pytest.param({"status": "done"}, id="done-status"),
            pytest.param({"status": "in_review"}, id="in-review-status"),
            pytest.param(
                {"status": "in_progress", "deletedBy": "operator"},
                id="soft-deleted-wins-over-in-progress",
            ),
        ],
    )
    def test_skip_terminal_precedence_is_preserved(
        self, implement_mod, overrides,  # noqa: F811 — fixture param
    ):
        """``skip-terminal`` outranks ``skip-in-progress`` in
        classification."""
        assert implement_mod._classify_child(_child("SA-T", **overrides)) == (
            "skip-terminal"
        )


# ---------------------------------------------------------------------------
# Parent AC3: commit-preservation gate before any reset (LP-0MVWEP2KB002WMRY)
# ---------------------------------------------------------------------------


class TestPreservationGate:
    """A worktree that carries commits is never reset — the tip is parked
    on ``refs/wip/<child-id>`` before any destructive command can run.

    Uses the :func:`commit_carrying_worktree` fixture (a real in-repo
    worktree one commit ahead of the parent branch) and the
    :func:`recording_reset` recorder, which runs the *real* implementation
    while recording every ``git`` command — so "no destructive command ran"
    is proven by the recorder, never a mocked reset.
    """

    def test_committed_worktree_refuses_reset_and_preserves_tip(
        self, recording_reset, commit_carrying_worktree,
    ):
        """AC1: ahead-of-parent worktree yields a refused result with the
        preservation ref and the tip — and no destructive command ran."""
        fixture = commit_carrying_worktree
        recorder = recording_reset()

        result = recorder(fixture["worktree"], "dev", child_id="SA-C1")

        assert result["success"] is False
        assert result["reset"] is False
        assert result["preserved_ref"] == "refs/wip/SA-C1"
        assert result["preserved_commit"] == fixture["head_rev"]
        assert "refs/wip/SA-C1" in result["reason"]
        # Neither `git reset --hard` nor `git clean -fd` ran (AC1, AC3).
        assert recorder.destructive(str(fixture["worktree"])) == []

    def test_preserved_ref_resolves_and_commit_stays_reachable(
        self, recording_reset, commit_carrying_worktree,
    ):
        """AC2: after the refused reset the wip ref resolves to the tip,
        the commit is still on the worktree branch, and the ref is invisible
        to ``git branch`` and ``git worktree list``."""
        fixture = commit_carrying_worktree
        worktree = fixture["worktree"]
        recorder = recording_reset()

        result = recorder(worktree, "dev", child_id="SA-C1")
        assert result["success"] is False

        assert _git("rev-parse", "refs/wip/SA-C1", cwd=worktree
                    ).stdout.strip() == fixture["head_rev"]
        assert _git("rev-parse", "HEAD", cwd=worktree
                    ).stdout.strip() == fixture["head_rev"]
        # The preservation ref is a fully qualified ref outside refs/heads/:
        # it never appears in `git branch` or `git worktree list` output.
        branch_output = _git("branch", "--list", cwd=fixture["repo"]).stdout
        assert "refs/wip/" not in branch_output
        worktree_list = _git(
            "worktree", "list", cwd=fixture["repo"],
        ).stdout
        assert "refs/wip/" not in worktree_list
        # The committed slice work is still present and the tree is clean
        # (AC2/AC3 byte-identical).
        assert (worktree / "feature.txt").read_text() \
            == "committed slice work\n"
        assert _git(
            "status", "--porcelain=v1", cwd=worktree,
        ).stdout.strip() == ""

    def test_preservation_ref_created_before_any_destructive_command(
        self, recording_reset, commit_carrying_worktree,
    ):
        """AC3: the preservation ref exists the moment the refused reset
        returns; the only ref-writing command was ``update-ref`` and no
        destructive reset/clean ran against the worktree."""
        fixture = commit_carrying_worktree
        recorder = recording_reset()

        result = recorder(fixture["worktree"], "dev", child_id="SA-C1")

        assert result["success"] is False
        assert any("update-ref" in cmd for cmd in recorder.commands)
        assert _git(
            "rev-parse", "refs/wip/SA-C1", cwd=fixture["worktree"],
        ).stdout.strip() == fixture["head_rev"]
        # Byte-identical working tree: git reports no changes and the slice
        # commit is still there.
        assert _git(
            "status", "--porcelain=v1", cwd=fixture["worktree"],
        ).stdout.strip() == ""
        assert (fixture["worktree"] / "feature.txt").exists()
        assert recorder.destructive(str(fixture["worktree"])) == []

    def test_uncommitted_dirty_worktree_still_resets_without_x_flag(
        self, recording_reset, child_worktree,
    ):
        """AC4: an only-uncommitted-dirty worktree takes the existing reset
        path unchanged — and ``git clean -fd`` still omits ``-x`` so ignored
        files (e.g. the ``node_modules`` symlink) survive."""
        worktree = child_worktree["worktree"]
        (worktree / ".gitignore").write_text("ignored.txt\n")
        ignored = worktree / "ignored.txt"
        ignored.write_text("keep me\n")
        recorder = recording_reset()

        result = recorder(worktree, "dev")

        assert result["success"] is True
        assert result["reset"] is True
        destructive = recorder.destructive(str(worktree))
        assert ["git", "-C", str(worktree), "reset", "--hard", "dev"] \
            in destructive
        assert ["git", "-C", str(worktree), "clean", "-fd"] in destructive
        # No `-x` flag anywhere (the flag-level assertion AC4 asks for).
        assert all("-x" not in cmd for cmd in recorder.commands)
        # `clean -fd` without -x leaves ignored files in place.
        assert ignored.read_text() == "keep me\n"
        # The abandoned uncommitted WIP was discarded as before.
        status = _git("status", "--porcelain=v1", cwd=worktree).stdout
        assert "wip.txt" not in status

    def test_commit_carrying_worktree_without_child_id_refuses_safely(
        self, recording_reset, commit_carrying_worktree,
    ):
        """A commit-carrying worktree is still never reset when no child id
        names the preservation ref — fail-safe, nothing destructive runs."""
        recorder = recording_reset()

        result = recorder(commit_carrying_worktree["worktree"], "dev")

        assert result["success"] is False
        assert result["reset"] is False
        assert "no child id" in result["reason"]
        assert recorder.destructive(
            str(commit_carrying_worktree["worktree"])
        ) == []

    def test_undecidable_ahead_state_refuses_without_destroying(
        self, recording_reset, child_worktree,
    ):
        """Tri-state fail-closed: when the ahead-of-parent relationship
        cannot be determined (unresolvable parent ref), the reset is refused
        — 'unknown' is never interpreted as 'safe' by a destroyer."""
        worktree = child_worktree["worktree"]
        recorder = recording_reset()

        result = recorder(worktree, "no-such-branch", child_id="SA-C1")

        assert result["success"] is False
        assert result["reset"] is False
        assert "could not determine" in result["reason"]
        assert recorder.destructive(str(worktree)) == []
        # The abandoned WIP is untouched (never discarded).
        assert (worktree / "wip.txt").exists()

    def test_drive_preserves_ref_comments_and_stops_chain(
        self, drive_harness, commit_carrying_worktree,
    ):
        """AC5/parent AC3: when the exhausted-budget reset is refused with a
        ``preserved_ref``, phase_drive records ``preserved_refs``, comments
        the wip ref + commit on the child, spawns **no** session for it and
        stops the chain naming the failing ``finish`` step and reason."""
        worktree = commit_carrying_worktree["worktree"]
        fixture_tip = commit_carrying_worktree["head_rev"]
        harness = drive_harness(
            children=[_child("SA-C1")],
            runner=RecordingParent([
                *(finishing_failed_report(
                    "SA-C1", step="tests",  # gate kind — not infra-retried
                ) for _ in range(3)),
            ]),
            child_states={"SA-C1": _child("SA-C1")},
            worktree_path=str(worktree),
        )

        report = harness.run(finish_attempt_limit=3)

        assert report["success"] is False
        assert report["failed_child"] == "SA-C1"
        assert report["sessions_spawned"] == 0
        assert harness.spawn_calls == []
        assert report["reset_for_retry"] == 0
        assert harness.status_updates == []
        preserved = report.get("preserved_refs", [])
        assert len(preserved) == 1
        assert preserved[0] == {
            "id": "SA-C1",
            "ref": "refs/wip/SA-C1",
            "commit": fixture_tip,
        }
        # The child is commented with the wip ref and the commit hash
        # (AC5) — but only once (the preservation comment, not a
        # reset-for-retry discard comment).
        child_comments = [
            text for wid, text in harness.comments if wid == "SA-C1"
        ]
        assert len(child_comments) == 1
        assert "refs/wip/SA-C1" in child_comments[0]
        assert fixture_tip in child_comments[0]
        # The chain-stop message names the real failing finish step/reason.
        assert "tests" in report["message"]
        assert "tests_failed" in report["message"]
        # The worktree genuinely survived: still ahead, wip ref in place.
        assert _git("rev-parse", "HEAD", cwd=worktree
                    ).stdout.strip() == fixture_tip
        assert _git(
            "rev-parse", "refs/wip/SA-C1", cwd=worktree,
        ).stdout.strip() == fixture_tip


# ---------------------------------------------------------------------------
# LP-0MVWEP2VQ001NPRK: reset-for-retry is limited to uncommitted WIP, and its
# comment names exactly what was discarded (parent AC4)
# ---------------------------------------------------------------------------

#: Any 7–40 character hex run that a reader could mistake for a commit hash.
_COMMIT_HASH_TOKEN_RE = re.compile(r"\b[0-9a-f]{7,40}\b")

#: The fixture's abandoned WIP — the exact files an uncommitted-only reset
#: discards (``child_worktree``): one modified tracked file, two untracked.
_CHILD_WORKTREE_WIP = ["README.md", "tests/test_wip.py", "wip.txt"]


class TestResetForRetryDiscardRecord:
    """The audit comment is an evidence-based record of the discard.

    ``reset-for-retry`` only ever runs for a worktree that carried **no**
    commits ahead of the parent branch (the preservation gate refuses a
    commit-carrying tree), so the comment must name exactly the uncommitted
    paths that were removed and state explicitly that no commit was
    discarded — and it must never contain a commit-hash-shaped token that
    could be read as "a commit was thrown away" (parent AC4).
    """

    def test_reset_helper_returns_the_discarded_paths(
        self, implement_mod, child_worktree,  # noqa: F811 — fixture param
    ):
        """AC2/AC3: ``_reset_child_worktree`` reports the pre-reset
        ``status --porcelain`` paths, so the comment is evidence-based rather
        than inferred from ``git clean`` stdout."""
        m = implement_mod
        worktree = child_worktree["worktree"]
        pre_reset = _git(
            "status", "--porcelain=v1", "--untracked-files=all",
            cwd=worktree,
        ).stdout
        assert "wip.txt" in pre_reset and "README.md" in pre_reset

        result = m._reset_child_worktree(str(worktree), "dev", child_id="SA-C1")

        assert result["success"] is True
        assert result["reset"] is True
        assert result["discarded_paths"] == _CHILD_WORKTREE_WIP
        # The reset is an uncommitted-only reset: nothing was ahead of dev.
        assert result["commits_ahead"] == 0
        # Those are exactly the paths the reset actually removed/restored.
        for path in _CHILD_WORKTREE_WIP:
            assert not (worktree / path).exists() or path == "README.md"
        assert (worktree / "README.md").read_text() == "# Test\n"

    def test_comment_lists_each_discarded_path_individually(
        self, implement_mod, child_worktree,  # noqa: F811 — fixture param
    ):
        """AC2/AC3: every discarded path appears on its own line — a file
        list, never a commit hash — and the comment states that no commit was
        discarded."""
        m = implement_mod
        reset_result = m._reset_child_worktree(
            str(child_worktree["worktree"]), "dev", child_id="SA-C1",
        )

        comment = m._reset_for_retry_comment(
            "SA-C1",
            3,
            "Failed to finish child SA-C1",
            {"evidence": "status=open, stage=intake_complete"},
            reset_result,
        )

        for path in _CHILD_WORKTREE_WIP:
            assert f"  - {path}\n" in comment, (
                f"discarded path {path!r} is not listed individually:\n"
                f"{comment}"
            )
        assert "no commit was discarded" in comment
        assert "Failed to finish child SA-C1" in comment
        assert "`dev`" in comment
        assert "git reset --hard" in comment
        # AC2: no hash-shaped token anywhere in the comment body.
        assert _COMMIT_HASH_TOKEN_RE.search(comment) is None, comment

    def test_comment_never_leaks_a_commit_hash_from_the_failure_text(
        self, implement_mod, child_worktree,  # noqa: F811 — fixture param
    ):
        """AC2: a failure message that carries a commit hash (the shape the
        refused/preserved path reports) is redacted, so no reader can infer
        that a commit was discarded — while the live file list stays
        verbatim."""
        m = implement_mod
        hash_token = "dc5b1123abcdef0123456789abcdef01234567"
        reset_result = m._reset_child_worktree(
            str(child_worktree["worktree"]), "dev", child_id="SA-C1",
        )

        comment = m._reset_for_retry_comment(
            "SA-C1",
            3,
            f"Failed to finish child SA-C1: commit {hash_token} refused",
            {"evidence": "status=open, stage=intake_complete"},
            reset_result,
        )

        assert hash_token not in comment
        assert _COMMIT_HASH_TOKEN_RE.search(comment) is None, comment
        for path in _CHILD_WORKTREE_WIP:
            assert path in comment

    def test_comment_names_the_failing_finish_step_when_classified(
        self, implement_mod, child_worktree,  # noqa: F811 — fixture param
    ):
        """The discard record attributes the reset: when the failure was
        classified, the comment names the failing ``finish`` step and
        reason."""
        m = implement_mod
        reset_result = m._reset_child_worktree(
            str(child_worktree["worktree"]), "dev", child_id="SA-C1",
        )

        comment = m._reset_for_retry_comment(
            "SA-C1",
            3,
            "Finishing failed",
            {"evidence": "status=open, stage=intake_complete"},
            reset_result,
            {"kind": "gate", "step": "tests", "reason": "tests_failed"},
        )

        assert "`tests`" in comment
        assert "`tests_failed`" in comment

    def test_comment_reports_no_discard_for_a_clean_worktree(
        self, implement_mod, tmp_path,  # noqa: F811 — fixture param
    ):
        """A clean worktree has nothing to discard — the comment says so
        instead of implying a discard happened."""
        m = implement_mod
        repo = tmp_path / "clean_repo"
        repo.mkdir()
        _git("init", "-q", cwd=repo)
        _git("config", "user.email", "test@test.com", cwd=repo)
        _git("config", "user.name", "Test", cwd=repo)
        (repo / "README.md").write_text("# Test\n")
        _git("add", "-A", cwd=repo)
        _git("commit", "-q", "-m", "Initial commit", cwd=repo)
        _git("branch", "dev", cwd=repo)
        worktree = (tmp_path / "wt-clean").resolve()
        _git(
            "worktree", "add", "--track", "-b", "wl-SA-C1-clean",
            str(worktree), "dev", cwd=repo,
        )

        reset_result = m._reset_child_worktree(
            str(worktree), "dev", child_id="SA-C1",
        )
        comment = m._reset_for_retry_comment(
            "SA-C1", 2, "Finishing failed",
            {"evidence": "status=open, stage=intake_complete"},
            reset_result,
        )

        assert reset_result["discarded_paths"] == []
        assert "Discarded uncommitted paths: none" in comment
        assert "no commit was discarded" in comment
        assert _COMMIT_HASH_TOKEN_RE.search(comment) is None


class TestResetForRetryInvariantGuard:
    """``reset-for-retry`` is only recorded when the reset kept no commits
    (parent AC4): the drive path fails closed if that ever stops holding."""

    def test_violation_helper_accepts_an_uncommitted_only_reset(
        self, implement_mod,  # noqa: F811 — fixture param
    ):
        result = implement_mod._reset_for_retry_violation({
            "success": True,
            "reset": True,
            "ref": "dev",
            "commits_ahead": 0,
            "discarded_paths": ["wip.txt"],
        })

        assert result == ""

    def test_violation_helper_rejects_a_commit_carrying_reset(
        self, implement_mod,  # noqa: F811 — fixture param
    ):
        ahead = implement_mod._reset_for_retry_violation({
            "success": True, "reset": True, "ref": "dev", "commits_ahead": 2,
        })
        preserved = implement_mod._reset_for_retry_violation({
            "success": False,
            "reset": False,
            "ref": "dev",
            "preserved_ref": "refs/wip/SA-C1",
            "preserved_commit": "dc5b1123",
        })

        assert "ahead of the parent branch" in ahead
        assert "preserved" in preserved

    def test_drive_stops_instead_of_recording_a_commit_carrying_reset(
        self, drive_harness,
    ):
        """The guard is wired into ``phase_drive``: a reset helper that
        reports commits ahead is never turned into a ``reset-for-retry``
        entry, and nothing is mutated or spawned."""
        harness = drive_harness(
            children=[_child("SA-C1")],
            runner=RecordingParent([
                *(finishing_failed_report("SA-C1") for _ in range(3)),
            ]),
            child_states={"SA-C1": _child("SA-C1")},
            worktree_path="/wt/SA-C1",
        )
        harness.patch(
            "_reset_child_worktree",
            return_value={
                "success": True,
                "reset": True,
                "ref": "dev",
                "reason": "",
                "commits_ahead": 1,
                "discarded_paths": ["wip.txt"],
            },
        )

        report = harness.run(finish_attempt_limit=3)

        assert report["success"] is False
        assert report["failed_child"] == "SA-C1"
        assert report["reset_for_retry"] == 0
        assert report["children_driven"] == []
        assert harness.status_updates == []
        assert harness.spawn_calls == []
        assert "commit" in report["message"]


class TestUncommittedOnlyResetScope:
    """AC1/AC4: reset-for-retry is unreachable for a commit-carrying
    worktree, and an infrastructure refusal never resets one."""

    def test_commit_carrying_worktree_never_yields_reset_for_retry(
        self, drive_harness, commit_carrying_worktree,
    ):
        """AC1: a commit-carrying worktree whose ``finish`` exhausts the gate
        bound produces **no** ``reset-for-retry`` entry, spawns **no**
        session, and reports ``preserved_refs`` for that child."""
        worktree = commit_carrying_worktree["worktree"]
        harness = drive_harness(
            children=[_child("SA-C1")],
            runner=RecordingParent([
                *(gate_failure_report("SA-C1") for _ in range(3)),
            ]),
            child_states={"SA-C1": _child("SA-C1")},
            worktree_path=str(worktree),
        )

        report = harness.run(finish_attempt_limit=3)

        assert "reset-for-retry" not in [
            entry["result"] for entry in report["children_driven"]
        ]
        assert report["reset_for_retry"] == 0
        assert report["sessions_spawned"] == 0
        assert harness.spawn_calls == []
        assert [entry["id"] for entry in report["preserved_refs"]] == ["SA-C1"]

    def test_infra_refusal_retries_finish_only_and_never_resets(
        self, drive_harness, commit_carrying_worktree, recording_reset,
    ):
        """AC4: an infrastructure refusal on a commit-carrying worktree
        re-runs ``parent`` (finish-only — ``sessions_spawned`` unchanged for
        the retries) and never resets the worktree."""
        fixture = commit_carrying_worktree
        worktree = fixture["worktree"]
        harness = drive_harness(
            children=[_child("SA-C1")],
            runner=RecordingParent([
                *(infra_refusal_report("SA-C1") for _ in range(2)),
                {"success": True, "next_child": "SA-C1",
                 "worktree_path": str(worktree)},
                {"success": True, "parent_advanced": True},
            ]),
            child_states={"SA-C1": _child("SA-C1")},
            worktree_path=str(worktree),
        )
        recorder = recording_reset()
        harness.patch("_reset_child_worktree", side_effect=recorder)

        report = harness.run(delay=lambda _s: None)

        assert report["success"] is True
        assert report["parent_advanced"] is True
        assert report["infra_retries"] == {"SA-C1": 2}
        # finish-only retry: `parent` was re-run per refusal, no reset (the
        # real helper, wrapped by the recorder, was never invoked), and only
        # the eventual child session counted.
        assert len(harness.runner.returned) == 4
        assert recorder.calls == []
        assert report["reset_for_retry"] == 0
        assert report["sessions_spawned"] == 1
        assert harness.spawn_calls == ["SA-C1"]
        # The committed slice survived byte-identically.
        assert _git("rev-parse", "HEAD", cwd=worktree
                    ).stdout.strip() == fixture["head_rev"]
        assert (worktree / "feature.txt").read_text() == "committed slice work\n"
        assert _git(
            "status", "--porcelain=v1", cwd=worktree,
        ).stdout.strip() == ""

    def test_exhausted_infra_budget_preserves_instead_of_resetting(
        self, drive_harness, commit_carrying_worktree, recording_reset,
    ):
        """AC4: when the infra budget is spent the loop falls through to the
        finish-attempt path, which still refuses to reset a commit-carrying
        worktree — the tip is preserved and no session is spawned."""
        fixture = commit_carrying_worktree
        worktree = fixture["worktree"]
        harness = drive_harness(
            children=[_child("SA-C1")],
            runner=RecordingParent([
                *(infra_refusal_report("SA-C1") for _ in range(3)),
            ]),
            child_states={"SA-C1": _child("SA-C1")},
            worktree_path=str(worktree),
        )
        recorder = recording_reset()
        harness.patch("_reset_child_worktree", side_effect=recorder)

        report = harness.run(
            infra_retry_limit=1,
            finish_attempt_limit=1,
            delay=lambda _s: None,
        )

        assert report["success"] is False
        assert report["failed_child"] == "SA-C1"
        assert report["reset_for_retry"] == 0
        assert [e["result"] for e in report["children_driven"]] == [
            "infra-retry"
        ]
        assert report["preserved_refs"] == [{
            "id": "SA-C1",
            "ref": "refs/wip/SA-C1",
            "commit": fixture["head_rev"],
        }]
        assert harness.spawn_calls == []
        # The real helper ran once and refused; no destructive command ran.
        assert len(recorder.calls) == 1
        assert recorder.destructive(str(worktree)) == []
        assert _git("rev-parse", "HEAD", cwd=worktree
                    ).stdout.strip() == fixture["head_rev"]
        assert (worktree / "feature.txt").exists()


# ---------------------------------------------------------------------------
# LP-0MVWEP37K000JU0O: drive verdicts surface the real failing finish
# step/reason and the report evidence (parent AC5)
# ---------------------------------------------------------------------------
#
# Every drive verdict — ``infra-retry``, ``reset-for-retry``,
# ``terminal-reconciled`` and the chain-stop messages — must name the real
# failing ``finish`` step and reason (``"<step>: <reason>"``), plus the
# main-checkout lock holder and/or the guard's offending paths and competing
# work item when the failing finish report carries them, instead of the
# generic ``Failed to finish child <id>`` (parent AC5).
#


class TestDescribeFinishFailure:
    """AC1/AC2: the shared ``_describe_finish_failure`` formatter.

    Returns ``"<step>: <reason>"`` (e.g. ``local_dev_fast_forward:
    local_dev_diverged``) and appends ``"; lock held by <holder>"`` and
    ``"; offending paths: <paths>; competing work item <id> (pid <n>)"``
    when the classified ``finish_failure`` payload's ``steps`` carry them.
    """

    def test_step_and_reason_only(self, implement_mod):  # noqa: F811
        m = implement_mod
        assert m._describe_finish_failure({
            "kind": "gate", "step": "tests", "reason": "tests_failed",
        }) == "tests: tests_failed"

    def test_unspecified_reason_defaults_to_unspecified(
        self, implement_mod,  # noqa: F811
    ):
        m = implement_mod
        assert m._describe_finish_failure({
            "kind": "gate", "step": "tests", "reason": None,
        }) == "tests: unspecified"

    def test_empty_when_not_classified(self, implement_mod):  # noqa: F811
        m = implement_mod
        assert m._describe_finish_failure(None) == ""
        assert m._describe_finish_failure({}) == ""
        assert m._describe_finish_failure({
            "kind": "unknown", "step": None, "reason": None,
        }) == ""

    def test_lock_holder_named_from_steps(self, implement_mod):  # noqa: F811
        """AC2: ``steps.main_checkout_lock.holder`` names the lock holder."""
        m = implement_mod
        holder = (
            "Main-checkout lock at /repo/.worklog/locks/main-checkout.lock "
            "(holder: work item SA-XYZ, pid 123, started "
            "2026-11-02T06:00:00Z)"
        )
        failure = m.classify_finish_failure(failing_finish_report(
            "main_checkout_lock", "lock_held", lock_holder=holder,
        ))
        desc = m._describe_finish_failure(failure)
        assert desc.startswith("main_checkout_lock: lock_held")
        assert f"lock held by {holder}" in desc

    def test_offending_paths_and_competing_actor(self, implement_mod):  # noqa: F811
        """AC2: ``steps.main_checkout_guard`` offending paths and actor
        evidence name the offending paths and the competing work item/pid."""
        m = implement_mod
        failure = m.classify_finish_failure(failing_finish_report(
            "main_checkout_guard", "fail",
            guard_verifies=[{
                "stage": "pre_push",
                "at": "2026-11-02T06:00:00Z",
                "verdict": "fail",
                "offending": ["src/a.py", "src/b.py"],
                "actor_evidence": (
                    "work item SA-123 (worktree /wt/wl-SA-123, started "
                    "2026-11-02T06:00:00Z); pid 456 with cwd in the main "
                    "checkout"
                ),
            }],
        ))
        desc = m._describe_finish_failure(failure)
        assert "main_checkout_guard: fail" in desc
        assert "offending paths: src/a.py, src/b.py" in desc
        assert "competing work item SA-123 (pid 456)" in desc

    def test_guard_without_evidence_omits_suffixes(
        self, implement_mod,  # noqa: F811
    ):
        m = implement_mod
        failure = m.classify_finish_failure(
            failing_finish_report("main_checkout_guard", "fail"),
        )
        assert m._describe_finish_failure(failure) == "main_checkout_guard: fail"

    def test_malformed_steps_are_graceful(self, implement_mod):  # noqa: F811
        m = implement_mod
        assert m._describe_finish_failure({
            "kind": "infrastructure", "step": "push", "reason": "timeout",
            "steps": "not-a-dict",
        }) == "push: timeout"
        assert m._describe_finish_failure({
            "kind": "infrastructure", "step": "push", "reason": "timeout",
            "steps": {"main_checkout_lock": None},
        }) == "push: timeout"
        assert m._describe_finish_failure({
            "kind": "infrastructure", "step": "push", "reason": "timeout",
            "steps": {
                "main_checkout_guard": {
                    "verdict": "fail", "verifies": [],
                },
            },
        }) == "push: timeout"


class TestDriveVerdictEvidence:
    """AC1/AC3/AC4: the four drive verdicts name the real failing step and
    reason in their messages and child comments (parent AC5)."""

    def test_infra_retry_entries_and_comments_name_step_reason(
        self, drive_harness,
    ):
        """AC1/AC4: each infra-retry entry and its child comment carry
        ``<step>: <reason>`` (and spawn no session)."""
        harness = drive_harness(
            children=[_child("SA-C1")],
            runner=RecordingParent([
                *(infra_refusal_report("SA-C1") for _ in range(2)),
                {"success": True, "next_child": "SA-C1",
                 "worktree_path": "/wt/SA-C1"},
                {"success": True, "parent_advanced": True},
            ]),
        )

        report = harness.run(delay=lambda _s: None)

        retries = [
            e for e in report["children_driven"]
            if e["result"] == "infra-retry"
        ]
        assert len(retries) == 2
        for entry in retries:
            assert (
                "local_dev_fast_forward: local_dev_diverged"
                in entry["message"]
            )
        comments = [
            text for wid, text in harness.comments if wid == "SA-C1"
        ]
        assert len(comments) == 2
        for text in comments:
            assert (
                "local_dev_fast_forward: local_dev_diverged" in text
            )
        # Finish-only retries never spawn a session.
        assert harness.spawn_calls == ["SA-C1"]

    def test_reset_for_retry_entry_and_comment_name_step_reason(
        self, drive_harness, child_worktree,
    ):
        """AC1/AC4: the reset-for-retry entry and its discard comment carry
        the failing step/reason."""
        worktree = child_worktree["worktree"]
        harness = drive_harness(
            children=[_child("SA-C1")],
            runner=RecordingParent([
                *(finishing_failed_report(
                    "SA-C1", step="tests", kind="gate",
                ) for _ in range(3)),
                {"success": True, "next_child": "SA-C1",
                 "worktree_path": str(worktree)},
                {"success": True, "parent_advanced": True},
            ]),
            child_states={"SA-C1": _child("SA-C1")},
            worktree_path=str(worktree),
        )

        report = harness.run(finish_attempt_limit=3)

        reset = [
            e for e in report["children_driven"]
            if e["result"] == "reset-for-retry"
        ][0]
        assert "tests: tests_failed" in reset["message"]
        comments = [
            text for wid, text in harness.comments if wid == "SA-C1"
        ]
        assert len(comments) == 1
        assert "tests: tests_failed" in comments[0]

    def test_terminal_reconciled_entry_and_comment_name_step_reason(
        self, drive_harness,
    ):
        """AC1/AC4: the terminal-reconciled entry and its child comment
        carry the failing step/reason."""
        child = _child("SA-C1", status="completed", stage="in_review")
        harness = drive_harness(
            children=[child],
            runner=RecordingParent([
                *(finishing_failed_report(
                    "SA-C1", step="tests", kind="gate",
                ) for _ in range(3)),
                {"success": True, "parent_advanced": True,
                 "message": "all children terminal"},
            ]),
            child_states={"SA-C1": child},
        )

        report = harness.run(finish_attempt_limit=3)

        entry = report["children_driven"][0]
        assert entry["result"] == "terminal-reconciled"
        assert "tests: tests_failed" in entry["message"]
        comments = [
            text for wid, text in harness.comments if wid == "SA-C1"
        ]
        assert len(comments) == 1
        assert "tests: tests_failed" in comments[0]

    def test_chain_stop_message_names_step_reason_never_generic_alone(
        self, drive_harness,
    ):
        """AC1/AC3: the chain-stop message names the failing step/reason and
        the generic ``Failed to finish child <id>`` never appears alone when
        a classified ``finish_failure`` is available."""
        harness = drive_harness(
            children=[_child("SA-C1")],
            runner=RecordingParent([
                *(finishing_failed_report(
                    "SA-C1", step="tests", kind="gate",
                ) for _ in range(3)),
            ]),
            child_states={"SA-C1": _child("SA-C1")},
            worktree_path="",  # no resettable worktree → reset-refused stop
        )

        report = harness.run(finish_attempt_limit=3)

        assert report["success"] is False
        assert report["failed_child"] == "SA-C1"
        assert "tests: tests_failed" in report["message"]
        assert (
            "Failed to finish child SA-C1" not in report["message"]
            or "tests: tests_failed" in report["message"]
        )

    def test_chain_stop_names_lock_holder_and_offending_actor(
        self, drive_harness,
    ):
        """AC2: when the failing finish report carries the lock holder /
        guard offending paths + actor evidence, the chain-stop message names
        them (never parsed from prose)."""
        holder = (
            "Main-checkout lock at /repo/.worklog/locks/main-checkout.lock "
            "(holder: work item SA-LOCKED, pid 2345, started "
            "2026-11-02T06:00:00Z)"
        )
        harness = drive_harness(
            children=[_child("SA-C1")],
            runner=RecordingParent([
                *(finishing_failed_report(
                    "SA-C1", step="main_checkout_lock", reason="lock_held",
                    kind="infrastructure", lock_holder=holder,
                ) for _ in range(3)),
            ]),
            child_states={"SA-C1": _child("SA-C1")},
        )

        report = harness.run(
            infra_retry_limit=1, finish_attempt_limit=1,
            delay=lambda _s: None,
        )

        assert report["success"] is False
        assert report["failed_child"] == "SA-C1"
        assert f"lock held by {holder}" in report["message"]
        assert "main_checkout_lock: lock_held" in report["message"]

        harness2 = drive_harness(
            children=[_child("SA-C1")],
            runner=RecordingParent([
                *(finishing_failed_report(
                    "SA-C1", step="main_checkout_guard", reason="fail",
                    kind="infrastructure",
                    guard_verifies=[{
                        "stage": "pre_push",
                        "verdict": "fail",
                        "offending": ["src/x.py"],
                        "actor_evidence": (
                            "work item SA-CLASH (worktree /wt/wt-clash, "
                            "started 2026-11-02T06:00:00Z); pid 99 with "
                            "cwd in the main checkout"
                        ),
                    }],
                ) for _ in range(3)),
            ]),
            child_states={"SA-C1": _child("SA-C1")},
        )

        report2 = harness2.run(
            infra_retry_limit=1, finish_attempt_limit=1,
            delay=lambda _s: None,
        )

        assert report2["success"] is False
        assert "main_checkout_guard: fail" in report2["message"]
        assert "offending paths: src/x.py" in report2["message"]
        assert "competing work item SA-CLASH (pid 99)" in report2["message"]

    def test_emit_phase_report_prints_failing_child_evidence(
        self, drive_harness, capsys,
    ):
        """AC5: the human drive summary prints the failing child's evidence
        lines (the per-entry message) and the chain-stop message."""
        harness = drive_harness(
            children=[_child("SA-C1")],
            runner=RecordingParent([
                *(infra_refusal_report("SA-C1") for _ in range(2)),
                {"success": True, "next_child": "SA-C1",
                 "worktree_path": "/wt/SA-C1"},
                {"success": True, "parent_advanced": True},
            ]),
        )
        harness.run(delay=lambda _s: None)
        out = capsys.readouterr().out
        assert "local_dev_fast_forward: local_dev_diverged" in out


# ---------------------------------------------------------------------------
# LP-0MVWEP3J3005Q4X6: gate-failure regression and termination proof
# (parent AC6 — a genuine gate failure still stops the chain at the
# finish-attempt bound with no infrastructure retry, a commit-carrying
# worktree is never reset, and drive always terminates)
# ---------------------------------------------------------------------------
#
# The refusal-aware drive (LP-0MVWEP292000ODBS) retries *infrastructure*
# refusals on a separate bounded budget. These tests pin the other side:
# a *genuine* gate failure (``tests: tests_failed``, the worktree-placement
# violation, or an unlisted ``unknown`` pair) is never retried as
# infrastructure — it consumes exactly the existing finish-attempt bound,
# leaves ``infra_retries`` empty, never resets a commit-carrying worktree,
# and the drive loop itself always terminates within its combined bound.
#


class TestGateFailureRegression:
    """A genuine gate failure still stops the chain at the finish-attempt
    bound with **no** infrastructure retry (parent AC6).

    AC1: a ``tests: tests_failed`` finish failure at the *default*
    ``--finish-attempt-limit`` yields exactly one finished attempt, an empty
    ``infra_retries``, no reset of a commit-carrying worktree, and a stop
    message naming ``tests: tests_failed``. AC2: a worktree-placement-owner
    finish failure likewise stops with no infrastructure retry and a stop
    message naming the failing step. AC3: an unlisted ``(step, reason)`` pair
    (``unknown``) counts against the gate bound — ``finish_attempts``
    increments and ``infra_retries`` stays empty.
    """

    def test_gate_failure_at_default_budget_names_step_and_preserves(
        self, drive_harness, commit_carrying_worktree, recording_reset,
    ):
        """AC1: one gate failure at the default bound — a single finish
        attempt, empty ``infra_retries``, the commit-carrying worktree never
        reset, and the stop message naming ``tests: tests_failed``."""
        worktree = commit_carrying_worktree["worktree"]
        fixture_tip = commit_carrying_worktree["head_rev"]
        harness = drive_harness(
            children=[_child("SA-C1")],
            runner=RecordingParent([
                *(gate_failure_report("SA-C1") for _ in range(5)),
            ]),
            child_states={"SA-C1": _child("SA-C1")},
            worktree_path=str(worktree),
        )
        recorder = recording_reset()
        harness.patch("_reset_child_worktree", side_effect=recorder)

        report = harness.run()  # default limits, no explicit bounds

        assert report["success"] is False
        assert report["failed_child"] == "SA-C1"
        # The default finish-attempt bound is the session cap: exactly one
        # `parent` invocation consumed, one finish execution observed.
        assert report["finish_attempt_limit"] == 1
        assert report["finish_attempts"] == {"SA-C1": 1}
        assert len(harness.runner.returned) == 1
        # A gate failure never touches the infrastructure budget.
        assert report["infra_retries"] == {}
        assert report["infra_retry_limit"] == 5
        # The stop message names the real failing step and reason.
        assert "tests: tests_failed" in report["message"]
        assert harness.spawner.calls == []
        # The commit-carrying worktree was never reset: the preservation
        # gate ran the real helper (recording it), parked the tip on the wip
        # ref, and no destructive git command executed against the worktree.
        assert [e["id"] for e in report["preserved_refs"]] == ["SA-C1"]
        assert report["preserved_refs"][0]["commit"] == fixture_tip
        assert len(recorder.calls) == 1
        assert recorder.destructive(str(worktree)) == []
        assert _git("rev-parse", "HEAD", cwd=worktree
                    ).stdout.strip() == fixture_tip
        assert (worktree / "feature.txt").read_text() == "committed slice work\n"

    def test_worktree_placement_failure_stops_without_infra_retry(
        self, drive_harness,
    ):
        """AC2: a worktree-placement-violation gate failure stops with no
        infrastructure retry and names the failing step in the message."""
        harness = drive_harness(
            children=[_child("SA-C1")],
            runner=RecordingParent([
                *(finishing_failed_report(
                    "SA-C1", step="worktree_placement",
                    reason="placement_violation", kind="gate",
                ) for _ in range(5)),
            ]),
            child_states={"SA-C1": _child("SA-C1")},
        )

        report = harness.run()  # default limits

        assert report["success"] is False
        assert report["failed_child"] == "SA-C1"
        assert report["finish_attempts"] == {"SA-C1": 1}
        assert report["infra_retries"] == {}
        assert "worktree_placement: placement_violation" in report["message"]
        assert harness.spawner.calls == []
        assert len(harness.runner.returned) == 1

    def test_unknown_pair_counts_against_the_gate_bound(
        self, drive_harness,
    ):
        """AC3: an unlisted ``(step, reason)`` pair — ``unknown``, never
        ``infrastructure`` — consumes the finish-attempt budget: repeated
        ``unknown`` failures are retried up to ``finish_attempt_limit`` and
        ``infra_retries`` stays empty."""
        harness = drive_harness(
            children=[_child("SA-C1")],
            runner=RecordingParent([
                *(finishing_failed_report(
                    "SA-C1", step="push", reason="push_failed",
                ) for _ in range(5)),
            ]),
            child_states={"SA-C1": _child("SA-C1")},
        )

        report = harness.run(finish_attempt_limit=2)

        # The classifier resolves the unlisted pair as ``unknown``.
        assert report["children_driven"] == []  # no infra-retry entries
        assert report["finish_attempts"] == {"SA-C1": 2}
        assert report["infra_retries"] == {}
        assert report["success"] is False
        assert report["failed_child"] == "SA-C1"
        # Both unknown failures were counted as real finish executions.
        assert len(harness.runner.returned) == 2
        assert harness.spawner.calls == []
        assert "push: push_failed" in report["message"]


class TestDriveTermination:
    """The drive loop always terminates within its combined bound (parent
    AC6).

    AC4: an always-infrastructure-refusing harness with ``--infra-retry-
    limit 5`` and ``--finish-attempt-limit 1`` terminates after exactly
    ``infra_retry_limit + finish_attempt_limit`` parent invocations (plus the
    terminal classification re-fetch of the child), with ``success: False``,
    ``failed_child`` set and no session spawned. AC5: alternating gate and
    infrastructure failures also terminate within the combined bound.
    """

    def test_always_infra_refusing_harness_terminates(
        self, drive_harness,
    ):
        """AC4: the infra budget is spent, then the single finish-attempt
        bound — 6 parent invocations in total, no session ever spawned, and
        the reconciliation still re-fetched the child (the terminal
        classification pass)."""
        harness = drive_harness(
            children=[_child("SA-C1")],
            runner=RecordingParent([
                *(infra_refusal_report("SA-C1") for _ in range(10)),
            ]),
            child_states={"SA-C1": _child("SA-C1")},
        )

        report = harness.run(
            infra_retry_limit=5, finish_attempt_limit=1,
            delay=lambda _s: None,
        )

        assert report["success"] is False
        assert report["failed_child"] == "SA-C1"
        # 5 infra retries on the separate budget, then 1 finish attempt on the
        # gate bound: exactly infra_retry_limit + finish_attempt_limit parent
        # invocations — the loop terminated, it never exceeded the iteration
        # bound or ran out of scripted reports.
        assert len(harness.runner.returned) == 6
        assert "exceeded its iteration bound" not in report["message"]
        assert report["infra_retries"] == {"SA-C1": 5}
        assert report["finish_attempts"] == {"SA-C1": 1}
        # No session was spawned for the never-finishing child.
        assert report["sessions_spawned"] == 0
        assert harness.spawner.calls == []
        # The terminal classification pass re-fetched the child after the
        # bound was exhausted (parent fetch + one reconcile fetch).
        assert report["_show_calls"] == [PARENT, "SA-C1"]
        # The stopping verdict still names the real failing step/reason.
        assert "local_dev_fast_forward: local_dev_diverged" in report["message"]

    def test_mixed_failure_kinds_terminate_within_the_combined_bound(
        self, drive_harness,
    ):
        """AC5: alternating gate and infrastructure failures terminate within
        the combined bound (infra budget + finish-attempt bound) and the
        final verdict names the step that exhausted the last bound."""
        alternating = [
            infra_refusal_report("SA-C1") if i % 2 == 0
            else gate_failure_report("SA-C1")
            for i in range(10)
        ]
        harness = drive_harness(
            children=[_child("SA-C1")],
            runner=RecordingParent(alternating),
            child_states={"SA-C1": _child("SA-C1")},
        )

        report = harness.run(
            infra_retry_limit=2, finish_attempt_limit=2,
            delay=lambda _s: None,
        )

        assert report["success"] is False
        assert report["failed_child"] == "SA-C1"
        # infra, gate, infra, gate → both budgets spent: 4 parent invocations
        # total, alternation never confused the two budgets.
        assert len(harness.runner.returned) == 4
        assert report["infra_retries"] == {"SA-C1": 2}
        assert report["finish_attempts"] == {"SA-C1": 2}
        assert report["sessions_spawned"] == 0
        assert harness.spawner.calls == []
        assert "tests: tests_failed" in report["message"]
        assert "exceeded its iteration bound" not in report["message"]
