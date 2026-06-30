"""Ingestion pipeline + service: extract → chunk → embed → store, with status machine."""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm.registry import fake_llm_client
from app.models.learner import Learner
from app.models.source import Chunk, SourceKind, SourceStatus
from app.rag.adapters.base import ExtractedUnit
from app.rag.chunking import chunk_units, normalize
from app.services import ingestion
from app.storage import InMemoryBlobStore


async def _make_source(
    session: AsyncSession,
    store: InMemoryBlobStore,
    *,
    data: bytes,
    content_type: str = "text/plain",
):
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    session.add(learner)
    await session.flush()
    return await ingestion.create_source(
        session,
        store,
        learner_id=learner.id,
        kind=SourceKind.FILE,
        origin="notes.txt",
        content_type=content_type,
        data=data,
    )


async def _chunk_count(session: AsyncSession, source_id: uuid.UUID) -> int:
    return (
        await session.scalar(
            select(func.count()).select_from(Chunk).where(Chunk.source_id == source_id)
        )
    ) or 0


# --- chunking (pure) --------------------------------------------------------


def test_normalize_collapses_whitespace() -> None:
    assert normalize("a\n\n  b\tc ") == "a b c"


def test_chunk_units_windows_long_text_with_overlap() -> None:
    text = " ".join(f"word{i}" for i in range(800))
    units = chunk_units([ExtractedUnit(text=text)], size=200, overlap=50)
    assert len(units) > 1
    starts = [u.locator["char_start"] for u in units]
    assert starts[0] == 0
    assert starts == sorted(starts) and len(set(starts)) == len(starts)  # strictly increasing
    assert all(u.text for u in units)


def test_chunk_units_short_text_single_window() -> None:
    units = chunk_units([ExtractedUnit(text="short text")], size=200, overlap=50)
    assert [u.text for u in units] == ["short text"]


def test_chunk_units_empty_yields_nothing() -> None:
    assert chunk_units([ExtractedUnit(text="   ")]) == []


# --- pipeline + service (DB) ------------------------------------------------


async def test_ingest_txt_creates_embedded_chunks(db_session: AsyncSession) -> None:
    store = InMemoryBlobStore()
    source = await _make_source(db_session, store, data=b"The cell is the unit of life.")
    result = await ingestion.ingest_source(db_session, store, fake_llm_client(), source.id)

    assert result.status == SourceStatus.DONE
    assert result.meta["chunk_count"] >= 1
    chunks = (await db_session.scalars(select(Chunk).where(Chunk.source_id == source.id))).all()
    assert len(chunks) >= 1
    assert len(chunks[0].embedding) == 768
    assert chunks[0].provenance["source_id"] == str(source.id)
    assert chunks[0].provenance["method"] == "text"


async def test_ingest_is_idempotent(db_session: AsyncSession) -> None:
    store = InMemoryBlobStore()
    big = " ".join(f"sentence number {i} about photosynthesis." for i in range(200)).encode()
    source = await _make_source(db_session, store, data=big)

    await ingestion.ingest_source(db_session, store, fake_llm_client(), source.id)
    first = await _chunk_count(db_session, source.id)
    await ingestion.ingest_source(db_session, store, fake_llm_client(), source.id)
    second = await _chunk_count(db_session, source.id)

    assert first > 0
    assert first == second  # replaced, not duplicated


async def test_ingest_empty_text_fails(db_session: AsyncSession) -> None:
    store = InMemoryBlobStore()
    source = await _make_source(db_session, store, data=b"   \n\t ")
    result = await ingestion.ingest_source(db_session, store, fake_llm_client(), source.id)

    assert result.status == SourceStatus.FAILED
    assert result.error
    assert await _chunk_count(db_session, source.id) == 0


async def test_ingest_unsupported_type_fails(db_session: AsyncSession) -> None:
    store = InMemoryBlobStore()
    source = await _make_source(
        db_session, store, data=b"\x00\x01\x02", content_type="application/zip"
    )
    result = await ingestion.ingest_source(db_session, store, fake_llm_client(), source.id)

    assert result.status == SourceStatus.FAILED
    assert "adapter" in (result.error or "").lower()
    assert await _chunk_count(db_session, source.id) == 0
