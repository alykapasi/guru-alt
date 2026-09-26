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
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_llm_client
from app.llm import ModelRole
from app.llm.registry import fake_llm_client
from app.main import app
from app.models.knowledge import Subject
from app.models.learner import Learner
from app.models.source import Chunk, Source, SourceKind, SourceStatus
from app.rag import retrieval
from app.rag.scope import SourceScope
from tests.embedding import FAKE_SPACE, crowd

API = "/api/v1"
_FAKE = fake_llm_client()


async def _embed(text: str) -> list[float]:
    return (await _FAKE.embed(ModelRole.EMBED, [text])).vectors[0]


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
        embedding_space=FAKE_SPACE,
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

    hits = await retrieval.retrieve(
        db_session, fake_llm_client(), "marker", scope=SourceScope(learner_id=learner.id)
    )
    assert hits[0].chunk_id == a.id  # found by the vector path alone (no keyword overlap)


async def test_keyword_match_boosts_ranking(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    source = await _source(db_session, learner)
    a = await _chunk(db_session, source, "the powerhouse mitochondria of the cell", ordinal=0)
    b = await _chunk(db_session, source, "rivers flow to the ocean slowly", ordinal=1)

    hits = await retrieval.retrieve(
        db_session, fake_llm_client(), "mitochondria", scope=SourceScope(learner_id=learner.id)
    )
    assert {h.chunk_id for h in hits} == {a.id, b.id}  # vector returns both
    assert hits[0].chunk_id == a.id  # keyword hit fuses higher


async def test_retrieval_respects_limit(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    source = await _source(db_session, learner)
    for i in range(5):
        await _chunk(db_session, source, f"topic sentence number {i}", ordinal=i)

    hits = await retrieval.retrieve(
        db_session, fake_llm_client(), "topic", limit=2, scope=SourceScope(learner_id=learner.id)
    )
    assert len(hits) == 2


async def test_empty_query_returns_empty(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    assert (
        await retrieval.retrieve(
            db_session, fake_llm_client(), "   ", scope=SourceScope(learner_id=learner.id)
        )
        == []
    )


# --- scoping ----------------------------------------------------------------


async def test_retrieval_scoped_to_learner(db_session: AsyncSession) -> None:
    l1, l2 = await _learner(db_session), await _learner(db_session)
    mine = await _chunk(db_session, await _source(db_session, l1), "shared keyword content")
    theirs = await _chunk(db_session, await _source(db_session, l2), "shared keyword content")

    hits = await retrieval.retrieve(
        db_session, fake_llm_client(), "shared", scope=SourceScope(learner_id=l1.id)
    )
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
        db_session,
        fake_llm_client(),
        "calculus",
        scope=SourceScope(learner_id=learner.id, subject_id=s1.id),
    )
    ids = {h.chunk_id for h in hits}
    assert in_scope.id in ids and out_scope.id not in ids


async def test_a_subject_scope_excludes_untagged_sources_unless_asked(
    db_session: AsyncSession,
) -> None:
    """The default, and the flag that widens it (S26).

    Subject tagging is optional at upload, so "no subject" and "a different subject" are
    different claims: the second says the material is about something else, the first says
    nobody said. Callers that already narrow by subject keep the strict reading; content
    generation opts into the wider one, because for it the alternative to imperfect grounding
    is no grounding at all.
    """
    learner = await _learner(db_session)
    subject = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name="S1")
    db_session.add(subject)
    await db_session.flush()
    tagged = await _chunk(
        db_session, await _source(db_session, learner, subject_id=subject.id), "calculus integrals"
    )
    untagged = await _chunk(db_session, await _source(db_session, learner), "calculus integrals")

    strict = await retrieval.retrieve(
        db_session,
        fake_llm_client(),
        "calculus",
        scope=SourceScope(learner_id=learner.id, subject_id=subject.id),
    )
    widened = await retrieval.retrieve(
        db_session,
        fake_llm_client(),
        "calculus",
        scope=SourceScope(learner_id=learner.id, subject_id=subject.id, include_untagged=True),
    )

    assert {h.chunk_id for h in strict} == {tagged.id}
    assert {h.chunk_id for h in widened} == {tagged.id, untagged.id}


async def test_retrieval_scoped_to_source_ids(db_session: AsyncSession) -> None:
    """A conversation narrowed to specific sources (Phase 7) — distinct from the single
    ``source_id`` the debug endpoint uses."""
    learner = await _learner(db_session)
    src_a = await _source(db_session, learner)
    src_b = await _source(db_session, learner)
    src_c = await _source(db_session, learner)
    a = await _chunk(db_session, src_a, "narrowed keyword content")
    b = await _chunk(db_session, src_b, "narrowed keyword content")
    c = await _chunk(db_session, src_c, "narrowed keyword content")

    hits = await retrieval.retrieve(
        db_session,
        fake_llm_client(),
        "narrowed",
        scope=SourceScope(learner_id=learner.id, source_ids=(src_a.id, src_b.id)),
    )
    ids = {h.chunk_id for h in hits}
    assert ids == {a.id, b.id}
    assert c.id not in ids


# --- endpoint ---------------------------------------------------------------


async def test_retrieve_endpoint(
    api_client: AsyncClient, db_session: AsyncSession, fake_llm: None, api_learner: Learner
) -> None:
    source = await _source(db_session, api_learner)
    await _chunk(db_session, source, "mitochondria powerhouse cell biology")

    r = await api_client.post(f"{API}/retrieve", json={"query": "mitochondria"})
    assert r.status_code == 200, r.text
    hits = r.json()
    assert len(hits) >= 1
    assert hits[0]["text"] and "score" in hits[0] and "provenance" in hits[0]


# --- vectors are only compared within their own embedding space --------------------------------


async def test_a_vector_from_another_embedding_model_is_not_ranked_against_this_query(
    db_session: AsyncSession,
) -> None:
    """A same-dimension model swap is a config edit with no error and no schema change; without
    the space recorded, its vectors would be ranked confidently against queries they have
    nothing to do with.

    Both chunks carry the query's own vector but text that shares no word with it, so the
    keyword arm cannot reach either: whatever comes back, the vector arm put it there.
    """
    learner = await _learner(db_session)
    source = await _source(db_session, learner)
    vector = await _embed("mitochondria")
    reachable = await _chunk(
        db_session, source, "quiet rivers reach the sea", embedding=vector, ordinal=0
    )
    stale = await _chunk(db_session, source, "distant hills at dusk", embedding=vector, ordinal=1)
    stale.embedding_space = "ollama:some-other-embedder:768"
    await db_session.flush()

    hits = await retrieval.retrieve(
        db_session, fake_llm_client(), "mitochondria", scope=SourceScope(learner_id=learner.id)
    )

    ids = {h.chunk_id for h in hits}
    assert reachable.id in ids
    assert stale.id not in ids


async def test_an_old_vector_does_not_make_its_text_unsearchable(
    db_session: AsyncSession,
) -> None:
    """Only the vector went stale. The words are still the words, and the keyword arm is the
    reason a re-embedding backlog degrades retrieval rather than deleting it."""
    learner = await _learner(db_session)
    source = await _source(db_session, learner)
    stale = await _chunk(db_session, source, "the powerhouse mitochondria of the cell", ordinal=0)
    stale.embedding_space = "ollama:some-other-embedder:768"
    await db_session.flush()

    hits = await retrieval.retrieve(
        db_session, fake_llm_client(), "mitochondria", scope=SourceScope(learner_id=learner.id)
    )
    assert {h.chunk_id for h in hits} == {stale.id}


async def test_picked_sources_are_all_a_scope_reads(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    picked, other = await _source(db_session, learner), await _source(db_session, learner)
    a = await _chunk(db_session, picked, "calculus limits", ordinal=0)
    await _chunk(db_session, other, "calculus derivatives", ordinal=0)

    hits = await retrieval.retrieve(
        db_session,
        fake_llm_client(),
        "calculus",
        scope=SourceScope(learner_id=learner.id, source_ids=(picked.id,)),
    )

    assert {h.chunk_id for h in hits} == {a.id}


async def test_the_space_names_the_provider_the_model_and_the_dimension() -> None:
    """Two backends serving the same model name are not a promise of the same weights."""
    from app.llm.embedding_space import current_space

    assert current_space(fake_llm_client(), dim=768) == "fake:fake-1:768"


# --- exactness under the learner filter (S76) -------------------------------


async def test_a_learners_chunks_are_found_however_near_other_learners_vectors_are(
    db_session: AsyncSession,
) -> None:
    """The failure behind S76's intermittent gate, pinned rather than waited for.

    Whether the planner answers the vector arm exactly or through the HNSW index was left to
    its statistics. The index finds the ~40 nearest vectors in the *whole table* and only then
    applies the learner filter, so a learner whose chunks are further from the query than
    other rows got some or none of them back. Disabling sorts makes the index path the
    planner's choice deterministically, which is what stale or small-table statistics did by
    accident.
    """
    stranger = await _source(db_session, await _learner(db_session))
    for i, vector in enumerate(crowd(await _embed("marker"))):
        await _chunk(db_session, stranger, f"someone else's note {i}", embedding=vector, ordinal=i)
    learner = await _learner(db_session)
    source = await _source(db_session, learner)
    mine = [
        await _chunk(
            db_session, source, f"lorem ipsum {i}", embedding=await _embed(f"far {i}"), ordinal=i
        )
        for i in range(3)
    ]
    await db_session.execute(text("SET LOCAL enable_sort = off"))

    hits = await retrieval.retrieve(
        db_session, fake_llm_client(), "marker", scope=SourceScope(learner_id=learner.id)
    )

    assert {h.chunk_id for h in hits} == {c.id for c in mine}
