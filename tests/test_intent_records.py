"""The practice gate keeps its decision for accounts that may be sampled (S59)."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.learning.conversation_evidence import TurnIntent
from app.learning.turn_read import ReadContext
from app.models.evaluation import IntentRecord
from app.models.learner import Learner
from app.services import chat as chat_svc
from app.services import intent_records
from tests.test_conversation_evidence import GRADE_REPLY, _open_check, _role_client


async def _learner(session: AsyncSession, *, eligible: bool) -> Learner:
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}", eval_eligible=eligible)
    session.add(learner)
    await session.flush()
    return learner


async def _records(session: AsyncSession, learner_id: uuid.UUID) -> list[IntentRecord]:
    rows = await session.scalars(select(IntentRecord).where(IntentRecord.learner_id == learner_id))
    return list(rows.all())


async def test_an_eligible_learners_intent_is_kept(db_session: AsyncSession) -> None:
    learner = await _learner(db_session, eligible=True)
    await intent_records.record(
        ReadContext(learner_id=learner.id, conversation_id=None, item_id=None),
        stem="What is a basis?",
        message="A spanning independent set.",
        intent=TurnIntent.ATTEMPT,
    )
    assert [(r.stem, r.message, r.intent) for r in await _records(db_session, learner.id)] == [
        ("What is a basis?", "A spanning independent set.", "attempt")
    ]


async def test_nobody_elses_is(db_session: AsyncSession) -> None:
    learner = await _learner(db_session, eligible=False)
    await intent_records.record(
        ReadContext(learner_id=learner.id, conversation_id=None, item_id=None),
        stem="Q",
        message="A",
        intent=TurnIntent.DEFERRAL,
    )
    assert await _records(db_session, learner.id) == []


async def test_a_context_with_no_learner_records_nothing(db_session: AsyncSession) -> None:
    await intent_records.record(
        ReadContext(learner_id=None, conversation_id=None, item_id=None),
        stem="Q",
        message="A",
        intent=TurnIntent.ATTEMPT,
    )
    assert (await db_session.scalars(select(IntentRecord))).all() == []


async def test_the_gate_records_through_a_real_check(db_session: AsyncSession) -> None:
    learner, conversation, _ = await _open_check(db_session)
    learner.eval_eligible = True
    await db_session.flush()
    llm, _provider = _role_client(fast='{"intent": "attempt"}', smart=GRADE_REPLY)

    _, outcome = await chat_svc._resolve_check(
        db_session,
        llm,
        learner_id=learner.id,
        conversation=conversation,
        user_content="Velocity is speed with a direction.",
    )

    assert outcome is not None  # the turn itself is unchanged
    (record,) = await _records(db_session, learner.id)
    assert record.intent == "attempt"
    assert record.message == "Velocity is speed with a direction."
    assert record.conversation_id == conversation.id
