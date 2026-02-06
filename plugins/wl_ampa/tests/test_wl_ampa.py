import os
import subprocess
import tempfile
import time
from pathlib import Path

from plugins.wl_ampa import __init__ as ampa


def write_tmp_project(tmp_path: Path) -> Path:
    # Create a fake project with worklog.json and a tiny script
    p = tmp_path / "proj"
    p.mkdir()
    (p / "worklog.json").write_text('{"name":"t","ampa":"python -u test_daemon.py"}')
    script = p / "test_daemon.py"
    script.write_text("""
import time
import sys
print('daemon starting')
sys.stdout.flush()
time.sleep(10)
""")
    return p


def test_start_status_stop(tmp_path):
    proj = write_tmp_project(tmp_path)
    # start detached
    rc = ampa.main(["start", "--name", "t1"])
    assert rc == 0
    # status should report running
    os.chdir(proj)
    s_rc = ampa.main(["status", "--name", "t1"])
    assert s_rc == 0
    # stop
    stop_rc = ampa.main(["stop", "--name", "t1"])
    assert stop_rc == 0
    # status now stopped
    s2_rc = ampa.main(["status", "--name", "t1"])
    assert s2_rc != 0
