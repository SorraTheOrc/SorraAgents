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

    def _run_pi(self, prompt: str, phase: str = "implementation", tier: str | None = None) -> str:
        self.pi_calls.append((phase, prompt))
        return "ok"

    def _wl_show(self, work_item_id: str, children: bool = False) -> dict:
        if children:
            return {"workItem": {"id": work_item_id, "stage": "plan_complete"}, "children": []}
        return {
            "workItem": {
                "id": work_item_id,
                "stage": "plan_complete",
            }
        }

    def _wl_audit_show(self, work_item_id: str) -> dict:
        audit_text = (
            "Ready to close: Yes\n\n"
            "## Acceptance Criteria Status\n"
            "| # | Criterion | Verdict | Evidence |\n"
            "| 1 | ok | met | evidence |"
        )
        return {
            "success": True,
            "workItemId": work_item_id,
            "audit": {
                "workItemId": work_item_id,
                "readyToClose": True,
                "auditedAt": "2026-06-05T12:00:00Z",
                "summary": "All criteria met",
                "rawOutput": audit_text,
                "author": "audit-agent",
            },
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
        self.branch_creations: list[str] = []

    def _get_children(self, target_id: str) -> list[dict]:
        return [
            {"id": "SA-CHILD-1", "stage": "plan_complete"},
            {"id": "SA-CHILD-2", "stage": "plan_complete"},
        ]

    def _create_feature_branch(self, work_item_id: str, short_desc: str = "") -> str:
        branch_name = f"wl-{work_item_id}-test-branch"
        self.branch_creations.append(branch_name)
        return branch_name

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


def test_run_creates_parent_branch_and_passes_to_children():
    loop = IterationLoop()

    result = loop.run("SA-PARENT")

    # Verify branch was created for parent
    assert len(loop.branch_creations) == 1
    assert loop.branch_creations[0] == "wl-SA-PARENT-test-branch"

    # Verify parent_branch is passed to child iterations
    for _, kwargs in loop.single_calls[:2]:
        assert kwargs["parent_branch"] == "wl-SA-PARENT-test-branch"

    # Parent integration audit should not have parent_branch
    assert loop.single_calls[-1][1].get("parent_branch") is None
    assert result["status"] == "success"


def test_build_implement_prompt_includes_parent_branch():
    from skill.ralph.scripts.ralph_loop import _build_implement_single_prompt

    prompt = _build_implement_single_prompt("SA-CHILD", parent_branch="wl-SA-PARENT-my-branch")
    assert "IMPORTANT: Use the existing feature branch 'wl-SA-PARENT-my-branch'" in prompt
    assert "git checkout wl-SA-PARENT-my-branch" in prompt
    assert "Do NOT create a new branch" in prompt
    assert "Related-Work: SA-CHILD" in prompt
    assert "SA-CHILD: <concise summary of changes>" in prompt


def test_build_implement_prompt_without_parent_branch():
    from skill.ralph.scripts.ralph_loop import _build_implement_single_prompt

    prompt = _build_implement_single_prompt("SA-CHILD")
    assert "parent_branch" not in prompt
    assert "IMPORTANT: Use the existing feature branch" not in prompt
