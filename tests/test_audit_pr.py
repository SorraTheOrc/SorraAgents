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


def test_record_audit_result_dry_run(tmp_path):
    ok = audit_pr.record_audit_result('SA-TEST', True, 'summary', 'raw', dry_run=True)
    assert ok
    fpath = os.path.join('.pi', 'tmp', 'audit-result-SA-TEST.json')
    assert os.path.exists(fpath)
    data = json.load(open(fpath))
    assert data['ready_to_close'] is True
    assert data['summary'] == 'summary'


def test_resolve_wl_for_pr_from_metadata():
    pr = audit_pr.PRInfo(owner='o', repo='r', number=1, title='Fix SA-12345 bug', body='details')
    wl, note = audit_pr.resolve_wl_for_pr(pr)
    assert wl == 'SA-12345'
    assert note == 'resolved-from-pr-metadata'


def test_resolve_wl_for_pr_create_dry_run():
    pr = audit_pr.PRInfo(owner='o', repo='r', number=2, title='No WL here', body='body')
    wl, note = audit_pr.resolve_wl_for_pr(pr, allow_create=True, dry_run=True)
    assert wl == 'SA-DRYRUN'
    assert note == 'created-from-pr'


def test_resolve_wl_for_pr_unresolved():
    pr = audit_pr.PRInfo(owner='o', repo='r', number=3, title='No WL here', body='body')
    wl, note = audit_pr.resolve_wl_for_pr(pr, allow_create=False)
    assert wl is None
    assert note == 'unresolved-needs-user-input'


def test_gh_get_pr_checks_fallback(monkeypatch):
    monkeypatch.setattr(subprocess, 'check_output', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('boom')))
    result = audit_pr.gh_get_pr_checks('o', 'r', 1)
    assert result['checks_ok'] is None


def test_merge_pr_dry_run():
    assert audit_pr.merge_pr('o', 'r', 1, dry_run=True) is True


def test_record_audit_result_non_dry_run_invokes_wl(monkeypatch):
    calls = []

    def fake_call(cmd, *a, **k):
        calls.append(cmd)
        return 0

    monkeypatch.setattr(subprocess, 'check_call', fake_call)
    ok = audit_pr.record_audit_result('SA-900', True, 'Summary Text', 'Raw Output', dry_run=False)
    assert ok is True
    assert calls
    assert calls[0][:3] == ['wl', 'audit-set', 'SA-900']
    assert '--ready-to-close' in calls[0]
    assert 'yes' in calls[0]
    assert '--summary' in calls[0]
    assert 'Summary Text' in calls[0]
    assert '--raw-output' in calls[0]
    assert 'Raw Output' in calls[0]


def test_append_audit_comment_dry_run():
    ok = audit_pr.append_audit_comment('SA-901', 'Ready to close: Yes', dry_run=True)
    assert ok is True
    fpath = os.path.join('.pi', 'tmp', 'audit-comment-SA-901.md')
    assert os.path.exists(fpath)
    assert '# AMPA Audit Result' in open(fpath).read()


def test_append_audit_comment_non_dry_run_invokes_wl(monkeypatch):
    calls = []

    def fake_call(cmd, *a, **k):
        calls.append(cmd)
        return 0

    monkeypatch.setattr(subprocess, 'check_call', fake_call)
    ok = audit_pr.append_audit_comment('SA-902', 'Ready to close: Yes', dry_run=False)
    assert ok is True
    assert calls
    assert calls[0][:4] == ['wl', 'comment', 'add', 'SA-902']


def test_create_ephemeral_checkout_non_dry_run_invokes_git(monkeypatch, tmp_path):
    calls = []

    def fake_call(cmd, *a, **k):
        calls.append((cmd, k.get('cwd')))
        return 0

    monkeypatch.setattr(subprocess, 'check_call', fake_call)
    monkeypatch.setattr(audit_pr.os.path, 'abspath', lambda p: str(tmp_path / '.pi' / 'tmp'))
    monkeypatch.setattr(audit_pr.os.path, 'exists', lambda p: False)
    monkeypatch.setattr(audit_pr.os, 'makedirs', lambda *a, **k: None)

    dest = audit_pr.create_ephemeral_checkout('owner', 'repo', 77, dry_run=False)
    assert 'pr-owner-repo-77' in dest
    assert len(calls) == 3
    assert calls[0][0][0:2] == ['git', 'clone']
    assert calls[1][0][0:2] == ['git', 'fetch']
    assert calls[2][0][0:2] == ['git', 'checkout']


def test_resolve_wl_for_pr_explicit_wl():
    pr = audit_pr.PRInfo(owner='o', repo='r', number=4, title='No wl')
    wl, note = audit_pr.resolve_wl_for_pr(pr, explicit_wl='SA-EXPLICIT')
    assert wl == 'SA-EXPLICIT'
    assert note == 'provided-explicitly'


def test_gh_get_pr_checks_parses_rollup(monkeypatch):
    payload = {
        'mergeStateStatus': 'CLEAN',
        'statusCheckRollup': [
            {'conclusion': 'SUCCESS'},
            {'conclusion': 'NEUTRAL'},
        ],
    }

    monkeypatch.setattr(subprocess, 'check_output', lambda *a, **k: json.dumps(payload))
    result = audit_pr.gh_get_pr_checks('o', 'r', 9)
    assert result['checks_ok'] is True
    assert result['merge_state'] == 'CLEAN'


def test_extract_structured_audit_text_with_markers():
    raw = "prefix\n--- AUDIT REPORT START ---\nReady to close: No\n--- AUDIT REPORT END ---\nsuffix"
    extracted = audit_pr.extract_structured_audit_text(raw)
    assert extracted == 'Ready to close: No'


def test_summarize_unmet_criteria_rows():
    report = """
| # | Criterion | Verdict | Evidence |
|---|-----------|---------|----------|
| 1 | A | met | x.py:1 |
| 2 | B | unmet | y.py:2 |
| 3 | C | partial | z.py:3 |
"""
    rows = audit_pr.summarize_unmet_criteria(report)
    assert len(rows) == 2
    assert 'unmet' in rows[0].lower()
    assert 'partial' in rows[1].lower()


def test_extract_ready_to_close():
    assert audit_pr.extract_ready_to_close("Ready to close: Yes") is True
    assert audit_pr.extract_ready_to_close("Ready to close: No") is False
    assert audit_pr.extract_ready_to_close("Ready to close: Partial") is False
    assert audit_pr.extract_ready_to_close("nothing") is False


def test_main_pr_flow_dry_run_success(capsys, monkeypatch):
    monkeypatch.setattr(
        audit_pr,
        'gh_get_pr',
        lambda owner, repo, number: audit_pr.PRInfo(owner=owner, repo=repo, number=number, title='Feature SA-100', body='body', head_ref='feat'),
    )
    rc = audit_pr.main(['owner/repo#12', '--dry-run', '--run-checkout', '--run-audit', '--offer-merge'])
    assert rc == 0
    out = capsys.readouterr().out
    assert 'WL resolution: resolved-from-pr-metadata -> SA-100' in out
    assert 'Recorded audit to WL: True, comment appended: True' in out
    assert 'Offer: merge this PR into main and push' in out


def test_main_pr_flow_unresolved_wl(capsys, monkeypatch):
    monkeypatch.setattr(
        audit_pr,
        'gh_get_pr',
        lambda owner, repo, number: audit_pr.PRInfo(owner=owner, repo=repo, number=number, title='No work item', body='still none', head_ref='feat'),
    )
    rc = audit_pr.main(['owner/repo#15', '--dry-run'])
    assert rc == 0
    out = capsys.readouterr().out
    assert 'WL resolution: unresolved-needs-user-input' in out


def test_main_pr_flow_confirm_merge_dry_run(capsys, monkeypatch):
    monkeypatch.setattr(
        audit_pr,
        'gh_get_pr',
        lambda owner, repo, number: audit_pr.PRInfo(owner=owner, repo=repo, number=number, title='Feature SA-200', body='body', head_ref='feat'),
    )
    rc = audit_pr.main([
        'owner/repo#20', '--dry-run', '--run-checkout', '--run-audit', '--offer-merge', '--confirm-merge'
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert 'Merge executed: True' in out


def test_main_pr_flow_non_dry_run_with_mocked_exec(capsys, monkeypatch, tmp_path):
    monkeypatch.setattr(
        audit_pr,
        'gh_get_pr',
        lambda owner, repo, number: audit_pr.PRInfo(owner=owner, repo=repo, number=number, title='Feature SA-300', body='body', head_ref='feat'),
    )
    monkeypatch.setattr(audit_pr, 'create_ephemeral_checkout', lambda *a, **k: str(tmp_path))
    monkeypatch.setattr(audit_pr, 'run_build_test', lambda *a, **k: (0, str(tmp_path / 'build.log')))

    def fake_audit(path, wl_id, timeout=600, dry_run=True):
        log = tmp_path / 'audit.log'
        log.write_text('--- AUDIT REPORT START ---\nReady to close: Yes\n--- AUDIT REPORT END ---\n')
        return 0, str(log)

    monkeypatch.setattr(audit_pr, 'run_audit_in_worktree', fake_audit)
    monkeypatch.setattr(audit_pr, 'record_audit_result', lambda *a, **k: True)
    monkeypatch.setattr(audit_pr, 'append_audit_comment', lambda wl, text, dry_run=False: True)
    monkeypatch.setattr(audit_pr, 'gh_get_pr_checks', lambda *a, **k: {'checks_ok': True, 'merge_state': 'CLEAN', 'raw': {}})
    monkeypatch.setattr(audit_pr, 'merge_pr', lambda *a, **k: True)

    rc = audit_pr.main([
        'owner/repo#30', '--run-checkout', '--run-audit', '--offer-merge', '--confirm-merge'
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert 'Recorded audit to WL: True, comment appended: True' in out
    assert 'Merge executed: True' in out


def test_main_pr_flow_audit_fail_reports_context_and_unmet(capsys, monkeypatch, tmp_path):
    monkeypatch.setattr(
        audit_pr,
        'gh_get_pr',
        lambda owner, repo, number: audit_pr.PRInfo(owner=owner, repo=repo, number=number, title='Feature SA-301', body='body', head_ref='feat'),
    )
    monkeypatch.setattr(audit_pr, 'create_ephemeral_checkout', lambda *a, **k: str(tmp_path))
    monkeypatch.setattr(audit_pr, 'run_build_test', lambda *a, **k: (0, str(tmp_path / 'build.log')))

    def fake_audit_fail(path, wl_id, timeout=600, dry_run=True):
        log = tmp_path / 'audit-fail.log'
        log.write_text(
            '--- AUDIT REPORT START ---\n'
            '| # | Criterion | Verdict | Evidence |\n'
            '|---|---|---|---|\n'
            '| 1 | X | unmet | src/x.py:10 |\n'
            '--- AUDIT REPORT END ---\n'
        )
        return 1, str(log)

    monkeypatch.setattr(audit_pr, 'run_audit_in_worktree', fake_audit_fail)
    monkeypatch.setattr(audit_pr, 'record_audit_result', lambda *a, **k: True)
    monkeypatch.setattr(audit_pr, 'append_audit_comment', lambda wl, text, dry_run=False: True)

    rc = audit_pr.main(['owner/repo#31', '--run-checkout', '--run-audit'])
    assert rc == 0
    out = capsys.readouterr().out
    assert 'Still on PR branch/worktree context' in out
    assert 'Unmet/partial criteria evidence' in out
    assert 'src/x.py:10' in out


if __name__ == '__main__':
    pytest.main([__file__])
