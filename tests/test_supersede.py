"""Re-ingesting a source keeps what old citations point at (S29)."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm.registry import fake_llm_client
from app.models.chat import Conversation, Message
from app.models.content import ContentBlock
from app.models.learner import Learner
from app.models.source import Chunk, ChunkKC, Source, SourceKind
from app.services import content as content_svc
from app.services import ingestion
from app.storage import InMemoryBlobStore

TEXT = b"Photosynthesis converts light energy into chemical energy in chloroplasts."


async def _ingest(session: AsyncSession, store: InMemoryBlobStore, learner: Learner) -> Source:
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
    return source


async def _chunks(session: AsyncSession, source: Source) -> list[Chunk]:
    # populate_existing rather than expire_all: expiring would also expire the test's own
    # objects, and reading an expired attribute is sync IO an async session cannot do.
    return list(
        (
            await session.scalars(
                select(Chunk)
                .where(Chunk.source_id == source.id)
                .order_by(Chunk.superseded_at)
                .execution_options(populate_existing=True)
            )
        ).all()
    )


async def _cite_in_message(session: AsyncSession, learner: Learner, chunk: Chunk) -> None:
    conversation = Conversation(learner_id=learner.id)
    session.add(conversation)
    await session.flush()
    session.add(
        Message(
            conversation_id=conversation.id,
            role="assistant",
            content="as shown [1]",
            citations=[{"marker": 1, "chunk_id": str(chunk.id), "source_id": str(chunk.source_id)}],
        )
    )
    await session.commit()


async def _reingest(session: AsyncSession, store: InMemoryBlobStore, source: Source) -> None:
    await ingestion.reset_for_reingest(session, source.id)
    await ingestion.ingest_source(session, store, fake_llm_client(), source.id)


async def test_a_cited_chunk_survives_reingest_as_superseded(
    db_session: AsyncSession, api_learner: Learner
) -> None:
    store = InMemoryBlobStore()
    source = await _ingest(db_session, store, api_learner)
    [original] = await _chunks(db_session, source)
    await _cite_in_message(db_session, api_learner, original)

    await _reingest(db_session, store, source)

    chunks = await _chunks(db_session, source)
    old = next(c for c in chunks if c.id == original.id)
    current = [c for c in chunks if c.superseded_at is None]
    assert old.superseded_at is not None
    assert old.text == original.text
    assert old.embedding is None
    assert len(current) == 1 and current[0].id != original.id
    tags = await db_session.scalars(select(ChunkKC).where(ChunkKC.chunk_id == old.id))
    assert list(tags) == []


async def test_an_uncited_chunk_is_deleted_on_reingest(
    db_session: AsyncSession, api_learner: Learner
) -> None:
    store = InMemoryBlobStore()
    source = await _ingest(db_session, store, api_learner)
    [original] = await _chunks(db_session, source)

    await _reingest(db_session, store, source)

    assert original.id not in {c.id for c in await _chunks(db_session, source)}


async def test_a_lesson_block_citation_also_keeps_the_chunk(
    db_session: AsyncSession, api_learner: Learner
) -> None:
    store = InMemoryBlobStore()
    source = await _ingest(db_session, store, api_learner)
    [original] = await _chunks(db_session, source)
    db_session.add(
        ContentBlock(
            learner_id=api_learner.id,
            kc_ids=[],
            block_type="lesson",
            body="x",
            citations=[{"chunk_id": str(original.id), "source_id": str(source.id)}],
            cache_key=f"k-{uuid.uuid4().hex}",
            model="fake-1",
        )
    )
    await db_session.commit()

    await _reingest(db_session, store, source)

    assert original.id in {c.id for c in await _chunks(db_session, source)}


async def test_a_second_reingest_leaves_earlier_history_alone(
    db_session: AsyncSession, api_learner: Learner
) -> None:
    store = InMemoryBlobStore()
    source = await _ingest(db_session, store, api_learner)
    [original] = await _chunks(db_session, source)
    await _cite_in_message(db_session, api_learner, original)

    await _reingest(db_session, store, source)
    await _reingest(db_session, store, source)

    chunks = await _chunks(db_session, source)
    assert original.id in {c.id for c in chunks}
    assert len([c for c in chunks if c.superseded_at is None]) == 1


async def test_deleting_the_source_takes_its_history_with_it(
    db_session: AsyncSession, api_learner: Learner
) -> None:
    store = InMemoryBlobStore()
    source = await _ingest(db_session, store, api_learner)
    [original] = await _chunks(db_session, source)
    await _cite_in_message(db_session, api_learner, original)
    await _reingest(db_session, store, source)

    await db_session.delete(await db_session.get(Source, source.id))
    await db_session.commit()

    assert await db_session.get(Chunk, original.id) is None


async def test_the_support_checker_still_reads_a_superseded_citation(
    db_session: AsyncSession, api_learner: Learner
) -> None:
    store = InMemoryBlobStore()
    source = await _ingest(db_session, store, api_learner)
    [original] = await _chunks(db_session, source)
    block = ContentBlock(
        learner_id=api_learner.id,
        kc_ids=[],
        block_type="lesson",
        body="Photosynthesis happens in chloroplasts.",
        citations=[{"chunk_id": str(original.id), "source_id": str(source.id)}],
        cache_key=f"k-{uuid.uuid4().hex}",
        model="fake-1",
    )
    db_session.add(block)
    await db_session.commit()
    await _reingest(db_session, store, source)

    passages = await content_svc._cited_passages(db_session, block)

    assert [p.chunk_id for p in passages] == [original.id]
