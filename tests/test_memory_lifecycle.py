"""Memory correction and forgetting are durable (S42).

Three things were lost. A near-duplicate was *skipped*, so a learner correcting a preference
had the correction discarded and the stale entry kept — precisely backwards. A deleted memory
was hard-deleted, so the next write-back over the same conversation re-extracted it and it came
back. And retrieval returned the nearest entries with no floor, so a learner with a handful of
memories had all of them injected into every turn regardless of the question.
"""

import contextlib
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.llm.registry import fake_llm_client
from app.memory import retrieval
from app.models.chat import Conversation, Message
from app.models.learner import Learner
from app.models.memory import Memory, MemoryKind, MemoryStatus
from app.services import memory as svc
from tests.embedding import FAKE_SPACE

MORNINGS = '{"memories": [{"kind": "preference", "content": "Studies in the mornings."}]}'
EVENINGS = '{"memories": [{"kind": "preference", "content": "Studies in the evenings now."}]}'


async def _learner(session: AsyncSession) -> Learner:
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    session.add(learner)
    await session.commit()
    return learner


async def _conversation(session: AsyncSession, learner: Learner, text: str) -> Conversation:
    conversation = Conversation(learner_id=learner.id)
    session.add(conversation)
    await session.flush()
    session.add(Message(conversation_id=conversation.id, role="user", content=text))
    await session.commit()
    return conversation


async def _memories(session: AsyncSession, learner_id: uuid.UUID) -> list[Memory]:
    return list(
        (
            await session.scalars(
                select(Memory).where(Memory.learner_id == learner_id).order_by(Memory.created_at)
            )
        ).all()
    )


@contextlib.contextmanager
def _everything_is_equivalent():
    """Treat any same-kind memory as the one a new extraction is about.

    The fake provider derives embeddings from a hash, so "studies in the mornings" and
    "studies in the evenings now" land nowhere near each other — real embeddings of a
    preference and its correction would. Widening the threshold isolates what these tests are
    about (skip vs supersede vs suppress) from a distance the harness cannot make meaningful.
    """
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "app.services.memory.get_settings",
            lambda: Settings(memory_dedup_max_distance=2.0),
        )
        yield


def _seed(learner_id: uuid.UUID, content: str, *, status=MemoryStatus.CURRENT) -> Memory:
    return Memory(
        embedding_space=FAKE_SPACE,
        learner_id=learner_id,
        kind=MemoryKind.PREFERENCE,
        content=content,
        embedding=[0.1] * 768,
        status=status,
    )


# --- correction ---------------------------------------------------------------------------


async def test_a_correction_replaces_the_entry_it_contradicts(db_session: AsyncSession) -> None:
    """The stale entry used to win, because "close enough" meant "skip"."""
    learner = await _learner(db_session)
    first = await _conversation(db_session, learner, "I study in the mornings.")
    await svc.write_back(db_session, fake_llm_client(MORNINGS), conversation_id=first.id)
    second = await _conversation(db_session, learner, "Actually I study evenings now.")

    with _everything_is_equivalent():
        created = await svc.write_back(
            db_session, fake_llm_client(EVENINGS), conversation_id=second.id
        )

    assert [m.content for m in created] == ["Studies in the evenings now."]
    current = await svc.list_memories(db_session, learner.id)
    assert [m.content for m in current] == ["Studies in the evenings now."]


async def test_the_superseded_entry_is_kept_and_points_at_its_replacement(
    db_session: AsyncSession,
) -> None:
    """A bare overwrite would discard the fact that the learner corrected something."""
    learner = await _learner(db_session)
    first = await _conversation(db_session, learner, "I study in the mornings.")
    await svc.write_back(db_session, fake_llm_client(MORNINGS), conversation_id=first.id)
    second = await _conversation(db_session, learner, "Actually I study evenings now.")
    with _everything_is_equivalent():
        await svc.write_back(db_session, fake_llm_client(EVENINGS), conversation_id=second.id)

    rows = await _memories(db_session, learner.id)

    superseded = [m for m in rows if m.status == MemoryStatus.SUPERSEDED]
    current = [m for m in rows if m.status == MemoryStatus.CURRENT]
    assert len(superseded) == 1 and len(current) == 1
    assert superseded[0].superseded_by_id == current[0].id


async def test_an_identical_restatement_creates_nothing(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    first = await _conversation(db_session, learner, "I study in the mornings.")
    await svc.write_back(db_session, fake_llm_client(MORNINGS), conversation_id=first.id)
    second = await _conversation(db_session, learner, "As I said, mornings.")

    created = await svc.write_back(db_session, fake_llm_client(MORNINGS), conversation_id=second.id)

    assert created == []
    assert len(await svc.list_memories(db_session, learner.id)) == 1


# --- forgetting ---------------------------------------------------------------------------


async def test_a_deleted_memory_is_not_re_extracted(db_session: AsyncSession) -> None:
    """The defect: deleting a memory, then talking about it again, brought it straight back."""
    learner = await _learner(db_session)
    first = await _conversation(db_session, learner, "I study in the mornings.")
    created = await svc.write_back(db_session, fake_llm_client(MORNINGS), conversation_id=first.id)
    assert await svc.delete_memory(db_session, learner.id, created[0].id) is True
    second = await _conversation(db_session, learner, "Mornings, like I said.")

    again = await svc.write_back(db_session, fake_llm_client(MORNINGS), conversation_id=second.id)

    assert again == []
    assert await svc.list_memories(db_session, learner.id) == []


async def test_deleting_twice_is_a_no_op(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    memory = _seed(learner.id, "x")
    db_session.add(memory)
    await db_session.commit()

    assert await svc.delete_memory(db_session, learner.id, memory.id) is True
    assert await svc.delete_memory(db_session, learner.id, memory.id) is False


async def test_forget_everything_leaves_tombstones_not_holes(db_session: AsyncSession) -> None:
    """Hard-deleting in bulk is how the next write-back rebuilt what was just erased."""
    learner = await _learner(db_session)
    db_session.add_all([_seed(learner.id, "a"), _seed(learner.id, "b")])
    await db_session.commit()

    assert await svc.delete_all_memories(db_session, learner.id) == 2

    assert await svc.list_memories(db_session, learner.id) == []
    rows = await _memories(db_session, learner.id)
    assert len(rows) == 2
    assert all(m.status == MemoryStatus.DELETED for m in rows)


async def test_forgetting_everything_twice_counts_only_what_it_changed(
    db_session: AsyncSession,
) -> None:
    learner = await _learner(db_session)
    db_session.add(_seed(learner.id, "a"))
    await db_session.commit()

    assert await svc.delete_all_memories(db_session, learner.id) == 1
    assert await svc.delete_all_memories(db_session, learner.id) == 0


# --- retrieval ------------------------------------------------------------------------------


async def test_retrieval_ignores_superseded_and_deleted_entries(
    db_session: AsyncSession,
) -> None:
    learner = await _learner(db_session)
    db_session.add_all(
        [
            _seed(learner.id, "gone", status=MemoryStatus.DELETED),
            _seed(learner.id, "old", status=MemoryStatus.SUPERSEDED),
            _seed(learner.id, "live"),
        ]
    )
    await db_session.commit()

    hits = await retrieval.retrieve(
        db_session, fake_llm_client(), "when do they study", learner_id=learner.id
    )

    assert [h.content for h in hits] == ["live"]


async def test_a_memory_beyond_the_relevance_floor_is_not_injected(
    db_session: AsyncSession,
) -> None:
    """Without a floor, `limit` alone guarantees the nearest are returned, relevant or not."""
    learner = await _learner(db_session)
    db_session.add(_seed(learner.id, "irrelevant"))
    await db_session.commit()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "app.memory.retrieval.get_settings",
            lambda: Settings(memory_retrieval_max_distance=0.0),
        )
        hits = await retrieval.retrieve(
            db_session, fake_llm_client(), "something else entirely", learner_id=learner.id
        )

    assert hits == []
