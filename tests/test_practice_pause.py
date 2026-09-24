"""Side discussions pause practice; resuming and skipping are explicit (S52, V07)."""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.learning.conversation_evidence import TurnIntent
from app.llm.providers.fake import FakeTurn
from app.llm.registry import fake_llm_client
from app.models.chat import Conversation, ConversationPhase
from app.models.knowledge import KC
from app.models.learning import LearnerKCState, LearningEvent
from app.services import practice
from app.services import workflow as workflow_svc
from tests.test_workflow import (
    PRESENT,
    RESPOND_2,
    RIGHT_GRADE,
    _conversation_with_active_step,
    _drain,
)


async def _events(session: AsyncSession, learner_id: uuid.UUID) -> list[LearningEvent]:
    return list(
        await session.scalars(select(LearningEvent).where(LearningEvent.learner_id == learner_id))
    )


async def _presented(session: AsyncSession, script: list[FakeTurn]):
    conv = await _conversation_with_active_step(session)
    llm = fake_llm_client(script=[FakeTurn(text=PRESENT), *script])
    await _drain(session, llm, conv, user_content="let's begin")
    conv.phase = ConversationPhase.AWAITING_ANSWER
    await session.commit()
    return conv, llm


async def _make_paused_question_stale(session: AsyncSession, conv: Conversation) -> None:
    """Copied from tests/test_workflow.py's
    test_a_paused_question_the_learner_has_since_outgrown_is_not_resumed: record enough
    ability on the practised KC that `checkpoints.paused_practice_is_current` says mastered."""
    kc = await session.scalar(select(KC).where(KC.slug == "photosynthesis"))
    assert kc is not None
    session.add(
        LearnerKCState(
            learner_id=conv.learner_id,
            kc_id=kc.id,
            ability=1.5,
            uncertainty=0.4,
            last_seen_at=datetime.now(UTC),
        )
    )
    await session.flush()


async def test_a_side_question_is_classified_as_a_deferral(db_session: AsyncSession) -> None:
    conv, llm = await _presented(db_session, [FakeTurn(text='{"intent": "deferral"}')])
    item_id = await workflow_svc.paused_item_id(
        llm, db_session, conv.id, learner_id=conv.learner_id
    )
    assert item_id is not None
    intent = await practice.classify_paused_message(
        db_session,
        llm,
        learner_id=conv.learner_id,
        conversation=conv,
        item_id=item_id,
        content="wait, what is chlorophyll?",
    )
    assert intent is TurnIntent.DEFERRAL


async def test_a_gate_failure_is_a_deferral(db_session: AsyncSession) -> None:
    conv, llm = await _presented(db_session, [FakeTurn(text="not json at all")])
    item_id = await workflow_svc.paused_item_id(
        llm, db_session, conv.id, learner_id=conv.learner_id
    )
    assert item_id is not None
    intent = await practice.classify_paused_message(
        db_session,
        llm,
        learner_id=conv.learner_id,
        conversation=conv,
        item_id=item_id,
        content="sunlight makes sugar",
    )
    assert intent is TurnIntent.DEFERRAL


async def test_pause_then_resume_returns_the_same_question(db_session: AsyncSession) -> None:
    conv, llm = await _presented(db_session, [])
    events_before = await _events(db_session, conv.learner_id)
    paused = await practice.pause(db_session, llm, learner_id=conv.learner_id, conversation=conv)
    assert paused.phase is ConversationPhase.PRACTICE_PAUSED
    assert conv.active_item_id is not None

    resumed = await practice.resume(db_session, llm, learner_id=conv.learner_id, conversation=conv)
    assert resumed.ended is False
    assert resumed.prompt == PRESENT
    assert resumed.item is not None and resumed.item.id == conv.active_item_id
    assert conv.phase == ConversationPhase.AWAITING_ANSWER
    assert await _events(db_session, conv.learner_id) == events_before


async def test_help_during_a_pause_counts_against_the_next_attempt(
    db_session: AsyncSession,
) -> None:
    conv, llm = await _presented(db_session, [FakeTurn(text=RIGHT_GRADE), FakeTurn(text=RESPOND_2)])
    await practice.pause(db_session, llm, learner_id=conv.learner_id, conversation=conv)
    conv.practice_scaffolds = 2  # two tutor replies during the side discussion (Task 6 counts them)
    await db_session.commit()
    await practice.resume(db_session, llm, learner_id=conv.learner_id, conversation=conv)

    await _drain(db_session, llm, conv, user_content="sunlight -> sugars", resume=True)
    observed = [
        e for e in await _events(db_session, conv.learner_id) if e.event_type == "observation"
    ]
    assert observed and all(e.payload["hints_used"] == 2 for e in observed)


async def test_skip_records_nothing_and_ends_practice(db_session: AsyncSession) -> None:
    conv, llm = await _presented(db_session, [])
    await practice.pause(db_session, llm, learner_id=conv.learner_id, conversation=conv)
    before = await _events(db_session, conv.learner_id)
    state = await practice.skip(db_session, llm, learner_id=conv.learner_id, conversation=conv)
    assert state.phase is ConversationPhase.CHATTING
    assert conv.active_item_id is None and conv.practice_scaffolds == 0
    assert (
        await workflow_svc.paused_item_id(llm, db_session, conv.id, learner_id=conv.learner_id)
        is None
    )
    assert await _events(db_session, conv.learner_id) == before


async def test_starting_again_after_a_skip_carries_no_old_help(db_session: AsyncSession) -> None:
    """Review focus 4."""
    conv, llm = await _presented(db_session, [])
    conv.practice_scaffolds = 3
    await db_session.commit()
    await practice.skip(db_session, llm, learner_id=conv.learner_id, conversation=conv)
    conv.practice_scaffolds = 3  # even if something left it set, a fresh start clears it
    await db_session.commit()
    await _drain(
        db_session, fake_llm_client(script=[FakeTurn(text=PRESENT)]), conv, user_content="again"
    )
    assert conv.practice_scaffolds == 0


async def test_resume_after_the_plan_moved_on_reports_that_practice_ended(
    db_session: AsyncSession,
) -> None:
    conv, llm = await _presented(db_session, [])
    await practice.pause(db_session, llm, learner_id=conv.learner_id, conversation=conv)
    # Make the paused question stale the way tests/test_workflow.py's
    # test_a_paused_question_the_learner_has_since_outgrown_is_not_resumed does.
    await _make_paused_question_stale(db_session, conv)
    state = await practice.resume(db_session, llm, learner_id=conv.learner_id, conversation=conv)
    assert state.ended is True
    assert conv.phase == ConversationPhase.CHATTING


async def test_resume_when_not_paused_is_a_conflict(db_session: AsyncSession) -> None:
    conv, llm = await _presented(db_session, [])
    with pytest.raises(practice.PracticeConflict):
        await practice.resume(db_session, llm, learner_id=conv.learner_id, conversation=conv)


async def test_pause_without_live_practice_is_a_conflict(db_session: AsyncSession) -> None:
    conv = await _conversation_with_active_step(db_session)
    with pytest.raises(practice.PracticeConflict):
        await practice.pause(
            db_session, fake_llm_client(), learner_id=conv.learner_id, conversation=conv
        )
