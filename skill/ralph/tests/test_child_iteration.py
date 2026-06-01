from __future__ import annotations

from pathlib import Path
import sys

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from skill.ralph.scripts.ralph_loop import RalphLoop, _build_implement_prompt


class SingleItemLoop(RalphLoop):
    def __init__(self):
        super().__init__(pi_bin="pi", stream=False)
        self.pi_calls: list[tuple[str, str]] = []

    def _run_pi(self, prompt: str, phase: str = "implementation") -> str:
        self.pi_calls.append((phase, prompt))
        return "ok"

    def _wl_show(self, work_item_id: str, children: bool = False) -> dict:
        if children:
            return {"workItem": {"id": work_item_id, "stage": "plan_complete"}, "children": []}
        audit_text = (
            "Ready to close: Yes\n\n"
            "## Acceptance Criteria Status\n"
            "| # | Criterion | Verdict | Evidence |\n"
            "| 1 | ok | met | evidence |"
        )
        return {
            "workItem": {
                "id": work_item_id,
                "stage": "plan_complete",
                "audit": {"text": audit_text},
            }
        }

    def _scope_ids_recursive(self, target_id: str) -> list[str]:
        return [target_id]

    def _scope_in_review(self, scope_ids):
        return True

    def _run_checks(self):
        return {"status": "ok"}

    def _capture_changed_files(self):
        return []

    def _run_merge(self):
        return None

    def _cleanup_pi_process(self):
        return None

    def _child_stage_map(self, target_id: str) -> dict[str, str]:
        return {}

    def _compact_after_child_transition(self, *args, **kwargs) -> tuple[int, int]:
        return 0, 0


class IterationLoop(RalphLoop):
    def __init__(self):
        super().__init__(pi_bin="pi", stream=False)
        self.single_calls: list[tuple[str, dict]] = []

    def _get_children(self, target_id: str) -> list[dict]:
        return [
            {"id": "SA-CHILD-1", "stage": "plan_complete"},
            {"id": "SA-CHILD-2", "stage": "plan_complete"},
        ]

    def run_single_item(self, item_id: str, **kwargs) -> dict:
        self.single_calls.append((item_id, kwargs))
        return {"status": "success", "attempt": 1}

    def _run_checks(self):
        return {"status": "ok"}

    def _capture_changed_files(self):
        return []

    def _run_merge(self):
        return None

    def _cleanup_pi_process(self):
        return None

    def _scope_ids_recursive(self, target_id: str) -> list[str]:
        return [target_id]


def test_build_implement_prompt_defaults_to_implement():
    prompt = _build_implement_prompt("SA-123")
    assert prompt.splitlines()[0] == "implement SA-123"


def test_build_implement_prompt_scopes_single_item():
    prompt = _build_implement_prompt("SA-123", command="implement-single")
    assert prompt.splitlines()[0] == "implement-single SA-123"
    assert "Complete only this work item" in prompt


def test_run_single_item_uses_implement_single_and_audits_child():
    loop = SingleItemLoop()

    result = loop.run_single_item("SA-CHILD", implement_command="implement-single")

    implement_prompts = [prompt for phase, prompt in loop.pi_calls if phase == "implementation"]
    audit_prompts = [prompt for phase, prompt in loop.pi_calls if phase == "audit"]

    assert implement_prompts[0].startswith("implement-single SA-CHILD")
    assert audit_prompts == ["/skill:audit SA-CHILD"]
    assert result["status"] == "success"


def test_run_iterates_children_and_runs_integration_audit():
    loop = IterationLoop()

    result = loop.run("SA-PARENT")

    assert [call[0] for call in loop.single_calls] == ["SA-CHILD-1", "SA-CHILD-2", "SA-PARENT"]
    for _, kwargs in loop.single_calls[:2]:
        assert kwargs["implement_command"] == "implement-single"
        assert kwargs.get("skip_implement") is None
    assert loop.single_calls[-1][1]["skip_implement"] is True
    assert result["status"] == "success"
