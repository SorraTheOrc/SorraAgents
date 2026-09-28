#!/usr/bin/env python3
"""faster-whisper worker for the voice-input pi extension.

The worker owns a single ``faster_whisper.WhisperModel`` for the lifetime of the
pi session and speaks a line-delimited JSON protocol over stdin/stdout so the
model is loaded once per session instead of once per utterance.

Protocol (one JSON object per line)
-----------------------------------

Client -> worker::

    {"type": "start", "model": "small", "device": "cuda",
     "computeType": "float16", "partialIntervalMs": 1000}
    {"type": "feed", "audio": "<base64 little-endian int16 mono 16 kHz PCM>"}
    {"type": "finalise"}
    {"type": "stop"}

Worker -> client::

    {"type": "ready", "model": "small", "device": "cuda", "computeType": "float16"}
    {"type": "partial", "text": "…", "sequence": 1}
    {"type": "final", "text": "…", "sequence": 2}
    {"type": "stopped"}
    {"type": "error", "message": "…"}

Model/device/compute-type come from the ``start`` command, with command-line
arguments as defaults. If ``device=cuda`` cannot be initialised the worker
transparently falls back to ``cpu`` + ``int8`` and reports the effective device
in the ``ready`` message.

Privacy: all transcription is local; the worker never opens a network
connection.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import signal
import sys
from typing import Any, Iterable

SAMPLE_RATE = 16000
BYTES_PER_SAMPLE = 2


def emit(message: dict[str, Any]) -> None:
    """Write one JSON message to stdout and flush immediately."""
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def load_model(model_size: str, device: str, compute_type: str) -> Any:
    """Import and construct a ``faster_whisper.WhisperModel``.

    Imported lazily so the module (and its tests / doctor checks) can run
    without faster-whisper installed; the resulting ``ImportError`` is surfaced
    as an actionable ``error`` message by the caller.
    """
    from faster_whisper import WhisperModel  # type: ignore import-not-found

    return WhisperModel(model_size, device=device, compute_type=compute_type)


def load_model_with_fallback(
    model_size: str, device: str, compute_type: str
) -> tuple[Any, str, str]:
    """Load the model, falling back to CPU int8 when CUDA is unavailable.

    Returns ``(model, effective_device, effective_compute_type)``. An
    ``ImportError`` (faster-whisper missing) is re-raised so the caller can
    report it rather than masking it as a CUDA failure.
    """
    if device == "cuda":
        try:
            return load_model(model_size, "cuda", compute_type), "cuda", compute_type
        except ImportError:
            raise
        except Exception as exc:  # noqa: BLE001 - any CUDA init failure falls back
            emit(
                {
                    "type": "warning",
                    "message": (
                        f"CUDA device unavailable ({exc}); "
                        "falling back to CPU int8 inference."
                    ),
                }
            )
    return load_model(model_size, "cpu", "int8"), "cpu", "int8"


def pcm_to_float32(pcm: bytes) -> Any:
    """Convert little-endian int16 mono PCM to a normalised float32 array."""
    import numpy as np

    samples = np.frombuffer(pcm, dtype="<i2").astype("float32") / 32768.0
    return samples


class TranscriptionSession:
    """Accumulates PCM and produces partial/final transcripts.

    Partials are emitted once per ``partial_interval_samples``; both partials
    and the final transcript re-transcribe the whole accumulated buffer, which
    keeps the live editor text cumulative. Prompt-length dictation is the target
    use case; a bounded sliding window can be layered on later if latency
    demands it.
    """

    def __init__(self, model: Any, partial_interval_samples: int, language: str | None = None):
        self.model = model
        self.partial_interval_samples = partial_interval_samples
        self.language = language
        self.buffer = bytearray()
        self.last_partial_bytes = 0
        self.sequence = 0

    def feed(self, pcm: bytes) -> None:
        self.buffer.extend(pcm)
        if self.partial_interval_samples <= 0:
            return
        new_samples = (len(self.buffer) - self.last_partial_bytes) // BYTES_PER_SAMPLE
        if new_samples >= self.partial_interval_samples:
            self.last_partial_bytes = len(self.buffer)
            self.sequence += 1
            emit(self._message("partial"))

    def finalise(self) -> str:
        self.sequence += 1
        text = self._transcribe()
        emit(self._message("final", text=text))
        # The persistent worker is reused across recordings: clear the
        # utterance buffer so the next recording starts from silence.
        self.reset()
        return text

    def reset(self) -> None:
        """Discard the accumulated utterance (called after finalise)."""
        self.buffer = bytearray()
        self.last_partial_bytes = 0

    def _message(self, kind: str, text: str | None = None) -> dict[str, Any]:
        return {
            "type": kind,
            "text": self._transcribe() if text is None else text,
            "sequence": self.sequence,
        }

    def _transcribe(self) -> str:
        if len(self.buffer) == 0:
            return ""
        audio = pcm_to_float32(bytes(self.buffer))
        segments, _info = self.model.transcribe(audio, language=self.language)
        return " ".join(_segment_text(segment) for segment in segments).strip()


def _segment_text(segment: Any) -> str:
    text = getattr(segment, "text", "")
    return str(text).strip()


def handle_command(
    message: dict[str, Any],
    session: "TranscriptionSession | None",
    args: argparse.Namespace,
) -> "TranscriptionSession | None":
    """Dispatch one protocol command. Returns the (possibly new) session."""
    kind = message.get("type")

    if kind == "start":
        model_size = str(message.get("model") or args.model)
        device = str(message.get("device") or args.device)
        compute_type = str(message.get("computeType") or args.compute_type)
        try:
            model, device, compute_type = load_model_with_fallback(
                model_size, device, compute_type
            )
        except ImportError as exc:
            emit(
                {
                    "type": "error",
                    "message": (
                        "faster-whisper is not installed in the selected Python "
                        f"environment: {exc}. Install it with `pip install "
                        "faster-whisper` (a CUDA build also needs cuDNN/cuBLAS)."
                    ),
                }
            )
            raise SystemExit(1)
        except Exception as exc:  # noqa: BLE001 - report any model load failure
            emit(
                {
                    "type": "error",
                    "message": (
                        f"failed to load faster-whisper model '{model_size}': {exc}"
                    ),
                }
            )
            raise SystemExit(1)

        partial_ms = message.get("partialIntervalMs", args.partial_interval_ms)
        try:
            partial_ms = int(partial_ms)
        except (TypeError, ValueError):
            partial_ms = args.partial_interval_ms
        partial_samples = max(0, partial_ms) * SAMPLE_RATE // 1000
        language = args.language or None
        emit(
            {
                "type": "ready",
                "model": model_size,
                "device": device,
                "computeType": compute_type,
            }
        )
        return TranscriptionSession(model, partial_samples, language=language)

    if kind == "feed":
        if session is None:
            emit({"type": "error", "message": "received 'feed' before 'start'"})
            return session
        try:
            pcm = base64.b64decode(message.get("audio", ""), validate=True)
        except (binascii.Error, ValueError):
            emit({"type": "error", "message": "invalid base64 audio payload in 'feed'"})
            return session
        try:
            session.feed(pcm)
        except Exception as exc:  # noqa: BLE001 - transcription failure is surfaced
            emit({"type": "error", "message": f"transcription failed: {exc}"})
        return session

    if kind == "finalise":
        if session is None:
            emit({"type": "error", "message": "received 'finalise' before 'start'"})
            return session
        try:
            session.finalise()
        except Exception as exc:  # noqa: BLE001 - transcription failure is surfaced
            emit({"type": "error", "message": f"transcription failed: {exc}"})
        return session

    if kind == "stop":
        emit({"type": "stopped"})
        raise SystemExit(0)

    emit({"type": "error", "message": f"unknown command type: {kind!r}"})
    return session


def _handle_signal(_signum: int, _frame: Any) -> None:
    emit({"type": "stopped"})
    raise SystemExit(0)


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="faster-whisper voice-input worker")
    parser.add_argument("--model", default="small")
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--compute-type", default="float16")
    parser.add_argument("--partial-interval-ms", type=int, default=1000)
    parser.add_argument("--language", default="")
    args = parser.parse_args(list(argv) if argv is not None else None)

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    session: TranscriptionSession | None = None
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            emit({"type": "error", "message": f"invalid JSON command: {line[:200]}"})
            continue
        if not isinstance(message, dict):
            emit({"type": "error", "message": "protocol commands must be JSON objects"})
            continue
        session = handle_command(message, session, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
