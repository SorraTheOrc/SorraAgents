"""Engine lag reported when a finish build fails after ``engine:sync``.

SA-0MW06FLFR009F0P4 (discovered while verifying AC1 of MS-0MVSB1OVW00178W6):
in a game repo ``npm run build`` starts with ``npm run engine:sync``, which
advances ``./core`` to the tracked-branch tip and rewrites/stages
``configs/engine-pin.json`` when the worktree's pin lags. If the item's source
predates a breaking engine API change, the build then compiles the item
against an engine it was never written for and fails with a rename error that
has nothing to do with the item's own change.

Two defects were reported: the advance was **invisible** (the failure comment
posted only ``stderr``, while ``engine:sync`` logs the advance to stdout) and
the **data was already there** but unused on the failure path
(``_build_churn_baseline`` records the pre-build gitlink,
``_composed_engine_state`` compares it with the checkout).

What is pinned here
-------------------
* **AC1** — a failed build's report step carries ``engine_lag`` with
  ``submodule``/``pinned_sha``/``composed_sha``/``advanced`` per submodule, in
  the returned report and in the printed JSON.
* **AC2** — the failure comment names both SHAs and states that the advance is
  not part of the item's commit.
* **AC3** — the failure comment reproduces the build's stdout tail, so the
  ``engine:sync: core updated … → …`` line is visible.
* **AC4** — no advance, or no submodule, reports ``advanced: false`` and adds
  no engine diagnosis: finish behaves exactly as before.
* **AC5** — a build with no ``engine:sync`` output at all (a sync that failed
  before printing) is handled without error.

Hermetic by construction: the real-submodule fixture and the finish harness
(build → gate → commit real, push/removal/``wl`` stubbed) come from
``test_finish_gate_build_churn.py`` — the build there is a project-supplied
callable with a supplied outcome, so a *failing* build is exercised against a
genuine ``engine:sync``-shaped advance.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[3]
for _path in (str(REPO_ROOT), str(REPO_ROOT / "skill")):
    if _path not in sys.path:
        sys.path.append(_path)

from shared import git_sandbox as gs

from skill.implement.scripts import implement
from skill.implement.tests.test_finish_gate_build_churn import (
    _GREEN_TEST_SOURCE,
    CORE_REL,
    GREEN_TEST_NAME,
    _advance_engine,
    _build_composition_fixture,
    _finish_harness,
)

#: A build that advanced the engine and then failed — the shape reproduced on
#: tce-main-street: ``engine:sync`` reports the advance and the pin rewrite on
#: stdout, then ``tsc`` reports a rename error against the newer engine.
_SYNC_STDOUT = (
    "engine:sync: core updated {pinned} → {composed}\n"
    "engine:sync: pinned configs/engine-pin.json → {composed}\n"
    "\n"
    "> tsc --noEmit\n"
)
_COMPILE_STDERR = (
    "src/MainStreetStorylineContentAdapter.ts(88,5): error TS2353: "
    "'hasChoices' does not exist in type 'ContentFieldMapping'.\n"
    "npm run build → exit 2\n"
)


def _failed_build_outcome(
    pinned: str = "", composed: str = "", stdout: str | None = None
) -> dict:
    """A failing ``run_build`` result with the fixture's sync output.

    Args:
        pinned: The engine SHA the item pinned (short form in the log line).
        composed: The engine SHA the build advanced to.
        stdout: Override for the captured stdout (e.g. no sync output at all).
    """
    if stdout is None:
        stdout = _SYNC_STDOUT.format(
            pinned=pinned[:10], composed=composed[:10]
        )
    return {
        "success": False,
        "stdout": stdout,
        "stderr": _COMPILE_STDERR,
        "exit_code": 2,
        "skipped": False,
    }


def _comment_texts(comment) -> list[str]:
    """Every work-item comment body recorded through ``wl_add_comment``."""
    return [
        str(call.args[1]) for call in comment.call_args_list if len(call.args) > 1
    ]


def _build_failure_comment(comment) -> str:
    """The single build-failure comment body."""
    texts = [text for text in _comment_texts(comment) if "Build failed" in text]
    assert len(texts) == 1, _comment_texts(comment)
    return texts[0]


def _plain_repo(tmp_path: Path) -> Path:
    """A repository without any submodule composition."""
    repo = gs.init_repo(tmp_path / "plain-repo", default_branch="dev")
    (repo / ".gitignore").write_text(
        "__pycache__/\n.pytest_cache/\n.worklog/\n", encoding="utf-8"
    )
    (repo / "README.md").write_text("# Plain\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / f"{GREEN_TEST_NAME}.py").write_text(
        _GREEN_TEST_SOURCE.format(test_name=GREEN_TEST_NAME), encoding="utf-8"
    )
    gs.commit_all(repo, "seed plain repo")
    (repo / "README.md").write_text("# Plain\n\nChange set.\n", encoding="utf-8")
    return repo


# ---------------------------------------------------------------------------
# AC1 + AC2 — a failed build after an advance reports the lag and names both
# SHAs in the comment
# ---------------------------------------------------------------------------


def test_failed_build_after_engine_advance_reports_the_lag(
    tmp_path, monkeypatch, capsys
):
    """The failed build step carries ``engine_lag``: pinned vs composed, advanced.

    The item pinned engine v1; the build's ``engine:sync`` moved ``core`` to
    v2 and then the compile step failed. The report must attribute the failure
    to a concrete engine composition instead of leaving an unexplained
    rename error (AC1), and the failure comment must name both SHAs and state
    that the advance is not part of the item's commit (AC2).
    """
    monkeypatch.delenv("IMPLEMENT_TEST_COMMAND", raising=False)
    game, engine_v1, engine_v2 = _build_composition_fixture(tmp_path)

    with _finish_harness(
        game,
        tmp_path,
        build=lambda cwd: _advance_engine(game, engine_v2),
        build_outcome=_failed_build_outcome(engine_v1, engine_v2),
    ), patch.object(implement, "wl_add_comment") as comment:
        report = implement.phase_finish("SA-TEST", json_output=True, no_refactor=True)
        update_status = implement.StatusLifecycle.update_status

    assert report["success"] is False, report
    build_step = report["steps"]["build"]
    assert build_step["success"] is False, build_step
    assert build_step["engine_lag"] == {
        "submodules": [
            {
                "submodule": CORE_REL,
                "pinned_sha": engine_v1,
                "composed_sha": engine_v2,
                "advanced": True,
            }
        ],
        "advanced": True,
    }, build_step["engine_lag"]

    # The same field is in the JSON the command prints (AC1).
    printed = capsys.readouterr().out
    assert json.loads(printed)["steps"]["build"]["engine_lag"] == (
        build_step["engine_lag"]
    )

    # The comment names both SHAs and the non-ownership of the advance (AC2).
    body = _build_failure_comment(comment)
    assert engine_v1 in body, body
    assert engine_v2 in body, body
    assert (
        f"build ran against engine {engine_v2}; the item pinned {engine_v1} — "
        "the advance is not part of this item's commit" in body
    ), body

    # The item is released back to open, exactly as before this change.
    update_status.assert_any_call("SA-TEST", "open", assignee="")


# ---------------------------------------------------------------------------
# AC3 — the comment reproduces the stdout sync line
# ---------------------------------------------------------------------------


def test_failed_build_comment_reproduces_the_engine_sync_line(
    tmp_path, monkeypatch
):
    """``engine:sync``'s ``core updated … → …`` line is visible in the comment.

    The advance is only logged to stdout, which the old failure comment never
    showed; an operator could not tell an engine-induced failure from a source
    failure (AC3).
    """
    monkeypatch.delenv("IMPLEMENT_TEST_COMMAND", raising=False)
    game, engine_v1, engine_v2 = _build_composition_fixture(tmp_path)

    with _finish_harness(
        game,
        tmp_path,
        build=lambda cwd: _advance_engine(game, engine_v2),
        build_outcome=_failed_build_outcome(engine_v1, engine_v2),
    ), patch.object(implement, "wl_add_comment") as comment:
        implement.phase_finish("SA-TEST", json_output=True, no_refactor=True)

    body = _build_failure_comment(comment)
    sync_line = (
        f"engine:sync: core updated {engine_v1[:10]} → {engine_v2[:10]}"
    )
    assert sync_line in body, body
    # The stdout tail is reproduced in addition to stderr (both streams).
    assert "**stdout (tail)**" in body, body
    assert "**stderr (tail)**" in body, body
    assert "error TS2353" in body, body


# ---------------------------------------------------------------------------
# AC4 — no advance: the field reports it and the message stays clean
# ---------------------------------------------------------------------------


def test_failed_build_without_an_advance_reports_no_lag(
    tmp_path, monkeypatch
):
    """A build that fails on the item's own source reports ``advanced: false``.

    The composition is unchanged, so finish behaves exactly as before: the
    lag field is still there (never absent), no engine diagnosis is invented
    and the comment carries the build output only (AC4).
    """
    monkeypatch.delenv("IMPLEMENT_TEST_COMMAND", raising=False)
    game, engine_v1, _v2 = _build_composition_fixture(tmp_path)

    failing = _failed_build_outcome(stdout="> tsc --noEmit\n")
    with _finish_harness(
        game, tmp_path, build_outcome=failing
    ), patch.object(implement, "wl_add_comment") as comment:
        report = implement.phase_finish("SA-TEST", json_output=True, no_refactor=True)

    assert report["success"] is False, report
    assert report["steps"]["build"]["engine_lag"] == {
        "submodules": [
            {
                "submodule": CORE_REL,
                "pinned_sha": engine_v1,
                "composed_sha": engine_v1,
                "advanced": False,
            }
        ],
        "advanced": False,
    }, report["steps"]["build"]["engine_lag"]

    body = _build_failure_comment(comment)
    assert "build ran against engine" not in body, body
    assert "Engine advanced past the item's pin" not in body, body
    # The build's own output is still reported.
    assert "> tsc --noEmit" in body, body
    assert "error TS2353" in body, body


def test_item_originated_composition_is_not_a_build_advance(
    tmp_path, monkeypatch
):
    """A pin the item staged itself is never reported as the build's advance.

    The item composed engine v2 before finish ran (its own change set, exactly
    the AC2 shape of SA-0MVZE164P0048306) and the build then failed without
    touching the composition. ``advanced`` compares the checkout against the
    **pre-build** staged pin, so the item's own composition is never attributed
    to the build — and no diagnosis claims a change outside this item's commit
    (AC4).
    """
    monkeypatch.delenv("IMPLEMENT_TEST_COMMAND", raising=False)
    game, _engine_v1, engine_v2 = _build_composition_fixture(tmp_path)
    _advance_engine(game, engine_v2)

    failing = _failed_build_outcome(stdout="> tsc --noEmit\n")
    with _finish_harness(
        game, tmp_path, build_outcome=failing
    ), patch.object(implement, "wl_add_comment") as comment:
        report = implement.phase_finish("SA-TEST", json_output=True, no_refactor=True)

    assert report["success"] is False, report
    assert report["steps"]["build"]["engine_lag"] == {
        "submodules": [
            {
                "submodule": CORE_REL,
                "pinned_sha": engine_v2,
                "composed_sha": engine_v2,
                "advanced": False,
            }
        ],
        "advanced": False,
    }, report["steps"]["build"]["engine_lag"]

    body = _build_failure_comment(comment)
    assert "build ran against engine" not in body, body
    assert "Engine advanced past the item's pin" not in body, body


def test_repo_without_submodules_reports_no_lag(tmp_path, monkeypatch):
    """No submodule composition: ``advanced: false``, no engine diagnosis.

    A plain repository must behave byte-for-byte as before — the additive
    field reports an empty composition and no engine section is added to the
    comment (AC4).
    """
    monkeypatch.delenv("IMPLEMENT_TEST_COMMAND", raising=False)
    repo = _plain_repo(tmp_path)

    failing = _failed_build_outcome(stdout="> tsc --noEmit\n")
    with _finish_harness(
        repo, tmp_path, build_outcome=failing
    ), patch.object(implement, "wl_add_comment") as comment:
        report = implement.phase_finish("SA-TEST", json_output=True, no_refactor=True)

    assert report["success"] is False, report
    assert report["steps"]["build"]["engine_lag"] == {
        "submodules": [],
        "advanced": False,
    }, report["steps"]["build"]["engine_lag"]

    body = _build_failure_comment(comment)
    assert "build ran against engine" not in body, body
    assert "Engine advanced past the item's pin" not in body, body
    assert "error TS2353" in body, body


# ---------------------------------------------------------------------------
# AC5 — a build with no engine:sync output at all
# ---------------------------------------------------------------------------


def test_failed_advance_without_sync_output_is_handled(tmp_path, monkeypatch):
    """An advance with no ``engine:sync`` output at all is still reported.

    ``engine:sync`` can fail before printing (or a build can advance the
    submodule silently). The lag is then derived from the git state — never
    from parsing output — so the diagnosis is complete and no empty output
    block is emitted (AC5).
    """
    monkeypatch.delenv("IMPLEMENT_TEST_COMMAND", raising=False)
    game, engine_v1, engine_v2 = _build_composition_fixture(tmp_path)

    with _finish_harness(
        game,
        tmp_path,
        build=lambda cwd: _advance_engine(game, engine_v2),
        build_outcome=_failed_build_outcome(stdout=""),
    ), patch.object(implement, "wl_add_comment") as comment:
        report = implement.phase_finish("SA-TEST", json_output=True, no_refactor=True)

    assert report["success"] is False, report
    lag = report["steps"]["build"]["engine_lag"]
    assert lag["advanced"] is True, lag
    assert lag["submodules"][0]["composed_sha"] == engine_v2, lag

    body = _build_failure_comment(comment)
    assert f"build ran against engine {engine_v2}" in body, body
    assert "engine:sync output" not in body, body
    assert "**stdout (tail)**" not in body, body
    assert "error TS2353" in body, body
