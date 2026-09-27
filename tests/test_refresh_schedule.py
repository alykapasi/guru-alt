"""Write-back and profile refresh run when a conversation or learner goes quiet (S43).

"Due" is read from the data on every pass, so a restart or a long absence is caught up by the
next pass without a catch-up job. A claim is a conditional update, so two workers never queue
the same item, and a failed item waits `refresh_retry_minutes` before it is tried again.
"""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.chat import Conversation, Message
from app.models.learner import Learner
from app.models.learning import LearningEvent
from app.models.profile import LearnerProfile
from app.services import refresh_schedule as sched

NOW = datetime(2026, 3, 1, 12, 0)
SETTINGS = Settings(memory_quiet_minutes=20, refresh_retry_minutes=60, refresh_batch_size=50)


def _ago(minutes: int) -> datetime:
    return NOW - timedelta(minutes=minutes)


async def _learner(session: AsyncSession, **kwargs) -> Learner:
    learner = Learner(handle=f"s-{uuid.uuid4().hex[:8]}", **kwargs)
    session.add(learner)
    await session.commit()
    return learner


async def _chat(
    session: AsyncSession, learner: Learner, *, said: datetime, **kwargs
) -> Conversation:
    conversation = Conversation(learner_id=learner.id, **kwargs)
    session.add(conversation)
    await session.flush()
    session.add(
        Message(conversation_id=conversation.id, role="user", content="hi", created_at=said)
    )
    await session.commit()
    return conversation


async def _claim(session: AsyncSession, now: datetime = NOW) -> sched.Claimed:
    return await sched.claim_due(session, now=now, settings=SETTINGS)


# --- conversations -------------------------------------------------------------------------


async def test_a_quiet_conversation_is_claimed_and_a_live_one_is_not(
    db_session: AsyncSession,
) -> None:
    learner = await _learner(db_session)
    quiet = await _chat(db_session, learner, said=_ago(30))
    live = await _chat(db_session, learner, said=_ago(5))

    claimed = await _claim(db_session)

    assert quiet.id in claimed.conversations
    assert live.id not in claimed.conversations


async def test_what_was_already_read_is_not_due(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    done = await _chat(db_session, learner, said=_ago(30), memory_watermark=_ago(30))

    assert done.id not in (await _claim(db_session)).conversations


async def test_an_administrators_message_makes_nothing_due(db_session: AsyncSession) -> None:
    """Review focus 3."""
    learner = await _learner(db_session)
    conversation = await _chat(db_session, learner, said=_ago(90), memory_watermark=_ago(90))
    db_session.add(
        Message(
            conversation_id=conversation.id,
            role="user",
            content="from a visit",
            created_at=_ago(30),
            admin_actor_id=uuid.uuid4(),
        )
    )
    # The profile has read the learner's own message; only the visit's is newer.
    db_session.add(LearnerProfile(learner_id=learner.id, evidence_watermark=_ago(90)))
    await db_session.commit()

    claimed = await _claim(db_session)

    assert conversation.id not in claimed.conversations
    assert learner.id not in claimed.learners  # its profile evidence did not move either


async def test_paused_closing_suspended_and_archived_are_left_alone(
    db_session: AsyncSession,
) -> None:
    paused = await _chat(
        db_session, await _learner(db_session, remember_conversations=False), said=_ago(30)
    )
    closing = await _chat(
        db_session,
        await _learner(db_session, deletion_requested_at=datetime.now(UTC)),
        said=_ago(30),
    )
    suspended = await _chat(
        db_session, await _learner(db_session, suspended_at=datetime.now(UTC)), said=_ago(30)
    )
    archived = await _chat(
        db_session, await _learner(db_session), said=_ago(30), archived_at=datetime.now(UTC)
    )

    claimed = (await _claim(db_session)).conversations

    assert not {paused.id, closing.id, suspended.id, archived.id} & set(claimed)


async def test_a_claim_waits_out_the_retry_window(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    conversation = await _chat(db_session, learner, said=_ago(30))
    assert conversation.id in (await _claim(db_session)).conversations

    # Still due (nothing ran), but claimed a minute ago: not again yet.
    assert (
        conversation.id not in (await _claim(db_session, NOW + timedelta(minutes=1))).conversations
    )
    # After the retry window it is claimed again.
    later = NOW + timedelta(minutes=61)
    assert conversation.id in (await _claim(db_session, later)).conversations


async def test_two_claims_in_the_same_instant_claim_once(db_session: AsyncSession) -> None:
    """Review focus 5: two workers polling together."""
    learner = await _learner(db_session)
    conversation = await _chat(db_session, learner, said=_ago(30))

    first = await _claim(db_session)
    second = await _claim(db_session)

    assert conversation.id in first.conversations
    assert conversation.id not in second.conversations


async def test_the_batch_takes_the_oldest_first(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    oldest = await _chat(db_session, learner, said=datetime(2000, 1, 1, 0, 0))
    older = await _chat(db_session, learner, said=datetime(2000, 1, 1, 0, 1))
    newest = await _chat(db_session, learner, said=datetime(2000, 1, 1, 0, 2))

    claimed = await sched.claim_due(
        db_session, now=NOW, settings=SETTINGS.model_copy(update={"refresh_batch_size": 2})
    )

    assert claimed.conversations == [oldest.id, older.id]
    assert newest.id not in claimed.conversations


# --- learners --------------------------------------------------------------------------------


async def test_a_learner_with_new_evidence_and_no_profile_is_claimed(
    db_session: AsyncSession,
) -> None:
    """Review focus 4: claiming creates the profile row it stamps."""
    learner = await _learner(db_session)
    db_session.add(
        LearningEvent(
            learner_id=learner.id,
            event_type="observation",
            payload={"score": 1.0},
            created_at=_ago(30),
        )
    )
    await db_session.commit()

    assert learner.id in (await _claim(db_session)).learners
    row = await db_session.scalar(
        select(LearnerProfile).where(LearnerProfile.learner_id == learner.id)
    )
    assert row is not None and row.refresh_attempted_at == NOW


async def test_a_learner_whose_profile_has_read_everything_is_not_due(
    db_session: AsyncSession,
) -> None:
    learner = await _learner(db_session)
    await _chat(db_session, learner, said=_ago(30))
    db_session.add(LearnerProfile(learner_id=learner.id, evidence_watermark=_ago(30)))
    await db_session.commit()

    assert learner.id not in (await _claim(db_session)).learners


async def test_a_learner_still_answering_is_not_due(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    await _chat(db_session, learner, said=_ago(5))

    assert learner.id not in (await _claim(db_session)).learners


# --- stuck -----------------------------------------------------------------------------------


async def test_work_due_for_hours_counts_as_stuck(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    await _chat(db_session, learner, said=NOW - timedelta(hours=7))
    fresh = await _learner(db_session)
    await _chat(db_session, fresh, said=_ago(30))
    settings = SETTINGS.model_copy(update={"refresh_stuck_hours": 6})

    # One conversation and one learner have been due for over six hours; the fresh ones not.
    assert await sched.stuck(db_session, now=NOW, settings=settings) == 2
