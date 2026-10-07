"""Ingestion in committed stages, resumed where it stopped (S37)."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.learning.kc_tagging import KCCandidate, tag_chunk
from app.llm import ModelRole
from app.llm.meter import ProviderUnavailable
from app.llm.providers.fake import FakeProvider
from app.llm.registry import LLMClient, ModelSpec, fake_llm_client
from app.models.knowledge import KC, Subject, Topic
from app.models.learner import Learner
from app.models.source import Chunk, ChunkKC, Source, SourceKind, SourceStatus, StagedChunk
from app.rag import pipeline
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


SETTINGS = Settings(embed_batch_size=1, embed_concurrency=1)


class _FlakyEmbed(FakeProvider):
    """Refuses the Nth embed call once, and remembers every text it embedded."""

    def __init__(self, fail_on: int | None) -> None:
        super().__init__()
        self.fail_on = fail_on
        self.calls = 0
        self.embedded: list[str] = []

    async def embed(self, *, model, texts):
        self.calls += 1
        if self.calls == self.fail_on:
            raise ProviderUnavailable("down")
        self.embedded.extend(texts)
        return await super().embed(model=model, texts=texts)


class _CountingStore(InMemoryBlobStore):
    def __init__(self) -> None:
        super().__init__()
        self.downloads = 0

    async def download(self, key, dest):
        self.downloads += 1
        return await super().download(key, dest)


def _llm(provider: FakeProvider) -> LLMClient:
    return LLMClient({"fake": provider}, {r: ModelSpec("fake", "fake-1") for r in ModelRole})


async def _live_chunks(session: AsyncSession, source_id: uuid.UUID) -> list[Chunk]:
    rows = await session.scalars(
        select(Chunk)
        .where(Chunk.source_id == source_id, Chunk.superseded_at.is_(None))
        .execution_options(populate_existing=True)
    )
    return list(rows.all())


async def test_a_retry_resumes_without_extracting_or_embedding_again(
    db_session: AsyncSession,
) -> None:
    store = _CountingStore()
    source = await _source(db_session, store)
    provider = _FlakyEmbed(fail_on=2)

    first = await ingestion.ingest_source(
        db_session, store, _llm(provider), source.id, settings=SETTINGS
    )
    assert first is not None
    assert (first.status, first.stage) == (SourceStatus.PENDING, "embed")
    assert await _staged(db_session, source.id) == 1  # the batch before the refusal is kept
    assert await _live_chunks(db_session, source.id) == []  # nothing half-published

    embedded_before = len(provider.embedded)
    second = await ingestion.ingest_source(
        db_session, store, _llm(provider), source.id, settings=SETTINGS
    )

    assert second is not None and second.status == SourceStatus.DONE
    assert store.downloads == 1  # extracted once
    chunks = await _live_chunks(db_session, source.id)
    assert len(provider.embedded) - embedded_before == len(chunks) - 1  # only what was missing
    assert await _staged(db_session, source.id) == 0
    assert not await store.exists(pipeline.artifact_key(source.id))


async def test_a_reprocessed_source_keeps_its_old_chunks_until_publish(
    db_session: AsyncSession,
) -> None:
    store = InMemoryBlobStore()
    source = await _source(db_session, store)
    await ingestion.ingest_source(db_session, store, fake_llm_client(), source.id)
    old = {c.id for c in await _live_chunks(db_session, source.id)}
    assert old

    await ingestion.reset_for_reingest(db_session, source.id)
    await ingestion.ingest_source(
        db_session, store, _llm(_FlakyEmbed(fail_on=1)), source.id, settings=SETTINGS
    )

    assert {c.id for c in await _live_chunks(db_session, source.id)} == old


async def test_reprocessing_starts_over_and_clears_what_was_staged(
    db_session: AsyncSession,
) -> None:
    store = InMemoryBlobStore()
    source = await _source(db_session, store)
    await ingestion.ingest_source(
        db_session, store, _llm(_FlakyEmbed(fail_on=2)), source.id, settings=SETTINGS
    )
    assert await _staged(db_session, source.id) == 1

    reset = await ingestion.reset_for_reingest(db_session, source.id)

    assert reset is not None and reset.stage is None
    assert await _staged(db_session, source.id) == 0


async def test_a_finished_source_is_done_with_its_chunks_and_nothing_staged(
    db_session: AsyncSession,
) -> None:
    store = InMemoryBlobStore()
    source = await _source(db_session, store)

    done = await ingestion.ingest_source(db_session, store, fake_llm_client(), source.id)

    assert done is not None
    assert (done.status, done.attempts, done.lease_expires_at) == (SourceStatus.DONE, 0, None)
    assert done.meta["chunk_count"] == len(await _live_chunks(db_session, source.id)) > 1
    assert await _staged(db_session, source.id) == 0
    assert not await store.exists(pipeline.artifact_key(source.id))


TAGS = '{"tags": [{"kc": 1, "confidence": 0.9}]}'


async def _scoped_source(session: AsyncSession, store: InMemoryBlobStore):
    subject = Subject(slug=f"s-{uuid.uuid4().hex[:6]}", name="S")
    session.add(subject)
    await session.flush()
    topic = Topic(subject_id=subject.id, slug="t", name="T")
    session.add(topic)
    await session.flush()
    session.add(KC(topic_id=topic.id, slug="k", name="K"))
    await session.flush()
    return await _source(
        session, store, data=b"Cells respire to release energy.", subject_id=subject.id
    )


async def _tags(session: AsyncSession, source_id: uuid.UUID) -> int:
    return (
        await session.scalar(
            select(func.count())
            .select_from(ChunkKC)
            .join(Chunk, Chunk.id == ChunkKC.chunk_id)
            .where(Chunk.source_id == source_id)
        )
        or 0
    )


async def test_a_tagging_refusal_is_not_swallowed() -> None:
    llm = _llm(FakeProvider(refuse=ProviderUnavailable("busy", retry_after=5.0)))
    with pytest.raises(ProviderUnavailable):
        await tag_chunk(llm, "text", [KCCandidate(id=uuid.uuid4(), name="K", description=None)])


async def test_a_parse_failure_still_tags_nothing_quietly() -> None:
    tags, _usage = await tag_chunk(
        fake_llm_client("not json"),
        "text",
        [KCCandidate(id=uuid.uuid4(), name="K", description=None)],
    )
    assert tags == []


async def test_a_finished_ingest_is_tagged(db_session: AsyncSession) -> None:
    store = InMemoryBlobStore()
    source = await _scoped_source(db_session, store)

    done = await ingestion.ingest_source(db_session, store, fake_llm_client(TAGS), source.id)

    assert done is not None and (done.status, done.stage) == (SourceStatus.DONE, None)
    assert await _tags(db_session, source.id) > 0


async def test_a_refused_tag_leaves_the_source_done_and_searchable(
    db_session: AsyncSession,
) -> None:
    store = InMemoryBlobStore()
    source = await _scoped_source(db_session, store)
    refusing = _llm(
        FakeProvider(refuse=ProviderUnavailable("down"), refuse_calls=frozenset({"complete"}))
    )

    done = await ingestion.ingest_source(db_session, store, refusing, source.id)

    assert done is not None and (done.status, done.stage) == (SourceStatus.DONE, "tag")
    assert done.lease_expires_at is None
    assert await _live_chunks(db_session, source.id)
    assert await _tags(db_session, source.id) == 0


async def test_the_sweep_requeues_a_source_waiting_to_be_tagged(db_session: AsyncSession) -> None:
    store = InMemoryBlobStore()
    source = await _scoped_source(db_session, store)
    refusing = _llm(
        FakeProvider(refuse=ProviderUnavailable("down"), refuse_calls=frozenset({"complete"}))
    )
    await ingestion.ingest_source(db_session, store, refusing, source.id)
    source.updated_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1)
    await db_session.commit()
    queued: list[uuid.UUID] = []

    async def enqueue(source_id: uuid.UUID) -> None:
        queued.append(source_id)

    await ingestion.reconcile_stranded(db_session, enqueue, settings=Settings())

    assert source.id in queued


async def test_the_tag_retry_does_not_embed_again(db_session: AsyncSession) -> None:
    store = _CountingStore()
    source = await _scoped_source(db_session, store)
    refusing = _llm(
        FakeProvider(refuse=ProviderUnavailable("down"), refuse_calls=frozenset({"complete"}))
    )
    await ingestion.ingest_source(db_session, store, refusing, source.id)
    tagger = _FlakyEmbed(fail_on=None)
    tagger._reply = TAGS

    done = await ingestion.ingest_source(db_session, store, _llm(tagger), source.id)

    assert done is not None and (done.status, done.stage) == (SourceStatus.DONE, None)
    assert tagger.embedded == [] and store.downloads == 1
    assert await _tags(db_session, source.id) > 0


async def test_tagging_that_runs_out_of_tries_leaves_the_source_done(
    db_session: AsyncSession,
) -> None:
    store = InMemoryBlobStore()
    source = await _scoped_source(db_session, store)
    refusing = _llm(
        FakeProvider(refuse=ProviderUnavailable("down"), refuse_calls=frozenset({"complete"}))
    )
    settings = Settings(ingest_max_attempts=1)
    await ingestion.ingest_source(db_session, store, refusing, source.id, settings=settings)
    again = await ingestion.ingest_source(db_session, store, refusing, source.id, settings=settings)

    assert again is not None
    assert (again.status, again.stage, again.attempts) == (SourceStatus.DONE, "tag", 1)
    assert await ingestion.claim_source(db_session, source.id, settings=settings) is None


async def test_deleting_a_half_ingested_source_removes_what_it_saved(
    db_session: AsyncSession,
) -> None:
    from app.services import removal

    store = InMemoryBlobStore()
    source = await _source(db_session, store)
    await ingestion.ingest_source(
        db_session, store, _llm(_FlakyEmbed(fail_on=2)), source.id, settings=SETTINGS
    )
    assert await store.exists(pipeline.artifact_key(source.id))

    await removal.delete_source(db_session, store, source.learner_id, source.id, forget=False)

    assert not await store.exists(pipeline.artifact_key(source.id))
    assert await _staged(db_session, source.id) == 0


async def test_erasing_an_account_removes_a_half_ingested_extraction(
    db_session: AsyncSession,
) -> None:
    from app.services import retention

    store = InMemoryBlobStore()
    source = await _source(db_session, store)
    await ingestion.ingest_source(
        db_session, store, _llm(_FlakyEmbed(fail_on=2)), source.id, settings=SETTINGS
    )

    report = await retention.delete_learner(db_session, store, source.learner_id)

    assert not await store.exists(pipeline.artifact_key(source.id))
    assert report.blobs_failed == []


async def _tagging_in_progress(session: AsyncSession, store: InMemoryBlobStore):
    """Published and being tagged by a live job, its last write long enough ago to look idle."""
    source = await _scoped_source(session, store)
    refusing = _llm(
        FakeProvider(refuse=ProviderUnavailable("down"), refuse_calls=frozenset({"complete"}))
    )
    await ingestion.ingest_source(session, store, refusing, source.id)
    source.lease_expires_at = func.now() + timedelta(hours=1)
    source.updated_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1)
    await session.commit()
    return source


async def test_a_source_being_tagged_is_not_claimed_twice(db_session: AsyncSession) -> None:
    """A long tag stage outlives the sweep's grace; its lease, not its age, says it is busy."""
    store = InMemoryBlobStore()
    source = await _tagging_in_progress(db_session, store)
    queued: list[uuid.UUID] = []

    async def enqueue(source_id: uuid.UUID) -> None:
        queued.append(source_id)

    await ingestion.reconcile_stranded(db_session, enqueue, settings=Settings())

    assert source.id not in queued
    assert await ingestion.claim_source(db_session, source.id, settings=Settings()) is None
    assert await ingestion.reset_for_reingest(db_session, source.id) is None


async def _waiting(session: AsyncSession, store: InMemoryBlobStore, learner_id: uuid.UUID):
    return await ingestion.create_source(
        session,
        store,
        learner_id=learner_id,
        kind=SourceKind.FILE,
        origin=f"{uuid.uuid4().hex}.txt",
        content_type="text/plain",
        data=uuid.uuid4().hex.encode() + b" The cell releases energy.",
    )


async def _running(session: AsyncSession, store: InMemoryBlobStore, learner_id: uuid.UUID):
    source = await _waiting(session, store, learner_id)
    source.status = SourceStatus.PROCESSING
    source.lease_expires_at = func.now() + timedelta(hours=1)
    await session.commit()
    return source


async def _two_learners(session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    learners = [Learner(handle=f"n-{uuid.uuid4().hex[:8]}") for _ in range(2)]
    session.add_all(learners)
    await session.flush()
    return learners[0].id, learners[1].id


async def test_the_next_upload_goes_to_the_learner_running_least(
    db_session: AsyncSession,
) -> None:
    """Preferring the finishing learner let two heavy queues hold every slot indefinitely."""
    store = InMemoryBlobStore()
    busy, quiet = await _two_learners(db_session)
    await _running(db_session, store, busy)
    await _waiting(db_session, store, busy)
    theirs = await _waiting(db_session, store, quiet)  # newer, but its learner runs nothing
    queued: list[uuid.UUID] = []

    async def enqueue(source_id: uuid.UUID) -> None:
        queued.append(source_id)

    await ingestion._start_next(db_session, enqueue, Settings())

    assert queued == [theirs.id]


async def test_the_next_upload_skips_a_learner_at_their_cap(db_session: AsyncSession) -> None:
    store = InMemoryBlobStore()
    busy, _ = await _two_learners(db_session)
    for _ in range(2):
        await _running(db_session, store, busy)
    await _waiting(db_session, store, busy)
    queued: list[uuid.UUID] = []

    async def enqueue(source_id: uuid.UUID) -> None:
        queued.append(source_id)

    await ingestion._start_next(db_session, enqueue, Settings(ingest_max_jobs_per_learner=2))

    assert queued == []  # its only candidate could not get a slot; the sweep starts it later
