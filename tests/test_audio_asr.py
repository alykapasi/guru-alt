"""Audio ASR ingestion (Phase 4b): the Transcriber seam + audio adapter, exercised offline.

Real transcription (faster-whisper) is an optional dep tested heavily later; here a
``FakeTranscriber`` drives the adapter, grouping, provenance, and pipeline wiring.
"""

import uuid
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.llm.registry import fake_llm_client
from app.models.learner import Learner
from app.models.source import Chunk, SourceKind, SourceStatus
from app.rag.adapters import ExtractContext, select_adapter
from app.rag.adapters.audio import AudioAdapter
from app.rag.transcription import FakeTranscriber, Transcriber, TranscriptSegment, build_transcriber
from app.services import ingestion
from app.storage import InMemoryBlobStore


def _audio(tmp_path: Path) -> Path:
    path = tmp_path / "lecture.mp3"
    path.write_bytes(b"not-real-audio-bytes")  # the fake transcriber ignores the content
    return path


async def test_audio_adapter_merges_short_segments_into_one_timestamped_unit(
    tmp_path: Path,
) -> None:
    segments = [
        TranscriptSegment(text="Hello and welcome to the lecture.", start=0.0, end=3.2),
        TranscriptSegment(text="Today we cover photosynthesis.", start=3.2, end=6.5),
    ]
    ctx = ExtractContext(transcriber=FakeTranscriber(segments))

    units = await AudioAdapter().extract(_audio(tmp_path), meta={}, ctx=ctx)

    assert len(units) == 1  # both short segments merge into a single unit
    assert units[0].locator == {"start": 0.0, "end": 6.5}  # span of the merged segments
    assert "photosynthesis" in units[0].text
    assert units[0].method is None  # falls back to the adapter's name ("asr")


async def test_audio_adapter_splits_over_char_target_preserving_spans(tmp_path: Path) -> None:
    long_text = "word " * 250  # ~1250 chars — each segment already exceeds the unit target
    segments = [
        TranscriptSegment(text=long_text, start=0.0, end=10.0),
        TranscriptSegment(text=long_text, start=10.0, end=20.5),
    ]
    ctx = ExtractContext(transcriber=FakeTranscriber(segments))

    units = await AudioAdapter().extract(_audio(tmp_path), meta={}, ctx=ctx)

    assert len(units) == 2
    assert units[0].locator["start"] == 0.0
    assert units[1].locator["end"] == 20.5


async def test_audio_adapter_requires_a_transcriber(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="transcriber"):
        await AudioAdapter().extract(_audio(tmp_path), meta={}, ctx=ExtractContext())


def test_registry_dispatches_audio() -> None:
    assert isinstance(select_adapter("audio/mpeg"), AudioAdapter)
    assert isinstance(select_adapter("audio/wav"), AudioAdapter)


def test_build_transcriber_satisfies_the_seam() -> None:
    # Constructs without loading a model (faster-whisper is imported lazily on first transcribe).
    assert isinstance(build_transcriber(Settings()), Transcriber)


async def test_ingest_audio_records_timestamp_provenance(db_session: AsyncSession) -> None:
    store = InMemoryBlobStore()
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    db_session.add(learner)
    await db_session.flush()

    source = await ingestion.create_source(
        db_session,
        store,
        learner_id=learner.id,
        kind=SourceKind.FILE,
        origin="lecture.mp3",
        content_type="audio/mpeg",
        data=b"not-real-audio-bytes",
    )
    transcriber = FakeTranscriber(
        [TranscriptSegment(text="Mitochondria are the powerhouse of the cell.", start=0.0, end=4.0)]
    )
    result = await ingestion.ingest_source(
        db_session, store, fake_llm_client(), source.id, transcriber=transcriber
    )
    assert result.status == SourceStatus.DONE

    chunks = (
        await db_session.scalars(
            select(Chunk).where(Chunk.source_id == source.id).order_by(Chunk.ordinal)
        )
    ).all()
    assert chunks
    assert all(c.provenance["method"] == "asr" for c in chunks)
    assert chunks[0].provenance["start"] == 0.0 and chunks[0].provenance["end"] == 4.0
    assert "Mitochondria" in chunks[0].text
