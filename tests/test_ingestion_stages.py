"""Ingestion in committed stages, resumed where it stopped (S37)."""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.llm import ModelRole
from app.llm.meter import ProviderUnavailable
from app.llm.providers.fake import FakeProvider
from app.llm.registry import LLMClient, ModelSpec, fake_llm_client
from app.models.learner import Learner
from app.models.source import Chunk, SourceKind, SourceStatus, StagedChunk
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
