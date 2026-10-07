"""Exact ingestion slots, global and per learner, held as advisory locks (S37)."""

import uuid

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import Settings, get_settings
from app.llm.registry import fake_llm_client
from app.models.learner import Learner
from app.models.source import SourceKind, SourceStatus
from app.services import ingest_slots, ingestion
from app.storage import InMemoryBlobStore


async def test_the_global_slots_are_exact(engine: AsyncEngine) -> None:
    settings = Settings(ingest_max_concurrent_jobs=2, ingest_max_jobs_per_learner=4)
    held = [await ingest_slots.take(engine, uuid.uuid4(), settings) for _ in range(2)]
    try:
        assert all(held)
        assert await ingest_slots.take(engine, uuid.uuid4(), settings) is None
    finally:
        for slots in held:
            if slots is not None:
                await slots.release()


async def test_one_learner_cannot_take_every_slot(engine: AsyncEngine) -> None:
    settings = Settings(ingest_max_concurrent_jobs=4, ingest_max_jobs_per_learner=1)
    learner = uuid.uuid4()
    first = await ingest_slots.take(engine, learner, settings)
    other = await ingest_slots.take(engine, uuid.uuid4(), settings)
    try:
        assert first is not None and other is not None
        assert await ingest_slots.take(engine, learner, settings) is None
    finally:
        for slots in (first, other):
            if slots is not None:
                await slots.release()


async def test_released_slots_can_be_taken_again() -> None:
    """The pool reuses connections, and a session lock survives close() — release unlocks.

    A pooled engine, so the released connection stays open in the pool rather than closing;
    with no unlock its lock would still be held there. The check is that a *different*
    connection can then take the slot.
    """
    pooled = create_async_engine(get_settings().database_url, pool_size=1, max_overflow=0)
    settings = Settings(ingest_max_concurrent_jobs=1)
    try:
        first = await ingest_slots.take(pooled, uuid.uuid4(), settings)
        assert first is not None
        await first.release()
        await first.release()  # idempotent

        # The released connection now sits idle in the pool, its session still open.
        other = create_async_engine(get_settings().database_url, poolclass=NullPool)
        try:
            again = await ingest_slots.take(other, uuid.uuid4(), settings)
            assert again is not None
            await again.release()
        finally:
            await other.dispose()
    finally:
        await pooled.dispose()


async def _source(session: AsyncSession, store: InMemoryBlobStore, learner: Learner):
    return await ingestion.create_source(
        session,
        store,
        learner_id=learner.id,
        kind=SourceKind.FILE,
        origin=f"{uuid.uuid4().hex}.txt",
        content_type="text/plain",
        data=uuid.uuid4().hex.encode() + b" The cell releases energy.",
    )


async def _learner(session: AsyncSession) -> Learner:
    learner = Learner(handle=f"q-{uuid.uuid4().hex[:8]}")
    session.add(learner)
    await session.flush()
    return learner


async def test_ingest_waits_when_no_slot_is_free(
    db_session: AsyncSession, engine: AsyncEngine
) -> None:
    store = InMemoryBlobStore()
    learner = await _learner(db_session)
    source = await _source(db_session, store, learner)
    settings = Settings(ingest_max_concurrent_jobs=1)
    busy = await ingest_slots.take(engine, uuid.uuid4(), settings)
    try:
        result = await ingestion.ingest_source(
            db_session, store, fake_llm_client(), source.id, settings=settings
        )
    finally:
        assert busy is not None
        await busy.release()

    assert result is None
    await db_session.refresh(source)
    assert (source.status, source.attempts) == (SourceStatus.PENDING, 0)


async def test_ingest_releases_its_slots_even_when_the_job_fails(
    db_session: AsyncSession, engine: AsyncEngine, monkeypatch
) -> None:
    store = InMemoryBlobStore()
    learner = await _learner(db_session)
    source = await _source(db_session, store, learner)
    settings = Settings(ingest_max_concurrent_jobs=1)

    async def boom(*args, **kwargs):
        raise ConnectionError("provider unreachable")

    monkeypatch.setattr("app.rag.pipeline.run", boom)
    await ingestion.ingest_source(
        db_session, store, fake_llm_client(), source.id, settings=settings
    )

    slots = await ingest_slots.take(engine, uuid.uuid4(), settings)
    assert slots is not None
    await slots.release()


async def test_a_finished_job_starts_the_next_waiting_upload(db_session: AsyncSession) -> None:
    store = InMemoryBlobStore()
    learner = await _learner(db_session)
    first = await _source(db_session, store, learner)
    waiting = await _source(db_session, store, learner)
    await db_session.commit()
    queued: list[uuid.UUID] = []

    async def enqueue(source_id: uuid.UUID) -> None:
        queued.append(source_id)

    await ingestion.ingest_source(db_session, store, fake_llm_client(), first.id, enqueue=enqueue)

    assert queued == [waiting.id]
