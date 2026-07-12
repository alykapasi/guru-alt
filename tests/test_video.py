"""Video ingestion (Phase 4b): the MediaDemuxer seam + video adapter, exercised offline.

Real demux (the ffmpeg/ffprobe binaries) and transcription (faster-whisper) are heavy and
tested later; here a ``FakeDemuxer`` + ``FakeTranscriber`` + fake vision LLM drive the
adapter's two signals — audio-track ASR and keyframe OCR — plus provenance, keyframe
concurrency, and pipeline wiring.
"""

import asyncio
import uuid
from collections.abc import Sequence
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.llm import ChatMessage, ChatResponse, ModelRole, ToolDef
from app.llm.providers import FakeProvider
from app.llm.registry import LLMClient, ModelSpec, fake_llm_client
from app.models.learner import Learner
from app.models.source import Chunk, SourceKind, SourceStatus
from app.rag.adapters import ExtractContext, select_adapter
from app.rag.adapters.video import VideoAdapter
from app.rag.demux import FakeDemuxer, Keyframe, MediaDemuxer, build_demuxer
from app.rag.transcription import FakeTranscriber, TranscriptSegment
from app.services import ingestion
from app.storage import InMemoryBlobStore


def _video(tmp_path: Path) -> Path:
    path = tmp_path / "lecture.mp4"
    path.write_bytes(b"not-real-video-bytes")  # the fake demuxer ignores the content
    return path


def _frames(n: int) -> list[Keyframe]:
    """`n` keyframes 10s apart, each carrying (ignored) image bytes."""
    return [
        Keyframe(time=float(i * 10), image=b"png-bytes", media_type="image/png") for i in range(n)
    ]


async def test_video_adapter_yields_asr_and_keyframe_units(tmp_path: Path) -> None:
    demuxer = FakeDemuxer(has_audio=True, keyframes=_frames(2))
    transcriber = FakeTranscriber(
        [TranscriptSegment(text="Welcome to the lecture on cells.", start=0.0, end=4.0)]
    )
    ctx = ExtractContext(
        content_type="video/mp4",
        llm=fake_llm_client("slide: mitochondria"),
        transcriber=transcriber,
        demuxer=demuxer,
    )

    units = await VideoAdapter().extract(_video(tmp_path), meta={}, ctx=ctx)

    asr = [u for u in units if u.method == "asr"]
    ocr = [u for u in units if u.method == "ocr"]
    assert len(asr) == 1
    assert asr[0].locator == {"start": 0.0, "end": 4.0}  # the transcript's time span
    assert "cells" in asr[0].text
    assert len(ocr) == 2
    assert [u.locator["frame_time"] for u in ocr] == [0.0, 10.0]  # keyframe timestamps, in order
    assert all("mitochondria" in u.text for u in ocr)


async def test_video_adapter_requires_a_demuxer(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="demuxer"):
        await VideoAdapter().extract(_video(tmp_path), meta={}, ctx=ExtractContext())


async def test_video_adapter_skips_asr_when_no_audio_track(tmp_path: Path) -> None:
    ctx = ExtractContext(
        llm=fake_llm_client("on-screen text"),
        transcriber=FakeTranscriber([TranscriptSegment(text="unused", start=0.0, end=1.0)]),
        demuxer=FakeDemuxer(has_audio=False, keyframes=_frames(1)),
    )

    units = await VideoAdapter().extract(_video(tmp_path), meta={}, ctx=ctx)

    assert units  # keyframe units still present
    assert all(u.method == "ocr" for u in units)  # no ASR units without an audio track


async def test_video_adapter_transcribes_audio_without_a_vision_llm(tmp_path: Path) -> None:
    # No LLM => keyframes aren't OCR'd, but the demuxed audio is still transcribed.
    ctx = ExtractContext(
        transcriber=FakeTranscriber([TranscriptSegment(text="audio only", start=0.0, end=2.0)]),
        demuxer=FakeDemuxer(has_audio=True, keyframes=_frames(3)),
    )

    units = await VideoAdapter().extract(_video(tmp_path), meta={}, ctx=ctx)

    assert [u.method for u in units] == ["asr"]


def test_registry_dispatches_video() -> None:
    assert isinstance(select_adapter("video/mp4"), VideoAdapter)
    assert isinstance(select_adapter("video/webm"), VideoAdapter)


def test_build_demuxer_satisfies_the_seam() -> None:
    # Constructs without shelling out (ffmpeg is only invoked on first extract).
    assert isinstance(build_demuxer(Settings()), MediaDemuxer)


# --- keyframe OCR concurrency (reuses gather_bounded, like the scanned-PDF path) -------------


class _CountingVisionProvider(FakeProvider):
    """A fake vision provider that records the peak number of concurrent OCR calls."""

    def __init__(self, reply: str = "frame text") -> None:
        super().__init__(reply=reply)
        self.inflight = 0
        self.max_inflight = 0

    async def complete(
        self,
        *,
        model: str,
        messages: Sequence[ChatMessage],
        system: str | None = None,
        max_tokens: int = 1024,
        tools: Sequence[ToolDef] | None = None,
    ) -> ChatResponse:
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            await asyncio.sleep(0.02)  # hold the slot so genuine overlap is observable
            return await super().complete(
                model=model, messages=messages, system=system, max_tokens=max_tokens
            )
        finally:
            self.inflight -= 1


async def test_video_keyframes_ocr_concurrently_bounded(tmp_path: Path) -> None:
    provider = _CountingVisionProvider()
    client = LLMClient({"fake": provider}, {r: ModelSpec("fake", "fake-1") for r in ModelRole})
    ctx = ExtractContext(
        llm=client,
        demuxer=FakeDemuxer(has_audio=False, keyframes=_frames(6)),
        ocr_concurrency=3,
    )

    units = await VideoAdapter().extract(_video(tmp_path), meta={}, ctx=ctx)

    # Every keyframe transcribed, in timestamp order despite out-of-order completion.
    assert [u.locator["frame_time"] for u in units] == [0.0, 10.0, 20.0, 30.0, 40.0, 50.0]
    assert len(ctx.usage_log) == 6
    assert 1 < provider.max_inflight <= 3  # concurrent, but never over the limit


async def test_ingest_video_records_asr_and_keyframe_provenance(db_session: AsyncSession) -> None:
    store = InMemoryBlobStore()
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    db_session.add(learner)
    await db_session.flush()

    source = await ingestion.create_source(
        db_session,
        store,
        learner_id=learner.id,
        kind=SourceKind.FILE,
        origin="lecture.mp4",
        content_type="video/mp4",
        data=b"not-real-video-bytes",
    )
    demuxer = FakeDemuxer(has_audio=True, keyframes=_frames(1))
    transcriber = FakeTranscriber(
        [TranscriptSegment(text="The cell is the basic unit of life.", start=0.0, end=5.0)]
    )
    result = await ingestion.ingest_source(
        db_session,
        store,
        fake_llm_client("on-screen slide text"),
        source.id,
        transcriber=transcriber,
        demuxer=demuxer,
    )
    assert result.status == SourceStatus.DONE

    chunks = (
        await db_session.scalars(
            select(Chunk).where(Chunk.source_id == source.id).order_by(Chunk.ordinal)
        )
    ).all()
    assert chunks
    methods = {c.provenance["method"] for c in chunks}
    assert methods == {"asr", "ocr"}  # both signals landed with honest provenance
    asr_chunk = next(c for c in chunks if c.provenance["method"] == "asr")
    assert asr_chunk.provenance["start"] == 0.0 and asr_chunk.provenance["end"] == 5.0
    ocr_chunk = next(c for c in chunks if c.provenance["method"] == "ocr")
    assert ocr_chunk.provenance["frame_time"] == 0.0
