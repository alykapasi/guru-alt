"""Diagnostic data keeps nothing pointing at a learner past its window (S61, V12)."""

import uuid
from datetime import timedelta

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat import Conversation, LLMCall, Turn, TurnStatus
from app.models.decision import DecisionCall
from app.models.learner import Learner
from app.models.ops import AlertTransition
from app.services import retention

WINDOW = timedelta(days=30)


async def _age(session: AsyncSession, model, row_id, days: int) -> None:
    await session.execute(
        update(model)
        .where(model.id == row_id)
        .values(created_at=func.now() - text(f"interval '{days} days'"))
    )


async def test_old_accounting_loses_its_learner_and_new_keeps_it(db_session: AsyncSession) -> None:
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    db_session.add(learner)
    await db_session.flush()
    old = LLMCall(learner_id=learner.id, role="fast", provider="fake", model="fake-1")
    new = LLMCall(learner_id=learner.id, role="fast", provider="fake", model="fake-1")
    db_session.add_all([old, new])
    await db_session.flush()
    await _age(db_session, LLMCall, old.id, 40)
    await db_session.commit()
    old_id, new_id = old.id, new.id

    report = await retention.expire_diagnostics(db_session, older_than=WINDOW)

    assert report.calls_anonymised >= 1
    old_row = await db_session.get(LLMCall, old_id, populate_existing=True)
    new_row = await db_session.get(LLMCall, new_id, populate_existing=True)
    assert old_row is not None and old_row.learner_id is None
    assert new_row is not None and new_row.learner_id == learner.id


async def test_old_finished_turns_go_and_pending_ones_stay(db_session: AsyncSession) -> None:
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    db_session.add(learner)
    await db_session.flush()
    conversation = Conversation(learner_id=learner.id)
    db_session.add(conversation)
    await db_session.flush()
    done = Turn(
        conversation_id=conversation.id, flow="tutor", content="hi", status=TurnStatus.COMPLETED
    )
    pending = Turn(
        conversation_id=conversation.id, flow="tutor", content="hi", status=TurnStatus.PENDING
    )
    recent = Turn(
        conversation_id=conversation.id, flow="tutor", content="hi", status=TurnStatus.FAILED
    )
    db_session.add_all([done, pending, recent])
    await db_session.flush()
    for turn in (done, pending):
        await _age(db_session, Turn, turn.id, 40)
    await db_session.commit()
    done_id, pending_id, recent_id = done.id, pending.id, recent.id

    await retention.expire_diagnostics(db_session, older_than=WINDOW)

    assert await db_session.get(Turn, done_id, populate_existing=True) is None
    assert await db_session.get(Turn, pending_id, populate_existing=True) is not None
    assert await db_session.get(Turn, recent_id, populate_existing=True) is not None


async def test_the_newest_row_per_alert_survives(db_session: AsyncSession) -> None:
    name = f"a-{uuid.uuid4().hex[:6]}"
    rows = [
        AlertTransition(name=name, firing=f, severity="warning", detail="d", action="a")
        for f in (True, False)
    ]
    db_session.add_all(rows)
    await db_session.flush()
    for row in rows:
        await _age(db_session, AlertTransition, row.id, 40)
    await db_session.commit()

    await retention.expire_diagnostics(db_session, older_than=WINDOW)

    left = list(
        await db_session.scalars(select(AlertTransition).where(AlertTransition.name == name))
    )
    assert len(left) == 1 and left[0].firing is False


async def test_old_decisions_lose_their_learner_and_new_keep_it(db_session: AsyncSession) -> None:
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    db_session.add(learner)
    await db_session.flush()

    def _decision() -> DecisionCall:
        return DecisionCall(
            request_id=uuid.uuid4(),
            learner_id=learner.id,
            question="intent",
            mode="shadow",
            provider="fake",
            model="fake-1",
            status="ok",
        )

    old, new = _decision(), _decision()
    db_session.add_all([old, new])
    await db_session.flush()
    await _age(db_session, DecisionCall, old.id, 40)
    await db_session.commit()
    old_id, new_id = old.id, new.id

    report = await retention.expire_diagnostics(db_session, older_than=WINDOW)

    assert report.decisions_anonymised >= 1
    old_row = await db_session.get(DecisionCall, old_id, populate_existing=True)
    new_row = await db_session.get(DecisionCall, new_id, populate_existing=True)
    assert old_row is not None and old_row.learner_id is None
    assert new_row is not None and new_row.learner_id == learner.id
