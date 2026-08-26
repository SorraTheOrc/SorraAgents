#!/usr/bin/env python3
"""Timing wrapper for ``skill/speak/scripts/speak.sh`` (SA-0MTACU3CQ0085KVO).

The ``speak.sh`` script itself is intentionally NOT instrumented internally
(AC4 of SA-0MT319YGQ002E801); instead, callers wrap the invocation so the
call duration is captured. This module is the canonical caller-side wrapper:
it invokes ``speak.sh`` through the shared ``Timer`` utility and emits a
timing report on **stderr** so stdout stays clean (``--stream`` pipes raw
audio to a remote player).

Usage (identical CLI to ``speak.sh``)::

    python3 skill/speak/scripts/speak.py "Text to convert to speech"
    python3 skill/speak/scripts/speak.py --stream "Hello" | aplay

The wrapper passes all arguments through to ``speak.sh`` unchanged, so
existing environment variables (``TTS_API_URL``, ``SPEAK_DIR``) and the
rolling-buffer behaviour are unaffected.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# Make skill/shared importable regardless of cwd.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_REPO_ROOT / "skill") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "skill"))

from shared.timing import Timer

SCRIPT_PATH = Path(__file__).resolve().parent / "speak.sh"


def main(argv: list[str] | None = None) -> int:
    """Invoke speak.sh wrapped in a Timer and emit the timing report.

    Args:
        argv: Command-line arguments (defaults to ``sys.argv[1:]``).

    Returns:
        The speak.sh exit code.
    """
    argv = list(sys.argv[1:] if argv is None else argv)

    with Timer("speak.sh") as timer:
        if "--help" in argv or "-h" in argv:
            # Delegate help to speak.sh so usage text stays in one place.
            proc = subprocess.run(
                ["bash", str(SCRIPT_PATH), *argv],
                env=os.environ.copy(),
                check=False,
            )
        else:
            with Timer("invoke_speak_sh"):
                proc = subprocess.run(
                    ["bash", str(SCRIPT_PATH), *argv],
                    env=os.environ.copy(),
                    check=False,
                )
        # Emit the timing report on stderr so stdout stays clean for
        # --stream (raw audio to stdout).
        print(timer.render(), file=sys.stderr)
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())