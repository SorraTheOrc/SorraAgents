import json
import subprocess

import pytest

from skill.ralph.scripts.ralph_loop import RalphLoop, RalphError


def test_wl_show_default_raises():
    def fake_runner(cmd):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="fail")

    loop = RalphLoop(runner=fake_runner)
    with pytest.raises(RalphError):
        loop._wl_show("WI")


def test_wl_show_fail_open_returns_empty():
    def fake_runner(cmd):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="fail")

    loop = RalphLoop(runner=fake_runner, fail_open=True)
    res = loop._wl_show("WI")
    assert res == {}


def test_wl_show_retry_success():
    calls = {"n": 0}

    def fake_runner(cmd):
        if cmd and cmd[0] == "wl":
            calls["n"] += 1
            if calls["n"] == 1:
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="fail1")
            return subprocess.CompletedProcess(cmd, 0, stdout='{"workItem":{"id":"WI","stage":"in_review"},"children":[]}')
        return subprocess.CompletedProcess(cmd, 0, stdout="")

    loop = RalphLoop(runner=fake_runner, retry=1, retry_delay=0.0)
    res = loop._wl_show("WI")
    assert "workItem" in res


def test_loop_with_failing_check_and_fail_open():
    def runner(cmd):
        if cmd and cmd[0] == 'wl':
            ws = {"workItem": {"id": "WI", "stage": "in_review", "audit": "Ready to close: Yes\n| # | Criterion | Verdict | Evidence |"}, "children": []}
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(ws))
        if cmd and cmd[0] == 'bash':
            # Simulate failing check
            return subprocess.CompletedProcess(cmd, 1, stdout='', stderr='fail check')
        if cmd and cmd[0] == 'pi':
            pi_json = json.dumps({"type": "agent_end", "messages": [{"role": "assistant", "content": [{"type": "text", "text": "Ready to close: Yes\n| # | Criterion | Verdict | Evidence |"}]}]})
            return subprocess.CompletedProcess(cmd, 0, stdout=pi_json)
        return subprocess.CompletedProcess(cmd, 0, stdout='')

    loop = RalphLoop(runner=runner, fail_open=True, retry=0, confirm_merge=False, stream=False)
    result = loop.run('WI')
    assert result['status'] == 'success'
