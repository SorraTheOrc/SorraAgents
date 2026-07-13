"""Code Review skill scripts package (canonical location).

This package provides automated code quality tooling integrated with
the code-review skill. Modules:

- detection: Language detection and linter probing
- linter_runner: Linter execution and severity classification
- code_quality: Orchestrator for the full code quality pipeline
- create_quality_epics: Work item creation for quality findings
"""

# NOTE: This is the canonical location for code-review scripts.
# The legacy skill/code_review/ directory contains backward-compatible
# copies for tests and imports expecting `skill.code_review.scripts.*`.
