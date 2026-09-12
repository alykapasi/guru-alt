"""Memory service + API: write-back (extract + dedup + persist) and view/erase."""

import json
import uuid
from collections.abc import Iterator

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_memory_write_back_enqueuer
from app.llm.registry import fake_llm_client
from app.main import app
from app.models.chat import Conversation, LLMCall, Message
from app.models.learner import Learner
from app.models.memory import Memory, MemoryKind, MemoryStatus
from app.services import memory as svc
from tests.embedding import FAKE_SPACE

API = "/api/v1"

FACT_REPLY = json.dumps(
    {
        "memories": [
            {"kind": "fact", "content": "Studying for the MCAT."},
            {"kind": "preference", "content": "Prefers morning study sessions."},
        ]
    }
)


async def _learner(session: AsyncSession) -> Learner:
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    session.add(learner)
    await session.flush()
    return learner


async def _memory(session: AsyncSession, learner: Learner, content: str) -> Memory:
    """One stored memory, embedded with a distinctive constant so a re-embedding is visible."""
    memory = Memory(
        embedding_space=FAKE_SPACE,
        learner_id=learner.id,
        kind=MemoryKind.FACT,
        content=content,
        embedding=[0.5] * 768,
    )
    session.add(memory)
    await session.flush()
    return memory


async def _conversation_with_messages(session: AsyncSession, learner: Learner) -> Conversation:
    conversation = Conversation(learner_id=learner.id)
    session.add(conversation)
    await session.flush()
    session.add_all(
        [
            Message(
                conversation_id=conversation.id,
                role="user",
                content="I'm studying for the MCAT, mornings only.",
            ),
            Message(
                conversation_id=conversation.id,
                role="assistant",
                content="Let's start with organic chemistry.",
            ),
        ]
    )
    await session.flush()
    return conversation


# --- write_back ---------------------------------------------------------------


async def test_write_back_persists_memories_and_logs_the_call(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    conversation = await _conversation_with_messages(db_session, learner)

    created = await svc.write_back(
        db_session, fake_llm_client(FACT_REPLY), conversation_id=conversation.id
    )

    assert {m.content for m in created} == {
        "Studying for the MCAT.",
        "Prefers morning study sessions.",
    }
    assert all(m.learner_id == learner.id for m in created)
    assert all(m.conversation_id == conversation.id for m in created)

    rows = (await db_session.scalars(select(Memory).where(Memory.learner_id == learner.id))).all()
    assert len(rows) == 2

    # Both paid calls: extracting the memories, and embedding them. The embedding used to be
    # invisible because embed() returned bare vectors with no usage attached.
    calls = (
        await db_session.scalars(select(LLMCall).where(LLMCall.learner_id == learner.id))
    ).all()
    assert sorted(c.role for c in calls) == ["embed", "fast"]
    assert all(c.conversation_id == conversation.id for c in calls)
    embed_call = next(c for c in calls if c.role == "embed")
    assert embed_call.input_tokens > 0


async def test_write_back_dedupes_against_an_existing_near_duplicate(
    db_session: AsyncSession,
) -> None:
    learner = await _learner(db_session)
    conversation = await _conversation_with_messages(db_session, learner)
    llm = fake_llm_client(FACT_REPLY)

    first = await svc.write_back(db_session, llm, conversation_id=conversation.id)
    assert len(first) == 2

    second = await svc.write_back(db_session, llm, conversation_id=conversation.id)
    assert second == []

    rows = (await db_session.scalars(select(Memory).where(Memory.learner_id == learner.id))).all()
    assert len(rows) == 2  # unchanged — the re-extraction deduped against the first batch


async def test_write_back_dedupes_within_one_batch(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    conversation = await _conversation_with_messages(db_session, learner)
    reply = json.dumps(
        {
            "memories": [
                {"kind": "fact", "content": "Studying for the MCAT."},
                {"kind": "fact", "content": "Studying for the MCAT."},
            ]
        }
    )

    created = await svc.write_back(
        db_session, fake_llm_client(reply), conversation_id=conversation.id
    )

    assert len(created) == 1  # the second identical item deduped against the first, intra-batch


async def test_write_back_unknown_conversation_returns_empty(db_session: AsyncSession) -> None:
    created = await svc.write_back(
        db_session, fake_llm_client(FACT_REPLY), conversation_id=uuid.uuid4()
    )
    assert created == []


async def test_write_back_no_extracted_memories_persists_nothing(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    conversation = await _conversation_with_messages(db_session, learner)

    created = await svc.write_back(
        db_session, fake_llm_client("not json"), conversation_id=conversation.id
    )

    assert created == []
    rows = (await db_session.scalars(select(Memory).where(Memory.learner_id == learner.id))).all()
    assert rows == []


# --- list / delete --------------------------------------------------------------


async def test_list_memories_most_recent_first_respects_limit(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    conversation = await _conversation_with_messages(db_session, learner)
    for i in range(3):
        db_session.add(
            Memory(
                embedding_space=FAKE_SPACE,
                learner_id=learner.id,
                conversation_id=conversation.id,
                kind=MemoryKind.FACT,
                content=f"fact {i}",
                embedding=[0.0] * 768,
            )
        )
    await db_session.flush()

    rows = await svc.list_memories(db_session, learner.id, limit=2)
    assert len(rows) == 2
    assert rows[0].created_at >= rows[1].created_at


async def test_delete_memory_own_row(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    memory = Memory(
        embedding_space=FAKE_SPACE,
        learner_id=learner.id,
        kind=MemoryKind.FACT,
        content="x",
        embedding=[0.0] * 768,
    )
    db_session.add(memory)
    await db_session.flush()

    assert await svc.delete_memory(db_session, learner.id, memory.id) is True
    # The row survives as a tombstone (S42) — its embedding is the only thing that can
    # recognise the same fact being extracted again. What must be gone is its visibility.
    assert await svc.list_memories(db_session, learner.id) == []
    tombstone = await db_session.get(Memory, memory.id)
    assert tombstone is not None and tombstone.status == MemoryStatus.DELETED


async def test_delete_memory_foreign_row_is_a_noop(db_session: AsyncSession) -> None:
    owner, other = await _learner(db_session), await _learner(db_session)
    memory = Memory(
        embedding_space=FAKE_SPACE,
        learner_id=owner.id,
        kind=MemoryKind.FACT,
        content="x",
        embedding=[0.0] * 768,
    )
    db_session.add(memory)
    await db_session.flush()

    assert await svc.delete_memory(db_session, other.id, memory.id) is False
    assert await db_session.get(Memory, memory.id) is not None


async def test_delete_memory_missing_id_is_a_noop(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    assert await svc.delete_memory(db_session, learner.id, uuid.uuid4()) is False


async def test_delete_all_memories_scoped_to_the_learner(db_session: AsyncSession) -> None:
    mine, theirs = await _learner(db_session), await _learner(db_session)
    db_session.add_all(
        [
            Memory(
                embedding_space=FAKE_SPACE,
                learner_id=mine.id,
                kind=MemoryKind.FACT,
                content="a",
                embedding=[0.0] * 768,
            ),
            Memory(
                embedding_space=FAKE_SPACE,
                learner_id=mine.id,
                kind=MemoryKind.FACT,
                content="b",
                embedding=[0.0] * 768,
            ),
            Memory(
                embedding_space=FAKE_SPACE,
                learner_id=theirs.id,
                kind=MemoryKind.FACT,
                content="c",
                embedding=[0.0] * 768,
            ),
        ]
    )
    await db_session.flush()

    count = await svc.delete_all_memories(db_session, mine.id)

    assert count == 2
    assert await svc.list_memories(db_session, mine.id) == []
    assert len(await svc.list_memories(db_session, theirs.id)) == 1


# --- API ------------------------------------------------------------------------


@pytest.fixture
def fake_enqueue() -> Iterator[list[uuid.UUID]]:
    """Capture enqueued write-back jobs instead of touching a real broker."""
    enqueued: list[uuid.UUID] = []

    async def _enqueue(conversation_id: uuid.UUID) -> None:
        enqueued.append(conversation_id)

    app.dependency_overrides[get_memory_write_back_enqueuer] = lambda: _enqueue
    yield enqueued
    app.dependency_overrides.pop(get_memory_write_back_enqueuer, None)


async def test_write_back_endpoint_enqueues_the_conversation(
    api_client: AsyncClient, db_session: AsyncSession, fake_enqueue: list[uuid.UUID]
) -> None:
    conv = (await api_client.post(f"{API}/conversations", json={})).json()

    r = await api_client.post(f"{API}/conversations/{conv['id']}/memory/write-back")

    assert r.status_code == 202, r.text
    assert r.json() == {"status": "queued"}
    assert fake_enqueue == [uuid.UUID(conv["id"])]


async def test_write_back_endpoint_404s_on_missing_conversation(
    api_client: AsyncClient, fake_enqueue: list[uuid.UUID]
) -> None:
    r = await api_client.post(f"{API}/conversations/{uuid.uuid4()}/memory/write-back")
    assert r.status_code == 404
    assert fake_enqueue == []


async def test_get_memory_omits_the_embedding(
    api_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    learner = api_learner
    db_session.add(
        Memory(
            embedding_space=FAKE_SPACE,
            learner_id=learner.id,
            kind=MemoryKind.FACT,
            content="x",
            embedding=[0.0] * 768,
        )
    )
    await db_session.flush()

    r = await api_client.get(f"{API}/memory")
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body) == 1
    assert body[0]["content"] == "x"
    assert "embedding" not in body[0]


async def test_delete_memory_endpoint(
    api_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    learner = api_learner
    memory = Memory(
        embedding_space=FAKE_SPACE,
        learner_id=learner.id,
        kind=MemoryKind.FACT,
        content="x",
        embedding=[0.0] * 768,
    )
    db_session.add(memory)
    await db_session.flush()

    r = await api_client.delete(f"{API}/memory/{memory.id}")
    assert r.status_code == 204

    assert (await api_client.get(f"{API}/memory")).json() == []


async def test_delete_memory_endpoint_404s_on_foreign_row(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    other = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    db_session.add(other)
    await db_session.flush()
    memory = Memory(
        embedding_space=FAKE_SPACE,
        learner_id=other.id,
        kind=MemoryKind.FACT,
        content="x",
        embedding=[0.0] * 768,
    )
    db_session.add(memory)
    await db_session.flush()

    r = await api_client.delete(f"{API}/memory/{memory.id}")
    assert r.status_code == 404


async def test_bulk_delete_memory_endpoint(
    api_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    learner = api_learner
    db_session.add_all(
        [
            Memory(
                embedding_space=FAKE_SPACE,
                learner_id=learner.id,
                kind=MemoryKind.FACT,
                content="a",
                embedding=[0.0] * 768,
            ),
            Memory(
                embedding_space=FAKE_SPACE,
                learner_id=learner.id,
                kind=MemoryKind.FACT,
                content="b",
                embedding=[0.0] * 768,
            ),
        ]
    )
    await db_session.flush()

    r = await api_client.delete(f"{API}/memory")
    assert r.status_code == 204

    assert (await api_client.get(f"{API}/memory")).json() == []


async def test_a_memory_from_another_embedding_model_is_not_retrieved_or_deduped_against(
    db_session: AsyncSession,
) -> None:
    """Distance to a vector from a different model is not a distance to anything: it would both
    miss real duplicates and suppress genuinely new memories at random."""
    learner = await _learner(db_session)
    conversation = await _conversation_with_messages(db_session, learner)
    llm = fake_llm_client(FACT_REPLY)

    first = await svc.write_back(db_session, llm, conversation_id=conversation.id)
    assert len(first) == 2
    for memory in first:
        memory.embedding_space = "ollama:some-other-embedder:768"
    await db_session.flush()

    # Same conversation, same facts: dedup cannot see the old space, so they are written again.
    # The extraction cursor (S43) would otherwise make a second run over the same messages a
    # no-op, which is correct but not what this test is about.
    conversation.memory_watermark = None
    await db_session.flush()
    second = await svc.write_back(db_session, llm, conversation_id=conversation.id)
    assert len(second) == 2
    assert all(m.embedding_space == FAKE_SPACE for m in second)


# --- the learner can say what is actually true (S16) -----------------------------------------


async def test_a_correction_supersedes_rather_than_overwrites(db_session: AsyncSession) -> None:
    """Overwriting in place would make the system's belief look like it had always been what
    the learner just typed, which is the one thing a record of what somebody was told must not
    do — and it would throw away the embedding that recognises the old claim."""
    learner = await _learner(db_session)
    original = await _memory(db_session, learner, "Studies best in the morning")

    corrected = await svc.correct_memory(
        db_session, fake_llm_client(), learner.id, original.id, content="Studies best late at night"
    )

    assert corrected is not None
    assert corrected.id != original.id
    assert corrected.content == "Studies best late at night"
    assert corrected.status == MemoryStatus.CURRENT
    await db_session.refresh(original)
    assert original.status == MemoryStatus.SUPERSEDED
    assert original.superseded_by_id == corrected.id
    assert original.content == "Studies best in the morning", "the old claim keeps its text"


async def test_only_the_correction_is_shown_back(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    original = await _memory(db_session, learner, "Studies best in the morning")
    await svc.correct_memory(
        db_session, fake_llm_client(), learner.id, original.id, content="Studies best late at night"
    )

    listed = await svc.list_memories(db_session, learner.id)
    assert [m.content for m in listed] == ["Studies best late at night"]


async def test_a_correction_is_embedded_rather_than_inheriting_the_old_vector(
    db_session: AsyncSession,
) -> None:
    """A corrected memory carrying its predecessor's embedding is retrieved for the wrong
    questions and missed for the right ones — a subtle way of not having corrected it."""
    learner = await _learner(db_session)
    original = await _memory(db_session, learner, "Studies best in the morning")
    before = list(original.embedding)

    corrected = await svc.correct_memory(
        db_session,
        fake_llm_client(),
        learner.id,
        original.id,
        content="Something entirely different",
    )

    assert corrected is not None
    assert list(corrected.embedding) != before


async def test_correcting_someone_elses_memory_does_nothing(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    other = await _learner(db_session)
    theirs = await _memory(db_session, other, "Studies best in the morning")

    assert (
        await svc.correct_memory(
            db_session, fake_llm_client(), learner.id, theirs.id, content="Nonsense"
        )
        is None
    )
    await db_session.refresh(theirs)
    assert theirs.status == MemoryStatus.CURRENT


async def test_correcting_a_forgotten_memory_does_not_bring_it_back(
    db_session: AsyncSession,
) -> None:
    """A deleted row is a tombstone that stops the fact being re-extracted. Correcting one
    would resurrect it under a new id and defeat the thing the tombstone is for."""
    learner = await _learner(db_session)
    original = await _memory(db_session, learner, "Studies best in the morning")
    assert await svc.delete_memory(db_session, learner.id, original.id)

    assert (
        await svc.correct_memory(
            db_session, fake_llm_client(), learner.id, original.id, content="Studies best at night"
        )
        is None
    )
    assert await svc.list_memories(db_session, learner.id) == []


async def test_an_unchanged_correction_writes_nothing(db_session: AsyncSession) -> None:
    """Otherwise pressing save without editing would supersede a memory with itself and leave
    a chain of identical rows behind."""
    learner = await _learner(db_session)
    original = await _memory(db_session, learner, "Studies best in the morning")

    assert (
        await svc.correct_memory(
            db_session,
            fake_llm_client(),
            learner.id,
            original.id,
            content="  Studies best in the morning ",
        )
        is None
    )
    await db_session.refresh(original)
    assert original.status == MemoryStatus.CURRENT
