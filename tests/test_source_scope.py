"""Source scope: parentage, reassignment, and what happens to derived tags (S55).

Retrieval filters on a source's subject *and* its topic. Nothing checked those agreed, so a
source could sit in a topic belonging to a different subject — reachable through neither
filter, while still being extracted, embedded, tagged and paid for. And moving a source
between subjects left its chunk KC tags naming concepts from the graph it just left.
"""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import DEV_LEARNER_HANDLE
from app.llm.registry import fake_llm_client
from app.models.knowledge import KC, Subject, Topic
from app.models.learner import Learner
from app.models.source import Chunk, ChunkKC, SourceKind
from app.rag import pipeline
from app.services import ingestion
from app.services import knowledge as svc
from app.storage import InMemoryBlobStore

API = "/api/v1"


async def _graph(session: AsyncSession, slug_prefix: str) -> tuple[Subject, Topic, KC]:
    subject = Subject(slug=f"{slug_prefix}-{uuid.uuid4().hex[:6]}", name="S")
    session.add(subject)
    await session.flush()
    topic = Topic(subject_id=subject.id, slug="t", name="T")
    session.add(topic)
    await session.flush()
    kc = KC(topic_id=topic.id, slug="k", name="K")
    session.add(kc)
    await session.flush()
    return subject, topic, kc


async def _learner(session: AsyncSession, *, dev: bool = False) -> Learner:
    handle = DEV_LEARNER_HANDLE if dev else f"l-{uuid.uuid4().hex[:8]}"
    existing = await session.scalar(select(Learner).where(Learner.handle == handle))
    if existing is not None:
        return existing
    learner = Learner(handle=handle)
    session.add(learner)
    await session.flush()
    return learner


# --- parentage ---------------------------------------------------------------------------


async def test_a_topic_from_another_subject_is_refused(db_session: AsyncSession) -> None:
    _subject_a, _t_a, _kc_a = await _graph(db_session, "a")
    subject_b, _t_b, _kc_b = await _graph(db_session, "b")
    other_topic = (await _graph(db_session, "c"))[1]

    with pytest.raises(svc.ScopeConflict):
        await svc.resolve_source_scope(db_session, subject_id=subject_b.id, topic_id=other_topic.id)


async def test_a_topic_implies_its_subject(db_session: AsyncSession) -> None:
    """Not an error — a topic belongs to exactly one subject, so it can be filled in."""
    subject, topic, _kc = await _graph(db_session, "d")

    resolved = await svc.resolve_source_scope(db_session, subject_id=None, topic_id=topic.id)

    assert resolved == (subject.id, topic.id)


async def test_a_missing_topic_is_refused(db_session: AsyncSession) -> None:
    with pytest.raises(svc.ScopeConflict):
        await svc.resolve_source_scope(db_session, subject_id=None, topic_id=uuid.uuid4())


async def test_no_scope_at_all_is_fine(db_session: AsyncSession) -> None:
    assert await svc.resolve_source_scope(db_session, subject_id=None, topic_id=None) == (
        None,
        None,
    )


async def test_uploading_into_a_mismatched_topic_is_422(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    subject_b, _t_b, _kc_b = await _graph(db_session, "e")
    foreign_topic = (await _graph(db_session, "f"))[1]
    await db_session.commit()

    from app.api.deps import get_blob_store
    from app.main import app

    app.dependency_overrides[get_blob_store] = lambda: InMemoryBlobStore()
    try:
        r = await api_client.post(
            f"{API}/sources/upload",
            files={"file": ("n.txt", b"cells respire", "text/plain")},
            data={"subject_id": str(subject_b.id), "topic_id": str(foreign_topic.id)},
        )
    finally:
        app.dependency_overrides.pop(get_blob_store, None)

    assert r.status_code == 422
    assert "belongs to subject" in r.text


# --- reassignment ------------------------------------------------------------------------


async def _tagged_source(session: AsyncSession, learner: Learner, subject: Subject, kc: KC):
    store = InMemoryBlobStore()
    source = await ingestion.create_source(
        session,
        store,
        learner_id=learner.id,
        kind=SourceKind.FILE,
        origin="n.txt",
        content_type="text/plain",
        data=b"Cells respire to release energy from glucose.",
        subject_id=subject.id,
    )
    await ingestion.ingest_source(
        session, store, fake_llm_client('{"tags": [{"kc": 1, "confidence": 0.9}]}'), source.id
    )
    return source


async def _tag_count(session: AsyncSession, source_id: uuid.UUID) -> int:
    return (
        await session.scalar(
            select(func.count())
            .select_from(ChunkKC)
            .join(Chunk, Chunk.id == ChunkKC.chunk_id)
            .where(Chunk.source_id == source_id)
        )
        or 0
    )


async def test_reassignment_drops_tags_naming_the_old_graph(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    subject, _topic, kc = await _graph(db_session, "g")
    source = await _tagged_source(db_session, learner, subject, kc)
    assert await _tag_count(db_session, source.id) > 0

    result = await svc.create_subject_with_graph(
        db_session,
        subject_name="Somewhere Else",
        subject_description=None,
        topics_data=[{"name": "T", "kcs": [{"name": "K"}]}],
        source_ids=[source.id],
        learner_id=learner.id,
    )

    assert result.reassigned_source_ids == [source.id]
    assert await _tag_count(db_session, source.id) == 0  # worse than absent if left behind


async def test_reassignment_clears_a_topic_that_cannot_still_apply(
    db_session: AsyncSession,
) -> None:
    learner = await _learner(db_session)
    subject, topic, _kc = await _graph(db_session, "h")
    store = InMemoryBlobStore()
    source = await ingestion.create_source(
        session=db_session,
        blobstore=store,
        learner_id=learner.id,
        kind=SourceKind.FILE,
        origin="n.txt",
        content_type="text/plain",
        data=b"x",
        topic_id=topic.id,
    )
    assert source.topic_id == topic.id

    await svc.create_subject_with_graph(
        db_session,
        subject_name="Elsewhere",
        subject_description=None,
        topics_data=[{"name": "T", "kcs": [{"name": "K"}]}],
        source_ids=[source.id],
        learner_id=learner.id,
    )

    await db_session.refresh(source)
    assert source.subject_id != subject.id
    assert source.topic_id is None  # the old topic is in a subject this source has left


async def test_a_source_that_did_not_move_keeps_its_tags(db_session: AsyncSession) -> None:
    """Reassigning to the subject it is already in derives nothing stale."""
    learner = await _learner(db_session)
    subject, _topic, kc = await _graph(db_session, "i")
    source = await _tagged_source(db_session, learner, subject, kc)
    before = await _tag_count(db_session, source.id)

    result = await svc.create_subject_with_graph(
        db_session,
        subject_name="Fresh",
        subject_description=None,
        topics_data=[{"name": "T", "kcs": [{"name": "K"}]}],
        source_ids=[],
        learner_id=learner.id,
    )

    assert result.reassigned_source_ids == []
    assert await _tag_count(db_session, source.id) == before


async def test_retagging_rebuilds_against_the_new_graph(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    subject, _topic, kc = await _graph(db_session, "j")
    source = await _tagged_source(db_session, learner, subject, kc)
    new_subject, _t, _k = await _graph(db_session, "k")
    source.subject_id = new_subject.id
    await db_session.commit()

    tagged = await pipeline.retag_source(
        db_session, fake_llm_client('{"tags": [{"kc": 1, "confidence": 0.9}]}'), source
    )

    assert tagged > 0
    kc_ids = (
        await db_session.scalars(
            select(ChunkKC.kc_id)
            .join(Chunk, Chunk.id == ChunkKC.chunk_id)
            .where(Chunk.source_id == source.id)
        )
    ).all()
    assert kc_ids  # rebuilt
    assert kc.id not in set(kc_ids)  # and not against the graph it left


# --- coverage: the tags finally have a reader ---------------------------------------------


async def test_coverage_counts_the_learners_own_chunks_per_kc(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    subject, _topic, kc = await _graph(db_session, "l")
    await _tagged_source(db_session, learner, subject, kc)

    coverage = await svc.kc_coverage(db_session, learner_id=learner.id, subject_id=subject.id)

    assert len(coverage) == 1
    assert coverage[0].kc_id == kc.id
    assert coverage[0].chunk_count > 0


async def test_coverage_keeps_the_kcs_nothing_covers(db_session: AsyncSession) -> None:
    """A gap in the library is the more actionable half of the answer."""
    learner = await _learner(db_session)
    subject, topic, kc = await _graph(db_session, "m")
    db_session.add(KC(topic_id=topic.id, slug="zz-uncovered", name="Uncovered"))
    await db_session.flush()
    await _tagged_source(db_session, learner, subject, kc)

    coverage = await svc.kc_coverage(db_session, learner_id=learner.id, subject_id=subject.id)

    by_slug = {c.slug: c.chunk_count for c in coverage}
    assert by_slug["k"] > 0
    assert by_slug["zz-uncovered"] == 0


async def test_coverage_does_not_count_another_learners_library(
    db_session: AsyncSession,
) -> None:
    owner = await _learner(db_session)
    subject, _topic, kc = await _graph(db_session, "n")
    await _tagged_source(db_session, owner, subject, kc)
    stranger = await _learner(db_session)

    coverage = await svc.kc_coverage(db_session, learner_id=stranger.id, subject_id=subject.id)

    assert [c.chunk_count for c in coverage] == [0]


async def test_coverage_endpoint_404s_for_a_missing_subject(api_client: AsyncClient) -> None:
    r = await api_client.get(f"{API}/subjects/{uuid.uuid4()}/coverage")
    assert r.status_code == 404
