"""The history the S62 budgets and report run against is the size it says it is."""

import uuid

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat import Conversation, LLMCall, Message, Turn
from app.models.learner import Learner
from app.models.learning import LearnerKCState, LearningEvent
from app.models.lesson_plan import LessonPlan
from app.models.memory import Memory
from app.models.note import Note, NoteRevision
from app.models.source import Chunk, Source
from tests.history import SMALL, seed_history
from tests.querycount import count_queries


async def _learner(session: AsyncSession) -> Learner:
    learner = Learner(handle=f"h-{uuid.uuid4().hex[:8]}")
    session.add(learner)
    await session.flush()
    return learner


async def _count(session: AsyncSession, column, learner_id: uuid.UUID) -> int:
    return await session.scalar(select(func.count()).where(column == learner_id)) or 0


async def test_the_small_history_has_the_stated_shape(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    h = await seed_history(db_session, learner.id, SMALL)

    # +1: the empty practice conversation
    assert await _count(db_session, Conversation.learner_id, learner.id) == SMALL.conversations + 1
    messages = await db_session.scalar(
        select(func.count(Message.id))
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(Conversation.learner_id == learner.id)
    )
    assert messages == SMALL.conversations * SMALL.messages_per_conversation
    turns = await db_session.scalar(
        select(func.count(Turn.id))
        .join(Conversation, Conversation.id == Turn.conversation_id)
        .where(Conversation.learner_id == learner.id)
    )
    assert turns == (messages or 0) // 2
    assert await _count(db_session, LearningEvent.learner_id, learner.id) == SMALL.events
    kcs = SMALL.subjects * SMALL.topics_per_subject * SMALL.kcs_per_topic
    assert await _count(db_session, LearnerKCState.learner_id, learner.id) == kcs
    assert await _count(db_session, Memory.learner_id, learner.id) == SMALL.memories
    assert await _count(db_session, Source.learner_id, learner.id) == SMALL.sources
    chunks = await db_session.scalar(
        select(func.count(Chunk.id))
        .join(Source, Source.id == Chunk.source_id)
        .where(Source.learner_id == learner.id)
    )
    assert chunks == SMALL.sources * SMALL.chunks_per_source
    assert await _count(db_session, Note.learner_id, learner.id) == SMALL.topics_per_subject
    revisions = await db_session.scalar(
        select(func.count(NoteRevision.id))
        .join(Note, Note.id == NoteRevision.note_id)
        .where(Note.learner_id == learner.id)
    )
    assert revisions == SMALL.topics_per_subject * SMALL.note_revisions
    assert await _count(db_session, LLMCall.learner_id, learner.id) == SMALL.llm_calls
    plan = await db_session.scalar(
        select(LessonPlan).where(
            LessonPlan.learner_id == learner.id, LessonPlan.subject_id == h.subject_id
        )
    )
    assert plan is not None


def test_scaling_multiplies_history_but_not_the_graph() -> None:
    big = SMALL.scaled(4)
    assert big.conversations == 4 * SMALL.conversations
    assert big.messages_per_conversation == 4 * SMALL.messages_per_conversation
    assert big.events == 4 * SMALL.events
    assert big.kcs_per_topic == SMALL.kcs_per_topic
    assert big.subjects == SMALL.subjects


async def test_the_counter_counts_rows(db_session: AsyncSession) -> None:
    with count_queries(db_session) as counted:
        await db_session.execute(text("SELECT g FROM generate_series(1, 7) g"))
        await db_session.execute(text("SELECT 1 WHERE false"))
    assert len(counted) == 2
    assert counted.rows == 7
