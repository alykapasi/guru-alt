"""Vector-only memory retrieval: ranking, limits, and learner scoping.

Fake embeddings are a deterministic hash of the text, so they aren't semantic — but they
*are* reproducible: a memory seeded with ``embedding = embed(X)`` is the exact nearest
neighbour of a query ``X``. Mirrors ``tests/test_retrieval.py``'s idiom for ``Chunk``.
"""

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm import ModelRole
from app.llm.registry import fake_llm_client
from app.memory import retrieval
from app.models.learner import Learner
from app.models.memory import Memory, MemoryKind, MemoryStatus
from app.services import memory as memory_svc
from tests.embedding import FAKE_SPACE, crowd

_FAKE = fake_llm_client()


async def _embed(text: str) -> list[float]:
    return (await _FAKE.embed(ModelRole.EMBED, [text])).vectors[0]


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
        embedding_space=FAKE_SPACE,
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


async def test_a_learners_memories_are_found_however_near_other_learners_vectors_are(
    db_session: AsyncSession,
) -> None:
    """S76, for memories: the index answered before the learner filter, so a learner whose
    memories were further from the query than other rows got some or none of them back.
    Disabling sorts makes that index path the planner's choice deterministically."""
    stranger = await _learner(db_session)
    for i, vector in enumerate(crowd(await _embed("marker"))):
        await _memory(db_session, stranger, f"someone else's fact {i}", embedding=vector)
    learner = await _learner(db_session)
    mine = [
        await _memory(db_session, learner, f"lorem ipsum {i}", embedding=await _embed(f"far {i}"))
        for i in range(3)
    ]
    await db_session.execute(text("SET LOCAL enable_sort = off"))

    hits = await retrieval.retrieve(db_session, fake_llm_client(), "marker", learner_id=learner.id)

    assert {h.id for h in hits} == {m.id for m in mine}


async def test_duplicate_detection_finds_the_learners_own_nearest_memory(
    db_session: AsyncSession,
) -> None:
    """S76, where it costs most: extraction recognises a fact it has already seen by the nearest
    existing memory. If other learners' vectors crowd the index, that lookup comes back empty,
    and a deleted memory returns or a correction is stored as a second fact."""
    stranger = await _learner(db_session)
    for i, vector in enumerate(crowd(await _embed("marker"))):
        await _memory(db_session, stranger, f"someone else's fact {i}", embedding=vector)
    learner = await _learner(db_session)
    mine = await _memory(db_session, learner, "lorem ipsum", embedding=await _embed("far"))
    await db_session.execute(text("SET LOCAL enable_sort = off"))

    nearest = await memory_svc._nearest(
        db_session,
        learner.id,
        MemoryKind.FACT,
        await _embed("marker"),
        max_distance=2.0,
        space=FAKE_SPACE,
        status=MemoryStatus.CURRENT,
    )

    assert nearest is not None and nearest.id == mine.id
