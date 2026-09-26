"""The operator reindex (S50): staleness derived from the data, so a second run is a resume."""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.llm.embedding_space import current_space
from app.llm.registry import fake_llm_client
from app.models.chat import LLMCall
from app.models.knowledge import Subject, Topic
from app.models.learner import Learner
from app.models.source import Chunk, Source, SourceKind, SourceStatus
from app.rag import pipeline
from app.services import ingestion, reindex
from app.storage import InMemoryBlobStore

OLD_SPACE = "ollama:old-embedder:768"


class _Queue:
    def __init__(self) -> None:
        self.enqueued: list[uuid.UUID] = []

    async def __call__(self, source_id: uuid.UUID) -> None:
        self.enqueued.append(source_id)


async def _learner(session: AsyncSession) -> Learner:
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    session.add(learner)
    await session.flush()
    return learner


async def _done_source(
    session: AsyncSession, learner: Learner, text: bytes = b"Mitochondria make ATP."
) -> Source:
    store = InMemoryBlobStore()
    source = await ingestion.create_source(
        session,
        store,
        learner_id=learner.id,
        kind=SourceKind.FILE,
        origin=f"{uuid.uuid4().hex[:6]}.txt",
        content_type="text/plain",
        data=text,
    )
    await ingestion.ingest_source(session, store, fake_llm_client(), source.id)
    return source


async def _chunks(session: AsyncSession, source: Source) -> list[Chunk]:
    # populate_existing rather than expire_all: expiring would also expire the test's own
    # objects, and reading an expired attribute is sync IO an async session cannot do.
    stmt = (
        select(Chunk).where(Chunk.source_id == source.id).execution_options(populate_existing=True)
    )
    return list((await session.scalars(stmt)).all())


def _space() -> str:
    return current_space(fake_llm_client(), dim=get_settings().embed_dim)


async def _age(
    session: AsyncSession, source: Source, *, space: str | None = None, version: int | None = None
) -> None:
    for chunk in await _chunks(session, source):
        if space is not None:
            chunk.embedding_space = space
        if version is not None:
            chunk.pipeline_version = version
    await session.commit()


async def test_a_dry_run_lists_and_changes_nothing(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    source = await _done_source(db_session, learner)
    await _age(db_session, source, space=OLD_SPACE)

    found = await reindex.plan(db_session, space=_space(), learner_id=learner.id)

    assert [s.source_id for s in found.reembed] == [source.id]
    assert {c.embedding_space for c in await _chunks(db_session, source)} == {OLD_SPACE}
    assert "re-embed" in reindex.render(found, None)


async def test_apply_re_embeds_in_place_and_a_second_run_finds_nothing(
    db_session: AsyncSession,
) -> None:
    learner = await _learner(db_session)
    source = await _done_source(db_session, learner)
    before = {c.id for c in await _chunks(db_session, source)}
    await _age(db_session, source, space=OLD_SPACE)
    calls_before = await db_session.scalar(select(func.count()).select_from(LLMCall))

    found = await reindex.plan(db_session, space=_space(), learner_id=learner.id)
    result = await reindex.apply(
        db_session,
        fake_llm_client(),
        found,
        space=_space(),
        reextract=False,
        limit=None,
        enqueue=_Queue(),
        settings=get_settings(),
    )

    after = await _chunks(db_session, source)
    assert result.reembedded == [source.id]
    assert {c.id for c in after} == before, "same chunks, so every citation still resolves"
    assert {c.embedding_space for c in after} == {_space()}
    calls_after = await db_session.scalar(select(func.count()).select_from(LLMCall))
    assert calls_before is not None and calls_after is not None
    assert calls_after > calls_before, "the embed was paid for, so it was recorded"
    again = await reindex.plan(db_session, space=_space(), learner_id=learner.id)
    assert again.reembed == [] and again.reextract == []


async def test_one_failure_does_not_stop_the_run(db_session: AsyncSession, monkeypatch) -> None:
    learner = await _learner(db_session)
    bad = await _done_source(db_session, learner, b"boom boom boom")
    good = await _done_source(db_session, learner, b"Chloroplasts hold chlorophyll.")
    await _age(db_session, bad, space=OLD_SPACE)
    await _age(db_session, good, space=OLD_SPACE)
    # Read before the run: a failed source rolls the session back, expiring these objects.
    good_id, bad_id = good.id, bad.id
    llm = fake_llm_client()
    original = llm.embed

    async def flaky(role, texts, **kwargs):
        if any("boom" in t for t in texts):
            raise RuntimeError("provider down")
        return await original(role, texts, **kwargs)

    monkeypatch.setattr(llm, "embed", flaky)

    found = await reindex.plan(db_session, space=_space(), learner_id=learner.id)
    result = await reindex.apply(
        db_session,
        llm,
        found,
        space=_space(),
        reextract=False,
        limit=None,
        enqueue=_Queue(),
        settings=get_settings(),
    )

    assert result.reembedded == [good_id]
    assert set(result.failed) == {bad_id}


async def test_limit_caps_the_sources_touched(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    for text in (b"one fact", b"two facts", b"three facts"):
        await _age(db_session, await _done_source(db_session, learner, text), space=OLD_SPACE)

    found = await reindex.plan(db_session, space=_space(), learner_id=learner.id)
    result = await reindex.apply(
        db_session,
        fake_llm_client(),
        found,
        space=_space(),
        reextract=False,
        limit=2,
        enqueue=_Queue(),
        settings=get_settings(),
    )

    assert len(result.reembedded) == 2


async def test_learner_restricts_the_plan(db_session: AsyncSession) -> None:
    mine, theirs = await _learner(db_session), await _learner(db_session)
    await _age(db_session, await _done_source(db_session, mine), space=OLD_SPACE)
    await _age(db_session, await _done_source(db_session, theirs), space=OLD_SPACE)

    found = await reindex.plan(db_session, space=_space(), learner_id=mine.id)

    assert {s.learner_id for s in found.reembed} == {mine.id}


async def test_reextract_resets_and_enqueues_only_with_the_flag(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    source = await _done_source(db_session, learner)
    await _age(db_session, source, version=pipeline.PIPELINE_VERSION - 1)
    queue = _Queue()

    found = await reindex.plan(db_session, space=_space(), learner_id=learner.id)
    without = await reindex.apply(
        db_session,
        fake_llm_client(),
        found,
        space=_space(),
        reextract=False,
        limit=None,
        enqueue=queue,
        settings=get_settings(),
    )
    assert [s.source_id for s in found.reextract] == [source.id]
    assert without.reextracted == [] and queue.enqueued == []

    with_flag = await reindex.apply(
        db_session,
        fake_llm_client(),
        found,
        space=_space(),
        reextract=True,
        limit=None,
        enqueue=queue,
        settings=get_settings(),
    )
    assert with_flag.reextracted == [source.id]
    assert queue.enqueued == [source.id]
    reset = await db_session.get(Source, source.id, populate_existing=True)
    assert reset is not None and reset.status == SourceStatus.PENDING


async def test_reextract_skips_a_source_mid_ingest(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    source = await _done_source(db_session, learner)
    await _age(db_session, source, version=pipeline.PIPELINE_VERSION - 1)
    found = await reindex.plan(db_session, space=_space(), learner_id=learner.id)
    await ingestion.reset_for_reingest(db_session, source.id)
    await ingestion.claim_source(db_session, source.id, settings=get_settings())

    result = await reindex.apply(
        db_session,
        fake_llm_client(),
        found,
        space=_space(),
        reextract=True,
        limit=None,
        enqueue=_Queue(),
        settings=get_settings(),
    )

    assert result.busy == [source.id]


async def test_a_source_with_only_history_left_is_not_stale(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    source = await _done_source(db_session, learner)
    for chunk in await _chunks(db_session, source):
        chunk.embedding_space = OLD_SPACE
        chunk.superseded_at = func.now()
        chunk.embedding = None
    await db_session.commit()

    found = await reindex.plan(db_session, space=_space(), learner_id=learner.id)

    assert found.reembed == [] and found.reextract == []


async def test_scope_repair_takes_the_topics_subject_when_there_is_none(
    db_session: AsyncSession,
) -> None:
    learner = await _learner(db_session)
    subject = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name="Bio", owner_learner_id=learner.id)
    db_session.add(subject)
    await db_session.flush()
    topic = Topic(subject_id=subject.id, slug="t", name="Cells")
    db_session.add(topic)
    await db_session.flush()
    source = await _done_source(db_session, learner)
    source.topic_id = topic.id
    source.subject_id = None
    await db_session.commit()

    found = await reindex.plan(db_session, space=_space(), learner_id=learner.id)
    await reindex.apply(
        db_session,
        fake_llm_client(),
        found,
        space=_space(),
        reextract=False,
        limit=None,
        enqueue=_Queue(),
        settings=get_settings(),
    )

    repaired = await db_session.get(Source, source.id, populate_existing=True)
    assert repaired is not None
    assert (repaired.subject_id, repaired.topic_id) == (subject.id, topic.id)


async def test_scope_repair_clears_a_topic_from_another_subject(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    s1 = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name="Bio", owner_learner_id=learner.id)
    s2 = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name="Chem", owner_learner_id=learner.id)
    db_session.add_all([s1, s2])
    await db_session.flush()
    topic = Topic(subject_id=s2.id, slug="t", name="Bonds")
    db_session.add(topic)
    await db_session.flush()
    source = await _done_source(db_session, learner)
    source.subject_id, source.topic_id = s1.id, topic.id
    await db_session.commit()

    found = await reindex.plan(db_session, space=_space(), learner_id=learner.id)
    assert [r.source_id for r in found.scope] == [source.id]
    await reindex.apply(
        db_session,
        fake_llm_client(),
        found,
        space=_space(),
        reextract=False,
        limit=None,
        enqueue=_Queue(),
        settings=get_settings(),
    )

    repaired = await db_session.get(Source, source.id, populate_existing=True)
    assert repaired is not None
    assert (repaired.subject_id, repaired.topic_id) == (s1.id, None)


async def test_a_legacy_url_source_does_not_stop_a_reextract_run(db_session: AsyncSession) -> None:
    """v0 refuses URL ingestion; such a source must not crash every future --reextract run."""
    learner = await _learner(db_session)
    url_source = await _done_source(db_session, learner, b"Old web page text.")
    url_source.kind = SourceKind.URL
    await _age(db_session, url_source, version=pipeline.PIPELINE_VERSION - 1)
    file_source = await _done_source(db_session, learner, b"A file of facts.")
    await _age(db_session, file_source, version=pipeline.PIPELINE_VERSION - 1)
    url_id, file_id = url_source.id, file_source.id

    found = await reindex.plan(db_session, space=_space(), learner_id=learner.id)
    result = await reindex.apply(
        db_session,
        fake_llm_client(),
        found,
        space=_space(),
        reextract=True,
        limit=None,
        enqueue=_Queue(),
        settings=get_settings(),
    )

    assert url_id not in {s.source_id for s in found.reextract}
    assert result.reextracted == [file_id]


async def test_reextracting_a_source_with_an_empty_twin_gives_it_fresh_chunks(
    db_session: AsyncSession,
) -> None:
    """A same-text twin with no chunks cannot stand in for this source; if it did, every
    reindex would re-extract (and pay for) the source again and it would never converge."""
    learner = await _learner(db_session)
    store = InMemoryBlobStore()
    original = await ingestion.create_source(
        db_session,
        store,
        learner_id=learner.id,
        kind=SourceKind.FILE,
        origin="a.txt",
        content_type="text/plain",
        data=b"Enzymes lower activation energy.",
    )
    await ingestion.ingest_source(db_session, store, fake_llm_client(), original.id)
    twin = await ingestion.create_source(
        db_session,
        store,
        learner_id=learner.id,
        kind=SourceKind.FILE,
        origin="a.md",
        content_type="text/markdown",
        data=b"Enzymes lower activation energy.\n",
    )
    await ingestion.ingest_source(db_session, store, fake_llm_client(), twin.id)
    original_id = original.id
    assert await _chunks(db_session, twin) == [], "the twin was suppressed as a duplicate"
    await _age(db_session, original, version=pipeline.PIPELINE_VERSION - 1)

    await ingestion.reset_for_reingest(db_session, original_id)
    await ingestion.ingest_source(db_session, store, fake_llm_client(), original_id)

    again = await reindex.plan(db_session, space=_space(), learner_id=learner.id)
    assert original_id not in {s.source_id for s in again.reextract}
