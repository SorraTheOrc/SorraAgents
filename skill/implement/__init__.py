"""Implement skill: deterministic implementation workflow orchestration.

This package provides a script-assisted implementation workflow, replacing
the purely document-based approach with a Python orchestration script that
manages worktree lifecycle, signal handling, process cleanup, build/test
cycles, and stage advancement.

Key components:
- ``scripts/implement.py`` — CLI orchestrator with start/finish/abort phases
"""

from __future__ import annotations
