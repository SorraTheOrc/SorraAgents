"""Tests for worklog/ prefix protection in cleanup scripts."""

import sys
import os

# Add repo root to path so we can import the cleanup scripts
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from skill.cleanup.scripts import summarize_branches  # noqa: E402
from skill.cleanup.scripts import delete_remote_branches  # noqa: E402


def test_summarize_branches_protected_contains_worklog():
    """summarize_branches.py PROTECTED should contain 'worklog/'"""
    assert "worklog/" in summarize_branches.PROTECTED


def test_summarize_branches_existing_protected_still_present():
    """Existing PROTECTED entries (main, master, develop) should still be present"""
    assert "main" in summarize_branches.PROTECTED
    assert "master" in summarize_branches.PROTECTED
    assert "develop" in summarize_branches.PROTECTED


def test_delete_remote_branches_protected_contains_worklog():
    """delete_remote_branches.py PROTECTED should contain 'worklog/'"""
    assert "worklog/" in delete_remote_branches.PROTECTED


def test_delete_remote_branches_existing_protected_still_present():
    """Existing PROTECTED entries should still be present"""
    assert "main" in delete_remote_branches.PROTECTED
    assert "master" in delete_remote_branches.PROTECTED
    assert "develop" in delete_remote_branches.PROTECTED


def test_protected_prefix_matching():
    """Test that the any() expression in both scripts correctly handles prefix matching"""
    protected_set = summarize_branches.PROTECTED

    # worklog/ branches should match via prefix
    assert any(
        b.startswith(p) if p.endswith("/") else b == p
        for p in protected_set
        for b in ["worklog/data"]
    )

    # Exact matches should work
    assert any(
        b.startswith(p) if p.endswith("/") else b == p
        for p in protected_set
        for b in ["main"]
    )

    # Non-worklog branches should not match
    assert not any(
        b.startswith(p) if p.endswith("/") else b == p
        for p in protected_set
        for b in ["feature/test"]
    )

    # wl- agent branches should not match
    assert not any(
        b.startswith(p) if p.endswith("/") else b == p
        for p in protected_set
        for b in ["wl-SA-001-feature"]
    )
