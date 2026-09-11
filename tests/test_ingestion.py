"""Ingestion pipeline + service: extract → chunk → embed → store, with status machine."""

import uuid
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.learning.kc_tagging import load_candidate_kcs
from app.llm import ChatMessage, ChatResponse, ModelRole, ToolDef
from app.llm.providers import FakeProvider
from app.llm.registry import LLMClient, ModelSpec, fake_llm_client
from app.llm.types import EmbedResult
from app.models.chat import LLMCall
from app.models.knowledge import KC, Subject, Topic
from app.models.learner import Learner
from app.models.source import Chunk, ChunkKC, Source, SourceKind, SourceStatus
from app.rag.adapters.base import ExtractedUnit
from app.rag.chunking import chunk_units, normalize
from app.rag.pipeline import embed_in_batches
from app.services import ingestion
from app.storage import InMemoryBlobStore


class _CountingEmbedProvider(FakeProvider):
    """A fake embed provider that records how many embed calls it received and their sizes."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0
        self.batch_sizes: list[int] = []

    async def embed(self, *, model: str, texts: Sequence[str]) -> EmbedResult:
        self.calls += 1
        self.batch_sizes.append(len(texts))
        return await super().embed(model=model, texts=texts)


async def test_embed_in_batches_splits_into_ceil_batches_and_preserves_order() -> None:
    provider = _CountingEmbedProvider()
    client = LLMClient({"fake": provider}, {r: ModelSpec("fake", "fake-1") for r in ModelRole})
    texts = [f"chunk number {i}" for i in range(5)]

    embedded = await embed_in_batches(client, texts, batch_size=2, concurrency=4)

    assert provider.calls == 3  # ceil(5 / 2)
    assert provider.batch_sizes == [2, 2, 1]
    # Batched embedding equals one big embed — same vectors, same order.
    whole = await fake_llm_client().embed(ModelRole.EMBED, texts)
    assert embedded.vectors == whole.vectors
    # ...and the usage of the parts adds up to the usage of the whole, or splitting a document
    # into batches would quietly divide its bill by the number of batches.
    assert embedded.usage == whole.usage


async def test_embed_in_batches_empty_makes_no_calls() -> None:
    provider = _CountingEmbedProvider()
    client = LLMClient({"fake": provider}, {r: ModelSpec("fake", "fake-1") for r in ModelRole})
    embedded = await embed_in_batches(client, [], batch_size=2, concurrency=4)
    assert embedded.vectors == [] and embedded.usage.total_tokens == 0
    assert provider.calls == 0


async def _make_source(
    session: AsyncSession,
    store: InMemoryBlobStore,
    *,
    data: bytes,
    content_type: str = "text/plain",
):
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    session.add(learner)
    await session.flush()
    return await ingestion.create_source(
        session,
        store,
        learner_id=learner.id,
        kind=SourceKind.FILE,
        origin="notes.txt",
        content_type=content_type,
        data=data,
    )


async def _chunk_count(session: AsyncSession, source_id: uuid.UUID) -> int:
    return (
        await session.scalar(
            select(func.count()).select_from(Chunk).where(Chunk.source_id == source_id)
        )
    ) or 0


# --- chunking (pure) --------------------------------------------------------


def test_normalize_collapses_whitespace() -> None:
    assert normalize("a\n\n  b\tc ") == "a b c"


def test_normalize_strips_nul_bytes() -> None:
    """Postgres text columns reject 0x00 outright, and \\s does not match it, so a single
    stray NUL from PDF extraction failed the whole chunk INSERT and the entire ingestion."""
    assert normalize("clean\x00text") == "cleantext"
    assert "\x00" not in normalize("a\x00\x00b c")


def test_chunk_units_output_never_contains_nul() -> None:
    units = chunk_units([ExtractedUnit(text="page one\x00 body text")], size=200, overlap=50)
    assert units and all("\x00" not in u.text for u in units)


def test_chunk_units_windows_long_text_with_overlap() -> None:
    text = " ".join(f"word{i}" for i in range(800))
    units = chunk_units([ExtractedUnit(text=text)], size=200, overlap=50)
    assert len(units) > 1
    starts = [u.locator["char_start"] for u in units]
    assert starts[0] == 0
    assert starts == sorted(starts) and len(set(starts)) == len(starts)  # strictly increasing
    assert all(u.text for u in units)


def test_chunk_units_short_text_single_window() -> None:
    units = chunk_units([ExtractedUnit(text="short text")], size=200, overlap=50)
    assert [u.text for u in units] == ["short text"]


def test_chunk_units_empty_yields_nothing() -> None:
    assert chunk_units([ExtractedUnit(text="   ")]) == []


# --- pipeline + service (DB) ------------------------------------------------


async def test_ingest_txt_creates_embedded_chunks(db_session: AsyncSession) -> None:
    store = InMemoryBlobStore()
    source = await _make_source(db_session, store, data=b"The cell is the unit of life.")
    result = await ingestion.ingest_source(db_session, store, fake_llm_client(), source.id)
    assert result is not None  # the source was claimable

    assert result.status == SourceStatus.DONE
    assert result.meta["chunk_count"] >= 1
    chunks = (await db_session.scalars(select(Chunk).where(Chunk.source_id == source.id))).all()
    assert len(chunks) >= 1
    assert len(chunks[0].embedding) == 768
    assert chunks[0].provenance["source_id"] == str(source.id)
    assert chunks[0].provenance["method"] == "text"


async def test_ingesting_a_document_records_what_the_embeddings_cost(
    db_session: AsyncSession,
) -> None:
    """Embedding a corpus is ingestion's largest model bill and used to be recorded as nothing."""
    store = InMemoryBlobStore()
    big = " ".join(f"sentence number {i} about photosynthesis." for i in range(200)).encode()
    source = await _make_source(db_session, store, data=big)

    await ingestion.ingest_source(db_session, store, fake_llm_client(), source.id)

    calls = (
        await db_session.scalars(
            select(LLMCall).where(LLMCall.learner_id == source.learner_id, LLMCall.role == "embed")
        )
    ).all()
    assert len(calls) == 1  # one record for the whole batched embedding pass, not one per batch
    assert calls[0].input_tokens > 0


async def test_ingest_is_idempotent(db_session: AsyncSession) -> None:
    store = InMemoryBlobStore()
    big = " ".join(f"sentence number {i} about photosynthesis." for i in range(200)).encode()
    source = await _make_source(db_session, store, data=big)

    await ingestion.ingest_source(db_session, store, fake_llm_client(), source.id)
    first = await _chunk_count(db_session, source.id)
    await ingestion.reset_for_reingest(db_session, source.id)
    await ingestion.ingest_source(db_session, store, fake_llm_client(), source.id)
    second = await _chunk_count(db_session, source.id)

    assert first > 0
    assert first == second  # replaced, not duplicated


async def test_ingest_empty_text_fails(db_session: AsyncSession) -> None:
    store = InMemoryBlobStore()
    source = await _make_source(db_session, store, data=b"   \n\t ")
    result = await ingestion.ingest_source(db_session, store, fake_llm_client(), source.id)
    assert result is not None  # the source was claimable

    assert result.status == SourceStatus.FAILED
    assert result.error
    assert await _chunk_count(db_session, source.id) == 0


async def test_ingest_unsupported_type_fails(db_session: AsyncSession) -> None:
    store = InMemoryBlobStore()
    source = await _make_source(
        db_session, store, data=b"\x00\x01\x02", content_type="application/zip"
    )
    result = await ingestion.ingest_source(db_session, store, fake_llm_client(), source.id)
    assert result is not None  # the source was claimable

    assert result.status == SourceStatus.FAILED
    assert "adapter" in (result.error or "").lower()
    assert await _chunk_count(db_session, source.id) == 0


# --- per-chunk KC auto-tagging (DB) -----------------------------------------


class _CountingCompleteProvider(FakeProvider):
    """A fake provider that records how many completion (tagging) calls it received."""

    def __init__(self, reply: str = "{}") -> None:
        super().__init__(reply=reply)
        self.complete_calls = 0

    async def complete(
        self,
        *,
        model: str,
        messages: Sequence[ChatMessage],
        system: str | None = None,
        max_tokens: int = 1024,
        tools: Sequence[ToolDef] | None = None,
    ) -> ChatResponse:
        self.complete_calls += 1
        return await super().complete(
            model=model, messages=messages, system=system, max_tokens=max_tokens
        )


async def _kc_graph(session: AsyncSession) -> tuple[Subject, Topic, KC, KC]:
    """A tiny Subject → Topic → {kc_a, kc_b} graph. ``kc_a.slug`` sorts first (candidate #1)."""
    subject = Subject(slug=f"bio-{uuid.uuid4().hex[:6]}", name="Biology")
    session.add(subject)
    await session.flush()
    topic = Topic(subject_id=subject.id, slug="a-cells", name="Cells")
    session.add(topic)
    await session.flush()
    kc_a = KC(topic_id=topic.id, slug="atp", name="ATP synthesis", description="How cells make ATP")
    kc_b = KC(topic_id=topic.id, slug="cell-theory", name="Cell theory")
    session.add_all([kc_a, kc_b])
    await session.flush()
    return subject, topic, kc_a, kc_b


async def _scoped_source(
    session: AsyncSession,
    store: InMemoryBlobStore,
    *,
    data: bytes,
    subject_id: uuid.UUID | None = None,
    topic_id: uuid.UUID | None = None,
):
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
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
        subject_id=subject_id,
        topic_id=topic_id,
    )


async def _chunk_kc_count(session: AsyncSession, source_id: uuid.UUID) -> int:
    return (
        await session.scalar(
            select(func.count())
            .select_from(ChunkKC)
            .join(Chunk, ChunkKC.chunk_id == Chunk.id)
            .where(Chunk.source_id == source_id)
        )
    ) or 0


async def test_ingest_tags_chunks_with_subject_scoped_kcs(db_session: AsyncSession) -> None:
    store = InMemoryBlobStore()
    subject, _topic, kc_a, _kc_b = await _kc_graph(db_session)
    source = await _scoped_source(
        db_session, store, subject_id=subject.id, data=b"The mitochondrion makes ATP for the cell."
    )
    # The model tags every chunk to candidate #1 (the lowest-slug KC = kc_a "atp").
    client = fake_llm_client('{"tags": [{"kc": 1, "confidence": 0.9}]}')

    result = await ingestion.ingest_source(db_session, store, client, source.id)
    assert result is not None  # the source was claimable
    assert result.status == SourceStatus.DONE

    links = (
        await db_session.scalars(
            select(ChunkKC)
            .join(Chunk, ChunkKC.chunk_id == Chunk.id)
            .where(Chunk.source_id == source.id)
        )
    ).all()
    assert links  # chunks were tagged
    assert all(link.kc_id == kc_a.id for link in links)  # index #1 mapped to the right KC
    assert all(link.confidence == 0.9 for link in links)


async def test_ingest_unscoped_source_skips_kc_tagging(db_session: AsyncSession) -> None:
    store = InMemoryBlobStore()
    await _kc_graph(db_session)  # KCs exist, but this source isn't scoped to them
    provider = _CountingCompleteProvider(reply='{"tags": [{"kc": 1, "confidence": 0.9}]}')
    client = LLMClient({"fake": provider}, {r: ModelSpec("fake", "fake-1") for r in ModelRole})
    source = await _make_source(db_session, store, data=b"Unrelated notes about cooking pasta.")

    result = await ingestion.ingest_source(db_session, store, client, source.id)
    assert result is not None  # the source was claimable

    assert result.status == SourceStatus.DONE
    assert provider.complete_calls == 0  # no candidate KCs ⇒ the tagger is never consulted
    assert await _chunk_kc_count(db_session, source.id) == 0


async def test_reingest_replaces_kc_tags(db_session: AsyncSession) -> None:
    store = InMemoryBlobStore()
    subject, _topic, _kc_a, _kc_b = await _kc_graph(db_session)
    source = await _scoped_source(
        db_session, store, subject_id=subject.id, data=b"Cells respire to release energy."
    )
    client = fake_llm_client('{"tags": [{"kc": 1, "confidence": 0.8}]}')

    await ingestion.ingest_source(db_session, store, client, source.id)
    first = await _chunk_kc_count(db_session, source.id)
    await ingestion.reset_for_reingest(db_session, source.id)
    await ingestion.ingest_source(db_session, store, client, source.id)
    second = await _chunk_kc_count(db_session, source.id)

    assert first > 0
    assert first == second  # tags cascade away with the replaced chunks, not duplicated


async def test_load_candidate_kcs_scopes_to_topic_then_subject(db_session: AsyncSession) -> None:
    subject, topic, kc_a, kc_b = await _kc_graph(db_session)
    topic2 = Topic(subject_id=subject.id, slug="z-genetics", name="Genetics")
    db_session.add(topic2)
    await db_session.flush()
    kc_c = KC(topic_id=topic2.id, slug="alleles", name="Alleles")
    db_session.add(kc_c)
    await db_session.flush()

    def _src(**scope: uuid.UUID) -> Source:
        return Source(learner_id=uuid.uuid4(), kind=SourceKind.FILE, origin="x", **scope)

    subject_scoped = await load_candidate_kcs(db_session, _src(subject_id=subject.id))
    topic_scoped = await load_candidate_kcs(db_session, _src(topic_id=topic.id))
    unscoped = await load_candidate_kcs(db_session, _src())

    assert {c.id for c in subject_scoped} == {kc_a.id, kc_b.id, kc_c.id}  # whole subject
    assert {c.id for c in topic_scoped} == {kc_a.id, kc_b.id}  # just that topic
    assert unscoped == []  # no scope ⇒ no candidates
