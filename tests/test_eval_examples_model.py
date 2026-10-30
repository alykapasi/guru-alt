"""Evaluation examples are a learner's data like any other (S59)."""

import uuid
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.evaluation import AREAS, EvalExample, IntentRecord
from app.models.learner import Learner
from app.workers import eval_eligible


async def _learner(session: AsyncSession) -> Learner:
    tag = uuid.uuid4().hex[:8]
    learner = Learner(handle=f"l-{tag}", email=f"{tag}@example.com")
    session.add(learner)
    await session.flush()
    return learner


async def test_accounts_are_not_eligible_by_default(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    await db_session.refresh(learner)
    assert learner.eval_eligible is False


async def test_a_source_is_copied_once_per_area(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    source = uuid.uuid4()
    for _ in range(2):
        db_session.add(
            EvalExample(
                area="grading",
                learner_id=learner.id,
                source_kind="event",
                source_id=source,
                snapshot={},
                decision={},
            )
        )
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_erasing_the_account_removes_its_examples_and_intents(
    db_session: AsyncSession,
) -> None:
    learner = await _learner(db_session)
    db_session.add(
        EvalExample(
            area="intent",
            learner_id=learner.id,
            source_kind="intent",
            source_id=uuid.uuid4(),
            snapshot={},
            decision={},
        )
    )
    db_session.add(IntentRecord(learner_id=learner.id, stem="Q", message="A", intent="attempt"))
    await db_session.flush()
    learner_id = learner.id
    await db_session.execute(delete(Learner).where(Learner.id == learner_id))
    examples = await db_session.scalars(
        select(EvalExample).where(EvalExample.learner_id == learner_id)
    )
    intents = await db_session.scalars(
        select(IntentRecord).where(IntentRecord.learner_id == learner_id)
    )
    assert examples.all() == []
    assert intents.all() == []


def test_the_four_areas() -> None:
    assert AREAS == ("grading", "diagnosis", "intent", "source_support")


async def test_the_cli_sets_and_revokes_and_refuses_an_unknown_address(engine, monkeypatch) -> None:
    factory = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def make():
        async with factory() as session:
            yield session

    monkeypatch.setattr(eval_eligible, "SessionFactory", make)
    tag = uuid.uuid4().hex[:8]
    email = f"eval-{tag}@example.com"
    async with factory() as setup:
        setup.add(Learner(handle=f"eval-{tag}", email=email))
        await setup.commit()

    try:
        assert await eval_eligible.run(email, revoke=False) == 0
        async with factory() as check:
            row = await check.scalar(select(Learner).where(Learner.email == email))
            assert row is not None and row.eval_eligible is True
        assert await eval_eligible.run(email, revoke=True) == 0
        async with factory() as check:
            row = await check.scalar(select(Learner).where(Learner.email == email))
            assert row is not None and row.eval_eligible is False
        assert await eval_eligible.run("nobody@example.com", revoke=False) == 1
    finally:
        async with factory() as cleanup:
            await cleanup.execute(delete(Learner).where(Learner.email == email))
            await cleanup.commit()
