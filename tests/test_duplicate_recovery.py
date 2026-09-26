"""A text duplicate never silently grounds nothing (S77)."""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.llm.registry import fake_llm_client
from app.models.learner import Learner
from app.models.source import Chunk, Source, SourceKind, SourceStatus
from app.services import ingestion
from app.storage import InMemoryBlobStore

BRITISH = b"The colour of the neighbouring fibre was analysed in the laboratory."
AMERICAN = b"The color of the neighboring fiber was analyzed in the laboratory."


class _Queue:
    def __init__(self) -> None:
        self.enqueued: list[uuid.UUID] = []

    async def __call__(self, source_id: uuid.UUID) -> None:
        self.enqueued.append(source_id)


async def _pair(session: AsyncSession) -> tuple[InMemoryBlobStore, uuid.UUID, uuid.UUID]:
    """An original and its text duplicate, both DONE. Returns (store, original_id, dup_id)."""
    store = InMemoryBlobStore()
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    session.add(learner)
    await session.flush()
    ids = []
    for data, name in ((BRITISH, "uk.txt"), (AMERICAN, "us.txt")):
        source = await ingestion.create_source(
            session,
            store,
            learner_id=learner.id,
            kind=SourceKind.FILE,
            origin=name,
            content_type="text/plain",
            data=data,
        )
        await ingestion.ingest_source(session, store, fake_llm_client(), source.id)
        ids.append(source.id)
    return store, ids[0], ids[1]


async def _get(session: AsyncSession, source_id: uuid.UUID) -> Source:
    source = await session.get(Source, source_id, populate_existing=True)
    assert source is not None
    return source


async def _current_chunks(session: AsyncSession, source_id: uuid.UUID) -> int:
    return (
        await session.scalar(
            select(func.count())
            .select_from(Chunk)
            .where(Chunk.source_id == source_id, Chunk.superseded_at.is_(None))
        )
    ) or 0


async def test_a_healthy_duplicate_is_not_stranded(db_session: AsyncSession) -> None:
    _, original, dup = await _pair(db_session)
    assert (await _get(db_session, dup)).duplicate_of_id == original
    assert dup not in await ingestion.stranded_duplicates(db_session)


async def test_a_duplicate_whose_original_was_deleted_is_stranded(db_session: AsyncSession) -> None:
    _, original, dup = await _pair(db_session)
    await db_session.delete(await _get(db_session, original))
    await db_session.commit()

    assert dup in await ingestion.stranded_duplicates(db_session)


async def test_a_duplicate_whose_original_moved_scope_is_stranded(
    db_session: AsyncSession,
) -> None:
    from app.models.knowledge import Subject

    _, original, dup = await _pair(db_session)
    moved = await _get(db_session, original)
    subject = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name="S", owner_learner_id=moved.learner_id)
    db_session.add(subject)
    await db_session.flush()
    moved.subject_id = subject.id
    await db_session.commit()

    assert dup in await ingestion.stranded_duplicates(db_session)


async def test_a_duplicate_whose_original_failed_is_stranded(db_session: AsyncSession) -> None:
    _, original, dup = await _pair(db_session)
    (await _get(db_session, original)).status = SourceStatus.FAILED
    await db_session.commit()

    assert dup in await ingestion.stranded_duplicates(db_session)


async def test_release_puts_it_back_in_the_queue_and_it_reingests_from_its_own_file(
    db_session: AsyncSession,
) -> None:
    store, original, dup = await _pair(db_session)
    await db_session.delete(await _get(db_session, original))
    await db_session.commit()

    released = await ingestion.release_duplicates(db_session, [dup])
    await db_session.commit()
    released_again = await ingestion.release_duplicates(db_session, [dup])

    assert released == [dup]
    assert released_again == [], "no longer DONE, so a second release does nothing"
    source = await _get(db_session, dup)
    assert source.status == SourceStatus.PENDING and source.duplicate_of_id is None
    await ingestion.ingest_source(db_session, store, fake_llm_client(), dup)
    assert await _current_chunks(db_session, dup) >= 1


async def test_the_reconcile_sweep_recovers_stranded_duplicates(db_session: AsyncSession) -> None:
    _, original, dup = await _pair(db_session)
    await db_session.delete(await _get(db_session, original))
    await db_session.commit()
    queue = _Queue()

    report = await ingestion.reconcile_stranded(db_session, queue, settings=Settings())

    assert report.recovered == 1
    assert dup in queue.enqueued


async def test_a_legacy_web_source_is_never_released(db_session: AsyncSession) -> None:
    """Web ingestion is off, so a released URL source could only fail; it is left as it is."""
    _, original, dup = await _pair(db_session)
    source = await _get(db_session, dup)
    source.kind = SourceKind.URL
    await db_session.delete(await _get(db_session, original))
    await db_session.commit()

    assert dup not in await ingestion.stranded_duplicates(db_session)
    assert await ingestion.release_duplicates(db_session, [dup]) == []
