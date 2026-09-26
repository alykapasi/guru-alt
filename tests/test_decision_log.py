"""Every Jev answer is recorded beside what today's model decided (S82).

The row is the whole evidence base for switching a question live, so the properties here are
the ones the report depends on: the baseline is kept, a request's cost is not double-counted
when two questions share it, no learner text is stored, and — like `llm_calls` — the row
survives the turn that paid for it rolling back.
"""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.learning.turn_read import ReadContext, TurnRead
from app.llm.decisions import (
    ChoiceAnswer,
    ChoiceQuestion,
    DecisionFailure,
    FailureKind,
    FakeDecisionClient,
    YesNoAnswer,
    YesNoQuestion,
)
from app.llm.pricing import price_usd
from app.llm.types import Usage
from app.models.decision import DecisionCall
from app.services.decision_log import record_decision
from app.services.llm_log import set_accounting_session_factory

INTENT_Q = ChoiceQuestion(instructions="i", options={"attempt": "a", "deferral": "d"})
CORRECT_Q = YesNoQuestion(instructions="c")
STATE = {"question": "What is velocity?", "learner_reply": "SECRET-LEARNER-WORDS"}
NOBODY = ReadContext(learner_id=None, conversation_id=None, item_id=None)


def _read(client: FakeDecisionClient) -> TurnRead:
    return TurnRead(
        client,
        {"intent": INTENT_Q, "fully_correct": CORRECT_Q},
        STATE,
        timeout_s=1.0,
        context=NOBODY,
    )


def test_jev_is_priced_per_input_token_with_free_output() -> None:
    cost = price_usd("typesafe", "jev-1.13.0", Usage(input_tokens=1_000_000, output_tokens=5))
    assert cost == pytest.approx(0.042)


def test_the_fake_decider_costs_nothing() -> None:
    assert price_usd("fake", "fake-decider", Usage(input_tokens=1_000, output_tokens=0)) == 0.0


async def test_a_shadow_intent_row_keeps_jev_and_the_baseline(db_session: AsyncSession) -> None:
    answer = ChoiceAnswer(
        label="attempt", probabilities={"attempt": 0.8, "deferral": 0.2}, confidence=0.8
    )
    read = _read(FakeDecisionClient({"intent": answer}))
    assert await read.answer("intent", deadline_s=1.0) == answer

    await record_decision(
        read=read,
        question="intent",
        mode="shadow",
        outcome=answer,
        used=False,
        baseline_intent="deferral",
    )

    row = (await db_session.scalars(select(DecisionCall))).one()
    assert (row.question, row.mode, row.status, row.answer) == (
        "intent",
        "shadow",
        "ok",
        "attempt",
    )
    assert row.probabilities == {"attempt": 0.8, "deferral": 0.2}
    assert row.confidence == pytest.approx(0.8)
    assert row.baseline_intent == "deferral"
    assert row.used is False
    assert row.request_id == read.request_id
    assert (row.provider, row.model, row.input_tokens, row.cost_usd) == (
        "fake",
        "fake-decider",
        100,
        0.0,
    )


async def test_a_yes_no_row_stores_the_probability_and_the_smart_score(
    db_session: AsyncSession,
) -> None:
    read = _read(FakeDecisionClient({"fully_correct": YesNoAnswer(probability=0.91)}))
    await read.answer("fully_correct", deadline_s=1.0)
    attempt = uuid.uuid4()

    await record_decision(
        read=read,
        question="fully_correct",
        mode="shadow",
        outcome=YesNoAnswer(probability=0.91),
        used=False,
        baseline_score=0.4,
        attempt_id=attempt,
    )

    row = (await db_session.scalars(select(DecisionCall))).one()
    assert row.answer is None
    assert row.probabilities == {"yes": pytest.approx(0.91)}
    assert row.confidence is None
    assert row.baseline_score == pytest.approx(0.4)
    assert row.attempt_id == attempt


async def test_a_failure_is_recorded_with_its_kind(db_session: AsyncSession) -> None:
    read = _read(FakeDecisionClient(failure=DecisionFailure(FailureKind.RATE_LIMITED)))
    outcome = await read.answer("intent", deadline_s=1.0)

    await record_decision(
        read=read,
        question="intent",
        mode="live",
        outcome=outcome,
        used=False,
        baseline_intent="attempt",
    )

    row = (await db_session.scalars(select(DecisionCall))).one()
    assert row.status == "rate_limited"
    assert row.answer is None and row.probabilities is None
    assert row.input_tokens is None  # a failed request reports no usage


async def test_a_request_still_running_when_recorded_has_unknown_tokens(
    db_session: AsyncSession,
) -> None:
    read = _read(FakeDecisionClient({}, delay_s=0.5))
    outcome = await read.answer("intent", deadline_s=0.01)
    assert outcome == DecisionFailure(FailureKind.TIMEOUT)

    await record_decision(read=read, question="intent", mode="live", outcome=outcome, used=False)

    row = (await db_session.scalars(select(DecisionCall))).one()
    assert row.status == "timeout"
    assert row.input_tokens is None and row.cost_usd is None and row.latency_ms is None
    await read.answer("intent", deadline_s=None)  # let the request finish before the loop closes


async def test_no_learner_text_is_stored(db_session: AsyncSession) -> None:
    read = _read(FakeDecisionClient({"fully_correct": YesNoAnswer(probability=0.5)}))
    await read.answer("fully_correct", deadline_s=1.0)

    await record_decision(
        read=read,
        question="fully_correct",
        mode="shadow",
        outcome=YesNoAnswer(probability=0.5),
        used=False,
    )

    row = (await db_session.scalars(select(DecisionCall))).one()
    stored = " ".join(str(getattr(row, c.key)) for c in DecisionCall.__table__.columns)
    assert "SECRET-LEARNER-WORDS" not in stored


@pytest_asyncio.fixture
async def independent_accounting(engine: AsyncEngine) -> AsyncIterator[async_sessionmaker]:
    """Production wiring: the row is written on its own connection and committed for real."""
    factory = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def make() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    previous = set_accounting_session_factory(make)
    try:
        yield factory
    finally:
        set_accounting_session_factory(previous)
        async with factory() as cleanup:
            await cleanup.execute(delete(DecisionCall).where(DecisionCall.learner_id.is_(None)))
            await cleanup.commit()


async def test_a_rolled_back_turn_still_leaves_the_request_recorded(
    db_session: AsyncSession, independent_accounting: async_sessionmaker
) -> None:
    read = _read(FakeDecisionClient({"fully_correct": YesNoAnswer(probability=0.5)}))
    await read.answer("fully_correct", deadline_s=1.0)

    await record_decision(
        read=read,
        question="fully_correct",
        mode="shadow",
        outcome=YesNoAnswer(probability=0.5),
        used=False,
    )
    await db_session.rollback()

    async with independent_accounting() as fresh:
        rows = (
            await fresh.scalars(select(DecisionCall).where(DecisionCall.learner_id.is_(None)))
        ).all()
    assert [r.question for r in rows] == ["fully_correct"]


async def test_a_broken_write_does_not_raise() -> None:
    @asynccontextmanager
    async def broken() -> AsyncIterator[AsyncSession]:
        raise RuntimeError("accounting database is down")
        yield  # pragma: no cover - unreachable, present so this is a generator

    read = _read(FakeDecisionClient({"fully_correct": YesNoAnswer(probability=0.5)}))
    await read.answer("fully_correct", deadline_s=1.0)
    previous = set_accounting_session_factory(broken)
    try:
        await record_decision(
            read=read,
            question="fully_correct",
            mode="shadow",
            outcome=YesNoAnswer(probability=0.5),
            used=False,
        )
    finally:
        set_accounting_session_factory(previous)
