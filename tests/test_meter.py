"""The client records every model call itself: pending, then ok, failed or partial (S48)."""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from app.llm import ChatMessage, ChatRole, LLMClient, ModelRole
from app.llm.attribution import attributed
from app.llm.providers.fake import FakeProvider
from app.llm.registry import ModelSpec, fake_llm_client
from app.models.chat import LLMCall
from app.models.learner import Learner
from app.services.llm_log import set_accounting_session_factory

HELLO = [ChatMessage(role=ChatRole.USER, content="hello there")]


async def _learner(session: AsyncSession) -> uuid.UUID:
    learner = Learner(handle=f"m-{uuid.uuid4().hex[:8]}")
    session.add(learner)
    await session.commit()
    return learner.id


async def _rows(session: AsyncSession, learner_id: uuid.UUID) -> list[LLMCall]:
    return list(
        (
            await session.scalars(
                select(LLMCall)
                .where(LLMCall.learner_id == learner_id)
                .execution_options(populate_existing=True)
            )
        ).all()
    )


def _client(provider: FakeProvider) -> LLMClient:
    return LLMClient({"fake": provider}, {r: ModelSpec("fake", "fake-1") for r in ModelRole})


async def test_a_completion_is_recorded_with_what_paid_for_it(db_session: AsyncSession) -> None:
    learner_id = await _learner(db_session)
    with attributed(learner_id=learner_id, feature="chat_turn", request_id="req-1"):
        await fake_llm_client("hi").complete(ModelRole.FAST, HELLO, system="Be brief.")

    [row] = await _rows(db_session, learner_id)
    assert (row.status, row.feature, row.request_id, row.estimated) == (
        "ok",
        "chat_turn",
        "req-1",
        False,
    )
    assert row.prompt_hash is not None and len(row.prompt_hash) == 16
    assert row.app_version == "dev"


async def test_an_unattributed_call_says_so(db_session: AsyncSession) -> None:
    await fake_llm_client("hi").complete(ModelRole.FAST, HELLO)
    row = await db_session.scalar(select(LLMCall).order_by(LLMCall.created_at.desc()).limit(1))
    assert row is not None and row.feature == "unattributed"


async def test_a_failed_call_is_recorded_without_its_message(db_session: AsyncSession) -> None:
    class Down(FakeProvider):
        async def complete(self, *, model, messages, system=None, max_tokens=1024, tools=None):
            raise TimeoutError("learner said something private")

    learner_id = await _learner(db_session)
    with attributed(learner_id=learner_id, feature="chat_turn"), pytest.raises(TimeoutError):
        await _client(Down()).complete(ModelRole.FAST, HELLO)

    [row] = await _rows(db_session, learner_id)
    assert (row.status, row.error_kind, row.estimated, row.output_tokens) == (
        "failed",
        "TimeoutError",
        True,
        0,
    )
    assert row.input_tokens > 0


async def test_a_stream_closed_early_is_partial(db_session: AsyncSession) -> None:
    """Review focus 2: an abandoned stream must not stay pending."""
    learner_id = await _learner(db_session)
    llm = fake_llm_client("one two three four five six")
    with attributed(learner_id=learner_id, feature="chat_turn"):
        stream = llm.stream(ModelRole.SMART, HELLO)
        seen = [await anext(stream), await anext(stream)]
        await stream.aclose()

    assert len(seen) == 2
    [row] = await _rows(db_session, learner_id)
    assert (row.status, row.estimated) == ("partial", True)
    assert row.output_tokens > 0


async def test_a_finished_stream_is_ok(db_session: AsyncSession) -> None:
    learner_id = await _learner(db_session)
    with attributed(learner_id=learner_id, feature="chat_turn"):
        async for _chunk in fake_llm_client("one two").stream(ModelRole.SMART, HELLO):
            pass

    [row] = await _rows(db_session, learner_id)
    assert (row.status, row.estimated) == ("ok", False)
    assert row.first_token_ms is not None


async def test_an_embedding_is_recorded(db_session: AsyncSession) -> None:
    learner_id = await _learner(db_session)
    with attributed(learner_id=learner_id, feature="ingestion"):
        await fake_llm_client().embed(ModelRole.EMBED, ["a", "b"])

    [row] = await _rows(db_session, learner_id)
    assert (row.status, row.feature, row.output_tokens) == ("ok", "ingestion", 0)


async def test_broken_accounting_does_not_fail_the_call() -> None:
    @asynccontextmanager
    async def broken():
        raise RuntimeError("accounting database down")
        yield  # pragma: no cover

    previous = set_accounting_session_factory(broken)
    try:
        reply = await fake_llm_client("still answered").complete(ModelRole.FAST, HELLO)
    finally:
        set_accounting_session_factory(previous)
    assert reply.content == "still answered"


# --- carried over from the hand-written recorder (test_llm_log) ---------------------------


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


async def _only_unowned(factory: async_sessionmaker) -> LLMCall:
    async with factory() as fresh:
        return (await fresh.scalars(select(LLMCall).where(LLMCall.learner_id.is_(None)))).one()


async def test_a_rolled_back_transaction_still_leaves_the_paid_call_recorded(
    db_session: AsyncSession, independent_accounting: async_sessionmaker
) -> None:
    """Money left the account whether or not the work it bought survived."""
    await fake_llm_client("hi").complete(ModelRole.SMART, HELLO)
    await db_session.rollback()

    call = await _only_unowned(independent_accounting)
    assert call.status == "ok" and call.output_tokens > 0


@pytest.mark.parametrize(
    ("provider", "model", "expected"),
    [
        ("openrouter", "some/unpriced-model", None),  # not 0.0 — we do not know what it cost
        ("ollama", "llama3.2", 0.0),
        ("anthropic", "claude-sonnet-4-6", 3e-6 * 2 + 15e-6 * 1),
    ],
)
async def test_a_call_records_its_price_or_that_it_has_none(
    independent_accounting: async_sessionmaker, provider: str, model: str, expected: float | None
) -> None:
    llm = LLMClient(
        {provider: FakeProvider(reply="ok")}, {ModelRole.FAST: ModelSpec(provider, model)}
    )
    await llm.complete(ModelRole.FAST, [ChatMessage(role=ChatRole.USER, content="hello there")])

    call = await _only_unowned(independent_accounting)
    if expected is None:
        assert call.cost_usd is None
    else:
        assert call.cost_usd == pytest.approx(expected)


async def test_a_recorded_completion_carries_its_latency_and_no_first_token(
    independent_accounting: async_sessionmaker,
) -> None:
    await fake_llm_client("hello").complete(ModelRole.SMART, HELLO)

    call = await _only_unowned(independent_accounting)
    assert call.latency_ms is not None and call.first_token_ms is None


async def test_a_recorded_stream_carries_its_first_token_and_no_latency(
    independent_accounting: async_sessionmaker,
) -> None:
    async for _chunk in fake_llm_client("one two").stream(ModelRole.SMART, HELLO):
        pass

    call = await _only_unowned(independent_accounting)
    assert call.first_token_ms is not None and call.latency_ms is None


async def test_a_stream_with_no_text_records_no_first_token(db_session: AsyncSession) -> None:
    """A turn that only called a tool has no first token; 0 would claim it arrived instantly."""
    learner_id = await _learner(db_session)
    with attributed(learner_id=learner_id, feature="chat_turn"):
        async for _chunk in fake_llm_client("").stream(ModelRole.SMART, HELLO):
            pass

    [row] = await _rows(db_session, learner_id)
    assert row.status == "ok" and row.first_token_ms is None


async def test_latency_and_status_reach_the_structured_log_not_only_the_row(
    db_session: AsyncSession,
) -> None:
    """The log line survives a failed write, so what the row carries the line must carry too."""
    with attributed(feature="chat_turn"), capture_logs() as logs:
        await fake_llm_client("hello").complete(ModelRole.SMART, HELLO)

    call = next(entry for entry in logs if entry["event"] == "llm.call")
    assert call["latency_ms"] is not None
    assert (call["status"], call["feature"]) == ("ok", "chat_turn")


async def test_a_broken_accounting_write_is_logged(db_session: AsyncSession) -> None:
    @asynccontextmanager
    async def broken() -> AsyncIterator[AsyncSession]:
        raise RuntimeError("accounting database down")
        yield  # pragma: no cover

    previous = set_accounting_session_factory(broken)
    try:
        with capture_logs() as logs:
            await fake_llm_client("still answered").complete(ModelRole.FAST, HELLO)
    finally:
        set_accounting_session_factory(previous)

    events = [entry["event"] for entry in logs]
    assert "llm.call_not_recorded" in events and "llm.call" in events
