"""Speech-to-text seam for audio/video ingestion (TECHNICAL_DESIGN §6.1).

A ``Transcriber`` turns an audio file into time-stamped ``TranscriptSegment``s. The default
implementation wraps **faster-whisper**, imported lazily so the heavy runtime (an optional
``asr`` extra) is only required when audio is actually transcribed — tests and the doc/web
paths never touch it. ``FakeTranscriber`` gives deterministic segments offline.

The seam mirrors the LLM seam: services depend on the ``Transcriber`` protocol, never on a
concrete engine, and the worker builds the concrete one from settings.
"""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from app.core.config import Settings


@dataclass(frozen=True)
class TranscriptSegment:
    """A span of transcribed speech and the time window (seconds) it covers."""

    text: str
    start: float
    end: float


@runtime_checkable
class Transcriber(Protocol):
    """Transcribes an audio file into time-stamped segments."""

    async def transcribe(self, path: Path, *, media_type: str) -> list[TranscriptSegment]: ...


class FakeTranscriber:
    """Deterministic transcriber for tests — returns preset segments, ignores the file."""

    def __init__(self, segments: Sequence[TranscriptSegment]) -> None:
        self._segments = list(segments)

    async def transcribe(self, path: Path, *, media_type: str) -> list[TranscriptSegment]:
        return list(self._segments)


class FasterWhisperTranscriber:
    """Transcriber backed by faster-whisper. The model loads lazily on first use and the
    (CPU/GPU-bound) transcription runs in a worker thread so it never blocks the event loop."""

    def __init__(self, model: str, *, device: str = "cpu", compute_type: str = "int8") -> None:
        self._model_name = model
        self._device = device
        self._compute_type = compute_type
        self._model: Any = None

    def _load(self) -> Any:
        if self._model is None:
            try:
                from faster_whisper import WhisperModel  # ty: ignore[unresolved-import]
            except ModuleNotFoundError as exc:  # pragma: no cover - exercised in prod/dev only
                raise RuntimeError(
                    "audio transcription needs faster-whisper; install the 'asr' extra "
                    "(uv sync --extra asr)"
                ) from exc
            self._model = WhisperModel(
                self._model_name, device=self._device, compute_type=self._compute_type
            )
        return self._model

    async def transcribe(self, path: Path, *, media_type: str) -> list[TranscriptSegment]:
        return await asyncio.to_thread(self._transcribe_sync, path)

    def _transcribe_sync(self, path: Path) -> list[TranscriptSegment]:  # pragma: no cover - heavy
        model = self._load()
        segments, _info = model.transcribe(str(path))
        return [
            TranscriptSegment(text=s.text, start=float(s.start), end=float(s.end)) for s in segments
        ]


def build_transcriber(settings: Settings) -> Transcriber:
    """Construct the configured transcriber (model loads lazily, so this is cheap)."""
    return FasterWhisperTranscriber(
        settings.asr_model, device=settings.asr_device, compute_type=settings.asr_compute_type
    )
