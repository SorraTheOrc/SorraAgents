import json
import os
import subprocess
import sys
import types

import pytest

from skill.audit import audit_pr


def test_parse_input_ref_url():
    url = 'https://github.com/owner/repo/pull/123'
    assert audit_pr.parse_input_ref(url) == ('owner', 'repo', 123)


def test_parse_input_ref_owner_repo():
    ref = 'owner/repo#45'
    assert audit_pr.parse_input_ref(ref) == ('owner', 'repo', 45)


def test_extract_wl_id():
    text = 'This fixes issue SA-0ABC123 and updates docs'
    assert audit_pr.extract_wl_id(text) == 'SA-0ABC123'


def test_detect_build_command(tmp_path, monkeypatch):
    # default: no build/test files
    cwd = tmp_path
    assert audit_pr.detect_build_command(str(cwd)) is None
    # package.json
    (cwd / 'package.json').write_text('{}')
    assert audit_pr.detect_build_command(str(cwd)) == 'npm test'
    (cwd / 'package.json').unlink()
    # pyproject
    (cwd / 'pyproject.toml').write_text('[tool]\n')
    assert audit_pr.detect_build_command(str(cwd)) == 'pytest'


def test_gh_get_pr_no_gh(monkeypatch):
    # simulate gh missing by forcing subprocess.check_output to raise
    monkeypatch.setattr(subprocess, 'check_output', lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()))
    assert audit_pr.gh_get_pr('owner', 'repo', 1) is None


def test_create_ephemeral_checkout_dry_run(tmp_path):
    dest = audit_pr.create_ephemeral_checkout('owner', 'repo', 123, 'branch', dry_run=True)
    assert 'pr-owner-repo-123' in dest
    # dry-run must not create the directory
    assert not os.path.exists(dest)


def test_run_build_test_dry_run(tmp_path):
    rc, log = audit_pr.run_build_test(str(tmp_path), 'echo hi', dry_run=True)
    assert rc == 0
    assert os.path.exists(log)
    content = open(log).read()
    assert 'DRY-RUN' in content


def test_run_build_test_real(tmp_path):
    # run a simple command that prints to stdout
    rc, log = audit_pr.run_build_test(str(tmp_path), 'echo hello', dry_run=False)
    assert rc == 0
    content = open(log).read()
    assert 'hello' in content


def test_run_audit_dry_run(tmp_path):
    rc, log = audit_pr.run_audit_in_worktree(str(tmp_path), 'SA-TEST', dry_run=True)
    assert rc == 0
    content = open(log).read()
    assert 'DRY-RUN' in content


def test_record_audit_text_dry_run(tmp_path):
    ok = audit_pr.record_audit_text('SA-TEST', '---AUDIT---\nOK', dry_run=True)
    assert ok
    fpath = os.path.join('.opencode', 'tmp', 'audit-SA-TEST.txt')
    assert os.path.exists(fpath)
    assert 'AUDIT' in open(fpath).read()


if __name__ == '__main__':
    pytest.main([__file__])
