"""Tests for the shared herdr pane-title status indicator (SA-0MTFLQEQQ0083EW1).

The helper prepends a status prefix (``✅`` / ``⚠️`` / ``🚫`` or
``Done`` / ``Note`` / ``Attention``) to the current herdr pane label at the end
of a skill session. These tests exercise the observable behaviour — status
derivation, idempotent prefixing, length-bounded truncation, and fail-open
behaviour when herdr is unavailable — via the public API with an injected
subprocess runner (never a live herdr call).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_SKILLS_ROOT = REPO_ROOT / "skill"
if str(_SKILLS_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILLS_ROOT))

from shared import herdr_pane

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakeHerdr:
    """A scriptable stand-in for ``subprocess.run`` that fakes the herdr CLI.

    ``pane list`` returns a single pane carrying the current ``label``;
    ``pane rename`` records the new label (and returns the configured
    result, allowing failure paths to be exercised).
    """

    def __init__(self, label="Manually triggered intake", rename_result=None):
        self.label = label
        self.rename_result = rename_result
        self.calls: list[list[str]] = []

    def __call__(self, cmd, **_kwargs):
        self.calls.append(list(cmd))
        if cmd[1:3] == ["pane", "list"]:
            payload = {"result": {"panes": [
                {"pane_id": "w1:p1", "label": self.label},
            ]}}
            return subprocess.CompletedProcess(cmd, 0, json.dumps(payload), "")
        if cmd[1:3] == ["pane", "rename"]:
            if self.rename_result is not None:
                return self.rename_result
            self.label = cmd[4]
            return subprocess.CompletedProcess(
                cmd, 0, json.dumps({"result": {"pane_id": cmd[3]}}), "")
        raise AssertionError(f"unexpected herdr call: {cmd}")


def _update(label="Manually triggered intake", status="done", **kwargs):
    fake = FakeHerdr(label=label)
    result = herdr_pane.update_pane_title(
        status,
        pane_id="w1:p1",
        env={},
        herdr_bin="/fake/herdr",
        runner=fake,
        **kwargs,
    )
    return result, fake


# ---------------------------------------------------------------------------
# derive_status
# ---------------------------------------------------------------------------


class TestDeriveStatus:
    """Status derivation from AC verdicts, producer actions and notes."""

    def test_all_acs_met_without_content_is_done(self):
        status = herdr_pane.derive_status(
            [("1", "works", "test", "met")], None, None)
        assert status == "done"

    def test_empty_criteria_is_done(self):
        assert herdr_pane.derive_status([], None, None) == "done"

    def test_unmet_ac_is_attention(self):
        status = herdr_pane.derive_status(
            [("1", "works", "test", "unmet")], None, None)
        assert status == "attention"

    def test_partial_verdict_is_attention(self):
        status = herdr_pane.derive_status(
            [("1", "works", "test", "partial")], None, None)
        assert status == "attention"

    def test_adjusted_verdict_is_attention(self):
        status = herdr_pane.derive_status(
            [("1", "works", "test", "adjusted")], None, None)
        assert status == "attention"

    def test_attention_wins_over_producer_content(self):
        status = herdr_pane.derive_status(
            [("1", "works", "test", "unmet")],
            producer_actions="Please review",
            notes="note",
        )
        assert status == "attention"

    def test_met_acs_with_producer_actions_is_note(self):
        status = herdr_pane.derive_status(
            [("1", "works", "test", "met")], "Please review the PR", None)
        assert status == "note"

    def test_met_acs_with_notes_is_note(self):
        status = herdr_pane.derive_status(
            [("1", "works", "test", "met")], None, "Assumption: X")
        assert status == "note"

    def test_none_needed_producer_actions_is_done(self):
        status = herdr_pane.derive_status(
            [("1", "works", "test", "met")], "None needed", None)
        assert status == "done"

    def test_empty_notes_is_done(self):
        status = herdr_pane.derive_status(
            [("1", "works", "test", "met")], None, "None")
        assert status == "done"

    def test_case_insensitive_met_verdict(self):
        status = herdr_pane.derive_status(
            [("1", "works", "test", "MET")], None, None)
        assert status == "done"


# ---------------------------------------------------------------------------
# Prefix composition
# ---------------------------------------------------------------------------


class TestComposePaneTitle:
    """Prefix composition is idempotent, bounded, and suffix-preserving."""

    def test_done_prefix_is_prepended(self):
        assert herdr_pane.compose_pane_title(
            "Manually triggered intake", "done") == "✅ Manually triggered intake"

    def test_note_prefix(self):
        assert herdr_pane.compose_pane_title(
            "Manually triggered intake", "note") == "⚠️ Manually triggered intake"

    def test_attention_prefix(self):
        assert herdr_pane.compose_pane_title(
            "Manually triggered intake", "attention") == "🚫 Manually triggered intake"

    def test_text_fallback_without_icons(self):
        assert herdr_pane.compose_pane_title(
            "Manually triggered intake", "note", use_icons=False
        ) == "Note Manually triggered intake"

    def test_idempotent_for_same_status(self):
        once = herdr_pane.compose_pane_title("My pane", "done")
        twice = herdr_pane.compose_pane_title(once, "done")
        assert twice == once

    def test_status_change_replaces_prefix(self):
        assert herdr_pane.compose_pane_title("✅ My pane", "attention") == "🚫 My pane"

    def test_text_prefix_replaced_by_icon(self):
        assert herdr_pane.compose_pane_title(
            "Done My pane", "attention") == "🚫 My pane"

    def test_truncates_within_herdr_limit(self):
        label = "Downtime triggered implement Update p… - SA-0MTFLQEQQ0083EW1"
        result = herdr_pane.compose_pane_title(label, "attention")
        assert herdr_pane.js_length(result) <= herdr_pane.MAX_PANE_TITLE_LENGTH

    def test_truncation_preserves_work_item_id_suffix(self):
        label = "A" * 70 + " - SA-0MTFLQEQQ0083EW1"
        result = herdr_pane.compose_pane_title(label, "done")
        assert result.endswith(" - SA-0MTFLQEQQ0083EW1")
        assert herdr_pane.js_length(result) <= herdr_pane.MAX_PANE_TITLE_LENGTH

    def test_short_label_unchanged_apart_from_prefix(self):
        result = herdr_pane.compose_pane_title("Short", "done")
        assert result == "✅ Short"


class TestTruncatePaneTitle:
    """Plain truncation mirrors herdr's own behaviour."""

    def test_short_title_unchanged(self):
        assert herdr_pane.truncate_pane_title("abc") == "abc"

    def test_long_title_bounded(self):
        result = herdr_pane.truncate_pane_title("x" * 100)
        assert herdr_pane.js_length(result) == herdr_pane.MAX_PANE_TITLE_LENGTH
        assert result.endswith("…")


# ---------------------------------------------------------------------------
# update_pane_title — happy path and idempotency
# ---------------------------------------------------------------------------


class TestUpdatePaneTitle:
    """Pane-title updates via an injected herdr CLI runner."""

    def test_happy_path_renames_with_prefixed_label(self):
        result, fake = _update(label="Manually triggered intake", status="done")
        assert result["updated"] is True
        assert fake.calls[-1] == [
            "/fake/herdr", "pane", "rename", "w1:p1", "✅ Manually triggered intake"]

    def test_reads_existing_label_when_not_supplied(self):
        result, fake = _update(status="note")
        assert result["updated"] is True
        assert any(call[1:3] == ["pane", "list"] for call in fake.calls)

    def test_explicit_label_avoids_pane_list(self):
        fake = FakeHerdr()
        result = herdr_pane.update_pane_title(
            "done", label="Given label", pane_id="w1:p1", env={},
            herdr_bin="/fake/herdr", runner=fake,
        )
        assert result["updated"] is True
        assert all(call[1:3] != ["pane", "list"] for call in fake.calls)
        assert fake.calls[-1][-1] == "✅ Given label"

    def test_rerun_is_idempotent(self):
        fake = FakeHerdr()
        kw = {"pane_id": "w1:p1", "env": {}, "herdr_bin": "/fake/herdr", "runner": fake}
        first = herdr_pane.update_pane_title("done", **kw)
        second = herdr_pane.update_pane_title("done", **kw)
        assert first["updated"] is True
        assert second["updated"] is False
        assert fake.label == "✅ Manually triggered intake"
        # Only one rename call despite two invocations.
        assert sum(1 for c in fake.calls if c[1:3] == ["pane", "rename"]) == 1

    def test_no_icons_uses_text_fallback(self):
        fake = FakeHerdr()
        herdr_pane.update_pane_title(
            "attention", pane_id="w1:p1", env={}, use_icons=False,
            herdr_bin="/fake/herdr", runner=fake,
        )
        assert fake.label == "Attention Manually triggered intake"

    def test_wl_no_icons_env_uses_text_fallback(self):
        fake = FakeHerdr()
        herdr_pane.update_pane_title(
            "done", pane_id="w1:p1", env={"WL_NO_ICONS": "1"},
            herdr_bin="/fake/herdr", runner=fake,
        )
        assert fake.label == "Done Manually triggered intake"

    def test_reports_pane_id_and_status(self):
        result, _ = _update(status="note")
        assert result["pane_id"] == "w1:p1"
        assert result["status"] == "note"
        assert result["label"] == "⚠️ Manually triggered intake"


# ---------------------------------------------------------------------------
# Fail-open behaviour
# ---------------------------------------------------------------------------


class TestUpdatePaneTitleFailOpen:
    """Every failure mode is a no-op that never raises."""

    def test_no_pane_id_skips(self):
        fake = FakeHerdr()
        result = herdr_pane.update_pane_title(
            "done", env={}, runner=fake, which=lambda _name: "/fake/herdr")
        assert result["updated"] is False
        assert fake.calls == []

    def test_missing_herdr_cli_skips(self):
        fake = FakeHerdr()
        result = herdr_pane.update_pane_title(
            "done", pane_id="w1:p1", env={}, runner=fake, which=lambda _name: None)
        assert result["updated"] is False
        assert fake.calls == []

    def test_runner_exception_is_swallowed(self):
        def boom(*_args, **_kwargs):
            raise OSError("herdr exploded")

        result = herdr_pane.update_pane_title(
            "done", pane_id="w1:p1", env={}, herdr_bin="/fake/herdr", runner=boom)
        assert result["updated"] is False

    def test_nonzero_exit_is_swallowed(self):
        def failing(cmd, **_kwargs):
            return subprocess.CompletedProcess(cmd, 1, "", "nope")

        result = herdr_pane.update_pane_title(
            "done", pane_id="w1:p1", env={}, herdr_bin="/fake/herdr",
            runner=failing)
        assert result["updated"] is False

    def test_error_json_is_treated_as_failure(self):
        def erroring(cmd, **_kwargs):
            return subprocess.CompletedProcess(
                cmd, 0, json.dumps({"error": {"code": "pane_not_found"}}), "")

        result = herdr_pane.update_pane_title(
            "done", pane_id="w1:p1", env={}, herdr_bin="/fake/herdr",
            runner=erroring)
        assert result["updated"] is False

    def test_malformed_list_output_is_swallowed(self):
        def malformed(cmd, **_kwargs):
            return subprocess.CompletedProcess(cmd, 0, "not json", "")

        result = herdr_pane.update_pane_title(
            "done", pane_id="w1:p1", env={}, herdr_bin="/fake/herdr",
            runner=malformed)
        assert result["updated"] is False

    def test_unknown_pane_label_is_skipped(self):
        fake = FakeHerdr(label=None)
        label = herdr_pane.read_pane_label(
            "w1:p1", herdr_bin="/fake/herdr", runner=fake, env={})
        assert label is None


# ---------------------------------------------------------------------------
# Abort convenience
# ---------------------------------------------------------------------------


class TestMarkAborted:
    """The abort convenience always signals the red (attention) state."""

    def test_mark_aborted_sets_attention(self):
        fake = FakeHerdr()
        result = herdr_pane.mark_aborted(
            pane_id="w1:p1", env={}, herdr_bin="/fake/herdr", runner=fake)
        assert result["status"] == "attention"
        assert fake.label == "🚫 Manually triggered intake"

    def test_mark_aborted_fails_open_without_pane(self):
        result = herdr_pane.mark_aborted(env={}, which=lambda _name: None)
        assert result["updated"] is False
