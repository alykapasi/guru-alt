"""Vector-only memory retrieval: ranking, limits, and learner scoping.

Fake embeddings are a deterministic hash of the text, so they aren't semantic — but they
*are* reproducible: a memory seeded with ``embedding = embed(X)`` is the exact nearest
neighbour of a query ``X``. Mirrors ``tests/test_retrieval.py``'s idiom for ``Chunk``.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.llm import ModelRole
from app.llm.registry import fake_llm_client
from app.memory import retrieval
from app.models.learner import Learner
from app.models.memory import Memory, MemoryKind

_FAKE = fake_llm_client()


async def _embed(text: str) -> list[float]:
    return (await _FAKE.embed(ModelRole.EMBED, [text]))[0]


async def _learner(session: AsyncSession) -> Learner:
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    session.add(learner)
    await session.flush()
    return learner


async def _memory(
    session: AsyncSession,
    learner: Learner,
    content: str,
    *,
    kind: MemoryKind = MemoryKind.FACT,
    embedding: list[float] | None = None,
) -> Memory:
    memory = Memory(
        learner_id=learner.id,
        kind=kind,
        content=content,
        embedding=embedding if embedding is not None else await _embed(content),
    )
    session.add(memory)
    await session.flush()
    return memory


async def test_vector_match_ranks_first(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    # `a`'s embedding equals the query's; its text shares no words with the query.
    a = await _memory(
        db_session, learner, "lorem ipsum dolor sit", embedding=await _embed("marker")
    )
    await _memory(db_session, learner, "an unrelated fact", embedding=await _embed("other"))

    hits = await retrieval.retrieve(db_session, fake_llm_client(), "marker", learner_id=learner.id)
    assert hits[0].id == a.id


async def test_retrieval_respects_limit(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    for i in range(5):
        await _memory(db_session, learner, f"fact number {i}")

    hits = await retrieval.retrieve(
        db_session, fake_llm_client(), "fact", learner_id=learner.id, limit=2
    )
    assert len(hits) == 2


async def test_empty_query_returns_empty(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    assert (
        await retrieval.retrieve(db_session, fake_llm_client(), "   ", learner_id=learner.id) == []
    )


async def test_retrieval_scoped_to_learner(db_session: AsyncSession) -> None:
    l1, l2 = await _learner(db_session), await _learner(db_session)
    mine = await _memory(db_session, l1, "shared keyword content")
    theirs = await _memory(db_session, l2, "shared keyword content")

    hits = await retrieval.retrieve(db_session, fake_llm_client(), "shared", learner_id=l1.id)
    ids = {h.id for h in hits}
    assert mine.id in ids and theirs.id not in ids


async def test_hit_shape_omits_the_embedding(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    memory = await _memory(db_session, learner, "target content", embedding=await _embed("q"))

    hits = await retrieval.retrieve(db_session, fake_llm_client(), "q", learner_id=learner.id)

    assert hits[0].id == memory.id
    assert hits[0].kind == MemoryKind.FACT
    assert hits[0].content == "target content"
    assert not hasattr(hits[0], "embedding")
