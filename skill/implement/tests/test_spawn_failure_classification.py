"""Classification tests for the implement skill's spawn-failure classifier.

``classify_spawn_failure`` decides whether a failed driven-child spawn is a
transient *provider-infrastructure* hiccup (retried on the shared
``--infra-retry-limit`` budget) or a genuine defect (``unknown`` — hard-stop).

A driven child is spawned with ``--no-extensions`` (see
``_default_session_spawner``), which disables the ContextHub ``Worklog``
recovery extension that would otherwise retry a ``Provider finish_reason:
error`` reply. The transient stop therefore reaches Pi core, which treats a
mid-stream ``finish_reason: "error"`` as a fatal turn and exits the session
non-zero. This module pins the drive-layer classification that absorbs it
(WL-0MWTZEDWG008YMGQ) and the fail-safe floor that keeps genuine defects
``unknown``.

Pi builds that message in ``mapStopReason`` (``openai-completions``): a raw
``finish_reason: "error"`` becomes the literal string
``Provider finish_reason: error``. The word ``Provider`` is Pi's fixed prefix,
not a provider name, so the classifier must not fabricate one.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) in sys.path:
    sys.path.remove(str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT))
_SKILLS_ROOT = REPO_ROOT / "skill"
if str(_SKILLS_ROOT) not in sys.path:
    sys.path.append(str(_SKILLS_ROOT))

from skill.implement.scripts.implement import classify_spawn_failure


class TestProviderFinishReasonErrorIsInfrastructure:
    """The transient provider-stream signature is retryable infrastructure."""

    @pytest.mark.parametrize(
        "reason",
        [
            "Provider finish_reason: error",
            "finish_reason: error",
            "FINISH_REASON: ERROR",
            "Provider finish_reason: ERROR",
            "child stderr: ... Provider finish_reason: error",
            (
                "Child session for WL-0MWU79PCE008NE2P exited with code 1: "
                "Provider finish_reason: error"
            ),
        ],
    )
    def test_classified_infrastructure(self, reason: str) -> None:
        result = classify_spawn_failure(reason)
        assert result["kind"] == "infrastructure"
        # The caller's reason is surfaced verbatim for the drive report.
        assert result["reason"] == reason
        assert result["remedy"]

    def test_remedy_names_drive_and_the_provider(self) -> None:
        remedy = classify_spawn_failure(
            "Provider finish_reason: error"
        )["remedy"].lower()
        assert "drive" in remedy
        # Actionable for a recurrence: names the proxy/provider and the model.
        assert "proxy" in remedy or "provider" in remedy
        assert "model" in remedy

    def test_no_provider_name_is_invented(self) -> None:
        # Pi's message is the fixed literal "Provider" prefix followed by
        # "finish_reason: <reason>"; it carries no provider name, so the
        # classifier must not fabricate one.
        assert (
            classify_spawn_failure("Provider finish_reason: error")["provider"]
            is None
        )
        assert (
            classify_spawn_failure("finish_reason: error")["provider"] is None
        )

    def test_existing_model_resolution_fragments_unchanged(self) -> None:
        no_model = classify_spawn_failure('No models match pattern "MunitoAI/*"')
        assert no_model["kind"] == "infrastructure"
        assert no_model["provider"] == "MunitoAI"

        no_key = classify_spawn_failure("No API key found for OpenAI")
        assert no_key["kind"] == "infrastructure"
        assert no_key["provider"] == "OpenAI"


class TestGenuineDefectsStayUnknown:
    """The fail-safe floor: a real defect is never retried as infrastructure."""

    @pytest.mark.parametrize(
        "reason",
        [
            "",
            "   ",
            None,
            "ModuleNotFoundError: No module named 'nonexistent'",
            "TypeError: unexpected type",
            (
                "Child session for WL-0MWU79PCE008NE2P exited with code 1: "
                "1 failed, 12 passed (test_failure)"
            ),
            "Child session for WL-0MWU79PCE008NE2P timed out after 900s.",
            # Bare "error" must never match — the fragment is the full
            # provider-stream signature, never the word alone.
            "error",
            # Other finish_reason values are not the transient error class.
            "Provider finish_reason: content_filter",
            "Provider finish_reason: length",
            "Provider finish_reason: network_error",
        ],
    )
    def test_unknown(self, reason: object) -> None:
        assert classify_spawn_failure(reason)["kind"] == "unknown"

    def test_unknown_carries_no_remedy_or_provider(self) -> None:
        result = classify_spawn_failure("TypeError: unexpected type")
        assert result["provider"] is None
        assert result["remedy"] == ""


class TestTimeoutPrecedence:
    """A timeout keeps its own reconciliation semantics."""

    def test_provider_text_echoed_into_a_timeout_stays_unknown(self) -> None:
        reason = (
            "Child session for WL-0MWU79PCE008NE2P timed out after 900s. "
            "stderr: Provider finish_reason: error"
        )
        assert classify_spawn_failure(reason)["kind"] == "unknown"
