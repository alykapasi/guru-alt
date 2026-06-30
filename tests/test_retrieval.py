"""Hybrid retrieval: vector + keyword paths, RRF fusion, scope filters, and the endpoint.

Fake embeddings are a deterministic hash of the text, so they aren't semantic — but they
*are* reproducible: a chunk seeded with ``embedding = embed(X)`` is the exact nearest
neighbour of a query ``X``. That lets us drive the vector path precisely, independent of
the (genuinely semantic) keyword path.
"""

import uuid
from collections.abc import Iterator

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import DEV_LEARNER_HANDLE, get_llm_client
from app.llm import ModelRole
from app.llm.registry import fake_llm_client
from app.main import app
from app.models.knowledge import Subject
from app.models.learner import Learner
from app.models.source import Chunk, Source, SourceKind, SourceStatus
from app.rag import retrieval

API = "/api/v1"
_FAKE = fake_llm_client()


async def _embed(text: str) -> list[float]:
    return (await _FAKE.embed(ModelRole.EMBED, [text]))[0]


async def _learner(session: AsyncSession) -> Learner:
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    session.add(learner)
    await session.flush()
    return learner


async def _source(
    session: AsyncSession, learner: Learner, *, subject_id: uuid.UUID | None = None
) -> Source:
    source = Source(
        learner_id=learner.id,
        kind=SourceKind.FILE,
        origin="x.txt",
        content_type="text/plain",
        status=SourceStatus.DONE,
        subject_id=subject_id,
        meta={},
    )
    session.add(source)
    await session.flush()
    return source


async def _chunk(
    session: AsyncSession,
    source: Source,
    text: str,
    *,
    embedding: list[float] | None = None,
    ordinal: int = 0,
) -> Chunk:
    chunk = Chunk(
        source_id=source.id,
        ordinal=ordinal,
        text=text,
        embedding=embedding if embedding is not None else await _embed(text),
        provenance={"source_id": str(source.id), "method": "text"},
    )
    session.add(chunk)
    await session.flush()
    return chunk


@pytest.fixture
def fake_llm() -> Iterator[None]:
    app.dependency_overrides[get_llm_client] = lambda: fake_llm_client()
    yield
    app.dependency_overrides.pop(get_llm_client, None)


# --- ranking ----------------------------------------------------------------


async def test_vector_match_ranks_first(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    source = await _source(db_session, learner)
    # `a`'s embedding equals the query's; its text shares no words with the query.
    a = await _chunk(db_session, source, "lorem ipsum dolor sit", embedding=await _embed("marker"))
    await _chunk(db_session, source, "an unrelated body of text", embedding=await _embed("other"))

    hits = await retrieval.retrieve(db_session, fake_llm_client(), "marker", learner_id=learner.id)
    assert hits[0].chunk_id == a.id  # found by the vector path alone (no keyword overlap)


async def test_keyword_match_boosts_ranking(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    source = await _source(db_session, learner)
    a = await _chunk(db_session, source, "the powerhouse mitochondria of the cell", ordinal=0)
    b = await _chunk(db_session, source, "rivers flow to the ocean slowly", ordinal=1)

    hits = await retrieval.retrieve(
        db_session, fake_llm_client(), "mitochondria", learner_id=learner.id
    )
    assert {h.chunk_id for h in hits} == {a.id, b.id}  # vector returns both
    assert hits[0].chunk_id == a.id  # keyword hit fuses higher


async def test_retrieval_respects_limit(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    source = await _source(db_session, learner)
    for i in range(5):
        await _chunk(db_session, source, f"topic sentence number {i}", ordinal=i)

    hits = await retrieval.retrieve(
        db_session, fake_llm_client(), "topic", learner_id=learner.id, limit=2
    )
    assert len(hits) == 2


async def test_empty_query_returns_empty(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    assert (
        await retrieval.retrieve(db_session, fake_llm_client(), "   ", learner_id=learner.id) == []
    )


# --- scoping ----------------------------------------------------------------


async def test_retrieval_scoped_to_learner(db_session: AsyncSession) -> None:
    l1, l2 = await _learner(db_session), await _learner(db_session)
    mine = await _chunk(db_session, await _source(db_session, l1), "shared keyword content")
    theirs = await _chunk(db_session, await _source(db_session, l2), "shared keyword content")

    hits = await retrieval.retrieve(db_session, fake_llm_client(), "shared", learner_id=l1.id)
    ids = {h.chunk_id for h in hits}
    assert mine.id in ids and theirs.id not in ids


async def test_retrieval_scoped_to_subject(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    s1 = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name="S1")
    s2 = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name="S2")
    db_session.add_all([s1, s2])
    await db_session.flush()
    in_scope = await _chunk(
        db_session, await _source(db_session, learner, subject_id=s1.id), "calculus integrals"
    )
    out_scope = await _chunk(
        db_session, await _source(db_session, learner, subject_id=s2.id), "calculus integrals"
    )

    hits = await retrieval.retrieve(
        db_session, fake_llm_client(), "calculus", learner_id=learner.id, subject_id=s1.id
    )
    ids = {h.chunk_id for h in hits}
    assert in_scope.id in ids and out_scope.id not in ids


# --- endpoint ---------------------------------------------------------------


async def test_retrieve_endpoint(
    api_client: AsyncClient, db_session: AsyncSession, fake_llm: None
) -> None:
    learner = Learner(handle=DEV_LEARNER_HANDLE, display_name="Dev")
    db_session.add(learner)
    await db_session.flush()
    source = await _source(db_session, learner)
    await _chunk(db_session, source, "mitochondria powerhouse cell biology")

    r = await api_client.post(f"{API}/retrieve", json={"query": "mitochondria"})
    assert r.status_code == 200, r.text
    hits = r.json()
    assert len(hits) >= 1
    assert hits[0]["text"] and "score" in hits[0] and "provenance" in hits[0]
