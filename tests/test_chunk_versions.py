"""Chunks carry the pipeline version that wrote them; superseded ones stay out of reach (S29)."""

import uuid
from datetime import UTC, datetime

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm.registry import fake_llm_client
from app.models.learner import Learner
from app.models.source import Chunk, SourceKind
from app.rag import pipeline, retrieval
from app.rag.scope import SourceScope
from app.services import ingestion
from app.storage import InMemoryBlobStore

TEXT = b"Photosynthesis converts light energy into chemical energy in chloroplasts."


async def _ingested(session: AsyncSession, learner: Learner) -> list[Chunk]:
    store = InMemoryBlobStore()
    source = await ingestion.create_source(
        session,
        store,
        learner_id=learner.id,
        kind=SourceKind.FILE,
        origin="notes.txt",
        content_type="text/plain",
        data=TEXT,
    )
    await ingestion.ingest_source(session, store, fake_llm_client(), source.id)
    return list((await session.scalars(select(Chunk).where(Chunk.source_id == source.id))).all())


async def test_new_chunks_carry_the_current_pipeline_version(
    db_session: AsyncSession, api_learner: Learner
) -> None:
    chunks = await _ingested(db_session, api_learner)
    assert chunks
    assert {c.pipeline_version for c in chunks} == {pipeline.PIPELINE_VERSION}
    assert all(c.superseded_at is None for c in chunks)


async def test_retrieval_never_returns_a_superseded_chunk(
    db_session: AsyncSession, api_learner: Learner
) -> None:
    [chunk, *_] = await _ingested(db_session, api_learner)
    chunk.superseded_at = datetime.now(UTC)
    chunk.embedding = None
    await db_session.flush()

    hits = await retrieval.retrieve(
        db_session,
        fake_llm_client(),
        "photosynthesis chloroplasts",
        scope=SourceScope(learner_id=api_learner.id),
    )

    assert chunk.id not in {h.chunk_id for h in hits}


async def test_the_chunk_endpoint_marks_a_superseded_chunk(
    api_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    [chunk, *_] = await _ingested(db_session, api_learner)
    chunk.superseded_at = datetime.now(UTC)
    chunk.embedding = None
    await db_session.commit()

    body = (await api_client.get(f"/api/v1/chunks/{chunk.id}")).json()
    listing = (await api_client.get(f"/api/v1/sources/{chunk.source_id}/chunks")).json()

    assert body["superseded"] is True
    assert body["text"] == chunk.text
    assert chunk.id not in {uuid.UUID(c["id"]) for c in listing}, "the listing shows current"
