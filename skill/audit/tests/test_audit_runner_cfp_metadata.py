"""Re-verification metadata on audit candidate-false-positive chores.

Covers SA-0MW7Z1JCE009G6J3 AC1 for the audit producer: every
candidate-false-positive chore records the exact ruff command that reproduces
the finding plus a content fingerprint (file hash + rule), so the
re-verification sweep can re-run it instead of guessing from a title.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from audit.scripts import audit_runner


def _finding(file: str, code: str = "F401", linter: str = "ruff") -> dict:
    return {
        "severity": "medium",
        "code": code,
        "file": file,
        "line": 7,
        "linter": linter,
        "message": "misfires here",
    }


def _patch_paths(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(audit_runner, "TARGET_PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        audit_runner,
        "_repo_relative_path",
        lambda path, *args, **kwargs: str(Path(path).relative_to(tmp_path)),
    )


class TestCfpChoreMetadata:
    def test_chore_records_verification_command_and_fingerprint(
        self, tmp_path: Path, monkeypatch
    ):
        _patch_paths(tmp_path, monkeypatch)
        target = tmp_path / "src" / "bad.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("import os\n")

        description = audit_runner._chore_description_for_cfp_decision(
            _finding(str(target)),
            {"justification": "misfires"},
            "ruff:F401:src/bad.py:7",
        )

        assert (
            f"{audit_runner.AUDIT_VERIFICATION_CMD_MARKER} "
            "ruff check --isolated --select F401 src/bad.py"
        ) in description
        assert f"{audit_runner.AUDIT_FINGERPRINT_MARKER} " in description
        # The dedup key is preserved unchanged.
        assert "CFP-Chore-Key: ruff:F401:src/bad.py:7" in description

    def test_non_ruff_finding_has_no_command_marker(
        self, tmp_path: Path, monkeypatch
    ):
        _patch_paths(tmp_path, monkeypatch)
        target = tmp_path / "src" / "bad.js"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("const a = 1\n")

        description = audit_runner._chore_description_for_cfp_decision(
            _finding(str(target), code="no-unused-vars", linter="eslint"),
            {"justification": "misfires"},
            "eslint:no-unused-vars:src/bad.js:7",
        )

        assert audit_runner.AUDIT_VERIFICATION_CMD_MARKER not in description

    def test_fingerprint_is_file_hash_plus_rule(self, tmp_path: Path, monkeypatch):
        _patch_paths(tmp_path, monkeypatch)
        target = tmp_path / "src" / "bad.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("import os\n")

        same_a = audit_runner._audit_content_fingerprint(_finding(str(target)))
        same_b = audit_runner._audit_content_fingerprint(_finding(str(target)))
        different_rule = audit_runner._audit_content_fingerprint(
            _finding(str(target), code="F841")
        )
        target.write_text("import sys\n")
        different_content = audit_runner._audit_content_fingerprint(
            _finding(str(target))
        )

        assert same_a and len(same_a) == 64
        assert same_a == same_b
        assert same_a != different_rule
        assert same_a != different_content

    def test_unreadable_target_omits_fingerprint(
        self, tmp_path: Path, monkeypatch
    ):
        _patch_paths(tmp_path, monkeypatch)

        description = audit_runner._chore_description_for_cfp_decision(
            _finding(str(tmp_path / "src" / "missing.py")),
            {"justification": "misfires"},
            "ruff:F401:src/missing.py:7",
        )

        assert audit_runner.AUDIT_FINGERPRINT_MARKER not in description
        # The command marker is still recorded: the sweep classifies it
        # target-gone from the missing file.
        assert audit_runner.AUDIT_VERIFICATION_CMD_MARKER in description
