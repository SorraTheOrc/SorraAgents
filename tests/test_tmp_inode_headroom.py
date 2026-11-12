
# <!-- REFACTOR-SA-0MWDV20G7008ZORK
# smell: unknown
# severity: medium
# description: `subprocess.run` without explicit `check` argument
# -->

# <!-- REFACTOR-SA-0MWDV1YYV007K5EA
# smell: ruff_specific
# severity: medium
# description: Unpacked variable `mode` is never used
# -->

# <!-- REFACTOR-SA-0MWDV1XHW005Y6J5
# smell: unused_import
# severity: critical
# description: Local variable `path` is assigned to but never used
# -->
"""Template + runbook consistency for the /tmp inode headroom rules.

Follow-up to the framework-level TMPDIR/prune item (SA-0MW2QED9M005FWOK):
this item (SA-0MW2QEZBG009I3AI) ships a host-level, age-based cleanup
template (``scripts/pi-test-tmp.conf``) for the high-churn temp directories,
plus the runbook ``docs/dev/host-hygiene.md``.

The template is version-controlled and *validated*, not auto-installed
(AC1/AC5: idempotent, explicit root opt-in via the operator).  This test
guards:

* the tmpfiles.d line format (6 fields, sane types, age >= 1 day — AC1);
* coverage of the candidate churn patterns from the work item (AC1);
* the safety exclusion for the active ``pytest-current`` root (AC3);
* the runbook's required install / verify / rollback / failure-signature
  sections (AC4);
* a live ``systemd-tmpfiles`` parse check where the binary is available, so
  a syntax regression in the committed template fails here rather than on
  the host.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

TEMPLATE = REPO_ROOT / "scripts" / "pi-test-tmp.conf"
RUNBOOK = REPO_ROOT / "docs" / "dev" / "host-hygiene.md"

#: Types accepted in the template.  ``e`` = content age cleanup,
#: ``x`` = exclusion from cleanup.  Modifier characters are allowed.
TYPE_RE = re.compile(r"^[eExX][+-]?$")

#: The active pytest root must never be removed (AC3).
ACTIVE_ROOT_EXCLUSION = "pytest-current"

#: Candidate churn patterns from the work-item description (AC1).
REQUIRED_PATTERNS = (
    "pytest-of-",
    "playwright",
    "chromium",
    "chrome",
    "tsx-",
    "node-compile-cache",
)

#: Runbook sections required by AC4.
REQUIRED_RUNBOOK_SECTIONS = (
    "# Host Hygiene",
    "## Install",
    "## Verify",
    "## Rollback",
    "## Failure signatures",
)

#: Failure signatures that must be documented (AC4).
REQUIRED_FAILURE_SIGNATURES = (
    "ENOSPC",
    "df -i",
    "unparsed",
    "infrastructure",
)


def _template_lines() -> list[str]:
    return [
        line.strip()
        for line in TEMPLATE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def test_template_has_valid_tmpfiles_lines() -> None:
    """Every rule line carries the full tmpfiles.d field set with sane values."""
    assert TEMPLATE.exists(), f"template missing: {TEMPLATE}"
    assert _template_lines(), "template contains no rule lines"

    for lineno, line in enumerate(_template_lines(), start=1):
        fields = line.split()
        rule_type, path = fields[0], fields[1]
        assert TYPE_RE.match(rule_type), (
            f"{TEMPLATE.name}:{lineno}: unsupported type {rule_type!r}"
        )
        # Exclusion rules (x/X) carry just "type path" (see man tmpfiles.d(5));
        # content-clean rules (e) carry the full 6-field set.
        if rule_type[0] in "xX":
            assert len(fields) == 2, (
                f"{TEMPLATE.name}:{lineno}: exclusion rules take (type path), "
                f"got {len(fields)} fields: {line!r}"
            )
            continue
        assert len(fields) == 6, (
            f"{TEMPLATE.name}:{lineno}: expected 6 fields "
            f"(type path mode uid gid age), got {len(fields)}: {line!r}"
        )
        _, _, mode, uid, gid, age = fields
        # AC1: age >= 1 day; AC3: threshold must exceed the ~48 min worst
        # case, so enforce at least a full day.
        match = re.fullmatch(r"(\d+)([smhdw]?)", age)
        assert match, f"{TEMPLATE.name}:{lineno}: unparseable age {age!r}"
        magnitude = int(match.group(1))
        unit = match.group(2) or "s"
        seconds = magnitude * {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[unit]
        assert seconds >= 86400, (
            f"{TEMPLATE.name}:{lineno}: age {age!r} is below the 1-day "
            f"minimum (measured worst-case suite ~48 min)"
        )


def test_template_covers_required_patterns() -> None:
    """The candidate churn patterns from the work item are all covered."""
    content = TEMPLATE.read_text(encoding="utf-8").casefold()
    for pattern in REQUIRED_PATTERNS:
        assert pattern in content, f"template lacks churn pattern {pattern!r}"


def test_template_protects_active_pytest_root() -> None:
    """AC3: the active pytest-current root is excluded from cleanup."""
    lines = _template_lines()
    exclusions = [line for line in lines if line.startswith("x ")]
    assert exclusions, "template has no x (exclusion) lines"
    assert any(ACTIVE_ROOT_EXCLUSION in line for line in exclusions), (
        f"no exclusion for {ACTIVE_ROOT_EXCLUSION!r}; the active root could "
        "be aged out"
    )


def test_runbook_is_complete_and_consistent() -> None:
    """AC4: the runbook documents install/verify/rollback + signatures and
    references the committed template."""
    assert RUNBOOK.exists(), f"runbook missing: {RUNBOOK}"
    content = RUNBOOK.read_text(encoding="utf-8")
    for section in REQUIRED_RUNBOOK_SECTIONS:
        assert section in content, f"runbook lacks section heading {section!r}"
    for signature in REQUIRED_FAILURE_SIGNATURES:
        assert signature in content, (
            f"runbook does not document failure signature {signature!r}"
        )
    # The runbook must reference the canonical template (AC1).
    assert "pi-test-tmp.conf" in content, "runbook does not reference the template"
    # The template must be user-scoped where possible (AC3) — the pytest
    # rule must cover all users rather than a single hard-coded name.
    assert "pytest-of-*" in content or "pytest-of-%u" in content


def test_template_parses_with_systemd_tmpfiles() -> None:
    """Live syntax check: the committed template must produce no parse errors.

    Runs ``systemd-tmpfiles --clean --dry-run`` against the template.  The
    dry-run never modifies the filesystem.  Parse failures surface as
    ``Invalid …`` diagnostics on stderr (e.g. a mistyped age or missing
    field); the exit code alone is not asserted because unprivileged runs
    report unrelated permission errors (e.g. root-owned private dirs).

    Skipped when the binary is unavailable (e.g. non-systemd hosts).
    """
    binary = shutil.which("systemd-tmpfiles")
    if binary is None:
        import pytest

        pytest.skip("systemd-tmpfiles not available on this host")

    proc = subprocess.run(
        [
            binary,
            "--clean",
            "--dry-run",
            "--graceful",
            "--exclude-prefix=/tmp/snap-private-tmp",
            "--replace=/etc/tmpfiles.d/pi-test-tmp.conf",
            str(TEMPLATE),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    output = proc.stdout + proc.stderr
    assert "Invalid" not in output, f"systemd-tmpfiles parse errors:\n{output}"
    assert "Unknown" not in output, f"systemd-tmpfiles parse errors:\n{output}"
