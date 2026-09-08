"""Accounting is not part of the work it pays for.

The property under test is the one the module exists for: a business transaction that
rolls back after a paid model call still leaves that call recorded. Money left the account
whether or not the work it bought survived, so the record has to survive too.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from app.llm.registry import ModelSpec
from app.llm.types import Usage
from app.models.chat import LLMCall
from app.services.llm_log import log_llm_call, set_accounting_session_factory

SPEC = ModelSpec(provider="anthropic", model="claude-sonnet-4-6")


@pytest_asyncio.fixture
async def independent_accounting(engine: AsyncEngine) -> AsyncIterator[async_sessionmaker]:
    """Real production wiring: accounting on its own connection, committing for real.

    The rows this writes are outside any test transaction, so they are deleted explicitly.
    Calls recorded here carry no learner — nothing else in the test is committed for them
    to reference, which is exactly the independence being demonstrated.
    """
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
            await cleanup.execute(delete(LLMCall).where(LLMCall.learner_id.is_(None)))
            await cleanup.commit()


async def test_a_rolled_back_transaction_still_leaves_the_paid_call_recorded(
    db_session: AsyncSession, independent_accounting: async_sessionmaker
) -> None:
    await log_llm_call(
        learner_id=None, role="smart", spec=SPEC, usage=Usage(input_tokens=900, output_tokens=100)
    )
    # The work the call paid for is abandoned — the caller's transaction goes away entirely.
    await db_session.rollback()

    async with independent_accounting() as fresh:
        calls = (await fresh.scalars(select(LLMCall).where(LLMCall.learner_id.is_(None)))).all()
    assert [(c.input_tokens, c.output_tokens) for c in calls] == [(900, 100)]


async def test_an_unpriced_call_is_recorded_with_no_cost_rather_than_zero(
    independent_accounting: async_sessionmaker,
) -> None:
    cost = await log_llm_call(
        learner_id=None,
        role="smart",
        spec=ModelSpec(provider="openrouter", model="some/unpriced-model"),
        usage=Usage(input_tokens=10, output_tokens=10),
    )
    assert cost is None

    async with independent_accounting() as fresh:
        call = (await fresh.scalars(select(LLMCall).where(LLMCall.learner_id.is_(None)))).one()
    assert call.cost_usd is None  # not 0.0 — we do not know what this cost
    assert call.input_tokens == 10


@pytest.mark.parametrize(
    ("provider", "model", "expected"),
    [("ollama", "llama3.2", 0.0), ("anthropic", "claude-sonnet-4-6", 3e-6)],
)
async def test_a_priced_call_records_its_cost(
    independent_accounting: async_sessionmaker, provider: str, model: str, expected: float
) -> None:
    cost = await log_llm_call(
        learner_id=None,
        role="fast",
        spec=ModelSpec(provider=provider, model=model),
        usage=Usage(input_tokens=1, output_tokens=0),
    )
    assert cost == pytest.approx(expected)


async def test_a_broken_accounting_write_does_not_fail_the_call_it_is_accounting_for() -> None:
    """The tokens are already spent and the work already succeeded; the turn must not die here.

    The structured `llm.call` event is emitted before the write, so the record still exists
    even when the row does not.
    """

    @asynccontextmanager
    async def broken() -> AsyncIterator[AsyncSession]:
        raise RuntimeError("accounting database is down")
        yield  # pragma: no cover - unreachable, present so this is a generator

    previous = set_accounting_session_factory(broken)
    try:
        with capture_logs() as logs:
            cost = await log_llm_call(
                learner_id=None,
                role="smart",
                spec=SPEC,
                usage=Usage(input_tokens=1_000_000, output_tokens=0),
            )
    finally:
        set_accounting_session_factory(previous)

    assert cost == pytest.approx(3.0)  # still computed and returned to the caller
    events = [entry["event"] for entry in logs]
    assert events == ["llm.call", "llm.call_not_recorded"]
