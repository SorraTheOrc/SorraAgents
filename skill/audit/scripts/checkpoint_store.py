"""Phase-level checkpoint persistence for the audit runner.

Motivation (SA-0MT6EZUS9004FJ9T): audit runs are killed by the parent Pi
process timeout (~600 s) with NO partial results saved — every restart
redoes the entire audit. This module persists per-phase results to a JSON
checkpoint file keyed by ``issue_id`` + git ``HEAD`` sha so an interrupted
audit resumes from the furthest completed phase instead of from scratch.

Phases checkpointed, in order::

    phase1_parent    Phase 1 parent AC screening (ac_results)
    phase1_children  Phase 1 child screenings + child audit persistence
    phase2           Phase 2 deep analysis (final ac/child results + flags)

Safety rules:

* A checkpoint is resumed only when BOTH the issue id and the git HEAD sha
  match the current run — partial results from a different HEAD or a
  different work item are NEVER reused (stale-checkpoint guarantee).
* ``force=True`` (the ``--force`` audit flag) ignores any existing
  checkpoint and starts fresh: a forced re-audit must never verify from an
  earlier run's partial results.
* Checkpointing is best-effort: any read/write/validation failure prints a
  warning and disables checkpointing for the run — it never affects the
  audit verdict or exit code.
* The checkpoint file is removed once the audit completes successfully, so
  a later run never resumes a finished audit.

The directory is configurable (``--checkpoint-dir`` flag /
``AUDIT_CHECKPOINT_DIR`` env); the default is
``<owning-project>/.worklog/audit-checkpoints`` — inside the worklog data
directory, so it survives process restarts but stays out of the git tree.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

# Version gate: bumping this invalidates all existing checkpoints (they are
# treated as fresh, never resumed) to protect against format drift.
CHECKPOINT_VERSION = 1

ENV_CHECKPOINT_DIR = "AUDIT_CHECKPOINT_DIR"
"""Environment variable overriding the checkpoint directory."""

DEFAULT_SUBDIR_NAME = "audit-checkpoints"
"""Default subdirectory under ``<owning>/.worklog``."""

CHECKPOINT_FILE_SUFFIX = ".checkpoint.json"

PHASE_PARENT = "phase1_parent"
PHASE_CHILDREN = "phase1_children"
PHASE_PHASE2 = "phase2"
PHASES = (PHASE_PARENT, PHASE_CHILDREN, PHASE_PHASE2)

PHASE_LABELS = {
    PHASE_PARENT: "Phase 1 parent screening",
    PHASE_CHILDREN: "Phase 1 child screenings",
    PHASE_PHASE2: "Phase 2 deep analysis",
}

STATUS_PENDING = "pending"
STATUS_IN_PROGRESS = "in_progress"
STATUS_COMPLETED = "completed"


class CheckpointError(Exception):
    """Raised for checkpoint I/O/validation failures.

    Callers catch this and degrade gracefully (best-effort checkpointing).
    """


def resolve_checkpoint_dir(
    owning_root: str | Path | None,
    explicit_dir: str | None = None,
) -> Path | None:
    """Resolve the checkpoint directory for a run.

    Precedence:
      1. ``explicit_dir`` (``--checkpoint-dir`` CLI flag)
      2. ``AUDIT_CHECKPOINT_DIR`` environment variable
      3. ``<owning_root>/.worklog/audit-checkpoints`` (default)

    An empty string for either the flag or the env var disables
    checkpointing. Returns ``None`` when checkpointing is disabled or no
    owning root is available (the default location cannot be derived).
    """
    value = explicit_dir
    if value is None:
        value = os.environ.get(ENV_CHECKPOINT_DIR)
    if value is None:
        if not owning_root:
            return None
        return Path(owning_root) / ".worklog" / DEFAULT_SUBDIR_NAME
    value = str(value).strip()
    if not value:
        return None  # explicit empty value disables checkpointing
    return Path(value)


class CheckpointStore:
    """Per-issue phase checkpoint with resume support.

    A single JSON file per issue (``<issue_id>.checkpoint.json``) records
    the status of each phase and the accumulated pipeline state. Writes are
    atomic (temp file + rename) and best-effort: a failed write logs a
    warning and leaves the previous file intact — the audit continues
    without checkpointing rather than failing.
    """

    def __init__(
        self,
        issue_id: str,
        git_head: str,
        checkpoint_dir: str | Path,
        force: bool = False,
    ) -> None:
        self.issue_id = issue_id
        self.git_head = git_head.lower()
        self.dir = Path(checkpoint_dir)
        self.force = force
        self._resuming = False
        self._data: dict = {
            "version": CHECKPOINT_VERSION,
            "issue_id": issue_id,
            "git_head": self.git_head,
            "phases": {
                p: {"status": STATUS_PENDING}
                for p in PHASES
            },
            "state": {},
        }
        if force:
            # A forced run never resumes: keep the in-memory data fresh.
            return
        self._load()

    # ------------------------------------------------------------------
    # Loading / validation
    # ------------------------------------------------------------------
    def _load(self) -> None:
        try:
            raw = self.path().read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except Exception as exc:  # noqa: BLE001 -- best-effort checkpointing
            print(
                f"Warning: ignoring unreadable audit checkpoint "
                f"{self.path()}: {exc}",
                file=sys.stderr,
            )
            return
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as exc:
            print(
                f"Warning: ignoring corrupt audit checkpoint {self.path()}: {exc}",
                file=sys.stderr,
            )
            return
        if not self._valid(data):
            return  # mismatch/corruption → treated as a fresh run
        self._data = data
        self._resuming = any(
            ph.get("status") == STATUS_COMPLETED
            for ph in data.get("phases", {}).values()
        )

    def _valid(self, data: object) -> bool:
        if not isinstance(data, dict):
            return False
        if data.get("version") != CHECKPOINT_VERSION:
            return False
        if data.get("issue_id") != self.issue_id:
            return False
        if data.get("git_head") != self.git_head:
            return False
        phases = data.get("phases")
        if not isinstance(phases, dict):
            return False
        for phase in PHASES:
            entry = phases.get(phase)
            if not isinstance(entry, dict) or "status" not in entry:
                return False
        return True

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def path(self) -> Path:
        return self.dir / f"{self.issue_id}{CHECKPOINT_FILE_SUFFIX}"

    def _write(self) -> None:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # noqa: BLE001 -- best-effort
            print(
                f"Warning: could not create audit checkpoint directory "
                f"{self.dir}: {exc} — audit continues without checkpointing",
                file=sys.stderr,
            )
            return
        try:
            tmp = self.path().with_name(
                f"{self.path().name}.tmp"
            )
            tmp.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
            tmp.replace(self.path())
        except Exception as exc:  # noqa: BLE001 -- best-effort
            print(
                f"Warning: could not write audit checkpoint {self.path()}: "
                f"{exc} — audit continues without checkpointing",
                file=sys.stderr,
            )

    def clear(self) -> None:
        """Remove the checkpoint file (fresh run / audit completed).

        Best-effort: a failed unlink logs a warning and never raises.
        """
        try:
            self.path().unlink(missing_ok=True)
        except Exception as exc:  # noqa: BLE001 -- best-effort
            print(
                f"Warning: could not clear audit checkpoint {self.path()}: "
                f"{exc}",
                file=sys.stderr,
            )

    # ------------------------------------------------------------------
    # Phase state
    # ------------------------------------------------------------------
    def phase_status(self, phase: str) -> str:
        entry = self._data.get("phases", {}).get(phase) or {}
        return entry.get("status", STATUS_PENDING)

    def completed_phases(self) -> list[str]:
        return [p for p in PHASES if self.phase_status(p) == STATUS_COMPLETED]

    def in_progress_phase(self) -> str | None:
        for p in PHASES:
            if self.phase_status(p) == STATUS_IN_PROGRESS:
                return p
        return None

    def interrupted_phase(self) -> str | None:
        """The phase a previous run died in (in_progress with no completion).

        Only meaningful on the freshly-loaded state — a completed phase can
        not have been interrupted; resume skips only completed phases and
        redoes the interrupted one.
        """
        return self.in_progress_phase()

    @property
    def is_resuming(self) -> bool:
        """True when at least one phase completed in a prior run and the
        issue/HEAD match the current run."""
        return self._resuming

    def accumulated_state(self) -> dict:
        """The pipeline state saved so far (ac_results / child_results /
        phase2 flags), merged across completed phases."""
        return dict(self._data.get("state", {}) or {})

    # ------------------------------------------------------------------
    # Transitions
    # ------------------------------------------------------------------
    def mark_started(self, phase: str) -> None:
        """Record that *phase* started this run (replaces a stale marker).

        The phase keeps any previously-completed status in OTHER phases; a
        phase that is already completed in the loaded store is unchanged by
        this call (the caller only marks phases it is about to re-run).
        """
        entry = self._data.setdefault("phases", {}).setdefault(
            phase, {"status": STATUS_PENDING}
        )
        entry["status"] = STATUS_IN_PROGRESS
        entry["started_at"] = time.time()
        entry.pop("completed_at", None)
        entry.pop("elapsed_s", None)
        self._write()

    def mark_completed(
        self, phase: str, state: dict | None = None
    ) -> None:
        """Record that *phase* completed successfully.

        *state* is merged into the accumulated pipeline state so a resume
        can restore it. Timestamp + duration are recorded for reporting.
        """
        now = time.time()
        entry = self._data.setdefault("phases", {}).setdefault(
            phase, {"status": STATUS_PENDING}
        )
        started = entry.get("started_at")
        entry["status"] = STATUS_COMPLETED
        entry["completed_at"] = now
        entry["elapsed_s"] = (now - started) if started else None
        if state:
            self._data.setdefault("state", {}).update(state)
        self._write()

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------
    def summary(self) -> str:
        """Human-readable per-phase status line (for stderr traces)."""
        parts: list[str] = []
        for phase in PHASES:
            status = self.phase_status(phase)
            label = PHASE_LABELS[phase]
            if status == STATUS_COMPLETED:
                elapsed = self._data.get("phases", {}).get(phase, {}).get(
                    "elapsed_s"
                )
                if elapsed is not None:
                    parts.append(f"{label} done ({elapsed:.0f}s)")
                else:
                    parts.append(f"{label} done")
            elif status == STATUS_IN_PROGRESS:
                parts.append(f"{label} IN PROGRESS")
        return "; ".join(parts) if parts else "no completed phases"