"""Ingestion in committed stages, resumed where it stopped (S37)."""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.learner import Learner
from app.models.source import SourceKind, StagedChunk
from app.services import ingestion
from app.storage import InMemoryBlobStore

# Paragraphs long enough that the chunker (1000-char windows) makes several chunks.
LONG = "\n\n".join(
    f"Paragraph {i}: " + "The mitochondrion releases energy from glucose inside the cell. " * 18
    for i in range(5)
).encode()


async def _source(session: AsyncSession, store: InMemoryBlobStore, *, data: bytes = LONG, **kw):
    learner = Learner(handle=f"s-{uuid.uuid4().hex[:8]}")
    session.add(learner)
    await session.flush()
    return await ingestion.create_source(
        session,
        store,
        learner_id=learner.id,
        kind=SourceKind.FILE,
        origin="notes.txt",
        content_type="text/plain",
        data=data,
        **kw,
    )


async def _staged(session: AsyncSession, source_id: uuid.UUID) -> int:
    return (
        await session.scalar(
            select(func.count()).select_from(StagedChunk).where(StagedChunk.source_id == source_id)
        )
        or 0
    )


async def test_staged_chunks_go_with_their_source(db_session: AsyncSession) -> None:
    from app.models.source import Source

    store = InMemoryBlobStore()
    source = await _source(db_session, store)
    db_session.add(
        StagedChunk(
            source_id=source.id,
            ordinal=0,
            text="x",
            embedding=[0.0] * get_settings().embed_dim,
            embedding_space="fake",
            pipeline_version=1,
            provenance={},
        )
    )
    await db_session.commit()

    await db_session.delete(await db_session.get(Source, source.id))
    await db_session.commit()

    assert await _staged(db_session, source.id) == 0
