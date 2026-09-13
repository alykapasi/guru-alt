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


# --- latency (S48) ----------------------------------------------------------


async def test_the_client_times_the_call_so_no_service_has_to(db_session: AsyncSession) -> None:
    """Latency is measured at the one place every non-streaming call passes through.

    Measuring it at each call site would mean every service remembering to, and each of them
    free to time a different span — the prompt build, the parse, the commit. Timing in
    `LLMClient` makes "how long the provider took" mean one thing everywhere.
    """
    from app.llm import ModelRole
    from app.llm.registry import fake_llm_client

    client = fake_llm_client(reply="hello")
    response = await client.complete(ModelRole.SMART, [])

    assert response.usage.latency_ms is not None
    assert response.usage.latency_ms >= 0


async def test_embedding_calls_are_timed_too(db_session: AsyncSession) -> None:
    """Ingesting a document is the largest single embedding bill the product has, so the
    embedding path is exactly where an unexplained slowdown would hide."""
    from app.llm import ModelRole
    from app.llm.registry import fake_llm_client

    result = await fake_llm_client().embed(ModelRole.EMBED, ["a", "b"])

    assert result.usage.latency_ms is not None


async def test_a_recorded_call_carries_its_latency(
    independent_accounting: async_sessionmaker,
) -> None:
    await log_llm_call(
        learner_id=None,
        role="smart",
        spec=SPEC,
        usage=Usage(input_tokens=10, output_tokens=5, latency_ms=1234),
    )

    async with independent_accounting() as session:
        row = await session.scalar(select(LLMCall).where(LLMCall.learner_id.is_(None)))
    assert row is not None
    assert row.latency_ms == 1234


async def test_an_untimed_call_records_no_latency_rather_than_zero(
    independent_accounting: async_sessionmaker,
) -> None:
    """Same distinction `cost_usd` already draws. Zero would claim an instantaneous call;
    NULL says it was never measured, which is what a streaming or hand-built usage means."""
    await log_llm_call(
        learner_id=None, role="smart", spec=SPEC, usage=Usage(input_tokens=1, output_tokens=1)
    )

    async with independent_accounting() as session:
        row = await session.scalar(select(LLMCall).where(LLMCall.learner_id.is_(None)))
    assert row is not None
    assert row.latency_ms is None


async def test_latency_reaches_the_structured_log_not_only_the_row(
    independent_accounting: async_sessionmaker,
) -> None:
    """The log line is emitted before the write and survives a failed one, so anything the
    row carries and the line does not is lost exactly when it is most needed."""
    with capture_logs() as logs:
        await log_llm_call(
            learner_id=None,
            role="smart",
            spec=SPEC,
            usage=Usage(input_tokens=1, output_tokens=1, latency_ms=77),
        )

    call = next(entry for entry in logs if entry["event"] == "llm.call")
    assert call["latency_ms"] == 77
