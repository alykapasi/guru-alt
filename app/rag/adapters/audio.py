"""Audio adapter — speech-to-text via the Transcriber seam (lectures, podcasts, memos).

The transcriber yields many short time-stamped segments; we merge consecutive segments into
coarser units (~one chunk's worth of text) so embeddings aren't one-sentence slivers, while
the unit's ``{start, end}`` locator keeps the time span for timestamped citations. Needs a
transcriber in the extraction context; the file is opened from a path (streamed by the pipeline).
"""

from pathlib import Path

from app.rag.adapters.base import ExtractContext, ExtractedUnit

_UNIT_CHAR_TARGET = 1000
"""Merge segments until a unit reaches ~this many chars (matches the chunker's window)."""


class AudioAdapter:
    name = "asr"

    def handles(self, content_type: str) -> bool:
        return content_type.startswith("audio/")

    async def extract(self, path: Path, *, meta: dict, ctx: ExtractContext) -> list[ExtractedUnit]:
        if ctx.transcriber is None:
            raise ValueError("audio ingestion requires a transcriber in the extraction context")
        segments = await ctx.transcriber.transcribe(path, media_type=ctx.media_type)

        units: list[ExtractedUnit] = []
        buffer: list[str] = []
        span_start: float | None = None
        span_end = 0.0
        for segment in segments:
            text = segment.text.strip()
            if not text:
                continue
            if span_start is None:
                span_start = segment.start
            buffer.append(text)
            span_end = segment.end
            if sum(len(part) for part in buffer) >= _UNIT_CHAR_TARGET:
                units.append(_unit(buffer, span_start, span_end))
                buffer, span_start = [], None
        if buffer and span_start is not None:
            units.append(_unit(buffer, span_start, span_end))
        return units


def _unit(parts: list[str], start: float, end: float) -> ExtractedUnit:
    return ExtractedUnit(
        text=" ".join(parts), locator={"start": round(start, 2), "end": round(end, 2)}
    )
