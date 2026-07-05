"""Video adapter — demux the audio track for ASR and OCR sampled keyframes (lectures, screencasts).

A video carries two text signals, and this adapter emits both as located units: spoken narration
(the audio track, transcribed via the ``Transcriber`` seam over a demuxed WAV, ``method="asr"`` with
a ``{start, end}`` span) and on-screen text (sampled keyframes, transcribed via vision-OCR,
``method="ocr"`` with a ``{frame_time}``). It needs a demuxer in the context; the transcriber and
LLM are each optional — each governs one signal, so a video with only narration or only slides still
yields what it can. The video is opened from a path (streamed by the pipeline); the demuxed audio is
spooled to its own temp file so nothing large sits in memory.
"""

import os
import tempfile
from collections.abc import Awaitable
from pathlib import Path

from app.core.config import get_settings
from app.llm import ModelRole
from app.rag.adapters.audio import merge_transcript_segments
from app.rag.adapters.base import ExtractContext, ExtractedUnit
from app.rag.concurrency import gather_bounded
from app.rag.demux import Keyframe
from app.rag.ocr import ocr_image


class VideoAdapter:
    name = "video"

    def handles(self, content_type: str) -> bool:
        return content_type.startswith("video/")

    async def extract(self, path: Path, *, meta: dict, ctx: ExtractContext) -> list[ExtractedUnit]:
        if ctx.demuxer is None:
            raise ValueError("video ingestion requires a demuxer in the extraction context")
        units = await self._transcribe_audio(path, ctx)
        units.extend(await self._ocr_keyframes(path, ctx))
        return units

    async def _transcribe_audio(self, path: Path, ctx: ExtractContext) -> list[ExtractedUnit]:
        """Demux the audio track to a temp WAV and transcribe it (skipped if silent or no ASR)."""
        if ctx.transcriber is None:
            return []
        assert ctx.demuxer is not None  # guarded by extract()
        fd, tmp = tempfile.mkstemp(suffix=".wav", dir=get_settings().ingest_tmp_dir)
        os.close(fd)
        audio_path = Path(tmp)
        try:
            if not await ctx.demuxer.extract_audio(path, audio_path):
                return []  # video has no audio track
            segments = await ctx.transcriber.transcribe(audio_path, media_type="audio/wav")
        finally:
            audio_path.unlink(missing_ok=True)
        return merge_transcript_segments(segments, method="asr")

    async def _ocr_keyframes(self, path: Path, ctx: ExtractContext) -> list[ExtractedUnit]:
        """Sample keyframes and vision-OCR them concurrently (bounded), preserving time order."""
        if ctx.llm is None:
            return []
        assert ctx.demuxer is not None  # guarded by extract()
        keyframes = await ctx.demuxer.extract_keyframes(path)
        coros: list[Awaitable[ExtractedUnit | None]] = [
            self._ocr_keyframe(frame, ctx) for frame in keyframes
        ]
        results = await gather_bounded(coros, ctx.ocr_concurrency)
        return [unit for unit in results if unit is not None]

    @staticmethod
    async def _ocr_keyframe(frame: Keyframe, ctx: ExtractContext) -> ExtractedUnit | None:
        assert ctx.llm is not None  # guarded by the caller
        text, usage = await ocr_image(ctx.llm, frame.image, media_type=frame.media_type)
        ctx.record_usage(ModelRole.VISION, usage)
        if text.strip():
            return ExtractedUnit(text=text, locator={"frame_time": frame.time}, method="ocr")
        return None
