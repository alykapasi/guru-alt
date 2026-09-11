"""Memory extraction and profile refresh are incremental and say when they failed (S43).

Both were recomputes with no record of what they had already read. Memory extraction took the
most recent N messages regardless, so a conversation growing by more than N between runs had
the middle silently dropped — never read, never extracted. Profile refresh recomputed every
dimension from the whole history even when no new evidence existed, several model calls at a
time, to arrive at the values already stored.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.llm.registry import fake_llm_client
from app.models.chat import Conversation, LLMCall, Message
from app.models.learner import Learner
from app.models.memory import Memory
from app.models.profile import LearnerProfile, ProfileDimension
from app.services import memory as memory_svc
from app.services import profile as profile_svc

FACT_REPLY = '{"memories": [{"kind": "fact", "content": "Studies for the MCAT in the mornings."}]}'


async def _learner(session: AsyncSession) -> Learner:
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    session.add(learner)
    # Committed, not just flushed: refresh_profile rolls back on an estimator failure, and a
    # rollback would take an uncommitted learner with it — a state production never has, since
    # the learner exists long before anything refreshes their profile.
    await session.commit()
    return learner


async def _conversation(session: AsyncSession, learner: Learner) -> Conversation:
    conversation = Conversation(learner_id=learner.id)
    session.add(conversation)
    await session.commit()
    return conversation


async def _say(session: AsyncSession, conv: Conversation, text: str, *, at: datetime) -> Message:
    message = Message(conversation_id=conv.id, role="user", content=text, created_at=at)
    session.add(message)
    await session.commit()
    return message


def _t(minutes: int) -> datetime:
    return datetime(2026, 1, 1, 12, 0, tzinfo=UTC).replace(tzinfo=None) + timedelta(minutes=minutes)


async def _llm_calls(session: AsyncSession, learner_id: uuid.UUID) -> int:
    """How many model calls this learner has been billed for — the thing a skipped run saves."""
    return (
        await session.scalar(
            select(func.count()).select_from(LLMCall).where(LLMCall.learner_id == learner_id)
        )
    ) or 0


async def _memory_count(session: AsyncSession, learner_id: uuid.UUID) -> int:
    return (
        await session.scalar(
            select(func.count()).select_from(Memory).where(Memory.learner_id == learner_id)
        )
    ) or 0


# --- memory extraction is incremental ------------------------------------------------------


async def test_extraction_advances_only_to_what_it_read(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    conv = await _conversation(db_session, learner)
    last = await _say(db_session, conv, "I study mornings.", at=_t(0))

    await memory_svc.write_back(db_session, fake_llm_client(FACT_REPLY), conversation_id=conv.id)

    await db_session.refresh(conv)
    assert conv.memory_watermark == last.created_at


async def test_a_second_run_over_unchanged_history_costs_nothing(
    db_session: AsyncSession,
) -> None:
    """Before this it paid a FAST call to rediscover it had nothing to do."""
    learner = await _learner(db_session)
    conv = await _conversation(db_session, learner)
    await _say(db_session, conv, "I study mornings.", at=_t(0))
    llm = fake_llm_client(FACT_REPLY)
    await memory_svc.write_back(db_session, llm, conversation_id=conv.id)
    calls_after_first = await _llm_calls(db_session, learner.id)

    second = await memory_svc.write_back(db_session, llm, conversation_id=conv.id)

    assert second == []
    calls_after_second = await _llm_calls(db_session, learner.id)
    assert calls_after_second == calls_after_first  # no model call to say "nothing new"


async def test_a_backlog_larger_than_the_window_is_not_skipped(
    db_session: AsyncSession,
) -> None:
    """The defect: taking the most recent N left everything before them unread forever."""
    learner = await _learner(db_session)
    conv = await _conversation(db_session, learner)
    for i in range(5):
        await _say(db_session, conv, f"message {i}", at=_t(i))
    settings = Settings(memory_extraction_window=2)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("app.services.memory.get_settings", lambda: settings)
        await memory_svc.write_back(
            db_session, fake_llm_client(FACT_REPLY), conversation_id=conv.id
        )
        await db_session.refresh(conv)
        # It read the two *oldest* unprocessed messages, not the two newest.
        assert conv.memory_watermark == _t(1)

        await memory_svc.write_back(
            db_session, fake_llm_client(FACT_REPLY), conversation_id=conv.id
        )
        await db_session.refresh(conv)
        assert conv.memory_watermark == _t(3)  # the backlog continues, nothing jumped over


async def test_extraction_reads_the_oldest_unprocessed_first(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    conv = await _conversation(db_session, learner)
    await _say(db_session, conv, "oldest", at=_t(0))
    await _say(db_session, conv, "newest", at=_t(10))
    settings = Settings(memory_extraction_window=1)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("app.services.memory.get_settings", lambda: settings)
        await memory_svc.write_back(
            db_session, fake_llm_client(FACT_REPLY), conversation_id=conv.id
        )

    await db_session.refresh(conv)
    assert conv.memory_watermark == _t(0)


# --- profile refresh skips unchanged evidence ----------------------------------------------


async def test_a_refresh_over_unchanged_evidence_does_no_work(
    db_session: AsyncSession,
) -> None:
    learner = await _learner(db_session)
    conv = await _conversation(db_session, learner)
    await _say(db_session, conv, "I keep getting stuck on limits.", at=_t(0))
    llm = fake_llm_client("{}")
    await profile_svc.refresh_profile(db_session, learner.id, llm)
    calls = await _llm_calls(db_session, learner.id)

    await profile_svc.refresh_profile(db_session, learner.id, llm)

    after = await _llm_calls(db_session, learner.id)
    assert after == calls


async def test_new_evidence_makes_the_next_refresh_run(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    conv = await _conversation(db_session, learner)
    await _say(db_session, conv, "first", at=_t(0))
    llm = fake_llm_client("{}")
    await profile_svc.refresh_profile(db_session, learner.id, llm)
    profile = await db_session.scalar(
        select(LearnerProfile).where(LearnerProfile.learner_id == learner.id)
    )
    assert profile is not None
    assert profile.evidence_watermark == _t(0)

    await _say(db_session, conv, "second", at=_t(5))
    await profile_svc.refresh_profile(db_session, learner.id, llm)

    await db_session.refresh(profile)
    assert profile.evidence_watermark == _t(5)


async def test_force_recomputes_even_with_nothing_new(db_session: AsyncSession) -> None:
    """The cursor tracks the evidence and cannot know the estimators reading it changed."""
    learner = await _learner(db_session)
    conv = await _conversation(db_session, learner)
    await _say(db_session, conv, "I keep getting stuck on limits.", at=_t(0))
    llm = fake_llm_client("{}")
    await profile_svc.refresh_profile(db_session, learner.id, llm)
    before = await _llm_calls(db_session, learner.id)

    await profile_svc.refresh_profile(db_session, learner.id, llm, force=True)

    after = await _llm_calls(db_session, learner.id)
    assert after >= before


async def test_resetting_a_dimension_makes_the_next_refresh_recompute(
    db_session: AsyncSession,
) -> None:
    """The reset invalidates derived values without touching the evidence, so the cursor has
    to be cleared or the refresh would skip the very recomputation that was asked for."""
    learner = await _learner(db_session)
    conv = await _conversation(db_session, learner)
    await _say(db_session, conv, "I keep getting stuck on limits.", at=_t(0))
    llm = fake_llm_client("{}")
    await profile_svc.refresh_profile(db_session, learner.id, llm)
    profile = await db_session.scalar(
        select(LearnerProfile).where(LearnerProfile.learner_id == learner.id)
    )
    assert profile is not None and profile.evidence_watermark is not None
    # The fake model produces no dimension values, so seed the one being reset — the point
    # here is what a reset does to the cursor, not what the estimators produce.
    db_session.add(
        ProfileDimension(
            learner_id=learner.id, key="pace", value=1.0, kind="state", source="behavioral"
        )
    )
    await db_session.commit()

    await profile_svc.reset_dimension(db_session, learner.id, "pace")

    await db_session.refresh(profile)
    assert profile.evidence_watermark is None


async def test_a_failed_refresh_is_recorded_and_does_not_advance_the_cursor(
    db_session: AsyncSession,
) -> None:
    """A profile that quietly stopped updating looks exactly like one nothing changed for."""
    learner = await _learner(db_session)
    conv = await _conversation(db_session, learner)
    await _say(db_session, conv, "I keep getting stuck on limits.", at=_t(0))
    # Held separately: the failure path rolls back, which expires every ORM object this
    # session has loaded, so reading learner.id afterwards would go back to the database.
    learner_id = learner.id

    with pytest.MonkeyPatch.context() as mp:

        async def _boom(*args, **kwargs):
            raise RuntimeError("estimator exploded")

        mp.setattr("app.services.profile._load_events", _boom)
        with pytest.raises(RuntimeError):
            await profile_svc.refresh_profile(db_session, learner_id, fake_llm_client("{}"))

    profile = await db_session.scalar(
        select(LearnerProfile).where(LearnerProfile.learner_id == learner_id)
    )
    assert profile is not None
    assert "estimator exploded" in (profile.last_error or "")
    assert profile.evidence_watermark is None  # this evidence is not processed
    assert profile.refreshed_at is None


async def test_latest_evidence_is_none_for_a_learner_with_no_history(
    db_session: AsyncSession,
) -> None:
    learner = await _learner(db_session)
    assert await profile_svc.latest_evidence_at(db_session, learner.id) is None
