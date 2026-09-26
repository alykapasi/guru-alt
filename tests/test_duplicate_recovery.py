"""A text duplicate never silently grounds nothing (S77)."""

import uuid
from collections.abc import Iterator

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_ingestion_enqueuer
from app.core.config import Settings
from app.llm.registry import fake_llm_client
from app.main import app
from app.models.learner import Learner
from app.models.publication import CurriculumProposal
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


async def _pair(
    session: AsyncSession, learner: Learner | None = None
) -> tuple[InMemoryBlobStore, uuid.UUID, uuid.UUID]:
    """An original and its text duplicate, both DONE. Returns (store, original_id, dup_id)."""
    store = InMemoryBlobStore()
    if learner is None:
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


async def test_an_original_still_answering_keeps_its_duplicate_whatever_its_status(
    db_session: AsyncSession,
) -> None:
    """Retrieval reads current chunks whatever the source's status, so an original being
    re-processed, or one whose re-processing failed, still answers for its duplicate. Releasing
    the duplicate then would put a second copy in every grounding window."""
    _, original, dup = await _pair(db_session)
    for status in (SourceStatus.PENDING, SourceStatus.PROCESSING, SourceStatus.FAILED):
        (await _get(db_session, original)).status = status
        await db_session.commit()

        assert dup not in await ingestion.stranded_duplicates(db_session), status
        assert await ingestion.release_duplicates(db_session, [dup]) == [], status


async def test_a_duplicate_whose_original_was_emptied_is_stranded(
    db_session: AsyncSession,
) -> None:
    _, original, dup = await _pair(db_session)
    for chunk in (await db_session.scalars(select(Chunk).where(Chunk.source_id == original))).all():
        await db_session.delete(chunk)
    await db_session.commit()

    assert dup in await ingestion.stranded_duplicates(db_session)


async def test_reprocessing_an_original_leaves_one_copy_answering(
    db_session: AsyncSession,
) -> None:
    """The swap the status check caused: reset the original, sweep, then let both finish."""
    store, original, dup = await _pair(db_session)
    await ingestion.reset_for_reingest(db_session, original)
    await db_session.commit()

    await ingestion.reconcile_stranded(db_session, _Queue(), settings=Settings())
    for source_id in (dup, original):
        if (await _get(db_session, source_id)).status != SourceStatus.DONE:
            await ingestion.ingest_source(db_session, store, fake_llm_client(), source_id)

    answering = [s for s in (original, dup) if await _current_chunks(db_session, s)]
    assert answering == [original]
    assert (await _get(db_session, dup)).duplicate_of_id == original


async def test_a_source_that_becomes_a_duplicate_stops_answering_itself(
    db_session: AsyncSession,
) -> None:
    """Re-ingested into a scope where its twin already answers, a source defers to it — and
    its own earlier chunks go, or both copies would be retrieved."""
    from app.models.knowledge import Subject

    store = InMemoryBlobStore()
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    db_session.add(learner)
    await db_session.flush()
    subject = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name="S", owner_learner_id=learner.id)
    db_session.add(subject)
    await db_session.flush()
    ids = []
    for data, subject_id in ((BRITISH, subject.id), (AMERICAN, None)):
        source = await ingestion.create_source(
            db_session,
            store,
            learner_id=learner.id,
            kind=SourceKind.FILE,
            origin="x.txt",
            content_type="text/plain",
            data=data,
            subject_id=subject_id,
        )
        await ingestion.ingest_source(db_session, store, fake_llm_client(), source.id)
        ids.append(source.id)
    kept, moved = ids
    assert await _current_chunks(db_session, moved), "different scopes, so both answered"

    (await _get(db_session, moved)).subject_id = subject.id
    await ingestion.reset_for_reingest(db_session, moved)
    await ingestion.ingest_source(db_session, store, fake_llm_client(), moved)

    assert (await _get(db_session, moved)).duplicate_of_id == kept
    assert await _current_chunks(db_session, moved) == 0


async def test_a_source_that_chunks_itself_is_no_longer_marked_a_duplicate(
    db_session: AsyncSession,
) -> None:
    from app.models.knowledge import Subject

    store, original, dup = await _pair(db_session)
    moved = await _get(db_session, original)
    subject = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name="S", owner_learner_id=moved.learner_id)
    db_session.add(subject)
    await db_session.flush()
    moved.subject_id = subject.id
    await ingestion.reset_for_reingest(db_session, dup)
    await ingestion.ingest_source(db_session, store, fake_llm_client(), dup)

    source = await _get(db_session, dup)
    assert await _current_chunks(db_session, dup) >= 1
    assert source.duplicate_of_id is None


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


async def test_moving_an_original_into_a_new_subject_releases_its_duplicate(
    db_session: AsyncSession,
) -> None:
    from app.services import knowledge

    _, original, dup = await _pair(db_session)
    owner = (await _get(db_session, original)).learner_id

    result = await knowledge.create_subject_with_graph(
        db_session,
        subject_name=f"Colour {uuid.uuid4().hex[:6]}",
        subject_description=None,
        topics_data=[],
        source_ids=[original],
        learner_id=owner,
        private_source_derived=True,
    )

    assert result.released_source_ids == [dup]
    assert (await _get(db_session, dup)).status == SourceStatus.PENDING


async def test_moving_an_original_and_its_duplicate_together_releases_nothing(
    db_session: AsyncSession,
) -> None:
    """Still the same scope, so the duplicate is still answered for; re-extracting it would pay
    for an OCR pass only for the twin check to suppress it again."""
    from app.services import knowledge

    _, original, dup = await _pair(db_session)
    owner = (await _get(db_session, original)).learner_id

    result = await knowledge.create_subject_with_graph(
        db_session,
        subject_name=f"Both {uuid.uuid4().hex[:6]}",
        subject_description=None,
        topics_data=[],
        source_ids=[original, dup],
        learner_id=owner,
        private_source_derived=True,
    )

    assert result.released_source_ids == []
    assert (await _get(db_session, dup)).status == SourceStatus.DONE


async def test_moving_a_duplicate_away_from_its_original_releases_it(
    db_session: AsyncSession,
) -> None:
    from app.services import knowledge

    _, original, dup = await _pair(db_session)
    owner = (await _get(db_session, original)).learner_id

    result = await knowledge.create_subject_with_graph(
        db_session,
        subject_name=f"Color {uuid.uuid4().hex[:6]}",
        subject_description=None,
        topics_data=[],
        source_ids=[dup],
        learner_id=owner,
        private_source_derived=True,
    )

    assert result.released_source_ids == [dup]


@pytest.fixture
def queue() -> Iterator[_Queue]:
    recorded = _Queue()
    app.dependency_overrides[get_ingestion_enqueuer] = lambda: recorded
    yield recorded
    app.dependency_overrides.pop(get_ingestion_enqueuer, None)


async def test_the_curriculum_commit_route_dispatches_what_it_released(
    api_client: AsyncClient, db_session: AsyncSession, api_learner: Learner, queue: _Queue
) -> None:
    _, original, dup = await _pair(db_session, api_learner)
    proposal = CurriculumProposal(learner_id=api_learner.id, grounded_in_sources=False)
    db_session.add(proposal)
    await db_session.commit()

    r = await api_client.post(
        "/api/v1/subjects/commit",
        json={
            "proposal_id": str(proposal.id),
            "subject_name": f"Colour {uuid.uuid4().hex[:6]}",
            "subject_description": None,
            "topics": [{"name": "Fibres", "description": "d", "kcs": []}],
            "source_ids": [str(original)],
        },
    )

    assert r.status_code == 201, r.text
    assert queue.enqueued == [dup]
