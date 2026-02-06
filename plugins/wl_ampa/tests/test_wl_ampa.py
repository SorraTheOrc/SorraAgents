import os
import subprocess
import tempfile
import time
from pathlib import Path

import sys
from pathlib import Path

# Ensure repo root is on sys.path for test imports
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
import plugins.wl_ampa as ampa


def write_tmp_project(tmp_path: Path) -> Path:
    # Create a fake project with worklog.json and a tiny script
    p = tmp_path / "proj"
    p.mkdir()
    (p / "worklog.json").write_text('{"name":"t","ampa":"python -u test_daemon.py"}')
    script = p / "test_daemon.py"
    script.write_text("""
import time
import sys
import signal

stop = False

def handler(signum, frame):
    global stop
    stop = True

signal.signal(signal.SIGTERM, handler)

print('daemon starting')
sys.stdout.flush()
while not stop:
    time.sleep(0.1)
print('daemon exiting')
""")
    return p


def test_start_status_stop(tmp_path):
    proj = write_tmp_project(tmp_path)
    # run from project dir so root detection resolves worklog.json
    os.chdir(proj)
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
